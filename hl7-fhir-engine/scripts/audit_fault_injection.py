#!/usr/bin/env python3
"""
audit_fault_injection.py - show on the running stack that the audit policy holds.

Scenarios (the database faults are injected by revoking a privilege of
audit_writer as the owner role, and always restored at the end):

  A  exchange_log not writable  -> synchronisation refused (HTTP 503),
                                   ORU over HTTP refused (503, ACK AE),
                                   ORU over MLLP answered with ACK AE,
                                   and HAPI receives nothing (the
                                   ServiceRequest keeps its version)
  B  security_event not writable -> reads still served, the access record
                                   goes to the local outbox and is replayed
                                   into Postgres once the fault is removed
  C  (--hapi-outage) HAPI stopped -> synchronisation fails and the audit
                                   records the outcome 'error'

  python scripts/audit_fault_injection.py [--hapi-outage]

Writes fault_injection/fault_injection.csv and .md.
"""
from __future__ import annotations

import argparse
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ENGINE_URL, HAPI_URL, MIDDLEWARE_URL, ROOT, HttpError, http, log, login,  # noqa: E402
                    markdown_table, psql, require_docker, results_dir, wait_for, write_csv)

ORDER = "PED-2024-00002"
ORU_FILE = ROOT / "samples" / "oru" / "oru_r01_03.hl7"
rows = []


def record(scenario, check, expected, observed, passed):
    rows.append({"scenario": scenario, "check": check, "expected": expected, "observed": str(observed)[:160],
                 "pass": "PASS" if passed else "FAIL"})
    print(f"{'PASS' if passed else 'FAIL'}  [{scenario}] {check}: {observed}")


def call(method, path, token, body=None, headers=None):
    try:
        return 200, http(method, f"{MIDDLEWARE_URL}{path}", body, token=token, headers=headers)
    except HttpError as e:
        return e.status, e.body


def sr_version(sr_id):
    try:
        return http("GET", f"{HAPI_URL}/ServiceRequest/{sr_id}")["meta"]["versionId"]
    except HttpError as e:
        return f"HTTP {e.status}"


def mllp_ack(er7, host="localhost", port=2575):
    with socket.create_connection((host, port), timeout=60) as s:
        s.sendall(b"\x0b" + er7.encode() + b"\x1c\x0d")
        buf = b""
        while b"\x1c\x0d" not in buf:
            buf += s.recv(65536)
    return buf[1:buf.index(b"\x1c\x0d")].decode()


def count(table):
    return int(psql(f"SELECT count(*) FROM audit.{table};").stdout.strip())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hapi-outage", action="store_true", help="also stop and restart the HAPI container")
    a = ap.parse_args()
    require_docker()
    wait_for(f"{MIDDLEWARE_URL}/health", label="middleware")
    admin, medico = login("admin"), login("medico")
    er7 = re.sub(r"\r\n|\n|\r", "\r", ORU_FILE.read_text(encoding="utf-8").strip()) + "\r"
    v2 = {"Content-Type": "x-application/hl7-v2+er7"}

    bundle = http("GET", f"{ENGINE_URL}/fhir/bundle/sil/order/{ORDER}")
    sr_id = next(e["resource"]["id"] for e in bundle["entry"] if e["resource"]["resourceType"] == "ServiceRequest")
    st, _ = call("POST", f"/sync/sil/order/{ORDER}", admin)
    record("baseline", "normal synchronisation", "200", st, st == 200)
    v_before, n_before = sr_version(sr_id), count("exchange_log")

    # ── A: intent row cannot be written ────────────────────────────────────
    try:
        psql("REVOKE INSERT ON audit.exchange_log FROM audit_writer;")
        st, body = call("POST", f"/sync/sil/order/{ORDER}", admin)
        record("A", "sync refused", "503 AUDIT_UNAVAILABLE", f"{st} {body.get('code') if isinstance(body, dict) else ''}",
               st == 503 and isinstance(body, dict) and body.get("code") == "AUDIT_UNAVAILABLE")
        st, body = call("POST", "/api/v2/oru", admin, er7, v2)
        record("A", "ORU over HTTP refused", "503, ACK AE", f"{st} {body.get('ack_code') if isinstance(body, dict) else ''}",
               st == 503 and isinstance(body, dict) and body.get("ack_code") == "AE")
        ack = mllp_ack(er7)
        msa = next((s for s in ack.split("\r") if s.startswith("MSA")), "")
        record("A", "ORU over MLLP answered", "MSA|AE", msa[:60], msa.startswith("MSA|AE"))
    finally:
        psql("GRANT INSERT ON audit.exchange_log TO audit_writer;")
    v_after, n_after = sr_version(sr_id), count("exchange_log")
    record("A", "HAPI ServiceRequest version unchanged", v_before, v_after, v_before == v_after)
    record("A", "no exchange rows written", n_before, n_after, n_before == n_after)
    n_err = int(psql("SELECT count(*) FROM audit.security_event WHERE level='ERROR' "
                     "AND message LIKE '%fail-closed%' AND occurred_at > now() - interval '5 minutes';").stdout)
    record("A", "refusals recorded as security events", ">= 1", n_err, n_err >= 1)

    # ── B: access record falls back to the outbox ──────────────────────────
    sec_before = count("security_event")
    try:
        psql("REVOKE INSERT ON audit.security_event FROM audit_writer;")
        st, _ = call("GET", "/fhir/Patient?limit=5", medico)
        record("B", "read served while the access log table is unavailable", "200", st, st == 200)
        pending = http("GET", f"{MIDDLEWARE_URL}/health")["audit_outbox_pending"]
        record("B", "access record kept in the local outbox", ">= 1", pending, pending >= 1)
    finally:
        psql("GRANT INSERT ON audit.security_event TO audit_writer;")
    t0 = time.time()
    while time.time() - t0 < 90:
        pending = http("GET", f"{MIDDLEWARE_URL}/health")["audit_outbox_pending"]
        if pending == 0:
            break
        time.sleep(3)
    record("B", "outbox replayed into Postgres", "0 pending", f"{pending} pending after {time.time() - t0:.0f} s",
           pending == 0)
    sec_after = count("security_event")
    record("B", "security events after replay", f"> {sec_before}", sec_after, sec_after > sec_before)

    # ── C: HAPI unavailable ─────────────────────────────────────────────────
    if a.hapi_outage:
        subprocess.run(["docker", "stop", "hapi-fhir"], check=True, capture_output=True)
        try:
            st, body = call("POST", f"/sync/sil/order/{ORDER}", admin)
            record("C", "sync fails while HAPI is down", "5xx", st, st >= 500)
            last = psql("SELECT status FROM audit.exchange_log WHERE phase='outcome' "
                        "ORDER BY occurred_at DESC LIMIT 1;").stdout.strip()
            record("C", "outcome recorded", "error", last, last == "error")
        finally:
            subprocess.run(["docker", "start", "hapi-fhir"], check=True, capture_output=True)
            log("HAPI restarting; waiting for /metadata")
            wait_for(f"{HAPI_URL}/metadata", timeout=600, label="HAPI FHIR")

    out = results_dir("fault_injection")
    write_csv(out / "fault_injection.csv", rows)
    (out / "fault_injection.md").write_text(markdown_table(rows), encoding="utf-8")
    failed = sum(r["pass"] == "FAIL" for r in rows)
    log(f"{len(rows) - failed}/{len(rows)} checks passed -> {out}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
