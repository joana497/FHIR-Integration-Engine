#!/usr/bin/env python3
"""
summarise_validator.py - turn the HL7 Validator output into the H1 tables.

  python scripts/summarise_validator.py results/<run>/validator [--bundles results/<run>/bundles]

Inputs: the <source>_<batch>.json files written by run_hl7_validator.py
(each an OperationOutcome, or a Bundle of OperationOutcomes when several
files were validated together) and validator_run.json.

Outputs (in the same folder)
  issues.csv            one row per issue (source, file, severity, resource type, message id, location, text)
  h1_by_source.csv/.md  per source: Bundles, Bundles with errors, error/warning/information counts
  h1_by_resource.csv    per resource type and severity
  h1_messages.csv/.md   issues grouped by message id (or normalised text), most frequent first
  SUMMARY.md            the tables above plus the validator version and settings

Issues are attributed to a resource type by resolving 'Bundle.entry[n]'
in the issue location against the Bundle file itself.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import log, markdown_table, write_csv  # noqa: E402

EXT_FILE = "operationoutcome-file"
EXT_MSGID = "operationoutcome-message-id"
ENTRY_RE = re.compile(r"Bundle\.entry\[(\d+)\]")
TYPE_IN_LOC = re.compile(r"/\*([A-Z][A-Za-z]+)/")
NOISE_RE = re.compile(r"^(all ok|no issues)", re.I)


def ext_value(obj: dict, suffix: str):
    for e in obj.get("extension", []) or []:
        if str(e.get("url", "")).endswith(suffix):
            for k, v in e.items():
                if k.startswith("value"):
                    return v
    return None


def outcomes(doc: dict) -> list[dict]:
    if doc.get("resourceType") == "OperationOutcome":
        return [doc]
    return [e["resource"] for e in doc.get("entry", []) if e.get("resource", {}).get("resourceType") == "OperationOutcome"]


def normalise(text: str) -> str:
    t = re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", text or "")
    t = re.sub(r"\b\d+(\.\d+)?\b", "n", t)
    return t[:160]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--bundles", default=None)
    a = ap.parse_args()
    folder = Path(a.folder)
    bundles = Path(a.bundles) if a.bundles else folder.parent / "bundles"
    meta = json.loads((folder / "validator_run.json").read_text(encoding="utf-8")) \
        if (folder / "validator_run.json").exists() else {"runs": {}}
    batch_files = {b["batch"]: b["files"] for runs in meta.get("runs", {}).values() for b in runs}

    entry_types: dict[tuple[str, str], list[str]] = {}

    def rtype(source: str, fname: str, loc: str) -> str:
        named = TYPE_IN_LOC.search(loc or "")  # the validator writes '.resource/*Observation/id*/...'
        if named:
            return named.group(1)
        m = ENTRY_RE.search(loc or "")
        if not m:
            return "Bundle"
        key = (source, fname)
        if key not in entry_types:
            p = bundles / source / fname
            try:
                b = json.loads(p.read_text(encoding="utf-8"))
                entry_types[key] = [e["resource"]["resourceType"] for e in b.get("entry", [])]
            except (OSError, ValueError, KeyError):
                entry_types[key] = []
        types = entry_types[key]
        i = int(m.group(1))
        return types[i] if i < len(types) else "?"

    issues, files_seen, order_fallback = [], defaultdict(set), 0
    for out in sorted(folder.glob("*_[0-9][0-9][0-9].json")):
        batch = out.stem
        source = batch.rsplit("_", 1)[0]
        listed = batch_files.get(batch, [])
        for k, oo in enumerate(outcomes(json.loads(out.read_text(encoding="utf-8")))):
            f = ext_value(oo, EXT_FILE)
            if f:
                fname = Path(str(f).replace("\\", "/")).name
            else:  # fall back to the order of the files given to the validator
                fname = listed[k] if k < len(listed) else f"#{k}"
                order_fallback += 1
            files_seen[source].add(fname)
            for iss in oo.get("issue", []):
                text = (iss.get("details") or {}).get("text") or iss.get("diagnostics") or ""
                if iss.get("severity") == "information" and NOISE_RE.match(text):
                    continue
                loc = (iss.get("expression") or iss.get("location") or [""])[0]
                issues.append({
                    "source": source, "file": fname, "severity": iss.get("severity"), "code": iss.get("code"),
                    "resource_type": rtype(source, fname, loc),
                    "message_id": ext_value(iss, EXT_MSGID) or "", "location": loc, "message": text,
                })

    write_csv(folder / "issues.csv", issues,
              ["source", "file", "severity", "code", "resource_type", "message_id", "location", "message"])

    by_source = []
    for src in sorted(files_seen):
        si = [i for i in issues if i["source"] == src]
        sev = Counter(i["severity"] for i in si)
        with_err = {i["file"] for i in si if i["severity"] in ("error", "fatal")}
        by_source.append({"source": src, "bundles": len(files_seen[src]),
                          "bundles_with_errors": len(with_err),
                          "bundles_valid_pct": round(100 * (1 - len(with_err) / max(1, len(files_seen[src]))), 1),
                          "fatal": sev["fatal"], "error": sev["error"], "warning": sev["warning"],
                          "information": sev["information"]})
    tot = {k: sum(r[k] for r in by_source) for k in ("bundles", "bundles_with_errors", "fatal", "error",
                                                         "warning", "information")}
    tot["source"] = "TOTAL"
    tot["bundles_valid_pct"] = round(100 * (1 - tot["bundles_with_errors"] / max(1, tot["bundles"])), 1)
    by_source.append(tot)
    cols = ["source", "bundles", "bundles_with_errors", "bundles_valid_pct", "fatal", "error", "warning", "information"]
    write_csv(folder / "h1_by_source.csv", by_source, cols)

    rc = Counter((i["resource_type"], i["severity"]) for i in issues)
    by_res = [{"resource_type": t, **{s: rc[(t, s)] for s in ("fatal", "error", "warning", "information")}}
              for t in sorted({t for t, _ in rc})]
    write_csv(folder / "h1_by_resource.csv", by_res)

    groups: dict[tuple, dict] = {}
    for i in issues:
        key = (i["severity"], i["message_id"] or normalise(i["message"]))
        g = groups.setdefault(key, {"severity": i["severity"], "message_id": i["message_id"],
                                    "pattern": normalise(i["message"]), "count": 0, "bundles": set(),
                                    "resource_types": set(), "example": i["message"][:240]})
        g["count"] += 1
        g["bundles"].add((i["source"], i["file"]))
        g["resource_types"].add(i["resource_type"])
    sev_rank = {"fatal": 0, "error": 1, "warning": 2, "information": 3}
    msgs = sorted(groups.values(), key=lambda g: (sev_rank.get(g["severity"], 9), -g["count"]))
    for g in msgs:
        g["bundles"] = len(g["bundles"])
        g["resource_types"] = ", ".join(sorted(g["resource_types"]))
    mcols = ["severity", "count", "bundles", "resource_types", "message_id", "example"]
    write_csv(folder / "h1_messages.csv", msgs, mcols)

    md = ["# H1 — Official HL7 FHIR Validator results", ""]
    md.append(f"- Validator jar SHA-256: `{meta.get('jar_sha256', '?')}`")
    vl = {b.get('version_line') for runs in meta.get('runs', {}).values() for b in runs}
    md.append(f"- Version line(s) printed by the validator: {'; '.join(sorted(v for v in vl if v)) or '?'}")
    md.append(f"- FHIR version: {meta.get('fhir_version', '4.0.1')} · terminology: {meta.get('tx', '?')}")
    if "allow_example_urls" in meta:
        md.append(f"- -allow-example-urls: {'true (example.org URIs accepted)' if meta['allow_example_urls'] else 'not set (example.org URIs reported as errors)'}")
    if order_fallback:
        md.append(f"- Note: {order_fallback} OperationOutcome(s) had no file extension and were matched by order.")
    md += ["", "## Per source", "", markdown_table(by_source, cols),
           "## Per resource type", "", markdown_table(by_res),
           "## Issues grouped by message (most frequent first)", "", markdown_table(msgs[:40], mcols)]
    (folder / "SUMMARY.md").write_text("\n".join(md), encoding="utf-8")
    (folder / "h1_by_source.md").write_text(markdown_table(by_source, cols), encoding="utf-8")
    (folder / "h1_messages.md").write_text(markdown_table(msgs, mcols), encoding="utf-8")
    print(markdown_table(by_source, cols))
    log(f"{len(issues)} issues summarised -> {folder / 'SUMMARY.md'}")


if __name__ == "__main__":
    main()
