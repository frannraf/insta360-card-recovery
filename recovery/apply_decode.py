#!/usr/bin/env python3
"""Apply blind-cluster placements found by fast_trial.py (jointly).

trial_blinds.py can only apply a file whose blind runs can each be scored
alone; when two blind runs sit in the same frame (or one frame depends on
the other), each must be scored with the other already placed
(fast_trial.py --set).  This applies such a set only if, for every K=CHUNK:
  * the chain's cluster K is still an unresolved blind placement,
  * CHUNK lies between K's nearest fixed neighbours and no other chain owns it,
  * work/trials/fast-<label>/k<K>-result.json ranks CHUNK first with every
    row band matching the LRV (26/26 at >= 28 dB), and the best candidate with
    different bytes matches at most 16 bands,
  * that run was made with every other applied pair in its --set when the
    other cluster lies in the same or an earlier frame of the same track.
The chain is backed up once as <label>.pre-trial.json.  Read-only on the RDR.

Usage: apply_decode.py <chain_dir> <label> K=CHUNK [K=CHUNK ...]
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chain_walk import UNRESOLVED  # noqa: E402
from rdr_reader import RdrObject  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402
FULL = 26
RIVAL_MAX = 16


def main() -> int:
    chain_dir, label = sys.argv[1], sys.argv[2]
    pairs = dict(tuple(map(int, a.split("="))) for a in sys.argv[3:])
    cdir = WORK / "chains" / chain_dir
    path = cdir / f"{label}.json"
    chain = json.loads(path.read_text())
    by_k = {m["k"]: m for m in chain["mapping"]}
    raw = RdrObject(chain["image"])
    own = {}
    for p in cdir.glob("*.json"):
        if p.name.endswith(".partial.json") or ".pre-" in p.name or p.name == path.name:
            continue
        for m in json.loads(p.read_text())["mapping"]:
            if m["how"] not in UNRESOLVED:
                own[m["chunk"]] = p.stem
    fixed = sorted((m["k"], m["chunk"]) for m in chain["mapping"] if m["how"] not in UNRESOLVED)
    tdir = WORK / "trials" / f"fast-{label}"
    meta = {k: json.loads((tdir / f"k{k}-args.json").read_text()) if (tdir / f"k{k}-args.json").exists() else None
            for k in pairs}
    problems, evidence = [], {}
    for k, c in pairs.items():
        m = by_k.get(k)
        if m is None or m["how"] not in UNRESOLVED:
            problems.append(f"k{k}: not an unresolved blind ({m and m['how']})")
            continue
        lo = max((ch for kk, ch in fixed if kk < k), default=None)
        hi = min((ch for kk, ch in fixed if kk > k), default=None)
        if lo is None or hi is None or not lo < c < hi:
            problems.append(f"k{k}: chunk {c} outside ({lo}, {hi})")
        if c in own:
            problems.append(f"k{k}: chunk {c} owned by {own[c]}")
        rf = tdir / f"k{k}-result.json"
        if not rf.exists():
            problems.append(f"k{k}: no fast_trial result")
            continue
        res = json.loads(rf.read_text())
        top = res[0]
        data = raw.read_chunk(c)
        rival = next((r for r in res[1:] if raw.read_chunk(r["chunk"]) != data), None)
        if not top.get("solo") or (rival and not rival.get("solo")):
            problems.append(f"k{k}: top candidates were not re-decoded solo")
        if top["chunk"] != c or top["good"] != FULL:
            problems.append(f"k{k}: result top is {top['chunk']} with {top['good']}/{FULL} bands")
        if rival and rival["good"] > RIVAL_MAX:
            problems.append(f"k{k}: rival {rival['chunk']} matches {rival['good']} bands")
        md = meta[k]
        if md is None:
            problems.append(f"k{k}: no args record for its run")
        else:
            used = dict(tuple(map(int, s.split("="))) for s in md["set"])
            for k2, c2 in pairs.items():
                if k2 == k:
                    continue
                m2 = meta.get(k2)
                if m2 and m2["track"] == md["track"] and m2["frame"] <= md["frame"] and used.get(k2) != c2:
                    problems.append(f"k{k}: scored without k{k2}={c2} placed (same/earlier frame)")
        evidence[k] = {"good": top["good"], "mean_db": top["mean"],
                       "rival_good": rival["good"] if rival else None, "tested": md and md["tested"]}
    print(json.dumps({"label": label, "pairs": pairs, "evidence": evidence, "problems": problems}))
    if problems:
        return 3
    backup = path.with_name(path.stem + ".pre-trial.json")
    if not backup.exists():
        shutil.copy2(path, backup)
    for k, c in pairs.items():
        by_k[k].update({"chunk": c, "how": "blind-resolved-decode", "fast_trial": evidence[k]})
    path.write_text(json.dumps(chain))
    print("applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
