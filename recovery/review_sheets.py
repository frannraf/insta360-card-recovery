#!/usr/bin/env python3
"""Contact sheets for validated recordings that still need a person's look.

Lists every clean, validated, unpublished label that is not yet approved
(typically what auto_review.py put in needs-eyes-<image>.txt), and stacks
their contact sheets (rows = lenses, columns = beginning/middle/end) ten per
image into work/review/<image>-sheets-NNN.jpg.  After looking, add approved
labels (one per line) to work/review/approved-<image>.txt and run the
publish,inst stages.

Usage: review_sheets.py --image NAME
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import final_name, validation_complete  # noqa: E402
from rconfig import MANIFEST, WORK, sh_run, sh_script  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    a = ap.parse_args()
    sa = WORK / "scan-analysis"
    files = json.loads((sa / f"{a.image}-full-catalog.json").read_text())["files"]
    nf = sa / f"{a.image}-file-numbers.json"
    numbers = json.loads(nf.read_text()) if nf.exists() else {}
    pub = {r["file"] for r in csv.DictReader(open(MANIFEST, newline=""))} if MANIFEST.exists() else set()
    rd = WORK / "review"
    rd.mkdir(parents=True, exist_ok=True)
    appr = rd / f"approved-{a.image}.txt"
    approved = {l.partition("#")[0].strip() for l in appr.read_text().splitlines()} if appr.exists() else set()
    pending, failed = [], []
    for f in files:
        lab = f"{f['kind']}_{f['name_time']}"
        if f["duration"] < 2.0 or lab in approved or not validation_complete(lab) or final_name(f, numbers) in pub:
            continue
        txt = (WORK / "validation" / lab / "result.txt").read_text()
        ok = all("decode_exit=0" in l and "decode_error_lines=0" in l
                 for l in txt.splitlines() if l.startswith(("v", "a")))
        (pending if ok else failed).append(lab)
    (rd / f"pending-{a.image}.txt").write_text("".join(l + "\n" for l in pending))
    (rd / f"failed-{a.image}.txt").write_text("".join(l + "\n" for l in failed))
    for old in rd.glob(f"{a.image}-sheets-*.jpg"):
        old.unlink()
    sheets = [f"validation/{l}/sheet.jpg" for l in pending]
    for i in range(0, len(sheets), 10):
        chunk = " ".join(f"'{s}'" for s in sheets[i:i + 10])
        sh_run(f"bash {sh_script('stack_sheets.sh')} 'review/{a.image}-sheets-{i // 10:03d}.jpg' {chunk}",
               capture_output=True, text=True)
    print(json.dumps({"needs_a_look": len(pending), "decode_errors": len(failed),
                      "sheets": sorted(p.name for p in rd.glob(f"{a.image}-sheets-*.jpg"))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
