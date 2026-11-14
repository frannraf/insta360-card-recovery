#!/usr/bin/env python3
"""Strictly read-only random access to a card image, in 256 KiB chunks.

Two image kinds are accepted:
  * indexed .rdr images (zlib or raw data records, compact chunk index frame
    type 0x90); the decoded chunk index is cached under <work>/rdr-index-cache
    so later processes start instantly;
  * raw card images (dd / .img / .bin of the card), read directly at
    chunk * 256 KiB.
The recovery treats one chunk as one exFAT cluster, so the cluster size must be
256 KiB and the cluster heap must start on a 256 KiB boundary of the image
(card_info.py checks this).  Images are only ever opened with mode "rb".
"""

from __future__ import annotations

import array
import os
import struct
import sys
import zlib
from pathlib import Path

MAGIC = 0xD754DA33
FILE_HEADER_SIZE = 52
RAW_DATA_FLAGS = 0x18000020
ZLIB_DATA_FLAGS = 0x18040220
RAW_CHUNK_INDEX_FLAGS = 0x08000090
ZLIB_CHUNK_INDEX_FLAGS = 0x08040290
ARCHIVE_DIRECTORY_FLAGS = 0x00000008
DIRECTORY_POINTER_FLAGS = 0x00000003

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402
CACHE_DIR = WORK / "rdr-index-cache"

from rconfig import IMAGES  # noqa: E402  (image name -> path, from recovery.json)
RAW_CHUNK = 256 * 1024


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def _u64(b: bytes, o: int) -> int:
    return struct.unpack_from("<Q", b, o)[0]


