"""
synth.py - synthetic patients, orders and results for the three sources.

Used by tools/build_seed.py (the fixed demonstration seed) and by
scripts/generate_orders.py (bulk data for load tests). Values are drawn
from the per-component distributions in lab_catalogue.json with a seeded
random generator, so every run with the same seed gives the same data.
All people, values and identifiers are fictitious.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

HERE = Path(__file__).resolve().parent

GIVEN_F = ["Ana", "Maria", "Inês", "Sofia", "Beatriz", "Catarina", "Teresa", "Rosa", "Daniela", "Patrícia",
           "Joana", "Marta", "Rita", "Helena", "Carla", "Sara", "Mariana", "Filipa", "Isabel", "Lúcia"]
GIVEN_M = ["João", "Pedro", "Rui", "Carlos", "Manuel", "Jorge", "Alberto", "Filipe", "Vítor", "Tiago",
           "Miguel", "Nuno", "Paulo", "Ricardo", "André", "Bruno", "Luís", "Hugo", "Diogo", "José"]
FAMILY = ["Silva", "Santos", "Ferreira", "Pereira", "Oliveira", "Costa", "Rodrigues", "Martins", "Sousa",
          "Fernandes", "Gonçalves", "Gomes", "Lopes", "Marques", "Alves", "Almeida", "Ribeiro", "Pinto",
          "Carvalho", "Teixeira", "Moreira", "Correia", "Mendes", "Nunes", "Soares", "Vieira", "Monteiro",
          "Cardoso", "Rocha", "Neves", "Coelho", "Cruz", "Cunha", "Pires", "Ramos", "Reis", "Simões",
          "Antunes", "Matos", "Fonseca", "Machado", "Nogueira", "Lourenço", "Barbosa", "Tavares"]


def load_catalogue(path: Path | None = None) -> dict:
    return json.loads((path or HERE / "catalogue_resolved.json").read_text(encoding="utf-8"))


@dataclass
class Person:
    given: str
    family: str
    sex: str          # M / F
    birth: date
    sns: str          # fictitious 9-digit number

    @property
    def full(self) -> str:
        return f"{self.given} {self.family}"


def person(rng: random.Random, sns_start: int, idx: int) -> Person:
    sex = rng.choice("MF")
    given = rng.choice(GIVEN_M if sex == "M" else GIVEN_F)
    family = f"{rng.choice(FAMILY)} {rng.choice(FAMILY)}" if rng.random() < 0.6 else rng.choice(FAMILY)
    birth = date(1940, 1, 1) + timedelta(days=rng.randrange(0, 365 * 62))
    return Person(given, family, sex, birth, str(sns_start + idx))


def _q(v: float, dp: int) -> Decimal:
    return Decimal(str(v)).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP)


def numeric_result(comp: dict, rng: random.Random, abnormal: float = 0.15) -> dict:
    mean, sd = comp["dist"]
    v = rng.gauss(mean, sd)
    low, high = comp["ref"]
    if rng.random() < abnormal:  # push a share of results outside the reference interval
        if high is not None and (low is None or rng.random() < 0.6):
            v = high * rng.uniform(1.05, 1.6)
        elif low is not None:
            v = low * rng.uniform(0.6, 0.95)
    v = max(v, 0.0)
    val = _q(v, comp["dp"])
    flag = "N"
    if low is not None and val < Decimal(str(low)):
        flag = "L"
    if high is not None and val > Decimal(str(high)):
        flag = "H"
    if low is None:
        ref_text = f"< {high}"
    elif high is None:
        ref_text = f"> {low}"
    else:
        ref_text = f"{low}-{high}"
    return {"value": val, "flag": flag, "low": low, "high": high, "ref_text": ref_text}


def coded_result(value_set: list, rng: random.Random, force: str | None = None) -> dict:
    if force is not None:
        return next(i for i in value_set if i["snomed"] == force or force in i["local"].values())
    r, acc = rng.random(), 0.0
    for item in value_set:
        acc += item.get("weight", 1.0 / len(value_set))
        if r <= acc:
            return item
    return value_set[-1]


def interpretation_for_coded(item: dict) -> str | None:
    return {"10828004": "POS", "260385009": "NEG"}.get(item["snomed"])


def sql(v) -> str:
    """Render a Python value as a SQL literal."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, Decimal)):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, datetime):
        return f"'{v.isoformat(sep=' ')}'"
    if isinstance(v, date):
        return f"'{v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"


# ════════════════════════════════════════════════════════════════════════════
#  Order specifications and writers
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class OrderSpec:
    number: int                    # sequence number used in every identifier
    person: Person
    tests: list                    # SIL test codes (e.g. "HEM01"); converted per source
    day: date
    urgent: bool = False
    final: bool = True             # False -> preliminary results, order not closed
    abnormal: float = 0.15
    force: dict | None = None      # {component sil code: SNOMED code or local value} for coded results
    department: str = "Análises Clínicas"


