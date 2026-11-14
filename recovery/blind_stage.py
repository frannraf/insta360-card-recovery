#!/usr/bin/env python3
"""Resolve every unresolved blind cluster of an image's VID chains.

A blind cluster lies wholly inside one NAL payload, so only decoding can tell
which chunk belongs there.  For each VID chain with unresolved placements:

  * single-cluster gaps: fast_trial.py tests every chunk between the gap's
    fixed neighbours against the companion LRV (free chunks first, then all
    chunks if no full match).  When a file has several gaps (often two in one
    frame), each gap is re-scored with the current best answers for the
    others, until the answers stop changing; the converged scores were all
    made with every other gap in place.  apply_decode.py then applies the set
    only if every gap matches the LRV in all 26 bands, a different-content
    rival matches at most 16, and the top candidates were re-decoded solo.
  * gaps of several clusters are treated cluster by cluster the same way
    (--multi-fallback also tries trial_blinds.py's contiguous candidate runs).
Afterwards resolve_blinds.py re-runs the elimination pass.  Nothing is
guessed: a gap without a clear winner stays unresolved and the file is left
out of assembly.  Read-only on the image; writes chain JSONs (backed up as
<label>.pre-trial.json) and work/trials/.

Usage: blind_stage.py --image NAME [--chains DIR] [--labels L ...] [--passes 3]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chain_walk import UNRESOLVED  # noqa: E402
from rconfig import CODE, WORK  # noqa: E402

PY = [sys.executable, "-I", "-u"]
FULL = 26


def gaps(chain: dict) -> list[list[int]]:
    mp = sorted(chain["mapping"], key=lambda m: m["k"])
    runs, cur = [], []
    for m in mp:
        if m["how"] in UNRESOLVED:
            if cur and m["k"] != cur[-1] + 1:
                runs.append(cur)
                cur = []
            cur.append(m["k"])
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs


def fast(chains: str, label: str, k: int, sets: dict[int, int], free_only: bool) -> list[dict]:
    cmd = PY + [str(CODE / "fast_trial.py"), chains, label, "--k", str(k), "--top", "3"]
    for k2, c2 in sorted(sets.items()):
        cmd += ["--set", f"{k2}={c2}"]
    if free_only:
        cmd.append("--free-only")
    subprocess.run(cmd, capture_output=True, text=True)
    rf = WORK / "trials" / f"fast-{label}" / f"k{k}-result.json"
    return json.loads(rf.read_text()) if rf.exists() else []


def solve(chains: str, label: str, ks: list[int], passes: int) -> dict[int, int] | None:
    best: dict[int, int] = {}
    for p in range(passes):
        changed = False
        for k in ks:
            others = {k2: c2 for k2, c2 in best.items() if k2 != k}
            res = fast(chains, label, k, others, free_only=True)
            if p > 0 and (not res or res[0].get("good", 0) < FULL):
                # still no full match with the other gaps in place: widen to
                # owned chunks too (reports a conflict rather than taking it)
                res = fast(chains, label, k, others, free_only=False)
            if not res or not res[0].get("solo"):
                print(f"  {label} k{k}: no candidate decoded", flush=True)
                return None
            top = res[0]
            print(f"  {label} pass {p + 1} k{k}: top {top['chunk']} {top['good']}/{FULL} bands "
                  f"({top['owner']})", flush=True)
            if best.get(k) != top["chunk"]:
                best[k] = top["chunk"]
                changed = True
        if not changed and len(best) == len(ks):
            return best
    return best if len(best) == len(ks) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--chains", default=None)
    ap.add_argument("--labels", nargs="*", default=None)
    ap.add_argument("--passes", type=int, default=3)
    ap.add_argument("--multi-cands", type=int, default=400, help="trial_blinds.py candidates for multi-cluster gaps")
    ap.add_argument("--multi-fallback", action="store_true",
                    help="also run trial_blinds.py on multi-cluster gaps (contiguous candidate runs)")
    a = ap.parse_args()
    chains = a.chains or a.image
    cdir = WORK / "chains" / chains
    subprocess.run(PY + [str(CODE / "resolve_blinds.py"), chains], capture_output=True, text=True)
    todo = []
    for p in sorted(cdir.glob("VID_*.json")):
        if ".pre-" in p.name or p.name.endswith(".partial.json"):
            continue
        lab = p.stem
        if a.labels and lab not in a.labels:
            continue
        g = gaps(json.loads(p.read_text()))
        if g:
            todo.append((lab, g))
    print(f"[blinds] {len(todo)} VID chains with unresolved gaps", flush=True)
    summary = {"applied": [], "unresolved": []}
    for lab, g in todo:
        # every unresolved cluster is scored on its own; clusters in the same
        # or neighbouring frames (incl. back-to-back ones) converge through
        # the repeated passes, each scored with the others in place
        singles = [k for run in g for k in run]
        multis = [run for run in g if len(run) > 1 and a.multi_fallback]
        ok = True
        if singles:
            best = solve(chains, lab, singles, a.passes)
            if best is None:
                ok = False
            else:
                r = subprocess.run(PY + [str(CODE / "apply_decode.py"), chains, lab] +
                                   [f"{k}={c}" for k, c in sorted(best.items())], capture_output=True, text=True)
                print(f"  {lab} apply: {r.stdout.strip()[-300:]}", flush=True)
                ok = r.returncode == 0
        if ok and multis:
            r = subprocess.run(PY + [str(CODE / "trial_blinds.py"), chains, lab, "--max-cands",
                                     str(a.multi_cands), "--apply"], capture_output=True, text=True)
            ok = r.returncode == 0
            print(f"  {lab} multi-cluster gaps: {'applied' if ok else 'unresolved'}", flush=True)
        summary["applied" if ok else "unresolved"].append(lab)
    subprocess.run(PY + [str(CODE / "resolve_blinds.py"), chains], capture_output=True, text=True)
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
