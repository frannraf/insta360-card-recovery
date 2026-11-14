#!/usr/bin/env python3
"""Fast trial decoding of one blind cluster against every chunk in a window.

trial_blinds.py starts ffmpeg once per candidate (~5 s each), so it only tries
the free chunks nearest the gap.  This tool scores thousands of chunks:

 * A blind cluster lies wholly inside one emulation-prevented NAL payload, so
   the right chunk cannot contain the byte sequences 00 00 00 / 00 00 01 /
   00 00 02, and every 00 00 03 is followed by a byte <= 03.  Chunks that
   break this are rejected without decoding.
 * The rest are decoded in batches, one ffmpeg run per batch.  Each unit is
   the GOP from its keyframe up to the frame holding the cluster; every unit
   starts at an IDR with its own parameter sets, so units are independent.
 * The decoded frame is compared with the companion LRV frame in 16-row
   bands (both fisheye halves; the better half is used).  Score = number of
   bands with PSNR >= 28 dB, then mean band PSNR.

Owned chunks are tested too and reported with their owner, so a cluster that
another chain holds is visible.  Read-only on the RDR; writes only under
work/trials/fast-<label>/; never modifies a chain (it reports).

Usage: fast_trial.py <chain_dir> <label> --k K [--lo N] [--hi N] [--batch 24]
"""

from __future__ import annotations

import argparse
import json
import math
import operator
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chain_walk import CS, UNRESOLVED, FileModel  # noqa: E402
from mp4_tables import parameter_sets  # noqa: E402
from rdr_reader import RdrObject  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402
from rconfig import CODE, SH_WORK as WSL_WORK, sh_argv, sh_script  # noqa: E402
SIDE = 416
FRAME = SIDE * SIDE
BAND = 16
GOOD_DB = 28.0


def nal_payload_ok(b: bytes) -> bool:
    if b"\x00\x00\x00" in b or b"\x00\x00\x01" in b or b"\x00\x00\x02" in b:
        return False
    i = b.find(b"\x00\x00\x03")
    while i != -1:
        if i + 3 < len(b) and b[i + 3] > 3:
            return False
        i = b.find(b"\x00\x00\x03", i + 1)
    return True


def owners(cdir: Path) -> dict[int, str]:
    own = {}
    for p in cdir.glob("*.json"):
        if p.name.endswith(".partial.json") or ".pre-" in p.name:
            continue
        d = json.loads(p.read_text())
        for m in d["mapping"]:
            own.setdefault(m["chunk"], f"{d['spec']['label']}:k{m['k']}:{m['how']}")
    return own


def band_psnr(a: bytes, b: bytes) -> list[float]:
    out = []
    row = SIDE * BAND
    for o in range(0, FRAME, row):
        se = sum(d * d for d in map(operator.sub, a[o:o + row], b[o:o + row]))
        out.append(99.0 if se == 0 else 10 * math.log10(255 * 255 * row / se))
    return out


def good_blocks(a: bytes, b: bytes, blk: int = 16) -> int:
    """16x16 blocks of `a` that match `b` (PSNR >= GOOD_DB): partial frames."""
    n = 0
    for y in range(0, SIDE, blk):
        for x in range(0, SIDE, blk):
            se = 0
            for r in range(y, y + blk):
                o = r * SIDE + x
                se += sum(d * d for d in map(operator.sub, a[o:o + blk], b[o:o + blk]))
            if se == 0 or 10 * math.log10(255 * 255 * blk * blk / se) >= GOOD_DB:
                n += 1
    return n


def score(frame: bytes, refs: list[bytes]):
    best = None
    for side, r in enumerate(refs):
        bp = band_psnr(frame, r)
        key = (sum(1 for x in bp if x >= GOOD_DB), sum(bp) / len(bp))
        if best is None or key > best[0]:
            best = (key, side, bp)
    return best


