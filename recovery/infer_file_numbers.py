#!/usr/bin/env python3
"""Infer camera file numbers (the NNN in VID_..._00_NNN.insv) for a catalog.

Recordings with a known original name (catalog field "name") anchor the
sequence.  Between two anchors, if the count of unnamed recordings equals the
gap in numbers, they are numbered consecutively ("inferred-exact");
otherwise they stay unknown and are named ..._unk.
Writes work/scan-analysis/<image>-file-numbers.json: {name_time: [n|null, how]}.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402


def main() -> int:
    image = sys.argv[1]
    cat = json.loads((WORK / "scan-analysis" / f"{image}-full-catalog.json").read_text())["files"]
    known: dict[str, int] = {}
    times = sorted({f["name_time"] for f in cat})
    for f in cat:
        m = re.search(r"_(\d{3})\.(insv|lrv)$", f.get("name") or "")
        if m:
            known[f["name_time"]] = int(m.group(1))
    out: dict[str, list] = {}
    idx = [i for i, t in enumerate(times) if t in known]
    for i in idx:
        out[times[i]] = [known[times[i]], "named"]
    for a, b in zip(idx, idx[1:]):
        na, nb = known[times[a]], known[times[b]]
        between = b - a - 1
        for j in range(1, between + 1):
            if nb - na - 1 == between:
                out[times[a + j]] = [na + j, "inferred-exact"]
            else:
                out[times[a + j]] = [None, f"between {na:03d} and {nb:03d}"]
    for i, t in enumerate(times):
        if t not in out:
            if not idx:
                out[t] = [None, "no named anchor on this card"]
            elif i < idx[0]:
                out[t] = [None, f"before {known[times[idx[0]]]:03d}"]
            else:
                out[t] = [None, f"after {known[times[idx[-1]]]:03d}"]
    path = WORK / "scan-analysis" / f"{image}-file-numbers.json"
    path.write_text(json.dumps(out, indent=1))
    print(json.dumps({"image": image, "recordings": len(times), "named": len(known),
                      "inferred": sum(1 for v in out.values() if v[1] == "inferred-exact"),
                      "unknown": sum(1 for v in out.values() if v[0] is None), "wrote": str(path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
