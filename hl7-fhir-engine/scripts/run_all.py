#!/usr/bin/env python3
"""
run_all.py - run the evaluation scripts in order, into one results folder.

  python scripts/run_all.py                          # everything except the load test
  python scripts/run_all.py --generate 500 --load    # with 500 synthetic orders per SQL source and the load test
  python scripts/run_all.py --skip-validator --skip-tx
  python scripts/run_all.py --startup 5               # also C5.2: five cold starts (restarts the stack)

Run it on a clean stack (docker compose down -v && docker compose up -d --build).

Steps (each writes into results/<UTC timestamp>/):
   1 record_environment.py        environment.json
   2 check_db_roles.py            db_roles/            (R1)
   3 stack_checks.py              stack_checks/        (roles, C1.1, C2.4, C3.1-C3.3, C5.3, C5.4, C7.2, C7.3;
                                                        C5.2 with --startup N)
   4 value_fidelity.py            value_fidelity/      (C2.2, engine Bundles and HAPI copies, with edge cases)
   5 generate_orders.py           generated/           (only with --generate N)
   6 export_bundles.py            bundles/
   7 run_hl7_validator.py         validator/           (H1, official HL7 Validator)
   8 hapi_validate_all.py         hapi_validation/     (H1, HAPI $validate)
   9 terminology_resolution.py    terminology/         (H7; --skip-tx = offline tables only)
  10 adapter_line_counts.py       h6/                  (H6)
  11 audit_fault_injection.py     fault_injection/     (audit policy)
  12 capture_docker_stats.py      docker_stats/idle    (60 s at rest)
  13 run_load_test.py             load/                (only with --load)

Steps 3 and 4 run before any generated order exists, because the per-patient searches of C3.2 expect
only the seed in HAPI. A failing step is reported and the run continues.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, log, results_dir  # noqa: E402

HERE = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--generate", type=int, default=0, help="synthetic SIL orders and LIS-B requisitions to add")
    ap.add_argument("--skip-validator", action="store_true")
    ap.add_argument("--validator-tx", default=None, help="passed to run_hl7_validator.py --tx")
    ap.add_argument("--skip-tx", action="store_true", help="terminology tables without tx.fhir.org")
    ap.add_argument("--skip-faults", action="store_true")
    ap.add_argument("--skip-stack-checks", action="store_true", help="skip steps 3 and 4")
    ap.add_argument("--startup", type=int, default=0, help="cold starts for C5.2 (restarts the stack)")
    ap.add_argument("--load", action="store_true", help="also run the load test (about 20 min)")
    ap.add_argument("--load-args", default="", help="extra arguments for run_load_test.py")
    a = ap.parse_args()

    out = results_dir()
    env = {**os.environ, "RESULTS_DIR": str(out)}
    steps = [("environment", ["record_environment.py"]), ("db roles (R1)", ["check_db_roles.py"])]
    if not a.skip_stack_checks:
        steps.append(("stack checks (C1.1, C2.4, C3, C5, C7.2, C7.3)",
                      ["stack_checks.py"] + (["--startup", str(a.startup)] if a.startup else [])))
        steps.append(("value fidelity (C2.2)", ["value_fidelity.py", "--hapi", "--edge-cases"]))
    if a.generate:
        steps.append(("generate", ["generate_orders.py", "--sil", str(a.generate), "--lisb", str(a.generate // 2),
                                   "--oru", str(max(20, a.generate // 10))]))
    # the five seed messages always, plus the generated ones when there are any
    oru_dirs = str(ROOT / "samples" / "oru") + ("," + str(out / "generated" / "oru") if a.generate else "")
    steps.append(("export Bundles", ["export_bundles.py", "--oru", oru_dirs]))
    if not a.skip_validator:
        steps.append(("HL7 Validator (H1)", ["run_hl7_validator.py"] + (["--tx", a.validator_tx] if a.validator_tx else [])))
    steps.append(("HAPI $validate (H1)", ["hapi_validate_all.py"]))
    steps.append(("terminology (H7)", ["terminology_resolution.py"] + (["--offline"] if a.skip_tx else [])))
    steps.append(("line counts (H6)", ["adapter_line_counts.py"]))
    if not a.skip_faults:
        steps.append(("audit fault injection", ["audit_fault_injection.py"]))
    steps.append(("docker stats at rest", ["capture_docker_stats.py", "--label", "idle", "--duration", "60"]))
    if a.load:
        steps.append(("load test", ["run_load_test.py"] + a.load_args.split()))

    status = []
    for name, cmd in steps:
        log(f"=== {name} ===")
        t0 = time.time()
        r = subprocess.run([sys.executable, str(HERE / cmd[0]), *cmd[1:]], env=env)
        status.append((name, r.returncode, time.time() - t0))
    lines = ["# Evaluation run", "", f"Folder: `{out}`", "", "| step | exit code | duration (s) |", "|---|---|---|"]
    lines += [f"| {n} | {c} | {d:.0f} |" for n, c, d in status]
    lines += ["", "Main tables: stack_checks/SUMMARY.md, value_fidelity/SUMMARY.md, validator/SUMMARY.md,",
              "hapi_validation/by_source.md, terminology/SUMMARY.md,",
              "h6/line_counts.md, db_roles/db_roles.md, fault_injection/fault_injection.md, load/load_results.md,",
              "docker_stats/idle_summary.md."]
    (out / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
