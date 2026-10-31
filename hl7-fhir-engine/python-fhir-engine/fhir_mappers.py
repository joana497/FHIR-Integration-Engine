"""
fhir_mappers.py
Maps the engine's canonical records (canonical.py) to HL7 FHIR R4
resources (dict representation). No source-specific knowledge lives
here: adapters translate their records into canonical form first, and
the ConceptMap adds the standard codings.
"""
from __future__ import annotations

import html
import os
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

import systems as S
from canonical import Coding, ExamRec, OrderRec, PatientRec, ResultRec, TestRec
from concept_map import ConceptMapIndex, load as load_concept_map

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _deterministic_id(namespace: str, key: str) -> str:
    """Generate a stable UUID from a namespace + business key."""
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"{namespace}/{key}"))


# Time zone of source timestamps that carry none (SQL DATE and TIMESTAMP columns,
# HL7 v2 DTM without an offset). FHIR requires a zone whenever a time is present,
# and the sources record local time; the default is mainland Portugal (WET/WEST,
# +00:00 in winter and +01:00 in summer). Set SOURCE_TIMEZONE per deployment,
# e.g. Atlantic/Azores, or UTC.
SOURCE_TZ = ZoneInfo(os.getenv("SOURCE_TIMEZONE", "Europe/Lisbon"))


def _aware(dt: datetime) -> datetime:
    """A naive datetime is local time of the source; an aware one is kept as it is."""
    return dt if dt.tzinfo else dt.replace(tzinfo=SOURCE_TZ)


def _fmt_date(d) -> Optional[str]:
    """Format a value for a FHIR `date` or `dateTime` element.

    A date stays a date; a datetime always carries its time zone (dateTime rule).
    """
    if d is None:
        return None
    if isinstance(d, datetime):
        return _aware(d).isoformat()
    if isinstance(d, date):
        return d.isoformat()
    return str(d)


def _fmt_instant(d) -> Optional[str]:
    """Format a value for a FHIR `instant` (RFC 3339 with time zone).

    A bare date is promoted to midnight local time of the source; a naive
    datetime is taken as local time of the source (SOURCE_TIMEZONE).
    """
    if d is None:
        return None
    if isinstance(d, datetime):
        return _aware(d).isoformat()
    if isinstance(d, date):
        return datetime.combine(d, time(0), SOURCE_TZ).isoformat()
    s = str(d)
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return datetime.combine(date.fromisoformat(s), time(0), SOURCE_TZ).isoformat()
    return s


def _compact(d: dict) -> dict:
    """Drop top-level keys whose value is None or an empty list/dict/string.

    FHIR R4 forbids null values and empty arrays; HAPI rejects both.
    """
    for k in [k for k, v in d.items()
              if v is None or (isinstance(v, (list, dict, str)) and len(v) == 0)]:
        del d[k]
    return d


def _narrative(summary_lines: list) -> dict:
    """Build a FHIR `text` element (status + XHTML div) from plain lines (dom-6)."""
    safe = [html.escape(str(x).strip(), quote=False) for x in summary_lines
            if x is not None and str(x).strip()]
    body = "<br/>".join(safe) if safe else "(no summary available)"
    return {"status": "generated", "div": f'<div xmlns="http://www.w3.org/1999/xhtml">{body}</div>'}


