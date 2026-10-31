#!/usr/bin/env python3
"""
value_fidelity.py - C2.2 Value fidelity for the SIL source.

  python3 scripts/value_fidelity.py            # every seed order: source rows vs the engine's Bundles
  python3 scripts/value_fidelity.py --hapi     # also the copies stored in HAPI (run stack_checks.py first)
  python3 scripts/value_fidelity.py --include-generated

For every mapped column and every record, the value in the FHIR resource must equal the source
value after the documented transformation (docs/h2/sil_fhir_mapping.csv). Threshold: zero
discrepancies.

How it works
  1. Reads the SIL tables directly, as the read-only role sil_reader (not through the engine).
  2. Restates each documented rule of the H2 table in this file, independently of the engine's
     code, and derives from each source row the resource it must produce (by its deterministic id,
     UUID v5 of the business key) and the value expected at each FHIR path.
  3. Fetches the engine's Bundle for each order and compares, path by path. With --hapi it also
     reads every expected resource from HAPI and compares the stored copy; there, a display added
     by the terminology check to a coding that had none is accepted.
  4. The standard codes (LOINC, SNOMED CT, UCUM) expected next to local codes are taken from the
     ConceptMap the engine serves (GET /conceptmap), with the engine's rule: only targets whose
     equivalence is 'equivalent' or 'equal', first target system not already present. Whether the
     ConceptMap itself is right is C7.4/C7.5, not this check.

Output in value_fidelity/: SUMMARY.md, checks_by_column.csv (checks and discrepancies per source
column), discrepancies.csv (one row each: table, column, record, resource, FHIR path, expected,
found), fallbacks.csv (how often each fallback column supplied the value), value_fidelity.json.
A mapped column with no check at all is listed in SUMMARY.md, so a gap in this script is visible.

Rules restated here (keep them in step with tools/sil_mapping.py when either changes):
  identifiers and ids, Patient name/gender/birthDate, ServiceRequest status/priority/category/code/
  orderDetail/requisition/notes, DiagnosticReport status/category/code/effective/issued/conclusion,
  ImagingStudy for DICOM modalities, one grouping Observation per ordered test (laboratory, or any
  test with results) and one Observation per result, value[x] by tiporesultado, reference range,
  interpretation, status, issued and performer.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ENGINE_URL, HAPI_URL, ROOT, HttpError, http, log, markdown_table, psql,  # noqa: E402
                    query_json, results_dir, write_csv, write_json)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8
    sys.exit("value_fidelity.py needs Python 3.9 or later (zoneinfo); e.g. conda activate fhir312")
# Same setting as the engine (docker-compose.yml): time zone of source timestamps without one.
SOURCE_TZ = ZoneInfo(os.environ.get("SOURCE_TIMEZONE", "Europe/Lisbon"))

RO = {"user": os.environ.get("SIL_READER", "sil_reader"),
      "password": os.environ.get("SIL_READER_PASSWORD", "sil_reader_pass")}
FHIR = {"Accept": "application/fhir+json"}

# ── URIs (python-fhir-engine/systems.py, restated) ──────────────────────────
BASE = "http://example.org/fhir"
SID = {n: f"{BASE}/sid/sil-{n}" for n in ("patient", "order", "report", "test", "result")}
CS = {n: f"{BASE}/CodeSystem/sil-{n}" for n in ("protocol", "exam-group", "analysis", "parameter", "unit",
                                                    "result-value")}
PT_SNS, PT_CONV = f"{BASE}/CodeSystem/pt-sns-mcdt", f"{BASE}/CodeSystem/pt-conv-mcdt"
BDNR = f"{BASE}/sid/pt-bdnr-requisition"
LOINC, SNOMED, UCUM = "http://loinc.org", "http://snomed.info/sct", "http://unitsofmeasure.org"
DCM = "http://dicom.nema.org/resources/ontology/DCM"
V2_0074 = "http://terminology.hl7.org/CodeSystem/v2-0074"
OBS_CAT = "http://terminology.hl7.org/CodeSystem/observation-category"
V3_INT = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"

# ── Documented rules (H2 table) ─────────────────────────────────────────────
CATEGORY = {"RX": "XR", "CT": "CT", "MRI": "MR", "ECO": "US", "MG": "MG", "NUC": "NM", "HEM": "XA",
            "LAB": "LAB", "ECG": "ECG", "ANA": "PATH"}
DICOM = {"XR": "DX", "CT": "CT", "MR": "MR", "US": "US", "MG": "MG", "NM": "NM", "XA": "XA"}
LAB = {"LAB", "HM", "CH", "BLB", "MB", "SR"}
IMAGING = {"XR", "CT", "MR", "US", "MG", "NM", "XA"}
ORDER_LOINC = {"XR": "43468-8", "CT": "25045-6", "MR": "25056-3", "US": "25061-3", "ECG": "11524-6",
               "LAB": "26436-6", "NM": "49118-3", "MG": "24605-8", "XA": "18745-0", "PATH": "22634-0"}
SECTION = {"XR": "RAD", "CT": "CT", "MR": "RAD", "US": "RUS", "MG": "RAD", "NM": "NMS", "XA": "CTH", "ECG": "EC",
           "PATH": "SP", "LAB": "LAB", "HM": "HM", "CH": "CH", "BLB": "BLB", "MB": "MB", "SR": "SR"}
SR_CATEGORY = {"lab": ("108252007", "Laboratory procedure"), "imaging": ("363679005", "Imaging")}
GENDER = {"M": "male", "F": "female"}
RESULT_STATUS = {"F": "final", "P": "preliminary", "C": "corrected"}
INTERPRETATION = {"N", "H", "L", "HH", "LL", "A", "POS", "NEG"}
CONV = re.compile(r"^[A-Z]\d{1,4}\.\d$")
# Rule branches reported in SUMMARY.md with the number of records that exercised each one, so that a
# branch the dataset never reaches (count 0) is visible.
BRANCHES = ["order line cancelled (pedidos.cancelar)", "order line with convention code (pt-conv-mcdt)",
            "urgent order", "requisition identifier set", "requisition identifier not set (none or several)",
            "imaging examination (ImagingStudy)", "microbiology examination (category MB)",
            "conclusion from CO results", "ordered test without Observation (not laboratory, no results)",
            "ordered test with SNS code (pt-sns-mcdt)", "codigif not a five-digit SNS code (not coded)",
            "result NM", "result CE", "result TX", "result with comparator", "result with reference text",
            "result unit without UCUM mapping", "interpretation flag kept",
            "interpretation flag dropped (not in the list)"]


def uid(rtype: str, key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"{rtype}/{key}"))


def flag(v) -> bool:
    return v is not None and int(v) != 0


def n(v) -> str:
    """Numeric identifier as text (no '.0')."""
    return str(int(v)) if isinstance(v, (int, Decimal)) else str(v)


def category(modalidade, especialidade=None) -> str:
    cat = CATEGORY.get((modalidade or "LAB").upper(), "LAB")
    if cat == "LAB" and especialidade and "microbiolog" in especialidade.lower():
        return "MB"
    return cat


def obs_category(cat: str) -> list:
    code, disp = (("imaging", "Imaging") if cat in IMAGING else ("procedure", "Procedure") if cat == "ECG"
                  else ("laboratory", "Laboratory"))
    return [{"system": OBS_CAT, "code": code, "display": disp}]


def coding(system, code, display=None):
    return {k: v for k, v in (("system", system), ("code", code), ("display", display)) if v not in (None, "")}


class ConceptMap:
    def __init__(self, cm: dict):
        self.url, self.version = cm.get("url"), cm.get("version")
        self.idx = defaultdict(list)
        for g in cm.get("group", []):
            for el in g.get("element", []):
                for t in el.get("target", []):
                    if t.get("equivalence") in ("equivalent", "equal"):
                        self.idx[(g["source"], el["code"])].append(coding(g["target"], t["code"], t.get("display")))

    def first(self, system, code, target):
        return next((c for c in self.idx.get((system, code), []) if c["system"] == target), None)

    def with_standard(self, codings: list, targets: tuple) -> list:
        """Local codings plus the first equivalent target of each target system not already present."""
        present, out = {c["system"] for c in codings}, list(codings)
        for c in codings:
            for t in targets:
                if t not in present:
                    hit = self.first(c["system"], c.get("code"), t)
                    if hit:
                        out.append(hit)
                        present.add(t)
        seen, dedup = set(), []
        for c in out:
            if c.get("code") and (c["system"], c["code"]) not in seen:
                seen.add((c["system"], c["code"]))
                dedup.append(c)
        return dedup


# ── Comparison ───────────────────────────────────────────────────────────────
def empty(x) -> bool:
    return x is None or (isinstance(x, (list, dict, str)) and len(x) == 0)


INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


def instant(s):
    """Parse a FHIR date/dateTime/instant to an aware datetime. A value without a zone (a SQL DATE or
    TIMESTAMP) is local time of the source, as the H2 table states (SOURCE_TIMEZONE)."""
    if s is None:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?(Z|[+-]\d{2}:\d{2})?$", str(s))
    if not m:
        return str(s)
    d, hh, mm, ss, frac, tz = m.groups()
    dt = datetime.strptime(d, "%Y-%m-%d").replace(hour=int(hh or 0), minute=int(mm or 0), second=int(ss or 0),
                                                   microsecond=int((frac or "0")[:6].ljust(6, "0")))
    if tz is None:
        return dt.replace(tzinfo=SOURCE_TZ)
    if tz == "Z":
        return dt.replace(tzinfo=timezone.utc)
    sign = 1 if tz[0] == "+" else -1
    return dt.replace(tzinfo=timezone(sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[4:6]))))


def codings_equal(exp: list, act, lenient: bool) -> bool:
    exp, act = exp or [], act or []
    if not lenient:
        return exp == act
    if len(exp) != len(act):
        return False
    for e, a in zip(exp, act):
        if (e.get("system"), e.get("code")) != (a.get("system"), a.get("code")):
            return False
        if e.get("display") is not None and e.get("display") != a.get("display"):
            return False
    return True


def same(exp, act, how: str, lenient: bool) -> bool:
    if how == "codings":
        return codings_equal(exp, act, lenient)
    if how == "instant":  # same moment, and written as a full FHIR instant (time and zone)
        return instant(exp) == instant(act) and (act is None or bool(INSTANT.match(str(act))))
    if how == "present":
        return exp == (not empty(act))
    if empty(exp) and empty(act):
        return True
    return exp == act


def get(res, path: str):
    cur = res
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if cur is None:
            return None
        if part[0] == "[":
            i = int(part[1:-1])
            cur = cur[i] if isinstance(cur, list) and len(cur) > i else None
        else:
            cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


# ── Expectations from the source rows ───────────────────────────────────────
class Oracle:
    def __init__(self, cm: ConceptMap):
        self.cm = cm
        self.exp: list[dict] = []
        self.fallbacks = Counter()
        self.branches = Counter({b: 0 for b in BRANCHES})
        self.per_order: dict[str, set] = defaultdict(set)

    def e(self, order, rtype, rid, cols, record, path, expected, how="eq"):
        self.exp.append({"order": order, "type": rtype, "id": rid, "cols": cols, "record": record, "path": path,
                         "expected": expected, "how": how})

    def exists(self, order, rtype, rid, cols, record, present=True):
        self.e(order, rtype, rid, cols, record, "(resource)", present, "exists")
        if present:
            self.per_order[order].add((rtype, rid))

    def pick(self, row, *cols, table=None):
        """First non-empty of `cols` in `row` (documented fallbacks); counts fallback use."""
        for i, c in enumerate(cols):
            if not empty(row.get(c)):
                if i:
                    self.fallbacks[f"{table}.{c}"] += 1
                return row[c], f"{table}.{c}"
        return None, f"{table}.{cols[0]}"

    def order(self, dp: dict, lines: list, exams: list, exam_lines: dict, results: dict) -> None:
        num = dp["numpedido"]
        okey = f"{SID['order']}|{num}"
        pid = uid("Patient", f"{SID['patient']}|{n(dp['dociddoente'])}")
        srid = uid("ServiceRequest", okey)
        P, S = "Patient", "ServiceRequest"
        rec = f"dpedidos {num}"
        e = lambda *a, **k: self.e(num, *a, **k)  # noqa: E731

        # Patient
        self.exists(num, P, pid, ["dpedidos.dociddoente"], rec)
        e(P, pid, ["dpedidos.dociddoente"], rec, "identifier",
          [{"system": SID["patient"], "value": n(dp["dociddoente"])}])
        name = dp["nomedoente"]
        tokens = (name or "").split()
        expected_name = None
        if name:
            expected_name = {"use": "official", "text": name, "given": tokens[:1]}
            if len(tokens) > 1:
                expected_name["family"] = tokens[-1]
            expected_name = [{k: v for k, v in expected_name.items() if not empty(v)}]
        e(P, pid, ["dpedidos.nomedoente"], rec, "name", expected_name)
        e(P, pid, ["dpedidos.sexo"], rec, "gender", GENDER.get((dp["sexo"] or "").upper(), "unknown"))
        e(P, pid, ["dpedidos.datanascimento"], rec, "birthDate", dp["datanascimento"])

        # ServiceRequest
        cat = category(dp["modalidade"])
        self.exists(num, S, srid, ["dpedidos.numpedido"], rec)
        e(S, srid, ["dpedidos.numpedido"], rec, "identifier", [{"system": SID["order"], "value": num}])
        e(S, srid, ["dpedidos.dociddoente"], rec, "subject.reference", f"Patient/{pid}")
        e(S, srid, ["dpedidos.nomedoente"], rec, "subject.display", name)
        e(S, srid, ["dpedidos.fechado", "dpedidos.verificado"], rec, "status",
          "completed" if flag(dp["fechado"]) else "active" if flag(dp["verificado"]) else "draft")
        e(S, srid, ["dpedidos.urgente"], rec, "priority", "urgent" if (dp["urgente"] or 0) > 0 else "routine")
        kind = "lab" if cat in LAB | {"PATH"} else "imaging" if cat in IMAGING else None
        e(S, srid, ["dpedidos.modalidade"], rec, "category[0].coding",
          [coding(SNOMED, *SR_CATEGORY[kind])] if kind else None, "codings")
        e(S, srid, ["dpedidos.modalidade"], rec, "code.coding",
          [coding(LOINC, ORDER_LOINC.get("LAB" if cat in LAB else cat, ORDER_LOINC["LAB"]))], "codings")
        v, col = self.pick(dp, "designamodulo", "modulo", table="dpedidos")
        e(S, srid, [col], rec, "code.text", v)
        v, col = self.pick(dp, "servico", "idser", table="dpedidos")
        e(S, srid, [col], rec, "performer", [{"display": v}] if v else None)
        e(S, srid, ["dpedidos.datapedido"], rec, "authoredOn", dp["datapedido"])
        e(S, srid, ["dpedidos.dataexame"], rec, "occurrenceDateTime", dp["dataexame"])
        e(S, srid, ["dpedidos.medico"], rec, "requester.display", dp["medico"])
        e(S, srid, ["dpedidos.infclinica"], rec, "reasonCode", [{"text": dp["infclinica"]}] if dp["infclinica"] else None)

        active = [l for l in lines if not flag(l["cancelar"])]
        self.branches["order line cancelled (pedidos.cancelar)"] += len(lines) - len(active)
        self.branches["order line with convention code (pt-conv-mcdt)"] += sum(
            bool(l["cod_mcdt"] and CONV.match(l["cod_mcdt"])) for l in active)
        self.branches["urgent order"] += (dp["urgente"] or 0) > 0
        notes = [x for x in (dp["observacoes"],
                             f"Contraindication: {dp['contraindic']}" if dp["contraindic"] else None) if x]
        notes += [f"Line {n(l['ordem'])} ({l['des_pedido'] or l['codprotocolo']}) cancelled in the source system"
                  for l in lines if flag(l["cancelar"])]
        e(S, srid, ["dpedidos.observacoes", "dpedidos.contraindic", "pedidos.cancelar"], rec, "note",
          [{"text": t} for t in notes] or None)
        e(S, srid, ["pedidos.numpedido", "pedidos.cancelar"], rec, "orderDetail", len(active) or None,
          "count")
        for i, l in enumerate(active):
            lrec = f"pedidos {num}/{n(l['ordem'])}"
            local = [c for c in (coding(CS["protocol"], l["codprotocolo"], l["designacaoprotocolo"]),
                                 coding(PT_CONV, l["cod_mcdt"]) if l["cod_mcdt"] and CONV.match(l["cod_mcdt"]) else None)
                     if c and c.get("code")]
            e(S, srid, ["pedidos.codprotocolo", "pedidos.designacaoprotocolo", "pedidos.cod_mcdt", "pedidos.ordem"],
              lrec, f"orderDetail[{i}].coding", self.cm.with_standard(local, (SNOMED,)), "codings")
            e(S, srid, ["pedidos.des_pedido"], lrec, f"orderDetail[{i}].text", l["des_pedido"])
        reqs = {l["id_requisicao"] for l in active}
        self.branches["requisition identifier set" if len(reqs) == 1 and None not in reqs
                      else "requisition identifier not set (none or several)"] += 1
        e(S, srid, ["pedidos.id_requisicao"], rec, "requisition",
          {"system": BDNR, "value": next(iter(reqs))} if len(reqs) == 1 and None not in reqs else None)

        for dx in exams:
            self.exam(num, okey, pid, srid, dp, dx, exam_lines.get(self.ekey_tuple(dx), []),
                      results.get(self.ekey_tuple(dx), []))

    @staticmethod
    def ekey_tuple(r):
        return (r["numexame"], r["idser"], r["grupo"], n(r["versao"]))

    def exam(self, num, okey, pid, srid, dp, dx, lines, results):
        ekey = ":".join(self.ekey_tuple(dx))
        cat = category(dx["modalidade"], dx["designaespecial"])
        D, I, O = "DiagnosticReport", "ImagingStudy", "Observation"
        drid, isid = uid(D, f"{okey}|{ekey}"), uid(I, f"{okey}|{ekey}")
        rec = f"dexames {ekey}"
        e = lambda *a, **k: self.e(num, *a, **k)  # noqa: E731
        keys = ["dexames.numexame", "dexames.versao", "dexames.idser", "dexames.grupo", "dexames.numpedido"]

        self.exists(num, D, drid, keys, rec)
        e(D, drid, ["dexames.numexame"], rec, "identifier", [{"system": SID["report"], "value": dx["numexame"]}])
        e(D, drid, ["dexames.numpedido"], rec, "basedOn", [{"reference": f"ServiceRequest/{srid}"}])
        e(D, drid, ["dpedidos.dociddoente"], rec, "subject.reference", f"Patient/{pid}")
        status = ("final" if flag(dx["relatorio"]) and flag(dx["fechado"]) else "preliminary" if flag(dx["relatorio"])
                  else "registered" if flag(dx["verificado"]) else "unknown")
        e(D, drid, ["dexames.relatorio", "dexames.fechado", "dexames.verificado"], rec, "status", status)
        self.branches[f"DiagnosticReport.status {status}"] += 1
        self.branches["microbiology examination (category MB)"] += cat == "MB"
        e(D, drid, ["dexames.modalidade"], rec, "category[0].coding",
          [coding(V2_0074, SECTION.get(cat, "OTH"))], "codings")
        e(D, drid, ["dexames.grupo", "dexames.designaespecial"], rec, "code.coding",
          [coding(CS["exam-group"], dx["grupo"], dx["designaespecial"])], "codings")
        desc, dcol = self.pick(dx, "designaespecial", "servico", table="dexames")
        e(D, drid, [dcol], rec, "code.text", desc)
        performed, pcol = self.pick(dx, "dataexame", "datapedido", table="dexames")
        effective, ecol = performed, pcol
        if effective is None:
            effective, ecol = self.pick(dp, "dataexame", "datapedido", table="dpedidos")
            if effective is not None:
                self.fallbacks["dexames->dpedidos (effectiveDateTime)"] += 1
        e(D, drid, [ecol], rec, "effectiveDateTime", effective)
        issued, icol = self.pick(dx, "datarelat", "dataverificacao", table="dexames")
        e(D, drid, [icol], rec, "issued", issued, "instant")
        e(D, drid, ["dexames.medico"], rec, "performer", [{"display": dx["medico"]}] if dx["medico"] else None)
        co = [r["valortexto"] for r in results if r["tiporesultado"] == "CO" and r["valortexto"]]
        e(D, drid, ["resultados.valortexto", "resultados.tiporesultado"], rec, "conclusion", "\n".join(co) or None)
        dicom = DICOM.get(cat)
        self.branches["conclusion from CO results"] += bool(co)
        self.branches["imaging examination (ImagingStudy)"] += bool(dicom)
        e(D, drid, ["dexames.modalidade"], rec, "imagingStudy",
          [{"reference": f"ImagingStudy/{isid}"}] if dicom else None)

        # ImagingStudy: DICOM modalities only
        self.exists(num, I, isid, ["dexames.modalidade"], rec, present=bool(dicom))
        if dicom:
            e(I, isid, ["dexames.numexame"], rec, "identifier", [{"system": SID["report"], "value": dx["numexame"]}])
            e(I, isid, ["dexames.fechado", "dexames.verificado"], rec, "status",
              "available" if flag(dx["fechado"]) else "registered" if flag(dx["verificado"]) else "unknown")
            e(I, isid, ["dexames.modalidade"], rec, "modality", [coding(DCM, dicom)], "codings")
            e(I, isid, ["dexames.modalidade"], rec, "series[0].modality", coding(DCM, dicom))
            e(I, isid, [pcol], rec, "started", performed)
            loc, lcol = self.pick(dx, "posto", "idser", table="dexames")
            e(I, isid, [lcol], rec, "location.display", loc)
            e(I, isid, ["dexames.bodypart"], rec, "series[0].bodySite.display", dx["bodypart"])
            e(I, isid, [dcol], rec, "description", desc)
            e(I, isid, [dcol], rec, "series[0].description", desc)
            e(I, isid, ["dpedidos.medico"], rec, "referrer.display", dp["medico"])
            e(I, isid, ["dexames.numpedido"], rec, "basedOn", [{"reference": f"ServiceRequest/{srid}"}])
            e(I, isid, ["dexames.grupo", "dexames.designaespecial"], rec, "procedureCode[0].coding",
              [coding(CS["exam-group"], dx["grupo"], dx["designaespecial"])], "codings")

        test_refs = []
        for ex in lines:
            mine = [r for r in results if r["codigoanalise"] == ex["codigoanalise"]
                    and n(r["ordem"]) == n(ex["ordem"]) and r["tiporesultado"] != "CO"]
            tkey = f"{ex['codigoanalise']}:{n(ex['ordem'])}"
            toid = uid(O, f"{okey}|{ekey}|{tkey}")
            trec = f"exames {ekey}/{tkey}"
            tcols = ["exames.numexame", "exames.versao", "exames.idser", "exames.grupo", "exames.ordem",
                     "exames.codigoanalise"]
            if not mine and cat not in {"LAB", "MB"}:
                # Imaging/ECG/pathology lines without result values produce no Observation
                self.exists(num, O, toid, tcols + ["resultados.tiporesultado"], trec, present=False)
                self.branches["ordered test without Observation (not laboratory, no results)"] += 1
                continue
            test_refs.append({"reference": f"Observation/{toid}"})
            self.exists(num, O, toid, tcols, trec)
            e(O, toid, tcols, trec, "identifier", [{"system": SID["test"], "value": f"{num}:{ekey}:{tkey}"}])
            local = [coding(CS["analysis"], ex["codigoanalise"], ex["designacaoanalise"])]
            if ex["codigif"] and ex["codigif"].isdigit() and len(ex["codigif"]) == 5:
                local.append(coding(PT_SNS, ex["codigif"], ex["designacaoigif"]))
                self.branches["ordered test with SNS code (pt-sns-mcdt)"] += 1
            elif ex["codigif"]:
                self.branches["codigif not a five-digit SNS code (not coded)"] += 1
            if ex["cod_mcdt"] and CONV.match(ex["cod_mcdt"]):
                local.append(coding(PT_CONV, ex["cod_mcdt"]))
            e(O, toid, ["exames.codigoanalise", "exames.designacaoanalise", "exames.codigif", "exames.designacaoigif",
                        "exames.cod_mcdt"], trec, "code.coding", self.cm.with_standard(local, (SNOMED, LOINC)),
              "codings")
            e(O, toid, ["exames.designacaoanalise"], trec, "code.text", ex["designacaoanalise"])
            st = {RESULT_STATUS.get(r["estado"] or "F", "final") for r in mine}
            tstatus = ("registered" if not st else ("corrected" if "corrected" in st else "final")
                       if st <= {"final", "corrected"} else "preliminary")
            e(O, toid, ["resultados.estado"], trec, "status", tstatus)
            e(O, toid, ["dexames.modalidade"], trec, "category[0].coding", obs_category(cat), "codings")
            e(O, toid, [ecol], trec, "effectiveDateTime", effective)
            e(O, toid, ["dexames.medico"], trec, "performer", [{"display": dx["medico"]}] if dx["medico"] else None)
            e(O, toid, ["dpedidos.dociddoente"], trec, "subject.reference", f"Patient/{pid}")
            members = []
            for r in mine:
                members.append({"reference": f"Observation/{self.result(num, okey, ekey, tkey, pid, cat, dx, r, ecol, effective)}"})
            e(O, toid, ["resultados.seq", "resultados.codigoanalise", "resultados.ordem"], trec, "hasMember",
              members or None)
        e(D, drid, ["exames.codigoanalise", "exames.ordem"], rec, "result", test_refs or None)

    def result(self, num, okey, ekey, tkey, pid, cat, dx, r, ecol, effective) -> str:
        O = "Observation"
        rkey = n(r["seq"])
        rid = uid(O, f"{okey}|{ekey}|{tkey}|{rkey}")
        rec = f"resultados {ekey}/{tkey}/{rkey}"
        e = lambda *a, **k: self.e(num, *a, **k)  # noqa: E731
        keys = ["resultados.numexame", "resultados.versao", "resultados.idser", "resultados.grupo",
                "resultados.codigoanalise", "resultados.ordem", "resultados.seq"]
        self.exists(num, O, rid, keys, rec)
        e(O, rid, keys, rec, "identifier", [{"system": SID["result"], "value": f"{num}:{ekey}:{tkey}:{rkey}"}])
        e(O, rid, ["resultados.codparametro", "resultados.designacaoparametro"], rec, "code.coding",
          self.cm.with_standard([coding(CS["parameter"], r["codparametro"], r["designacaoparametro"])], (LOINC,)),
          "codings")
        e(O, rid, ["resultados.designacaoparametro"], rec, "code.text", r["designacaoparametro"])
        e(O, rid, ["resultados.estado"], rec, "status", RESULT_STATUS.get(r["estado"] or "F", "final"))
        e(O, rid, ["resultados.dataresultado"], rec, "issued", r["dataresultado"], "instant")
        perf, pcol = r["validadopor"], "resultados.validadopor"
        if empty(perf) and not empty(dx["medico"]):
            perf, pcol = dx["medico"], "dexames.medico"
            self.fallbacks["resultados->dexames.medico (performer)"] += 1
        e(O, rid, [pcol], rec, "performer", [{"display": perf}] if perf else None)
        e(O, rid, [ecol], rec, "effectiveDateTime", effective)
        e(O, rid, ["dexames.modalidade"], rec, "category[0].coding", obs_category(cat), "codings")
        e(O, rid, ["dpedidos.dociddoente"], rec, "subject.reference", f"Patient/{pid}")

        tipo = r["tiporesultado"]
        quantity = tipo == "NM" and r["valornumerico"] is not None
        coded = tipo == "CE" and not empty(r["codvalor"])
        string = tipo == "TX" and not empty(r["valortexto"])
        self.branches[f"result {tipo}"] += 1
        self.branches["result with comparator"] += bool(quantity and r["comparador"])
        self.branches["result with reference text"] += bool(r["reftexto"])
        self.branches["result unit without UCUM mapping"] += bool(
            quantity and r["unidade"] and not self.cm.first(CS["unit"], r["unidade"], UCUM))
        if r["flag"]:
            self.branches["interpretation flag kept" if r["flag"] in INTERPRETATION
                          else "interpretation flag dropped (not in the list)"] += 1
        e(O, rid, ["resultados.tiporesultado"], rec, "valueQuantity", quantity, "present")
        e(O, rid, ["resultados.tiporesultado"], rec, "valueCodeableConcept", coded, "present")
        e(O, rid, ["resultados.tiporesultado"], rec, "valueString", string, "present")
        unit = None
        if quantity:
            ucum = self.cm.first(CS["unit"], r["unidade"], UCUM) if r["unidade"] else None
            unit = {k: v for k, v in (("unit", r["unidade"]), ("system", UCUM if ucum else None),
                                      ("code", ucum["code"] if ucum else None)) if v is not None}
            e(O, rid, ["resultados.valornumerico"], rec, "valueQuantity.value", r["valornumerico"])
            e(O, rid, ["resultados.comparador"], rec, "valueQuantity.comparator", r["comparador"])
            e(O, rid, ["resultados.unidade"], rec, "valueQuantity.unit", r["unidade"])
            e(O, rid, ["resultados.unidade"], rec, "valueQuantity.system", unit.get("system"))
            e(O, rid, ["resultados.unidade"], rec, "valueQuantity.code", unit.get("code"))
        for col, key in (("refmin", "low"), ("refmax", "high")):
            exp = {"value": r[col], **unit} if quantity and r[col] is not None else None
            e(O, rid, [f"resultados.{col}"], rec, f"referenceRange[0].{key}", exp)
        e(O, rid, ["resultados.reftexto"], rec, "referenceRange[0].text", r["reftexto"])
        if coded:
            e(O, rid, ["resultados.codvalor", "resultados.valortexto"], rec, "valueCodeableConcept.coding",
              self.cm.with_standard([coding(CS["result-value"], r["codvalor"], r["valortexto"])], (SNOMED,)), "codings")
            e(O, rid, ["resultados.valortexto"], rec, "valueCodeableConcept.text", r["valortexto"])
        if tipo == "TX":
            e(O, rid, ["resultados.valortexto"], rec, "valueString", r["valortexto"])
        f = r["flag"] if r["flag"] in INTERPRETATION else None
        e(O, rid, ["resultados.flag"], rec, "interpretation[0].coding[0].code", f)
        e(O, rid, ["resultados.flag"], rec, "interpretation[0].coding[0].system", V3_INT if f else None)
        return rid

# ── Edge-case records (--edge-cases) ────────────────────────────────────────
# Two orders that reach the rule branches the seed does not: a cancelled order line, a convention code
# in the wrong format, a five-token name, lower-case and unknown sex codes, every fallback column
# (modulo, idser, servico, datapedido, dataverificacao, posto, the order's dates, dexames.medico), an
# SNS code that is not five digits, a comparator, a unit and a coded value with no ConceptMap entry, an
# interpretation flag outside the list, preliminary and corrected results, a numeric result without a
# value, TX results, a coded result without a code, fractional seconds, an ECG with a report only.
EDGE_PREFIX = "PED-EDGE-"
EDGE_DELETE = """
DELETE FROM sil.resultados WHERE numexame LIKE 'EX-EDGE-%';
DELETE FROM sil.exames     WHERE numexame LIKE 'EX-EDGE-%';
DELETE FROM sil.dexames    WHERE numexame LIKE 'EX-EDGE-%';
DELETE FROM sil.pedidos    WHERE numpedido LIKE 'PED-EDGE-%';
DELETE FROM sil.dpedidos   WHERE numpedido LIKE 'PED-EDGE-%';
"""
EDGE_INSERT = """
INSERT INTO sil.dpedidos (numpedido, dociddoente, nomedoente, datanascimento, sexo, modalidade, designamodulo, modulo,
  servico, idser, datapedido, dataexame, medico, urgente, fechado, verificado, observacoes, contraindic, infclinica)
