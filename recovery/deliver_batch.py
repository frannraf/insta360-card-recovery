#!/usr/bin/env python3
"""Run deliver.py for approved labels.

Usage: deliver_batch.py --image=NAME [--chains=DIR] LABEL [LABEL ...]
How each label passed the visual gate is taken from work/review/approved-<image>.txt,
whose lines are "LABEL" (a person looked at the contact sheet) or
"LABEL # auto: ..." (auto_review.py matched it against the companion LRV).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import final_name  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import CODE, WORK  # noqa: E402


def main() -> int:
    labels = sys.argv[1:]
    chains = image = None
    while labels and labels[0].startswith("--"):
        k, v = labels.pop(0)[2:].split("=", 1)
        if k == "chains":
            chains = v
        elif k == "image":
            image = v
    if not image:
        raise SystemExit(__doc__)
    chains = chains or image
    approval = {}
    appr = WORK / "review" / f"approved-{image}.txt"
    if appr.exists():
        for line in appr.read_text().splitlines():
            lab, _, note = line.partition("#")
            if lab.strip():
                approval[lab.strip()] = note.strip() or "beginning/middle/end inspected"
    cat = json.loads((WORK / "scan-analysis" / f"{image}-full-catalog.json").read_text())["files"]
    nf = WORK / "scan-analysis" / f"{image}-file-numbers.json"
    numbers = json.loads(nf.read_text()) if nf.exists() else {}
    by = {f"{f['kind']}_{f['name_time']}": f for f in cat}
    rc = 0
    for lab in labels:
        e = by[lab]
        name = final_name(e, numbers)
        n, how = numbers.get(e["name_time"], (None, ""))
        notes = "original name" if e.get("name") else (f"file number {how}" if n is not None else f"file number unknown ({how})")
        r = subprocess.run([sys.executable, "-I", str(CODE / "deliver.py"), "--label", lab,
                            "--assembled", str(WORK / "assembled" / name),
                            "--validation", str(WORK / "validation" / lab),
                            "--chain", str(WORK / "chains" / chains / f"{lab}.json"),
                            "--final-name", name, "--visual-ok", "--notes", notes, "--image", image,
                            "--visual-check", approval.get(lab, "beginning/middle/end inspected")],
                           capture_output=True, text=True)
        print(r.stdout.strip() or r.stderr.strip()[-400:], flush=True)
        rc |= r.returncode
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
