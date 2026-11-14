#!/usr/bin/env python3
"""Build a full header<->moov catalog for any image from scan_markers_py output.

Output: work/scan-analysis/<image>-full-catalog.json (schema:
 kind, name_time, creation_utc, duration, moov_start/size, inst_start/
size, mdat_end, tracks, header_chunk, file_size).
"""

from __future__ import annotations

import datetime as dt
import json
import struct
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mp4_tables import parse_moov, sample_layout  # noqa: E402
from rdr_reader import RdrObject  # noqa: E402

CS = 262144
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402
EPOCH = dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc)


def mvhd_creation(moov: bytes) -> dt.datetime:
    i = moov.find(b"mvhd")
    ver = moov[i + 4]
    secs = struct.unpack_from(">Q" if ver == 1 else ">I", moov, i + 8)[0]
    return EPOCH + dt.timedelta(seconds=secs)



def header_mdat_end(chunk: bytes):
    """Parse an MP4 file's first cluster: brand and where mdat data ends."""
    pos = 0
    if chunk[4:8] != b"ftyp":
        return None
    brand = chunk[8:12].decode("latin1")
    while pos + 16 <= 256:
        size, typ = struct.unpack_from(">I4s", chunk, pos)
        if typ == b"mdat":
            hdr = 8
            if size == 1:
                size = struct.unpack_from(">Q", chunk, pos + 8)[0]
                hdr = 16
            return {"brand": brand, "mdat_atom": pos, "mdat_data": pos + hdr, "mdat_end": pos + size}
        if size < 8:
            return None
        pos += size
    return None

def main() -> int:
    image = sys.argv[1]
    raw = RdrObject(image)
    mk = json.loads((WORK / "scan-results" / f"{image}-markers-py.json").read_text())
    headers = []
    for c in sorted(set(mk["ftyp_chunks"])):
        h = header_mdat_end(raw.read_chunk(c)[:256])
        if h:
            h["header_chunk"] = c
            headers.append(h)
    by_end = defaultdict(list)
    for h in headers:
        by_end[h["mdat_end"]].append(h)
    files, bad = [], []
    for start, size in sorted({tuple(x) for x in mk["moov"]}):
        try:
            moov = raw.read(start, size)
            info, tracks = parse_moov(moov)
            lay = sample_layout(tracks)
            if not lay:
                raise ValueError("empty")
        except Exception as exc:  # noqa: BLE001
            bad.append([start, str(exc)])
            continue
        mdat_end = max(o + s for o, s, *_ in lay)
        ih = raw.read(start + size, 8)
        inst_size, inst_type = struct.unpack(">I4s", ih)
        if inst_type != b"inst":
            inst_size = 0
        v = [t for t in tracks if t.handler == "vide"]
        kind = "VID" if (len(v) >= 2 or any(t.codec in ("hvc1", "hev1") for t in v)) else "LRV"
        created = mvhd_creation(moov)
        name_time = (created + dt.timedelta(hours=1)).strftime("%Y%m%d_%H%M%S")
        cands = by_end.get(mdat_end, [])
        before = [h for h in cands if h["header_chunk"] * CS < start]
        pick = max(before, key=lambda h: h["header_chunk"]) if before else None
        files.append({
            "kind": kind, "name_time": name_time,
            "creation_utc": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration": info["duration"] / info["timescale"],
            "moov_start": start, "moov_size": size, "inst_start": start + size, "inst_size": inst_size,
            "mdat_end": mdat_end,
            "tracks": [[t.handler, t.codec, t.width, t.height, t.frame_count, t.timescale, t.duration]
                       for t in tracks],
            "header_candidates": [h["header_chunk"] for h in cands],
            "header_chunk": pick["header_chunk"] if pick else None,
            "file_size": mdat_end + size + inst_size,
        })
    files.sort(key=lambda f: (f["creation_utc"], f["kind"]))
    out = WORK / "scan-analysis" / f"{image}-full-catalog.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"files": files, "unparsed_moov": bad, "headers": len(headers)}, indent=1))
    paired = sum(1 for f in files if f["header_chunk"] is not None)
    print(f"headers={len(headers)} moov={len(files)} paired={paired} unparsed={len(bad)}")
    for f in files:
        print(f["kind"], f["name_time"], f"{f['duration']:.1f}s", f["header_chunk"], f"{f['file_size']/1e6:.0f}MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