VALUES
 ('PED-EDGE-00001', 990000001, 'Maria da Conceição Silva Santos', '1950-02-28', 'I', 'LAB', NULL, 'CON', NULL, 'PATCL',
  '2024-06-01', NULL, 'Dr. Teste Regras', 2, 0, 1, 'Edge-case order (value_fidelity.py)', 'Látex',
  'Verificação das regras de mapeamento'),
 ('PED-EDGE-00002', 990000002, 'Anónimo', '1980-01-01', 'f', 'ECG', 'Cardiologia', 'CAR', 'Cardiologia', 'CARD',
  '2024-06-04', '2024-06-05', NULL, 0, 1, 1, NULL, NULL, NULL);
INSERT INTO sil.pedidos (numpedido, ordem, codprotocolo, designacaoprotocolo, des_pedido, cod_mcdt, cancelar, id_requisicao)
VALUES
 ('PED-EDGE-00001', 1, 'PEDG1', 'Protocolo edge 1', 'Linha activa', 'A697.1', 0, 'REQ-EDGE-1'),
 ('PED-EDGE-00001', 2, 'PEDG2', 'Protocolo edge 2', NULL, '9999', 1, 'REQ-EDGE-1'),
 ('PED-EDGE-00001', 3, 'PEDG3', NULL, 'Linha sem requisição', NULL, NULL, NULL),
 ('PED-EDGE-00002', 1, 'PEDG4', 'ECG', 'Electrocardiograma', NULL, 0, 'REQ-EDGE-2');
