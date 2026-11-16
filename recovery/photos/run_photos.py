#!/usr/bin/env python3
"""Recover Insta360 360-degree photos (.insp) from a card image. Resumable; read-only on the image.

Stages (default: all, in this order):
  scan     read every free cluster once and record photo features (scan_free.py)
  walk     rebuild each photo's cluster chain (walk_photo.py), in the order the photos were
           taken, and assemble the complete ones into work/photos/assembled/<image>/
  check    full checks of every assembled photo (check_photo.py)
  retry    re-walk the photos that failed, with the clusters of verified photos off limits,
           and check them again
  review   work/photos/review-<image>.html: both previews of every photo, for the visual check
  publish  hard-link every verified photo into <outputs>/../photos/<yyyy-mm>/ and update PHOTOS.csv

Usage:
  python recovery/photos/run_photos.py --image card1
  python recovery/photos/run_photos.py --image card1 --stages review,publish

Video chains (work/chains/<image>, from run_recovery.py) are used to skip clusters that
belong to recovered videos. Without them the whole card is scanned, which also works.
After looking at the review page, list the photos you checked in
work/photos/visual-ok-<image>.txt (one name per line, or a single "*"), and run the
publish stage again: PHOTOS.csv records it.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_photo  # noqa: E402
import walk_photo as wp  # noqa: E402
from photo_common import PH, PHOTO_OUT, cache_source, ensure_dirs, ensure_jpegmcu, load_scan  # noqa: E402

STAGES = ["scan", "walk", "check", "retry", "review", "publish"]


def photo_name(st: dict, image: str, chunk: int) -> str:
    name = st.get("name") or f"unnamed_{image}_{chunk}.insp"
    return name + "p" if name.endswith(".ins") else name


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest().upper()


def verified(image: str) -> dict[str, dict]:
    """name -> chain plan, for photos whose assembled file passed every check."""
    out = {}
    for cf in (PH / "chains" / image).glob("*.json"):
        plan = json.loads(cf.read_text())
        chk = PH / "validation" / image / f"{plan['name']}.check.json"
        if plan.get("status") == "complete" and chk.exists() and json.loads(chk.read_text()).get("structure_ok"):
            out[plan["name"]] = plan
    return out


def walk_all(image: str, only_failed: bool) -> list[str]:
    scan = load_scan(image)
    feats = scan["features"]
    card = wp.Card(feats, cache_source(image, feats), scan.get("tiny", []))
    keep = verified(image) if only_failed else {}
    for plan in keep.values():
        card.used.update(c for c in plan["chain"] if c is not None)
    # the camera allocates first-fit in time order: an earlier photo owns the clusters a
    # later photo's walk would otherwise run into, so walk in the order they were taken
    starts = sorted((f for f in feats if "start" in f), key=lambda f: (f["start"].get("name") or "~", f["chunk"]))
    chains, out = PH / "chains" / image, PH / "assembled" / image
    chains.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    walked, seen = [], set()
    for f in starts:
        st = f["start"]
        name = photo_name(st, image, f["chunk"])
        if name in seen:
            name = name.replace(".insp", f".dup-{f['chunk']}.insp")
        seen.add(name)
        if name in keep:
            continue
        if st["kind"] != "jpeg" or not name.endswith(".insp"):
            print(f"{name}: not walked ({st['kind']} start; only .insp photos are supported)", flush=True)
            continue
        t = time.time()
        try:
            plan = wp.walk(card, f["chunk"], log=lambda s: print(s, flush=True))
        except Exception as e:  # noqa: BLE001
            plan = {"status": "error", "error": repr(e), "trace": traceback.format_exc()[-800:], "mapping": [], "chain": []}
        plan.update(name=name, image=image, seconds=round(time.time() - t, 1))
        (out / name).unlink(missing_ok=True)
        if plan["status"] == "complete":
            data = wp.assemble(card, plan)
            (out / name).write_bytes(data)
            plan["sha256"] = hashlib.sha256(data).hexdigest().upper()
        plan.pop("_tail", None)
        (chains / f"{name}.json").write_text(json.dumps(plan, indent=1))
        print(json.dumps({"name": name, "status": plan["status"], "clusters": plan.get("n_clusters"),
                          "how": plan.get("counts"), "seconds": plan["seconds"]}), flush=True)
        walked.append(name)
    return walked


def check_all(image: str, names: list[str] | None = None) -> None:
    ensure_jpegmcu()
    vd = PH / "validation" / image
    for p in sorted((PH / "assembled" / image).glob("*.insp")):
        if names is not None and p.name not in names:
            continue
        rep_path = vd / f"{p.name}.check.json"
        if names is None and rep_path.exists() and rep_path.stat().st_mtime > p.stat().st_mtime:
            continue  # already checked
        rep = check_photo.check(p, vd)
        print(f"{p.name}: {'all checks OK' if rep['structure_ok'] else 'FAILED'}", flush=True)


def review(image: str) -> Path:
    vd = PH / "validation" / image
    rows = []
    for r in sorted(vd.glob("*.check.json")):
        rep = json.loads(r.read_text())
        n = html.escape(rep["file"])
        ok = rep["structure_ok"]
        rows.append(f"<section><h2>{n} <span class={'ok' if ok else 'bad'}>{'checks OK' if ok else 'checks FAILED'}"
                    f"</span></h2><img src='validation/{image}/{n}.preview.jpg' alt=''>"
                    f"<img src='validation/{image}/{n}.stitched.jpg' alt=''></section>")
    page = PH / f"review-{image}.html"
    page.write_text(f"""<!doctype html><meta charset=utf-8><title>Photos: {html.escape(image)}</title>
