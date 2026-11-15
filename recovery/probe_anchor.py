#!/usr/bin/env python3
"""probe_anchor.py <chains> <label> <k> <lo> <hi>: find candidates for cluster k
using only the sample starts the moov tables put in it (no state carried from
earlier clusters, e.g. after an unrecoverable gap).  Read-only.  Prints hits
with their evidence bits, best first, and the chunk's current owner."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chain_walk import FileModel, check_cluster  # noqa: E402
from rdr_reader import RdrObject  # noqa: E402

from rconfig import WORK  # noqa: E402
chains, label, k, lo, hi = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
cdir = WORK / "chains" / chains
part = json.loads((cdir / f"{label}.partial.json").read_text())
raw = RdrObject(part["image"])
model = FileModel(part["spec"], raw)
own = {}
for p in cdir.glob("*.json"):
    if p.name.endswith(".partial.json") or ".pre-" in p.name:
        continue
    d = json.loads(p.read_text())
    for m in d["mapping"]:
        own[m["chunk"]] = f"{d['spec']['label']}:k{m['k']}:{m['how']}"
print(json.dumps({"label": label, "k": k, "anchors": [(o - k * 262144, s) for o, s, _c in model.samples_starting_in(k)],
                  "window": [lo, hi]}), flush=True)
hits = []
for c in range(lo, hi + 1):
    ok, ch, ex, _pend, bits = check_cluster(model, k, raw.read_chunk(c), [])
    if ok and ch > 0 and bits >= 15:
        hits.append((c, round(bits, 1), ch, ex, own.get(c, "free")))
hits.sort(key=lambda h: -h[1])
for h in hits[:40]:
    print(h)
print("hits", len(hits))