INSERT INTO sil.dexames (numexame, versao, idser, grupo, numpedido, modalidade, designaespecial, servico, dataexame,
  datapedido, datarelat, dataverificacao, relatorio, fechado, verificado, medico, posto, bodypart)
VALUES
 ('EX-EDGE-00001', 1, 'PATCL', 'GLAB', 'PED-EDGE-00001', 'LAB', NULL, 'Patologia Clínica', NULL, '2024-06-02', NULL,
  '2024-06-03', 1, 0, 1, 'Dra. Validadora', NULL, NULL),
 ('EX-EDGE-00002', 2, 'RAD1', 'GRX', 'PED-EDGE-00001', 'RX', 'Radiologia', NULL, NULL, NULL, NULL, NULL, 0, 0, 0,
  NULL, NULL, 'Tórax PA'),
 ('EX-EDGE-00003', 1, 'CARD', 'GECG', 'PED-EDGE-00002', 'ECG', 'Cardiologia', NULL, '2024-06-05', NULL, '2024-06-06',
  NULL, 1, 1, 1, 'Dr. Cardio', 'ECG-1', NULL);
INSERT INTO sil.exames (numexame, versao, idser, grupo, ordem, codigoanalise, designacaoanalise, codigif,
  designacaoigif, cod_mcdt)
