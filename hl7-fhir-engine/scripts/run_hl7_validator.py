#!/usr/bin/env python3
"""
run_hl7_validator.py - validate the exported Bundles with the official HL7 FHIR Validator (H1).

The validator (validator_cli.jar, maintained by HL7 in hapifhir/org.hl7.fhir.core)
runs in a Java container, so only Docker is needed:

  python scripts/run_hl7_validator.py                       # results/<run>/bundles
  python scripts/run_hl7_validator.py --bundles results/<run>/bundles --tx n/a
  python scripts/run_hl7_validator.py --forbid-example-urls     # the validator's default for example.org

Every local URI of the demonstration lies under example.org on purpose (no
institution's domain is used). The validator treats example.org URIs as errors
unless -allow-example-urls is given, so this script passes it by default and
records the setting; --forbid-example-urls reproduces the default behaviour.

What it does
  1. downloads validator_cli.jar once into tools/validator/ (or uses --jar)
     and records its SHA-256 and the version line it prints;
  2. validates every Bundle against FHIR R4 (4.0.1), one run per source
     folder, with the terminology server given by --tx (default:
     tx.fhir.org, the validator's own default; 'n/a' disables terminology);
  3. writes validator/<source>.json (OperationOutcome per file) and the raw
     log, then calls summarise_validator.py to build the tables.

Note: 'HAPI accepted the Bundle' is not the same as 'the Bundle is valid';
this script produces the second.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, log, require_docker, results_dir, sha256, write_json  # noqa: E402

JAR_URL = "https://github.com/hapifhir/org.hl7.fhir.core/releases/latest/download/validator_cli.jar"
JAVA_IMAGE = os.environ.get("VALIDATOR_JAVA_IMAGE", "eclipse-temurin:21-jre")


def ensure_jar(path: Path) -> Path:
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    log(f"downloading {JAR_URL}")
    urllib.request.urlretrieve(JAR_URL, path)  # noqa: S310 - fixed https URL
    return path


def docker_path(p: Path) -> str:
    return str(p.resolve())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundles", default=None, help="folder written by export_bundles.py")
    ap.add_argument("--jar", default=str(ROOT / "tools" / "validator" / "validator_cli.jar"))
    ap.add_argument("--tx", default=None, help="terminology server for the validator; 'n/a' to disable")
    ap.add_argument("--memory", default="4g", help="Java heap (default 4g)")
    ap.add_argument("--batch", type=int, default=150, help="Bundles per validator run (default 150)")
    ap.add_argument("--extra", default="", help="extra validator arguments, e.g. '-level warnings'")
    ap.add_argument("--forbid-example-urls", action="store_true",
                    help="do not pass -allow-example-urls (example.org URIs are then reported as errors)")
    a = ap.parse_args()

    require_docker()
    bundles = Path(a.bundles) if a.bundles else results_dir("bundles")
    sources = sorted(d for d in bundles.iterdir() if d.is_dir() and any(d.glob("*.json")))
    if not sources:
        sys.exit(f"no Bundles under {bundles}; run export_bundles.py first")
    out = results_dir("validator")
    jar = ensure_jar(Path(a.jar))
    meta = {"jar": str(jar), "jar_sha256": sha256(jar), "jar_url": JAR_URL, "java_image": JAVA_IMAGE,
            "fhir_version": "4.0.1", "tx": a.tx or "validator default (tx.fhir.org)",
            "allow_example_urls": not a.forbid_example_urls, "runs": {}}
    cache = ROOT / "tools" / "validator" / "fhir-cache"   # package cache, reused between runs
    cache.mkdir(parents=True, exist_ok=True)

    for src in sources:
        files = sorted(src.glob("*.json"))
        # Batches keep the command line short (Windows limits it to ~32k characters).
        batches = [files[i:i + a.batch] for i in range(0, len(files), a.batch)]
        log(f"validating {len(files)} Bundles of '{src.name}' in {len(batches)} run(s)")
        meta["runs"][src.name] = []
        for k, batch in enumerate(batches, start=1):
            name = f"{src.name}_{k:03d}"
            cmd = ["docker", "run", "--rm",
                   "-v", f"{docker_path(jar.parent)}:/validator",
                   "-v", f"{docker_path(src)}:/in:ro",
                   "-v", f"{docker_path(out)}:/out",
                   "-v", f"{docker_path(cache)}:/root/.fhir",
                   JAVA_IMAGE, "java", f"-Xmx{a.memory}", "-jar", f"/validator/{jar.name}"]
            cmd += [f"/in/{f.name}" for f in batch]
            cmd += ["-version", "4.0.1", "-output", f"/out/{name}.json"]
            if a.tx:
                cmd += ["-tx", a.tx]
            if not a.forbid_example_urls:
                cmd += ["-allow-example-urls=true"]
            if a.extra:
                cmd += a.extra.split()
            r = subprocess.run(cmd, capture_output=True, text=True)
            (out / f"{name}.log").write_text(r.stdout + "\n--- stderr ---\n" + r.stderr, encoding="utf-8")
            head = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()][:3]
            version = next((ln for ln in head if "Validat" in ln), head[0] if head else "")
            meta["runs"][src.name].append({"batch": name, "files": [f.name for f in batch],
                                           "exit_code": r.returncode, "version_line": version})
            # The validator exits non-zero when it finds errors: that is a result, not a failure.
            if (out / f"{name}.json").exists():
                log(f"  {name}: done (exit {r.returncode}) {version}")
            else:
                log(f"  {name}: NO OUTPUT (exit {r.returncode}); see {out / (name + '.log')}")
    write_json(out / "validator_run.json", meta)
    subprocess.run([sys.executable, str(Path(__file__).with_name("summarise_validator.py")), str(out)], check=False)


if __name__ == "__main__":
    main()
