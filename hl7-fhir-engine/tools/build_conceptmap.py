#!/usr/bin/env python3
"""
build_conceptmap.py - derive the laboratory ConceptMap from CPAL v7.0.

Reads catalogue/lab_catalogue.json (local codes chosen for the
demonstration, each component pointing to a CPAL line) and the CPAL
spreadsheet, then writes:

  python-fhir-engine/conceptmaps/lab-catalogue.json   FHIR R4 ConceptMap
  catalogue/catalogue_resolved.json                    catalogue + CPAL data

Every standard code (LOINC, SNOMED CT) and every LOINC display is read
from CPAL; nothing standard is typed by hand. The script stops with an
error if a CPAL line does not exist, belongs to a different SNS code,
or has a scale that contradicts the component's result type.

Usage:
  pip install openpyxl
  python3 tools/build_conceptmap.py --cpal /path/to/CPAL_V7.0.xlsx
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python-fhir-engine"))
import systems as S  # noqa: E402

SOURCES = ("sil", "lisb", "lisc")
# Local code system names per source (see systems.cs)
PARAM_CS = {"sil": "parameter", "lisb": "analyte", "lisc": "observation"}
TEST_CS = {"sil": "analysis", "lisb": "test", "lisc": "test"}

# SPMS, "ESP - Especificação Técnica - Integrações SONHO-LIS/RIS" v1.0 (2022), view MCDT_BDNR:
# COD_MCDT = "Área de Convenção + Código MCDT Tabela de Convencionados". Every test of this
# catalogue is clinical pathology, area "A" (Análises Clínicas) in the SPMS list of areas.
# The concatenation without a separator is an assumption to confirm against real SIL data.
CONV_AREA = "A"


def cod_mcdt(conv: str) -> str:
    return f"{CONV_AREA}{conv}"


VALUE_SET_SHEETS = ("AB0 Group Phenotype", "Rhesus Factor", "Clinical Pathology Outcome", "Microorganism")
SCALE_FOR_TYPE = {"NM": {"Qn"}, "CE": {"Nom", "Ord"}}


def read_cpal(path: Path):
    import openpyxl  # imported here so the rest of the repo does not need it

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)

    def rows(sheet):
        out = []
        for r in wb[sheet].iter_rows(values_only=True):
            out.append(["" if c is None else str(c).strip() for c in r])
        return out

    proc = {}
    for r in rows("Clinical Pathology Procedure")[4:]:
        if r and r[0]:
            proc[r[0]] = {
                "line": r[0], "area": r[1], "sns": r[3], "conv": r[4],
                "procedure_pt": r[5], "order_code": r[6], "order_pt": r[7], "order_en": r[8],
                "loinc": r[9], "loinc_pt": r[10], "loinc_lcn": r[11], "property": r[13],
                "scale": r[14], "ucum_examples": [u.strip() for u in r[16].split(";") if u.strip() and u.strip() != "-"],
            }
    snomed = {}
    for sheet in VALUE_SET_SHEETS:
        for r in rows(sheet)[4:]:
            if len(r) > 5 and r[2]:
                snomed[(sheet, r[2])] = {"fsn": r[3], "pt_en": r[4], "pt_pt": r[5]}
    version = ""
    for r in rows("Version"):
        if any(r):
            version = " ".join(c for c in r if c)
    return proc, snomed


def ucum_for(comp, src):
    u = comp.get("ucum")
    return u.get(src) if isinstance(u, dict) else u


def build(catalogue: dict, proc: dict, snomed: dict):
    errors = []
    resolved_tests = []
    for t in catalogue["tests"]:
        rt = dict(t)
        rt["components"] = []
        first = None
        for c in t["components"]:
            row = proc.get(c["cpal_line"])
            if row is None:
                errors.append(f"CPAL line {c['cpal_line']} not found")
                continue
            if row["sns"] != t["sns"]:
                errors.append(f"line {c['cpal_line']}: SNS {row['sns']} != test SNS {t['sns']}")
            if row["scale"] not in SCALE_FOR_TYPE[c["type"]]:
                errors.append(f"line {c['cpal_line']}: scale {row['scale']} incompatible with type {c['type']}")
            first = first or row
            rc = dict(c)
            rc.update({"loinc": row["loinc"], "loinc_lcn": row["loinc_lcn"], "name_pt": row["loinc_pt"],
                       "scale": row["scale"], "property": row["property"], "cpal_ucum_examples": row["ucum_examples"]})
            if c["type"] == "NM":
                rc["ucum_in_cpal_examples"] = {
                    s: (ucum_for(c, s) in row["ucum_examples"]) for s in SOURCES if c.get("units", {}).get(s)}
            rt["components"].append(rc)
        if first:
            rt.update({"conv": first["conv"], "cod_mcdt": cod_mcdt(first["conv"]), "procedure_pt": first["procedure_pt"],
                       "order_code": first["order_code"], "order_en": first["order_en"], "order_pt": first["order_pt"],
                       "order_system": S.LOINC if "-" in first["order_code"] else S.SNOMED})
        resolved_tests.append(rt)

    resolved_vs = {}
    for name, items in catalogue["value_sets"].items():
        out = []
        for it in items:
            info = snomed.get((it["cpal_sheet"], it["snomed"]))
            if info is None:
                errors.append(f"SNOMED {it['snomed']} not in CPAL sheet '{it['cpal_sheet']}'")
                continue
            out.append({**it, **info})
        resolved_vs[name] = out

    if errors:
        raise SystemExit("CPAL consistency errors:\n  " + "\n  ".join(errors))
    return {"tests": resolved_tests, "value_sets": resolved_vs}


def concept_map(resolved: dict, cpal_version: str) -> dict:
    groups = {}

    def add(source_sys, target_sys, code, display, tcode, tdisplay, comment, equivalence="equivalent"):
        g = groups.setdefault((source_sys, target_sys), {"source": source_sys, "target": target_sys, "element": {}})
        el = g["element"].setdefault(code, {"code": code, "display": display, "target": []})
        if not any(x["code"] == tcode for x in el["target"]):
            el["target"].append({"code": tcode, "display": tdisplay, "equivalence": equivalence, "comment": comment})

    for t in resolved["tests"]:
        cmt_t = f"CPAL v{cpal_version}, SNS {t['sns']}: {t['procedure_pt']}"
        # ordered test -> order-level standard code
        add(S.PT_SNS_MCDT, t["order_system"], t["sns"], t["procedure_pt"], t["order_code"], t["order_en"], cmt_t)
        add(S.PT_CONV_MCDT, t["order_system"], t["cod_mcdt"], t["procedure_pt"], t["order_code"], t["order_en"],
            f"{cmt_t}; convention code {t['conv']}, area {CONV_AREA}")
        for src in ("lisb", "lisc"):
            if t.get(src):
                add(S.cs(src, TEST_CS[src]), t["order_system"], t[src], t["procedure_pt"],
                    t["order_code"], t["order_en"], cmt_t)
        for c in t["components"]:
            cmt = f"CPAL v{cpal_version}, line {c['cpal_line']} (SNS {t['sns']})"
            for src in SOURCES:
                if c.get(src):
                    add(S.cs(src, PARAM_CS[src]), S.LOINC, c[src], c["name_pt"], c["loinc"], c["loinc_lcn"], cmt)
                    if c["type"] == "NM" and c.get("units", {}).get(src):
                        add(S.cs(src, "unit"), S.UCUM, c["units"][src], c["units"][src], ucum_for(c, src),
                            ucum_for(c, src), "UCUM code; CPAL examples for this LOINC: "
                            + (", ".join(c["cpal_ucum_examples"]) or "none"))
    for name, items in resolved["value_sets"].items():
        for it in items:
            for src, code in it["local"].items():
                add(S.cs(src, "result-value"), S.SNOMED, code, it["pt_pt"], it["snomed"], it["pt_en"],
                    f"CPAL v{cpal_version}, value set '{it['cpal_sheet']}'")

    group_list = []
    for g in groups.values():
        group_list.append({"source": g["source"], "target": g["target"], "element": list(g["element"].values())})
    group_list.sort(key=lambda g: (g["source"], g["target"]))
    return {
        "resourceType": "ConceptMap",
        "id": "lab-catalogue",
        "url": S.LAB_CONCEPTMAP_URL,
        "version": "1.0.0",
        "name": "LabCatalogueConceptMap",
        "title": "Local laboratory codes to LOINC, UCUM and SNOMED CT (demonstration catalogue)",
        "status": "active",
        "experimental": True,
        "date": dt.date.today().isoformat(),
        "publisher": "HL7 FHIR integration engine (dissertation demonstration)",
        "description": ("Maps the local laboratory codes, unit strings and qualitative result values of the three "
                        "demonstration sources (SIL, second SQL system, HL7 v2 laboratory) to LOINC, UCUM and "
                        f"SNOMED CT. Standard codes are taken from CPAL v{cpal_version} (SPMS); each target "
                        "carries the CPAL line it came from."),
        "group": group_list,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cpal", required=True, type=Path)
    ap.add_argument("--catalogue", type=Path, default=ROOT / "catalogue" / "lab_catalogue.json")
    args = ap.parse_args()

    catalogue = json.loads(args.catalogue.read_text(encoding="utf-8"))
    proc, snomed = read_cpal(args.cpal)
    resolved = build(catalogue, proc, snomed)
    resolved["cpal"] = catalogue["cpal"]
    cm = concept_map(resolved, catalogue["cpal"]["version"])

    out_cm = ROOT / "python-fhir-engine" / "conceptmaps" / "lab-catalogue.json"
    out_cm.parent.mkdir(parents=True, exist_ok=True)
    out_cm.write_text(json.dumps(cm, ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / "catalogue" / "catalogue_resolved.json").write_text(
        json.dumps(resolved, ensure_ascii=False, indent=1), encoding="utf-8")

    n_el = sum(len(g["element"]) for g in cm["group"])
    print(f"ConceptMap: {len(cm['group'])} groups, {n_el} source codes -> {out_cm.relative_to(ROOT)}")
    for g in cm["group"]:
        print(f"  {g['source'].rsplit('/', 1)[-1]:<22} -> {g['target']:<28} {len(g['element'])} codes")
    warn = [(t['sil'], c['cpal_line'], s) for t in resolved["tests"] for c in t["components"]
            for s, ok in c.get("ucum_in_cpal_examples", {}).items() if not ok]
    if warn:
        print("UCUM codes not listed among CPAL examples (valid UCUM, different unit choice):")
        for w in warn:
            print("  ", w)


if __name__ == "__main__":
    main()
