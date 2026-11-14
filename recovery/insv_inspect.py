#!/usr/bin/env python3
"""Structural dump of an .insv/.lrv file for side-by-side comparison.

Top-level atoms, moov children (incl. udta/meta), per-track sample entry and
codec config, and the Insta360 trailer: version, record index (id, size,
offset, cluster alignment), record-header verification, and a shallow
protobuf field dump of the 0x101 info record.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mp4_tables import iter_atoms, parse_moov  # noqa: E402

MAG = b"8db42d694ccc418790edff439fe026bf"


def pb_fields(b: bytes, depth=0, limit=60):
    out, p = [], 0
    while p < len(b) and len(out) < limit:
        try:
            key, p = varint(b, p)
        except IndexError:
            break
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, p = varint(b, p)
            out.append((fn, "varint", v))
        elif wt == 2:
            ln, p = varint(b, p)
            v = b[p:p + ln]
            p += ln
            s = v.decode("utf-8", "replace") if all(32 <= c < 127 for c in v) and v else None
            out.append((fn, "bytes", s if s is not None else f"<{len(v)} bytes>"))
        elif wt == 5:
            out.append((fn, "fixed32", struct.unpack_from("<I", b, p)[0])); p += 4
        elif wt == 1:
            out.append((fn, "fixed64", struct.unpack_from("<Q", b, p)[0])); p += 8
        else:
            out.append((fn, f"wt{wt}", "?"))
            break
    return out


def varint(b, p):
    v = s = 0
    while True:
        c = b[p]; p += 1
        v |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            return v, p


def main():
    path = Path(sys.argv[1])
    data_len = path.stat().st_size
    fh = open(path, "rb")
    rep = {"file": path.name, "size": data_len, "top": []}
    pos = 0
    moov = inst_off = None
    while pos < data_len:
        fh.seek(pos)
        h = fh.read(16)
        size, typ = struct.unpack(">I4s", h[:8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", h[8:16])[0]; hdr = 16
        rep["top"].append([typ.decode("latin1"), pos, size])
        if typ == b"ftyp":
            fh.seek(pos); rep["ftyp"] = fh.read(size)[8:].hex()
        if typ == b"moov":
            fh.seek(pos); moov = fh.read(size)
        if typ == b"inst":
            inst_off = pos
        if size < 8:
            break
        pos += size
    info, tracks = parse_moov(moov)
    rep["moov_children"] = [t.decode("latin1") for t, s, e in iter_atoms(moov, 8, len(moov))]
    udta = [(t, s, e) for t, s, e in iter_atoms(moov, 8, len(moov)) if t == b"udta"]
    if udta:
        t, s, e = udta[0]
        rep["udta_children"] = [(tt.decode("latin1"), ee - ss) for tt, ss, ee in iter_atoms(moov, s, e)]
    rep["mvhd"] = info
    rep["tracks"] = [{"handler": t.handler, "codec": t.codec, "w": t.width, "h": t.height,
                      "samples": len(t.sizes), "timescale": t.timescale, "config_len": len(t.config)}
                     for t in tracks]
    fh.seek(data_len - 72)
    tail = fh.read(72)
    rep["trailer_magic_ok"] = tail[40:] == MAG
    rep["trailer_len"], rep["trailer_ver"] = struct.unpack_from("<II", tail, 32)
    fh.seek(data_len - 78)
    fmt, rid, isz = struct.unpack("<BBI", fh.read(6))
    fh.seek(data_len - 78 - isz)
    idx = fh.read(isz)
    base = inst_off + 8
    recs = []
    for i in range(0, len(idx) - 9, 10):
        r, sz, off = struct.unpack_from("<HII", idx, i)
        if not sz:
            continue
        fh.seek(base + off + sz)
        h6 = fh.read(6)
        ok = h6 == struct.pack(">H", r) + struct.pack("<I", sz)
        recs.append({"id": hex(r), "size": sz, "aligned": (base + off) % 262144 == 0, "hdr_ok": ok})
    rep["records"] = recs
    info_rec = next((r for r in recs if r["id"] == "0x101"), None)
    if info_rec:
        r = next(x for x in range(0, len(idx) - 9, 10) if struct.unpack_from("<H", idx, x)[0] == 0x101)
        _, sz, off = struct.unpack_from("<HII", idx, r)
        fh.seek(base + off)
        rep["info_fields"] = pb_fields(fh.read(sz))
    print(json.dumps(rep, indent=1, default=str))


if __name__ == "__main__":
    main()
