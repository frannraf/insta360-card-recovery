#!/usr/bin/env python3
"""Resume a lost (partial) chain from clusters confirmed by decoding.

A recording whose cluster 1 sits in a far-away hole often carries only one NAL
boundary there (~15 bits), so the walker cannot accept it and the chain is
lost at k=1.  When decoding has confirmed the cluster (the slice that runs
into it reproduces the companion LRV picture), pass it with --set K=CHUNK;
the forced clusters must still pass the NAL-boundary checks, and the walk then
continues with the normal hardened rules (chain_walk._walk_from).

Writes chains/<dir>/<label>.json only when the walk completes; the old
.partial.json is kept as <label>.pre-resume.json.  Read-only on the RDR.

Usage: resume_lost.py <chain_dir> <label> --set 1=CHUNK [--set 2=CHUNK ...]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chain_walk as cw  # noqa: E402
from rdr_reader import RdrObject  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("chain_dir")
    ap.add_argument("label")
    ap.add_argument("--set", action="append", required=True, metavar="K=CHUNK")
    ap.add_argument("--how", default="decode-verified")
    ap.add_argument("--strict", action="store_true",
                    help="no relaxed branching or first-fit speculation: stop at the first weak step "
                         "(rebuild_lost.py confirms weak steps by decoding instead; the relaxed search "
                         "can branch for hours on first-fit holes)")
    args = ap.parse_args()
    if args.strict:
        cw.RELAXED_MAX_DEPTH = 0
        cw._firstfit_extend = lambda *a, **k: None
    cdir = WORK / "chains" / args.chain_dir
    ppath = cdir / f"{args.label}.partial.json"
    part = json.loads(ppath.read_text())
    spec = part["spec"]
    raw = RdrObject(part["image"])
    model = cw.FileModel(spec, raw)
    cache = cw.ChunkCache(raw, 4096)
    forced = dict(tuple(map(int, s.split("="))) for s in args.set)
    own = set()
    for p in cdir.glob("*.json"):
        if p.name.endswith(".partial.json") or ".pre-" in p.name:
            continue
        for m in json.loads(p.read_text())["mapping"]:
            if m["how"] not in cw.UNRESOLVED:
                own.add(m["chunk"])
    clash = [c for c in forced.values() if c in own]
    if clash:
        raise SystemExit(f"forced chunks owned by another chain: {clash}")
    excluded = own - {spec["header_chunk"]}
    max_chunk = spec["moov_start"] // cw.CS + 64
    last_k = model.nclusters - 1
    pinned_last = spec["moov_start"] // cw.CS if model.mdat_end % cw.CS else None

    first = cache.get(spec["header_chunk"])
    ok, ch, ex, pending, bits = cw.check_cluster(model, 0, first, [])
    mapping = [{"k": 0, "chunk": spec["header_chunk"], "checks": ch, "exact": ex, "bits": round(bits, 1),
                "how": "header"}]
    k = 1
    prev = spec["header_chunk"]
    # Keep what the partial chain already verified (walker steps keep their
    # own "how"), in one unbroken run from k=1, then add the forced clusters
    # that continue it.  Forced clusters win where both exist.
    known = {m["k"]: m for m in part["mapping"] if m["how"] not in cw.UNRESOLVED and m["k"] > 0}
    while k in forced or k in known:
        if k not in forced:
            m = known[k]
            ok, ch, ex, pending, bits = cw.check_cluster(model, k, cache.get(m["chunk"]), pending)
            if not ok:
                break  # stale prefix: stop here and let the walk decide
            mapping.append(dict(m))
            excluded.add(m["chunk"])
            prev = m["chunk"]
            k += 1
            continue
        c = forced[k]
        ok, ch, ex, pending, bits = cw.check_cluster(model, k, cache.get(c), pending)
        if not ok:
            raise SystemExit(f"forced cluster k={k} chunk={c} fails the NAL checks")
        mapping.append({"k": k, "chunk": c, "checks": ch, "exact": ex, "bits": round(bits, 1),
                        "how": args.how, "skipped": c - prev - 1})
        excluded.add(c)
        prev = c
        k += 1
    st = {"mapping": mapping, "prev": prev, "pending": pending, "k": k, "blind": []}
    t0 = time.time()
    status = cw._walk_from(model, cache, excluded, max_chunk, st, model.nclusters, 0, last_k,
                           pinned_last, 10.0, 40.0, 500, t0)
    mp = sorted(st["mapping"], key=lambda m: m["k"])
    res = {"status": status if status != "ok" else "complete", "seconds": round(time.time() - t0, 1),
           "chunk_reads": cache.reads, "resumed_from": {str(a): b for a, b in forced.items()}}
    if status != "ok":
        res.update({"at_cluster": st["blind"][0] if st["blind"] else st["k"], "prev_chunk": st["prev"]})
    blind = {}
    for m in mp:
        if m["how"].startswith("blind"):
            blind[m["how"]] = blind.get(m["how"], 0) + 1
    rec = {"image": part["image"], "spec": spec, "status": res, "placed": len(mp), "nclusters": model.nclusters,
           "blind": blind, "jumps": sum(1 for m in mp if m.get("skipped")), "mapping": mp}
    print(json.dumps({k2: v for k2, v in rec.items() if k2 not in ("mapping", "spec")}))
    if status == "ok":
        out = cdir / f"{args.label}.json"
        if out.exists():
            raise SystemExit(f"{out} exists; not overwriting")
        out.write_text(json.dumps(rec))
        ppath.rename(cdir / f"{args.label}.pre-resume.json")
        print("wrote", out)
        return 0
    # still lost: keep the forced clusters in the partial so probe_lost.py
    # continues from the new break (original partial kept once)
    orig = cdir / f"{args.label}.pre-resume-partial.json"
    if not orig.exists():
        orig.write_text(ppath.read_text())
    # header + forced clusters + whatever the walker verified after them, up to
    # the first gap or unresolved blind placement
    keep = []
    for m in mp:
        if m["k"] != len(keep) or m["how"] in cw.UNRESOLVED:
            break
        keep.append(m)
    rec["mapping"] = keep
    rec["placed"] = len(keep)
    ppath.write_text(json.dumps(rec))
    print("partial updated:", [(m["k"], m["chunk"], m["how"]) for m in keep])
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
