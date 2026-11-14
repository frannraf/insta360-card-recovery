#!/usr/bin/env python3
"""One-command, resumable recovery of deleted Insta360 recordings from a card image.

Every stage skips work that is already done and only ever reads the card
image.  Stages, in order:

  index       build/cache the image's chunk index
  scan        marker scan for file headers + moov atoms
  catalog     pair headers with moov atoms (the file list + sample tables)
  numbers     camera file numbers and camera-local name times
  chains      strict cluster-chain walk of every file, capped retries of lost ones
  lrv         assemble + full-decode-validate every LRV with an unambiguous chain
  blinds      resolve blind clusters by trial decoding against the LRV
  lost        rebuild lost VID chains step by step, confirming weak steps by decoding
  vid         assemble + full-decode-validate every VID with an unambiguous chain
  autoreview  approve validated recordings whose VID and LRV agree picture-for-picture
  review      contact sheets for anything auto-review could not approve
  publish     publish approved labels into the outputs folder (hard links + SHA-256 manifest)
  inst        rebuild + install the Insta360 trailer (gyro, calibration, thumbnails)
  audit       byte-compare sampled clusters of every published file with the image

Default: all stages except "review".  Only one run per work folder at a time
(work/run.lock).  Configuration: recovery.json (see rconfig.py).

Usage:
  python run_recovery.py --image card1                 # everything, unattended
  python run_recovery.py --image card1 --stages vid,autoreview,publish,inst
  python run_recovery.py --image card1 --dry-run       # print the plan only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import CODE, FLUSH_CACHE, IMAGES, ON_WINDOWS, UTC_OFFSET, WORK, run_lock_path, sh_argv, sh_script  # noqa: E402

ALL = ["index", "scan", "catalog", "numbers", "chains", "lrv", "blinds", "lost", "vid",
       "autoreview", "review", "publish", "inst", "audit"]
DEFAULT = [s for s in ALL if s != "review"]
HEAVY = {"lrv", "blinds", "lost", "vid", "autoreview"}
PY = [sys.executable, "-I", "-u"]


class Runner:
    def __init__(self, args):
        self.a = args
        self.logdir = WORK / "logs"
        if not args.dry_run:
            self.logdir.mkdir(parents=True, exist_ok=True)
        self.flusher = None

    def run(self, stage: str, cmd: list[str], timeout: int | None = None) -> int:
        line = " ".join(str(c) for c in cmd)
        if self.a.dry_run:
            print(f"[{stage}] would run: {line}")
            return 0
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        log = self.logdir / f"{self.a.image}-{stage}-{ts}.log"
        print(f"[{stage}] {line}\n         log: {log}", flush=True)
        try:
            with open(log, "w") as fh:
                r = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout, cwd=CODE)
            rc = r.returncode
        except subprocess.TimeoutExpired:
            rc = -9
        tail = log.read_text(errors="replace").strip().splitlines()[-3:]
        print(f"[{stage}] exit {rc}; " + " | ".join(t[:140] for t in tail), flush=True)
        return rc

    def py(self, stage: str, script: str, *args: str, timeout: int | None = None) -> int:
        return self.run(stage, PY + [str(CODE / script), *args], timeout=timeout)

    # On WSL, multi-GB decodes fill the VM page cache until reads fail with
    # "Cannot allocate memory"; a root loop flushes it every 15 s meanwhile.
    def start_flush(self) -> None:
        if self.a.dry_run or not (ON_WINDOWS and FLUSH_CACHE) or self.flusher:
            return
        from rconfig import sh_path
        self.pidfile = WORK / "tmp" / "drop_cache_loop.pid"
        self.pidfile.parent.mkdir(parents=True, exist_ok=True)
        self.pidfile.unlink(missing_ok=True)
        self.flusher = subprocess.Popen(
            sh_argv(f"bash {sh_script('drop_cache_loop.sh')} '{sh_path(self.pidfile)}'", root=True),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop_flush(self) -> None:
        if self.flusher:
            try:  # stop exactly our loop (other loops on the machine keep running)
                pid = int(self.pidfile.read_text().split()[0])
                subprocess.run(sh_argv(f"kill {pid}", root=True), capture_output=True)
            except (OSError, ValueError, IndexError):
                pass
            self.flusher.terminate()
            self.flusher = None


def catalog(image: str) -> list[dict]:
    return json.loads((WORK / "scan-analysis" / f"{image}-full-catalog.json").read_text())["files"]


def oom_failures(image: str) -> list[str]:
    """Validated labels whose decode was cut short by a memory error even after
    the validator's own retries: the file is not at fault, validate again."""
    bad = []
    for f in catalog(image):
        lab = f"{f['kind']}_{f['name_time']}"
        res = WORK / "validation" / lab / "result.txt"
        if res.exists():
            txt = res.read_text()
            if "oom_retries" in txt and any("decode_error_lines=0" not in l
                                            for l in txt.splitlines() if l.startswith(("v", "a"))):
                bad.append(lab)
    return bad


