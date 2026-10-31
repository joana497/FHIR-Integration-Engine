#!/usr/bin/env python3
"""
adapter_line_counts.py - H6: what did it cost to add a second and a third source?

Counts logical source lines (no blank lines, comments or docstrings) of
each part of the engine, grouped as
  shared     code every source uses (canonical model, FHIR mappers, ConceptMap, API)
  adapter    code specific to one source (SIL, LIS-B, ORU^R01)
and, with --baseline, how many lines of the shared code changed compared
with an earlier copy of the engine (for example the version before the
second adapter was added).

  python scripts/adapter_line_counts.py
  python scripts/adapter_line_counts.py --baseline ../hl7-fhir-engine-v1

Writes h6/line_counts.csv/.md (and h6/shared_changes.csv with --baseline).
The figures are counts of lines, not of effort; report them as such.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import io
import re
import sys
import tokenize
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, log, markdown_table, results_dir, write_csv  # noqa: E402

PY = "python-fhir-engine"
GROUPS = [  # (group, part, files)
    ("shared", "canonical model", [f"{PY}/canonical.py"]),
    ("shared", "FHIR mappers", [f"{PY}/fhir_mappers.py"]),
    ("shared", "code systems / ConceptMap", [f"{PY}/systems.py", f"{PY}/concept_map.py"]),
    ("shared", "API (FastAPI)", [f"{PY}/main.py"]),
    ("shared", "adapter contracts and helpers", [f"{PY}/adapters/base.py", f"{PY}/adapters/common.py",
                                                  f"{PY}/adapters/__init__.py"]),
    ("adapter", "SIL (SQL, Portuguese schema)", [f"{PY}/adapters/sil.py", f"{PY}/database.py"]),
    ("adapter", "LIS-B (SQL, second schema)", [f"{PY}/adapters/lisb.py"]),
    ("adapter", "ORU^R01 (HL7 v2 message)", [f"{PY}/adapters/oru_r01.py", f"{PY}/hl7v2.py"]),
    ("adapter", "ORU^R01 transport (MLLP, Node.js)", ["node-middleware/src/services/mllpServer.js"]),
]


def py_sloc(text: str) -> int:
    doc_lines: set[int] = set()
    try:
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    doc_lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    except SyntaxError:
        pass
    code_lines: set[int] = set()
    skip = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING,
            tokenize.ENDMARKER}
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type not in skip:
            code_lines.update(range(tok.start[0], tok.end[0] + 1))
    return len(code_lines - doc_lines)


def js_sloc(text: str) -> int:
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    return sum(1 for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("//"))


def sloc(path: Path) -> int:
    t = path.read_text(encoding="utf-8")
    return py_sloc(t) if path.suffix == ".py" else js_sloc(t)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", default=None, help="root folder of an earlier copy of the engine")
    a = ap.parse_args()
    out = results_dir("h6")
    rows = []
    for group, part, files in GROUPS:
        present = [ROOT / f for f in files if (ROOT / f).exists()]
        n = sum(sloc(p) for p in present)
        physical = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in present)
        rows.append({"group": group, "part": part, "files": ", ".join(f.split("/")[-1] for f in files),
                     "logical_sloc": n, "physical_lines": physical})
    shared = sum(r["logical_sloc"] for r in rows if r["group"] == "shared")
    for r in rows:
        r["pct_of_shared"] = round(100 * r["logical_sloc"] / shared, 1) if r["group"] == "adapter" else ""
    write_csv(out / "line_counts.csv", rows)
    md = ["# H6 — Lines of code per part of the engine", "",
          "Logical SLOC exclude blank lines, comments and docstrings.", "", markdown_table(rows)]

    if a.baseline:
        base = Path(a.baseline)
        changes = []
        for group, part, files in GROUPS:
            if group != "shared":
                continue
            for f in files:
                new, old = ROOT / f, base / f
                if not new.exists():
                    continue
                old_lines = old.read_text(encoding="utf-8").splitlines() if old.exists() else []
                diff = list(difflib.unified_diff(old_lines, new.read_text(encoding="utf-8").splitlines(), lineterm="", n=0))
                added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
                removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
                changes.append({"file": f, "in_baseline": old.exists(), "lines_added": added, "lines_removed": removed})
        write_csv(out / "shared_changes.csv", changes)
        md += ["", f"## Changes to shared code relative to {base}", "", markdown_table(changes)]
    (out / "line_counts.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    log(f"-> {out}")


if __name__ == "__main__":
    main()
