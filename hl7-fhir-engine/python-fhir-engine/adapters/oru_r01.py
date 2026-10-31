"""
oru_r01.py - adapter for HL7 v2.5.1 ORU^R01 result messages.

A message source rather than a database: an external laboratory pushes
messages (over MLLP or HTTP, see node-middleware/src/services/mllpServer.js)
and the engine turns each one into canonical orders.

Mapping (following the structure of the HL7 v2-to-FHIR mappings):
  PID          -> PatientRec          (PID-3 repetitions, PID-5, PID-7, PID-8)
  ORC          -> OrderRec            (placer order number ORC-2, ORC-12 orderer)
  OBR          -> ExamRec + TestRec   (one DiagnosticReport per OBR, OBR-4, OBR-24, OBR-25)
  OBX          -> ResultRec           (OBX-2 type, OBX-3, OBX-5, OBX-6, OBX-7, OBX-8, OBX-11)
Coding systems (HL7 table 0396): LN -> LOINC, SCT -> SNOMED CT, UCUM -> UCUM,
anything else (L, 99xxx) -> the sending laboratory's local code systems.
"""
from __future__ import annotations

from typing import Optional

import systems as S
from canonical import (Coding, ExamRec, Identifier, OrderLineRec, OrderRec,
                       PatientRec, ResultRec, TestRec)
from hl7v2 import HL7ParseError, Message, Segment, parse, ts
from .common import dec as _dec, parse_ref_range as _ref

STANDARD = {"LN": S.LOINC, "SCT": S.SNOMED, "SNM": S.SNOMED, "UCUM": S.UCUM}
GENDER = {"M": "male", "F": "female", "O": "other", "U": "unknown", "A": "other", "N": "unknown"}
STATUS = {"F": "final", "P": "preliminary", "C": "corrected", "X": "cancelled", "R": "preliminary"}
INTERPRETATION = {"N", "H", "L", "HH", "LL", "A", "POS", "NEG"}
CATEGORY = {"HM": "HM", "CH": "CH", "BLB": "BLB", "MB": "MB", "SR": "SR", "LAB": "LAB", "RAD": "XR", "CT": "CT"}


class OruError(ValueError):
    """Message is well-formed HL7 v2 but not a usable ORU^R01."""


def _cwe(seg: Segment, n: int, local_system: str, rep: int = 0) -> list[Coding]:
    """CWE/CE field -> codings (identifier and alternate identifier)."""
    c = seg.components(n, rep) + [""] * 6
    out = []
    for code, disp, sysname in ((c[0], c[1], c[2]), (c[3], c[4], c[5])):
        if code:
            out.append(Coding(STANDARD.get(sysname, local_system), code, disp or None))
    return out