class RdrObject:
    def __init__(self, image: str | Path, object_index: int = 0):
        self.path = Path(IMAGES.get(str(image), image))
        self.object_index = object_index
        self.fh = open(self.path, "rb", buffering=0)  # read-only, never written
        self.file_size = os.fstat(self.fh.fileno()).st_size
        header = self._pread(0, FILE_HEADER_SIZE)
        self.raw = _u32(header, 0) != MAGIC
        if self.raw:
            # plain card image: chunk i is bytes [i*256K, (i+1)*256K)
            self.chunk_size = RAW_CHUNK
            self.logical_size = self.file_size
            return
        if _u32(header, 4) != FILE_HEADER_SIZE:
            raise ValueError("not an RDR image")
        if _u64(header, 44) != self.file_size:
            raise ValueError("RDR declared size mismatch")
        self._load_index()

    # -- low level -------------------------------------------------------
    def _pread(self, offset: int, length: int) -> bytes:
        # Images often live on a NAS: a transient SMB error (EINVAL, EIO, a
        # dropped session) must not kill an hours-long scan.  Retry with
        # backoff, reopening the (read-only) handle; a short read is not
        # transient and fails at once.
        import time
        for attempt in range(6):
            try:
                data = os.pread(self.fh.fileno(), length, offset) if hasattr(os, "pread") else None
                if data is None:
                    self.fh.seek(offset)
                    data = self.fh.read(length)
                break
            except OSError:
                if attempt == 5:
                    raise
                time.sleep(2 ** attempt)
                try:
                    self.fh.close()
                except OSError:
                    pass
                self.fh = open(self.path, "rb", buffering=0)  # read-only, never written
        if len(data) != length:
            raise EOFError(f"short read at {offset}")
        return data

    def _cache_path(self) -> Path:
        stem = self.path.stem.replace(" ", "_")
        return CACHE_DIR / f"{stem}-{self.file_size}-obj{self.object_index}.idx"

    def _load_index(self) -> None:
        cache = self._cache_path()
        if cache.exists():
            raw = cache.read_bytes()
            self.logical_size, self.chunk_size, count = struct.unpack_from("<QQQ", raw, 0)
            self.rec_off = array.array("Q")
            self.rec_off.frombytes(raw[24 : 24 + 8 * count])
            self.rec_len = array.array("I")
            self.rec_len.frombytes(raw[24 + 8 * count : 24 + 12 * count])
            return
        tail_size = min(self.file_size, 64 * 1024)
        tail = self._pread(self.file_size - tail_size, tail_size)
        ptr = None
        for off in range(len(tail) - 24, -1, -1):
            if (
                _u32(tail, off) == MAGIC
                and _u32(tail, off + 4) == 24
                and _u32(tail, off + 8) == DIRECTORY_POINTER_FLAGS
            ):
                ptr = off
                break
        if ptr is None:
            raise ValueError("directory pointer not found")
        dir_off = FILE_HEADER_SIZE + _u64(tail, ptr + 12)
        dir_len = _u32(tail, ptr + 20)
        d = self._pread(dir_off, dir_len)
        if _u32(d, 0) != MAGIC or _u32(d, 4) != dir_len or _u32(d, 8) != ARCHIVE_DIRECTORY_FLAGS:
            raise ValueError("bad archive directory")
        frame = None
        for e in range(12, len(d), 20):
            rec_off = FILE_HEADER_SIZE + _u64(d, e)
            tot = _u32(d, e + 8)
            obj = _u32(d, e + 12)
            ftype = _u32(d, e + 16)
            if obj == self.object_index and ftype == 0x90:
                frame = (rec_off, tot)
                break
        if frame is None:
            raise ValueError("no compact chunk index")
        fb = self._pread(*frame)
        flags = _u32(fb, 8)
        if flags == RAW_CHUNK_INDEX_FLAGS:
            decoded = fb[20:]
        elif flags == ZLIB_CHUNK_INDEX_FLAGS:
            n = _u32(fb, 12)
            decoded = zlib.decompress(fb[24:])
            if len(decoded) != n:
                raise ValueError("index length mismatch")
        else:
            raise ValueError(f"unsupported index flags {flags:#x}")
        self.logical_size = _u64(decoded, 0)
        self.chunk_size = _u64(decoded, 16)
        count = _u32(decoded, 24)
        if len(decoded) != 28 + 12 * count:
            raise ValueError("malformed index")
        self.rec_off = array.array("Q")
        self.rec_len = array.array("I")
        for i in range(count):
            o = 28 + 12 * i
            self.rec_off.append(FILE_HEADER_SIZE + _u64(decoded, o))
            self.rec_len.append(_u32(decoded, o + 8))
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        with open(tmp, "wb") as out:
            out.write(struct.pack("<QQQ", self.logical_size, self.chunk_size, count))
            out.write(self.rec_off.tobytes())
            out.write(self.rec_len.tobytes())
        os.replace(tmp, cache)

    # -- public ----------------------------------------------------------
    @property
    def chunk_count(self) -> int:
        if self.raw:
            return -(-self.logical_size // self.chunk_size)
        return len(self.rec_off)

    def read_chunk(self, index: int) -> bytes:
        if self.raw:
            off = index * self.chunk_size
            if not 0 <= off < self.logical_size:
                raise IndexError(f"chunk {index} outside the image")
            return self._pread(off, min(self.chunk_size, self.logical_size - off))
        off = self.rec_off[index]
        tot = self.rec_len[index]
        rec = self._pread(off, tot)
        if _u32(rec, 0) != MAGIC or _u32(rec, 4) != tot:
            raise ValueError(f"bad record at chunk {index}")
        flags = _u32(rec, 8)
        expected_off = index * self.chunk_size
        expected_len = min(self.chunk_size, self.logical_size - expected_off)
        if flags == RAW_DATA_FLAGS:
            lo, ll = _u64(rec, 20), _u32(rec, 28)
            payload = rec[36:]
        elif flags == ZLIB_DATA_FLAGS:
            lo, ll = _u64(rec, 24), _u32(rec, 32)
            if _u32(rec, 12) != ll:
                raise ValueError("conflicting zlib sizes")
            payload = zlib.decompress(rec[40:])
        else:
            raise ValueError(f"chunk {index} points at metadata")
        if lo != expected_off or ll != expected_len or len(payload) != ll:
            raise ValueError(f"chunk {index} geometry mismatch")
        return payload

    def read(self, offset: int, length: int) -> bytes:
        """Read an arbitrary logical byte range of the raw object."""
        out = bytearray()
        while length > 0:
            ci, within = divmod(offset, self.chunk_size)
            data = self.read_chunk(ci)
            take = min(length, len(data) - within)
            out += data[within : within + take]
            offset += take
            length -= take
        return bytes(out)

    def close(self) -> None:
        self.fh.close()


if __name__ == "__main__":
    import sys
    import time

    # usage: rdr_reader.py <image name or path>   (builds/caches the index,
    # prints the geometry and a read-speed sample)
    t = time.time()
    r = RdrObject(sys.argv[1])
    print("kind", "raw" if r.raw else "rdr", "chunks", r.chunk_count, "chunk_size", r.chunk_size,
          "logical", r.logical_size, "index_s", round(time.time() - t, 2))
    t = time.time()
    n = 0
    mid = r.chunk_count // 2
    for i in range(mid, min(r.chunk_count, mid + 200)):
        n += len(r.read_chunk(i))
    dt = max(time.time() - t, 1e-6)
    print("read", n, "bytes in", round(dt, 2), "s =", round(n / dt / 1e6, 1), "MB/s")