<style>body{{font:14px system-ui;margin:16px;background:#fafafa}}section{{margin:0 0 18px}}
img{{width:49%;margin-right:1%;background:#000}}h2{{font-size:14px;margin:4px 0}}.ok{{color:#170}}.bad{{color:#b00}}</style>
<h1>{len(rows)} photos from {html.escape(image)}</h1>
<p>Left: the photo as stored (two fisheye circles, 1/8 scale). Right: the stitched preview the
camera stores inside the file. Look for grey, green or smeared areas, or a picture that changes
partway down. Then list the photos you checked in visual-ok-{html.escape(image)}.txt (or a single *).</p>
{''.join(rows)}""", encoding="utf-8")
    print(f"review page: {page}")
    return page


def publish(image: str) -> None:
    ok_file = PH / f"visual-ok-{image}.txt"
    looked = set(ok_file.read_text().split()) if ok_file.exists() else set()
    rows = []
    for name, plan in sorted(verified(image).items()):
        src = PH / "assembled" / image / name
        h = sha256(src)
        if h != plan.get("sha256"):
            raise SystemExit(f"{name}: assembled file does not match its chain's SHA-256")
        rep = json.loads((PH / "validation" / image / f"{name}.check.json").read_text())
        ymd, hms = name[4:12], name[13:19]
        month = f"{ymd[:4]}-{ymd[4:6]}" if ymd.isdigit() else "unknown-date"
        dst = PHOTO_OUT / month / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            if sha256(dst) != h:
                raise SystemExit(f"refusing to overwrite {dst} (different content)")
        else:
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)
        rows.append({"file": f"{month}/{name}", "card": image,
                     "taken": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]} {hms[:2]}:{hms[2:4]}:{hms[4:]}" if ymd.isdigit() else "",
                     "bytes": src.stat().st_size,
                     "image": f"{rep['D_mcu'].get('width')}x{rep['D_mcu'].get('height')}",
                     "mcus": rep["D_mcu"].get("mcus"), "clusters": plan.get("n_clusters"),
                     "checks": "header, scan, MCU-exact decode, trailer chain, preview copies, FFmpeg: all OK",
                     "visual_check": "looked at" if ("*" in looked or name in looked) else "not recorded",
                     "sha256": h, "chain": f"work/photos/chains/{image}/{name}.json"})
    cols = ["file", "card", "taken", "bytes", "image", "mcus", "clusters", "checks", "visual_check", "sha256", "chain"]
    man = PHOTO_OUT / "PHOTOS.csv"
    old = []
    if man.exists():
        with open(man, newline="", encoding="utf-8") as fh:
            old = [r for r in csv.DictReader(fh) if r["card"] != image]
    PHOTO_OUT.mkdir(parents=True, exist_ok=True)
    with open(man, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(old + rows)
    print(f"published {len(rows)} verified photos from {image} to {PHOTO_OUT} (PHOTOS.csv)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", required=True)
    ap.add_argument("--stages", default=",".join(STAGES))
    ap.add_argument("--chains", default=None, help="video chain set(s) for the scan (default: <image>[,<image>-patched])")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    ensure_dirs()
    stages = a.stages.split(",")
    for s in stages:
        if s not in STAGES:
            raise SystemExit(f"unknown stage {s}; stages: {', '.join(STAGES)}")
    if "scan" in stages:
        cmd = [sys.executable, str(HERE / "scan_free.py"), "--image", a.image, "--workers", str(a.workers)]
        if a.chains:
            cmd += ["--chains", a.chains]
        if subprocess.run(cmd).returncode:
            raise SystemExit("scan failed (it resumes where it stopped when run again)")
    if "walk" in stages:
        walk_all(a.image, only_failed=False)
    if "check" in stages:
        check_all(a.image)
    if "retry" in stages:
        again = walk_all(a.image, only_failed=True)
        if again:
            check_all(a.image, again)
    if "review" in stages:
        review(a.image)
    if "publish" in stages:
        publish(a.image)
    v = verified(a.image)
    names = sorted(p.name[:-5] for p in (PH / "chains" / a.image).glob("*.json"))
    print(json.dumps({"image": a.image, "photos_found": len(names), "verified": len(v),
                      "not_verified": [n for n in names if n not in v]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