class Catalogue:
    def __init__(self, data: dict | None = None):
        self.data = data or load_catalogue()
        self.by_sil = {t["sil"]: t for t in self.data["tests"]}
        self.vs = self.data["value_sets"]

    def tests_for(self, source: str) -> list[str]:
        return [t["sil"] for t in self.data["tests"] if t.get(source)]


def _results_for(cat: Catalogue, spec: OrderSpec, test: dict, rng: random.Random):
    """Yield (component, result-dict) for one test."""
    for comp in test["components"]:
        if comp["type"] == "NM":
            yield comp, {"kind": "NM", **numeric_result(comp, rng, spec.abnormal)}
        else:
            force = (spec.force or {}).get(comp["sil"])
            item = coded_result(cat.vs[comp["value_set"]], rng, force)
            yield comp, {"kind": "CE", "item": item, "flag": interpretation_for_coded(item)}


# ── SIL ─────────────────────────────────────────────────────────────────────
DPEDIDOS_COLS = ("numpedido,numprocesso,episodio,modulo,designamodulo,especialid,designaespecial,idser,servico,"
                 "datapedido,posto,dociddoente,nomedoente,datanascimento,sexo,modalidade,numexame,verificado,avisado,"
                 "marcado,recolhido,fechado,segura,utiliza,tipoanalise,medico,serv,dataverificacao,dataexame,urgente,"
                 "observacoes,infclinica")
PEDIDOS_COLS = ("numpedido,ordem,idser,modalidade,codprotocolo,designacaoprotocolo,codprop,verificado,dataverificacao,"
                "cod_pedido,des_pedido,notas_linhas,cancelar,id_bdnr,id_requisicao,cod_mcdt")
DEXAMES_COLS = ("numexame,versao,numprocesso,episodio,modulo,designamodulo,especialid,designaespecial,idser,servico,"
                "datapedido,dataexame,dociddoente,nomedoente,datanascimento,sexo,grupo,numpedido,verificado,facturado,"
                "imagem,relatorio,fechado,segura,utiliza,tipoanalise,medico,serv,dataverificacao,datafacturacao,"
                "datarelat,datafecho,subsistema,proveniencia,posto,modalidade,bodypart")
EXAMES_COLS = ("numexame,versao,ordem,validade,idser,grupo,codigif,designacaoigif,codigoanalise,designacaoanalise,"
               "codprop,verificado,facturado,dataverificacao,datafacturacao,id_bdnr,id_requisicao,cod_mcdt")
RESULTADOS_COLS = ("idser,grupo,numexame,codigoanalise,versao,ordem,seq,codparametro,designacaoparametro,"
                   "tiporesultado,valornumerico,comparador,unidade,codvalor,valortexto,refmin,refmax,reftexto,flag,"
                   "estado,dataresultado,validadopor")

DOCTORS = ["Dra. Mendes", "Dr. Faria", "Dra. Torres", "Dr. Campos", "Dra. Nunes", "Dr. Azevedo", "Dra. Lima"]
VALIDATORS = ["Dra. Patologista A", "Dr. Patologista B", "Dra. Patologista C"]


def sil_ids(prefix: str, n: int):
    return f"{prefix}-{n:05d}" if "-" not in prefix else f"{prefix}{n:05d}"


