#!/usr/bin/env python3
"""Automatic visual gate: compare each validated recording with its companion.

The camera records every clip twice at the same time: the full-resolution VID
(two fisheye video streams) and a low-resolution LRV preview (both fisheyes
side by side).  The two are recovered independently from different clusters,
so agreement between them is strong evidence that the picture is right.

For every clean, validated, unpublished label (decode exit 0, zero error
lines, full frame counts), frames are taken at POINTS evenly spaced times.
Each VID lens frame is scaled to 416x416 grey; each LRV half (832x832) is
scaled the same way; a lens matches when its best pairing with either LRV half
reaches MIN_DB PSNR.  A label is approved when the median point reaches
MEDIAN_DB, at most MAX_WEAK points fall below MIN_DB (fast camera spins put
the two copies a frame apart) and no point falls below FLOOR_DB (a wrong
picture), and the result is appended to
work/review/approved-<image>.txt as "LABEL # auto: ...".  LRV labels are
checked against their VID the same way.  Labels without a usable companion, or
that fail, are listed in work/review/needs-eyes-<image>.txt for a person to
look at (contact sheets: run_recovery.py --stages review).

Night clips and covered lenses compare fine: both copies are equally dark.
Read-only on the media; writes only under work/review and work/tmp.

Usage: auto_review.py --image NAME [--chains DIR] [--labels L ...] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import math
import operator
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import OUT, WORK, sh_run  # noqa: E402
from pipeline import final_name, validation_complete  # noqa: E402

POINTS = 8
MIN_DB = 24.0      # a lens/point below this is a weak match ...
MAX_WEAK = 2       # ... allowed at up to this many points (fast camera spins,
                   # where VID and LRV frames differ by a frame or two)
FLOOR_DB = 18.0    # but never below this: wrong pictures score 8-18 dB
MEDIAN_DB = 28.0   # typical match is 30-40 dB
SIDE = 416
FRAME = SIDE * SIDE


def psnr(a: bytes, b: bytes) -> float:
    se = sum(d * d for d in map(operator.sub, a, b))
    return 99.0 if se == 0 else 10 * math.log10(255 * 255 * FRAME / se)


def clean(label: str, entry: dict) -> bool:
    txt = (WORK / "validation" / label / "result.txt").read_text()
    vexp = [t[4] for t in entry["tracks"] if t[0] == "vide"]
    for line in txt.splitlines():
        key = line.split(" ", 1)[0]
        if not (key[:1] in "va" and key[1:].isdigit()):
            continue
        m = dict(x.split("=") for x in line.split()[1:] if "=" in x)
        if m.get("decode_exit") != "0" or m.get("decode_error_lines") != "0":
            return False
        if key[0] == "v" and int(m.get("decoded_frames", -1)) != vexp[int(key[1:])]:
            return False
    return True


def grab(rel_vid: str, rel_lrv: str, times: list[float], nvid: int, tag: str) -> dict:
    """Extract grey frames with one bash call; returns {(src, idx, t): bytes}."""
    tmp = WORK / "tmp" / "auto_review"
    tmp.mkdir(parents=True, exist_ok=True)
    for f in tmp.glob(f"{tag}-*.gray"):
        f.unlink()
    cmds = []
    for ti, t in enumerate(times):
        for v in range(nvid):
            cmds.append(f"ffmpeg -nostdin -v error -ss {t:.3f} -i '{rel_vid}' -map 0:v:{v} -frames:v 1 "
                        f"-vf scale={SIDE}:{SIDE},format=gray -f rawvideo -y 'tmp/auto_review/{tag}-v{v}-{ti}.gray'")
        for h, x in (("L", 0), ("R", 832)):
            cmds.append(f"ffmpeg -nostdin -v error -ss {t:.3f} -i '{rel_lrv}' -map 0:v:0 -frames:v 1 "
                        f"-vf crop=832:832:{x}:0,scale={SIDE}:{SIDE},format=gray -f rawvideo -y "
                        f"'tmp/auto_review/{tag}-{h}-{ti}.gray'")
    sh_run(" ; ".join(cmds), capture_output=True, text=True)
    out = {}
    for f in tmp.glob(f"{tag}-*.gray"):
        src, idx = f.stem.split("-")[1:]
        b = f.read_bytes()
        if len(b) == FRAME:
            out[(src, int(idx))] = b
        f.unlink()
    return out


def compare(vid_rel: str, lrv_rel: str, duration: float, nvid: int, tag: str) -> dict:
    times = [duration * (0.03 + 0.94 * i / (POINTS - 1)) for i in range(POINTS)]
    fr = grab(vid_rel, lrv_rel, times, nvid, tag)
    per_point, missing = [], 0
    for ti in range(POINTS):
        lenses = []
        for v in range(nvid):
            a = fr.get((f"v{v}", ti))
            refs = [fr.get((h, ti)) for h in "LR"]
            if a is None or not all(refs):
                missing += 1
                continue
            lenses.append(max(psnr(a, r) for r in refs))
        if lenses:
            per_point.append(min(lenses))
    return {"points": len(per_point), "missing_frames": missing,
            "min_db": round(min(per_point), 2) if per_point else 0.0,
            "median_db": round(statistics.median(per_point), 2) if per_point else 0.0,
            "per_point_db": [round(x, 1) for x in per_point]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--chains", default=None)
    ap.add_argument("--labels", nargs="*", default=None, help="only these labels")
    ap.add_argument("--include-published", action="store_true", help="also score published labels (calibration)")
    ap.add_argument("--dry-run", action="store_true", help="score only; do not write approvals")
    a = ap.parse_args()
    sa = WORK / "scan-analysis"
    files = json.loads((sa / f"{a.image}-full-catalog.json").read_text())["files"]
    nf = sa / f"{a.image}-file-numbers.json"
    numbers = json.loads(nf.read_text()) if nf.exists() else {}
    by_label = {f"{f['kind']}_{f['name_time']}": f for f in files}
    published = set()
    man = OUT / "MANIFEST.csv"
    if man.exists():
        import csv
        published = {r["file"] for r in csv.DictReader(open(man, newline=""))}
    rd = WORK / "review"
    rd.mkdir(parents=True, exist_ok=True)
    appr = rd / f"approved-{a.image}.txt"
    already = {l.partition("#")[0].strip() for l in appr.read_text().splitlines()} if appr.exists() else set()
    results, approved, eyes = {}, [], []
    labels = a.labels or sorted(by_label)
    for lab in labels:
        f = by_label.get(lab)
        if f is None or f["duration"] < 2.0 or not validation_complete(lab):
            continue
        name = final_name(f, numbers)
        if (name in published and not a.include_published) or (lab in already and not a.labels):
            continue
        if not clean(lab, f):
            continue
        other_kind = "LRV" if f["kind"] == "VID" else "VID"
        other = by_label.get(f"{other_kind}_{f['name_time']}")
        if other is None or not validation_complete(f"{other_kind}_{f['name_time']}") or \
                not clean(f"{other_kind}_{f['name_time']}", other):
            results[lab] = {"verdict": "needs-eyes", "why": f"no clean {other_kind} companion"}
            eyes.append(lab)
            continue
        vid, lrv = (f, other) if f["kind"] == "VID" else (other, f)
        vrel = f"assembled/{final_name(vid, numbers)}"
        lrel = f"assembled/{final_name(lrv, numbers)}"
        nvid = sum(1 for t in vid["tracks"] if t[0] == "vide")
        r = compare(vrel, lrel, min(vid["duration"], lrv["duration"]), nvid, lab)
        weak = sum(1 for x in r["per_point_db"] if x < MIN_DB)
        ok = (r["points"] == POINTS and not r["missing_frames"] and r["median_db"] >= MEDIAN_DB
              and weak <= MAX_WEAK and r["min_db"] >= FLOOR_DB)
        r["verdict"] = "approved" if ok else "needs-eyes"
        results[lab] = r
        (approved if ok else eyes).append(lab)
        print(json.dumps({"label": lab, **r}), flush=True)
    stamp = WORK / "review" / f"auto-{a.image}.json"
    old = json.loads(stamp.read_text()) if stamp.exists() else {}
    old.update(results)
    stamp.write_text(json.dumps(old, indent=1))
    if not a.dry_run:
        with open(appr, "a") as fh:
            for lab in approved:
                if lab not in already:
                    r = results[lab]
                    fh.write(f"{lab} # auto: matches companion at {r['points']} points, "
                             f"min {r['min_db']} dB, median {r['median_db']} dB\n")
        (rd / f"needs-eyes-{a.image}.txt").write_text("\n".join(eyes) + ("\n" if eyes else ""))
    print(json.dumps({"approved": len(approved), "needs_eyes": len(eyes), "dry_run": a.dry_run}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
