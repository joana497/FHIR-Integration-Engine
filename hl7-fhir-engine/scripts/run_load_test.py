#!/usr/bin/env python3
"""
run_load_test.py - load test of the engine with k6 (in Docker), with resource sampling.

  python scripts/run_load_test.py                                  # defaults below
  python scripts/run_load_test.py --scenarios engine,bundle --vus 1,5,10,20,40 --duration 60s

Before running, restart the middleware with a rate limit that the test
will not reach (the default of 300 requests per minute would turn most
requests into HTTP 429):

  RATE_LIMIT_PER_MIN=1000000 docker compose up -d node-middleware

For every (scenario, VUs) pair the script
  1. starts capture_docker_stats.py,
  2. runs k6 (constant VUs for --duration) in the stack's Docker network,
  3. stops the sampler and keeps k6's summary,
then writes load/load_results.csv/.md (latency percentiles, throughput,
error rate, and mean CPU / maximum memory of each container).

Warm-up: unless --no-warmup, every order of the source is synchronised
once first, so the terminology cache is warm and HAPI already holds the
resources (the 'sync' scenario then measures updates). The load test is
therefore a measurement of the engine, not of tx.fhir.org: its latency is
measured separately by terminology_resolution.py.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import MIDDLEWARE_URL, ROOT, http, log, login, markdown_table, require_docker, results_dir, write_csv  # noqa: E402

HERE = Path(__file__).resolve().parent


def docker_out(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout.strip()


def network_of(container: str) -> str:
    nets = json.loads(docker_out("inspect", container, "--format", "{{json .NetworkSettings.Networks}}"))
    return next(iter(nets))


def rate_limit_setting() -> int | None:
    env = docker_out("inspect", "node-middleware", "--format", "{{range .Config.Env}}{{println .}}{{end}}")
    m = re.search(r"^RATE_LIMIT_PER_MIN=(\d+)", env, re.M)
    return int(m.group(1)) if m else None


def metric(summary: dict, name: str, stat: str):
    """A statistic of the scenario sub-metric (excludes setup), else of the whole metric."""
    ms = summary.get("metrics", {})
    m = ms.get(f"{name}{{scenario:main}}") or ms.get(name, {})
    v = m.get("values", m)
    return v.get(stat)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenarios", default="engine,bundle,sync,oru")
    ap.add_argument("--vus", default="1,5,10,20")
    ap.add_argument("--duration", default="60s")
    ap.add_argument("--source", default="sil")
    ap.add_argument("--pause", type=float, default=15, help="seconds of rest between runs")
    ap.add_argument("--k6-image", default="grafana/k6:latest", help="pin a version for reproducibility")
    ap.add_argument("--no-warmup", action="store_true")
    ap.add_argument("--oru-dir", default=str(ROOT / "samples" / "oru"), help="ORU messages for the 'oru' scenario")
    a = ap.parse_args()

    require_docker()
    rl = rate_limit_setting()
    if rl is None or rl < 100000:
        sys.exit(f"node-middleware has RATE_LIMIT_PER_MIN={rl}. Restart it with\n"
                 f"  RATE_LIMIT_PER_MIN=1000000 docker compose up -d node-middleware\nand run again.")
    out = results_dir("load")
    net = network_of("node-middleware")
    k6_version = docker_out("run", "--rm", a.k6_image, "version")
    log(f"network {net}; {k6_version}")
    (out / "k6_version.txt").write_text(k6_version + "\n" + a.k6_image + "\n", encoding="utf-8")

    if not a.no_warmup:
        log(f"warm-up: synchronising every '{a.source}' order once")
        t0 = time.time()
        r = http("POST", f"{MIDDLEWARE_URL}/sync/all?source={a.source}&limit=1000", token=login("admin"), timeout=3600)
        log(f"warm-up done: {r['summary']['ok']}/{r['summary']['total']} in {time.time() - t0:.0f} s")

    oru_dir = Path(a.oru_dir).resolve()
    oru_files = sorted(oru_dir.glob("*.hl7"))
    oru_env = ",".join(f"/samples/{p.name}" for p in oru_files)
    rows = []
    for scenario in a.scenarios.split(","):
        for vus in [int(v) for v in a.vus.split(",")]:
            label = f"{scenario}_vus{vus:03d}"
            stop = out / f"{label}.stop"
            stop.unlink(missing_ok=True)
            sampler = subprocess.Popen([sys.executable, str(HERE / "capture_docker_stats.py"), "--label", label,
                                        "--interval", "1", "--until-file", str(stop)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            log(f"run {label} ({a.duration})")
            cmd = ["docker", "run", "--rm", "--network", net,
                   "-v", f"{(HERE / 'load').resolve()}:/scripts:ro",
                   "-v", f"{oru_dir}:/samples:ro",
                   "-v", f"{out.resolve()}:/out",
                   "-e", f"SCENARIO={scenario}", "-e", f"VUS={vus}", "-e", f"DURATION={a.duration}",
                   "-e", f"SOURCE={a.source}", "-e", f"ORU_FILES={oru_env}",
                   "-e", f"SUMMARY_PATH=/out/{label}.json", "-e", "K6_NO_USAGE_REPORT=true",
                   a.k6_image, "run", "--quiet", "/scripts/k6_engine.js"]
            if hasattr(os, "getuid"):  # let k6 write its summary into a folder owned by the host user
                cmd[3:3] = ["--user", f"{os.getuid()}:{os.getgid()}"]
            k = subprocess.run(cmd, capture_output=True, text=True)
            (out / f"{label}.log").write_text(k.stdout + "\n--- stderr ---\n" + k.stderr, encoding="utf-8")
            stop.write_text("stop")
            sampler.wait(timeout=60)
            stop.unlink(missing_ok=True)
            sj = out / f"{label}.json"
            if not sj.exists():
                log(f"  no k6 summary (exit {k.returncode}); see {out / (label + '.log')}")
                continue
            s = json.loads(sj.read_text(encoding="utf-8"))
            stats_csv = results_dir("docker_stats") / f"{label}_summary.csv"
            res = {}
            if stats_csv.exists():
                with open(stats_csv, encoding="utf-8") as f:
                    res = {r["container"]: r for r in csv.DictReader(f)}
            row = {
                "scenario": scenario, "vus": vus, "duration": a.duration,
                "requests": metric(s, "http_reqs", "count"),
                "throughput_rps": round(metric(s, "http_reqs", "rate") or 0, 1),
                "error_pct": round(100 * (metric(s, "http_req_failed", "rate") or metric(s, "http_req_failed", "value") or 0), 2),
                "p50_ms": round(metric(s, "http_req_duration", "med") or 0, 1),
                "p90_ms": round(metric(s, "http_req_duration", "p(90)") or 0, 1),
                "p95_ms": round(metric(s, "http_req_duration", "p(95)") or 0, 1),
                "p99_ms": round(metric(s, "http_req_duration", "p(99)") or 0, 1),
                "max_ms": round(metric(s, "http_req_duration", "max") or 0, 1),
                "http_429": metric(s, "rate_limited_429", "count") or 0,
                "http_503": metric(s, "audit_refused_503", "count") or 0,
            }
            for c, short in (("node-middleware", "node"), ("python-fhir-engine", "py"), ("hapi-fhir", "hapi"),
                             ("sil-postgres", "pg")):
                if c in res:
                    row[f"{short}_cpu_mean_pct"] = res[c]["cpu_mean_pct"]
                    row[f"{short}_mem_max_mib"] = res[c]["mem_max_mib"]
            if "ALL (sum per sample)" in res:  # the five containers together (C5.3)
                row["all_mem_max_mib"] = res["ALL (sum per sample)"]["mem_max_mib"]
            rows.append(row)
            log(f"  {row['requests']} req, {row['throughput_rps']} req/s, p95 {row['p95_ms']} ms, "
                f"errors {row['error_pct']} %")
            time.sleep(a.pause)
    cols = list(dict.fromkeys(k for r in rows for k in r))
    write_csv(out / "load_results.csv", rows, cols)
    (out / "load_results.md").write_text(markdown_table(rows, cols), encoding="utf-8")
    print(markdown_table(rows, cols))
    log(f"load test results -> {out / 'load_results.csv'}")


if __name__ == "__main__":
    main()
