#!/usr/bin/env python3
"""
build_mapping_table.py - write the H2 column-by-column SIL -> FHIR table.

  python3 tools/build_mapping_table.py

Reads tools/sil_mapping.py and the column types from postgres/01_schema.sql;
writes docs/h2/sil_fhir_mapping.csv and docs/h2/summary.csv (counts per
table and status). The Word version is produced from the same CSV.
"""
from __future__ import annotations

import csv
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from sil_mapping import SUPPLIED, TABLES, USED  # noqa: E402


def schema_columns() -> dict[str, dict[str, str]]:
    ddl = (ROOT / "postgres" / "01_schema.sql").read_text(encoding="utf-8")
    out = {}
    for name, body in re.findall(r"CREATE TABLE sil\.(\w+) \((.*?)\n\);", ddl, re.S):
        cols = {}
        for line in body.splitlines():
            m = re.match(r"\s*(\w+)\s+([A-Z]+(?:\([\d, ]+\))?)", line)
            if m and m.group(1).upper() not in ("PRIMARY", "FOREIGN", "CONSTRAINT", "UNIQUE", "CHECK"):
                cols[m.group(1)] = m.group(2)
        out[name] = cols
    return out


def main() -> None:
    types = schema_columns()
    rows, summary = [], []
    for table, entries in TABLES.items():
        for col, status, target, rule, reason, note in entries:
            rows.append({"table": table, "column": col, "type": types.get(table, {}).get(col, "?"),
                         "status": status, "fhir_target": target, "rule": rule, "reason": reason, "note": note,
                         "in_supplied_schema": table in SUPPLIED})
        c = Counter(e[1] for e in entries)
        r = Counter(e[4] for e in entries if e[1] == "not mapped")
        summary.append({"table": table, "columns": len(entries), "used": sum(c[s] for s in USED),
                        **{s: c[s] for s in ("mapped", "fallback", "rule", "key", "not mapped")},
                        "used_pct": round(100 * sum(c[s] for s in USED) / len(entries), 1),
                        **{f"reason_{k}": v for k, v in sorted(r.items())}})
    out = ROOT / "docs" / "h2"
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "sil_fhir_mapping.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    keys = list(dict.fromkeys(k for s in summary for k in s))
    with open(out / "summary.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, restval=0)
        w.writeheader()
        w.writerows(summary)
    for s in summary:
        print(s)


if __name__ == "__main__":
    main()
