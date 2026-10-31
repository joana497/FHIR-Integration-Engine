#!/usr/bin/env python3
"""
generate_orders.py - bulk synthetic laboratory orders for the measurements.

Uses the same catalogue and value generator as the fixed seed
(catalogue/synth.py), so every generated order is codable by the
ConceptMap. Generated records use their own identifier ranges and can be
removed again with --delete:

  SIL     numpedido PED-SYN-nnnnn, numexame EX-SYN-nnnnn  (database sildb)
  LIS-B   req_id / patient_id >= 10001                    (database lisbdb)
  ORU     files results/<run>/oru/oru_syn_nnnnn.hl7 (send with send_oru_mllp.py)

The rows are written as the owner role (sil_user), i.e. acting as the
legacy system itself; the engine keeps reading through its SELECT-only
roles.

Examples
  python scripts/generate_orders.py --sil 500 --lisb 200 --oru 100
  python scripts/generate_orders.py --sil 500 --sql-only      # only write the .sql files
  python scripts/generate_orders.py --delete                  # remove all generated rows
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, log, psql, require_docker, results_dir, write_json  # noqa: E402

sys.path.insert(0, str(ROOT / "catalogue"))
import synth  # noqa: E402
from synth import Catalogue, OrderSpec, sql_inserts  # noqa: E402

SIL_PREFIX, SIL_EXAM_PREFIX = "PED-SYN-", "EX-SYN-"
LISB_START = 10001
# Coded results need a forced value only when the test should look realistic;
# otherwise the value set is sampled at random.
TEST_WEIGHTS = {"HEM01": 8, "GLI01": 6, "CRE01": 5, "URE01": 3, "NA01": 4, "K01": 4, "PCR01": 4, "COL01": 3,
                "HDL01": 2, "LDL01": 2, "TRG01": 2, "TSH01": 3, "HBA1C": 3, "ALT01": 3, "AST01": 3, "PSA01": 2,
                "PRL01": 1, "TP01": 2, "FER01": 2, "GS01": 1, "URO01": 2, "VIH01": 1}


def pick_tests(cat: Catalogue, source: str, rng: random.Random) -> list[str]:
    avail = [t for t in cat.tests_for(source) if t in TEST_WEIGHTS]
    k = rng.choices([1, 2, 3, 4], weights=[4, 3, 2, 1])[0]
    tests: list[str] = []
    while len(tests) < k:
        t = rng.choices(avail, weights=[TEST_WEIGHTS[x] for x in avail])[0]
        if t not in tests:
            tests.append(t)
    return tests


def sil_sql(cat: Catalogue, n: int, rng: random.Random, start_day: date) -> tuple[str, list[str]]:
    rows = {"dpedidos": [], "pedidos": [], "dexames": [], "exames": [], "resultados": []}
    ids = []
    for i in range(1, n + 1):
        spec = OrderSpec(i, synth.person(rng, 800000, i), pick_tests(cat, "sil", rng),
                         start_day + timedelta(days=rng.randrange(0, 300)), urgent=rng.random() < 0.1,
                         final=rng.random() < 0.9, abnormal=0.15)
        r = synth.sil_order_rows(cat, spec, rng, prefix=SIL_PREFIX, exam_prefix=SIL_EXAM_PREFIX)
        for k in rows:
            rows[k].extend(r[k])
        ids.append(r["dpedidos"][0][0])
    parts = ["BEGIN;"]
    parts.append(sql_inserts("sil.dpedidos", synth.DPEDIDOS_COLS, rows["dpedidos"]))
    parts.append(sql_inserts("sil.pedidos", synth.PEDIDOS_COLS, rows["pedidos"]))
    parts.append(sql_inserts("sil.dexames", synth.DEXAMES_COLS, rows["dexames"]))
    parts.append(sql_inserts("sil.exames", synth.EXAMES_COLS, rows["exames"]))
    parts.append(sql_inserts("sil.resultados", synth.RESULTADOS_COLS, rows["resultados"]))
    parts.append("COMMIT;")
    return "\n".join(parts), ids


def lisb_sql(cat: Catalogue, n: int, rng: random.Random, start_day: date) -> tuple[str, list[str]]:
    rows = {k: [] for k in synth.LISB_COLS}
    people = max(1, n * 2 // 3)  # some patients have more than one requisition
    seen, ids = set(), []
    for i in range(n):
        number = LISB_START + i
        pi = rng.randrange(people)
        spec = OrderSpec(number, synth.person(random.Random(pi), 900000, pi), pick_tests(cat, "lisb", rng),
                         start_day + timedelta(days=rng.randrange(0, 300)), urgent=rng.random() < 0.1,
                         final=rng.random() < 0.9)
        r = synth.lisb_order_rows(cat, spec, rng, patient_id=LISB_START + pi, new_patient=pi not in seen)
        seen.add(pi)
        for k in rows:
            rows[k].extend(r[k])
        ids.append(r["requisition"][0][1])
    parts = ["BEGIN;"] + [sql_inserts(f"lis.{t}", synth.LISB_COLS[t], rows[t])
                          for t in ("patient", "requisition", "req_test", "result")] + ["COMMIT;"]
    return "\n".join(parts), ids


def oru_files(cat: Catalogue, n: int, rng: random.Random, start_day: date, out: Path) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    names = []
    for i in range(n):
        number = 1001 + i
        spec = OrderSpec(number, synth.person(rng, 950000, i), pick_tests(cat, "lisc", rng),
                         start_day + timedelta(days=rng.randrange(0, 300)), final=rng.random() < 0.9)
        msg = synth.oru_message(cat, spec, rng, with_loinc=0.5, ucum_units=0.5)
        p = out / f"oru_syn_{number:05d}.hl7"
        p.write_text(msg.replace("\r", "\n"), encoding="utf-8")
        names.append(p.name)
    return names


DELETE_SQL = {
    "sildb": f"""BEGIN;
