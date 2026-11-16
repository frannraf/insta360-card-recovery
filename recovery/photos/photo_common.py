"""Shared paths and helpers for the photo tools (recovery/photos).

Work data lives in <work>/photos/ (scan features, cluster cache, chains, assembled photos,
validation reports, the review page). Verified photos are published to
<outputs>/../photos/<yyyy-mm>/ (next to the recovered videos) with PHOTOS.csv.

jpegmcu (the bit-exact JPEG scan checker, jpegmcu.c) and FFmpeg run in bash, like the
video tools: natively on Linux/macOS, through WSL on Windows. jpegmcu is compiled with gcc
on first use.
"""
from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from rconfig import IMAGES, OUT, WORK, sh_path, sh_run  # noqa: E402,F401
from rdr_reader import RdrObject  # noqa: E402,F401

CS = 256 * 1024
PH = WORK / "photos"
PHOTO_OUT = OUT.parent / "photos"
JPEGMCU_SRC = HERE / "jpegmcu.c"
JPEGMCU_BIN = PH / "bin" / "jpegmcu"


def ensure_dirs() -> None:
    for d in ("scan", "cache", "chains", "assembled", "validation", "tmp", "bin"):
        (PH / d).mkdir(parents=True, exist_ok=True)


def ensure_jpegmcu() -> None:
    """Compile jpegmcu.c (gcc in bash) if the binary is missing or older than the source."""
    ensure_dirs()
    if JPEGMCU_BIN.exists() and JPEGMCU_BIN.stat().st_mtime >= JPEGMCU_SRC.stat().st_mtime:
        return
    r = sh_run(f"gcc -O2 -o '{sh_path(JPEGMCU_BIN)}' '{sh_path(JPEGMCU_SRC)}'", capture_output=True, text=True)
    if r.returncode != 0 or not JPEGMCU_BIN.exists():
        raise SystemExit("could not compile jpegmcu.c with gcc in bash (on Ubuntu/WSL: "
                         "sudo apt install build-essential)\n" + r.stderr[-1500:])


def jpegmcu_file(path: Path) -> dict:
    r = sh_run(f"'{sh_path(JPEGMCU_BIN)}' '{sh_path(path)}'", capture_output=True, text=True)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"ok": False, "error": "jpegmcu failed: " + r.stderr[-300:]}


def jpegmcu_bytes(data: bytes) -> dict:
    import tempfile
    (PH / "tmp").mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=PH / "tmp", suffix=".jpg", delete=False) as fh:
        fh.write(data)
        name = Path(fh.name)
    try:
        return jpegmcu_file(name)
    finally:
        name.unlink(missing_ok=True)


def heap_start_chunk(raw: RdrObject) -> int:
    """First chunk of the exFAT cluster heap (0 if no exFAT boot sector is readable)."""
    sys.path.insert(0, str(HERE.parent))
    from card_info import exfat_at  # noqa: E402
    info = exfat_at(raw, 0)
    if info is None:
        mbr = raw.read(0, 512)
        if mbr[510:512] == b"\x55\xaa":
            lba = struct.unpack_from("<I", mbr, 0x1BE + 8)[0]
            info = exfat_at(raw, lba * 512) if lba else None
    if info is None:
        print("warning: no exFAT boot sector found; scanning from the start of the image", flush=True)
        return 0
    if info["cluster_size"] != CS or info["heap_offset"] % CS:
        # the walker needs the files' clusters to be 256 KiB chunks of the image; the current boot
        # sector may describe a later reformat, so scan everything and let the checks decide
        print("warning: the current exFAT layout is not 256 KiB clusters aligned to the image "
              "(see card_info.py); scanning the whole image", flush=True)
        return 0
    return info["heap_offset"] // CS


def video_chain_chunks(chain_sets: list[str]) -> set[int]:
    """Chunks used by the recovered video chains (those cannot hold photo data)."""
    used: set[int] = set()
    for s in chain_sets:
        for f in (WORK / "chains" / s).glob("*.json"):
            if ".pre-" in f.name:
                continue
            try:
                m = json.loads(f.read_text()).get("mapping", [])
            except (json.JSONDecodeError, OSError):
                continue
            used.update(x["chunk"] for x in m if isinstance(x, dict) and x.get("chunk") is not None)
    return used


def cache_source(image: str, features: list[dict]):
    """Chunk reader: the local cache (zlib packs written by scan_free.py) first, else the image."""
    by = {f["chunk"]: f for f in features}
    raw_holder: dict = {}
    packs: dict = {}

    def get(c: int) -> bytes:
        f = by.get(c)
        if f and "cache" in f:
            p = packs.get(f["cache"][0])
            if p is None:
                p = packs[f["cache"][0]] = open(PH / "cache" / image / f["cache"][0], "rb")
            p.seek(f["cache"][1])
            return zlib.decompress(p.read(f["cache"][2]))
        if "raw" not in raw_holder:
            raw_holder["raw"] = RdrObject(image)
        return raw_holder["raw"].read_chunk(c)
    return get


def load_scan(image: str) -> dict:
    p = PH / "scan" / f"{image}-features.json"
    if not p.exists():
        raise SystemExit(f"no scan for {image}: run  python recovery/photos/run_photos.py --image {image}")
    return json.loads(p.read_text())
