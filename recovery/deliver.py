#!/usr/bin/env python3
"""Gate + publish a validated reconstruction into outputs/recovered.

Checks validation/<label>/result.txt and probe.json against the moov tables
from the full catalog (stream count, dimensions, frame counts, duration,
decode exit 0, zero error lines).  On pass, hard-links the assembled file into
outputs/recovered under its final name (no extra disk use, assembled copy is
never modified), computes SHA-256 and appends a manifest row.

Visual inspection is a separate, manual gate: pass --visual-ok only after the
beginning/middle/end frames have been looked at.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402
from rconfig import OUT  # noqa: E402
MANIFEST = OUT / "MANIFEST.csv"
FIELDS = ["file", "kind", "bytes", "duration_s", "streams", "decoded_frames", "expected_frames",
          "decode_exit_codes", "decode_error_lines", "visual_check", "sha256", "source",
          "chain_json", "clusters", "blind_clusters", "inst_trailer", "notes"]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(16 * 1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest().upper()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--assembled", required=True, type=Path)
    ap.add_argument("--validation", required=True, type=Path)
    ap.add_argument("--chain", required=True, type=Path)
    ap.add_argument("--final-name", required=True)
    ap.add_argument("--visual-ok", action="store_true")
    ap.add_argument("--inst", default="raw-contiguous (not reconstructed)")
    ap.add_argument("--notes", default="")
    ap.add_argument("--visual-check", default="beginning/middle/end inspected",
                    help="how the visual gate was passed (recorded in the manifest)")
    ap.add_argument("--min-duration", type=float, default=2.0)
    ap.add_argument("--image", required=True)
    args = ap.parse_args()
    args.assembled = args.assembled.resolve()
    args.validation = args.validation.resolve()
    args.chain = args.chain.resolve()

    cat = json.loads((WORK / "scan-analysis" / f"{args.image}-full-catalog.json").read_text())["files"]
    kind, t = args.label.split("_", 1)
    entry = next(f for f in cat if f["kind"] == kind and f["name_time"] == t)
    probe = json.loads((args.validation / "probe.json").read_text())
    res = (args.validation / "result.txt").read_text()
    chain = json.loads(args.chain.read_text())

    problems = []
    tracks = [tr for tr in entry["tracks"] if tr[0] in ("vide", "soun")]
    vexp = [tr for tr in tracks if tr[0] == "vide"]
    aexp = [tr for tr in tracks if tr[0] == "soun"]
    streams = probe["streams"]
    vs = [s for s in streams if s["codec_type"] == "video"]
    as_ = [s for s in streams if s["codec_type"] == "audio"]
    if len(vs) != len(vexp) or len(as_) != len(aexp):
        problems.append(f"stream count {len(vs)}v/{len(as_)}a vs moov {len(vexp)}v/{len(aexp)}a")
    for s, tr in zip(vs, vexp):
        if (s["width"], s["height"]) != (tr[2], tr[3]):
            problems.append(f"dims {s['width']}x{s['height']} vs {tr[2]}x{tr[3]}")
    rows = dict(re.findall(r"^(\w+) (.*)$", res, re.M))
    decoded, expected, exits, errs = [], [], [], []
    for i, tr in enumerate(vexp):
        line = rows.get(f"v{i}", "")
        m = dict(x.split("=") for x in line.split())
        decoded.append(int(m.get("decoded_frames", -1)))
        expected.append(tr[4])
        exits.append(int(m.get("decode_exit", -1)))
        errs.append(int(m.get("decode_error_lines", -1)))
    for i, tr in enumerate(aexp):
        line = rows.get(f"a{i}", "")
        m = dict(x.split("=") for x in line.split())
        decoded.append(int(m.get("decoded_frames", -1)))
        expected.append(tr[4])
        exits.append(int(m.get("decode_exit", -1)))
        errs.append(int(m.get("decode_error_lines", -1)))
    if any(e != 0 for e in exits):
        problems.append(f"decode exits {exits}")
    if any(e != 0 for e in errs):
        problems.append(f"decode error lines {errs}")
    if decoded[: len(vexp)] != expected[: len(vexp)]:
        problems.append(f"video frames {decoded[:len(vexp)]} vs {expected[:len(vexp)]}")
    # AAC: decoder output frame count may differ by priming; require >= expected-2
    for d, e in zip(decoded[len(vexp):], expected[len(vexp):]):
        if d < e - 2:
            problems.append(f"audio frames {d} vs {e}")
    dur = float(probe["format"]["duration"])
    # ffprobe's format duration is the longest stream; the camera's audio track
    # can outlast the video by a second or two (seen on a night clip:
    # audio 432.17 s, video 430.375 s), so accept the movie duration or the
    # longest track in the moov's own tables.
    longest = max((tr[6] / tr[5] for tr in entry["tracks"] if len(tr) > 6 and tr[5]), default=entry["duration"])
    if min(abs(dur - entry["duration"]), abs(dur - longest)) > 0.2:
        problems.append(f"duration {dur} vs {entry['duration']} (longest track {longest:.3f})")
    if dur < args.min_duration:
        problems.append(f"duration {dur} below substantial threshold")
    size = args.assembled.stat().st_size
    if size != entry["file_size"]:
        problems.append(f"size {size} vs expected {entry['file_size']}")
    if not args.visual_ok:
        problems.append("visual check not confirmed")
    # the chain must be fully resolved, and the assembled bytes must still be
    # what the *current* chain says (binds file, chain and validation)
    from chain_walk import CS, UNRESOLVED  # noqa: E402
    from rdr_reader import RdrObject  # noqa: E402
    unresolved = [m["k"] for m in chain["mapping"] if m["how"] in UNRESOLVED]
    if unresolved:
        problems.append(f"chain has {len(unresolved)} unresolved blind clusters {unresolved[:5]}")
    if f"assembled/{args.assembled.name}" not in res.split("\n", 1)[0]:
        problems.append(f"validation was run on a different file: {res.splitlines()[0] if res else '?'}")
    if not problems:
        import random  # noqa: E402
        mp = sorted(chain["mapping"], key=lambda m: m["k"])
        weak = [m for m in mp if m["how"] not in ("verified", "header", "last-pinned")]
        rnd = random.Random(args.label).sample(mp, min(32, len(mp)))
        sample = {m["k"]: m for m in weak + rnd + [mp[-1]]}
        raw = RdrObject(chain["image"])
        mdat_end = entry["mdat_end"]
        with open(args.assembled, "rb") as fh:
            for k, m in sorted(sample.items()):
                n = min(CS, mdat_end - k * CS)
                fh.seek(k * CS)
                if fh.read(n) != raw.read_chunk(m["chunk"])[:n]:
                    problems.append(f"assembled cluster {k} differs from chain chunk {m['chunk']}")
                    break
        chain_check = f"{len(sample)} clusters byte-identical to chain ({len(weak)} non-NAL-verified)"
    if problems:
        print(json.dumps({"label": args.label, "deliverable": False, "problems": problems}))
        return 3

    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / args.final_name
    listed = MANIFEST.exists() and any(r["file"] == args.final_name for r in csv.DictReader(open(MANIFEST)))
    if dest.exists():
        if not os.path.samefile(dest, args.assembled):
            raise SystemExit(f"{dest} exists and is a different file")
        if listed:
            print(json.dumps({"label": args.label, "deliverable": True, "note": "already delivered", "dest": str(dest)}))
            return 0
    else:
        os.link(args.assembled, dest)
    digest = sha256(dest)
    blind = sum(1 for m in chain["mapping"] if m["how"].startswith("blind"))
    desc = "; ".join(
        f"{s['codec_name']} {s.get('width','')}x{s.get('height','')} {s.get('r_frame_rate','')}".strip()
        if s["codec_type"] == "video" else f"{s['codec_name']} {s.get('sample_rate','')}Hz {s.get('channels','')}ch"
        for s in streams)
    row = {"file": args.final_name, "kind": kind, "bytes": size, "duration_s": f"{dur:.6f}",
           "streams": desc, "decoded_frames": "/".join(map(str, decoded)),
           "expected_frames": "/".join(map(str, expected)),
           "decode_exit_codes": "/".join(map(str, exits)), "decode_error_lines": "/".join(map(str, errs)),
           "visual_check": args.visual_check, "sha256": digest,
           "source": f"{chain['image']} card-image cluster-chain reconstruction",
           "chain_json": str(args.chain.relative_to(WORK.parent)), "clusters": len(chain["mapping"]),
           "blind_clusters": blind, "inst_trailer": args.inst,
           "notes": "; ".join(x for x in (args.notes, chain_check) if x)}
    from rconfig import manifest_lock  # noqa: E402
    with manifest_lock():
        if MANIFEST.exists() and any(r["file"] == args.final_name for r in csv.DictReader(open(MANIFEST, newline=""))):
            print(json.dumps({"label": args.label, "deliverable": True, "note": "already delivered", "dest": str(dest)}))
            return 0
        new = not MANIFEST.exists()
        with open(MANIFEST, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)
    print(json.dumps({"label": args.label, "deliverable": True, "dest": str(dest), "sha256": digest}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
