#!/usr/bin/env python3
"""Rebuild the cluster chain of an Insta360 X5 .insp photo from the free clusters of a card.

Layout of an X5 .insp (all offsets follow from the Exif length in the first cluster):
  SOI, APP1 Exif (camera path), 75 APP2 segments of 65,535 + 1 of 227 bytes holding a
  4,915,200-byte stitched preview (2560x1280 YUV420), DQT/SOF0/DHT/DRI/SOS, the baseline scan
  (11904x5952, restart interval 65535), EOI, then the Insta360 trailer: small records,
  record 0x2 = 40-byte header + the SAME 4,915,200-byte preview, and a 78-byte tail
  (record header, 32 zero bytes, trailer length, version 3, magic).

Walk (first-fit model, photos walked in the order they were taken: a file's clusters are free clusters in ascending order):
  header region  candidates must carry FF E2 FF FF at exactly the predicted offsets
                 (and the DQT..SOS markers where predicted)
  scan region    candidates must be JPEG scan data (entropy-valid, many FF 00 pairs); the
                 prefix is checked bit-exactly with jpegmcu (MCU counts at every RST/EOI);
                 on a failure the walk backtracks over the last restart interval
  trailer        once the 0x2 record header is found, every remaining byte is known (it is a
                 copy of the APP2 preview), so each cluster is found by exact content hash;
                 the last cluster must hold the magic at the predicted offset
A cluster that cannot be found in the header or trailer region can be rebuilt byte-exactly
from the other copy of the preview ("rebuilt-exact"); a missing scan cluster cannot.
"""
from __future__ import annotations

import bisect
import hashlib
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from photo_common import CS, jpegmcu_bytes  # noqa: E402

APP2_SEG = 65537
APP2_PAY = 65533
PREVIEW = 4915200
N_FULL = PREVIEW // APP2_PAY          # 75 full APP2 segments
LAST_PAY = PREVIEW - N_FULL * APP2_PAY  # 225 bytes in the short one
REC2_SIZE = PREVIEW + 40
REC2_PREFIX = bytes.fromhex("0100000028004b000100000000000000000a000000050000")
MAG = b"8db42d694ccc418790edff439fe026bf"


class Card:
    """Features of the free clusters of one card plus a way to read them."""

    def __init__(self, features: list[dict], source, tiny: list[int] | None = None):
        self.f = {x["chunk"]: x for x in features}
        self.free = sorted(self.f)
        self.src = source
        # free chunks whose record is tiny (mostly zero/constant): not scanned, but a file's
        # last cluster (a few KB of data, then zeros) looks exactly like that
        self.tiny = sorted(tiny or [])
        self.used: set[int] = set()
        self.by_app2pos: dict[int, set[int]] = {}
        self.by_sha: dict[str, list[int]] = {}
        self.scan: list[int] = []
        self.rec2: list[int] = []
        self.mag: list[int] = []
        for c in self.free:
            x = self.f[c]
            for p in x.get("app2", []):
                self.by_app2pos.setdefault(p, set()).add(c)
            self.by_sha.setdefault(x.get("sha1", ""), []).append(c)
            if (x.get("entropy") and x.get("ff00", 0) >= 300) or "eoi" in x:
                self.scan.append(c)
            if "rec2" in x:
                self.rec2.append(c)
            if "mag" in x:
                self.mag.append(c)

    def get(self, c: int) -> bytes:
        return self.src(c)

    @staticmethod
    def first_after(lst: list[int], prev: int, ok=lambda c: True, limit=None):
        i = bisect.bisect_right(lst, prev)
        n = 0
        while i < len(lst):
            if ok(lst[i]):
                return lst[i]
            i += 1
            n += 1
            if limit and n >= limit:
                break
        return None

    def nearest(self, lst: list[int], prev: int, ok=lambda c: True):
        c = self.first_after(lst, prev, ok)
        if c is not None:
            return c, "ascending"
        for c in lst:  # nothing after: allow a jump back (not first-fit, flagged)
            if ok(c):
                return c, "jump-back"
        return None, "missing"


