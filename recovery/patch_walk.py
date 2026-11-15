#!/usr/bin/env python3
"""Rebuild a lost chain around an unrecoverable gap, for a PATCHED file.

patch_walk.py <chains> <label> <reenter_k> <reenter_chunk> [--strict]

Clusters 1 .. reenter_k-1 that the partial chain does not have are recorded
as {"chunk": null, "how": "missing-zero"} (the assembler writes zeros there);
the walk restarts at reenter_k from the moov's sample-start anchors only (no
NAL state can be carried across the gap) and continues with the normal walker.
The result goes to work/chains/<chains>-patched/<label>.json, never into the
verified chain set, so it cannot be published as a verified recovery.
Read-only on the image.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chain_walk as cw  # noqa: E402
from rdr_reader import RdrObject  # noqa: E402

from rconfig import WORK  # noqa: E402
chains, label, rk, rc = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
if "--strict" in sys.argv:
    cw.RELAXED_MAX_DEPTH = 0
cdir = WORK / "chains" / chains
part = json.loads((cdir / f"{label}.partial.json").read_text())
spec = part["spec"]
raw = RdrObject(part["image"])
model = cw.FileModel(spec, raw)
cache = cw.ChunkCache(raw, 4096)
own = set()
for p in cdir.glob("*.json"):
    if p.name.endswith(".partial.json") or ".pre-" in p.name:
        continue
    for m in json.loads(p.read_text())["mapping"]:
        if m["how"] not in cw.UNRESOLVED:
            own.add(m["chunk"])
if rc in own:
    raise SystemExit(f"re-entry chunk {rc} is owned by another chain")
known = {m["k"]: m for m in part["mapping"] if m["how"] not in cw.UNRESOLVED}
mapping = []
for k in range(rk):
    if k in known:
        mapping.append(dict(known[k]))
        own.add(known[k]["chunk"])
    else:
        mapping.append({"k": k, "chunk": None, "how": "missing-zero"})
ok, ch, ex, pending, bits = cw.check_cluster(model, rk, cache.get(rc), [])
if not ok or ch == 0:
    raise SystemExit(f"re-entry chunk {rc} fails the anchor check for k={rk}")
mapping.append({"k": rk, "chunk": rc, "checks": ch, "exact": ex, "bits": round(bits, 1), "how": "reentry-anchor"})
excluded = own | {rc}
st = {"mapping": mapping, "prev": rc, "pending": pending, "k": rk + 1, "blind": []}
last_k = model.nclusters - 1
pinned_last = spec["moov_start"] // cw.CS if model.mdat_end % cw.CS else None
t0 = time.time()
status = cw._walk_from(model, cache, excluded, spec["moov_start"] // cw.CS + 64, st, model.nclusters, 0,
                       last_k, pinned_last, 10.0, 40.0, 500, t0)
mp = sorted(st["mapping"], key=lambda m: m["k"])
res = {"status": "complete" if status == "ok" else "lost", "seconds": round(time.time() - t0, 1),
       "reentry": {"k": rk, "chunk": rc}, "missing": [m["k"] for m in mp if m["how"] == "missing-zero"]}
if status != "ok":
    res.update({"at_cluster": st["blind"][0] if st["blind"] else st["k"], "prev_chunk": st["prev"]})
rec = {"image": part["image"], "spec": spec, "status": res, "placed": len(mp), "nclusters": model.nclusters,
       "mapping": mp, "patched": True}
out = WORK / "chains" / f"{chains}-patched"
out.mkdir(exist_ok=True)
name = f"{label}.json" if status == "ok" else f"{label}.partial.json"
(out / name).write_text(json.dumps(rec))
print(json.dumps({k: v for k, v in rec.items() if k not in ("mapping", "spec")}))
print("wrote", out / name)
