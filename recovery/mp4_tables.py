#!/usr/bin/env python3
"""Minimal ISO-BMFF moov parser: per-track sample offsets/sizes/key flags."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"udta"}


def iter_atoms(buf: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, pos)
        hdr = 8
        if size == 1:
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            raise ValueError(f"bad atom {typ!r} size {size} at {pos}")
        yield typ, pos + hdr, pos + size
        pos += size


@dataclass
class Track:
    track_id: int = 0
    handler: str = ""
    codec: str = ""
    width: int = 0
    height: int = 0
    timescale: int = 0
    duration: int = 0
    sizes: list[int] = field(default_factory=list)
    chunk_offsets: list[int] = field(default_factory=list)
    stsc: list[tuple[int, int, int]] = field(default_factory=list)
    sync: set[int] | None = None  # 1-based sample numbers; None => all sync
    stts: list[tuple[int, int]] = field(default_factory=list)
    nal_length_size: int = 4
    config: bytes = b""  # raw avcC/hvcC payload

    def samples(self):
        """Yield (sample_index0, file_offset, size, is_key)."""
        out = []
        sidx = 0
        nchunks = len(self.chunk_offsets)
        for ei, (first_chunk, per_chunk, _desc) in enumerate(self.stsc):
            last_chunk = self.stsc[ei + 1][0] - 1 if ei + 1 < len(self.stsc) else nchunks
            for chunk in range(first_chunk, last_chunk + 1):
                off = self.chunk_offsets[chunk - 1]
                for _ in range(per_chunk):
                    size = self.sizes[sidx]
                    key = True if self.sync is None else (sidx + 1) in self.sync
                    out.append((sidx, off, size, key))
                    off += size
                    sidx += 1
        if sidx != len(self.sizes):
            raise ValueError(f"track {self.track_id}: stsc covers {sidx} of {len(self.sizes)} samples")
        return out

    @property
    def frame_count(self) -> int:
        return len(self.sizes)


def parse_moov(buf: bytes) -> tuple[dict, list[Track]]:
    """buf must start at the moov atom header."""
    size, typ = struct.unpack_from(">I4s", buf, 0)
    if typ != b"moov":
        raise ValueError("not a moov")
    info: dict = {}
    tracks: list[Track] = []

    def walk(start, end, trk: Track | None):
        for typ, s, e in iter_atoms(buf, start, end):
            if typ == b"mvhd":
                ver = buf[s]
                if ver == 1:
                    ts, dur = struct.unpack_from(">IQ", buf, s + 20)
                else:
                    ts, dur = struct.unpack_from(">II", buf, s + 12)
                info["timescale"], info["duration"] = ts, dur
            elif typ == b"trak":
                t = Track()
                walk(s, e, t)
                tracks.append(t)
            elif trk is None:
                if typ in CONTAINERS:
                    walk(s, e, None)
            elif typ in CONTAINERS:
                walk(s, e, trk)
            elif typ == b"tkhd":
                ver = buf[s]
                trk.track_id = struct.unpack_from(">I", buf, s + (20 if ver == 1 else 12))[0]
            elif typ == b"mdhd":
                ver = buf[s]
                if ver == 1:
                    trk.timescale, trk.duration = struct.unpack_from(">IQ", buf, s + 20)
                else:
                    trk.timescale, trk.duration = struct.unpack_from(">II", buf, s + 12)
            elif typ == b"hdlr":
                trk.handler = buf[s + 8 : s + 12].decode("latin1")
            elif typ == b"stsd":
                count = struct.unpack_from(">I", buf, s + 4)[0]
                if count:
                    esz, fourcc = struct.unpack_from(">I4s", buf, s + 8)
                    trk.codec = fourcc.decode("latin1")
                    entry = s + 8
                    if trk.handler == "vide":
                        trk.width, trk.height = struct.unpack_from(">HH", buf, entry + 32)
                        # find avcC / hvcC for NAL length size
                        for t2, s2, e2 in iter_atoms(buf, entry + 86, entry + esz):
                            if t2 == b"avcC":
                                trk.nal_length_size = (buf[s2 + 4] & 3) + 1
                                trk.config = bytes(buf[s2:e2])
                            elif t2 == b"hvcC":
                                trk.nal_length_size = (buf[s2 + 21] & 3) + 1
                                trk.config = bytes(buf[s2:e2])
            elif typ == b"stsz":
                fixed, count = struct.unpack_from(">II", buf, s + 4)
                if fixed:
                    trk.sizes = [fixed] * count
                else:
                    trk.sizes = list(struct.unpack_from(f">{count}I", buf, s + 12))
            elif typ == b"stco":
                count = struct.unpack_from(">I", buf, s + 4)[0]
                trk.chunk_offsets = list(struct.unpack_from(f">{count}I", buf, s + 8))
            elif typ == b"co64":
                count = struct.unpack_from(">I", buf, s + 4)[0]
                trk.chunk_offsets = list(struct.unpack_from(f">{count}Q", buf, s + 8))
            elif typ == b"stsc":
                count = struct.unpack_from(">I", buf, s + 4)[0]
                vals = struct.unpack_from(f">{count * 3}I", buf, s + 8)
                trk.stsc = [tuple(vals[i : i + 3]) for i in range(0, len(vals), 3)]
            elif typ == b"stss":
                count = struct.unpack_from(">I", buf, s + 4)[0]
                trk.sync = set(struct.unpack_from(f">{count}I", buf, s + 8))
            elif typ == b"stts":
                count = struct.unpack_from(">I", buf, s + 4)[0]
                vals = struct.unpack_from(f">{count * 2}I", buf, s + 8)
                trk.stts = [tuple(vals[i : i + 2]) for i in range(0, len(vals), 2)]

    walk(8, size, None)
    return info, tracks


def sample_layout(tracks: list[Track]):
    """All samples of all tracks sorted by file offset: (offset, size, track_idx, key, codec)."""
    rows = []
    for ti, t in enumerate(tracks):
        if not t.sizes:
            continue
        for sidx, off, size, key in t.samples():
            rows.append((off, size, ti, key, t.codec, sidx))
    rows.sort()
    return rows


def parameter_sets(trk: Track) -> list[bytes]:
    """NAL units (VPS/SPS/PPS) from avcC or hvcC."""
    c = trk.config
    out = []
    if not c:
        return out
    if trk.codec in ("avc1", "avc3"):
        n_sps = c[5] & 0x1F
        p = 6
        for _ in range(n_sps):
            ln = struct.unpack_from(">H", c, p)[0]
            out.append(c[p + 2:p + 2 + ln]); p += 2 + ln
        n_pps = c[p]; p += 1
        for _ in range(n_pps):
            ln = struct.unpack_from(">H", c, p)[0]
            out.append(c[p + 2:p + 2 + ln]); p += 2 + ln
    else:
        p = 22
        n_arrays = c[p]; p += 1
        for _ in range(n_arrays):
            p += 1
            n = struct.unpack_from(">H", c, p)[0]; p += 2
            for _ in range(n):
                ln = struct.unpack_from(">H", c, p)[0]
                out.append(c[p + 2:p + 2 + ln]); p += 2 + ln
    return out