def walk(card: Card, start: int, log=print) -> dict:
    d0 = card.get(start)
    if d0[:4] != b"\xff\xd8\xff\xe1":
        return {"status": "not-an-exif-jpeg", "start": start}
    exif_len = struct.unpack_from(">H", d0, 4)[0]
    a0 = 4 + exif_len
    app2_at = [a0 + k * APP2_SEG for k in range(N_FULL)]
    last_app2 = a0 + N_FULL * APP2_SEG
    E = last_app2 + 4 + LAST_PAY
    marks_exp = {E: 0xDB, E + 134: 0xC0, E + 153: 0xC4, E + 573: 0xDD, E + 579: 0xDA}
    S = E + 593
    plan = {"start": start, "exif_len": exif_len, "scan_start": S, "mapping": [{"k": 0, "chunk": start, "how": "start"}]}
    chain = [start]
    card.used.add(start)

    def mark_ok(c, lo, hi):
        want = {p - lo: m for p, m in marks_exp.items() if lo <= p < hi - 1}
        if not want:
            return True
        have = {p: m for p, m in card.f[c].get("marks", [])}
        return all(have.get(p) == m for p, m in want.items())

    # -- header region -------------------------------------------------------------
    k = 1
    while k * CS < S:
        lo, hi = k * CS, (k + 1) * CS
        exp = [p - lo for p in app2_at if lo <= p and p + 4 <= hi]
        if exp:
            cands = sorted(set.intersection(*(card.by_app2pos.get(p, set()) for p in exp)))
        else:
            cands = [c for c in card.free if "marks" in card.f[c]]
        prev = max(x for x in chain if x is not None)
        c, how = card.nearest(cands, prev, lambda c: c not in card.used and mark_ok(c, lo, hi))
        chain.append(c)
        plan["mapping"].append({"k": k, "chunk": c, "how": how if c is not None else "missing-header"})
        if c is not None:
            card.used.add(c)
        k += 1

    # -- scan region ---------------------------------------------------------------
    def assembled(upto_k=None):
        parts = []
        for i, c in enumerate(chain[:upto_k]):
            parts.append(card.get(c) if c is not None else bytes(CS))
        return b"".join(parts)

    eoi = None
    checks = 0
    fails = 0
    while eoi is None:
        prev = max(x for x in chain if x is not None)
        c, how = card.nearest(card.scan, prev, lambda c: c not in card.used)
        if c is None:
            plan["status"] = "scan-ran-out"
            plan["mapping"].append({"k": k, "chunk": None, "how": "missing-scan"})
            return finish(plan, chain)
        chain.append(c)
        card.used.add(c)
        plan["mapping"].append({"k": k, "chunk": c, "how": how})
        fx = card.f[c]
        if "eoi" in fx or k % 6 == 0:
            checks += 1
            data = assembled()
            r = jpegmcu_bytes(data)
            real_error = not r["ok"] and not (r.get("error", "").startswith("marker reached") and r["error_offset"] >= len(data) - 2)
            if r["ok"]:
                eoi = r["eoi"]
            elif real_error:
                fails += 1
                bad_k = r["error_offset"] // CS
                log(f"  scan check failed at cluster {bad_k} ({r['error']}); backtracking")
                fixed = backtrack(card, chain, plan, bad_k, S, log)
                if not fixed or fails > 20:
                    plan["status"] = "scan-unresolved"
                    plan["scan_error"] = r
                    return finish(plan, chain)
                k = len(chain) - 1
                r2 = jpegmcu_bytes(assembled())
                if r2["ok"]:
                    eoi = r2["eoi"]
        k += 1
    plan["eoi"] = eoi
    plan["scan_checks"] = checks

    # -- trailer ---------------------------------------------------------------------
    t0 = eoi + 2
    kc = t0 // CS
    while len(chain) > kc + 1:  # EOI cluster is the last one we need from the scan walk
        card.used.discard(chain.pop())
        plan["mapping"].pop()
    rec2_off = None
    dk = card.get(chain[kc])
    i = dk.find(REC2_PREFIX, t0 - kc * CS)
    if i >= 0:
        rec2_off = kc * CS + i
    else:
        prev = max(x for x in chain if x is not None)
        c, how = card.nearest(card.rec2, prev, lambda c: c not in card.used)
        if c is not None:
            chain.append(c)
            card.used.add(c)
            plan["mapping"].append({"k": kc + 1, "chunk": c, "how": how + "-rec2"})
            rec2_off = (kc + 1) * CS + card.f[c]["rec2"][0]
    if rec2_off is None:
        plan["status"] = "no-trailer-record"
        return finish(plan, chain)
    size = rec2_off + REC2_SIZE + 78
    plan["file_size"] = size
    plan["rec2_off"] = rec2_off
    # predicted bytes from rec2_off to the end
    payload = bytearray(PREVIEW)
    have_pay = bytearray(PREVIEW)  # 1 where known from the header copy
    for seg in range(N_FULL + 1):
        fo = app2_at[seg] + 4 if seg < N_FULL else last_app2 + 4
        n = APP2_PAY if seg < N_FULL else LAST_PAY
        po = seg * APP2_PAY
        # copy byte ranges cluster by cluster
        o = fo
        while o < fo + n:
            kk = o // CS
            take = min(fo + n, (kk + 1) * CS) - o
            if kk < len(chain) and chain[kk] is not None:
                src = card.get(chain[kk])
                payload[po + (o - fo):po + (o - fo) + take] = src[o - kk * CS:o - kk * CS + take]
                have_pay[po + (o - fo):po + (o - fo) + take] = b"\x01" * take
            o += take
    rec2_hdr = dk[i:i + 40] if i >= 0 else card.get(chain[kc + 1])[card.f[chain[kc + 1]]["rec2"][0]:][:40]
    tail = (rec2_hdr + bytes(payload) + struct.pack(">H", 2) + struct.pack("<I", REC2_SIZE) + bytes(32)
            + struct.pack("<II", size - t0, 3) + MAG)
    plan["preview_known_from_header"] = round(sum(have_pay) / PREVIEW, 4)
    k = len(chain)
    while k * CS < size:
        lo = k * CS
        hi = min(size, lo + CS)
        exp = tail[lo - rec2_off:hi - rec2_off]
        prev = max(x for x in chain if x is not None)
        if hi - lo == CS:
            cands = card.by_sha.get(hashlib.sha1(exp).hexdigest(), [])
            c, how = card.nearest(cands, prev, lambda c: c not in card.used)
        else:
            want = (size - 32) - lo
            c, how = card.nearest(card.mag, prev,
                                  lambda c: c not in card.used and want in card.f[c]["mag"] and card.get(c)[:hi - lo] == exp)
            if c is None and card.tiny:
                i = bisect.bisect_right(card.tiny, prev)
                for t in card.tiny[i:i + 16]:
                    if t not in card.used and card.get(t)[:hi - lo] == exp:
                        c, how = t, "ascending-tiny"
                        break
        if c is None:
            how = "rebuilt-exact" if sum(have_pay[max(0, lo - rec2_off - 40):max(0, hi - rec2_off - 40)]) == \
                len(have_pay[max(0, lo - rec2_off - 40):max(0, hi - rec2_off - 40)]) else "missing-trailer"
        chain.append(c)
        if c is not None:
            card.used.add(c)
        plan["mapping"].append({"k": k, "chunk": c, "how": how})
        k += 1
    plan["predicted_tail_sha1"] = hashlib.sha1(tail).hexdigest()
    plan["_tail"] = tail  # used by assemble(), not saved
    return finish(plan, chain)


