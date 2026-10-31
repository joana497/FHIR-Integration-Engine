"""
lisb.py - adapter for LIS-B, the second (differently structured) SQL
laboratory system (postgres/05_lisb_schema.sql).

LIS-B has no examination header: one requisition becomes one
DiagnosticReport, each ordered test one grouping Observation, and each
result row one Observation. Connects as lisb_reader (SELECT only).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

import systems as S
from canonical import (Coding, ExamRec, Identifier, OrderLineRec, OrderRec,
                       PatientRec, ResultRec, TestRec)
from .common import parse_ref_range

SRC = "lisb"
GENDER = {"M": "male", "F": "female", "U": "unknown"}
PRIORITY = {"R": "routine", "A": "asap", "S": "stat"}
ORDER_STATUS = {"NW": "active", "IP": "active", "CM": "completed", "CA": "revoked"}
RESULT_STATUS = {"F": "final", "P": "preliminary", "C": "corrected"}
INTERPRETATION = {"N", "H", "L", "HH", "LL", "A", "POS", "NEG"}


class LisbAdapter:
    name = SRC
    description = "LIS-B (second laboratory system) - PostgreSQL database lisbdb, schema lis"

    def __init__(self, url: str):
        self.engine = create_engine(url, poolclass=NullPool,
                                    connect_args={"options": "-c default_transaction_read_only=on"})

    def list_orders(self, limit: int = 50, offset: int = 0) -> list[str]:
        with self.engine.connect() as c:
            rows = c.execute(text("SELECT accession FROM lis.requisition ORDER BY req_id LIMIT :l OFFSET :o"),
                             {"l": limit, "o": offset})
            return [r.accession for r in rows]

    def get_order(self, order_id: str) -> Optional[OrderRec]:
        with self.engine.connect() as c:
            q = c.execute(text("""
                SELECT r.*, p.mrn, p.national_id, p.given_name, p.family_name, p.birth_date, p.sex
                FROM lis.requisition r JOIN lis.patient p ON p.patient_id = r.patient_id
                WHERE r.accession = :a"""), {"a": order_id}).mappings().first()
            if q is None:
                return None
            tests = c.execute(text("SELECT * FROM lis.req_test WHERE req_id = :r ORDER BY line_no"),
                              {"r": q["req_id"]}).mappings().all()
            results = c.execute(text("SELECT * FROM lis.result WHERE req_id = :r ORDER BY line_no, result_id"),
                                {"r": q["req_id"]}).mappings().all()

        ids = [Identifier(S.sid(SRC, "patient"), q["mrn"])]
        if q["national_id"]:
            ids.append(Identifier(S.SNS_NUMBER, q["national_id"]))
        name = " ".join(x for x in (q["given_name"], q["family_name"]) if x)
        patient = PatientRec(identifiers=ids, family=q["family_name"],
                             given=[q["given_name"]] if q["given_name"] else [], name_text=name or None,
                             gender=GENDER.get(q["sex"] or "U", "unknown"), birth_date=q["birth_date"])
        accession = q["accession"]
        order = OrderRec(
            source=SRC, identifier=Identifier(S.sid(SRC, "accession"), accession), patient=patient,
            status=ORDER_STATUS.get(q["status"], "unknown"), priority=PRIORITY.get(q["priority"], "routine"),
            category="LAB", text=q["department"], authored_on=q["requested_at"], occurrence=q["collected_at"],
            requester=q["requester"], performer=q["department"], reason_text=q["clinical_info"],
            lines=[OrderLineRec([Coding(S.cs(SRC, "test"), t["test_code"], t["test_name"])], t["test_name"])
                   for t in tests],
        )
        statuses = {RESULT_STATUS.get(r["result_status"], "final") for r in results}
        exam = ExamRec(
            key="R", identifier=Identifier(S.sid(SRC, "report"), accession), category="LAB",
            codings=[Coding(S.cs(SRC, "department"), "PC", q["department"])] if q["department"] else [],
            description=q["department"] or "Laboratory report",
            status_report=("registered" if not statuses else "final" if statuses == {"final"}
                           else "corrected" if statuses <= {"final", "corrected"} else "partial"),
            performed=q["collected_at"] or q["requested_at"],
            issued=max((r["resulted_at"] for r in results if r["resulted_at"]), default=None),
            performer=next((r["validated_by"] for r in results if r["validated_by"]), None),
        )
        for t in tests:
            mine = [r for r in results if r["line_no"] == t["line_no"]]
            st = {RESULT_STATUS.get(r["result_status"], "final") for r in mine}
            exam.tests.append(TestRec(
                key=str(t["line_no"]),
                codings=[Coding(S.cs(SRC, "test"), t["test_code"], t["test_name"])],
                text=t["test_name"],
                status="registered" if not st else "final" if st == {"final"} else "preliminary",
                results=[self._result(r) for r in mine],
            ))
        order.exams.append(exam)
        return order

    @staticmethod
    def _result(r) -> ResultRec:
        kind = {"NM": "quantity", "CE": "coded", "ST": "string"}.get(r["value_type"])
        low, high = parse_ref_range(r["ref_range"])
        return ResultRec(
            key=str(r["result_id"]),
            codings=[Coding(S.cs(SRC, "analyte"), r["analyte_code"], r["analyte_name"])],
            text=r["analyte_name"],
            status=RESULT_STATUS.get(r["result_status"], "final"),
            issued=r["resulted_at"],
            kind=kind,
            value_num=r["num_value"] if kind == "quantity" else None,
            comparator=r["operator"],
            unit_local=Coding(S.cs(SRC, "unit"), r["unit"]) if r["unit"] else None,
            value_coding=(Coding(S.cs(SRC, "result-value"), r["code_value"], r["text_value"])
                          if kind == "coded" and r["code_value"] else None),
            value_text=r["text_value"],
            ref_low=low, ref_high=high, ref_text=r["ref_range"],
            interpretation=r["abnormal_flag"] if r["abnormal_flag"] in INTERPRETATION else None,
            performer=r["validated_by"],
        )

    def health(self) -> dict:
        try:
            with self.engine.connect() as c:
                c.execute(text("SELECT 1"))
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
