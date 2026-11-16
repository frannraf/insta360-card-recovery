#!/usr/bin/env python3
"""Self-test of the photo tools with a few known-good .insp photos (e.g. ones copied off the
camera before the accident). Nothing is written except temporary files in work/photos/tmp.

1. jpegmcu must accept each photo and reject it once any one scan cluster is replaced
   (by zeros, by a cluster of another photo, by another cluster of the same photo), or one
   cluster is dropped or duplicated.
2. The photos are laid out on a fake card with holes, interleaving, random junk, a decoy
   scan cluster and a missing trailer cluster; the walker must rebuild each byte for byte.

Usage: selftest.py <good1.insp> <good2.insp> [<good3.insp>]
"""
from __future__ import annotations

import hashlib
import os
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scan_free  # noqa: E402
import walk_photo as wp  # noqa: E402
from photo_common import CS, ensure_jpegmcu, jpegmcu_bytes  # noqa: E402


def clusters(b: bytes) -> list[bytes]:
    return [b[i:i + CS].ljust(CS, b"\x00") for i in range(0, len(b), CS)]


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    ensure_jpegmcu()
    photos = [Path(p).read_bytes() for p in sys.argv[1:4]]
    A, B = photos[0], photos[1]
    ok = True
    # 1. splice test
    r = jpegmcu_bytes(A)
    print("good photo accepted:", r["ok"])
    ok &= r["ok"]
    nA = len(A) // CS
    first_scan, last_scan = 20, int(r.get("eoi", nA * CS)) // CS - 1
    random.seed(1)
    caught = total = 0
    for k in sorted(random.sample(range(first_scan, last_scan), min(5, last_scan - first_scan))):
        for what, blob in (("zeros", bytes(CS)), ("other photo", B[k * CS:(k + 1) * CS]),
                           ("own other cluster", A[(k + 3) * CS:(k + 4) * CS])):
            total += 1
            caught += not jpegmcu_bytes(A[:k * CS] + blob + A[(k + 1) * CS:])["ok"]
    for what, data in (("dropped", A[:30 * CS] + A[31 * CS:]), ("duplicated", A[:31 * CS] + A[30 * CS:])):
        total += 1
        caught += not jpegmcu_bytes(data)["ok"]
    print(f"spliced clusters caught: {caught}/{total}")
    ok &= caught == total
    # 2. synthetic card
    cl = [clusters(p) for p in photos]
    junk = lambda: os.urandom(CS)  # noqa: E731
    card, pos = {}, 1000
    for k in range(6):  # photo 1: header and first clusters in holes, then the rest elsewhere
        card[pos], card[pos + 1], card[pos + 2] = cl[0][k], junk(), junk()
        pos += 3
    for k in range(6, len(cl[0])):
        card[5000 + k] = cl[0][k]
    for k, c in enumerate(cl[1]):  # photo 2: contiguous
        card[6000 + k] = c
    if len(cl) > 2:  # photo 3: junk every 9 clusters, a decoy before cluster 30, trailer cluster lost
        pos = 7000
        last = len(cl[2]) - 3
        for k, c in enumerate(cl[2]):
            if k == 30:
                card[pos] = cl[1][40]
                pos += 1
            if k % 9 == 0:
                card[pos] = junk()
                pos += 1
            card[pos] = junk() if k == last else c
            pos += 1
    feats = [scan_free.features(c, d) for c, d in sorted(card.items())]
    cardobj = wp.Card(feats, lambda c: card[c])
    starts = sorted((f for f in feats if "start" in f), key=lambda f: f["start"]["name"] or "")
    order = {f["start"]["name"]: f["chunk"] for f in starts}
    for p in photos:
        name = scan_free.NAME_RE.search(p[:65536]).group(1).decode()
        plan = wp.walk(cardobj, order[name], log=lambda s: None)
        same = hashlib.sha256(wp.assemble(cardobj, plan)).digest() == hashlib.sha256(p).digest()
        print(f"{name}: {plan['status']} {plan.get('counts')} {'IDENTICAL' if same else 'DIFFERENT'}")
        ok &= same
    print("SELFTEST PASSED" if ok else "SELFTEST FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
