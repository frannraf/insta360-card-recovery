#!/usr/bin/env python3
"""Reconstruct a deleted MP4/INSV cluster chain by walking length-prefixed NAL units.

Read-only on the RDR.  For every logical file cluster the walker knows (from the
recovered moov sample tables) exactly where each video sample starts and ends.
Inside a sample, every NAL unit is prefixed by a 4-byte big-endian length, so the
walker can predict every NAL boundary that falls inside the next 256 KiB file
cluster once the previous cluster has been placed.  A raw card chunk is accepted
for file cluster k only if *every* predicted NAL boundary in it carries a length
that fits exactly inside its sample and a valid NAL header.

Output: a JSON mapping (file cluster -> raw chunk, evidence) under work/chains.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mp4_tables import parse_moov, sample_layout  # noqa: E402
from rdr_reader import RdrObject  # noqa: E402

CS = 262144
RELAXED_MAX_DEPTH = 40  # set to 0 for a strict-only pass
# placement types that are NOT trustworthy until resolved; everything else
# (header, verified, verified-relaxed, verified-firstfit, last-pinned,
# blind-contiguous, blind-resolved-*) is a fixed placement
UNRESOLVED = {"blind-ambiguous", "blind-gapfill"}
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402
CATALOG = WORK / "recovery-candidate-map.csv"  # legacy chain_walk.py CLI input only


def nal_header_ok(codec: str, b0: int, b1: int | None) -> bool:
    if b0 & 0x80:
        return False
    if codec in ("avc1", "avc3"):
        t = b0 & 0x1F
        return 1 <= t <= 23
    # HEVC
    t = (b0 >> 1) & 0x3F
    if t > 40 or (b0 & 1):  # nuh_layer_id high bit must be 0 for single-layer
        return False
    if b1 is None:
        return True
    return (b1 & 0xF8) == 0 and (b1 & 7) >= 1


class FileModel:
    def __init__(self, spec: dict, raw: RdrObject):
        self.spec = spec
        moov = raw.read(spec["moov_start"], spec["moov_size"])
        self.info, self.tracks = parse_moov(moov)
        self.moov = moov
        lay = sample_layout(self.tracks)
        self.mdat_end = max(o + s for o, s, *_ in lay)
        if spec.get("mdat_end") and spec["mdat_end"] != self.mdat_end:
            raise ValueError(f"mdat end mismatch {spec['mdat_end']} vs {self.mdat_end}")
        self.nclusters = (self.mdat_end + CS - 1) // CS
        # video samples sorted by offset: (offset, size, codec)
        self.video = [(o, s, c) for (o, s, ti, k, c, si) in lay if self.tracks[ti].handler == "vide"
                      and c in ("avc1", "avc3", "hvc1", "hev1")]
        self.vstarts = [v[0] for v in self.video]

    def samples_starting_in(self, k: int):
        lo = bisect.bisect_left(self.vstarts, k * CS)
        hi = bisect.bisect_left(self.vstarts, (k + 1) * CS)
        return self.video[lo:hi]


def check_cluster(model: FileModel, k: int, data: bytes, pending: list, count_carried: bool = True):
    """Verify `data` as file cluster k.

    pending: list of (pos, sample_end, codec, prefix_bytes) NAL boundaries already
    predicted at file positions >= k*CS (prefix_bytes for length fields that
    straddle the previous cluster boundary).
    Returns (ok, checks, exact, new_pending).
    """
    base = k * CS
    end = base + len(data)
    queue = list(pending)
    for o, s, c in model.samples_starting_in(k):
        queue.append((o, o + s, c, b""))
    queue.sort()
    checks = exact = 0
    bits = 0.0
    carry = []
    table_anchors = {o for o, _s, _c in model.samples_starting_in(k)}
    carried_anchors = {q[0] for q in pending}
    anchors = table_anchors | (carried_anchors if count_carried else set())
    i = 0
    while i < len(queue):
        pos, send, codec, prefix = queue[i]
        i += 1
        if pos >= end:
            carry.append((pos, send, codec, prefix))
            continue
        need = 6 - len(prefix)  # 4 length bytes + 2 header bytes
        rel = pos + len(prefix) - base
        avail = data[rel : rel + need] if rel >= 0 else b""
        hdr = prefix + avail
        if len(hdr) < 6:
            # straddles cluster end: verify the bytes we have later
            carry.append((pos, send, codec, hdr))
            continue
        L = struct.unpack_from(">I", hdr, 0)[0]
        nxt = pos + 4 + L
        if L < 2 or nxt > send:
            return False, checks, exact, [], bits
        avc = codec in ("avc1", "avc3")
        b1 = None if avc else hdr[5]
        if not nal_header_ok(codec, hdr[4], b1):
            return False, checks, exact, [], bits
        checks += 1
        # Evidence against the strongest alternative (foreign but valid
        # length-prefixed video): only an independent anchor alignment and an
        # exact landing on *our* sample end discriminate; chained NALs inside
        # a foreign stream are valid by construction and earn nothing.
        if pos in anchors:
            span = max(2, send - pos - 4)
            bits += min(15.0, max(0.0, 32.0 - math.log2(span)) + (2.0 if avc else 8.0))
        if nxt == send:
            exact += 1
            bits += 15.0
        else:
            # next NAL in same sample; insert keeping order
            item = (nxt, send, codec, b"")
            bisect.insort(queue, item, lo=i)
    return True, checks, exact, carry, bits


def cluster_is_blind(model: FileModel, k: int, pending: list) -> bool:
    """True when no NAL boundary or sample start falls inside file cluster k."""
    base = k * CS
    end = min(base + CS, model.mdat_end)
    if model.samples_starting_in(k):
        return False
    return not any(base <= p[0] < end or (p[0] < base and p[3]) for p in pending)


def load_spec(file_id: str, kind: str) -> dict:
    with open(CATALOG, newline="") as fh:
        for row in csv.DictReader(fh):
            name = row["FileName"]
            if row["Kind"] == kind and name.rsplit("_", 1)[-1].split(".")[0] == file_id:
                return {
                    "name": name,
                    "kind": kind,
                    "header_chunk": int(row["HeaderChunkIndex"]),
                    "header_offset": int(row["HeaderStartLogicalOffset"]),
                    "declared_mdat_size": int(row["DeclaredMdatSize"]),
                    "moov_start": int(row["MoovStartLogicalOffset"]),
                    "moov_size": int(row["MoovSize"]),
                    "inst_start": int(row["InstStartLogicalOffset"]),
                    "inst_size": int(row["InstSize"]),
                    "duration": float(row["DurationSeconds"]),
                }
    raise KeyError(f"{kind} {file_id} not in catalog")


class ChunkCache:
    def __init__(self, raw: RdrObject, limit: int = 4096):
        self.raw, self.limit, self.d = raw, limit, {}
        self.reads = 0

    def get(self, i: int) -> bytes:
        v = self.d.get(i)
        if v is None:
            v = self.raw.read_chunk(i)
            self.reads += 1
            if len(self.d) >= self.limit:
                for key in list(self.d)[: self.limit // 4]:
                    del self.d[key]
            self.d[i] = v
        return v


def _place(st: dict, cand: tuple) -> None:
    """Commit a found candidate (and any deferred blind clusters) into state."""
    c, ch, ex, newp, bits, how, skipped_free, k = cand
    nb = len(st["blind"])
    prev = st["prev"]
    if nb:
        if len(skipped_free) == nb:
            run = all(skipped_free[i] == prev + 1 + i for i in range(nb)) and c == prev + nb + 1
            label = "blind-contiguous" if run else "blind-gapfill"
            chosen = skipped_free
        else:
            label = "blind-ambiguous"
            chosen = skipped_free[:nb]
        for bk, bc in zip(st["blind"], chosen):
            st["mapping"].append({"k": bk, "chunk": bc, "checks": 0, "exact": 0, "bits": 0,
                                  "how": label, "free_between": len(skipped_free)})
        st["blind"] = []
    st["mapping"].append({"k": k, "chunk": c, "checks": ch, "exact": ex, "bits": round(bits, 1),
                          "how": how, "skipped": c - prev - 1 - nb})
    st["pending"] = newp
    st["prev"] = c
    st["k"] = k + 1


def _search(model, cache, excluded, max_chunk, st, k, last_k, pinned_last, weak_bits, strong_bits):
    """Return (strong_candidate or None, weak_candidates list)."""
    nb = len(st["blind"])
    prev, pending = st["prev"], st["pending"]

    def limit(kk, data):
        return data[: model.mdat_end - kk * CS] if kk == last_k else data

    if k == last_k and pinned_last is not None:
        c = pinned_last
        free = []
        x = prev + 1
        while x < c and len(free) <= nb:
            if x not in excluded:
                free.append(x)
            x += 1
        ok, ch, ex, newp, bits = check_cluster(model, k, limit(k, cache.get(c)), pending)
        if ok and c > prev and len(free) >= nb:
            return (c, ch, ex, newp, bits, "last-pinned", free, k), []
        return None, []
    skipped_free: list[int] = []
    weak = []
    c = prev + 1
    while c <= max_chunk:
        if c in excluded:
            c += 1
            continue
        ok, ch, ex, newp, bits = check_cluster(model, k, limit(k, cache.get(c)), pending)
        if ok and ch > 0 and len(skipped_free) >= nb:
            contiguous = len(skipped_free) == nb
            need = weak_bits if contiguous else strong_bits
            total = bits
            kk, cc, pp = k + 1, c, newp
            while total < need and kk < last_k and kk <= k + 12:
                cc += 1
                while cc in excluded:
                    cc += 1
                if cluster_is_blind(model, kk, pp):
                    kk += 1
                    continue
                ok2, ch2, _ex2, pp, bits2 = check_cluster(model, kk, cache.get(cc), pp, count_carried=False)
                if not ok2:
                    break
                total += bits2
                kk += 1
            cand = (c, ch, ex, newp, bits, "verified", list(skipped_free), k)
            if total >= need:
                return cand, weak
            if len(weak) < 400 and bits >= 15.0:
                weak.append(cand)
        skipped_free.append(c)
        c += 1
    return None, weak


def _firstfit_extend(model, cache, excluded, max_chunk, st, last_k, pinned_last, weak_bits,
                     strong_bits, max_weak=150, confirm=48):
    """Follow first-fit weak candidates sequentially (what a first-fit
    allocator does) and accept only if `confirm` consecutive strictly verified
    clusters follow the last weak step (or the file completes)."""
    trial = {"mapping": [], "prev": st["prev"], "pending": st["pending"], "k": st["k"],
             "blind": list(st["blind"])}
    weak_steps = strong_run = 0
    while trial["k"] < model.nclusters:
        k = trial["k"]
        is_pinned = k == last_k and pinned_last is not None
        if not is_pinned and cluster_is_blind(model, k, trial["pending"]):
            trial["blind"].append(k)
            trial["k"] = k + 1
            continue
        cand, weak = _search(model, cache, excluded, max_chunk, trial, k, last_k, pinned_last,
                             weak_bits, strong_bits)
        if cand is not None:
            _place(trial, cand)
            strong_run += 1
            if weak_steps and strong_run >= confirm:
                return trial
        elif weak and weak_steps < max_weak:
            w = weak[0]
            _place(trial, (w[0], w[1], w[2], w[3], w[4], "verified-firstfit", w[6], w[7]))
            weak_steps += 1
            strong_run = 0
        else:
            return None
    return trial if not trial["blind"] else None


def _walk_from(model, cache, excluded, max_chunk, st, stop_k, depth, last_k, pinned_last,
               weak_bits, strong_bits, log_every, t0):
    while st["k"] < stop_k:
        k = st["k"]
        is_pinned = k == last_k and pinned_last is not None
        if not is_pinned and cluster_is_blind(model, k, st["pending"]):
            st["blind"].append(k)
            st["k"] = k + 1
            continue
        cand, weak = _search(model, cache, excluded, max_chunk, st, k, last_k, pinned_last,
                             weak_bits, strong_bits)
        if cand is None and depth < RELAXED_MAX_DEPTH:
            # shallow levels may branch; deep levels follow first-fit order only
            for w in weak[: (10 if depth == 0 else 3 if depth == 1 else 1)]:
                trial = {"mapping": [], "prev": st["prev"], "pending": st["pending"], "k": k,
                         "blind": list(st["blind"])}
                _place(trial, w)
                horizon = min(model.nclusters, k + 49)
                status = _walk_from(model, cache, excluded, max_chunk, trial, horizon, depth + 1,
                                    last_k, pinned_last, weak_bits, strong_bits, 0, t0)
                ev = sum(m["bits"] for m in trial["mapping"])
                if status == "ok" and (ev >= 300 or horizon == model.nclusters):
                    cand = (w[0], w[1], w[2], w[3], w[4], "verified-relaxed", w[6], w[7])
                    break
        if cand is None and depth == 0:
            ext = _firstfit_extend(model, cache, excluded, max_chunk, st, last_k, pinned_last,
                                   weak_bits, strong_bits)
            if ext is not None:
                st["mapping"].extend(ext["mapping"])
                st["prev"], st["pending"], st["k"], st["blind"] = ext["prev"], ext["pending"], ext["k"], ext["blind"]
                continue
        if cand is None:
            return "lost"
        _place(st, cand)
        if log_every and k % log_every == 0:
            print(f"  k={k}/{model.nclusters} chunk={cand[0]} reads={cache.reads} {time.time()-t0:.0f}s",
                  flush=True)
    if st["k"] >= model.nclusters and st["blind"]:
        return "lost"
    return "ok"


def walk(model: FileModel, cache: ChunkCache, excluded: set[int], max_chunk: int,
         min_chunk: int | None = None, log_every: int = 200,
         weak_bits: float = 10.0, strong_bits: float = 40.0):
    """Hardened chain walk.

    * Blind clusters (no NAL boundary inside) are deferred, never guessed; they
      are filled only from the free chunks between their verified neighbours.
    * The last mdat cluster is pinned to the chunk holding the moov start
      (the moov begins inside that same cluster) whenever mdat_end % CS != 0.
    * A non-contiguous jump needs >= strong_bits of NAL evidence accumulated over
      the jump target and up to 12 contiguous followers; a contiguous step needs
      >= weak_bits.  If no candidate qualifies, weak candidates are tried in a
      bounded speculative sub-walk that must then succeed for 48 more clusters
      with >= 300 bits of evidence (handles first-fit single-cluster holes).
    """
    spec = model.spec
    H = spec["header_chunk"]
    last_k = model.nclusters - 1
    pinned_last = spec["moov_start"] // CS if model.mdat_end % CS else None
    first = cache.get(H)
    if last_k == 0:
        first = first[: model.mdat_end]
    ok, ch, ex, pending, bits = check_cluster(model, 0, first, [])
    if not ok:
        raise RuntimeError("header chunk fails its own NAL checks")
    st = {"mapping": [{"k": 0, "chunk": H, "checks": ch, "exact": ex, "bits": round(bits, 1),
                       "how": "header"}],
          "prev": H, "pending": pending, "k": 1, "blind": []}
    status = _walk_from(model, cache, excluded, max_chunk, st, model.nclusters, 0, last_k,
                        pinned_last, weak_bits, strong_bits, log_every, time.time())
    mapping = sorted(st["mapping"], key=lambda m: m["k"])
    if status != "ok":
        return mapping, {"status": "lost", "at_cluster": st["blind"][0] if st["blind"] else st["k"],
                         "prev_chunk": st["prev"], "pending_blind": st["blind"]}
    return mapping, {"status": "complete"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--kind", required=True, choices=["LRV", "VID"])
    ap.add_argument("--id", required=True)
    ap.add_argument("--exclude", action="append", default=[], help="chain JSON whose chunks to exclude")
    ap.add_argument("--max-chunk", type=int, default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    raw = RdrObject(args.image)
    spec = load_spec(args.id, args.kind)
    spec["mdat_end"] = 36 + spec["declared_mdat_size"] if spec["declared_mdat_size"] < 2**32 else None
    model = FileModel(spec, raw)
    excluded: set[int] = set()
    for path in args.exclude:
        excluded |= {m["chunk"] for m in json.loads(Path(path).read_text())["mapping"]}
    max_chunk = args.max_chunk or (spec["moov_start"] // CS + 64)
    cache = ChunkCache(raw)
    print(json.dumps({"file": spec["name"], "clusters": model.nclusters, "mdat_end": model.mdat_end,
                      "tracks": [(t.handler, t.codec, t.width, t.height, t.frame_count) for t in model.tracks],
                      "header_chunk": spec["header_chunk"], "max_chunk": max_chunk}), flush=True)
    t = time.time()
    mapping, status = walk(model, cache, excluded, max_chunk)
    status["seconds"] = round(time.time() - t, 1)
    status["chunk_reads"] = cache.reads
    out = args.out or (WORK / "chains" / f"{args.image}-{args.kind}{args.id}-chain.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out = out.with_name(out.stem + f"-{int(time.time())}.json")
    blind = sum(1 for m in mapping if m["how"].startswith("blind"))
    jumps = [(m["k"], m["chunk"], m["skipped"]) for m in mapping if m.get("skipped")]
    summary = {"image": args.image, "spec": spec, "status": status, "placed": len(mapping),
               "nclusters": model.nclusters, "blind": blind, "jumps": len(jumps), "mapping": mapping}
    out.write_text(json.dumps(summary, indent=0))
    print(json.dumps({k: v for k, v in summary.items() if k != "mapping"}))
    print("first jumps:", jumps[:30])
    print("wrote", out)
    return 0 if status["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
