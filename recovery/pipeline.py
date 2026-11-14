#!/usr/bin/env python3
"""Assemble + validate reconstructed files from complete chain JSONs.

For each label: assemble into work/assembled/<final name> (never overwrites),
then run the WSL validation (full per-stream decode, frame counts, frames,
contact sheet) into work/validation/<label>.  Keeps >= --min-free-gb free.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402
from rconfig import CODE, SH_WORK as WSL_WORK, sh_argv, sh_script  # noqa: E402
VALIDATE, SHEET = sh_script("validate_media2.sh"), sh_script("make_sheet.sh")


def validation_complete(label: str) -> bool:
    """True only when every probed stream reported a decode result and the
    contact sheet exists (an interrupted run leaves a partial directory)."""
    d = WORK / "validation" / label
    res, probe = d / "result.txt", d / "probe.json"
    if not (res.exists() and probe.exists() and (d / "sheet.jpg").exists()):
        return False
    try:
        streams = json.loads(probe.read_text()).get("streams", [])
    except ValueError:
        return False
    nv = sum(1 for s in streams if s.get("codec_type") == "video")
    na = sum(1 for s in streams if s.get("codec_type") == "audio")
    txt = res.read_text()
    need = [f"v{i} " for i in range(nv)] + [f"a{i} " for i in range(na)]
    return bool(need) and all(any(l.startswith(n) and "decoded_frames=" in l for l in txt.splitlines()) for n in need)


def final_name(entry: dict, numbers: dict) -> str:
    if entry.get("name"):
        return entry["name"]
    n, _how = numbers.get(entry["name_time"], (None, ""))
    num = f"{n:03d}" if n is not None else "unk"
    t = entry.get("local_time") or entry["name_time"]  # camera-local naming time
    if entry["kind"] == "VID":
        return f"VID_{t}_00_{num}.insv"
    return f"LRV_{t}_01_{num}.lrv"


def companion_path(image: str, label: str, kind: str = "LRV"):
    """Assembled file of the recording's companion (VID <-> LRV), found via the
    catalog: file names use camera-local time, labels do not."""
    sa = WORK / "scan-analysis"
    cat = json.loads((sa / f"{image}-full-catalog.json").read_text())["files"]
    nf = sa / f"{image}-file-numbers.json"
    numbers = json.loads(nf.read_text()) if nf.exists() else {}
    t = label.split("_", 1)[1]
    e = next((f for f in cat if f["kind"] == kind and f["name_time"] == t), None)
    if e is None:
        return None
    path = WORK / "assembled" / final_name(e, numbers)
    return path if path.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("labels", nargs="*")
    ap.add_argument("--image", required=True)
    ap.add_argument("--min-free-gb", type=float, default=100.0)
    ap.add_argument("--no-validate", action="store_true")
    ap.add_argument("--chains", default=None, help="chain dir under work/chains (default: image)")
    ap.add_argument("--auto", default=None, help="kinds to select automatically, e.g. VID or LRV,VID")
    ap.add_argument("--max-files", type=int, default=10**6)
    args = ap.parse_args()

    cat = json.loads((WORK / "scan-analysis" / f"{args.image}-full-catalog.json").read_text())["files"]
    nf = WORK / "scan-analysis" / f"{args.image}-file-numbers.json"
    numbers = json.loads(nf.read_text()) if nf.exists() else {}
    by_label = {f"{f['kind']}_{f['name_time']}": f for f in cat}
    if args.auto:
        cdir = WORK / "chains" / (args.chains or args.image)
        sel = []
        for lab, f in by_label.items():
            if f["kind"] not in args.auto.split(",") or f["duration"] < 2.0:
                continue
            cp = cdir / f"{lab}.json"
            if not cp.exists() or validation_complete(lab):
                continue
            m = json.loads(cp.read_text())["mapping"]
            if any(x["how"] in ("blind-ambiguous", "blind-gapfill") for x in m):
                continue
            sel.append((0 if f.get("name") else 1, f["file_size"], lab))
        args.labels = [lab for *_x, lab in sorted(sel)][: args.max_files]
        print("auto-selected", len(args.labels), flush=True)
    (WORK / "assembled").mkdir(parents=True, exist_ok=True)
    (WORK / "validation").mkdir(parents=True, exist_ok=True)
    for label in args.labels:
        entry = by_label[label]
        chain = WORK / "chains" / (args.chains or args.image) / f"{label}.json"
        if not chain.exists():
            print(f"{label}: no complete chain", flush=True)
            continue
        name = final_name(entry, numbers)
        out = WORK / "assembled" / name
        if not out.exists():
            free = shutil.disk_usage(WORK).free / 1e9
            need = entry["file_size"] / 1e9
            if free - need < args.min_free_gb:
                print(f"{label}: skipped, free {free:.0f} GB - {need:.1f} GB < {args.min_free_gb} GB floor", flush=True)
                continue
            r = subprocess.run([sys.executable, "-I", str(CODE / "assemble_chain.py"), str(chain),
                                "--out", str(out), "--allow-blind"], capture_output=True, text=True)
            if r.returncode:
                print(f"{label}: assemble failed {r.stdout} {r.stderr}", flush=True)
                continue
        vdir = WORK / "validation" / label
        if args.no_validate or validation_complete(label):
            print(f"{label}: assembled {name}", flush=True)
            continue
        t = time.time()
        rel = out.relative_to(WORK).as_posix()
        cmd = (f"cd '{WSL_WORK}' && bash {VALIDATE} 'assembled/{name}' 'validation/{label}' "
               f"> 'validation/{label}.log' 2>&1; bash {SHEET} 'validation/{label}' '{name}' >/dev/null 2>&1; "
               f"cat 'validation/{label}/result.txt'")
        r = subprocess.run(sh_argv(cmd), capture_output=True, text=True)
        res = " | ".join(l for l in r.stdout.splitlines() if l and not l.startswith("file="))
        print(f"{label}: {name} validated in {time.time()-t:.0f}s :: {res}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