def wsl(*args: str) -> str:
    r = subprocess.run(sh_argv(f"cd '{WSL_WORK}' && bash {sh_script('fast_decode.sh')} " + " ".join(args)),
                       capture_output=True, text=True)
    return r.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("chain_dir")
    ap.add_argument("label")
    ap.add_argument("--k", type=int, required=True)
    ap.add_argument("--lo", type=int, default=None)
    ap.add_argument("--hi", type=int, default=None)
    ap.add_argument("--batch", type=int, default=24)
    ap.add_argument("--set", action="append", default=[], metavar="K=CHUNK",
                    help="override other clusters (e.g. an already-found blind)")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--solo", type=int, default=10, help="re-decode this many top candidates one at a time")
    ap.add_argument("--free-only", action="store_true", help="skip chunks owned by other chains")
    ap.add_argument("--partial", action="store_true",
                    help="lost chain: load <label>.partial.json, decode only NALs whose clusters are "
                         "known (mapping + --set), score by matching 16x16 blocks")
    args = ap.parse_args()
    if not args.label.startswith("VID_"):
        raise SystemExit("needs the companion LRV as reference: VID labels only")
    K = args.k
    cdir = WORK / "chains" / args.chain_dir
    chain = json.loads((cdir / f"{args.label}{'.partial' if args.partial else ''}.json").read_text())
    mapping = sorted(chain["mapping"], key=lambda m: m["k"])
    cmap = {m["k"]: m["chunk"] for m in mapping}
    for s in args.set:
        k, c = map(int, s.split("="))
        cmap[k] = c
    raw = RdrObject(chain["image"])
    model = FileModel(chain["spec"], raw)
    # window: between the nearest fixed neighbours of K
    fixed = {m["k"]: m["chunk"] for m in mapping if m["how"] not in UNRESOLVED and m["k"] != K}
    fixed.update({k: c for k, c in cmap.items() if k != K and k not in {m["k"] for m in mapping}})
    below = [k for k in fixed if k < K]
    above = [k for k in fixed if k > K]
    if args.lo is None and not below or args.hi is None and not above:
        raise SystemExit("no known neighbour on one side: pass --lo/--hi")
    lo = args.lo if args.lo is not None else fixed[max(below)] + 1
    hi = args.hi if args.hi is not None else fixed[min(above)] - 1

    # locate the sample (frame) and track holding cluster K
    a0, a1 = K * CS, (K + 1) * CS
    hit = None
    for ti, t in enumerate(model.tracks):
        if t.handler != "vide" or not t.sizes:
            continue
        for sidx, off, size, key in t.samples():
            if off < a1 and off + size > a0:
                hit = (ti, t, sidx)
                break
        if hit:
            break
    if hit is None:
        raise SystemExit(f"cluster {K} is not inside any video sample")
    ti, t, s = hit
    samples = t.samples()
    g0 = max(x[0] for x in samples if x[3] and x[0] <= s)
    nf = s - g0 + 1
    fmt = "h264" if t.codec.startswith("avc") else "hevc"

    cache = {}

    def rd(off, n):
        out = bytearray()
        while n:
            k, w = divmod(off, CS)
            c = cmap[k]
            if c not in cache:
                cache[c] = raw.read_chunk(c)
            x = min(n, CS - w)
            out += cache[c][w:w + x]
            off += x
            n -= x
        return bytes(out)

    def known(x0, x1):
        return all(k == K or k in cmap for k in range(x0 // CS, (x1 - 1) // CS + 1))

    pieces = [b"".join(b"\x00\x00\x00\x01" + p for p in parameter_sets(t))]
    for sidx, off, size, key in samples[g0:s + 1]:
        p = 0
        while p + 4 <= size:
            if args.partial and not known(off + p, off + p + 4):
                break
            ln = int.from_bytes(rd(off + p, 4), "big")
            n0, n1 = off + p + 4, off + p + 4 + ln
            if args.partial and not known(n0, n1):
                break  # NAL reaches an unknown cluster: drop it and the rest of the sample
            pieces.append(b"\x00\x00\x00\x01")
            if n0 <= a0 and a1 <= n1:
                pieces += [rd(n0, a0 - n0), None, rd(a1, n1 - a1)]
            elif n0 < a1 and n1 > a0:
                raise SystemExit(f"cluster {K} holds a NAL boundary: not blind")
            else:
                pieces.append(rd(n0, ln))
            p += 4 + ln
    if None not in pieces:
        raise SystemExit("cluster not found inside a NAL of the unit")
    i = pieces.index(None)
    pre, post = b"".join(pieces[:i]), b"".join(pieces[i + 1:])

    out = WORK / "trials" / f"fast-{args.label}"
    out.mkdir(parents=True, exist_ok=True)
    rel = out.relative_to(WORK).as_posix()
    # reference: LRV frame at the same time as frame s, both halves
    from pipeline import companion_path  # noqa: E402
    lrv = companion_path(chain["image"], args.label)
    if lrv is None:
        raise SystemExit("companion LRV is not assembled")
    vfps = len(t.sizes) / (t.duration / t.timescale)
    lf = s // max(1, round(vfps / 30.0))
    subprocess.run(sh_argv(f"cd '{WSL_WORK}' && bash {sh_script('trial_score.sh')} ref "
                    f"'{lrv.relative_to(WORK).as_posix()}' {lf} {lf} '{rel}/k{K}-refL.gray' '{rel}/k{K}-refR.gray'"),
                   capture_output=True, text=True)
    refs = [(out / f"k{K}-ref{x}.gray").read_bytes() for x in "LR"]
    if any(len(r) != FRAME for r in refs):
        raise SystemExit("reference frames missing")

    own = owners(cdir)
    t0 = time.time()
    cands, rejected = [], 0
    for c in range(lo, hi + 1):
        if args.free_only and c in own:
            continue
        d = raw.read_chunk(c)
        if nal_payload_ok(d):
            cands.append(c)
        else:
            rejected += 1
    print(json.dumps({"label": args.label, "k": K, "track": ti, "frame": s, "gop_start": g0,
                      "window": [lo, hi], "tested": len(cands), "rejected_by_bytes": rejected,
                      "lrv_frame": lf, "prefilter_s": round(time.time() - t0, 1)}), flush=True)

    results = []

    def decode(batch):
        tag = f"batch-k{K}-{os.getpid()}"  # unique: runs for one label may overlap
        es = out / f"{tag}.es"
        with open(es, "wb") as fh:
            for c in batch:
                fh.write(pre)
                fh.write(raw.read_chunk(c))
                fh.write(post)
        res = wsl(f"{rel}/{tag}.es", f"{rel}/{tag}.gray", fmt).split()
        g = (out / f"{tag}.gray").read_bytes()
        return g, int(res[1]) if len(res) > 1 else -1

    done = 0
    for b0 in range(0, len(cands), args.batch):
        batch = cands[b0:b0 + args.batch]
        g, errs = decode(batch)
        if len(g) == FRAME * nf * len(batch):
            frames = [g[(j * nf + nf - 1) * FRAME:(j * nf + nf) * FRAME] for j in range(len(batch))]
            errs_each = [None] * len(batch)
        else:
            # frame count off: decode this batch one unit at a time
            frames, errs_each = [], []
            for c in batch:
                g1, e1 = decode([c])
                frames.append(g1[(nf - 1) * FRAME:nf * FRAME] if len(g1) >= nf * FRAME else b"")
                errs_each.append(e1)
        for c, f, e in zip(batch, frames, errs_each):
            if len(f) != FRAME:
                results.append({"chunk": c, "good": -1, "mean": 0.0, "errors": e, "owner": own.get(c, "free")})
                continue
            if args.partial:
                gb = [good_blocks(f, r) for r in refs]
                results.append({"chunk": c, "good": max(gb), "mean": 0.0, "side": "LR"[gb.index(max(gb))],
                                "errors": e, "owner": own.get(c, "free"), "unit": "16x16 blocks"})
                continue
            (good, mean), side, bp = score(f, refs)
            results.append({"chunk": c, "good": good, "mean": round(mean, 2), "side": "LR"[side],
                            "errors": e, "owner": own.get(c, "free"),
                            "bands": "".join(str(min(9, int(x // 5))) for x in bp)})
        done += len(batch)
        if done % (args.batch * 20) == 0:
            print(f"  {done}/{len(cands)} {time.time() - t0:.0f}s", flush=True)
    results.sort(key=lambda r: (-r["good"], -r["mean"]))
    # Inside a batch, a garbage unit can inherit picture from the previous
    # (correct) unit's frame buffer, inflating its score.  Re-decode the top
    # candidates one per ffmpeg run and keep only those solo scores.
    for r in results[:args.solo]:
        g1, e1 = decode([r["chunk"]])
        f = g1[(nf - 1) * FRAME:nf * FRAME] if len(g1) >= nf * FRAME else b""
        if len(f) != FRAME:
            r.update({"good": -1, "mean": 0.0, "errors": e1, "solo": True})
        elif args.partial:
            gb = [good_blocks(f, x) for x in refs]
            r.update({"good": max(gb), "errors": e1, "solo": True})
        else:
            (good, mean), side, bp = score(f, refs)
            r.update({"good": good, "mean": round(mean, 2), "side": "LR"[side], "errors": e1, "solo": True,
                      "bands": "".join(str(min(9, int(x // 5))) for x in bp)})
    results.sort(key=lambda r: (not r.get("solo"), -r["good"], -r["mean"]))
    for x in out.glob(f"batch-k{K}-{os.getpid()}.*"):
        x.unlink(missing_ok=True)
    (out / f"k{K}-result.json").write_text(json.dumps(results, indent=0))
    (out / f"k{K}-args.json").write_text(json.dumps({"set": args.set, "window": [lo, hi], "partial": args.partial,
                                                     "frame": s, "track": ti, "tested": len(cands)}))
    for r in results[: args.top]:
        print(json.dumps(r), flush=True)
    print(f"done {len(results)} in {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
