#!/usr/bin/env python3
"""
capture_docker_stats.py - sample CPU and memory of the stack's containers.

  python scripts/capture_docker_stats.py --duration 300 --label idle
  python scripts/capture_docker_stats.py --until-file stop.flag --label load   # stops when the file appears
  python scripts/capture_docker_stats.py --summarise results/<run>/docker_stats/load.csv

Samples `docker stats --no-stream` every --interval seconds (each call
itself takes about 1-2 s, so the effective interval is a little longer)
and writes docker_stats/<label>.csv; on exit it writes
docker_stats/<label>_summary.csv/.md with mean, p95 and maximum CPU (%)
and memory (MiB) per container, plus a row "ALL (sum per sample)" for the
stack as a whole (its mem_max_mib is the peak combined memory of C5.3).

CPU % is relative to one core (200 % = two cores busy), as Docker reports it.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import log, markdown_table, require_docker, results_dir, write_csv  # noqa: E402

CONTAINERS = ["sil-postgres", "hapi-postgres", "hapi-fhir", "python-fhir-engine", "node-middleware"]
UNITS = {"b": 1, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4,
         "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4}


def to_bytes(s: str) -> float:
    m = re.match(r"\s*([\d.]+)\s*([a-zA-Z]+)", s or "")
    return float(m.group(1)) * UNITS.get(m.group(2).lower(), 1) if m else 0.0


def sample(containers):
    r = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", *containers],
                       capture_output=True, text=True)
    ts = datetime.now(timezone.utc).isoformat()
    rows = []
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        used, limit = (d.get("MemUsage", "0B / 0B").split("/") + ["0B"])[:2]
        net_in, net_out = (d.get("NetIO", "0B / 0B").split("/") + ["0B"])[:2]
        blk_in, blk_out = (d.get("BlockIO", "0B / 0B").split("/") + ["0B"])[:2]
        rows.append({"timestamp": ts, "container": d.get("Name"),
                     "cpu_pct": float(d.get("CPUPerc", "0%").rstrip("%") or 0),
                     "mem_mib": round(to_bytes(used) / 1024 ** 2, 1),
                     "mem_limit_mib": round(to_bytes(limit) / 1024 ** 2, 1),
                     "mem_pct": float(d.get("MemPerc", "0%").rstrip("%") or 0),
                     "net_in_mb": round(to_bytes(net_in) / 1e6, 2), "net_out_mb": round(to_bytes(net_out) / 1e6, 2),
                     "block_in_mb": round(to_bytes(blk_in) / 1e6, 2), "block_out_mb": round(to_bytes(blk_out) / 1e6, 2),
                     "pids": d.get("PIDs")})
    return rows


def p95(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))] if xs else 0


def summarise(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = list(csv.DictReader(f))
    out = []
    for c in sorted({r["container"] for r in data}):
        cpu = [float(r["cpu_pct"]) for r in data if r["container"] == c]
        mem = [float(r["mem_mib"]) for r in data if r["container"] == c]
        out.append({"container": c, "samples": len(cpu),
                    "cpu_mean_pct": round(statistics.mean(cpu), 1), "cpu_p95_pct": round(p95(cpu), 1),
                    "cpu_max_pct": round(max(cpu), 1),
                    "mem_mean_mib": round(statistics.mean(mem), 1), "mem_max_mib": round(max(mem), 1)})
    # The stack as a whole: CPU and memory summed over the containers of each sample, then
    # mean, p95 and maximum of those sums (the maximum is the peak combined memory, C5.3).
    per_ts: dict[str, list[float]] = {}
    for r in data:
        s = per_ts.setdefault(r["timestamp"], [0.0, 0.0])
        s[0] += float(r["cpu_pct"])
        s[1] += float(r["mem_mib"])
    if per_ts:
        cpu = [v[0] for v in per_ts.values()]
        mem = [v[1] for v in per_ts.values()]
        out.append({"container": "ALL (sum per sample)", "samples": len(cpu),
                    "cpu_mean_pct": round(statistics.mean(cpu), 1), "cpu_p95_pct": round(p95(cpu), 1),
                    "cpu_max_pct": round(max(cpu), 1),
                    "mem_mean_mib": round(statistics.mean(mem), 1), "mem_max_mib": round(max(mem), 1)})
    stem = path.with_suffix("")
    write_csv(Path(f"{stem}_summary.csv"), out)
    Path(f"{stem}_summary.md").write_text(markdown_table(out), encoding="utf-8")
    print(markdown_table(out))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default="capture")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--duration", type=float, default=0, help="seconds (0 = until Ctrl-C or --until-file)")
    ap.add_argument("--until-file", default=None, help="stop when this file exists")
    ap.add_argument("--containers", default=",".join(CONTAINERS))
    ap.add_argument("--summarise", default=None, help="only summarise an existing CSV")
    a = ap.parse_args()
    if a.summarise:
        summarise(Path(a.summarise))
        return
    require_docker()
    out = results_dir("docker_stats") / f"{a.label}.csv"
    containers = a.containers.split(",")
    cols = ["timestamp", "container", "cpu_pct", "mem_mib", "mem_limit_mib", "mem_pct", "net_in_mb", "net_out_mb",
            "block_in_mb", "block_out_mb", "pids"]
    log(f"sampling {', '.join(containers)} every {a.interval} s -> {out}")
    t0 = time.time()
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        try:
            while True:
                for r in sample(containers):
                    w.writerow(r)
                f.flush()
                if a.duration and time.time() - t0 >= a.duration:
                    break
                if a.until_file and Path(a.until_file).exists():
                    break
                time.sleep(a.interval)
        except KeyboardInterrupt:
            pass
    summarise(out)


if __name__ == "__main__":
    main()