def validate(R: Runner, st: str, img: str, ch: str, kind: str, a) -> None:
    R.py(st, "pipeline.py", "--image", img, "--chains", ch, "--auto", kind, "--min-free-gb", str(a.min_free_gb))
    if a.dry_run:
        return
    retry = [l for l in oom_failures(img) if l.startswith(kind + "_")]
    if retry:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        for lab in retry:  # keep the cut-short run for the record, then validate again
            shutil.move(str(WORK / "validation" / lab), str(WORK / "validation" / f"{lab}.oom-{stamp}"))
        print(f"[{st}] re-validating {len(retry)} labels cut short by memory errors", flush=True)
        R.py(st, "pipeline.py", "--image", img, "--chains", ch, "--min-free-gb", str(a.min_free_gb), *retry)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", required=True, help="image name from recovery.json")
    ap.add_argument("--chains", default=None, help="chain set name under work/chains (default: the image name)")
    ap.add_argument("--stages", default=",".join(DEFAULT))
    ap.add_argument("--min-free-gb", type=float, default=50.0, help="never assemble below this much free space")
    ap.add_argument("--scan-procs", type=int, default=3)
    ap.add_argument("--retry-timeout", type=int, default=1500, help="seconds per lost-chain retry in 'chains'")
    ap.add_argument("--lost-timeout", type=int, default=6 * 3600, help="seconds per lost VID in 'lost'")
    ap.add_argument("--utc-offset", type=float, default=None,
                    help="camera clock offset from UTC in hours (default: recovery.json utc_offset, else 0); "
                         "per-recording trailer timestamps override it")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-lock", action="store_true", help="remove a stale work/run.lock")
    a = ap.parse_args()
    if a.image not in IMAGES:
        ap.error(f"unknown image {a.image!r}; recovery.json lists {sorted(IMAGES)}")
    a.chains = a.chains or a.image
    utc = a.utc_offset if a.utc_offset is not None else UTC_OFFSET.get(a.image, 0.0)
    stages = [s.strip() for s in a.stages.split(",") if s.strip()]
    bad = [s for s in stages if s not in ALL]
    if bad:
        ap.error(f"unknown stages {bad}")

    lock = run_lock_path()
    if not a.dry_run:
        from rconfig import ensure_work_dirs
        ensure_work_dirs()
        if lock.exists() and not a.force_lock:
            raise SystemExit(f"{lock} exists ({lock.read_text().strip()}): another run is active. "
                             "If it is not, rerun with --force-lock.")
        lock.write_text(f"pid {os.getpid()} image {a.image} since {dt.datetime.now():%Y-%m-%d %H:%M}")
    R = Runner(a)
    img, ch = a.image, a.chains
    sa = WORK / "scan-analysis"
    try:
        for st in [s for s in ALL if s in stages]:
            (R.start_flush if st in HEAVY else R.stop_flush)()
            if st == "index":
                R.py(st, "rdr_reader.py", img)
            elif st == "scan":
                if (WORK / "scan-results" / f"{img}-markers-py.json").exists():
                    print("[scan] markers already present, skipping")
                else:
                    R.py(st, "scan_markers_py.py", img, str(a.scan_procs))
            elif st == "catalog":
                if (sa / f"{img}-full-catalog.json").exists():
                    print("[catalog] catalog already present, skipping")
                else:
                    R.py(st, "build_catalog_from_markers.py", img)
            elif st == "numbers":
                R.py(st, "infer_file_numbers.py", img)
                R.py(st, "set_local_time.py", img, str(utc))
            elif st == "chains":
                R.py(st, "batch_chains.py", "--image", img, "--outdir", ch, "--no-relaxed")
                R.py(st, "retry_lost.py", img, ch, "--timeout", str(a.retry_timeout))
            elif st == "lrv":
                validate(R, st, img, ch, "LRV", a)
            elif st == "blinds":
                R.py(st, "blind_stage.py", "--image", img, "--chains", ch)
            elif st == "lost":
                cdir = WORK / "chains" / ch
                labs = sorted(p.name[:-len(".partial.json")] for p in cdir.glob("VID_*.partial.json")) \
                    if cdir.exists() else ["<each VID_*.partial.json>"]
                for lab in labs:
                    R.py(st, "rebuild_lost.py", ch, lab, timeout=a.lost_timeout)
            elif st == "vid":
                validate(R, st, img, ch, "VID", a)
            elif st == "autoreview":
                R.py(st, "auto_review.py", "--image", img, "--chains", ch)
            elif st == "review":
                R.py(st, "review_sheets.py", "--image", img)
            elif st == "publish":
                appr = WORK / "review" / f"approved-{img}.txt"
                labels = [l.partition("#")[0].strip() for l in appr.read_text().splitlines()] if appr.exists() else []
                labels = [l for l in labels if l]
                if not labels:
                    print(f"[publish] nothing approved in {appr}")
                    continue
                for i in range(0, len(labels), 25):
                    R.py(st, "deliver_batch.py", f"--image={img}", f"--chains={ch}", *labels[i:i + 25])
            elif st == "inst":
                R.py(st, "inst_publish.py", img, ch)
                # trailers give exact camera timestamps: correct the names
                R.py(st, "set_local_time.py", img, str(utc), "--rename")
            elif st == "audit":
                R.py(st, "audit_published.py")
        R.stop_flush()
        if not a.dry_run:
            R.py("status", "recovery_status.py", f"--chains={img}={ch}")
    finally:
        R.stop_flush()
        if not a.dry_run:
            lock.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
