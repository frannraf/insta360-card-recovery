#!/usr/bin/env python3
"""Rebuild a lost chain step by step, confirming each weak step by decoding.

Loop: resume_lost.py with every confirmed cluster -> if the walk completes,
done.  Otherwise probe_lost.py forward from the break for the next cluster
with a NAL boundary (hits are weak, ~15 bits), then confirm by decoding
against the companion LRV:
  * no blind cluster before it: decode with each free hit appended
    (lost_decode.py); accept the hit whose matching-block gain is >= MIN_GAIN
    and at least 3x the runner-up's gain;
  * one blind cluster before it: for each free hit, search the blind cluster
    among the free chunks in between (fast_trial.py --partial --free-only,
    solo re-decodes); accept when the best pair beats the best rival by
    >= MIN_GAIN blocks.
Anything else stops the loop for manual work.  Owned chunks are never taken.
Read-only on the RDR; chains change only through resume_lost.py.

Usage: rebuild_lost.py <chain_dir> <label> [--window 30000] [--max-steps 40]
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402
from rconfig import CODE as SCRIPTS  # noqa: E402
PY = [sys.executable, "-I", "-u"]
MIN_GAIN = 20


def run(args: list[str]) -> str:
    r = subprocess.run(PY + args, cwd=SCRIPTS, capture_output=True, text=True)
    return r.stdout + r.stderr


def forced_of(cdir: Path, label: str) -> dict[int, int]:
    p = cdir / f"{label}.partial.json"
    mp = json.loads(p.read_text())["mapping"]
    return {m["k"]: m["chunk"] for m in mp if m["how"] == "decode-verified"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("chain_dir")
    ap.add_argument("label")
    ap.add_argument("--window", type=int, default=30000)
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--set", action="append", default=[], metavar="K=CHUNK",
                    help="extra confirmed clusters to start from")
    a = ap.parse_args()
    cdir = WORK / "chains" / a.chain_dir
    forced = forced_of(cdir, a.label)
    forced.update(dict(tuple(map(int, s.split("="))) for s in a.set))
    for step in range(a.max_steps):
        sets = [x for k, c in sorted(forced.items()) for x in ("--set", f"{k}={c}")]
        out = run(["resume_lost.py", a.chain_dir, a.label, "--strict", *sets])
        print(f"[step {step}] resume with {len(forced)} forced:", out.strip().splitlines()[0][:300], flush=True)
        if "wrote" in out:
            print("COMPLETE", flush=True)
            return 0
        if not (cdir / f"{a.label}.partial.json").exists():
            print("no partial written; stopping", flush=True)
            return 2
        prev = max(forced.values()) if forced else None
        mp = json.loads((cdir / f"{a.label}.partial.json").read_text())["mapping"]
        prev = max(mp, key=lambda m: m["k"])["chunk"]
        top_k = max(m["k"] for m in mp)
        pr = run(["probe_lost.py", a.chain_dir, a.label, "--lo", str(prev + 1), "--hi", str(prev + a.window)])
        lines = pr.strip().splitlines()
        head = json.loads(lines[0])
        pk = head["probe_k"]
        nb = pk - top_k - 1
        hits = [ast.literal_eval(x) for x in lines[1:] if x.startswith("(")]
        free = sorted((h for h in hits if h[4] == "free"), key=lambda h: h[0])[:6]
        print(f"[step {step}] probe k={pk} blind_before={nb} hits={len(hits)} free={[h[0] for h in free]}", flush=True)
        if not free:
            print("no free hit in window; stopping", flush=True)
            return 2
        if nb == 0:
            res = run(["lost_decode.py", a.chain_dir, a.label, *[str(h[0]) for h in free]])
            rows = [json.loads(x) for x in res.strip().splitlines() if x.startswith("{")]
            # a right chunk decodes with zero error lines and adds picture that
            # matches the LRV; wrong chunks usually throw decode errors
            scored = sorted((r for r in rows[1:] if r.get("gain") is not None), key=lambda r: -r["gain"])
            cleanly = [r for r in scored if r["decode"][1:] == ["0"]]
            print(f"[step {step}] decode gains:", [(r["chunk"], r["gain"], r["decode"][-1]) for r in scored], flush=True)
            if not cleanly or cleanly[0]["gain"] < MIN_GAIN or (
                    len(cleanly) > 1 and cleanly[1]["gain"] * 3 > cleanly[0]["gain"]):
                print("no clear winner; stopping", flush=True)
                return 2
            forced[pk] = cleanly[0]["chunk"]
        elif nb == 1:
            best = []
            for h in free:
                run(["fast_trial.py", a.chain_dir, a.label, "--partial", "--k", str(pk - 1),
                     "--set", f"{pk}={h[0]}", "--free-only", "--lo", str(prev + 1), "--hi", str(h[0] - 1),
                     *[x for k, c in sorted(forced.items()) for x in ("--set", f"{k}={c}")]])
                rf = WORK / "trials" / f"fast-{a.label}" / f"k{pk - 1}-result.json"
                res = json.loads(rf.read_text()) if rf.exists() else []
                if res and res[0].get("solo"):
                    best.append((res[0]["good"], h[0], res[0]["chunk"]))
            best.sort(reverse=True)
            print(f"[step {step}] pairs (blocks, k{pk}, k{pk - 1}):", best[:4], flush=True)
            if not best:
                print("no candidate pair; stopping", flush=True)
                return 2
            # rival: decode gain of a wrong blind chunk with the same hit
            rv = run(["fast_trial.py", a.chain_dir, a.label, "--partial", "--k", str(pk - 1),
                      # rivals: the chunks around the winner, also outside the gap
                      # (any other chunk is a wrong answer by definition, and a
                      # tiny gap may hold only one candidate)
                      "--set", f"{pk}={best[0][1]}", "--lo", str(best[0][2] - 40),
                      "--hi", str(best[0][2] + 40),
                      *[x for k, c in sorted(forced.items()) for x in ("--set", f"{k}={c}")]])
            rf = WORK / "trials" / f"fast-{a.label}" / f"k{pk - 1}-result.json"
            res = json.loads(rf.read_text())
            top = res[0]
            rival = next((r for r in res[1:] if r.get("solo")), None)
            print(f"[step {step}] confirm: top {top['chunk']} {top['good']} rival "
                  f"{rival and rival['chunk']} {rival and rival['good']}", flush=True)
            if top["chunk"] != best[0][2] or rival is None or top["good"] - rival["good"] < MIN_GAIN:
                print("no clear winner; stopping", flush=True)
                return 2
            forced[pk - 1] = best[0][2]
            forced[pk] = best[0][1]
        else:
            print(f"{nb} blind clusters before k={pk}: manual work needed", flush=True)
            return 2
    print("max steps reached", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
