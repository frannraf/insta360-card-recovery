#!/usr/bin/env python3
"""Parallel read-only marker scan of an RDR object (ftyp starts + moov atoms).

Outputs work/scan-results/<image>-markers-py.json:
  {"ftyp_chunks": [...], "moov": [[logical_offset, size], ...]}
Progress is checkpointed per range so an interrupted scan can resume.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rdr_reader import RdrObject  # noqa: E402

CS = 262144
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402


def scan_range(args):
    image, lo, hi = args
    raw = RdrObject(image)
    ftyp, moov = [], []
    for c in range(lo, hi):
        d = raw.read_chunk(c)
        if d[4:8] == b"ftyp" and struct.unpack_from(">I", d, 0)[0] in (20, 24, 28, 32):
            ftyp.append(c)
        i = d.find(b"moov")
        while i >= 0:
            if i >= 4:
                size = struct.unpack_from(">I", d, i - 4)[0]
                if 100_000 <= size <= 64 << 20 and d[i + 8:i + 12] == b"mvhd":
                    moov.append([c * CS + i - 4, size])
            i = d.find(b"moov", i + 1)
    return lo, hi, ftyp, moov


def main() -> int:
    image = sys.argv[1]
    procs = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    raw = RdrObject(image)
    n = raw.chunk_count
    step = 5000
    (WORK / "scan-results").mkdir(parents=True, exist_ok=True)
    ck = WORK / "scan-results" / f"{image}-markers-py.ckpt.json"
    done = json.loads(ck.read_text()) if ck.exists() else {"ranges": [], "ftyp": [], "moov": []}
    have = {tuple(r) for r in done["ranges"]}
    jobs = [(image, lo, min(n, lo + step)) for lo in range(0, n, step) if (lo, min(n, lo + step)) not in have]
    t0 = time.time()
    with mp.Pool(procs) as pool:
        for lo, hi, f, m in pool.imap_unordered(scan_range, jobs):
            done["ranges"].append([lo, hi])
            done["ftyp"] += f
            done["moov"] += m
            ck.write_text(json.dumps(done))
            cnt = sum(h - l for l, h in done["ranges"])
            print(f"{cnt}/{n} chunks ftyp={len(done['ftyp'])} moov={len(done['moov'])} {time.time()-t0:.0f}s", flush=True)
    out = WORK / "scan-results" / f"{image}-markers-py.json"
    out.write_text(json.dumps({"ftyp_chunks": sorted(done["ftyp"]), "moov": sorted(done["moov"])}))
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
