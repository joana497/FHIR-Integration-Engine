"""
sil.py - adapter for the SIL laboratory/imaging information system.

Reads the four SIL tables (plus the demonstration table sil.resultados)
through the read-only sil_reader role and returns canonical records.
All SIL conventions - flag columns, legacy modality codes, the IGIF/SNS
code in exames.codigif, the convention code in cod_mcdt - are interpreted
here and nowhere else.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from sqlalchemy import text

import systems as S
from canonical import (Coding, ExamRec, Identifier, OrderLineRec, OrderRec,
                       PatientRec, ResultRec, TestRec)
from database import DExame, DPedido, Exame, Pedido, Resultado, SessionLocal

SRC = "sil"

# SIL modalidade -> canonical category (see canonical.ExamRec.category)
CATEGORY = {"RX": "XR", "CT": "CT", "MRI": "MR", "ECO": "US", "MG": "MG", "NUC": "NM",
            "HEM": "XA", "LAB": "LAB", "ECG": "ECG", "ANA": "PATH"}
DICOM = {"XR": "DX", "CT": "CT", "MR": "MR", "US": "US", "MG": "MG", "NM": "NM", "XA": "XA"}
GENDER = {"M": "male", "F": "female"}
RESULT_STATUS = {"F": "final", "P": "preliminary", "C": "corrected"}
INTERPRETATION = {"N", "H", "L", "HH", "LL", "A", "POS", "NEG"}

# Columns this adapter reads (checked against the H2 mapping table by tests).
COLUMNS_READ = {
    "dpedidos": {"numpedido", "dociddoente", "nomedoente", "datanascimento", "sexo", "modalidade",
                 "designamodulo", "modulo", "datapedido", "dataexame", "medico", "servico", "idser",
                 "urgente", "fechado", "verificado", "observacoes", "contraindic", "infclinica"},
    "pedidos": {"numpedido", "ordem", "codprotocolo", "designacaoprotocolo", "des_pedido", "cod_mcdt",
                "cancelar", "id_requisicao"},
    "dexames": {"numexame", "versao", "idser", "grupo", "numpedido", "modalidade", "designaespecial",
                "servico", "dataexame", "datapedido", "datarelat", "dataverificacao", "relatorio",
                "fechado", "verificado", "medico", "posto", "bodypart"},
    "exames": {"numexame", "versao", "idser", "grupo", "ordem", "codigoanalise", "designacaoanalise",
               "codigif", "designacaoigif", "cod_mcdt"},
    "resultados": {"numexame", "versao", "idser", "grupo", "codigoanalise", "ordem", "seq",
                   "codparametro", "designacaoparametro", "tiporesultado", "valornumerico",
                   "comparador", "unidade", "codvalor", "valortexto", "refmin", "refmax",
                   "reftexto", "flag", "estado", "dataresultado", "validadopor"},
}


def _flag(v) -> bool:
    return bool(v) and int(v) != 0


def _id(v) -> str:
    """NUMERIC identifiers arrive as Decimal; render without '.0'."""
    return str(int(v)) if isinstance(v, Decimal) else str(v)


def _sns(code: Optional[str]) -> bool:
    """CPAL 'Código SNS' values are five digits; anything else is not an SNS code."""
    return bool(code) and code.isdigit() and len(code) == 5


# SPMS (Integrações SONHO-LIS/RIS, view MCDT_BDNR): COD_MCDT = convention area
# (one letter, e.g. A = Análises Clínicas) + code of the convention price table.
_CONV = re.compile(r"^[A-Z]\d{1,4}\.\d$")


def _conv(code: Optional[str]) -> bool:
    return bool(code) and bool(_CONV.match(code))


def _category(modalidade: Optional[str], especialidade: Optional[str] = None) -> str:
    cat = CATEGORY.get((modalidade or "LAB").upper(), "LAB")
    if cat == "LAB" and especialidade and "microbiolog" in especialidade.lower():
        return "MB"
    return cat


def _patient(dp: DPedido) -> PatientRec:
    tokens = (dp.nomedoente or "").split()
    # SIL keeps the full name in one column; only first and last tokens are
    # structured, the full string is kept in HumanName.text.
    return PatientRec(
        identifiers=[Identifier(S.sid(SRC, "patient"), _id(dp.dociddoente))],
        family=tokens[-1] if len(tokens) > 1 else None,
        given=tokens[:1],
        name_text=dp.nomedoente,
        gender=GENDER.get((dp.sexo or "").upper(), "unknown"),
        birth_date=dp.datanascimento,
    )


def _order_status(dp: DPedido) -> str:
    if _flag(dp.fechado):
        return "completed"
    if _flag(dp.verificado):
        return "active"
    return "draft"


def _report_status(dx: DExame) -> str:
    if _flag(dx.relatorio) and _flag(dx.fechado):
        return "final"
    if _flag(dx.relatorio):
        return "preliminary"
    if _flag(dx.verificado):
        return "registered"
    return "unknown"


def _study_status(dx: DExame) -> str:
    if _flag(dx.fechado):
        return "available"
    if _flag(dx.verificado):
        return "registered"
    return "unknown"


def _test_status(results) -> str:
    """Status of an ordered test from the statuses of its results."""
    statuses = {RESULT_STATUS.get(r.estado or "F", "final") for r in results}
    if not statuses:
        return "registered"
    if statuses <= {"final", "corrected"}:
        return "corrected" if "corrected" in statuses else "final"
    return "preliminary"


def _result(r: Resultado) -> ResultRec:
    kind = {"NM": "quantity", "CE": "coded", "TX": "string"}.get(r.tiporesultado)
    return ResultRec(
        key=_id(r.seq),
        codings=[Coding(S.cs(SRC, "parameter"), r.codparametro, r.designacaoparametro)],
        text=r.designacaoparametro,
        status=RESULT_STATUS.get(r.estado or "F", "final"),
        issued=r.dataresultado,
        kind=kind,
        value_num=r.valornumerico if kind == "quantity" else None,
        comparator=r.comparador or None,
        unit_local=Coding(S.cs(SRC, "unit"), r.unidade) if r.unidade else None,
        value_coding=(Coding(S.cs(SRC, "result-value"), r.codvalor, r.valortexto)
                      if kind == "coded" and r.codvalor else None),
        value_text=r.valortexto,
        ref_low=r.refmin, ref_high=r.refmax, ref_text=r.reftexto,
        interpretation=r.flag if r.flag in INTERPRETATION else None,
        performer=r.validadopor,
    )


class SilAdapter:
    name = SRC
    description = "SIL (Sistema de Informação Laboratorial) - PostgreSQL, schema sil"

    def list_orders(self, limit: int = 50, offset: int = 0) -> list[str]:
        with SessionLocal() as db:
            rows = (db.query(DPedido.numpedido).order_by(DPedido.numpedido)
                    .offset(offset).limit(limit).all())
            return [r.numpedido for r in rows]

    def list_patients(self, limit: int = 50, offset: int = 0) -> list[PatientRec]:
        with SessionLocal() as db:
            rows = (db.query(DPedido).distinct(DPedido.dociddoente)
                    .filter(DPedido.dociddoente.isnot(None))
                    .order_by(DPedido.dociddoente).offset(offset).limit(limit).all())
            return [_patient(r) for r in rows]

    def get_order(self, order_id: str) -> Optional[OrderRec]:
        with SessionLocal() as db:
            dp = db.query(DPedido).filter(DPedido.numpedido == order_id).first()
            if dp is None:
                return None
            category = _category(dp.modalidade)
            order = OrderRec(
                source=SRC,
                identifier=Identifier(S.sid(SRC, "order"), dp.numpedido),
                patient=_patient(dp),
                status=_order_status(dp),
                priority="urgent" if (dp.urgente or 0) > 0 else "routine",
                category=category,
                text=dp.designamodulo or dp.modulo,
                authored_on=dp.datapedido,
                occurrence=dp.dataexame,
                requester=dp.medico,
                performer=dp.servico or dp.idser,
                reason_text=dp.infclinica,
                notes=[n for n in (dp.observacoes,
                                   f"Contraindication: {dp.contraindic}" if dp.contraindic else None) if n],
            )
            requisitions = set()
            for pl in db.query(Pedido).filter(Pedido.numpedido == order_id).order_by(Pedido.ordem):
                if _flag(pl.cancelar):  # a cancelled line is not part of what is requested
                    order.notes.append(f"Line {_id(pl.ordem)} ({pl.des_pedido or pl.codprotocolo}) "
                                       "cancelled in the source system")
                    continue
                codings = [Coding(S.cs(SRC, "protocol"), pl.codprotocolo, pl.designacaoprotocolo)]
                if _conv(pl.cod_mcdt):
                    codings.append(Coding(S.PT_CONV_MCDT, pl.cod_mcdt))
                order.lines.append(OrderLineRec([c for c in codings if c.code], pl.des_pedido))
                requisitions.add(pl.id_requisicao)
            # ServiceRequest.requisition only when every active line names the same requisition.
            if len(requisitions) == 1 and None not in requisitions:
                order.requisition = Identifier(S.BDNR_REQUISITION, requisitions.pop())

            for dx in (db.query(DExame).filter(DExame.numpedido == order_id)
                       .order_by(DExame.numexame, DExame.versao)):
                order.exams.append(self._exam(db, dx))
            return order

    def _exam(self, db, dx: DExame) -> ExamRec:
        cat = _category(dx.modalidade, dx.designaespecial)
        exam = ExamRec(
            key=f"{dx.numexame}:{dx.idser}:{dx.grupo}:{_id(dx.versao)}",
            identifier=Identifier(S.sid(SRC, "report"), dx.numexame),
            category=cat,
            modality_dicom=DICOM.get(cat),
            codings=[Coding(S.cs(SRC, "exam-group"), dx.grupo, dx.designaespecial)],
            description=dx.designaespecial or dx.servico,
            status_report=_report_status(dx),
            status_study=_study_status(dx),
            performed=dx.dataexame or dx.datapedido,
            issued=dx.datarelat or dx.dataverificacao,
            performer=dx.medico,
            location=dx.posto or dx.idser,
            body_site_text=dx.bodypart,
        )
        keyf = (Exame.numexame == dx.numexame, Exame.idser == dx.idser,
                Exame.grupo == dx.grupo, Exame.versao == dx.versao)
        lines = db.query(Exame).filter(*keyf).order_by(Exame.ordem).all()
        results = (db.query(Resultado)
                   .filter(Resultado.numexame == dx.numexame, Resultado.idser == dx.idser,
                           Resultado.grupo == dx.grupo, Resultado.versao == dx.versao)
                   .order_by(Resultado.ordem, Resultado.seq).all())
        conclusions = [r.valortexto for r in results if r.tiporesultado == "CO" and r.valortexto]
        exam.conclusion = "\n".join(conclusions) or None
        for ex in lines:
            mine = [r for r in results if r.codigoanalise == ex.codigoanalise and r.ordem == ex.ordem
                    and r.tiporesultado != "CO"]
            # Observations are produced for laboratory tests and for any test with result values;
            # imaging/ECG/pathology report text goes to DiagnosticReport.conclusion instead.
            if not mine and cat not in {"LAB", "MB"}:
                continue
            codings = [Coding(S.cs(SRC, "analysis"), ex.codigoanalise, ex.designacaoanalise)]
            if _sns(ex.codigif):
                codings.append(Coding(S.PT_SNS_MCDT, ex.codigif, ex.designacaoigif))
            if _conv(ex.cod_mcdt):
                codings.append(Coding(S.PT_CONV_MCDT, ex.cod_mcdt))
            exam.tests.append(TestRec(
                key=f"{ex.codigoanalise}:{_id(ex.ordem)}",
                codings=codings,
                text=ex.designacaoanalise,
                status=_test_status(mine),
                results=[_result(r) for r in mine],
            ))
        return exam

    def health(self) -> dict:
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