def sil_order_rows(cat: Catalogue, spec: OrderSpec, rng: random.Random, prefix: str = "PED-2024-",
                   exam_prefix: str = "EX-2024-") -> dict:
    """Rows (as SQL value tuples) for one laboratory order in the SIL schema."""
    numpedido, numexame = f"{prefix}{spec.number:05d}", f"{exam_prefix}{spec.number:05d}"
    micro = any(t in ("URO01",) for t in spec.tests)
    idser, servico = ("MIC1", "Microbiologia") if micro else ("LAB1", "Laboratório Central")
    dept = "Microbiologia" if micro else spec.department
    d0, d1, d2 = spec.day, spec.day + timedelta(days=1), spec.day + timedelta(days=2)
    medico = rng.choice(DOCTORS)
    p = spec.person
    doc = int(p.sns) % 10**12
    closed = 1 if spec.final else 0
    dp = [numpedido, 100000 + spec.number, 200000 + spec.number, "LAB", "Laboratório", 20, dept, idser, servico,
          d0, "P01", doc, p.full, p.birth, p.sex, "LAB", numexame, 1, 0, 1, 1, closed, 1, "user01", "A", medico,
          "SRV20", d1, d1, 1 if spec.urgent else 0, None, None]
    dx = [numexame, 1, 100000 + spec.number, 200000 + spec.number, "LAB", "Laboratório", 20, dept, idser, servico,
          d0, d1, doc, p.full, p.birth, p.sex, "GLAB", numpedido, 1, 1, 0, 1, closed, 1, "user01", "A", medico,
          "SRV20", d1, d2, d2 if spec.final else None, d2 if spec.final else None, 1, 1, "P01", "LAB", None]
    peds, exs, res = [], [], []
    validator = rng.choice(VALIDATORS)
    for i, code in enumerate(spec.tests, start=1):
        t = cat.by_sil[code]
        # SPMS view MCDT_BDNR: ID_REQUISICAO identifies the requisition, ID_BDNR each MCDT (line) in it,
        # COD_MCDT = convention area + convention-table code; exames.codigif holds the SNS (ACSS) code.
        id_req, id_bdnr = f"REQ{spec.number:05d}", f"BDNR{spec.number:05d}{i:02d}"
        peds.append([numpedido, i, idser, "LAB", f"PROTO{code}", t["procedure_pt"][:90], "N", 1, d1, code,
                     t["procedure_pt"][:200], None, 0, id_bdnr, id_req, t["cod_mcdt"]])
        exs.append([numexame, 1, i, 365, idser, "GLAB", t["sns"], t["procedure_pt"][:200], code,
                    t["procedure_pt"][:200], "N", 1, 1, d1, d2, id_bdnr, id_req, t["cod_mcdt"]])
        for seq, (comp, r) in enumerate(_results_for(cat, spec, t, rng), start=1):
            when = datetime(d1.year, d1.month, d1.day, 9 + rng.randrange(0, 8), rng.randrange(0, 60))
            if r["kind"] == "NM":
                res.append([idser, "GLAB", numexame, code, 1, i, seq, comp["sil"], comp["name_pt"][:200], "NM",
                            r["value"], None, comp["units"]["sil"], None, None,
                            r["low"], r["high"], r["ref_text"], r["flag"], "F" if spec.final else "P", when, validator])
            else:
                it = r["item"]
                res.append([idser, "GLAB", numexame, code, 1, i, seq, comp["sil"], comp["name_pt"][:200], "CE",
                            None, None, None, it["local"]["sil"], it["pt_pt"], None, None, None, r["flag"],
                            "F" if spec.final else "P", when, validator])
    return {"dpedidos": [dp], "pedidos": peds, "dexames": [dx], "exames": exs, "resultados": res}


def sql_inserts(table: str, cols: str, rows: list) -> str:
    if not rows:
        return ""
    body = ",\n".join("(" + ", ".join(sql(v) for v in r) + ")" for r in rows)
    return f"INSERT INTO {table} ({cols}) VALUES\n{body};\n"


# ── LIS-B (second SQL schema) ───────────────────────────────────────────────
def lisb_order_rows(cat: Catalogue, spec: OrderSpec, rng: random.Random, patient_id: int, new_patient: bool) -> dict:
    p = spec.person
    req_id = spec.number
    accession = f"B{spec.day:%y%m%d}{spec.number:05d}"
    at = datetime(spec.day.year, spec.day.month, spec.day.day, 8, rng.randrange(0, 60))
    status = "CM" if spec.final else "IP"
    out = {"patient": [], "requisition": [], "req_test": [], "result": []}
    if new_patient:
        out["patient"].append([patient_id, f"MRN{patient_id:06d}", p.sns, p.given, p.family, p.birth, p.sex])
    out["requisition"].append([req_id, accession, patient_id, at, at + timedelta(minutes=30),
                               "S" if spec.urgent else "R", status, rng.choice(DOCTORS), "Patologia Clínica", None])
    rid = req_id * 100
    for line, code in enumerate([c for c in spec.tests if cat.by_sil[c].get("lisb")], start=1):
        t = cat.by_sil[code]
        out["req_test"].append([req_id, line, t["lisb"], t["procedure_pt"][:120]])
        for comp, r in _results_for(cat, spec, t, rng):
            if not comp.get("lisb"):
                continue
            rid += 1
            when = at + timedelta(hours=3, minutes=rng.randrange(0, 120))
            if r["kind"] == "NM":
                rr = (f"{r['low']}-{r['high']}" if r["low"] is not None and r["high"] is not None
                      else f"<{r['high']}" if r["low"] is None else f">{r['low']}")
                out["result"].append([rid, req_id, line, comp["lisb"], comp["name_pt"][:120], "NM", r["value"], None,
                                      None, None, comp["units"]["lisb"], rr, r["flag"],
                                      "F" if spec.final else "P", when, rng.choice(VALIDATORS)])
            else:
                it = r["item"]
                out["result"].append([rid, req_id, line, comp["lisb"], comp["name_pt"][:120], "CE", None, None,
                                      it["local"]["lisb"], it["pt_pt"], None, None, r["flag"],
                                      "F" if spec.final else "P", when, rng.choice(VALIDATORS)])
    return out