class OruR01Adapter:
    name = "oru"
    description = "HL7 v2.5.1 ORU^R01 messages from an external laboratory (MLLP or HTTP)"

    def parse(self, message: str) -> list[OrderRec]:
        msg = parse(message)
        msh = msg.first("MSH")
        mtype = (msh.comp(9, 1), msh.comp(9, 2))
        if mtype != ("ORU", "R01"):
            raise OruError(f"unsupported message type {mtype[0]}^{mtype[1]}")
        sender = (msh.comp(3, 1) or "ext").lower()
        if msg.first("PID") is None or msg.first("OBR") is None:
            raise OruError("ORU^R01 without PID or OBR")
        return self._orders(msg, sender)

    def control_id(self, message: str) -> str:
        return parse(message).first("MSH").raw(10)

    # ── structure walk: PID, then ORC/OBR/OBX groups ─────────────────────────
    def _orders(self, msg: Message, sender: str) -> list[OrderRec]:
        src = sender  # local code systems are named after the sending application, e.g. lisc-observation
        orders: dict[str, OrderRec] = {}
        patient: Optional[PatientRec] = None
        orc: Optional[Segment] = None
        exam: Optional[ExamRec] = None
        for seg in msg.segments:
            if seg.name == "PID":
                patient = self._patient(seg, src)
                orc = None
            elif seg.name == "ORC":
                orc = seg
            elif seg.name == "OBR":
                if patient is None:
                    raise OruError("OBR before PID")
                placer = (orc.comp(2) if orc else "") or seg.comp(2) or seg.comp(3)
                if not placer:
                    raise OruError("OBR without placer or filler order number")
                order = orders.get(placer)
                if order is None:
                    order = OrderRec(
                        source="oru", identifier=Identifier(S.sid(src, "order"), placer), patient=patient,
                        status={"CM": "completed", "IP": "active", "CA": "revoked"}.get(orc.comp(5) if orc else "",
                                                                                        "active"),
                        category="LAB", authored_on=ts(orc.comp(9)) if orc else None,
                        requester=self._person(orc, 12) if orc else self._person(seg, 16),
                        performer=msg.first("MSH").comp(4, 1) or None,
                    )
                    orders[placer] = order
                exam = self._exam(seg, src, len(order.exams) + 1)
                order.lines.append(OrderLineRec(exam.tests[0].codings, exam.tests[0].text))
                order.exams.append(exam)
            elif seg.name == "OBX":
                if exam is None:
                    raise OruError("OBX before OBR")
                exam.tests[0].results.append(self._result(seg, src, exam))
            elif seg.name == "NTE" and exam is not None:
                note = seg.comp(3)
                if note:
                    exam.conclusion = f"{exam.conclusion}\n{note}" if exam.conclusion else note
        for o in orders.values():
            for e in o.exams:
                st = {r.status for r in e.tests[0].results}
                e.tests[0].status = ("registered" if not st else "final" if st == {"final"}
                                     else "corrected" if st <= {"final", "corrected"} else "preliminary")
        return list(orders.values())

    @staticmethod
    def _person(seg: Segment, n: int) -> Optional[str]:
        c = seg.components(n) + [""] * 3
        name = " ".join(x for x in (c[2], c[1]) if x)
        return name or c[0] or None

    @staticmethod
    def _patient(pid: Segment, src: str) -> PatientRec:
        ids = []
        for rep in range(len(pid.reps(3))):
            c = pid.components(3, rep) + [""] * 5
            if not c[0]:
                continue
            aa = c[3]
            system = S.SNS_NUMBER if aa.upper() == "SNS" else S.sid(src, "patient")
            ids.append(Identifier(system, c[0]))
        if not ids:
            raise OruError("PID-3 has no identifier")
        ids.sort(key=lambda i: i.system != S.sid(src, "patient"))  # local MRN first: primary key
        n = pid.components(5) + [""] * 3
        given = [x for x in (n[1], n[2]) if x]
        return PatientRec(identifiers=ids, family=n[0] or None, given=given,
                          name_text=" ".join([*given, n[0]]).strip() or None,
                          gender=GENDER.get(pid.comp(8).upper(), "unknown"), birth_date=ts(pid.comp(7)))

    def _exam(self, obr: Segment, src: str, n: int) -> ExamRec:
        codings = _cwe(obr, 4, S.cs(src, "test"))
        if not codings:
            raise OruError("OBR-4 (universal service identifier) is empty")
        section = obr.comp(24)
        filler = obr.comp(3) or obr.comp(2)
        status = STATUS.get(obr.comp(25), "unknown")
        return ExamRec(
            key=f"OBR{n}", identifier=Identifier(S.sid(src, "report"), filler),
            category=CATEGORY.get(section, "LAB"), codings=codings,
            description=codings[0].display, status_report=status,
            performed=ts(obr.comp(7)), issued=ts(obr.comp(22)), performer=self._person(obr, 16),
            tests=[TestRec(key="1", codings=codings, text=codings[0].display, status=status)],
        )

    def _result(self, obx: Segment, src: str, exam: ExamRec) -> ResultRec:
        vtype = obx.comp(2)
        codings = _cwe(obx, 3, S.cs(src, "observation"))
        if not codings:
            raise OruError("OBX-3 (observation identifier) is empty")
        key = f"{obx.raw(1) or len(exam.tests[0].results) + 1}" + (f".{obx.raw(4)}" if obx.raw(4) else "")
        r = ResultRec(key=key, codings=codings, text=codings[0].display,
                      status=STATUS.get(obx.comp(11), "final"),
                      issued=ts(obx.comp(14)) or exam.issued,
                      interpretation=obx.comp(8) if obx.comp(8) in INTERPRETATION else None,
                      performer=self._person(obx, 16))
        if vtype == "NM":
            r.kind, r.value_num = "quantity", _dec(obx.comp(5))
            if r.value_num is None:
                raise OruError(f"OBX {key}: NM value '{obx.comp(5)}' is not a number")
            units = _cwe(obx, 6, S.cs(src, "unit"))
            r.unit_local = next((u for u in units if u.system != S.UCUM), None)
            r.unit_standard = next((u for u in units if u.system == S.UCUM), None)
            if r.unit_local is None and r.unit_standard is not None:
                r.unit_local = Coding(S.cs(src, "unit"), r.unit_standard.display or r.unit_standard.code)
            r.ref_low, r.ref_high = _ref(obx.comp(7))
            r.ref_text = obx.comp(7) or None
        elif vtype in ("CE", "CWE", "CNE"):
            vals = _cwe(obx, 5, S.cs(src, "result-value"))
            r.kind = "coded"
            r.value_coding = vals[0] if vals else None
            r.value_text = vals[0].display if vals else None
        elif vtype in ("ST", "TX", "FT"):
            r.kind, r.value_text = "string", obx.comp(5)
        else:
            raise OruError(f"OBX {key}: value type {vtype!r} not supported")
        return r


__all__ = ["OruR01Adapter", "OruError", "HL7ParseError"]
