#!/usr/bin/env python3
"""
build_seed.py - write the fixed demonstration seed (deterministic, seed 2024).

Outputs
  postgres/02b_seed_lab.sql   result values for the original SIL orders,
                              report conclusions, and ten new laboratory orders
  postgres/06_lisb_seed.sql   the second SQL system (LIS-B): 6 patients, 8 requisitions
  samples/oru/*.hl7           five HL7 v2.5.1 ORU^R01 messages from an external laboratory

Run after tools/build_conceptmap.py (it reads catalogue/catalogue_resolved.json).
"""
from __future__ import annotations

import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "catalogue"))
import synth  # noqa: E402
from synth import Catalogue, OrderSpec, Person, sql_inserts  # noqa: E402

SEED = 2024
cat = Catalogue()
rng = random.Random(SEED)

# ── 1. Results for the laboratory lines of the original 15 orders ──────────
EXISTING = [  # numexame, idser, grupo, [(ordem, test)], day, force
    ("EX-2024-00002", "LAB1", "GLAB", [(1, "HEM01"), (2, "GLI01")], date(2024, 1, 13), None),
    ("EX-2024-00007", "LAB1", "GLAB", [(1, "PSA01"), (2, "COL01"), (3, "HDL01"), (4, "LDL01"), (5, "TRG01")],
     date(2024, 1, 23), None),
    ("EX-2024-00014", "MIC1", "GLAB", [(1, "URO01")], date(2024, 2, 8), {"BACT": "ECOLI", "EPIT": "PRES"}),
]
CONCLUSIONS = [  # synthetic report text for imaging, ECG and pathology examinations
    ("EX-2024-00001", "RX01", "GRX", "RXT01", "Dr. Silva", "Sem alterações pleuroparenquimatosas. Seios costofrénicos livres."),
    ("EX-2024-00003", "CAR1", "GECG", "ECG01", "Dr. Rodrigues", "Ritmo sinusal, 88 bpm. Sem alterações da repolarização."),
    ("EX-2024-00004", "ECO1", "GECO", "ECA01", "Dra. Oliveira", "Fígado de dimensões normais e ecoestrutura homogénea. Vesícula sem litíase."),
    ("EX-2024-00006", "RMN1", "GRMN", "RMJ01", "Dra. Nunes", "Rotura do corno posterior do menisco interno."),
    ("EX-2024-00008", "RX01", "GRX", "RXP01", "Dra. Carvalho", "Fratura do rádio distal sem desvio."),
    ("EX-2024-00010", "HEM1", "GCAT", "COR01", "Dr. Azevedo", "Doença coronária de um vaso: estenose de 80% da DA proximal."),
    ("EX-2024-00012", "ANA1", "GANA", "BIO01", "Dr. Baptista", "Adenocarcinoma acinar da próstata, Gleason 3+4=7 (grupo de grau 2)."),
    ("EX-2024-00015", "TAC1", "GTAC", "TCT01", "Dra. Magalhães", "Nódulo pulmonar sólido de 8 mm no lobo superior direito."),
]

res_rows = []
for numexame, idser, grupo, lines, day, force in EXISTING:
    spec = OrderSpec(0, Person("x", "x", "F", date(1980, 1, 1), "0"), [t for _, t in lines], day, force=force)
    validator = rng.choice(synth.VALIDATORS)
    for ordem, code in lines:
        t = cat.by_sil[code]
        for seq, (comp, r) in enumerate(synth._results_for(cat, spec, t, rng), start=1):
            when = datetime(day.year, day.month, day.day, 10, rng.randrange(0, 60))
            if r["kind"] == "NM":
                res_rows.append([idser, grupo, numexame, code, 1, ordem, seq, comp["sil"], comp["name_pt"][:200], "NM",
                                 r["value"], None, comp["units"]["sil"], None, None, r["low"], r["high"],
                                 r["ref_text"], r["flag"], "F", when, validator])
            else:
                it = r["item"]
                res_rows.append([idser, grupo, numexame, code, 1, ordem, seq, comp["sil"], comp["name_pt"][:200], "CE",
                                 None, None, None, it["local"]["sil"], it["pt_pt"], None, None, None, r["flag"], "F",
                                 when, validator])
for numexame, idser, grupo, code, medico, text in CONCLUSIONS:
    res_rows.append([idser, grupo, numexame, code, 1, 1, 1, "CONCL", "Conclusão", "CO", None, None, None, None, text,
                     None, None, None, None, "F", datetime(2024, 2, 14, 12, 0), medico])

