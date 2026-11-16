#!/usr/bin/env python3
"""Check one Insta360 photo (.insp, or a plain JPEG) end to end. Read-only on the photo.

  A  header: SOI, APP1 Exif with the camera path, APP2 run, SOF0/DHT/DRI/SOS, baseline only
  B  scan data: every 0xFF is FF 00 / RSTn in order / fill, up to EOI
  C  trailer (.insp): length field == bytes after EOI, magic, and the backward record chain
     lands exactly on the first byte after EOI
  C2 the two stored copies of the 2560x1280 stitched preview (APP2 and trailer record 0x2)
     are byte-identical
  D  jpegmcu: bit-exact Huffman decode, every restart interval ends exactly at its RST
     marker and the last at EOI; FFmpeg decode with error detection, no messages allowed
  E  previews for the visual check: the main image at 1/8 scale (two fisheye circles) and
     the stitched preview, written next to the report

Usage: check_photo.py <photo> <out_dir>   -> <out_dir>/<name>.check.json, .preview.jpg, .stitched.jpg
Prints a one-line summary; "structure_ok" is true only if every check passed.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from photo_common import PH, ensure_jpegmcu, jpegmcu_file, sh_path, sh_run  # noqa: E402

MAG = b"8db42d694ccc418790edff439fe026bf"
PREVIEW = 4915200


def parse_header(d: bytes):
    r = {"ok": False}
    if d[:2] != b"\xff\xd8":
        r["error"] = "no SOI"
        return r, None
    i, segs, app2 = 2, [], 0
    while i + 4 <= len(d):
        if d[i] != 0xFF:
            r["error"] = f"marker sync lost at {i}"
            return r, None
        m = d[i + 1]
        ln = struct.unpack_from(">H", d, i + 2)[0]
        seg = d[i + 4:i + 2 + ln]
        if m == 0xE2:
            app2 += 1
        else:
            segs.append(f"FF{m:02X}")
        if m == 0xE1 and "exif_len" not in r:
            r["exif_len"] = ln
            k = seg.find(b"DCIM\\")
            if k >= 0:
                r["camera_path"] = seg[k:seg.find(b"\x00", k)].decode("latin1")
        if m == 0xC0:
            _p, h, w, nc = struct.unpack_from(">BHHB", seg, 0)
            r["width"], r["height"], r["comps"] = w, h, nc
        if m in (0xC1, 0xC2, 0xC3):
            r["error"] = f"unexpected SOF{m - 0xC0} (not baseline)"
            return r, None
        if m == 0xDD:
            r["restart_interval"] = struct.unpack_from(">H", seg, 0)[0]
        i += 2 + ln
        if m == 0xDA:
            r["sos_end"] = i
            break
    r["app2_segments"] = app2
    r["segments"] = segs
    r["ok"] = "sos_end" in r and "width" in r
    return r, r.get("sos_end")


def scan_data(d: bytes, start: int):
    j, rst_expect, n_rst, bad = start, 0, 0, []
    while True:
        k = d.find(b"\xff", j)
        if k < 0 or k + 1 >= len(d):
            return {"ok": False, "error": "no EOI", "rst": n_rst}, None
        nb = d[k + 1]
        if nb == 0x00:
            j = k + 2
        elif 0xD0 <= nb <= 0xD7:
            if nb - 0xD0 != rst_expect:
                bad.append([k, "rst order"])
            rst_expect = (nb - 0xCF) & 7
            n_rst += 1
            j = k + 2
        elif nb == 0xD9:
            return {"ok": not bad, "rst": n_rst, "bad": bad[:5], "eoi": k}, k
        elif nb == 0xFF:
            j = k + 1
        else:
            return {"ok": False, "rst": n_rst, "error": f"FF{nb:02X} inside scan data at {k}"}, None


def trailer(d: bytes, eoi: int) -> dict:
    t = {"bytes_after_eoi": len(d) - eoi - 2}
    if t["bytes_after_eoi"] == 0:
        t.update(present=False, ok=True)
        return t
    t["present"] = True
    tail = d[-72:]
    t["magic_ok"] = tail[40:] == MAG
    t["len"], t["ver"] = struct.unpack_from("<II", tail, 32)
    t["len_ok"] = t["len"] == t["bytes_after_eoi"]
    # records are chained backwards, each followed by a 6-byte header (>H id, <I size);
    # the last header sits 78 bytes before the end
    base, pos, recs = eoi + 2, len(d) - 78, []
    while pos > base and len(recs) < 4096:
        rid = struct.unpack_from(">H", d, pos)[0]
        sz = struct.unpack_from("<I", d, pos + 2)[0]
        if not (0 < rid < 0x1000) or sz <= 0 or pos - sz < base:
            break
        recs.append([hex(rid), sz])
        pos -= sz
        if pos == base:
            break
        pos -= 6
    t["records"] = recs[::-1][:40]
    t["n_records"] = len(recs)
    t["chain_lands_on_eoi"] = pos == base
    t["ok"] = t["magic_ok"] and t["len_ok"] and t["chain_lands_on_eoi"] and bool(recs)
    return t


def preview_copies(d: bytes, eoi: int):
    out = {}
    i = 2 + 2 + struct.unpack_from(">H", d, 4)[0]  # after APP1
    app2 = bytearray()
    while d[i] == 0xFF and d[i + 1] == 0xE2:
        ln = struct.unpack_from(">H", d, i + 2)[0]
        app2 += d[i + 4:i + 2 + ln]
        i += 2 + ln
    out["app2_bytes"] = len(app2)
    k = d.find(bytes.fromhex("0100000028004b00"), eoi + 2)
    if k < 0:
        out.update(ok=False, error="no 0x2 record in the trailer")
        return out, bytes(app2)
    rec = d[k + 40:k + 40 + len(app2)]
    out["equal"] = rec == bytes(app2)
    if not out["equal"]:
        out["differing_4k_blocks"] = sum(1 for j in range(0, len(app2), 4096) if rec[j:j + 4096] != app2[j:j + 4096])
    out["ok"] = out["equal"] and len(app2) == PREVIEW
    return out, bytes(app2)


def ffmpeg(args: str) -> tuple[int, list[str]]:
    r = sh_run(f"ffmpeg -nostdin -hide_banner {args}", capture_output=True, text=True)
    return r.returncode, [x for x in r.stderr.splitlines() if x.strip()]


def check(src: Path, outd: Path) -> dict:
    outd.mkdir(parents=True, exist_ok=True)
    tmp = PH / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    d = src.read_bytes()
    rep = {"file": src.name, "bytes": len(d)}
    hdr, sos = parse_header(d)
    rep["A_header"] = hdr
    keys = ["A_header", "B_scan", "C_trailer", "D_mcu", "D_ffmpeg", "E_previews"]
    if sos:
        rep["B_scan"], eoi = scan_data(d, sos)
        if eoi is not None:
            rep["C_trailer"] = trailer(d, eoi)
            pay = b""
            if rep["C_trailer"].get("present"):
                rep["C2_preview_copies"], pay = preview_copies(d, eoi)
                keys.append("C2_preview_copies")
            rep["D_mcu"] = jpegmcu_file(src)
            jp = tmp / (src.name + ".jpeg-part.jpg")
            jp.write_bytes(d[:eoi + 2])
            try:
                code, msgs = ffmpeg(f"-v warning -err_detect crccheck+bitstream+buffer+explode -f jpeg_pipe "
                                    f"-i '{sh_path(jp)}' -f null -")
                rep["D_ffmpeg"] = {"exit": code, "messages": msgs[:10], "ok": code == 0 and not msgs}
                pv = outd / (src.name + ".preview.jpg")
                code1, m1 = ffmpeg(f"-v error -y -f jpeg_pipe -i '{sh_path(jp)}' -vf scale=iw/8:ih/8 -q:v 3 '{sh_path(pv)}'")
                e = {"preview": pv.name, "ok": code1 == 0 and pv.exists()}
                if len(pay) == PREVIEW:
                    nv = tmp / (src.name + ".nv12")
                    nv.write_bytes(pay)
                    st = outd / (src.name + ".stitched.jpg")
                    code2, _ = ffmpeg(f"-v error -y -f rawvideo -pix_fmt nv12 -s 2560x1280 -i '{sh_path(nv)}' "
                                      f"-vf scale=1280:640 -q:v 3 '{sh_path(st)}'")
                    nv.unlink(missing_ok=True)
                    e["stitched"] = st.name
                    e["ok"] = e["ok"] and code2 == 0
                rep["E_previews"] = e
            finally:
                jp.unlink(missing_ok=True)
    rep["structure_ok"] = all(rep.get(k, {}).get("ok") for k in keys)
    (outd / (src.name + ".check.json")).write_text(json.dumps(rep, indent=1))
    return rep


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    ensure_jpegmcu()
    rep = check(Path(sys.argv[1]), Path(sys.argv[2]))
    short = {k: (v.get("ok") if isinstance(v, dict) else v) for k, v in rep.items()}
    short["camera_path"] = rep["A_header"].get("camera_path")
    print(json.dumps(short))


if __name__ == "__main__":
    main()
