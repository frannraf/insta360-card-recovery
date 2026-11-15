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


def lookahead(cdir: Path, label: str, k: int, cands: list[int], max_gap: int = 6, steps: int = 16,
              need_bits: float = 40.0) -> list[tuple[int, float, int]]:
    """Structural check for a candidate whose decode adds nothing yet (its
    slice ends in a later cluster): follow it forward the way the walker does,
    but let each next cluster sit up to max_gap chunks further on (the camera
    writes trailer records between video clusters at the start of a
    recording).  Only independent evidence counts (sample starts from the
    tables, exact landings on sample ends).  Returns (chunk, bits, clusters)."""
    sys.path.insert(0, str(SCRIPTS))
    import chain_walk as cw
    from rdr_reader import RdrObject
    own = set()
    for q in cdir.glob("*.json"):
        if q.name.endswith(".partial.json") or ".pre-" in q.name:
            continue
        for m in json.loads(q.read_text())["mapping"]:
            if m["how"] not in cw.UNRESOLVED:
                own.add(m["chunk"])
    part = json.loads((cdir / f"{label}.partial.json").read_text())
    raw = RdrObject(part["image"])
    model = cw.FileModel(part["spec"], raw)
    pending, kk = [], 0
    for m in sorted(part["mapping"], key=lambda m: m["k"]):
        if m["k"] != kk:
            break
        _ok, _ch, _ex, pending, _b = cw.check_cluster(model, kk, raw.read_chunk(m["chunk"]), pending)
        own.add(m["chunk"])
        kk += 1
    if kk != k:
        return []
    out = []
    for h in cands:
        ok, ch, _ex, pend, bits = cw.check_cluster(model, k, raw.read_chunk(h), pending)
        if not ok or ch == 0:
            out.append((h, 0.0, 0))
            continue
        total, prev, n, used = bits, h, k + 1, {h}
        while n < min(k + 1 + steps, model.nclusters - 1) and total < need_bits:
            if cw.cluster_is_blind(model, n, pend):
                c = prev + 1
                while c in own or c in used:
                    c += 1
                if c - prev > max_gap:
                    break
                prev, n = c, n + 1
                used.add(c)
                continue
            found = None
            for c in range(prev + 1, prev + 1 + max_gap):
                if c in own or c in used:
                    continue
                ok2, ch2, _ex2, pend2, b2 = cw.check_cluster(model, n, raw.read_chunk(c), pend, count_carried=False)
                if ok2 and ch2 > 0:
                    found = (c, pend2, b2)
                    break
            if not found:
                break
            prev, pend, n = found[0], found[1], n + 1
            total += found[2]
            used.add(prev)
        out.append((h, round(total, 1), n - k))
    return out


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
            decisive = bool(cleanly) and cleanly[0]["gain"] >= MIN_GAIN and (
                len(cleanly) == 1 or cleanly[1]["gain"] * 3 <= cleanly[0]["gain"])
            if not decisive:
                # the decode cannot tell (e.g. the slice ends in a later
                # cluster): decide by structure, with the walker's 40-bit bar
                pool = [r["chunk"] for r in cleanly] or [h[0] for h in free]
                la = lookahead(cdir, a.label, pk, pool)
                print(f"[step {step}] lookahead (chunk, bits, clusters):", la, flush=True)
                strong = [x for x in la if x[1] >= 40.0]
                if len(strong) == 1:
                    forced[pk] = strong[0][0]
                    continue
            if not decisive:
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
