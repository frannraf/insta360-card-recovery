#!/usr/bin/env python3
"""Photo stage 1: read every free cluster of a card image once and record photo features.

"Free" = in the exFAT cluster heap and not used by any recovered video chain
(work/chains/<set>/*.json; with no video chains the whole heap is scanned). Clusters that
are a single repeated byte (raw image) or whose RDR record is tiny (< 4 KB: zero/constant
fill, or a file's last cluster followed by zeros) are listed as "tiny" and not read.

Features per chunk:
  start           JPEG SOI at offset 0: camera path (DCIM\\Camera01\\IMG_..._NNN.ins[p]),
                  Exif length, model; or a TIFF/DNG header
  app2            offsets of FF E2 FF FF (full-size APP2 segment headers)
  entropy         every 0xFF is followed by 00, D0-D7, D9 or FF (JPEG scan data, or tables
                  with no FF at all)
  entropy_prefix  scan data that stops inside the chunk (eoi = offsets of FF D9 in it)
  entropy_suffix  scan data that starts inside the chunk
  marks           offsets of FF D9/DA/C0/C4/DD/DB (header clusters only)
  mag, rec2       offsets of the Insta360 trailer magic / the record-0x2 header
  ff00            number of FF 00 pairs (scan data: thousands, tables ~0, video a few)
  sha1            hash of the whole chunk (exact content matching)
Photo-like chunks are cached in work/photos/cache/<image>/ (zlib), so later stages do not
need the image except for a few uncached clusters.

Usage: scan_free.py --image NAME [--chains SET[,SET...]] [--workers N]
Output: work/photos/scan/<image>-features.json  (resumable through <image>-features.ckpt.ndjson)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import re
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from photo_common import PH, RdrObject, ensure_dirs, heap_start_chunk, video_chain_chunks  # noqa: E402

TINY_RECORD = 4096
NAME_RE = re.compile(rb"DCIM\\[A-Za-z0-9]+\\([A-Z]{3}_\d{8}_\d{6}_\d{2}_\d{3}\.[a-z]{2,4})")
BAD_FF = re.compile(rb"\xff[^\x00\xd0-\xd7\xd9\xff]")
BAD_FF_REV = re.compile(rb"[^\x00\xd0-\xd7\xd9\xff]\xff")  # the same pair, in reversed bytes
MARKS = re.compile(rb"\xff[\xd9\xda\xc0\xc4\xdd\xdb]")
MAG = b"8db42d694ccc418790edff439fe026bf"
REC2 = bytes.fromhex("0100000028004b000100000000000000000a000000050000")
APP2 = b"\xff\xe2\xff\xff"


def all_pos(hay: bytes, needle: bytes, cap=64):
    out, i = [], hay.find(needle)
    while i >= 0 and len(out) < cap:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


def features(c: int, d: bytes) -> dict:
    f = {"chunk": c}
    if d[:3] == b"\xff\xd8\xff":
        m = NAME_RE.search(d[:65536])
        f["start"] = {"kind": "jpeg", "name": m.group(1).decode() if m else None,
                      "exif_len": int.from_bytes(d[4:6], "big") if d[3] == 0xE1 else None,
                      "model": "Insta360 X5" if b"Insta360 X5" in d[:65536] else None}
    elif d[:4] in (b"II*\x00", b"MM\x00*"):
        m = NAME_RE.search(d[:65536])
        f["start"] = {"kind": "tiff", "name": m.group(1).decode() if m else None,
                      "insta": b"Insta360" in d[:65536] or b"Arashi" in d[:65536]}
    a = all_pos(d, APP2)
    if a:
        f["app2"] = a
    fb = BAD_FF.search(d)
    if fb is None:
        f["entropy"] = True
    else:
        first_bad = fb.start()
        lb = BAD_FF_REV.search(d[::-1])  # last bad FF, found from the end
        last_bad = len(d) - 2 - lb.start() if lb else first_bad
        e = [m.start() for m in re.finditer(rb"\xff\xd9", d[:first_bad + 2])]
        if first_bad >= 4096 or e:
            f["entropy_prefix"] = first_bad
            if e:
                f["eoi"] = e[:8]
        if len(d) - (last_bad + 2) >= 4096:
            f["entropy_suffix"] = last_bad + 2
    if "app2" in f or "start" in f or "entropy_suffix" in f:
        f["marks"] = [[m.start(), d[m.start() + 1]] for m in MARKS.finditer(d)][:64]
    for key, needle in (("mag", MAG), ("rec2", REC2)):
        p = all_pos(d, needle, 8)
        if p:
            f[key] = p
    f["ff00"] = d.count(b"\xff\x00")
    f["sha1"] = hashlib.sha1(d).hexdigest()
    return f


def photo_like(f: dict) -> bool:
    return any(k in f for k in ("start", "app2", "entropy", "eoi", "entropy_prefix", "entropy_suffix", "mag", "rec2"))


def scan_batch(args):
    image, lo, chunks = args
    raw = RdrObject(image)
    (PH / "cache" / image).mkdir(parents=True, exist_ok=True)
    pack_name = f"{lo:08d}.pack"
    feats, tiny = [], []
    with open(PH / "cache" / image / pack_name, "wb") as pack:
        for c in chunks:
            if not raw.raw and raw.rec_len[c] < TINY_RECORD:
                tiny.append(c)
                continue
            d = raw.read_chunk(c)
            if raw.raw and d.count(d[:1]) == len(d):
                tiny.append(c)
                continue
            f = features(c, d)
            if photo_like(f):
                z = zlib.compress(d, 1)
                f["cache"] = [pack_name, pack.tell(), len(z)]
                pack.write(z)
            feats.append(f)
    raw.close()
    return lo, feats, tiny


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", required=True)
    ap.add_argument("--chains", default=None, help="video chain set(s) under work/chains, comma-separated "
                    "(default: the image name, plus <image>-patched if it exists)")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    ensure_dirs()
    sets = a.chains.split(",") if a.chains else [a.image, f"{a.image}-patched"]
    used = video_chain_chunks(sets)
    raw = RdrObject(a.image)
    h0, n = heap_start_chunk(raw), raw.chunk_count
    raw.close()
    todo = [c for c in range(h0, n) if c not in used]
    print(f"{a.image}: {n} chunks, heap starts at {h0}, {len(used)} used by video chains, "
          f"{len(todo)} to scan", flush=True)
    step = 1000
    batches = [(a.image, todo[i], todo[i:i + step]) for i in range(0, len(todo), step)]
    ck = PH / "scan" / f"{a.image}-features.ckpt.ndjson"
    done = set()
    if ck.exists():
        for line in ck.open():
            done.add(json.loads(line)["lo"])
    jobs = [b for b in batches if b[1] not in done]
    t0 = time.time()
    with mp.Pool(a.workers) as pool, ck.open("a") as ckf:
        for i, (lo, feats, tiny) in enumerate(pool.imap_unordered(scan_batch, jobs), 1):
            ckf.write(json.dumps({"lo": lo, "feats": feats, "tiny": tiny}) + "\n")
            ckf.flush()
            if i % 10 == 0 or i == len(jobs):
                el = time.time() - t0
                print(f"{a.image}: {i}/{len(jobs)} batches, {el:.0f} s", flush=True)
    feats, tiny = [], []
    for line in ck.open():
        rec = json.loads(line)
        feats.extend(rec["feats"])
        tiny.extend(rec["tiny"])
    feats.sort(key=lambda f: f["chunk"])
    out = {"image": a.image, "heap_start": h0, "scanned": len(todo), "tiny": sorted(tiny), "features": feats}
    (PH / "scan" / f"{a.image}-features.json").write_text(json.dumps(out))
    starts = [f for f in feats if "start" in f]
    print(json.dumps({"image": a.image, "read": len(feats), "tiny": len(tiny), "photo_starts": len(starts),
                      "named": sorted(f["start"]["name"] for f in starts if f["start"].get("name"))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
