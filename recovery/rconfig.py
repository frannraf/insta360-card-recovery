"""Paths, configuration and platform glue shared by every recovery tool.

Configuration is a JSON file, found as $RECOVERY_CONFIG, else ./recovery.json,
else recovery.json in the repository root.  Relative paths in it resolve
against the file's own folder.  Keys:

  work        folder for all intermediate data (index cache, chains,
              assembled files, validation output, logs).  Must be on a local
              disk: on Windows the decoder reaches it through WSL (/mnt/<drive>).
  outputs     folder that receives verified recoveries + MANIFEST.csv
              (default: <work>/../outputs/recovered)
  images      {"name": "path to an .rdr image or a raw card image", ...}
              Images are only ever opened read-only.
  utc_offset  {"name": hours}  default camera clock offset per image, used
              until the per-recording trailer timestamps are known (default 0)
  flush_cache true|false: on WSL, flush the Linux page cache every 15 s while
              long decodes run (default true).  Multi-GB decodes otherwise
              fill the VM's cache until reads fail with "Cannot allocate memory".

FFmpeg and ffprobe (6.x) run in bash: natively on Linux/macOS, through WSL on
Windows.  Everything that writes the shared MANIFEST.csv holds manifest_lock().
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from pathlib import Path

CODE = Path(__file__).resolve().parent
ON_WINDOWS = os.name == "nt"


def _load() -> dict:
    for c in (os.environ.get("RECOVERY_CONFIG"), Path.cwd() / "recovery.json", CODE.parent / "recovery.json"):
        if c and Path(c).is_file():
            p = Path(c).resolve()
            cfg = json.loads(p.read_text())
            cfg["_path"] = p
            os.environ["RECOVERY_CONFIG"] = str(p)  # child processes use the same config
            return cfg
    raise SystemExit("no recovery.json found: copy recovery.example.json to recovery.json "
                     "and edit it (or point RECOVERY_CONFIG at your config file)")


def _resolve(base: Path, v: str) -> Path:
    p = Path(v)
    return p if p.is_absolute() else (base / p).resolve()


CFG = _load()
_BASE = CFG["_path"].parent
WORK = _resolve(_BASE, CFG["work"])
OUT = _resolve(_BASE, CFG["outputs"]) if CFG.get("outputs") else WORK.parent / "outputs" / "recovered"
MANIFEST = OUT / "MANIFEST.csv"
IMAGES = {k: str(_resolve(_BASE, v)) for k, v in CFG.get("images", {}).items()}
UTC_OFFSET = {k: float(v) for k, v in CFG.get("utc_offset", {}).items()}
FLUSH_CACHE = bool(CFG.get("flush_cache", True))


def sh_path(p: Path | str) -> str:
    """The path as the bash that runs FFmpeg sees it."""
    p = Path(p).resolve()
    if not ON_WINDOWS:
        return str(p)
    s = str(p)
    if s.startswith("\\\\"):
        raise SystemExit(f"{s} is on a network share, which WSL cannot reach as /mnt/<drive>: "
                         "keep 'work' on a local disk (images may stay on the share)")
    return f"/mnt/{s[0].lower()}{s[2:].replace(chr(92), '/')}"


SH_WORK = sh_path(WORK)
SH_CODE = sh_path(CODE)


def sh_argv(cmd: str, root: bool = False) -> list[str]:
    """argv that runs a bash command line where FFmpeg lives."""
    if ON_WINDOWS:
        return ["wsl.exe"] + (["-u", "root"] if root else []) + ["bash", "-lc", cmd]
    return ["bash", "-lc", cmd]


def sh_run(cmd: str, **kw) -> subprocess.CompletedProcess:
    """Run a bash command line from the work folder (relative paths = work)."""
    return subprocess.run(sh_argv(f"cd '{SH_WORK}' && {cmd}"), **kw)


def sh_script(name: str) -> str:
    """Quoted bash path of one of this package's shell helpers."""
    return f"'{SH_CODE}/{name}'"


@contextlib.contextmanager
def _file_lock(lock: Path, timeout: float, stale: float):
    lock.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.time() - t0 > timeout:
                raise SystemExit(f"{lock} is held by another process")
            time.sleep(0.2)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def manifest_lock(timeout: float = 900):
    """Exclusive access to MANIFEST.csv (append, rewrite, rename)."""
    return _file_lock(OUT / "MANIFEST.csv.lock", timeout, stale=1800)


def run_lock_path() -> Path:
    return WORK / "run.lock"


WORK_DIRS = ("rdr-index-cache", "scan-results", "scan-analysis", "chains", "assembled", "validation",
             "trials", "review", "inst", "logs", "tmp")


def ensure_work_dirs() -> None:
    for d in WORK_DIRS:
        (WORK / d).mkdir(parents=True, exist_ok=True)
