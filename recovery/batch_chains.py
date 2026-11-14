#!/usr/bin/env python3
"""Walk the mdat cluster chain of every catalogued card file (read-only).

Processes files in recording order so later files (often allocated into holes)
skip chunks already proven to belong to earlier files.  Resumable: existing
chain JSONs are loaded instead of recomputed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chain_walk import CS, ChunkCache, FileModel, walk  # noqa: E402
from rdr_reader import RdrObject  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402


def label(f: dict) -> str:
    return f"{f['kind']}_{f['name_time']}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--only", default=None, help="comma list of labels")
    ap.add_argument("--kinds", default="LRV,VID")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--skip-partial", action="store_true")
    ap.add_argument("--no-relaxed", action="store_true")
    args = ap.parse_args()
    if args.no_relaxed:
        import chain_walk
        chain_walk.RELAXED_MAX_DEPTH = 0

    cat = json.loads((WORK / "scan-analysis" / f"{args.image}-full-catalog.json").read_text())
    files = [f for f in cat["files"] if f["header_chunk"] is not None and f["kind"] in args.kinds.split(",")]
    files.sort(key=lambda f: (f["creation_utc"], 0 if f["kind"] == "LRV" else 1))
    if args.only:
        want = set(args.only.split(","))
        files = [f for f in files if label(f) in want]

    outdir = WORK / "chains" / (args.outdir or args.image)
    outdir.mkdir(parents=True, exist_ok=True)
    raw = RdrObject(args.image)
    cache = ChunkCache(raw, limit=2048)
    excluded: set[int] = set()
    for p in outdir.glob("*.json"):
        d = json.loads(p.read_text())
        excluded |= {m["chunk"] for m in d["mapping"] if m["how"] != "blind-ambiguous"}

    summary_path = WORK / "chains" / f"{args.image}-batch-summary.ndjson"
    for f in files:
        out = outdir / f"{label(f)}.json"
        if out.exists():
            continue
        if args.skip_partial and (outdir / f"{label(f)}.partial.json").exists():
            continue
        spec = {
            "name": f.get("name") or label(f),
            "label": label(f),
            "kind": f["kind"],
            "header_chunk": f["header_chunk"],
            "moov_start": f["moov_start"],
            "moov_size": f["moov_size"],
            "inst_start": f["inst_start"],
            "inst_size": f["inst_size"],
            "mdat_end": f["mdat_end"],
            "duration": f["duration"],
            "file_size": f["file_size"],
        }
        t = time.time()
        reads0 = cache.reads
        try:
            model = FileModel(spec, raw)
            max_chunk = f["moov_start"] // CS + 64
            excl = excluded - {f["header_chunk"]}
            mapping, status = walk(model, cache, excl, max_chunk, log_every=10**9)
        except Exception as exc:  # noqa: BLE001
            mapping, status = [], {"status": "error", "error": repr(exc)}
            model = None
        status["seconds"] = round(time.time() - t, 1)
        status["chunk_reads"] = cache.reads - reads0
        nclusters = model.nclusters if model else None
        blind = {}
        for m in mapping:
            if m["how"].startswith("blind"):
                blind[m["how"]] = blind.get(m["how"], 0) + 1
        rec = {"image": args.image, "spec": spec, "status": status, "placed": len(mapping),
               "nclusters": nclusters, "blind": blind,
               "jumps": sum(1 for m in mapping if m.get("skipped")), "mapping": mapping}
        if status["status"] == "complete":
            out.write_text(json.dumps(rec))
            excluded |= {m["chunk"] for m in mapping if m["how"] != "blind-ambiguous"}
        else:
            (outdir / f"{label(f)}.partial.json").write_text(json.dumps(rec))
        line = {k: v for k, v in rec.items() if k != "mapping"}
        line["label"] = label(f)
        with open(summary_path, "a") as fh:
            fh.write(json.dumps(line) + "\n")
        print(f"{label(f)} {status['status']} placed={len(mapping)}/{nclusters} blind={blind} "
              f"jumps={rec['jumps']} reads={status['chunk_reads']} {status['seconds']}s"
              + (f" lost@{status.get('at_cluster')} prev={status.get('prev_chunk')}" if status['status'] != 'complete' else "")
              + (f" {status.get('error')}" if status['status'] == 'error' else ""), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