# ── 2. Ten new laboratory orders (PED-2024-00016 .. 00025) ─────────────────
NEW = [  # tests, urgent, final, abnormal, force
    (["CRE01", "URE01", "NA01", "K01"], False, True, 0.2, None),
    (["TSH01", "PRL01"], False, True, 0.15, None),
    (["HBA1C", "GLI01"], False, True, 0.5, None),
    (["ALT01", "AST01", "PCR01"], True, True, 0.3, None),
    (["GS01"], False, True, 0.0, {"ABO": "A", "RH": "RH+"}),
    (["TP01", "FER01"], False, True, 0.15, None),
    (["VIH01"], False, True, 0.0, {"VIH12": "NEG"}),
    (["HEM01", "FER01"], False, True, 0.6, None),
    (["URO01"], False, True, 0.0, {"BACT": "SEMCRESC", "EPIT": "AUS"}),
    (["HEM01"], False, False, 0.1, None),
]
new_rows = {"dpedidos": [], "pedidos": [], "dexames": [], "exames": [], "resultados": []}
for i, (tests, urgent, final, abn, force) in enumerate(NEW):
    number = 16 + i
    spec = OrderSpec(number, synth.person(rng, 300000, number), tests, date(2024, 2, 9) + timedelta(days=i),
                     urgent=urgent, final=final, abnormal=abn, force=force)
    rows = synth.sil_order_rows(cat, spec, rng)
    for k in new_rows:
        new_rows[k].extend(rows[k])

out = ["-- ============================================================",
       "-- 02b_seed_lab.sql - GENERATED by tools/build_seed.py (seed 2024).",
       "-- Synthetic laboratory data: result values for the original orders,",
       "-- report conclusions, and ten new laboratory orders (00016-00025).",
       "-- All people and values are fictitious. Do not edit by hand.",
       "-- ============================================================", ""]
out.append(sql_inserts("sil.dpedidos", synth.DPEDIDOS_COLS, new_rows["dpedidos"]))
out.append(sql_inserts("sil.pedidos", synth.PEDIDOS_COLS, new_rows["pedidos"]))
out.append(sql_inserts("sil.dexames", synth.DEXAMES_COLS, new_rows["dexames"]))
out.append(sql_inserts("sil.exames", synth.EXAMES_COLS, new_rows["exames"]))
out.append(sql_inserts("sil.resultados", synth.RESULTADOS_COLS, res_rows + new_rows["resultados"]))
(ROOT / "postgres" / "02b_seed_lab.sql").write_text("\n".join(out), encoding="utf-8")

# ── 3. LIS-B: 6 patients, 8 requisitions ────────────────────────────────────
lrows = {k: [] for k in synth.LISB_COLS}
lisb_people = [synth.person(rng, 500000, k) for k in range(1, 7)]
LISB = [  # patient index, tests, urgent, final, force
    (0, ["HEM01", "GLI01"], False, True, None),
    (1, ["CRE01", "NA01", "K01"], False, True, None),
    (2, ["TSH01"], False, True, None),
    (3, ["GS01"], False, True, {"ABO": "O", "RH": "RH-"}),
    (4, ["PCR01", "HEM01"], True, True, None),
    (5, ["VIH01"], False, True, {"VIH12": "NEG"}),
    (0, ["GLI01", "CRE01"], False, True, None),
    (1, ["HEM01"], False, False, None),
]
seen = set()
for n, (pi, tests, urgent, final, force) in enumerate(LISB, start=1):
    spec = OrderSpec(n, lisb_people[pi], tests, date(2024, 3, 4) + timedelta(days=n), urgent=urgent, final=final,
                     force=force)
    rows = synth.lisb_order_rows(cat, spec, rng, patient_id=pi + 1, new_patient=pi not in seen)
    seen.add(pi)
    for k in lrows:
        lrows[k].extend(rows[k])
lout = ["-- 06_lisb_seed.sql - GENERATED by tools/build_seed.py (seed 2024). Synthetic data.",
        "\\connect lisbdb", ""]
for table in ("patient", "requisition", "req_test", "result"):
    lout.append(sql_inserts(f"lis.{table}", synth.LISB_COLS[table], lrows[table]))
(ROOT / "postgres" / "06_lisb_seed.sql").write_text("\n".join(lout), encoding="utf-8")

# ── 4. Sample ORU^R01 messages ──────────────────────────────────────────────
sdir = ROOT / "samples" / "oru"
sdir.mkdir(parents=True, exist_ok=True)
ORU = [(["HEM01"], True), (["GLI01", "HBA1C"], True), (["GS01"], True), (["URO01"], True), (["TSH01", "PRL01"], False)]
for n, (tests, final) in enumerate(ORU, start=1):
    spec = OrderSpec(n, synth.person(rng, 700000, n), tests, date(2024, 4, 1) + timedelta(days=n), final=final,
                     force={"BACT": "ECOLI"} if "URO01" in tests else None)
    msg = synth.oru_message(cat, spec, rng, with_loinc=0.5 if n % 2 else 0.0, ucum_units=0.5)
    (sdir / f"oru_r01_{n:02d}.hl7").write_text(msg.replace("\r", "\n"), encoding="utf-8")

print(f"02b_seed_lab.sql: {len(new_rows['dpedidos'])} new orders, "
      f"{len(res_rows) + len(new_rows['resultados'])} result rows")
print(f"06_lisb_seed.sql: {len(lrows['patient'])} patients, {len(lrows['requisition'])} requisitions, "
      f"{len(lrows['result'])} results")
print(f"samples/oru: {len(ORU)} messages")