DELETE FROM sil.resultados WHERE numexame LIKE '{SIL_EXAM_PREFIX}%';
DELETE FROM sil.exames     WHERE numexame LIKE '{SIL_EXAM_PREFIX}%';
DELETE FROM sil.dexames    WHERE numexame LIKE '{SIL_EXAM_PREFIX}%';
DELETE FROM sil.pedidos    WHERE numpedido LIKE '{SIL_PREFIX}%';
DELETE FROM sil.dpedidos   WHERE numpedido LIKE '{SIL_PREFIX}%';
COMMIT;""",
    "lisbdb": f"""BEGIN;
DELETE FROM lis.result      WHERE req_id >= {LISB_START};
DELETE FROM lis.req_test    WHERE req_id >= {LISB_START};
DELETE FROM lis.requisition WHERE req_id >= {LISB_START};
DELETE FROM lis.patient     WHERE patient_id >= {LISB_START};
COMMIT;""",
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sil", type=int, default=0, help="number of SIL orders")
    ap.add_argument("--lisb", type=int, default=0, help="number of LIS-B requisitions")
    ap.add_argument("--oru", type=int, default=0, help="number of ORU^R01 message files")
    ap.add_argument("--seed", type=int, default=7, help="random seed (default 7)")
    ap.add_argument("--sql-only", action="store_true", help="write the .sql files but do not load them")
    ap.add_argument("--delete", action="store_true", help="remove previously generated rows and exit")
    a = ap.parse_args()

    if a.delete:
        require_docker()
        for db, q in DELETE_SQL.items():
            psql(q, db=db)
        log("generated SIL and LIS-B rows removed")
        return

    if a.sil > 99999 or a.lisb > 89999:
        sys.exit("too many orders for the identifier ranges")
    cat, rng, start = Catalogue(), random.Random(a.seed), date(2024, 3, 1)
    out = results_dir("generated")
    manifest = {"seed": a.seed, "sil": [], "lisb": [], "oru": []}
    if (a.sil or a.lisb) and not a.sql_only:
        require_docker()
        for db, q in DELETE_SQL.items():  # idempotent: regenerate from scratch
            psql(q, db=db)
    if a.sil:
        q, manifest["sil"] = sil_sql(cat, a.sil, rng, start)
        (out / "sil_generated.sql").write_text(q, encoding="utf-8")
        if not a.sql_only:
            psql(q, db="sildb")
        log(f"SIL: {a.sil} orders {'written' if a.sql_only else 'loaded'}")
    if a.lisb:
        q, manifest["lisb"] = lisb_sql(cat, a.lisb, rng, start)
        (out / "lisb_generated.sql").write_text(q, encoding="utf-8")
        if not a.sql_only:
            psql(q, db="lisbdb")
        log(f"LIS-B: {a.lisb} requisitions {'written' if a.sql_only else 'loaded'}")
    if a.oru:
        manifest["oru"] = oru_files(cat, a.oru, rng, start, out / "oru")
        log(f"ORU: {a.oru} messages in {out / 'oru'}")
    write_json(out / "manifest.json", manifest)
    log(f"manifest: {out / 'manifest.json'}")


if __name__ == "__main__":
    main()