def backtrack(card: Card, chain: list, plan: dict, bad_k: int, S: int, log) -> bool:
    """Try alternatives for the scan clusters from bad_k back over one restart interval."""
    first_scan = S // CS + 1
    end = len(chain)
    for j in range(min(bad_k, end - 1), max(first_scan, bad_k - 12) - 1, -1):
        prev = max(x for x in chain[:j] if x is not None)
        tried = {chain[j]}
        old = chain[j:end]
        for c in old:  # release the tail so its clusters can be re-chosen in another order
            card.used.discard(c)
        for _ in range(8):
            alt = card.first_after(card.scan, prev, lambda c: c not in card.used and c not in tried)
            if alt is None:
                break
            tried.add(alt)
            # rebuild chain[j:end] greedily from alt
            new = [alt]
            card.used.add(alt)
            while len(new) < end - j:
                nxt = card.first_after(card.scan, new[-1], lambda c: c not in card.used)
                if nxt is None:
                    break
                new.append(nxt)
                card.used.add(nxt)
            data = b"".join(card.get(c) for c in chain[:j] + new)
            r = jpegmcu_bytes(data)
            ok = r["ok"] or (r.get("error", "").startswith("marker reached") and r["error_offset"] >= len(data) - 2)
            if ok:
                chain[j:end] = new
                for m, c in zip(plan["mapping"][j:end], new):
                    m["chunk"], m["how"] = c, "backtracked"
                log(f"  fixed: cluster {j} -> chunk {alt}")
                return True
            for c in new:
                card.used.discard(c)
        for c in old:
            card.used.add(c)
    return False


def finish(plan: dict, chain: list) -> dict:
    plan["chain"] = chain
    plan.setdefault("status", "complete" if all(
        m["chunk"] is not None or m["how"] == "rebuilt-exact" for m in plan["mapping"]) else "incomplete")
    plan["n_clusters"] = len(chain)
    plan["counts"] = {}
    for m in plan["mapping"]:
        plan["counts"][m["how"]] = plan["counts"].get(m["how"], 0) + 1
    return plan


def assemble(card: Card, plan: dict) -> bytes:
    out = bytearray()
    tail = plan.get("_tail")
    for m in plan["mapping"]:
        lo = m["k"] * CS
        if m["chunk"] is not None:
            out += card.get(m["chunk"])
        elif m["how"] == "rebuilt-exact" and tail is not None:
            out += tail[lo - plan["rec2_off"]:lo - plan["rec2_off"] + CS]
        else:
            out += bytes(CS)
    return bytes(out[:plan.get("file_size", len(out))])
