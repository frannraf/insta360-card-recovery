#!/usr/bin/env python3
"""One-screen status of the whole recovery, per card and per file kind.

Every catalogued file is in exactly one state:
  published    - in outputs/recovered/MANIFEST.csv
  reviewed-ok  - validated clean, waiting for the visual review / publish stage
  decode-bad   - validated, but a stream failed (exit, error lines or frame count)
  validating   - assembled, validation running or not yet started
  queued       - complete, fully resolved chain; not assembled yet
  blind        - chain complete but has unresolved blind clusters (trial stage)
  lost         - no complete chain
Also reports, per recording (a VID and its LRV), the best version recovered.
Read-only.  Usage: recovery_status.py [--chains card1=card1,card2=card2-v2]
"""

from __future__ import annotations

import collections
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chain_walk import UNRESOLVED  # noqa: E402
from pipeline import final_name, validation_complete  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402
from rconfig import OUT  # noqa: E402
from rconfig import IMAGES  # noqa: E402
CHAINS = {name: name for name in IMAGES}  # chain dir per image (override with --chains=)
CARD = {}
ORDER = ["published", "reviewed-ok", "decode-bad", "validating", "queued", "blind", "lost"]


def clean(label: str, f: dict) -> bool:
    rows = dict(re.findall(r"^(\w+) (.*)$", (WORK / "validation" / label / "result.txt").read_text(), re.M))
    vexp = [t[4] for t in f["tracks"] if t[0] == "vide"]
    for key, line in rows.items():
        if not re.fullmatch(r"[va]\d+", key):
            continue
        m = dict(x.split("=") for x in line.split())
        if m.get("decode_exit") != "0" or m.get("decode_error_lines") != "0":
            return False
        if key[0] == "v" and int(m.get("decoded_frames", -1)) != vexp[int(key[1:])]:
            return False
    return True


def main() -> int:
    for a in sys.argv[1:]:
        if a.startswith("--chains="):
            CHAINS.update(dict(x.split("=") for x in a.split("=", 1)[1].split(",")))
    published = {}
    man = OUT / "MANIFEST.csv"
    for r in (csv.DictReader(open(man, newline="")) if man.exists() else []):
        p = Path(r["chain_json"].replace("\\", "/"))
        published[(r["source"].split()[0], p.stem)] = r  # source = "<image> ... reconstruction"
    for image, cdir in CHAINS.items():
        cpath = WORK / "scan-analysis" / f"{image}-full-catalog.json"
        if not cpath.exists():
            continue
        files = json.loads(cpath.read_text())["files"]
        nf = WORK / "scan-analysis" / f"{image}-file-numbers.json"
        numbers = json.loads(nf.read_text()) if nf.exists() else {}
        state, dur = {}, {}
        for f in files:
            lab = f"{f['kind']}_{f['name_time']}"
            dur[lab] = f["duration"]
            chain = WORK / "chains" / cdir / f"{lab}.json"
            if (image, lab) in published:
                s = "published"
            elif not chain.exists():
                s = "lost"
            elif any(m["how"] in UNRESOLVED for m in json.loads(chain.read_text())["mapping"]):
                s = "blind"
            elif validation_complete(lab):
                s = "reviewed-ok" if clean(lab, f) else "decode-bad"
            elif (WORK / "assembled" / final_name(f, numbers)).exists():
                s = "validating"
            else:
                s = "queued"
            state[lab] = s
        print(f"\n{CARD.get(image, image)}  [chains: {cdir}]")
        for kind in ("VID", "LRV"):
            labs = [lab for lab in state if lab.startswith(kind)]
            c = collections.Counter(state[lab] for lab in labs)
            m = collections.Counter()
            for lab in labs:
                m[state[lab]] += dur[lab] / 60
            parts = "  ".join(f"{s} {c[s]} ({m[s]:.0f} min)" for s in ORDER if c[s])
            print(f"  {kind}: {len(labs)} files, {sum(dur[x] for x in labs)/60:.0f} min | {parts}")
        rec = collections.defaultdict(dict)
        for f in files:
            rec[f["creation_utc"]][f["kind"]] = state[f"{f['kind']}_{f['name_time']}"]
        best = collections.Counter()
        for r in rec.values():
            if r.get("VID") == "published":
                best["full-res 360 VID published"] += 1
            elif r.get("LRV") == "published":
                best["LRV preview published, VID still to come" if r.get("VID") not in (None, "lost")
                     else "LRV preview only (VID lost)"] += 1
            else:
                best["nothing published yet"] += 1
        print(f"  recordings: {len(rec)} | " + "  ".join(f"{k}: {v}" for k, v in best.most_common()))
        vid_pub = [r for (img, lab), r in published.items() if img == image and lab.startswith("VID")]
        if vid_pub:
            rebuilt = sum(1 for r in vid_pub if r["inst_trailer"].startswith("rebuilt"))
            print(f"  published VIDs with rebuilt Insta360 trailer (gyro etc.): {rebuilt}/{len(vid_pub)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
