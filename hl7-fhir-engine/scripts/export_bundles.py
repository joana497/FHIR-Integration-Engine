#!/usr/bin/env python3
"""
export_bundles.py - save the FHIR Bundles the engine produces, one file per order.

Reads every order of every database source from the Python engine (which
never writes anywhere) and transforms the ORU^R01 sample messages; writes
  bundles/<source>/<order>.json
  bundles/index.csv          (file, source, order, resource counts)

These files are the input of run_hl7_validator.py, hapi_validate_all.py
and terminology_resolution.py, so all three evaluate exactly the same
Bundles.

  python scripts/export_bundles.py                 # all orders, samples/oru
  python scripts/export_bundles.py --limit 25 --oru samples/oru,results/<run>/generated/oru
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ENGINE_URL, ROOT, http, log, results_dir, wait_for, write_csv, write_json  # noqa: E402

SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=5000, help="maximum orders per source")
    ap.add_argument("--sources", default="", help="comma-separated database sources (default: all)")
    ap.add_argument("--oru", default=str(ROOT / "samples" / "oru"),
                    help="folder(s) of .hl7 files, comma-separated ('' to skip)")
    a = ap.parse_args()

    wait_for(f"{ENGINE_URL}/health", label="python-fhir-engine")
    out = results_dir("bundles")
    sources = [s["name"] for s in http("GET", f"{ENGINE_URL}/sources")["database"]]
    if a.sources:
        sources = [s for s in sources if s in a.sources.split(",")]
    index = []
    for src in sources:
        ids = http("GET", f"{ENGINE_URL}/sources/{src}/orders?limit={a.limit}")["orders"]
        log(f"{src}: {len(ids)} orders")
        for oid in ids:
            b = http("GET", f"{ENGINE_URL}/fhir/bundle/{src}/order/{oid}")
            f = out / src / f"{SAFE.sub('_', oid)}.json"
            write_json(f, b)
            c = Counter(e["resource"]["resourceType"] for e in b.get("entry", []))
            index.append({"file": f"{src}/{f.name}", "source": src, "order": oid, "entries": sum(c.values()), **c})
    if a.oru:
        files = [f for d in a.oru.split(",") if d.strip() for f in sorted(Path(d.strip()).glob("*.hl7"))]
        for p in files:
            er7 = re.sub(r"\r\n|\n|\r", "\r", p.read_text(encoding="utf-8").strip()) + "\r"
            r = http("POST", f"{ENGINE_URL}/v2/oru/transform", er7,
                     headers={"Content-Type": "x-application/hl7-v2+er7"})
            b = r["bundle"]
            f = out / "oru" / f"{p.stem}.json"
            write_json(f, b)
            c = Counter(e["resource"]["resourceType"] for e in b.get("entry", []))
            index.append({"file": f"oru/{f.name}", "source": "oru", "order": ",".join(r.get("orders", [])),
                          "entries": sum(c.values()), **c})
        log(f"oru: {len(files)} messages")
    cols = ["file", "source", "order", "entries", "Patient", "ServiceRequest", "ImagingStudy", "DiagnosticReport",
            "Observation"]
    write_csv(out / "index.csv", index, cols)
    totals = Counter()
    for r in index:
        for k in cols[4:]:
            totals[(r["source"], k)] += r.get(k, 0)
    summary = [{"source": s, **{k: totals[(s, k)] for k in cols[4:]},
                "bundles": sum(1 for r in index if r["source"] == s)} for s in dict.fromkeys(r["source"] for r in index)]
    write_csv(out / "summary.csv", summary)
    print()
    for r in summary:
        print(r)
    log(f"{len(index)} Bundles written to {out}")


if __name__ == "__main__":
    main()
