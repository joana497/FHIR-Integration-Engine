#!/usr/bin/env python3
"""
terminology_resolution.py - H7: how much of the produced data carries standard codes,
and are those codes valid?

  python scripts/terminology_resolution.py [--bundles results/<run>/bundles]
         [--tx https://tx.fhir.org/r4] [--rate 1.0] [--offline]

Three tables, all computed from the exported Bundles:

  1. Coverage   per source and element: share of result Observations whose
                code carries LOINC, of valueQuantity with a UCUM code, of
                coded values with SNOMED CT, of ServiceRequests with SNOMED CT.
  2. ConceptMap per local code system: distinct local codes seen in the data
                and how many have an automatic standard target
                (equivalence 'equivalent' or 'equal') in the ConceptMap.
  3. Validity   per standard code system: every distinct (system, code) is
                checked once with CodeSystem/$validate-code on the
                terminology server (default tx.fhir.org, the public HL7
                server). Requests are spaced by 1/--rate seconds and cached
                in tools/tx-cache.json, so a re-run sends nothing already
                checked. No display is sent, only system and code.

Local systems (http://example.org/...) cannot be validated by any public
server and are reported as 'local'.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, HttpError, http, log, markdown_table, results_dir, write_csv, write_json  # noqa: E402

LOINC, SNOMED, UCUM = "http://loinc.org", "http://snomed.info/sct", "http://unitsofmeasure.org"
LOCAL_PREFIX = "http://example.org/"
UA = {"User-Agent": "hl7-fhir-engine-dissertation-measurement/1.0"}
TAG_RE = re.compile(r"\s*\([^()]*\)\s*$")


def walk(node, path, out):
    """Collect every Coding and coded Quantity with the element path it sits on."""
    if isinstance(node, dict):
        if "system" in node and "code" in node and isinstance(node.get("code"), str):
            out.append((path, node))
        for k, v in node.items():
            if k in ("text", "div", "extension"):
                continue
            walk(v, f"{path}.{k}", out)
    elif isinstance(node, list):
        for v in node:
            walk(v, path, out)


def has(codings, system):
    return any(c.get("system") == system for c in codings or [])


def coverage(resources):
    """resources: list of (source, resource). Returns coverage rows."""
    acc = defaultdict(lambda: [0, 0])  # (source, metric) -> [numerator, denominator]

    def add(src, metric, ok):
        for s in (src, "ALL"):
            acc[(s, metric)][1] += 1
            acc[(s, metric)][0] += 1 if ok else 0

    for src, r in resources:
        rt = r.get("resourceType")
        if rt == "Observation":
            codings = (r.get("code") or {}).get("coding", [])
            is_result = any(k.startswith("value") for k in r) or "dataAbsentReason" in r
            if is_result:
                add(src, "Observation (result) .code has LOINC", has(codings, LOINC))
            elif r.get("hasMember"):
                add(src, "Observation (ordered test) .code has SNOMED CT or LOINC",
                    has(codings, SNOMED) or has(codings, LOINC))
            if "valueQuantity" in r:
                q = r["valueQuantity"]
                add(src, "valueQuantity has UCUM system+code", q.get("system") == UCUM and bool(q.get("code")))
            if "valueCodeableConcept" in r:
                add(src, "valueCodeableConcept has SNOMED CT", has(r["valueCodeableConcept"].get("coding"), SNOMED))
            if r.get("interpretation"):
                add(src, "interpretation coded (v3)", any(c.get("system", "").startswith("http://terminology.hl7.org/")
                                                         for i in r["interpretation"] for c in i.get("coding", [])))
        elif rt == "ServiceRequest":
            add(src, "ServiceRequest.code has LOINC (order level)", has((r.get("code") or {}).get("coding"), LOINC))
            for d in r.get("orderDetail", []):
                add(src, "ServiceRequest.orderDetail (test line) has SNOMED CT", has(d.get("coding"), SNOMED))
        elif rt == "DiagnosticReport":
            add(src, "DiagnosticReport.code has SNOMED CT or LOINC",
                has((r.get("code") or {}).get("coding"), SNOMED) or has((r.get("code") or {}).get("coding"), LOINC))
    rows = []
    for (s, m), (n, d) in sorted(acc.items(), key=lambda x: (x[0][0] != "ALL", x[0][0], x[0][1])):
        rows.append({"source": s, "element": m, "with_standard_code": n, "total": d,
                     "coverage_pct": round(100 * n / d, 1) if d else ""})
    return rows


def conceptmap_rows(seen_local: dict[str, set]):
    cm_path = ROOT / "python-fhir-engine" / "conceptmaps" / "lab-catalogue.json"
    cm = json.loads(cm_path.read_text(encoding="utf-8"))
    auto = defaultdict(set)   # source system -> codes with an automatic target
    target_of = {}
    for g in cm.get("group", []):
        for el in g.get("element", []):
            for t in el.get("target", []):
                if t.get("equivalence") in ("equivalent", "equal"):
                    auto[g["source"]].add(el["code"])
                    target_of[g["source"]] = g.get("target")
    rows = []
    for system in sorted(set(seen_local) | set(auto)):
        seen = seen_local.get(system, set())
        mapped = seen & auto.get(system, set())
        rows.append({"local_system": system.replace(LOCAL_PREFIX, ""), "target_system": target_of.get(system, ""),
                     "codes_in_conceptmap": len(auto.get(system, set())), "distinct_codes_in_data": len(seen),
                     "resolved": len(mapped), "resolution_pct": round(100 * len(mapped) / len(seen), 1) if seen else "",
                     "unresolved_examples": ", ".join(sorted(seen - mapped)[:8])})
    return rows, {"url": cm.get("url"), "version": cm.get("version")}


def validate(pairs, tx, rate, cache_path):
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    todo = [p for p in pairs if f"{p[0]}|{p[1]}" not in cache]
    log(f"terminology: {len(pairs)} distinct standard codes, {len(todo)} not in cache; "
        f"~{len(todo) / rate:.0f} s at {rate} request(s)/s")
    for k, (system, code) in enumerate(todo, start=1):
        q = urllib.parse.urlencode({"url": system, "code": code})
        try:
            r = http("GET", f"{tx}/CodeSystem/$validate-code?{q}", headers={**UA, "Accept": "application/fhir+json"},
                     timeout=60)
            params = {p["name"]: next((v for kk, v in p.items() if kk.startswith("value")), None)
                      for p in r.get("parameter", [])}
            res = {"result": "valid" if params.get("result") is True else "invalid",
                   "display": params.get("display"), "message": params.get("message")}
        except HttpError as e:
            msg = e.body.get("issue", [{}])[0].get("diagnostics") if isinstance(e.body, dict) else str(e.body)[:200]
            res = {"result": "error", "display": None, "message": f"HTTP {e.status}: {msg}"}
        except Exception as e:  # noqa: BLE001
            res = {"result": "error", "display": None, "message": str(e)[:200]}
        res["checked_at"] = datetime.now(timezone.utc).isoformat()
        res["server"] = tx
        cache[f"{system}|{code}"] = res
        if k % 25 == 0 or k == len(todo):
            cache_path.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
            log(f"  {k}/{len(todo)} checked")
        time.sleep(1.0 / rate)
    return cache


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundles", default=None)
    ap.add_argument("--tx", default="https://tx.fhir.org/r4")
    ap.add_argument("--rate", type=float, default=1.0, help="requests per second to the terminology server")
    ap.add_argument("--offline", action="store_true", help="skip the terminology server (tables 1 and 2 only)")
    a = ap.parse_args()
    bundles = Path(a.bundles) if a.bundles else results_dir("bundles")
    out = results_dir("terminology")

    resources, occurrences = [], []
    for src in sorted(d for d in bundles.iterdir() if d.is_dir()):
        for p in sorted(src.glob("*.json")):
            b = json.loads(p.read_text(encoding="utf-8"))
            for e in b.get("entry", []):
                r = e["resource"]
                resources.append((src.name, r))
                found = []
                walk(r, r["resourceType"], found)
                for path, c in found:
                    occurrences.append((src.name, path, c))

    cov = coverage(resources)
    seen_local = defaultdict(set)
    per_system = defaultdict(lambda: {"occurrences": 0, "codes": set(), "elements": set()})
    displays = {}
    for src, path, c in occurrences:
        s = c["system"]
        per_system[s]["occurrences"] += 1
        per_system[s]["codes"].add(c["code"])
        per_system[s]["elements"].add(re.sub(r"\.coding$", "", path))
        if s.startswith(LOCAL_PREFIX):
            seen_local[s].add(c["code"])
        elif c.get("display"):
            displays.setdefault((s, c["code"]), c["display"])
    cm_rows, cm_meta = conceptmap_rows(seen_local)

    standard = sorted((s, code) for s, v in per_system.items() if not s.startswith(LOCAL_PREFIX) for code in v["codes"])
    cache = {} if a.offline else validate(standard, a.tx.rstrip("/"), a.rate, ROOT / "tools" / "tx-cache.json")

    tx_rows, sys_rows = [], []
    for s in sorted(per_system):
        v = per_system[s]
        local = s.startswith(LOCAL_PREFIX)
        res = Counter()
        disp_match = 0
        for code in v["codes"]:
            r = None if local else cache.get(f"{s}|{code}")
            status = "local" if local else (r["result"] if r else "not checked")
            res[status] += 1
            if r and not local:
                ours, theirs = displays.get((s, code)), r.get("display")
                match = bool(ours and theirs and TAG_RE.sub("", ours).strip().lower() == theirs.strip().lower())
                disp_match += match
                tx_rows.append({"system": s, "code": code, "result": status, "our_display": ours or "",
                                "server_display": theirs or "", "display_matches": match,
                                "message": (r.get("message") or "")[:300]})
        n = len(v["codes"])
        sys_rows.append({"system": s, "kind": "local" if local else "standard", "occurrences": v["occurrences"],
                         "distinct_codes": n, "valid": res["valid"], "invalid": res["invalid"], "error": res["error"],
                         "not_checked": res["not checked"],
                         "valid_pct": "" if local or not n else round(100 * res["valid"] / n, 1),
                         "display_matches": "" if local else disp_match,
                         "elements": ", ".join(sorted(v["elements"]))[:200]})

    write_csv(out / "h7_coverage.csv", cov)
    write_csv(out / "h7_conceptmap.csv", cm_rows)
    write_csv(out / "h7_per_system.csv", sys_rows)
    write_csv(out / "tx_results.csv", tx_rows)
    write_json(out / "h7_meta.json", {"tx": None if a.offline else a.tx, "rate_per_s": a.rate, "conceptmap": cm_meta,
                                      "bundles": str(bundles), "resources": len(resources)})
    syscols = ["system", "kind", "occurrences", "distinct_codes", "valid", "invalid", "error", "not_checked",
               "valid_pct", "display_matches"]
    md = ["# H7 — Standard terminology in the produced data", "",
          f"- Terminology server: {'(offline)' if a.offline else a.tx} · ConceptMap {cm_meta['url']} v{cm_meta['version']}",
          "- 'display_matches' compares our display (semantic tag removed) with the server's; it is informational.",
          "", "## 1. Coverage of standard codes", "", markdown_table(cov),
          "## 2. ConceptMap resolution of local codes", "", markdown_table(cm_rows),
          "## 3. Validity per code system", "", markdown_table(sys_rows, syscols)]
    (out / "SUMMARY.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md[5:]))
    log(f"H7 tables -> {out}")


if __name__ == "__main__":
    main()
