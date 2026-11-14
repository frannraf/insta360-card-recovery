#!/usr/bin/env python3
"""Assemble a file from a verified chain mapping (read-only on the RDR).

Writes clusters in logical order through mdat end, then appends exactly
moov_size + inst_size bytes from the recovered raw tail.  Never overwrites.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rdr_reader import RdrObject  # noqa: E402

CS = 262144
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402
INST_MAGIC = bytes.fromhex("8db42d694ccc418790edff439fe026bf")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("chain", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--allow-blind", action="store_true")
    ap.add_argument("--tail-chain", type=Path, default=None,
                    help="optional JSON list of raw chunks to use for the moov+inst tail")
    args = ap.parse_args()

    chain = json.loads(args.chain.read_text())
    spec = chain["spec"]
    mapping = sorted(chain["mapping"], key=lambda m: m["k"])
    if chain["status"]["status"] != "complete" or len(mapping) != chain["nclusters"]:
        raise SystemExit("chain incomplete")
    if [m["k"] for m in mapping] != list(range(chain["nclusters"])):
        raise SystemExit("chain has holes")
    blind = [m for m in mapping if m["how"].startswith("blind")]
    if blind and not args.allow_blind:
        raise SystemExit(f"{len(blind)} blind clusters; refusing without --allow-blind")

    raw = RdrObject(chain["image"])
    mdat_end = None
    # mdat end = last sample end: recompute from header declared size
    hdr = raw.read_chunk(spec["header_chunk"])[:64]
    if hdr[4:8] != b"ftyp":
        raise SystemExit("header chunk is not ftyp")
    pos = 0
    while pos < 64:
        size, typ = struct.unpack_from(">I4s", hdr, pos)
        if typ == b"mdat":
            if size == 1:
                size = struct.unpack_from(">Q", hdr, pos + 8)[0]
            mdat_end = pos + size
            break
        pos += size
    if mdat_end is None:
        raise SystemExit("no mdat in header")

    tail_size = spec["moov_size"] + spec["inst_size"]
    tail = raw.read(spec["moov_start"], tail_size)
    if tail[4:8] != b"moov" or struct.unpack_from(">I", tail, 0)[0] != spec["moov_size"]:
        raise SystemExit("tail moov mismatch")
    inst = tail[spec["moov_size"]:]
    if inst[4:8] != b"inst" or struct.unpack_from(">I", inst, 0)[0] != spec["inst_size"]:
        raise SystemExit("tail inst mismatch")
    trailer_ok = INST_MAGIC in inst[-64:]

    name = Path(spec["name"])
    out = args.out or (WORK / "assembled" / f"{name.stem}-chain-{chain['image']}{name.suffix}")
    out.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    written = 0
    with open(out, "xb") as fh:
        for m in mapping:
            data = raw.read_chunk(m["chunk"])
            take = min(CS, mdat_end - m["k"] * CS)
            block = data[:take]
            fh.write(block)
            digest.update(block)
            written += len(block)
        if written != mdat_end:
            raise SystemExit(f"wrote {written} != mdat_end {mdat_end}")
        fh.write(tail)
        digest.update(tail)
        fh.flush()
        os.fsync(fh.fileno())
    size = out.stat().st_size
    print(json.dumps({"output": str(out), "size": size, "mdat_end": mdat_end, "tail": tail_size,
                      "sha256": digest.hexdigest().upper(), "inst_trailer_magic_at_end": trailer_ok,
                      "blind_clusters": len(blind)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
