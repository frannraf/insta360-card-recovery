#!/usr/bin/env python3
"""Diagnose a lost chain: test every chunk in a window as the missing cluster.

Re-walks the partial chain to rebuild the NAL state at the break, then tests
all chunks in [lo, hi] (owned or not) as the missing cluster and reports hits
with anchor-based evidence bits and the chunk's current owner.
Usage: probe_lost.py <chains_dir> <label> [--lo N] [--hi N]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chain_walk import CS, FileModel, check_cluster, cluster_is_blind  # noqa: E402
from rdr_reader import RdrObject  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("chains")
    ap.add_argument("label")
    ap.add_argument("--lo", type=int, default=None)
    ap.add_argument("--hi", type=int, default=None)
    ap.add_argument("--min-bits", type=float, default=15.0)
    args = ap.parse_args()
    cdir = WORK / "chains" / args.chains
    part = json.loads((cdir / f"{args.label}.partial.json").read_text())
    spec = part["spec"]
    raw = RdrObject(part["image"])
    model = FileModel(spec, raw)
    mapping = sorted(part["mapping"], key=lambda m: m["k"])
    pending = []
    k = 0
    for m in mapping:
        if m["k"] != k:
            break
        d = raw.read_chunk(m["chunk"])
        ok, ch, ex, pending, bits = check_cluster(model, k, d, pending)
        k += 1
    while k < model.nclusters and cluster_is_blind(model, k, pending):
        k += 1
    own = {}
    for p in cdir.glob("*.json"):
        if ".partial" in p.name or ".pre-" in p.name:
            continue
        d = json.loads(p.read_text())
        for m in d["mapping"]:
            own[m["chunk"]] = f"{d['spec']['label']}:k{m['k']}:{m['how']}"
    lo = args.lo if args.lo is not None else spec["header_chunk"]
    hi = args.hi if args.hi is not None else spec["moov_start"] // CS + 64
    print(json.dumps({"label": args.label, "probe_k": k, "placed": len(mapping),
                      "pending": [(p[0] - k * CS, p[1] - p[0]) for p in pending][:4], "window": [lo, hi]}), flush=True)
    hits = []
    for c in range(lo, hi + 1):
        r = check_cluster(model, k, raw.read_chunk(c), pending)
        if r[0] and r[1] > 0 and r[4] >= args.min_bits:
            hits.append((c, round(r[4], 1), r[1], r[2], own.get(c, "free")))
    hits.sort(key=lambda h: -h[1])
    for h in hits:
        print(h)
    print("hits", len(hits))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