LISB_COLS = {
    "patient": "patient_id,mrn,national_id,given_name,family_name,birth_date,sex",
    "requisition": "req_id,accession,patient_id,requested_at,collected_at,priority,status,requester,department,clinical_info",
    "req_test": "req_id,line_no,test_code,test_name",
    "result": ("result_id,req_id,line_no,analyte_code,analyte_name,value_type,num_value,operator,code_value,"
               "text_value,unit,ref_range,abnormal_flag,result_status,resulted_at,validated_by"),
}


# ── HL7 v2.5.1 ORU^R01 (external laboratory "LIS-C") ────────────────────────
V2_SECTION = {"HM": "HM", "CH": "CH", "BLB": "BLB", "MB": "MB", "SR": "SR"}


def _v2_esc(s: str) -> str:
    return (s.replace("\\", "\\E\\").replace("|", "\\F\\").replace("^", "\\S\\")
            .replace("&", "\\T\\").replace("~", "\\R\\"))


def oru_message(cat: Catalogue, spec: OrderSpec, rng: random.Random, with_loinc: float = 0.5,
                ucum_units: float = 0.5) -> str:
    p = spec.person
    now = datetime(spec.day.year, spec.day.month, spec.day.day, 14, rng.randrange(0, 60))
    ctrl = f"LISC{spec.number:08d}"
    placer, filler = f"C-ORD-{spec.number:06d}", f"C-FIL-{spec.number:06d}"
    req = rng.choice(DOCTORS).replace("Dra. ", "").replace("Dr. ", "")
    segs = [
        f"MSH|^~\\&|LISC|LAB-C^^EXAMPLE|FHIR-ENGINE|EXAMPLE|{now:%Y%m%d%H%M%S}||ORU^R01^ORU_R01|{ctrl}|P|2.5.1"
        "|||AL|NE||UNICODE UTF-8",
        f"PID|1||C{p.sns}^^^LISC^MR~{p.sns}^^^SNS^NNPRT||{_v2_esc(p.family)}^{_v2_esc(p.given)}||"
        f"{p.birth:%Y%m%d}|{p.sex}",
        f"ORC|RE|{placer}|{filler}||{'CM' if spec.final else 'IP'}||||{now:%Y%m%d%H%M%S}|||^{_v2_esc(req)}",
    ]
    obr_n = 0
    for code in spec.tests:
        t = cat.by_sil[code]
        if not t.get("lisc"):
            continue
        obr_n += 1
        status = "F" if spec.final else "P"
        segs.append(f"OBR|{obr_n}|{placer}|{filler}-{obr_n}|{t['lisc']}^{_v2_esc(t['procedure_pt'][:60])}^99LISC"
                    f"|||{spec.day:%Y%m%d}0800|||||||||^{_v2_esc(req)}||||||{now:%Y%m%d%H%M%S}||"
                    f"{V2_SECTION.get(t['category'], 'LAB')}|{status}")
        for i, (comp, r) in enumerate(_results_for(cat, spec, t, rng), start=1):
            if not comp.get("lisc"):
                continue
            ident = f"{comp['lisc']}^{_v2_esc(comp['name_pt'][:60])}^99LISC"
            if rng.random() < with_loinc:
                ident += f"^{comp['loinc']}^{_v2_esc(comp['loinc_lcn'][:60])}^LN"
            if r["kind"] == "NM":
                u = comp["units"]["lisc"]
                units = f"{u}^{_v2_esc(u)}^UCUM" if rng.random() < ucum_units else f"{_v2_esc(u)}^{_v2_esc(u)}^99LISC"
                rr = (f"{r['low']}-{r['high']}" if r["low"] is not None and r["high"] is not None
                      else f"<{r['high']}" if r["low"] is None else f">{r['low']}")
                segs.append(f"OBX|{i}|NM|{ident}||{r['value']}|{units}|{rr}|{r['flag']}|||{status}|||"
                            f"{now:%Y%m%d%H%M%S}")
            else:
                it = r["item"]
                val = f"{it['local']['lisc']}^{_v2_esc(it['pt_pt'][:60])}^99LISC"
                segs.append(f"OBX|{i}|CWE|{ident}||{val}|||{r['flag'] or ''}|||{status}|||{now:%Y%m%d%H%M%S}")
    return "\r".join(segs) + "\r"