VALUES
 ('EX-EDGE-00001', 1, 'PATCL', 'GLAB', 1, 'EDG1', 'Teste numérico', '1234', 'Código curto', 'BAD'),
 ('EX-EDGE-00001', 1, 'PATCL', 'GLAB', 2, 'EDG2', 'Teste textual', NULL, NULL, NULL),
 ('EX-EDGE-00001', 1, 'PATCL', 'GLAB', 3, 'EDG3', 'Teste sem resultados', NULL, NULL, NULL),
 ('EX-EDGE-00002', 2, 'RAD1', 'GRX', 1, 'RXT', 'Radiografia do tórax', NULL, NULL, NULL),
 ('EX-EDGE-00003', 1, 'CARD', 'GECG', 1, 'ECG12', 'ECG de 12 derivações', NULL, NULL, NULL);
INSERT INTO sil.resultados (idser, grupo, numexame, codigoanalise, versao, ordem, seq, codparametro,
  designacaoparametro, tiporesultado, valornumerico, comparador, unidade, codvalor, valortexto, refmin, refmax,
  reftexto, flag, estado, dataresultado, validadopor)
VALUES
 ('PATCL', 'GLAB', 'EX-EDGE-00001', 'EDG1', 1, 1, 1, 'EDGP1', 'Parâmetro numérico', 'NM', 0.000123, '<', 'unid/xyz',
  NULL, NULL, 0, 1.5, '0-1.5', 'XX', 'P', '2024-06-03 10:20:30.5', NULL),
 ('PATCL', 'GLAB', 'EX-EDGE-00001', 'EDG1', 1, 1, 2, 'EDGP2', 'Parâmetro pendente', 'NM', NULL, NULL, 'mg/dL',
  NULL, NULL, NULL, NULL, 'pendente', NULL, 'C', '2024-06-03 11:00:00', NULL),
 ('PATCL', 'GLAB', 'EX-EDGE-00001', 'EDG1', 1, 1, 3, 'EDGCO', 'Comentário', 'CO', NULL, NULL, NULL, NULL,
  'Conclusão do teste numérico', NULL, NULL, NULL, NULL, 'F', NULL, NULL),
 ('PATCL', 'GLAB', 'EX-EDGE-00001', 'EDG2', 1, 2, 1, 'EDGT1', 'Observação textual', 'TX', NULL, NULL, NULL, NULL,
  'Texto livre do resultado', NULL, NULL, 'Ausente', 'A', 'F', '2024-06-03 12:00:00', 'Téc. Revisor'),
 ('PATCL', 'GLAB', 'EX-EDGE-00001', 'EDG2', 1, 2, 2, 'EDGC1', 'Valor codificado local', 'CE', NULL, NULL, NULL, 'ZZZ',
  'Valor local', NULL, NULL, NULL, NULL, 'F', '2024-06-03 12:00:00', 'Téc. Revisor'),
 ('PATCL', 'GLAB', 'EX-EDGE-00001', 'EDG2', 1, 2, 3, 'EDGC2', 'Valor sem código', 'CE', NULL, NULL, NULL, NULL,
  'Sem código', NULL, NULL, NULL, NULL, 'F', NULL, NULL),
 ('CARD', 'GECG', 'EX-EDGE-00003', 'ECG12', 1, 1, 1, 'ECGCO', 'Relatório', 'CO', NULL, NULL, NULL, NULL,
  'Ritmo sinusal, sem alterações.', NULL, NULL, NULL, NULL, 'F', NULL, NULL);
