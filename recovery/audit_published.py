#!/usr/bin/env python3
"""Audit every published file against the chain its manifest row cites.

For each row: the chain must have no unresolved placements, and every
non-NAL-verified cluster plus 32 random clusters (and the last) of the
published file must be byte-identical to the RDR chunk the chain names.
Read-only.  Writes work/logs/audit-published.json.
"""

from __future__ import annotations

import csv
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chain_walk import CS, UNRESOLVED  # noqa: E402
from rdr_reader import RdrObject  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402
from rconfig import OUT  # noqa: E402


def main() -> int:
    if not (OUT / "MANIFEST.csv").exists():
        print("nothing published yet")
        return 0
    rows = list(csv.DictReader(open(OUT / "MANIFEST.csv", newline="")))
    raws, cats, results = {}, {}, []
    for r in rows:
        chain_path = WORK.parent / r["chain_json"]
        chain = json.loads(chain_path.read_text())
        img = chain["image"]
        raws.setdefault(img, RdrObject(img))
        cats.setdefault(img, {f"{f['kind']}_{f['name_time']}": f for f in
                              json.loads((WORK / "scan-analysis" / f"{img}-full-catalog.json").read_text())["files"]})
        entry = cats[img][chain["spec"]["label"]]
        mp = sorted(chain["mapping"], key=lambda m: m["k"])
        unresolved = [m["k"] for m in mp if m["how"] in UNRESOLVED]
        weak = [m for m in mp if m["how"] not in ("verified", "header", "last-pinned")]
        sample = {m["k"]: m for m in weak + random.Random(r["file"]).sample(mp, min(32, len(mp))) + [mp[-1]]}
        bad = None
        with open(OUT / r["file"], "rb") as fh:
            for k, m in sorted(sample.items()):
                n = min(CS, entry["mdat_end"] - k * CS)
                fh.seek(k * CS)
                if fh.read(n) != raws[img].read_chunk(m["chunk"])[:n]:
                    bad = k
                    break
        res = {"file": r["file"], "chain": r["chain_json"], "unresolved": len(unresolved),
               "checked": len(sample), "weak": len(weak), "mismatch_at": bad}
        results.append(res)
        if unresolved or bad is not None:
            print("PROBLEM", res, flush=True)
    (WORK / "logs").mkdir(exist_ok=True)
    (WORK / "logs" / "audit-published.json").write_text(json.dumps(results, indent=1))
    ok = sum(1 for x in results if not x["unresolved"] and x["mismatch_at"] is None)
    print(f"audited {len(results)} published files: {ok} pass, {len(results) - ok} problems; "
          f"{sum(x['checked'] for x in results)} clusters compared, "
          f"{sum(x['weak'] for x in results)} of them non-NAL-verified")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
