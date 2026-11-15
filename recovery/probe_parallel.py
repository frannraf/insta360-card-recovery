#!/usr/bin/env python3
"""probe_parallel.py <chains> <label> <lo> <hi> <procs>: run probe_lost.py over
<procs> equal slices of [lo, hi] at once and merge the hits (read-only)."""
import ast
import subprocess
import sys
from pathlib import Path

chains, label, lo, hi, procs = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
here = Path(__file__).resolve().parent
step = (hi - lo + 1 + procs - 1) // procs
jobs = []
for i in range(procs):
    a, b = lo + i * step, min(hi, lo + (i + 1) * step - 1)
    jobs.append(subprocess.Popen([sys.executable, "-I", "-u", str(here / "probe_lost.py"), chains, label,
                                  "--lo", str(a), "--hi", str(b)], cwd=here,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
hits, head, failed = [], None, 0
for j in jobs:
    out, _ = j.communicate()
    lines = out.strip().splitlines()
    if j.returncode != 0:
        failed += 1
        print("slice failed:", lines[-1][:200] if lines else "?")
    for line in lines:
        if line.startswith("{") and head is None:
            head = line
        elif line.startswith("("):
            hits.append(ast.literal_eval(line))
hits.sort(key=lambda h: -h[1])
print(head)
for h in hits:
    print(h)
print("hits", len(hits), "failed slices", failed)
