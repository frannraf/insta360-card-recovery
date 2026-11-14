#!/usr/bin/env python3
"""Resolve ambiguous blind clusters by trial decoding (read-only on the RDR).

A blind cluster lies wholly inside one NAL payload, so only decoding can tell
which candidate chunk is right.  For each candidate, rebuild just the affected
GOP of every video track touching the cluster as an Annex-B elementary stream
(parameter sets + samples from the chain, candidate substituted), decode it in
WSL ffmpeg, and score by decode-error lines and the largest inter-frame jump
around the affected frames.  The winner must decode with zero errors.

Usage: trial_blinds.py <chain_dir> <label> [--max-cands N] [--apply]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chain_walk import CS, FileModel  # noqa: E402
from mp4_tables import parameter_sets  # noqa: E402
from rdr_reader import RdrObject  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402
from rconfig import CODE, SH_WORK as WSL_WORK, sh_argv, sh_script  # noqa: E402
from chain_walk import UNRESOLVED  # noqa: E402


class _Fixed:
    """Every placement type except the unresolved blind ones is fixed."""
    def __contains__(self, how):
        return how not in UNRESOLVED


FIXED = _Fixed()
MIN_PSNR = 28.0    # correct placements scored 33-38 dB vs the LRV
MIN_MARGIN = 10.0  # wrong candidates scored 12-16 dB


def owners(chain_dir: Path) -> dict[int, str]:
    own = {}
    for p in chain_dir.glob("*.json"):
        if p.name.endswith(".partial.json") or ".pre-" in p.name:
            continue
        d = json.loads(p.read_text())
        for m in d["mapping"]:
            if m["how"] in FIXED:
                own[m["chunk"]] = d["spec"]["label"]
    return own


def build_stream(raw, model, cmap, track, s_from, s_to) -> bytes:
    samples = track.samples()
    out = bytearray()
    for ps in parameter_sets(track):
        out += b"\x00\x00\x00\x01" + ps
    cache = {}

    def read(off, n):
        buf = bytearray()
        while n:
            k, w = divmod(off, CS)
            c = cmap[k]
            if c not in cache:
                cache[c] = raw.read_chunk(c)
            take = min(n, CS - w)
            buf += cache[c][w:w + take]
            off += take
            n -= take
        return bytes(buf)

    for sidx, off, size, _key in samples[s_from:s_to]:
        b = read(off, size)
        p = 0
        while p + 4 <= len(b):
            ln = int.from_bytes(b[p:p + 4], "big")
            out += b"\x00\x00\x00\x01" + b[p + 4:p + 4 + ln]
            p += 4 + ln
    return bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("chain_dir")
    ap.add_argument("label")
    ap.add_argument("--max-cands", type=int, default=80)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--near", type=int, default=None,
                    help="order candidates by distance to this chunk number")
    args = ap.parse_args()
    if not args.label.startswith("VID_"):
        raise SystemExit("trial decoding needs an independent reference (the companion LRV); "
                         "LRV blind clusters are not resolved this way")
    cdir = WORK / "chains" / args.chain_dir
    path = cdir / f"{args.label}.json"
    chain = json.loads(path.read_text())
    mapping = sorted(chain["mapping"], key=lambda m: m["k"])
    spec = chain["spec"]
    raw = RdrObject(chain["image"])
    model = FileModel(spec, raw)
    own = owners(cdir)
    cmap = {m["k"]: m["chunk"] for m in mapping}
    runs = []
    i = 0
    while i < len(mapping):
        if mapping[i]["how"] not in FIXED:
            j = i
            while j + 1 < len(mapping) and mapping[j + 1]["how"] not in FIXED:
                j += 1
            runs.append((i, j))
            i = j + 1
        else:
            i += 1
    tdir = WORK / "trials" / args.label
    tdir.mkdir(parents=True, exist_ok=True)
    results = []
    for i, j in runs:
        ks = [mapping[t]["k"] for t in range(i, j + 1)]
        lo, hi = mapping[i - 1]["chunk"], mapping[j + 1]["chunk"]
        free = [c for c in range(lo + 1, hi) if c not in own or c in {mapping[t]["chunk"] for t in range(i, j + 1)}]
        n = len(ks)
        options = [free[a:a + n] for a in range(len(free) - n + 1)
                   if free[a + n - 1] - free[a] == n - 1 or n == 1]
        if args.near is not None:
            # try the chunks closest to a predicted position first (e.g. the
            # usual header->cluster-1 spacing); the winner rule is unchanged
            options.sort(key=lambda o: abs(o[0] - args.near))
        options = options[: args.max_cands]
        a0, a1 = ks[0] * CS, (ks[-1] + 1) * CS
        jobs = []
        for ti, t in enumerate(model.tracks):
            if t.handler != "vide" or not t.sizes:
                continue
            ss = t.samples()
            hit = [s for s in ss if s[1] < a1 and s[1] + s[2] > a0]
            if not hit:
                continue
            first, last = hit[0][0], hit[-1][0]
            g0 = max(s[0] for s in ss if s[3] and s[0] <= first)
            g1 = min(last + 4, len(ss))
            jobs.append((ti, t, g0, g1, first, last))
        # reference: companion LRV frames (both fisheye halves) for each job's frame range
        lrv_label = "LRV_" + args.label.split("_", 1)[1]
        from pipeline import companion_path  # noqa: E402
        cand = companion_path(chain["image"], args.label)
        lrv_file = cand.relative_to(WORK).as_posix() if cand else None
        refs = {}
        steps = {}
        for ti, t, g0, g1, first, last in jobs:
            # VID may be 60 fps while the LRV is 30 fps: align by time
            vfps = len(t.sizes) / (t.duration / t.timescale)
            step = max(1, round(vfps / 30.0))
            steps[ti] = step
            if lrv_file:
                rL = f"trials/{args.label}/ref-t{ti}-L.gray"
                rR = f"trials/{args.label}/ref-t{ti}-R.gray"
                subprocess.run(sh_argv(f"cd '{WSL_WORK}' && bash {sh_script('trial_score.sh')} ref "
                                f"'{lrv_file}' {g0 // step} {(g1 - 1) // step} '{rL}' '{rR}'"), capture_output=True, text=True)
                refs[ti] = (rL, rR)

        scored = []
        for opt in options:
            cm = dict(cmap)
            for k, c in zip(ks, opt):
                cm[k] = c
            errs, frames, psnrs = 0, 0, []
            for ti, t, g0, g1, first, last in jobs:
                es = build_stream(raw, model, cm, t, g0, g1)
                ext = "h264" if t.codec.startswith("avc") else "hevc"
                fn = tdir / f"k{ks[0]}-c{opt[0]}-t{ti}.{ext}"
                fn.write_bytes(es)
                rel = fn.relative_to(WORK).as_posix()
                rL, rR = refs.get(ti, ("/dev/null", "/dev/null"))
                r = subprocess.run(sh_argv(f"cd '{WSL_WORK}' && bash {sh_script('trial_score.sh')} cand "
                                    f"'{rel}' {ext} '{rL}' '{rR}' {steps[ti]}"), capture_output=True, text=True)
                v = r.stdout.split()
                try:
                    errs += int(v[0]); frames += int(v[1])
                    ps = [float(x) for x in v[2:4] if x not in ("inf",)]
                    psnrs.append(max(ps) if ps else 0.0)
                except (ValueError, IndexError):
                    errs += 999
                fn.unlink(missing_ok=True)
            score = min(psnrs) if psnrs else 0.0
            scored.append({"chunks": opt, "errors": errs, "psnr_vs_lrv": round(score, 3), "frames": frames})
        scored.sort(key=lambda s: (s["errors"], -s["psnr_vs_lrv"]))
        clean = [s for s in scored if s["errors"] == 0]
        res = {"ks": ks, "prev": lo, "next": hi, "n_options": len(options), "top": scored[:5],
               "clean": len(clean)}
        # winner rule: needs a real reference, an absolute PSNR floor, and a
        # large margin over the best candidate with *different* content
        # (byte-identical twin chunks are the same answer)
        if not refs:
            res["refused"] = "no LRV reference assembled"
        elif clean and clean[0]["psnr_vs_lrv"] >= MIN_PSNR:
            top = clean[0]
            top_data = [raw.read_chunk(c) for c in top["chunks"]]
            rivals = [s for s in clean[1:] if [raw.read_chunk(c) for c in s["chunks"]] != top_data]
            margin = top["psnr_vs_lrv"] - (rivals[0]["psnr_vs_lrv"] if rivals else 0.0)
            res["margin_db"] = round(margin, 2)
            if margin >= MIN_MARGIN:
                res["winner"] = top["chunks"]
        results.append(res)
        print(json.dumps(res), flush=True)
    if args.apply and results and all("winner" in r for r in results):
        backup = path.with_name(path.stem + ".pre-trial.json")
        if not backup.exists():
            shutil.copy2(path, backup)
        for r in results:
            for k, c in zip(r["ks"], r["winner"]):
                for m in chain["mapping"]:
                    if m["k"] == k:
                        m["chunk"], m["how"] = c, "blind-resolved-decode"
        path.write_text(json.dumps(chain))
        print("applied")
    (tdir / "result.json").write_text(json.dumps(results, indent=1))
    return 0 if all("winner" in r for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
