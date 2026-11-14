#!/usr/bin/env python3
"""Global post-pass for blind clusters (no NAL boundary inside, so no direct check).

Ownership from every complete chain's *verified* placements (header, verified,
verified-relaxed, last-pinned, blind-contiguous) is fixed.  For each run of
non-contiguous blind clusters, the candidates are the chunks strictly between
the run's verified neighbours that no verified placement owns.  A run whose
candidate count equals its length is resolved in order; then ownership is
updated and the pass repeats until nothing changes.  Remaining runs are written
to work/chains/<image>-blind-trials.json for decode-based resolution.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from chain_walk import UNRESOLVED  # noqa: E402


class _Fixed:
    """Every placement type except the unresolved blind ones is fixed."""
    def __contains__(self, how):
        return how not in UNRESOLVED


FIXED = _Fixed()


def runs_of(mapping):
    """Yield (start_index, end_index) of consecutive non-fixed blind entries."""
    i = 0
    while i < len(mapping):
        if mapping[i]["how"] not in FIXED:
            j = i
            while j + 1 < len(mapping) and mapping[j + 1]["how"] not in FIXED:
                j += 1
            yield i, j
            i = j + 1
        else:
            i += 1


def main() -> int:
    image = sys.argv[1]
    d = WORK / "chains" / image
    chains = {}
    for p in sorted(d.glob("*.json")):
        if p.name.endswith(".partial.json") or ".pre-" in p.name:
            continue
        c = json.loads(p.read_text())
        c["mapping"].sort(key=lambda m: m["k"])
        chains[p.stem] = (p, c)
    changed_files = set()
    while True:
        owner = {}
        for lab, (_p, c) in chains.items():
            for m in c["mapping"]:
                if m["how"] in FIXED:
                    owner[m["chunk"]] = lab
        progress = False
        for lab, (_p, c) in chains.items():
            m = c["mapping"]
            for i, j in list(runs_of(m)):
                n = j - i + 1
                lo = m[i - 1]["chunk"] if i > 0 else None
                hi = m[j + 1]["chunk"] if j + 1 < len(m) else None
                if lo is None or hi is None:
                    continue
                cands = [x for x in range(lo + 1, hi) if x not in owner] if hi - lo < 200000 else None
                if cands is not None and len(cands) == n:
                    for t, x in zip(range(i, j + 1), cands):
                        m[t]["chunk"] = x
                        m[t]["how"] = "blind-resolved-unique"
                        m[t]["candidates"] = n
                        owner[x] = lab
                    changed_files.add(lab)
                    progress = True
        if not progress:
            break
    trials = []
    for lab, (_p, c) in chains.items():
        m = c["mapping"]
        for i, j in runs_of(m):
            lo = m[i - 1]["chunk"] if i > 0 else None
            hi = m[j + 1]["chunk"] if j + 1 < len(m) else None
            cands = [x for x in range(lo + 1, hi) if x not in owner] if lo is not None and hi is not None and hi - lo < 200000 else []
            trials.append({"label": lab, "ks": [m[t]["k"] for t in range(i, j + 1)], "prev": lo,
                           "next": hi, "candidates": cands[:500], "n_candidates": len(cands)})
    for lab in changed_files:
        p, c = chains[lab]
        backup = p.with_name(p.stem + ".pre-resolve.json")
        if not backup.exists():
            shutil.copy2(p, backup)
        p.write_text(json.dumps(c))
    out = WORK / "chains" / f"{image}-blind-trials.json"
    out.write_text(json.dumps(trials, indent=1))
    print(json.dumps({"resolved_files": sorted(changed_files), "unresolved_runs": len(trials)}))
    for t in trials:
        print(t["label"], "ks", t["ks"], "prev", t["prev"], "next", t["next"], "candidates", t["n_candidates"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
