#!/usr/bin/env python3
"""Set the camera-local timestamp used in published file names.

The moov creation time is UTC; the camera names files with its own clock,
whose offset can change between sessions (travel, manual clock changes).
For each recording the local time is taken, in priority order, from:
  1. the camera's own timestamp (info record 0x101, field 7) in the file's
     final trailer cluster on the card (located by the inst plan);
  2. the companion file (VID<->LRV share the timestamp);
  3. the offset measured on other recordings of the same session
     (same UTC date, most common offset);
  4. the card default given on the command line.
Writes "local_time" + "local_time_source" into the catalog.  With --rename,
renames already published files (outputs/recovered hard links and their
work/assembled copies) and rewrites MANIFEST.csv atomically.

Usage: set_local_time.py <image> <default_utc_offset_hours> [--rename]
"""

from __future__ import annotations

import collections
import csv
import datetime as dt
import json
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from insv_inspect import pb_fields  # noqa: E402
from pipeline import final_name  # noqa: E402
from rdr_reader import RdrObject  # noqa: E402

CS = 262144
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402
from rconfig import OUT  # noqa: E402
MANIFEST = OUT / "MANIFEST.csv"


def camera_time_from_card(raw: RdrObject, f: dict, label: str) -> str | None:
    plan = WORK / "inst" / f"{label}.plan.json"
    if not plan.exists():
        return None
    p = json.loads(plan.read_text())
    final = p.get("final") or (p.get("finals") or [None])[-1]
    if final is None:
        return None
    d = raw.read_chunk(final)
    end = f["file_size"] % CS
    _fm, _r, isz = struct.unpack("<BBI", d[end - 78:end - 72])
    idx = d[end - 78 - isz:end - 78]
    inst_start = f["mdat_end"] + f["moov_size"]
    for i in range(0, len(idx) - 9, 10):
        rid, sz, off = struct.unpack_from("<HII", idx, i)
        if rid == 0x101:
            pos = inst_start + 8 + off
            if pos // CS != (f["file_size"] - 1) // CS:
                return None  # info record not in the final cluster
            w = pos % CS
            fields = dict((x[0], x[2]) for x in pb_fields(d[w:w + sz]))
            t = str(fields.get(7) or "")
            return t if len(t) == 14 and t.isdigit() else None
    return None


def main() -> int:
    image, default = sys.argv[1], float(sys.argv[2])
    rename = "--rename" in sys.argv
    cpath = WORK / "scan-analysis" / f"{image}-full-catalog.json"
    cat = json.loads(cpath.read_text())
    files = cat["files"]
    nf = WORK / "scan-analysis" / f"{image}-file-numbers.json"
    numbers = json.loads(nf.read_text()) if nf.exists() else {}
    old_names = {id(f): final_name(f, numbers) for f in files}
    raw = RdrObject(image)
    utc = {id(f): dt.datetime.strptime(f["creation_utc"], "%Y-%m-%dT%H:%M:%SZ") for f in files}
    cam: dict[int, dt.datetime] = {}
    for f in files:
        t = camera_time_from_card(raw, f, f"{f['kind']}_{f['name_time']}")
        if t:
            cam[id(f)] = dt.datetime.strptime(t, "%Y%m%d%H%M%S")
    by_time = collections.defaultdict(list)
    for f in files:
        by_time[f["creation_utc"]].append(f)
    session = collections.defaultdict(collections.Counter)
    for f in files:
        if id(f) in cam:
            off = round((cam[id(f)] - utc[id(f)]).total_seconds() / 900) * 900  # quarter hours
            session[utc[id(f)].date()][off] += 1
    stats = collections.Counter()
    for f in files:
        if id(f) in cam:
            lt, src = cam[id(f)], "camera trailer"
        else:
            mate = next((g for g in by_time[f["creation_utc"]] if g is not f and id(g) in cam), None)
            if mate is not None:
                lt, src = cam[id(mate)], "companion trailer"
            elif session[utc[id(f)].date()]:
                off = session[utc[id(f)].date()].most_common(1)[0][0]
                lt, src = utc[id(f)] + dt.timedelta(seconds=off), "session offset"
            else:
                lt, src = utc[id(f)] + dt.timedelta(hours=default), "card default"
        f["local_time"] = lt.strftime("%Y%m%d_%H%M%S")
        f["local_time_source"] = src
        stats[src] += 1
    offsets = {str(k): {str(o / 3600): n for o, n in v.items()} for k, v in session.items()}
    print(json.dumps({"image": image, "sources": stats, "session_offsets_h": offsets}))
    cpath.write_text(json.dumps(cat, indent=1))
    if not rename or not MANIFEST.exists():
        return 0
    from rconfig import manifest_lock  # noqa: E402
    with manifest_lock():
        return _rename(files, old_names, numbers)


def _rename(files, old_names, numbers) -> int:
    rows = list(csv.DictReader(open(MANIFEST, newline="")))
    by = {r["file"]: r for r in rows}
    renamed = []
    for f in files:
        old, new = old_names[id(f)], final_name(f, numbers)
        if old == new:
            continue
        for d in (WORK / "assembled", OUT):
            if (d / old).exists():
                if (d / new).exists():
                    raise SystemExit(f"{d / new} already exists")
                os.rename(d / old, d / new)
        if old in by:
            by[old]["file"] = new
            renamed.append((old, new))
    tmp = MANIFEST.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, MANIFEST)
    print(json.dumps({"renamed_published": len(renamed), "examples": renamed[:3]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
