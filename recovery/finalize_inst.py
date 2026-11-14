#!/usr/bin/env python3
"""Install a verified rebuilt inst trailer into an assembled file (in place).

Only bytes [inst_start, file_size) are rewritten.  The SHA-256 of the
mdat+moov prefix is computed before and after and must be identical, so the
full-decode validation of the media remains valid.  If the file is published
in outputs/recovered (hard link), the manifest row is updated with the new
SHA-256 and inst status.  A JSON log is written to work/inst/<label>.final.json.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import final_name  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402
from rconfig import OUT  # noqa: E402
MANIFEST = OUT / "MANIFEST.csv"
MAG = b"8db42d694ccc418790edff439fe026bf"
# what is actually established: every record header at its indexed position,
# trailer/index/camera-info from the file's own final cluster, IMU (0x3)
# timestamps monotonic; which copy/version of the other records was used is
# chosen by allocation heuristics and is not content-verified
INST_LABEL = "rebuilt from card (structure + IMU checked; other record versions heuristic)"


def sha_range(path: Path, start: int, end: int) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        fh.seek(start)
        left = end - start
        while left:
            b = fh.read(min(16 << 20, left))
            if not b:
                raise EOFError
            h.update(b)
            left -= len(b)
    return h.hexdigest().upper()


def main() -> int:
    label = sys.argv[1]
    image = sys.argv[2]
    cat = json.loads((WORK / "scan-analysis" / f"{image}-full-catalog.json").read_text())["files"]
    nf = WORK / "scan-analysis" / f"{image}-file-numbers.json"
    numbers = json.loads(nf.read_text()) if nf.exists() else {}
    kind, t = label.split("_", 1)
    f = next(x for x in cat if x["kind"] == kind and x["name_time"] == t)
    plan = json.loads((WORK / "inst" / f"{label}.plan.json").read_text())
    if plan.get("status") != "complete":
        raise SystemExit(f"inst plan not complete: {plan.get('status')}")
    inst = (WORK / "inst" / f"{label}.inst.bin").read_bytes()
    if len(inst) != f["inst_size"] or inst[4:8] != b"inst" or inst[-32:] != MAG:
        raise SystemExit("rebuilt inst fails basic checks")
    name = final_name(f, numbers)
    path = WORK / "assembled" / name
    size = path.stat().st_size
    if size != f["file_size"]:
        raise SystemExit("assembled size mismatch")
    inst_start = f["mdat_end"] + f["moov_size"]
    with open(path, "rb") as fh:
        fh.seek(inst_start)
        current = fh.read(len(inst))
    if current == inst:
        print(json.dumps({"label": label, "note": "inst already installed"}))
        return 0
    # independent sanity: the bytes being replaced must be an 'inst' atom of
    # the expected size that ends exactly at EOF
    if current[:8] != struct.pack(">I4s", f["inst_size"], b"inst") or inst_start + f["inst_size"] != size:
        raise SystemExit("existing bytes at inst_start are not the expected inst atom - refusing to patch")
    backup = WORK / "inst" / f"{label}.prev-inst.bin"
    if not backup.exists():
        backup.write_bytes(current)
    before = sha_range(path, 0, inst_start)
    with open(path, "r+b") as fh:
        fh.seek(inst_start)
        fh.write(inst)
        fh.flush()
        os.fsync(fh.fileno())
    after = sha_range(path, 0, inst_start)
    if before != after:
        raise SystemExit("PREFIX CHANGED - abort")
    with open(path, "rb") as fh:
        fh.seek(size - 32)
        tail = fh.read(32)
    full = sha_range(path, 0, size)
    rec = {"label": label, "file": name, "prefix_sha256_unchanged": before, "new_sha256": full,
           "trailer_magic_at_eof": tail == MAG, "inst_sha256": hashlib.sha256(inst).hexdigest().upper()}
    published = OUT / name
    if published.exists() and os.path.samefile(published, path) and MANIFEST.exists():
        from rconfig import manifest_lock  # noqa: E402
        with manifest_lock():
            rows = list(csv.DictReader(open(MANIFEST, newline="")))
            for r in rows:
                if r["file"] == name:
                    r["sha256"] = full
                    r["inst_trailer"] = INST_LABEL
            tmp = MANIFEST.with_suffix(".csv.tmp")
            with open(tmp, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            os.replace(tmp, MANIFEST)
        rec["manifest_updated"] = True
    (WORK / "inst" / f"{label}.final.json").write_text(json.dumps(rec, indent=1))
    print(json.dumps(rec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
