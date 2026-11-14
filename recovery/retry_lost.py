#!/usr/bin/env python3
"""Retry every lost (partial) chain in a chain dir, one subprocess per file with
a wall-clock cap, excluding all chunks owned by complete chains.

Usage: retry_lost.py <image> <chains_dir> [--timeout S] [labels...]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import CODE, WORK  # noqa: E402
ONE = r'''
import sys, json, time
sys.path.insert(0, r"{scripts}")
from chain_walk import CS, ChunkCache, FileModel, walk
from rdr_reader import RdrObject
image, cdir, label = sys.argv[1], sys.argv[2], sys.argv[3]
from pathlib import Path
W = Path(r"{work}")
cat = json.loads((W / "scan-analysis" / f"{{image}}-full-catalog.json").read_text())["files"]
kind, t = label.split("_", 1)
f = next(x for x in cat if x["kind"] == kind and x["name_time"] == t)
own = set()
for p in (W / "chains" / cdir).glob("*.json"):
    if ".partial" in p.name or ".pre-" in p.name:
        continue
    for m in json.loads(p.read_text())["mapping"]:
        if m["how"] != "blind-ambiguous":
            own.add(m["chunk"])
raw = RdrObject(image)
spec = {{"name": f.get("name") or label, "label": label, "kind": kind, "header_chunk": f["header_chunk"],
        "moov_start": f["moov_start"], "moov_size": f["moov_size"], "inst_start": f["inst_start"],
        "inst_size": f["inst_size"], "mdat_end": f["mdat_end"], "duration": f["duration"],
        "file_size": f["file_size"]}}
model = FileModel(spec, raw)
cache = ChunkCache(raw, 4096)
t0 = time.time()
mapping, st = walk(model, cache, own - {{f["header_chunk"]}}, f["moov_start"] // CS + 64, log_every=10**9)
st["seconds"] = round(time.time() - t0, 1)
st["chunk_reads"] = cache.reads
blind = {{}}
for m in mapping:
    if m["how"].startswith("blind"):
        blind[m["how"]] = blind.get(m["how"], 0) + 1
rec = {{"image": image, "spec": spec, "status": st, "placed": len(mapping), "nclusters": model.nclusters,
       "blind": blind, "jumps": sum(1 for m in mapping if m.get("skipped")), "mapping": mapping}}
out = W / "chains" / cdir / (label + (".json" if st["status"] == "complete" else ".partial.json"))
out.write_text(json.dumps(rec))
if st["status"] == "complete":
    (W / "chains" / cdir / (label + ".partial.json")).unlink(missing_ok=True)
print(label, st["status"], len(mapping), model.nclusters, blind, st["seconds"], st.get("at_cluster", ""))
'''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("chains")
    ap.add_argument("labels", nargs="*")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()
    cdir = WORK / "chains" / args.chains
    labels = args.labels or sorted(p.name[:-len(".partial.json")] for p in cdir.glob("*.partial.json"))
    script = WORK / "tmp" / "_retry_one.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(ONE.format(scripts=str(CODE), work=str(WORK)))
    for lab in labels:
        t = time.time()
        try:
            r = subprocess.run([sys.executable, "-I", str(script), args.image, args.chains, lab],
                               capture_output=True, text=True, timeout=args.timeout)
            print((r.stdout.strip() or r.stderr.strip()[-300:]), flush=True)
        except subprocess.TimeoutExpired:
            print(f"{lab} timeout after {time.time()-t:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