"""


# ── Evaluation ───────────────────────────────────────────────────────────────
def evaluate(oracle: Oracle, provider, target: str, lenient: bool, bundles: dict | None = None,
             only: set | None = None):
    checks = defaultdict(lambda: [0, 0, 0])  # column -> [checks, with a non-empty expected value, discrepancies]
    disc = []
    cache = {}
    compared = 0
    for x in oracle.exp:
        if only is not None and x["order"] not in only:
            continue
        compared += 1
        k = (x["order"], x["type"], x["id"])
        if k not in cache:
            cache[k] = provider(*k)
        res = cache[k]
        if x["how"] == "exists":
            actual = res is not None
            ok = actual == x["expected"]
        elif res is None:
            continue  # the missing resource is already one discrepancy
        else:
            actual = get(res, x["path"])
            if x["how"] == "count":
                actual = len(actual) if isinstance(actual, list) and actual else None
            ok = same(x["expected"], actual, x["how"], lenient)
        for c in x["cols"]:
            checks[c][0] += 1
            checks[c][1] += 0 if empty(x["expected"]) or x["expected"] is False else 1
            checks[c][2] += 0 if ok else 1
        if not ok:
            disc.append({"target": target, "order": x["order"], "columns": " ".join(x["cols"]), "record": x["record"],
                         "resource": f"{x['type']}/{x['id']}", "path": x["path"],
                         "expected": json.dumps(x["expected"], default=str, ensure_ascii=False),
                         "found": json.dumps(actual, default=str, ensure_ascii=False)})
    if bundles is not None:  # nothing in a Bundle that the rules do not predict
        for order, b in bundles.items():
            produced = {(en["resource"]["resourceType"], en["resource"]["id"]) for en in b.get("entry", [])}
            for t, i in sorted(produced - oracle.per_order[order]):
                disc.append({"target": target, "order": order, "columns": "", "record": "", "resource": f"{t}/{i}",
                             "path": "(resource)", "expected": "not produced", "found": "produced"})
    return checks, disc, compared


def load_rows(include_generated: bool, edge_cases: bool, limit: int | None):
    def rows(sql):
        out = query_json(sql, **RO)
        return [{k: (None if v == "" else v) for k, v in r.items()} for r in out]
    skip = ([] if include_generated else ["PED-SYN-%"]) + ([] if edge_cases else [f"{EDGE_PREFIX}%"])
    where = ("WHERE " + " AND ".join(f"numpedido NOT LIKE '{x}'" for x in skip)) if skip else ""
    dp = rows(f"SELECT * FROM sil.dpedidos {where}")
    dp.sort(key=lambda r: r["numpedido"])
    if limit:
        dp = dp[:limit]
    wanted = {r["numpedido"] for r in dp}
    pl = [r for r in rows("SELECT * FROM sil.pedidos") if r["numpedido"] in wanted]
    dx = [r for r in rows("SELECT * FROM sil.dexames") if r["numpedido"] in wanted]
    keys = {Oracle.ekey_tuple(r) for r in dx}
    ex = [r for r in rows("SELECT * FROM sil.exames") if Oracle.ekey_tuple(r) in keys]
    rs = [r for r in rows("SELECT * FROM sil.resultados") if Oracle.ekey_tuple(r) in keys]
    lines, exams = defaultdict(list), defaultdict(list)
    for r in sorted(pl, key=lambda r: Decimal(str(r["ordem"]))):
        lines[r["numpedido"]].append(r)
    for r in sorted(dx, key=lambda r: (r["numexame"], Decimal(str(r["versao"])))):
        exams[r["numpedido"]].append(r)
    exam_lines, results = defaultdict(list), defaultdict(list)
    for r in sorted(ex, key=lambda r: Decimal(str(r["ordem"]))):
        exam_lines[Oracle.ekey_tuple(r)].append(r)
    for r in sorted(rs, key=lambda r: (Decimal(str(r["ordem"])), Decimal(str(r["seq"])))):
        results[Oracle.ekey_tuple(r)].append(r)
    counts = {"dpedidos": len(dp), "pedidos": len(pl), "dexames": len(dx), "exames": len(ex), "resultados": len(rs)}
    return dp, lines, exams, exam_lines, results, counts


def mapping_columns() -> dict:
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import sil_mapping  # noqa: PLC0415
    except ImportError:
        return {}
    return {f"{t}.{r[0]}": r[1] for t, rows in sil_mapping.TABLES.items() for r in rows if r[1] in sil_mapping.USED}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hapi", action="store_true", help="also compare the copies stored in HAPI")
    ap.add_argument("--include-generated", action="store_true", help="include generated orders (PED-SYN-...)")
    ap.add_argument("--edge-cases", action="store_true",
                    help="insert two edge-case orders (PED-EDGE-...) that reach the rule branches the seed does not, "
                         "include them, and delete them at the end")
    ap.add_argument("--keep-edge-cases", action="store_true", help="do not delete the edge-case orders")
    ap.add_argument("--limit", type=int, default=None, help="first N orders only")
    ap.add_argument("--conceptmap", default=None, help="ConceptMap file (default: the one the engine serves)")
    a = ap.parse_args()
    out = results_dir("value_fidelity")

    if a.conceptmap:
        cm_json = json.loads(Path(a.conceptmap).read_text(encoding="utf-8"))
    else:
        try:
            cm_json = http("GET", f"{ENGINE_URL}/conceptmap")
        except Exception:  # noqa: BLE001
            cm_json = json.loads((ROOT / "python-fhir-engine" / "conceptmaps" / "lab-catalogue.json")
                                 .read_text(encoding="utf-8"))
    cm = ConceptMap(cm_json)
    if a.edge_cases:
        log(f"inserting the edge-case orders ({EDGE_PREFIX}...) as the owner role, acting as the legacy system")
        psql(EDGE_DELETE + EDGE_INSERT)
    try:
        run(a, out, cm)
    finally:
        if a.edge_cases and not a.keep_edge_cases:
            psql(EDGE_DELETE)
            log("edge-case orders deleted")


def run(a, out: Path, cm: ConceptMap) -> None:
    log("reading the SIL tables as sil_reader")
    dp, lines, exams, exam_lines, results, counts = load_rows(a.include_generated, a.edge_cases, a.limit)
    log("source rows: " + ", ".join(f"{t} {c}" for t, c in counts.items()))
    oracle = Oracle(cm)
    for row in dp:
        oracle.order(row, lines.get(row["numpedido"], []), exams.get(row["numpedido"], []), exam_lines, results)
    log(f"{len(oracle.exp)} expected values derived from {len(dp)} orders")

    bundles = {}
    for row in dp:
        num = row["numpedido"]
        text = http("GET", f"{ENGINE_URL}/fhir/bundle/sil/order/{quote(num, safe='')}", raw=True)
        bundles[num] = json.loads(text, parse_float=Decimal)

    def from_bundle(order, rtype, rid):
        return next((en["resource"] for en in bundles[order].get("entry", [])
                     if en["resource"]["resourceType"] == rtype and en["resource"]["id"] == rid), None)

    targets = [("engine Bundle", len(dp), *evaluate(oracle, from_bundle, "bundle", False, bundles))]
    if a.hapi:
        stored = {}

        def from_hapi(order, rtype, rid):
            if (rtype, rid) not in stored:
                try:
                    stored[(rtype, rid)] = json.loads(http("GET", f"{HAPI_URL}/{rtype}/{rid}", headers=FHIR,
                                                           raw=True), parse_float=Decimal)
                except HttpError:
                    stored[(rtype, rid)] = None
            return stored[(rtype, rid)]
        # Edge-case orders are never sent to HAPI, so only the other orders are compared there.
        only = {r["numpedido"] for r in dp if not r["numpedido"].startswith(EDGE_PREFIX)}
        log(f"reading the expected resources of {len(only)} orders from HAPI")
        targets.append(("HAPI (stored)", len(only), *evaluate(oracle, from_hapi, "hapi", True, None, only)))

    mapped = mapping_columns()
    by_col, all_disc = [], []
    for label, _, checks, disc, _ in targets:
        all_disc += disc
        for col in sorted(set(checks) | set(mapped)):
            c = checks.get(col, [0, 0, 0])
            by_col.append({"target": label, "column": col, "h2_status": mapped.get(col, "(not in H2 table)"),
                           "checks": c[0], "checks_with_value": c[1], "discrepancies": c[2]})
    write_csv(out / "checks_by_column.csv", by_col)
    write_csv(out / "discrepancies.csv", all_disc,
              ["target", "order", "columns", "record", "resource", "path", "expected", "found"])
    write_csv(out / "fallbacks.csv", [{"fallback": k, "records": v} for k, v in sorted(oracle.fallbacks.items())])

    rows, unchecked = [], sorted(c for c in mapped if not any(r["column"] == c and r["checks"] for r in by_col))
    for label, n_orders, checks, disc, compared in targets:
        rows.append({"target": label, "orders": n_orders, "FHIR values compared": compared,
                     "column checks": sum(v[0] for v in checks.values()),
                     "discrepancies": len(disc), "status": "Met" if not disc else "Not met"})
    novalue = sorted(r["column"] for r in by_col if r["target"] == "engine Bundle" and r["checks"]
                     and not r["checks_with_value"])
    edge = sum(r["numpedido"].startswith(EDGE_PREFIX) for r in dp)
    md = ["# C2.2 Value fidelity (SIL)", "",
          f"Source rows read as `{RO['user']}`: " + ", ".join(f"{t} {c}" for t, c in counts.items())
          + (f" (including {edge} edge-case orders)" if edge else "")
          + f". ConceptMap {cm.url} version {cm.version}.", "",
          markdown_table(rows),
          "A column check is one source column compared at one FHIR path for one record (a value checked "
          "at two paths, or by two rules, counts twice).", "",
          "Fallback columns never needed in this dataset (the preferred column always had a value): "
          + (", ".join(c for c in unchecked if mapped[c] == "fallback") or "none") + ".", "",
          "Mapped, rule and key columns of the H2 table with no check (a gap in this script): "
          + (", ".join(c for c in unchecked if mapped[c] != "fallback") or "none") + ".", "",
          "Columns checked only where the source value is empty (no non-empty value in this dataset): "
          + (", ".join(novalue) or "none") + ".", "",
          "Fallbacks used: " + (", ".join(f"{k} {v}" for k, v in sorted(oracle.fallbacks.items())) or "none") + ".", "",
          "## Rule branches exercised by the dataset", "",
          "Records that reached each branch of the documented rules; a branch with 0 was not tested by this run "
          "(--edge-cases adds records for the branches the seed does not reach).", "",
          markdown_table([{"branch": k, "records": v} for k, v in oracle.branches.items()])]
    if all_disc:
        md += ["## First discrepancies", "", markdown_table(all_disc[:30],
               ["target", "record", "resource", "path", "expected", "found"])]
    (out / "SUMMARY.md").write_text("\n".join(md), encoding="utf-8")
    write_json(out / "value_fidelity.json", {"summary": rows, "source_rows": counts, "edge_case_orders": edge,
                                             "unchecked": unchecked, "fallbacks": oracle.fallbacks,
                                             "branches": oracle.branches,
                                             "conceptmap": {"url": cm.url, "version": cm.version}})
    print("\n".join(md))
    log(f"-> {out / 'SUMMARY.md'}")


if __name__ == "__main__":
    main()
