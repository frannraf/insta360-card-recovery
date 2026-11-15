#!/usr/bin/env python3
"""find_reentry.py <chains> <label> <lo> <hi> <k> [<k> ...]

After an unrecoverable gap at the start of a file, find where the chain can be
picked up again: for each cluster k that holds a sample start (from the moov
tables), test every free chunk in [lo, hi] using only those table anchors, then
follow each hit forward (next clusters up to 6 chunks apart, as the camera
interleaves trailer records) counting only independent evidence.  A re-entry
point needs >= 40 bits.  Read-only.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chain_walk as cw  # noqa: E402
from rdr_reader import RdrObject  # noqa: E402

from rconfig import WORK  # noqa: E402
chains, label, lo, hi = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
ks = [int(x) for x in sys.argv[5:]]
cdir = WORK / "chains" / chains
part = json.loads((cdir / f"{label}.partial.json").read_text())
raw = RdrObject(part["image"])
model = cw.FileModel(part["spec"], raw)
own = set()
for p in cdir.glob("*.json"):
    if p.name.endswith(".partial.json") or ".pre-" in p.name:
        continue
    for m in json.loads(p.read_text())["mapping"]:
        if m["how"] not in cw.UNRESOLVED:
            own.add(m["chunk"])
free = [c for c in range(lo, hi + 1) if c not in own]
data = {}


def chunk(c):
    if c not in data:
        data[c] = raw.read_chunk(c)
    return data[c]


def follow(k, h, steps=20):
    ok, ch, _ex, pend, bits = cw.check_cluster(model, k, chunk(h), [])
    if not ok or ch == 0:
        return 0.0, []
    total, prev, n, path = bits, h, k + 1, [h]
    while n < min(k + 1 + steps, model.nclusters - 1) and total < 120:
        if cw.cluster_is_blind(model, n, pend):
            c = prev + 1
            while c in own:
                c += 1
            if c - prev > 6:
                break
            prev, n = c, n + 1
            path.append(c)
            continue
        found = None
        for c in range(prev + 1, prev + 7):
            if c in own:
                continue
            ok2, ch2, _e2, p2, b2 = cw.check_cluster(model, n, chunk(c), pend, count_carried=False)
            if ok2 and ch2 > 0:
                found = (c, p2, b2)
                break
        if not found:
            break
        prev, pend, n = found[0], found[1], n + 1
        total += found[2]
        path.append(prev)
    return round(total, 1), path


for k in ks:
    anchors = model.samples_starting_in(k)
    best = []
    for c in free:
        ok, ch, _ex, _p, bits = cw.check_cluster(model, k, chunk(c), [])
        if ok and ch > 0 and bits >= 15:
            total, path = follow(k, c)
            best.append((total, c, len(path), path[:6]))
    best.sort(reverse=True)
    print(json.dumps({"k": k, "anchors": len(anchors), "hits": len(best),
                      "top": [{"chunk": b[1], "bits": b[0], "clusters": b[2], "path": b[3]} for b in best[:3]]}),
          flush=True)
    if len(data) > 20000:
        data.clear()
