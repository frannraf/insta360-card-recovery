#!/usr/bin/env python3
"""Check that a card image fits the recovery's assumptions (read-only).

The method treats one 256 KiB image chunk as one exFAT cluster, so it needs:
  * an exFAT volume (at the image start, or as the first MBR partition),
  * a cluster size of exactly 256 KiB,
  * the cluster heap starting on a 256 KiB boundary of the image.
Prints the volume geometry and a verdict.

Usage: card_info.py <image name or path>
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rdr_reader import RdrObject  # noqa: E402

CHUNK = 256 * 1024


def exfat_at(img: RdrObject, off: int) -> dict | None:
    bs = img.read(off, 512)
    if bs[3:11] != b"EXFAT   ":
        return None
    bps_shift, spc_shift = bs[108], bs[109]
    sector = 1 << bps_shift
    return {"partition_offset": off, "bytes_per_sector": sector,
            "cluster_size": sector << spc_shift,
            "heap_offset": off + struct.unpack_from("<I", bs, 88)[0] * sector,
            "cluster_count": struct.unpack_from("<I", bs, 92)[0],
            "volume_bytes": struct.unpack_from("<Q", bs, 72)[0] * sector}


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    img = RdrObject(sys.argv[1])
    info = exfat_at(img, 0)
    if info is None:  # MBR: first partition entry at 0x1BE, LBA start at +8
        mbr = img.read(0, 512)
        if mbr[510:512] == b"\x55\xaa":
            lba = struct.unpack_from("<I", mbr, 0x1BE + 8)[0]
            info = exfat_at(img, lba * 512) if lba else None
    if info is None:
        print(json.dumps({"image": sys.argv[1], "verdict": "no exFAT volume found at the start or in MBR "
                          "partition 1 (the boot region may be overwritten; the marker scan can still run)"}))
        return 1
    ok_cluster = info["cluster_size"] == CHUNK
    ok_align = info["heap_offset"] % CHUNK == 0
    info.update({"image": sys.argv[1], "image_kind": "raw" if img.raw else "rdr",
                 "heap_start_chunk": info["heap_offset"] // CHUNK if ok_align else None,
                 "verdict": "ok" if ok_cluster and ok_align else
                 ("cluster size is not 256 KiB: not supported" if not ok_cluster else
                  "cluster heap is not 256 KiB aligned in this image: not supported")})
    print(json.dumps(info, indent=1))
    return 0 if ok_cluster and ok_align else 2


if __name__ == "__main__":
    raise SystemExit(main())
