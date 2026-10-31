#!/usr/bin/env python3
"""
check_db_roles.py - evidence for requirement R1 (the engine never writes to a legacy system).

Connects as each application role, with that role's own password, and
attempts allowed and forbidden operations. Every write is attempted
twice for the read-only roles: once in a normal session (stopped by the
read-only transaction default) and once with that default switched off
(stopped by the missing privilege), so both layers are shown to hold.
Allowed writes of audit_writer run inside BEGIN ... ROLLBACK.

  python scripts/check_db_roles.py                 # through the sil-postgres container
  PSQL_DIRECT=1 PGHOST=localhost PGPORT=5432 python scripts/check_db_roles.py   # local psql client

Writes db_roles/db_roles.csv and db_roles/db_roles.md.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import PG_CONTAINER, log, markdown_table, results_dir, write_csv  # noqa: E402

PASSWORDS = {"sil_reader": "sil_reader_pass", "lisb_reader": "lisb_reader_pass", "audit_writer": "audit_writer_pass"}
RO_OFF = "SET default_transaction_read_only = off;\n"
DENIED = r"permission denied"
READONLY = r"read-only transaction"
NOCONNECT = r"permission denied for database|not have CONNECT"

CHECKS = [  # role, database, operation, SQL, expected ('allow'|'deny'), reason regex for a denial
    ("sil_reader", "sildb", "SELECT sil.dpedidos", "SELECT count(*) FROM sil.dpedidos;", "allow", None),
    ("sil_reader", "sildb", "SELECT sil.resultados", "SELECT count(*) FROM sil.resultados;", "allow", None),
    ("sil_reader", "sildb", "INSERT sil.dpedidos (default session)",
     "INSERT INTO sil.dpedidos (numpedido) VALUES ('R1-TEST');", "deny", READONLY),
    ("sil_reader", "sildb", "INSERT sil.dpedidos (read-only guard off)",
     RO_OFF + "INSERT INTO sil.dpedidos (numpedido) VALUES ('R1-TEST');", "deny", DENIED),
    ("sil_reader", "sildb", "UPDATE sil.dpedidos (read-only guard off)",
     RO_OFF + "UPDATE sil.dpedidos SET observacoes = 'x';", "deny", DENIED),
    ("sil_reader", "sildb", "DELETE sil.resultados (read-only guard off)",
     RO_OFF + "DELETE FROM sil.resultados;", "deny", DENIED),
    ("sil_reader", "sildb", "TRUNCATE sil.exames (read-only guard off)",
     RO_OFF + "TRUNCATE sil.exames;", "deny", DENIED),
    ("sil_reader", "sildb", "CREATE TABLE in schema sil (read-only guard off)",
     RO_OFF + "CREATE TABLE sil.r1_test (x int);", "deny", DENIED),
    ("sil_reader", "sildb", "CREATE TABLE in schema public (read-only guard off)",
     RO_OFF + "CREATE TABLE public.r1_test (x int);", "deny", DENIED),
    ("sil_reader", "sildb", "SELECT audit.exchange_log", "SELECT count(*) FROM audit.exchange_log;", "deny", DENIED),
    ("sil_reader", "lisbdb", "CONNECT lisbdb", "SELECT 1;", "deny", NOCONNECT),
    ("lisb_reader", "lisbdb", "SELECT lis.result", "SELECT count(*) FROM lis.result;", "allow", None),
    ("lisb_reader", "lisbdb", "INSERT lis.patient (default session)",
     "INSERT INTO lis.patient (patient_id, mrn) VALUES (-1, 'R1');", "deny", READONLY),
    ("lisb_reader", "lisbdb", "INSERT lis.patient (read-only guard off)",
     RO_OFF + "INSERT INTO lis.patient (patient_id, mrn) VALUES (-1, 'R1');", "deny", DENIED),
    ("lisb_reader", "lisbdb", "UPDATE lis.result (read-only guard off)",
     RO_OFF + "UPDATE lis.result SET unit = 'x';", "deny", DENIED),
    ("lisb_reader", "lisbdb", "DELETE lis.requisition (read-only guard off)",
     RO_OFF + "DELETE FROM lis.requisition;", "deny", DENIED),
    ("lisb_reader", "sildb", "CONNECT sildb", "SELECT 1;", "deny", NOCONNECT),
    ("audit_writer", "sildb", "INSERT audit.security_event (rolled back)",
     "BEGIN; INSERT INTO audit.security_event (level, message) VALUES ('INFO', 'role check'); ROLLBACK;", "allow", None),
    ("audit_writer", "sildb", "SELECT audit.exchange_summary", "SELECT count(*) FROM audit.exchange_summary;", "allow", None),
    ("audit_writer", "sildb", "UPDATE audit.exchange_log", "UPDATE audit.exchange_log SET status = 'ok';", "deny", DENIED),
    ("audit_writer", "sildb", "DELETE audit.security_event", "DELETE FROM audit.security_event;", "deny", DENIED),
    ("audit_writer", "sildb", "TRUNCATE audit.exchange_log", "TRUNCATE audit.exchange_log;", "deny", DENIED),
    ("audit_writer", "sildb", "UPDATE terminology cache (rolled back)",
     "BEGIN; UPDATE audit.terminology_validation SET times_seen = times_seen; ROLLBACK;", "allow", None),
    ("audit_writer", "sildb", "EXECUTE audit.purge_old_payloads() (rolled back)",
     "BEGIN; SELECT audit.purge_old_payloads(); ROLLBACK;", "allow", None),
    ("audit_writer", "sildb", "SELECT sil.dpedidos (clinical data)", "SELECT count(*) FROM sil.dpedidos;", "deny", DENIED),
    ("audit_writer", "lisbdb", "CONNECT lisbdb", "SELECT 1;", "deny", NOCONNECT),
]


def run_as(role: str, db: str, sql: str) -> subprocess.CompletedProcess:
    pw = PASSWORDS[role]
    if os.environ.get("PSQL_DIRECT"):
        cmd = ["psql", "-h", os.environ.get("PGHOST", "localhost"), "-p", os.environ.get("PGPORT", "5432"),
               "-U", role, "-d", db, "-v", "ON_ERROR_STOP=1", "-qtA"]
        env = {**os.environ, "PGPASSWORD": pw}
    else:
        cmd = ["docker", "exec", "-i", "-e", f"PGPASSWORD={pw}", PG_CONTAINER,
               "psql", "-h", "127.0.0.1", "-U", role, "-d", db, "-v", "ON_ERROR_STOP=1", "-qtA"]
        env = None
    return subprocess.run(cmd, input=sql, text=True, capture_output=True, env=env)


def main() -> None:
    rows, ok = [], 0
    for role, db, op, sql, expected, reason in CHECKS:
        r = run_as(role, db, sql)
        err = next((ln for ln in r.stderr.splitlines() if "ERROR" in ln or "FATAL" in ln), r.stderr.strip())
        observed = "allow" if r.returncode == 0 else "deny"
        passed = observed == expected and (expected == "allow" or bool(re.search(reason, err, re.I)))
        ok += passed
        rows.append({"role": role, "database": db, "operation": op, "expected": expected, "observed": observed,
                     "pass": "PASS" if passed else "FAIL",
                     "server_message": re.sub(r"^.*?(ERROR|FATAL):\s*", "", err)[:160]})
        print(f"{'PASS' if passed else 'FAIL'}  {role:12s} {db:7s} {op:52s} {observed}")
    out = results_dir("db_roles")
    write_csv(out / "db_roles.csv", rows)
    (out / "db_roles.md").write_text(markdown_table(rows), encoding="utf-8")
    log(f"{ok}/{len(rows)} privilege checks passed -> {out}")
    sys.exit(0 if ok == len(rows) else 1)


if __name__ == "__main__":
    main()
