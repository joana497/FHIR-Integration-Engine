#!/usr/bin/env python3
"""
record_environment.py - record the conditions of a measurement run.

Writes environment.json: host (OS, CPU count, memory reported by Docker),
Docker and Compose versions, the exact image digest of every running
container of the stack, and SHA-256 of the engine's source files. Report
these values in the thesis next to every figure, so that the
measurements can be reproduced or at least compared.
"""
from __future__ import annotations

import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, log, results_dir, sha256, write_json  # noqa: E402

CONTAINERS = ["sil-postgres", "hapi-postgres", "hapi-fhir", "python-fhir-engine", "node-middleware"]
SOURCES = ["python-fhir-engine", "node-middleware/src", "node-middleware/public", "postgres", "catalogue",
           "docker-compose.yml"]


def run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"unavailable ({exc})"


def main() -> None:
    out = results_dir()
    try:
        info = json.loads(run(["docker", "info", "--format", "{{json .}}"]))
    except json.JSONDecodeError:
        info = {}
    env = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "host": {"platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version()},
        "docker": {
            "server_version": info.get("ServerVersion"),
            "os": info.get("OperatingSystem"),
            "kernel": info.get("KernelVersion"),
            "cpus_available_to_docker": info.get("NCPU"),
            "memory_available_to_docker_bytes": info.get("MemTotal"),
            "compose_version": run(["docker", "compose", "version", "--short"]),
        },
        "containers": {},
        "source_sha256": {},
    }
    for c in CONTAINERS:
        image = run(["docker", "inspect", "-f", "{{.Config.Image}}", c])
        image_id = run(["docker", "inspect", "-f", "{{.Image}}", c])
        digests = run(["docker", "image", "inspect", "-f", "{{json .RepoDigests}}", image_id])
        env["containers"][c] = {"image": image, "image_id": image_id, "repo_digests": digests,
                                "limits": run(["docker", "inspect", "-f",
                                               "cpus={{.HostConfig.NanoCpus}} mem={{.HostConfig.Memory}}", c])}
    for rel in SOURCES:
        p = ROOT / rel
        files = [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file()
                                               and "__pycache__" not in x.parts and "node_modules" not in x.parts)
        for f in files:
            env["source_sha256"][str(f.relative_to(ROOT)).replace("\\", "/")] = sha256(f)
    write_json(out / "environment.json", env)
    log(f"environment recorded in {out / 'environment.json'}")
    hapi = env["containers"].get("hapi-fhir", {})
    log(f"HAPI image: {hapi.get('image')} {hapi.get('repo_digests')}")
    log("Tip: pin it for later runs with HAPI_IMAGE=<repo>@<digest> docker compose up -d")


if __name__ == "__main__":
    main()
