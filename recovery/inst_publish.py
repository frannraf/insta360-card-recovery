#!/usr/bin/env python3
"""Fresh inst rebuild + installation for every published file of an image."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import final_name  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import CODE, WORK  # noqa: E402
from rconfig import MANIFEST  # noqa: E402


def main() -> int:
    image, chains = sys.argv[1], sys.argv[2]
    cat = json.loads((WORK / "scan-analysis" / f"{image}-full-catalog.json").read_text())["files"]
    nf = WORK / "scan-analysis" / f"{image}-file-numbers.json"
    numbers = json.loads(nf.read_text()) if nf.exists() else {}
    if not MANIFEST.exists():
        print("nothing published yet")
        return 0
    published = {r["file"]: r for r in csv.DictReader(open(MANIFEST, newline=""))}
    for f in cat:
        name = final_name(f, numbers)
        if name not in published or published[name].get("inst_trailer", "").startswith("rebuilt"):
            continue
        lab = f"{f['kind']}_{f['name_time']}"
        (WORK / "inst" / f"{lab}.plan.json").unlink(missing_ok=True)
        subprocess.run([sys.executable, "-I", str(CODE / "rebuild_inst.py"), lab, "--chains", chains,
                        "--image", image], capture_output=True, text=True)
        plan = json.loads((WORK / "inst" / f"{lab}.plan.json").read_text())
        if plan.get("status") != "complete":
            print(lab, "inst", plan.get("status"), "missing", plan.get("missing"), flush=True)
            continue
        r = subprocess.run([sys.executable, "-I", str(CODE / "finalize_inst.py"), lab, image],
                           capture_output=True, text=True)
        out = (r.stdout.strip() or r.stderr.strip()[-300:])
        print(lab, "installed" if '"new_sha256"' in out else out[:200], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
