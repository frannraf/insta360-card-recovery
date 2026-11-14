#!/usr/bin/env python3
"""Decode-check candidates for the next cluster of a lost (partial) chain.

The partial chain's clusters k=0..n-1 are known; each candidate chunk is
appended as cluster n, every complete NAL of the frame(s) touching cluster n
is decoded (from the GOP's keyframe), and the result is compared with the
companion LRV frame in 16x16 blocks.  Score = blocks matching the LRV
(PSNR >= 28 dB) minus the baseline decoded without the candidate: a right
chunk adds picture that matches the LRV, a wrong one adds garbage.
Saves a PNG per candidate.  Read-only on the RDR; writes only under
work/trials/lost-<label>/.

Usage: lost_decode.py <chain_dir> <label> CHUNK [CHUNK ...]
"""

from __future__ import annotations

import json
import math
import operator
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fast_trial as ft  # noqa: E402
from chain_walk import CS, FileModel  # noqa: E402
from mp4_tables import parameter_sets  # noqa: E402
from rdr_reader import RdrObject  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402
S = ft.SIDE


def good_blocks(a: bytes, b: bytes, blk: int = 16) -> int:
    n = 0
    for y in range(0, S, blk):
        for x in range(0, S, blk):
            se = 0
            for r in range(y, y + blk):
                o = r * S + x
                se += sum(d * d for d in map(operator.sub, a[o:o + blk], b[o:o + blk]))
            if se == 0 or 10 * math.log10(255 * 255 * blk * blk / se) >= ft.GOOD_DB:
                n += 1
    return n


def main() -> int:
    chain_dir, label = sys.argv[1], sys.argv[2]
    cands = [int(x) for x in sys.argv[3:]]
    cdir = WORK / "chains" / chain_dir
    part = json.loads((cdir / f"{label}.partial.json").read_text())
    raw = RdrObject(part["image"])
    model = FileModel(part["spec"], raw)
    mp = sorted(part["mapping"], key=lambda m: m["k"])
    n = 0
    while n < len(mp) and mp[n]["k"] == n:
        n += 1
    known = b"".join(raw.read_chunk(m["chunk"]) for m in mp[:n])
    a0, a1 = n * CS, (n + 1) * CS
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
        raise SystemExit(f"cluster {n} holds no video sample data")
    ti, t, s = hit
    samples = t.samples()
    g0 = max(x[0] for x in samples if x[3] and x[0] <= s)
    ps = b"".join(b"\x00\x00\x00\x01" + p for p in parameter_sets(t))
    fmt = "h264" if t.codec.startswith("avc") else "hevc"

    def stream(buf: bytes) -> bytes:
        out = [ps]
        for sidx, off, size, key in samples[g0:s + 1]:
            p = off
            while p + 4 <= off + size and p + 4 <= len(buf):
                ln = int.from_bytes(buf[p:p + 4], "big")
                if ln < 2 or p + 4 + ln > len(buf):
                    break
                out.append(b"\x00\x00\x00\x01" + buf[p + 4:p + 4 + ln])
                p += 4 + ln
        return b"".join(out)

    out = WORK / "trials" / f"lost-{label}"
    out.mkdir(parents=True, exist_ok=True)
    rel = out.relative_to(WORK).as_posix()
    from pipeline import companion_path  # noqa: E402
    lrv = companion_path(part["image"], label)
    if lrv is None:
        raise SystemExit("companion LRV is not assembled")
    vfps = len(t.sizes) / (t.duration / t.timescale)
    lf = s // max(1, round(vfps / 30.0))
    subprocess.run(ft.sh_argv(f"cd '{ft.WSL_WORK}' && bash {ft.sh_script('trial_score.sh')} ref "
                    f"'{lrv.relative_to(WORK).as_posix()}' {lf} {lf} '{rel}/refL.gray' '{rel}/refR.gray'"),
                   capture_output=True)
    refs = [(out / f"ref{x}.gray").read_bytes() for x in "LR"]

    def run(buf: bytes, tag: str):
        (out / "c.es").write_bytes(stream(buf))
        r = ft.wsl(f"{rel}/c.es", f"{rel}/c.gray", fmt).split()
        g = (out / "c.gray").read_bytes()
        nfr = len(g) // ft.FRAME
        if nfr == 0:
            return None, r
        f = g[(nfr - 1) * ft.FRAME:nfr * ft.FRAME]
        subprocess.run(ft.sh_argv(f"cd '{ft.WSL_WORK}' && ffmpeg -v error -f rawvideo -pix_fmt gray "
                        f"-s {S}x{S} -i {rel}/c.gray -vf 'select=eq(n\\,{nfr - 1})' -frames:v 1 -y {rel}/{tag}.png"),
                       capture_output=True)
        return max(good_blocks(f, r_) for r_ in refs), r

    base, r = run(known, "base")
    if base is None:
        # nothing of this frame/lens is decodable without cluster n (it starts
        # a new lens or frame): score a deterministic random chunk instead, so
        # gains are measured against a known-wrong candidate
        import random
        base, r = run(known + random.Random(0).randbytes(CS), "base-random")
    print(json.dumps({"label": label, "cluster": n, "track": ti, "frame": s, "gop_start": g0,
                      "lrv_frame": lf, "baseline_good_blocks": base, "baseline_decode": r}), flush=True)
    for c in cands:
        g, r = run(known + raw.read_chunk(c), f"c{c}")
        print(json.dumps({"chunk": c, "good_blocks": g, "gain": None if g is None or base is None else g - base,
                          "decode": r}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