def _num(v):
    """Decimal -> int when integral, else float (JSON numbers)."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


def _coding(c: Coding) -> dict:
    return _compact({"system": c.system, "code": c.code, "display": c.display})


def _codeable(codings: list[Coding], text: Optional[str] = None) -> dict:
    seen, out = set(), []
    for c in codings:
        if c and c.code and (c.system, c.code) not in seen:
            seen.add((c.system, c.code))
            out.append(_coding(c))
    return _compact({"coding": out, "text": text})


def _with_standard(codings: list[Coding], cm: ConceptMapIndex, targets: tuple[str, ...]) -> list[Coding]:
    """Local codings plus their ConceptMap translations ("dual coding").

    A target system already present (e.g. LOINC sent in an HL7 v2 OBX-3
    alternate identifier) is not added again.
    """
    present = {c.system for c in codings}
    out = list(codings)
    for c in codings:
        for t in targets:
            if t in present:
                continue
            hit = cm.first(c, t)
            if hit:
                out.append(hit)
                present.add(t)
    return out


def _gender_label(g: str) -> str:
    return g or "unknown"


# --------------------------------------------------------------------------
# Category tables (canonical category -> standard codes)
# --------------------------------------------------------------------------

# ServiceRequest.code for the order as a whole: one LOINC code per kind of
# examination, sent without a display so that the terminology run of H7
# (scripts/terminology_resolution.py) reports each code's validity and display.
# The first evaluation run (tx.fhir.org, LOINC 2.83, 2026-10-30) found ECG, LAB,
# XA and PATH valid and the six imaging codes of the first version unknown
# (24629-1, 24627-5, 24628-3, 24636-6, 24631-7, 24634-1). They were replaced by
# the LOINC radiology terms for an unspecified body region (XR, CT, MR, US, NM)
# and by "MG Breast Diagnostic" (MG), the order giving neither region nor purpose.
MODALITY_LOINC = {
    "XR": "43468-8", "CT": "25045-6", "MR": "25056-3", "US": "25061-3", "ECG": "11524-6", "LAB": "26436-6",
    "NM": "49118-3", "MG": "24605-8", "XA": "18745-0", "PATH": "22634-0",
}
LAB_CATEGORIES = {"LAB", "HM", "CH", "BLB", "MB", "SR"}
IMAGING_CATEGORIES = {"XR", "CT", "MR", "US", "MG", "NM", "XA"}

# HL7 v2 table 0074 (Diagnostic Service Section ID)
V2_0074_BY_CATEGORY = {
    "XR": "RAD", "CT": "CT", "MR": "RAD", "US": "RUS", "MG": "RAD", "NM": "NMS",
    "XA": "CTH", "ECG": "EC", "PATH": "SP",
    "LAB": "LAB", "HM": "HM", "CH": "CH", "BLB": "BLB", "MB": "MB", "SR": "SR",
}
# ServiceRequest.category (SNOMED CT, from the FHIR R4 example value set)
SR_CATEGORY = {"lab": ("108252007", "Laboratory procedure"), "imaging": ("363679005", "Imaging")}


def _order_loinc_category(cat: str) -> str:
    return "LAB" if cat in LAB_CATEGORIES else cat


def _obs_category(cat: str) -> tuple[str, str]:
    if cat in IMAGING_CATEGORIES:
        return "imaging", "Imaging"
    if cat == "ECG":
        return "procedure", "Procedure"
    return "laboratory", "Laboratory"


# --------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------

def patient_id(p: PatientRec) -> str:
    i = p.identifiers[0]
    return _deterministic_id("Patient", f"{i.system}|{i.value}")


def _order_key(o: OrderRec) -> str:
    return f"{o.identifier.system}|{o.identifier.value}"


def service_request_id(o: OrderRec) -> str:
    return _deterministic_id("ServiceRequest", _order_key(o))


def diagnostic_report_id(o: OrderRec, e: ExamRec) -> str:
    return _deterministic_id("DiagnosticReport", f"{_order_key(o)}|{e.key}")


def imaging_study_id(o: OrderRec, e: ExamRec) -> str:
    return _deterministic_id("ImagingStudy", f"{_order_key(o)}|{e.key}")


def test_observation_id(o: OrderRec, e: ExamRec, t: TestRec) -> str:
    return _deterministic_id("Observation", f"{_order_key(o)}|{e.key}|{t.key}")


def result_observation_id(o: OrderRec, e: ExamRec, t: TestRec, r: ResultRec) -> str:
    return _deterministic_id("Observation", f"{_order_key(o)}|{e.key}|{t.key}|{r.key}")


# --------------------------------------------------------------------------
# Patient
# --------------------------------------------------------------------------

def map_patient(p: PatientRec, source: str) -> dict:
    name = _compact({"use": "official", "family": p.family, "given": p.given, "text": p.name_text})
    display = p.name_text or " ".join([*p.given, p.family or ""]).strip() or "(unnamed)"
    return _compact({
        "resourceType": "Patient",
        "id": patient_id(p),
        "meta": {"source": S.meta_source(source)},
        "text": _narrative([
            f"Patient: {display}",
            *[f"Identifier: {i.value} ({i.system.rsplit('/', 1)[-1]})" for i in p.identifiers],
            f"Gender: {_gender_label(p.gender)}",
            f"Born: {_fmt_date(p.birth_date) or 'unknown'}",
        ]),
        "identifier": [{"system": i.system, "value": i.value} for i in p.identifiers],
        "name": [name] if len(name) > 1 else [],
        "gender": _gender_label(p.gender),
        "birthDate": _fmt_date(p.birth_date if not isinstance(p.birth_date, datetime) else p.birth_date.date()),
    })


# --------------------------------------------------------------------------
# ServiceRequest
# --------------------------------------------------------------------------

def map_service_request(o: OrderRec, cm: ConceptMapIndex) -> dict:
    cat_key = _order_loinc_category(o.category)
    loinc_code = MODALITY_LOINC.get(cat_key, MODALITY_LOINC["LAB"])
    sr_cat = SR_CATEGORY.get("lab" if o.category in LAB_CATEGORIES | {"PATH"}
                             else "imaging" if o.category in IMAGING_CATEGORIES else "")
    codings = list(o.codings) + [Coding(S.LOINC, loinc_code, None)]
    resource = {
        "resourceType": "ServiceRequest",
        "id": service_request_id(o),
        "meta": {"source": S.meta_source(o.source)},
        "text": _narrative([
            f"Requisition {o.identifier.value}",
            f"Category: {o.category}",
            f"Status: {o.status}",
            f"Priority: {o.priority}",
            f"Requested by: {o.requester or 'unknown'}",
            f"Patient: {o.patient.name_text or o.patient.identifiers[0].value}",
        ]),
        "identifier": [{"system": o.identifier.system, "value": o.identifier.value}],
        "requisition": ({"system": o.requisition.system, "value": o.requisition.value}
                        if o.requisition else None),
        "status": o.status,
        "intent": "order",
        "priority": o.priority,
        "category": [{"coding": [{"system": S.SNOMED, "code": sr_cat[0], "display": sr_cat[1]}]}] if sr_cat else [],
        "code": _codeable(codings, o.text),
        "orderDetail": [_codeable(_with_standard(l.codings, cm, (S.SNOMED,)), l.text) for l in o.lines],
        "subject": {"reference": f"Patient/{patient_id(o.patient)}",
                    "display": o.patient.name_text or None},
        "authoredOn": _fmt_date(o.authored_on),
        "requester": {"display": o.requester} if o.requester else None,
        "performer": [{"display": o.performer}] if o.performer else [],
        "note": [{"text": n} for n in o.notes if n],
        "occurrenceDateTime": _fmt_date(o.occurrence),
        "reasonCode": [{"text": o.reason_text}] if o.reason_text else [],
    }
    resource["subject"] = _compact(resource["subject"])
    return _compact(resource)


# --------------------------------------------------------------------------
# ImagingStudy (imaging examinations only)
# --------------------------------------------------------------------------

def map_imaging_study(o: OrderRec, e: ExamRec) -> Optional[dict]:
    """Return None for anything that is not a DICOM imaging examination."""
    if not e.modality_dicom:
        return None
    series = _compact({
        "uid": _deterministic_id("Series", f"{_order_key(o)}|{e.key}|1"),
        "number": 1,
        "modality": {"system": S.DICOM, "code": e.modality_dicom},
        "description": e.description,
        "bodySite": {"display": e.body_site_text} if e.body_site_text else None,
    })
    # series.instance deliberately omitted: no DICOM instance UIDs in the source
    resource = {
        "resourceType": "ImagingStudy",
        "id": imaging_study_id(o, e),
        "meta": {"source": S.meta_source(o.source)},
        "text": _narrative([
            f"Imaging study {e.identifier.value}",
            f"Modality: {e.modality_dicom}",
            f"Description: {e.description or '(none)'}",
            f"Performed: {_fmt_date(e.performed) or 'unknown'}",
            f"Status: {e.status_study}",
        ]),
        "identifier": [{"system": e.identifier.system, "value": e.identifier.value}],
        "status": e.status_study,
        "modality": [{"system": S.DICOM, "code": e.modality_dicom}],
        "subject": {"reference": f"Patient/{patient_id(o.patient)}"},
        "basedOn": [{"reference": f"ServiceRequest/{service_request_id(o)}"}],
        "started": _fmt_date(e.performed),
        "referrer": {"display": o.requester} if o.requester else None,
        "numberOfSeries": 1,
        "series": [series],
        "procedureCode": [_codeable(e.codings)] if e.codings else [],
        "location": {"display": e.location} if e.location else None,
        "description": e.description,
    }
    return _compact(resource)


# --------------------------------------------------------------------------
# Observations: one per ordered test (grouping) and one per result
# --------------------------------------------------------------------------

INTERPRETATION_DISPLAY = {"N": "Normal", "H": "High", "L": "Low", "HH": "Critical high",
                          "LL": "Critical low", "A": "Abnormal", "POS": "Positive", "NEG": "Negative"}


def map_result_observation(o: OrderRec, e: ExamRec, t: TestRec, r: ResultRec, cm: ConceptMapIndex) -> dict:
    cat_code, cat_disp = _obs_category(e.category)
    codings = _with_standard(r.codings, cm, (S.LOINC,))
    obs = {
        "resourceType": "Observation",
        "id": result_observation_id(o, e, t, r),
        "meta": {"source": S.meta_source(o.source)},
        "identifier": [{"system": S.sid(o.source, "result"),
                        "value": f"{o.identifier.value}:{e.key}:{t.key}:{r.key}"}],
        "status": r.status,
        "category": [{"coding": [{"system": S.OBS_CATEGORY, "code": cat_code, "display": cat_disp}]}],
        "code": _codeable(codings, r.text),
        "subject": {"reference": f"Patient/{patient_id(o.patient)}"},
        "effectiveDateTime": _fmt_date(e.performed or o.occurrence or o.authored_on),
        "issued": _fmt_instant(r.issued),
        "performer": [{"display": r.performer or e.performer}] if (r.performer or e.performer) else [],
    }
    value_line = None
    if r.kind == "quantity" and r.value_num is not None:
        ucum = r.unit_standard or cm.first(r.unit_local, S.UCUM)
        unit_text = (r.unit_local.code if r.unit_local
                     else (r.unit_standard.display or r.unit_standard.code) if r.unit_standard else None)
        q = _compact({"value": _num(r.value_num), "comparator": r.comparator,
                      "unit": unit_text,
                      "system": S.UCUM if ucum else None, "code": ucum.code if ucum else None})
        obs["valueQuantity"] = q
        value_line = f"{r.comparator or ''}{q['value']} {q.get('unit', '')}".strip()
        rr = {}
        for k, v in (("low", r.ref_low), ("high", r.ref_high)):
            if v is not None:
                rr[k] = _compact({"value": _num(v), "unit": q.get("unit"), "system": q.get("system"), "code": q.get("code")})
        if r.ref_text:
            rr["text"] = r.ref_text
        if rr:
            obs["referenceRange"] = [rr]
    elif r.kind == "coded" and r.value_coding is not None:
        vcodings = _with_standard([r.value_coding], cm, (S.SNOMED,))
        obs["valueCodeableConcept"] = _codeable(vcodings, r.value_text or r.value_coding.display)
        value_line = r.value_text or r.value_coding.display or r.value_coding.code
    elif r.kind == "string" and r.value_text:
        obs["valueString"] = r.value_text
        value_line = r.value_text
    if r.ref_text and "referenceRange" not in obs:
        # Reference text of a qualitative or textual result (e.g. "Negativo")
        obs["referenceRange"] = [{"text": r.ref_text}]
    if r.interpretation:
        obs["interpretation"] = [{"coding": [{"system": S.V3_INTERPRETATION, "code": r.interpretation,
                                              "display": INTERPRETATION_DISPLAY.get(r.interpretation)}]}]
    std = next((c for c in codings if c.system == S.LOINC), None)
    obs["text"] = _narrative([
        f"{r.text or (codings[0].display if codings else 'Result')}",
        f"LOINC: {std.code}" if std else "LOINC: (no mapping)",
        f"Value: {value_line}" if value_line else "Value: (not yet available)",
        f"Interpretation: {INTERPRETATION_DISPLAY.get(r.interpretation, r.interpretation)}" if r.interpretation else None,
        f"Status: {r.status}",
    ])
    if "interpretation" in obs:
        obs["interpretation"][0]["coding"][0] = _compact(obs["interpretation"][0]["coding"][0])
    return _compact(obs)


def map_test_observation(o: OrderRec, e: ExamRec, t: TestRec, cm: ConceptMapIndex) -> dict:
    """Grouping Observation for one ordered test; its results are hasMember."""
    cat_code, cat_disp = _obs_category(e.category)
    codings = _with_standard(t.codings, cm, (S.SNOMED, S.LOINC))
    members = [{"reference": f"Observation/{result_observation_id(o, e, t, r)}"} for r in t.results]
    return _compact({
        "resourceType": "Observation",
        "id": test_observation_id(o, e, t),
        "meta": {"source": S.meta_source(o.source)},
        "text": _narrative([
            f"Ordered test: {t.text or (codings[0].display if codings else t.key)}",
            f"Codes: {', '.join(c.code for c in codings)}",
            f"Results: {len(t.results)}",
            f"Status: {t.status}",
        ]),
        "identifier": [{"system": S.sid(o.source, "test"), "value": f"{o.identifier.value}:{e.key}:{t.key}"}],
        "status": t.status,
        "category": [{"coding": [{"system": S.OBS_CATEGORY, "code": cat_code, "display": cat_disp}]}],
        "code": _codeable(codings, t.text),
        "subject": {"reference": f"Patient/{patient_id(o.patient)}"},
        "effectiveDateTime": _fmt_date(e.performed or o.occurrence or o.authored_on),
        "performer": [{"display": e.performer}] if e.performer else [],
        "hasMember": members,
    })


# --------------------------------------------------------------------------
# DiagnosticReport
# --------------------------------------------------------------------------

def map_diagnostic_report(o: OrderRec, e: ExamRec, cm: ConceptMapIndex) -> dict:
    results = [{"reference": f"Observation/{test_observation_id(o, e, t)}"} for t in e.tests]
    v2 = V2_0074_BY_CATEGORY.get(e.category, "OTH")
    resource = {
        "resourceType": "DiagnosticReport",
        "id": diagnostic_report_id(o, e),
        "meta": {"source": S.meta_source(o.source)},
        "text": _narrative([
            f"Diagnostic report {e.identifier.value}",
            f"Category: {e.category} (v2-0074 {v2})",
            f"Description: {e.description or '(none)'}",
            f"Performed: {_fmt_date(e.performed) or 'unknown'}",
            f"Issued: {_fmt_date(e.issued) or 'unknown'}",
            f"Status: {e.status_report} · {len(e.tests)} ordered test(s)",
            f"Performer: {e.performer or 'unknown'}",
            f"Conclusion: {e.conclusion}" if e.conclusion else None,
        ]),
        "identifier": [{"system": e.identifier.system, "value": e.identifier.value}],
        "basedOn": [{"reference": f"ServiceRequest/{service_request_id(o)}"}],
        "status": e.status_report,
        "category": [{"coding": [{"system": S.V2_0074, "code": v2}]}],
        "code": _codeable(e.codings, e.description),
        "subject": {"reference": f"Patient/{patient_id(o.patient)}"},
        "effectiveDateTime": _fmt_date(e.performed or o.occurrence or o.authored_on),
        "issued": _fmt_instant(e.issued),
        "performer": [{"display": e.performer}] if e.performer else [],
        "result": results,
        "imagingStudy": ([{"reference": f"ImagingStudy/{imaging_study_id(o, e)}"}]
                         if e.modality_dicom else []),
        "conclusion": e.conclusion,
    }
    if not resource["code"]:
        resource["code"] = {"text": e.description or e.category}
    return _compact(resource)


# --------------------------------------------------------------------------
# Order -> resources -> Bundle
# --------------------------------------------------------------------------

def build_order_resources(o: OrderRec, cm: Optional[ConceptMapIndex] = None) -> list[dict]:
    cm = cm or load_concept_map()
    out = [map_patient(o.patient, o.source), map_service_request(o, cm)]
    for e in o.exams:
        study = map_imaging_study(o, e)
        if study is not None:
            out.append(study)
        out.append(map_diagnostic_report(o, e, cm))
        for t in e.tests:
            out.append(map_test_observation(o, e, t, cm))
            out.extend(map_result_observation(o, e, t, r, cm) for r in t.results)
    return out


def build_bundle(resources: list, bundle_type: str = "transaction") -> dict:
    """Wrap resources in a FHIR Bundle (PUT entries: idempotent upserts)."""
    entries = []
    for res in resources:
        rtype = res.get("resourceType", "Resource")
        rid = res.get("id", str(uuid.uuid4()))
        entries.append({
            "fullUrl": f"urn:uuid:{rid}",
            "resource": res,
            "request": {"method": "PUT", "url": f"{rtype}/{rid}"},
        })
    return {
        "resourceType": "Bundle",
        "id": str(uuid.uuid4()),
        "type": bundle_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entry": entries,
    }
