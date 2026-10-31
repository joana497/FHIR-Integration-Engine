#!/usr/bin/env python3
"""
hapi_validate_all.py - run HAPI's own $validate on every exported Bundle.

  python scripts/hapi_validate_all.py [--bundles results/<run>/bundles]

Gives the second column of the H1 comparison: what the server that
stores the data reports, next to the official HL7 Validator. Writes
hapi_validation/issues.csv and hapi_validation/by_source.csv (+ .md).
Nothing is stored: $validate does not persist the resource.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path
import json

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import HAPI_URL, HttpError, http, log, markdown_table, results_dir, wait_for, write_csv  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundles", default=None)
    a = ap.parse_args()
    bundles = Path(a.bundles) if a.bundles else results_dir("bundles")
    out = results_dir("hapi_validation")
    wait_for(f"{HAPI_URL}/metadata", label="HAPI FHIR")
    issues, files = [], defaultdict(set)
    for src in sorted(d for d in bundles.iterdir() if d.is_dir()):
        paths = sorted(src.glob("*.json"))
        log(f"{src.name}: {len(paths)} Bundles")
        for p in paths:
            files[src.name].add(p.name)
            body = json.loads(p.read_text(encoding="utf-8"))
            try:
                oo = http("POST", f"{HAPI_URL}/Bundle/$validate", body,
                          headers={"Content-Type": "application/fhir+json"}, timeout=300)
            except HttpError as e:  # HAPI answers 412 with an OperationOutcome when invalid
                oo = e.body if isinstance(e.body, dict) else {"issue": [{"severity": "fatal", "diagnostics": str(e)}]}
            for i in oo.get("issue", []):
                msg = i.get("diagnostics") or (i.get("details") or {}).get("text") or ""
                if i.get("severity") == "information" and msg.lower().startswith("no issues"):
                    continue
                issues.append({"source": src.name, "file": p.name, "severity": i.get("severity"),
                               "code": i.get("code"), "location": (i.get("location") or [""])[0], "message": msg})
    write_csv(out / "issues.csv", issues, ["source", "file", "severity", "code", "location", "message"])
    rows = []
    for s in sorted(files):
        si = [i for i in issues if i["source"] == s]
        sev = Counter(i["severity"] for i in si)
        bad = {i["file"] for i in si if i["severity"] in ("error", "fatal")}
        rows.append({"source": s, "bundles": len(files[s]), "bundles_with_errors": len(bad),
                     "fatal": sev["fatal"], "error": sev["error"], "warning": sev["warning"],
                     "information": sev["information"]})
    write_csv(out / "by_source.csv", rows)
    (out / "by_source.md").write_text(markdown_table(rows), encoding="utf-8")
    print(markdown_table(rows))
    log(f"HAPI $validate: {len(issues)} issues -> {out}")


if __name__ == "__main__":
    main()
