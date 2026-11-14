#!/usr/bin/env python3
"""Rebuild the Insta360 'inst' trailer of a reconstructed file (read-only on RDR).

Model (verified on pairs 004/009/016):
  * inst = [8-byte atom header][records ...][index][32 pad][u32 len][u32 ver][magic]
  * every record starts cluster-aligned (or right after the previous record's
    6-byte header inside the same cluster) and is followed by a 6-byte header
    [u8 fmt][u8 id][u32 size LE]; the index (last cluster) lists (id, size, off).
  * VID and LRV carry byte-identical record payloads (two physical copies).
  * static records (images: 0x16, 0x2, ...) are written at recording start as
    contiguous runs whose first cluster begins [u32 1][u32 size];
  * time-series records (IMU 0x3, 0x1a, 0x9, 0xb, 0x12, ...) are appended as
    duplicate cluster pairs during the recording, in physical (time) order;
  * a slot may be rewritten later (copy-on-write); the final version is the one
    carrying *every* record header expected in that slot.

Writes work/inst/<label>.plan.json (slot -> chunk, evidence) and, if complete,
work/inst/<label>.inst.bin (exactly inst_size bytes).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rdr_reader import RdrObject  # noqa: E402

CS = 262144
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rconfig import WORK  # noqa: E402
MAG = b"8db42d694ccc418790edff439fe026bf"
from chain_walk import UNRESOLVED  # noqa: E402


class _Fixed:
    """Every placement type except the unresolved blind ones is fixed."""
    def __contains__(self, how):
        return how not in UNRESOLVED


FIXED = _Fixed()


def load_ownership(image: str, cat: list[dict], chains: str | None = None) -> dict[int, str]:
    own: dict[int, str] = {}
    for p in (WORK / "chains" / (chains or image)).glob("*.json"):
        if p.name.endswith(".partial.json") or ".pre-resolve" in p.name:
            continue
        d = json.loads(p.read_text())
        for m in d["mapping"]:
            if m["how"] in FIXED:
                own[m["chunk"]] = d["spec"]["label"]
    for f in cat:
        a = f["moov_start"] // CS
        b = (f["moov_start"] + f["moov_size"]) // CS  # includes inst-header cluster
        for c in range(a, b + 1):
            own.setdefault(c, f"moov:{f['kind']}_{f['name_time']}")
    return own


def row_period(d: bytes) -> int:
    best, bestv = 0, 0.0
    s = d[:32768]
    for p in (4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 48, 56, 64, 72, 80, 96, 128):
        n = len(s) - p
        eq = sum(1 for i in range(0, n, 5) if s[i] == s[i + p]) / len(range(0, n, 5))
        if eq > bestv + 0.03:
            best, bestv = p, eq
    return best


def ts_monotonic(buf: bytes, period: int) -> tuple[int, float] | None:
    """Find a u64 field offset that increases monotonically across rows."""
    best = None
    rows = len(buf) // period
    if rows < 8:
        return None
    for off in range(0, period - 7, 4):
        vals = [struct.unpack_from("<Q", buf, i * period + off)[0] for i in range(rows)]
        if not (1000 < vals[0] < 1 << 48):
            continue
        inc = sum(1 for a, b in zip(vals, vals[1:]) if 0 < b - a < 10_000_000)
        frac = inc / (rows - 1)
        if best is None or frac > best[1]:
            best = (off, frac)
    return best


def expected_positions(chain_dir: Path, pair: list[dict], rec: dict, nslots: int) -> list[int] | None:
    """Physical chunk where each series slot is expected to be flushed.

    slot j is written once (j+1)*CS bytes of the record exist; with a constant
    data rate that is at t=(j+1)*CS/rate, mapped to a physical chunk through
    the companion VID (else LRV) mdat chain.
    """
    for kind in ("VID", "LRV"):
        f = next((x for x in pair if x["kind"] == kind), None)
        if not f:
            continue
        cp = chain_dir / f"{kind}_{f['name_time']}.json"
        if not cp.exists():
            continue
        cm = {m["k"]: m["chunk"] for m in json.loads(cp.read_text())["mapping"]}
        dur = f["duration"]
        rate = rec["size"] / dur
        out = []
        for j in range(nslots):
            t = min(dur, ((j + 1) * CS - rec["start"] % CS) / rate)
            fc = int(t / dur * f["mdat_end"]) // CS
            out.append(cm.get(fc, cm.get(fc - 1)))
        if all(x is not None for x in out):
            return out
    return None


def best_ordered_subset(cands: list[int], expected: list[int]) -> list[int]:
    """Order-preserving choice of len(expected) candidates minimising |c-e|."""
    n, m = len(cands), len(expected)
    INF = float("inf")
    dp = [[INF] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0
    for i in range(1, n + 1):
        dp[i][0] = 0
        for j in range(1, min(i, m) + 1):
            dp[i][j] = min(dp[i - 1][j], dp[i - 1][j - 1] + abs(cands[i - 1] - expected[j - 1]))
    out, i, j = [], n, m
    while j:
        if dp[i][j] == dp[i - 1][j] and i - 1 >= j:
            i -= 1
        else:
            out.append(cands[i - 1]); i -= 1; j -= 1
    return out[::-1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("label")
    ap.add_argument("--image", required=True)
    ap.add_argument("--after", type=int, default=400, help="chunks searched past the moov")
    ap.add_argument("--chains", default=None, help="chain directory name under work/chains")
    args = ap.parse_args()

    cat = json.loads((WORK / "scan-analysis" / f"{args.image}-full-catalog.json").read_text())["files"]
    kind, t = args.label.split("_", 1)
    f = next(x for x in cat if x["kind"] == kind and x["name_time"] == t)
    pair = [x for x in cat if x["name_time"] == t]
    times = sorted({x["name_time"] for x in cat})
    prev_t = times[times.index(t) - 1] if times.index(t) > 0 else None
    prev = [x for x in cat if x["name_time"] == prev_t]
    raw = RdrObject(args.image)
    own = load_ownership(args.image, cat, args.chains)

    moov_end_chunk = max((x["moov_start"] + x["moov_size"]) // CS for x in pair)
    R0 = max(((x["moov_start"] + x["moov_size"]) // CS for x in prev), default=min(x["header_chunk"] for x in pair) - 3000)
    RA = moov_end_chunk
    RB = moov_end_chunk + args.after
    # this recording's own region starts at its first mdat chunk after the
    # previous recording's moov; chunks before that are the previous
    # recording's end-of-recording zone
    data_start = R0
    cdir = WORK / "chains" / (args.chains or args.image)
    own_chunks = []
    for x in pair:
        cp = cdir / f"{x['kind']}_{x['name_time']}.json"
        if cp.exists():
            own_chunks += [m["chunk"] for m in json.loads(cp.read_text())["mapping"] if m["chunk"] > R0]
    if own_chunks:
        data_start = min(own_chunks)
    win = [c for c in range(R0, RB + 1) if c not in own]
    data = {c: raw.read_chunk(c) for c in win}
    inA = {c for c in win if data_start <= c <= RA}
    plan_data_start = data_start

    inst_start = f["mdat_end"] + f["moov_size"]
    fsize = f["file_size"]
    end = fsize % CS
    finals = [c for c in win if data[c][end - 32:end] == MAG
              and struct.unpack_from("<I", data[c], end - 40)[0] == f["inst_size"] - 8]
    plan = {"label": args.label, "window": [R0, RA, RB], "data_start": data_start, "free_in_window": len(win), "finals": finals}
    out_dir = WORK / "inst"
    out_dir.mkdir(exist_ok=True)
    if not finals:
        plan["status"] = "no-final"
        (out_dir / f"{args.label}.plan.json").write_text(json.dumps(plan, indent=1))
        print(json.dumps(plan))
        return 2
    final = max(finals)
    fd = data[final]
    _fmt, _rid, isz = struct.unpack("<BBI", fd[end - 78:end - 72])
    idx = fd[end - 78 - isz:end - 78]
    recs = []
    for i in range(0, len(idx) - 9, 10):
        rid, sz, off = struct.unpack_from("<HII", idx, i)
        if sz:
            P = inst_start + 8 + off
            recs.append({"id": rid, "size": sz, "start": P, "hdr": P + sz})
    recs.sort(key=lambda r: r["start"])
    fc0 = inst_start // CS
    fcN = (fsize - 1) // CS
    slots = list(range(fc0 + 1, fcN))  # clusters to fill (fc0 = moov tail cluster, fcN = final)

    # expected headers per slot
    exp_hdr = defaultdict(list)
    for r in recs:
        if r["id"] == 0x101:
            continue
        exp_hdr[r["hdr"] // CS].append((r["hdr"] % CS, struct.pack(">H", r["id"]) + struct.pack("<I", r["size"])))
    # record coverage per slot
    cover = defaultdict(list)
    for r in recs:
        for s in range(r["start"] // CS, (r["hdr"] + 5) // CS + 1):
            cover[s].append(r["id"])

    assign: dict[int, dict] = {}
    used: set[int] = {final}

    def full_match(c, s):
        d = data[c]
        return all(d[o:o + 6] == sig for o, sig in exp_hdr[s])

    # 1) slots carrying record headers: pick latest full match, window A first
    for s in slots:
        if not exp_hdr[s]:
            continue
        cands = [c for c in win if full_match(c, s)]
        a = [c for c in cands if c in inA]
        b = [c for c in cands if c > RA]  # own end-of-recording zone only
        # end-of-recording zone (right after own moov) holds the final
        # rewritten versions (0x16 thumbnails + 0x14, 0x1c); otherwise the
        # latest version written during the recording wins
        pick = min(b) if b else (max(a) if a else None)
        if pick is None and cands:
            assign[s] = {"chunk": None, "how": f"only-outside-recording {sorted(cands)[:6]}"}
            continue
        if pick is not None:
            assign[s] = {"chunk": pick, "how": "header-sig" + ("-end-zone" if pick > RA else ""),
                         "versions": len(cands)}

    # 2) multi-slot records
    groups = defaultdict(list)
    for c in win:
        groups[hashlib.md5(data[c]).digest()].append(c)
    hdr_sigs = [(r["hdr"] % CS, struct.pack(">H", r["id"]) + struct.pack("<I", r["size"])) for r in recs]
    def carries_header(c):
        d = data[c]
        return any(d[o:o + 6] == sig for o, sig in hdr_sigs)
    dup_first = {}
    for g in groups.values():
        if len(g) >= 2 and not any(carries_header(c) for c in g):
            dup_first[min(g)] = sorted(g)
    report = []
    for phase in ("static", "series"):
      for r in recs:
        s0, s1 = r["start"] // CS, r["hdr"] // CS
        body = [s for s in range(s0, s1) if s in slots and s not in assign]
        if not body:
            continue
        if phase == "series":
            last = assign.get(s1, {}).get("chunk")
            tmpl = data[last][: max(1, r["hdr"] % CS)] if last else b""
            p_r = row_period(tmpl) if len(tmpl) > 4096 else None
            lower = plan_data_start
            pool = [(c0, g) for c0, g in sorted(dup_first.items())
                    if c0 not in used and not (last and c0 > last) and c0 in inA and c0 > lower]
            report.append({"id": r["id"], "kind": "series", "slots": len(body), "period": p_r,
                           "pool": len(pool)})
            r["_pool"], r["_period"], r["_body"] = pool, p_r, body
            continue
        # a) static run from a size-prefixed start cluster
        startsig = struct.pack("<II", 1, r["size"])
        ok_static = False
        # the body must be the contiguous run ending right before the chosen
        # last-slot version (end-of-recording rewrite) or the final cluster
        last_c = final if s1 == fcN else assign.get(s1, {}).get("chunk")
        starts = []
        if last_c is not None and r["start"] % CS == 0:
            st = last_c - len(body)
            if st in data and data[st][:8] == startsig:
                starts = [st]
        if not starts and r["start"] % CS == 0 and last_c is None:
            starts = sorted(c for c in win if data[c][:8] == startsig and c in inA)
        for st in starts:
            run = [st + i for i in range(len(body))]
            if all(c in data and c not in used for c in run):
                for s, c in zip(body, run):
                    assign[s] = {"chunk": c, "how": f"static-run {r['id']:#x}"}
                used.update(run)
                ok_static = True
                break
        if ok_static:
            report.append({"id": r["id"], "kind": "static", "slots": len(body)})

    # assign series records: classify pool chunks by period and markers
    series = [r for r in recs if "_body" in r]
    if series:
        feats = {}
        allpool = sorted({c0 for r in series for c0, _ in r["_pool"]})
        for c0 in allpool:
            d = data[c0]
            feats[c0] = (row_period(d), d.count(b"\x08\x10\x22\x20\x18\x00\x6f\x00"))
        taken = set()
        # most specific first: records with a distinctive period
        for r in sorted(series, key=lambda r: (r["_period"] in (None, 4), -len(r["_body"]))):
            need = len(r["_body"])
            if r["id"] == 0xB:
                cand = [c0 for c0, _ in r["_pool"] if c0 not in taken and feats[c0][1] > 0]
            elif r["_period"] and r["_period"] != 4:
                cand = [c0 for c0, _ in r["_pool"] if c0 not in taken and feats[c0][0] == r["_period"]]
            else:
                cand = [c0 for c0, _ in r["_pool"] if c0 not in taken and feats[c0][1] == 0
                        and feats[c0][0] in (4, 0)]
            r["_cand"] = cand
            if len(cand) > need:
                exp = expected_positions(WORK / "chains" / (args.chains or args.image), pair, r, need)
                if exp:
                    picked = best_ordered_subset(sorted(cand), exp)
                    r["_expected"] = exp
                    r["_dropped"] = sorted(set(cand) - set(picked))
                    cand = picked
            if len(cand) == need:
                for s, c in zip(r["_body"], sorted(cand)):
                    assign[s] = {"chunk": c, "how": f"series {r['id']:#x} p{r['_period']}"}
                taken.update(cand)
            else:
                for s in r["_body"]:
                    assign.setdefault(s, {"chunk": None, "how": f"series {r['id']:#x} unresolved "
                                          f"(need {need}, have {len(cand)})"})
    missing = [s for s in slots if s not in assign or assign[s]["chunk"] is None]
    plan.update({"final": final, "inst_slots": len(slots) + 2, "records": [
        {"id": hex(r["id"]), "size": r["size"], "slot0": r["start"] // CS - fc0, "slot1": r["hdr"] // CS - fc0,
         "series_candidates": r.get("_cand"), "expected": r.get("_expected"), "dropped": r.get("_dropped")}
        for r in recs], "report": report,
        "assign": {str(s - fc0): v for s, v in sorted(assign.items())}, "missing": [s - fc0 for s in missing]})

    # 3) build bytes + checks
    if not missing:
        tail_first = raw.read(f["moov_start"] + f["moov_size"], CS - (inst_start % CS)) if inst_start % CS else b""
        buf = bytearray(tail_first)
        for s in slots:
            buf += data[assign[s]["chunk"]]
        buf += fd[:end]
        if len(buf) != f["inst_size"]:
            plan["status"] = f"size-mismatch {len(buf)} vs {f['inst_size']}"
        else:
            checks = []
            for r in recs:
                o = r["hdr"] - inst_start
                hdr = bytes(buf[o:o + 6])
                exp = struct.pack(">H", r["id"]) + struct.pack("<I", r["size"])
                checks.append(hdr == exp)
            ts = {}
            for r in recs:
                if r["id"] in (0x3,):
                    a, b = r["start"] - inst_start, r["hdr"] - inst_start
                    rows = (b - a) // 20
                    vals = [struct.unpack_from("<Q", buf, a + i * 20)[0] for i in range(rows)]
                    bad = sum(1 for x, y in zip(vals, vals[1:]) if not (0 < y - x < 1_000_000))
                    ts[hex(r["id"])] = {"rows": rows, "non_monotonic_steps": bad,
                                        "first": vals[0] if vals else None, "last": vals[-1] if vals else None}
            # generic: fixed-row series records -> find a monotonic u64/u32 field
            for r in recs:
                per = r.get("_period")
                if r["id"] == 0x3 or not per or per < 8 or "_body" not in r:
                    continue
                a, b = r["start"] - inst_start, r["hdr"] - inst_start
                rec = bytes(buf[a:b])
                rows = len(rec) // per
                best = None
                for off in range(0, per - 3, 4):
                    for fmt, w in (("<Q", 8), ("<I", 4)):
                        if off + w > per:
                            continue
                        vals = [struct.unpack_from(fmt, rec, i * per + off)[0] for i in range(rows)]
                        nz = [v for v in vals if v]
                        if len(nz) < rows * 0.5:
                            continue
                        inc = sum(1 for x, y in zip(nz, nz[1:]) if y > x)
                        frac = inc / max(1, len(nz) - 1)
                        if best is None or frac > best[0]:
                            best = (frac, off, fmt, len(nz))
                if best:
                    ts[hex(r["id"])] = {"rows": rows, "field": f"{best[2]}@{best[1]}",
                                        "monotonic_fraction": round(best[0], 5), "nonzero_rows": best[3]}
            plan["record_headers_ok"] = all(checks)
            plan["timeseries_checks"] = ts
            plan["inst_header_ok"] = bytes(buf[4:8]) == b"inst" and struct.unpack_from(">I", buf, 0)[0] == f["inst_size"]
            plan["status"] = "complete" if all(checks) and plan["inst_header_ok"] and all(
                v.get("non_monotonic_steps", 0) == 0 for v in ts.values()) else "complete-with-warnings"
            (out_dir / f"{args.label}.inst.bin").write_bytes(bytes(buf))
            plan["inst_sha256"] = hashlib.sha256(buf).hexdigest().upper()
    else:
        plan["status"] = "incomplete"
    (out_dir / f"{args.label}.plan.json").write_text(json.dumps(plan, indent=1))
    print(json.dumps({k: plan[k] for k in ("label", "status", "finals", "missing", "report")
                      if k in plan}))
    for k in ("record_headers_ok", "inst_header_ok", "timeseries_checks"):
        if k in plan:
            print(k, plan[k])
    return 0 if plan["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
