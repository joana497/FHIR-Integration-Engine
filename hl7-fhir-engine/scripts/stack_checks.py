#!/usr/bin/env python3
"""
stack_checks.py - the Chapter 6 checks on the running stack that no other script covers.

  python3 scripts/stack_checks.py                 # every check below except c52
  python3 scripts/stack_checks.py --startup 5     # also c52: five cold starts (restarts the stack; run it last)
  python3 scripts/stack_checks.py --only c11,c24  # selected checks (c11 is always run when a later check needs it)

Run it on a clean stack holding only the seed data, before generate_orders.py:
  docker compose down -v && docker compose up -d --build

Checks, in the order they run:
  roles  each demo account against read, write, sync, audit and admin: 200 when permitted, 403 when not
  c11    pass 1: every order of the evaluation dataset through POST /sync/{source}/order/{id}, and every
         ORU^R01 sample through POST /api/v2/oru; HTTP status and the status of every entry in HAPI's reply
  c24    resource counts per type in HAPI, pass 2 (the same Bundles again), counts again
  c54    end-to-end time of each POST /sync in pass 2, with the terminology cache warm; median
  c31    GET of every resource HAPI reported in pass 1, directly on HAPI
  c32    searches by subject for each patient and by business identifier for each order, directly on HAPI,
         compared with the resources the engine generated for that patient and order
  c33    CapabilityStatement: fhirVersion 4.0.1, read and search-type for the five resource types
  c72    a test Observation whose LOINC code is invalid goes through POST /api/fhir/ingest; the stored copy
         must carry the validation-issue extension and keep all its codings (deleted afterwards)
  c73    the middleware is restarted with an unreachable terminology server and an empty terminology cache;
         pass 3 must store every Bundle and record no code as invalid; the middleware is then restored and
         pass 4 sends the dataset once more, so HAPI and the cache end as in normal use
  c53    peak combined memory of the five containers while this script runs (docker stats)
  c52    (only with --startup N) N cold starts: docker compose down, docker compose up -d, time until the
         middleware's /health and HAPI's /metadata both answer 200 (polled every second); median.
         Images must already be built; volumes are kept (with --startup-volumes they are removed, so
         every start initialises empty databases; this deletes all stored data). Also evidences C5.1

The evaluation dataset is the seed: SIL orders except generated ones (PED-SYN-...), LIS-B requisitions
numbered below 10001, and samples/oru/*.hl7. Output in stack_checks/: one CSV per check, stack_checks.json
and SUMMARY.md (criterion, value, threshold, status).

Side effects: the same resources are stored again in HAPI (idempotent upserts); audit rows are added;
c72 stores and deletes two test resources; c73 empties audit.terminology_validation and restarts the
middleware twice; c52 restarts the whole stack (with --startup-volumes it also deletes every volume).
Use it on the demonstration stack only. Python 3.9 or later, standard library only.
"""
from __future__ import annotations

import argparse
import copy
import re
import statistics
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ENGINE_URL, HAPI_URL, MIDDLEWARE_URL, ROOT, HttpError, http, log, login,  # noqa: E402
                    markdown_table, psql, results_dir, write_csv, write_json)

HERE = Path(__file__).resolve().parent
TYPES = ["Patient", "ServiceRequest", "ImagingStudy", "DiagnosticReport", "Observation"]
SEARCHED = ["ServiceRequest", "DiagnosticReport", "Observation"]
ISSUE_URL = "http://example.org/fhir/StructureDefinition/terminology-validation-issue"
LOINC = "http://loinc.org"
V2 = {"Content-Type": "x-application/hl7-v2+er7"}
FHIR = {"Content-Type": "application/fhir+json", "Accept": "application/fhir+json"}
OFFLINE_TX = "http://127.0.0.1:9/r4"   # nothing listens on port 9 inside the container: refused at once
PERMISSIONS = {  # what each role may do (node-middleware/src/services/authService.js)
    "admin": {"read", "write", "sync", "audit", "admin"},
    "medico": {"read"},
    "tecnico": {"read", "write", "sync"},
    "auditor": {"read", "audit"},
}
ENTRY_OK = re.compile(r"^20[01]\b")
LOCATION = re.compile(r"([A-Z][A-Za-z]+)/([A-Za-z0-9\-.]{1,64})/_history")

summary: list[dict] = []
raw: dict = {}


def add(criterion: str, value: str, threshold: str, status: str) -> None:
    summary.append({"criterion": criterion, "value": value, "threshold": threshold, "status": status})
    log(f"{criterion}: {value} [{status}]")


def call(method: str, url: str, token: str | None = None, body=None, headers: dict | None = None,
         timeout: float = 300):
    """(status, body, seconds). HTTP errors are returned, not raised; 2xx is reported as 200."""
    t0 = time.perf_counter()
    try:
        b, s = http(method, url, body, token=token, headers=headers, timeout=timeout), 200
    except HttpError as e:
        b, s = e.body, e.status
    except Exception as e:  # noqa: BLE001 - connection refused, timeout
        b, s = str(e), 0
    return s, b, time.perf_counter() - t0


def er7(path: Path) -> str:
    return re.sub(r"\r\n|\n|\r", "\r", path.read_text(encoding="utf-8").strip()) + "\r"


def wait_until(check, timeout: float, what: str, interval: float = 2.0) -> float:
    t0 = time.time()
    while True:
        try:
            if check():
                return time.time() - t0
        except Exception:  # noqa: BLE001
            pass
        if time.time() - t0 > timeout:
            raise SystemExit(f"timed out after {timeout:.0f} s waiting for {what}")
        time.sleep(interval)


def middleware_ready() -> bool:
    s, b, _ = call("GET", f"{MIDDLEWARE_URL}/health", timeout=10)
    return s == 200 and isinstance(b, dict) and b.get("audit_db") == "ready"


def hapi_ready() -> bool:
    return call("GET", f"{HAPI_URL}/metadata", headers=FHIR, timeout=10)[0] == 200


# ── Dataset and submission ───────────────────────────────────────────────────
def dataset(token: str, include_generated: bool, oru_dir: Path):
    sources = [s["name"] for s in http("GET", f"{MIDDLEWARE_URL}/api/sources", token=token)["database"]]
    orders = {}
    for src in sources:
        ids = http("GET", f"{MIDDLEWARE_URL}/api/sources/{src}/orders?limit=5000", token=token)["orders"]
        if not include_generated:
            ids = [i for i in ids if not i.startswith("PED-SYN-")
                   and not (src == "lisb" and i[-5:].isdigit() and int(i[-5:]) >= 10001)]
        orders[src] = ids
    return orders, sorted(oru_dir.glob("*.hl7"))


def call_limited(*args, **kw):
    """call(), waiting and retrying when the middleware's rate limit (RATE_LIMIT_PER_MIN) answers 429."""
    for attempt in range(4):
        s, b, sec = call(*args, **kw)
        if s != 429:
            return s, b, sec
        log("rate limit reached (HTTP 429); waiting 61 s - raise RATE_LIMIT_PER_MIN for large datasets")
        time.sleep(61)
    return s, b, sec


def submit_all(token: str, orders: dict, oru_files: list, label: str):
    """Send every Bundle of the dataset once; returns per-Bundle rows and every location HAPI reported."""
    rows, locations = [], []
    for source, ids in orders.items():
        for oid in ids:
            s, b, sec = call_limited("POST", f"{MIDDLEWARE_URL}/sync/{source}/order/{quote(oid, safe='')}", token)
            b = b if isinstance(b, dict) else {"error": str(b)}
            entries = (b.get("result") or {}).get("entry", [])
            st = [(e.get("response") or {}).get("status", "") for e in entries]
            locations += [(e.get("response") or {}).get("location") for e in entries]
            t = b.get("terminology") or {}
            rows.append({"pass": label, "source": source, "order": oid, "route": "POST /sync", "http": s,
                         "seconds": round(sec, 3), "entries_sent": b.get("entriesIn"), "entries": len(st),
                         "entries_ok": sum(bool(ENTRY_OK.match(x)) for x in st), "ack": "",
                         "tx_validated": t.get("validated"), "tx_unresolved": t.get("unresolved"),
                         "tx_offline": t.get("offline"), "tx_local": t.get("local"), "audit": b.get("audit"),
                         "error": "" if s == 200 else str(b.get("error") or b)[:200]})
    for f in oru_files:
        s, b, sec = call_limited("POST", f"{MIDDLEWARE_URL}/api/v2/oru", token, er7(f), V2)
        b = b if isinstance(b, dict) else {"error": str(b)}
        stages = ((b.get("trace") or {}).get("stages") or {})
        persist = stages.get("persist") or {}
        summ = persist.get("hapi_response_summary") or []
        st = [x.get("status", "") for x in summ]
        locations += [x.get("location") for x in summ]
        t = stages.get("terminology") or {}
        rows.append({"pass": label, "source": "oru", "order": f.name, "route": "POST /api/v2/oru", "http": s,
                     "seconds": round(sec, 3), "entries_sent": None, "entries": len(st),
                     "entries_ok": sum(bool(ENTRY_OK.match(x)) for x in st), "ack": b.get("ack_code"),
                     "tx_validated": t.get("validated"), "tx_unresolved": t.get("unresolved"),
                     "tx_offline": t.get("offline"), "tx_local": t.get("local"),
                     "audit": (b.get("trace") or {}).get("audit"), "error": b.get("error") or ""})
    for r in rows:
        r["accepted"] = (r["http"] == 200 and r["entries"] > 0 and r["entries_ok"] == r["entries"]
                         and (r["entries_sent"] in (None, r["entries"])) and r["ack"] in ("", "AA"))
    return rows, [x for x in locations if x]


def counts() -> dict:
    return {t: http("GET", f"{HAPI_URL}/{t}?_summary=count", headers=FHIR)["total"] for t in TYPES}


# ── Checks ───────────────────────────────────────────────────────────────────
def check_roles(out: Path, orders: dict, oru_files: list) -> None:
    sil = orders.get("sil") or next(iter(orders.values()))
    ops = {
        "read": ("GET", "/api/sources", None, None),
        "write": ("POST", "/api/v2/oru", er7(oru_files[0]) if oru_files else None, V2),
        "sync": ("POST", f"/sync/{'sil' if 'sil' in orders else next(iter(orders))}/order/{quote(sil[0], safe='')}",
                 None, None),
        "audit": ("GET", "/audit?limit=1", None, None),
        "admin": ("POST", "/terminology/revalidate", None, None),
    }
    rows = []
    for user, allowed in PERMISSIONS.items():
        tok = login(user)
        for perm, (m, path, body, hdr) in ops.items():
            s, _, _ = call(m, f"{MIDDLEWARE_URL}{path}", tok, body, hdr)
            expected = "allowed" if perm in allowed else "403"
            ok = (0 < s < 400) if perm in allowed else s == 403
            rows.append({"user": user, "permission": perm, "request": f"{m} {path.split('?')[0]}",
                         "expected": expected, "http": s, "as_expected": ok})
    write_csv(out / "roles.csv", rows)
    n_ok = sum(r["as_expected"] for r in rows)
    add("Role matrix (Table 6.2)", f"{n_ok} of {len(rows)} requests answered as the permission matrix requires",
        "all", "Met" if n_ok == len(rows) else "Not met")
    raw["roles"] = rows


def check_c11(rows: list) -> None:
    acc = [r for r in rows if r["accepted"]]
    entries = sum(r["entries"] for r in rows)
    ok_entries = sum(r["entries_ok"] for r in rows)
    per = defaultdict(lambda: [0, 0])
    for r in rows:
        per[r["source"]][1] += 1
        per[r["source"]][0] += r["accepted"]
    detail = ", ".join(f"{s} {a}/{n}" for s, (a, n) in per.items())
    add("C1.1 Acceptance", f"{len(acc)} of {len(rows)} Bundles accepted ({detail}); {ok_entries} of {entries} "
        f"entries with status 200 or 201", "N of N Bundles", "Met" if len(acc) == len(rows) else "Not met")


def check_c24(before: dict, after: dict) -> None:
    diff = {t: after[t] - before[t] for t in TYPES}
    extra = sum(diff.values())
    add("C2.4 Idempotency", f"{extra} additional resources after resubmission ("
        + ", ".join(f"{t} {before[t]}→{after[t]}" for t in TYPES) + ")",
        "0 additional resources", "Met" if extra == 0 else "Not met")


def check_c54(rows: list, cold: list) -> None:
    t = [r["seconds"] for r in rows if r["route"] == "POST /sync" and r["http"] == 200]
    tc = [r["seconds"] for r in cold if r["route"] == "POST /sync" and r["http"] == 200]
    if not t:
        add("C5.4 Processing time", "no successful synchronisation", "median ≤ 5 s", "Not met")
        return
    med = statistics.median(t)
    p95 = sorted(t)[max(0, int(round(0.95 * len(t))) - 1)]
    value = f"median {med:.2f} s, p95 {p95:.2f} s, max {max(t):.2f} s over {len(t)} orders, cache warm (pass 2)"
    if tc:
        value += f"; pass 1 (cache filling): median {statistics.median(tc):.2f} s"
    add("C5.4 Processing time", value, "median ≤ 5 s", "Met" if med <= 5 else "Not met")


def check_c31(out: Path, locations: list) -> None:
    uniq = sorted({m.groups() for m in (LOCATION.search(x) for x in locations) if m})
    rows = []
    for rtype, rid in uniq:
        s, b, _ = call("GET", f"{HAPI_URL}/{rtype}/{rid}", headers=FHIR)
        ok = s == 200 and isinstance(b, dict) and b.get("resourceType") == rtype and b.get("id") == rid
        rows.append({"type": rtype, "id": rid, "http": s, "ok": ok})
    write_csv(out / "c31_read.csv", rows)
    n = sum(r["ok"] for r in rows)
    add("C3.1 Read", f"{n} of {len(rows)} distinct resources returned by GET [base]/{{type}}/{{id}}",
        "100%", "Met" if rows and n == len(rows) else "Not met")


def expected_resources(orders: dict, oru_files: list):
    """Resources the engine generates, per patient reference, and the ServiceRequest identifiers."""
    per = defaultdict(lambda: {t: set() for t in SEARCHED})
    idents = {}
    bundles = []
    for source, ids in orders.items():
        for oid in ids:
            bundles.append(http("GET", f"{ENGINE_URL}/fhir/bundle/{source}/order/{quote(oid, safe='')}"))
    for f in oru_files:
        bundles.append(http("POST", f"{ENGINE_URL}/v2/oru/transform", er7(f), headers=V2)["bundle"])
    for b in bundles:
        for e in b.get("entry", []):
            r = e["resource"]
            if r["resourceType"] in SEARCHED:
                per[r["subject"]["reference"]][r["resourceType"]].add(r["id"])
            if r["resourceType"] == "ServiceRequest":
                for i in r.get("identifier", []):
                    idents[(i["system"], i["value"])] = r["id"]
    return per, idents, bundles


def check_c32(out: Path, per: dict, idents: dict) -> None:
    rows = []
    for ref, by_type in sorted(per.items()):
        for t in SEARCHED:
            q = urlencode({"subject": ref, "_summary": "count"})
            s, b, _ = call("GET", f"{HAPI_URL}/{t}?{q}", headers=FHIR)
            got = b.get("total") if isinstance(b, dict) else None
            rows.append({"patient": ref, "type": t, "expected": len(by_type[t]), "found": got,
                         "ok": s == 200 and got == len(by_type[t])})
    write_csv(out / "c32_search_by_subject.csv", rows)
    irows = []
    for (system, value), rid in sorted(idents.items()):
        q = urlencode({"identifier": f"{system}|{value}", "_summary": "count"})
        s, b, _ = call("GET", f"{HAPI_URL}/ServiceRequest?{q}", headers=FHIR)
        got = b.get("total") if isinstance(b, dict) else None
        irows.append({"system": system, "value": value, "found": got, "ok": s == 200 and got == 1})
    write_csv(out / "c32_search_by_identifier.csv", irows)
    n, m = sum(r["ok"] for r in rows), sum(r["ok"] for r in irows)
    hint = "" if n == len(rows) else " (if other orders of these patients were synchronised earlier, e.g. generated " \
                                      "ones, rerun on a clean stack)"
    add("C3.2 Search", f"{n} of {len(rows)} searches by subject exact ({len(per)} patients × 3 types); "
        f"{m} of {len(irows)} searches by identifier return exactly one ServiceRequest{hint}",
        "exact match for every patient and order",
        "Met" if rows and n == len(rows) and m == len(irows) else "Not met")


def check_c33(out: Path) -> None:
    cs = http("GET", f"{HAPI_URL}/metadata", headers=FHIR)
    declared = {}
    for rest in cs.get("rest", []):
        for r in rest.get("resource", []):
            declared[r.get("type")] = {i.get("code") for i in r.get("interaction", [])}
    rows = [{"type": t, "read": "read" in declared.get(t, set()), "search_type": "search-type" in declared.get(t, set())}
            for t in TYPES]
    write_csv(out / "c33_capability.csv", rows)
    write_json(out / "c33_capabilitystatement.json", cs)
    n = sum(r["read"] and r["search_type"] for r in rows)
    fv = cs.get("fhirVersion")
    sw = cs.get("software") or {}
    add("C3.3 Discovery", f"fhirVersion {fv}; read and search-type declared for {n} of 5 types "
        f"({sw.get('name', '?')} {sw.get('version', '')})".strip(), "4.0.1 and all five types",
        "Met" if fv == "4.0.1" and n == 5 else "Not met")


def check_c72(out: Path, token: str, bundles: list, invalid_code: str, keep: bool) -> None:
    src = next((e["resource"] for b in bundles for e in b.get("entry", [])
                if e["resource"]["resourceType"] == "Observation" and "valueQuantity" in e["resource"]
                and any(c.get("system") == LOINC for c in e["resource"]["code"].get("coding", []))), None)
    if src is None:
        add("C7.2 Traceability of invalid codes", "no Observation with a LOINC code in the dataset", "", "Not run")
        return
    patient = next(e["resource"] for b in bundles for e in b.get("entry", [])
                   if e["resource"]["resourceType"] == "Patient"
                   and f"Patient/{e['resource']['id']}" == src["subject"]["reference"])
    pid = str(uuid.uuid5(uuid.NAMESPACE_URL, "stack-checks/c72/patient"))
    oid = str(uuid.uuid5(uuid.NAMESPACE_URL, "stack-checks/c72/observation"))
    tag = {"system": "http://example.org/fhir/sid/stack-checks", "value": "c72"}
    pat = copy.deepcopy(patient)
    pat.update({"id": pid, "identifier": [tag]})
    obs = copy.deepcopy(src)
    for k in ("basedOn", "partOf", "hasMember", "derivedFrom", "focus", "encounter"):
        obs.pop(k, None)
    obs.update({"id": oid, "identifier": [tag], "subject": {"reference": f"Patient/{pid}"}})
    codings = obs["code"]["coding"]
    loinc = next(c for c in codings if c.get("system") == LOINC)
    original = loinc["code"]
    loinc["code"] = invalid_code
    sent = len(codings)
    bundle = {"resourceType": "Bundle", "type": "transaction", "entry": [
        {"fullUrl": f"urn:uuid:{pid}", "resource": pat, "request": {"method": "PUT", "url": f"Patient/{pid}"}},
        {"fullUrl": f"urn:uuid:{oid}", "resource": obs, "request": {"method": "PUT", "url": f"Observation/{oid}"}}]}
    s, b, _ = call("POST", f"{MIDDLEWARE_URL}/api/fhir/ingest", token, bundle,
                   {"X-Source-Institution": "stack-checks C7.2"})
    trace = (b or {}).get("trace", {}) if isinstance(b, dict) else {}
    term = (trace.get("stages") or {}).get("terminology") or {}
    stored_s, stored, _ = call("GET", f"{HAPI_URL}/Observation/{oid}", headers=FHIR)
    code = (stored or {}).get("code", {}) if isinstance(stored, dict) else {}
    exts = [x for x in code.get("extension", []) if x.get("url") == ISSUE_URL]
    issues = [y.get("valueString", "") for x in exts for y in x.get("extension", [])]
    flagged = any(f"{LOINC}|{invalid_code}" in i for i in issues)
    kept = len(code.get("coding", []))
    still_there = any(c.get("system") == LOINC and c.get("code") == invalid_code for c in code.get("coding", []))
    result = {"ingest_http": s, "terminology": term, "stored_http": stored_s, "invalid_code": invalid_code,
              "replaced_code": original, "codings_sent": sent, "codings_stored": kept, "invalid_coding_kept": still_there,
              "extension_found": bool(exts), "issues": issues, "observation_id": oid}
    if not keep:
        result["deleted"] = [call("DELETE", f"{HAPI_URL}/Observation/{oid}", headers=FHIR)[0],
                             call("DELETE", f"{HAPI_URL}/Patient/{pid}", headers=FHIR)[0]]
        # the test code must not remain in the terminology cache shown by the dashboard
        psql(f"DELETE FROM audit.terminology_validation WHERE code_system = '{LOINC}' AND code = '{invalid_code}';",
             check=False)
    write_json(out / "c72_invalid_code.json", result)
    raw["c72"] = result
    if (term.get("unresolved") or 0) == 0 and (term.get("offline") or 0) > 0:
        add("C7.2 Traceability of invalid codes", "terminology server unreachable; the code could not be judged",
            "100% flagged, 0 removed", "Inconclusive")
        return
    ok = s == 200 and flagged and still_there and kept == sent
    add("C7.2 Traceability of invalid codes",
        f"invalid LOINC {invalid_code}: {'flagged' if flagged else 'NOT flagged'} with the validation-issue extension; "
        f"{kept} of {sent} codings kept; {term.get('local', 0)} local codings counted, not submitted",
        "100% flagged, 0 removed", "Met" if ok else "Not met")


def docker_restart_middleware(offline: bool, override: Path) -> None:
    files = ["-f", str(ROOT / "docker-compose.yml")] + (["-f", str(override)] if offline else [])
    r = subprocess.run(["docker", "compose", *files, "up", "-d", "node-middleware"], cwd=ROOT,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"docker compose failed: {r.stderr[-500:]}")
    time.sleep(3)
    wait_until(middleware_ready, 300, "the middleware")


def check_c73(out: Path, orders: dict, oru_files: list, restart=None) -> None:
    restart = restart or docker_restart_middleware
    override = out / "terminology-offline.override.yml"
    override.write_text("services:\n  node-middleware:\n    environment:\n"
                        f"      TERMINOLOGY_SERVER_URL: \"{OFFLINE_TX}\"\n", encoding="utf-8")
    rows, server = [], None
    try:
        psql("DELETE FROM audit.terminology_validation;")
        restart(True, override)
        token = login("admin")
        server = http("GET", f"{MIDDLEWARE_URL}/terminology/metrics", token=token).get("server")
        rows, _ = submit_all(token, orders, oru_files, "pass3-terminology-unreachable")
        invalid = int(psql("SELECT count(*) FROM audit.terminology_validation WHERE is_valid = false;").stdout.strip())
    finally:
        log("restoring the middleware's terminology server")
        restart(False, override)
    write_csv(out / "c73_unreachable.csv", rows)
    n = sum(r["accepted"] for r in rows)
    offline = sum(r["tx_offline"] or 0 for r in rows)
    unresolved = sum(r["tx_unresolved"] or 0 for r in rows)
    ok = server == OFFLINE_TX and n == len(rows) and invalid == 0 and unresolved == 0 and offline > 0
    add("C7.3 Robustness to unavailability", f"{n} of {len(rows)} Bundles stored; {invalid} codes recorded as "
        f"invalid; {offline} lookups reported offline (server {server})",
        "0 invalid; N of N stored", "Met" if ok else "Not met")
    # Pass 3 left in HAPI copies checked while the server was unreachable, and an empty terminology
    # cache. Send the dataset once more with the server back, so the stack ends as in normal use.
    log("pass 4: the dataset again with the terminology server restored")
    restored, _ = submit_all(login("admin"), orders, oru_files, "pass4-restored")
    write_csv(out / "c73_restored.csv", restored)
    log(f"  {sum(r['accepted'] for r in restored)} of {len(restored)} Bundles stored again")


def start_sampler(label: str):
    stop = results_dir("docker_stats") / f"{label}.stop"
    stop.unlink(missing_ok=True)
    p = subprocess.Popen([sys.executable, str(HERE / "capture_docker_stats.py"), "--label", label, "--interval", "1",
                          "--until-file", str(stop)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    return p, stop


def check_c53(label: str) -> None:
    import csv
    path = results_dir("docker_stats") / f"{label}.csv"
    if not path.exists() or path.stat().st_size < 100:
        add("C5.3 Memory", "not measured (docker stats unavailable)", "≤ 4 GB", "Not run")
        return
    per_ts = defaultdict(float)
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            per_ts[r["timestamp"]] += float(r["mem_mib"])
    peak = max(per_ts.values())
    add("C5.3 Memory", f"peak combined {peak:.0f} MiB ({peak * 1.048576 / 1000:.2f} GB) over {len(per_ts)} samples "
        "of this run", "≤ 4 GB", "Met" if peak * 1.048576 <= 4000 else "Not met")


def check_c52(out: Path, n: int, volumes: bool = False) -> None:
    rows = []
    down = ["docker", "compose", "down"] + (["-v"] if volumes else [])
    for i in range(1, n + 1):
        log(f"cold start {i} of {n}: {' '.join(down)}")
        subprocess.run(down, cwd=ROOT, capture_output=True, text=True, check=True)
        t0 = time.time()
        r = subprocess.run(["docker", "compose", "up", "-d"], cwd=ROOT, capture_output=True, text=True)
        returned = time.time() - t0
        if r.returncode != 0:
            raise SystemExit(f"docker compose up failed: {r.stderr[-500:]}")
        seen: dict[str, float] = {}
        while len(seen) < 3:
            now = time.time() - t0
            if now > 900:
                raise SystemExit("timed out after 900 s waiting for the middleware and HAPI")
            if "health" not in seen and call("GET", f"{MIDDLEWARE_URL}/health", timeout=5)[0] == 200:
                seen["health"] = now
            if "metadata" not in seen and hapi_ready():
                seen["metadata"] = now
            if "audit" not in seen and middleware_ready():
                seen["audit"] = now
            time.sleep(1)
        total = max(seen["health"], seen["metadata"])
        rows.append({"start": i, "command_returned_s": round(returned, 1), "health_200_s": round(seen["health"], 1),
                     "metadata_200_s": round(seen["metadata"], 1), "audit_db_ready_s": round(seen["audit"], 1),
                     "ready_s": round(total, 1)})
        log(f"  ready after {total:.1f} s (HAPI /metadata {seen['metadata']:.1f} s)")
    write_csv(out / "c52_startup.csv", rows)
    med = statistics.median(r["ready_s"] for r in rows)
    add("C5.2 Start-up time", f"median {med:.1f} s over {n} cold starts (" + ", ".join(
        f"{r['ready_s']:.0f}" for r in rows) + " s); images already present, " +
        ("volumes removed before each start (empty databases)" if volumes else "volumes kept"),
        "median ≤ 180 s", "Met" if med <= 180 else "Not met")
    add("C5.1 Single-command deployment (partial)", f"after docker compose up -d, both endpoints answered with no "
        f"further step in {n} of {n} starts; a start from a fresh clone was not tested by this script",
        "0 manual steps", "Reported")


# ── Main ─────────────────────────────────────────────────────────────────────
ALL = ["roles", "c11", "c24", "c54", "c31", "c32", "c33", "c72", "c73", "c53"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default=",".join(ALL), help="comma-separated checks (default: all except c52)")
    ap.add_argument("--startup", type=int, default=0, help="also run c52 with this many cold starts (e.g. 5)")
    ap.add_argument("--startup-volumes", action="store_true",
                    help="c52 with 'docker compose down -v': each start with empty databases (deletes all data)")
    ap.add_argument("--include-generated", action="store_true", help="include generated orders in the dataset")
    ap.add_argument("--oru-dir", default=str(ROOT / "samples" / "oru"))
    ap.add_argument("--invalid-code", default="00000-0", help="LOINC code used by c72 (must not exist)")
    ap.add_argument("--keep", action="store_true", help="keep the c72 test resources in HAPI")
    a = ap.parse_args()
    if not re.fullmatch(r"[0-9A-Za-z.\-]{1,20}", a.invalid_code):
        ap.error("--invalid-code: letters, digits, '.' and '-' only")
    only = set(a.only.split(","))
    if only & {"c24", "c54", "c31", "c32"}:
        only.add("c11")
    out = results_dir("stack_checks")

    log("waiting for the middleware and HAPI")
    wait_until(lambda: middleware_ready() and hapi_ready(), 300, "the middleware and HAPI")
    sampler = start_sampler("stack_checks") if "c53" in only else None
    token = login("admin")
    orders, oru = dataset(token, a.include_generated, Path(a.oru_dir))
    raw["dataset"] = {"orders": orders, "oru": [f.name for f in oru]}
    log("dataset: " + ", ".join(f"{s} {len(v)}" for s, v in orders.items()) + f", oru {len(oru)}")
    all_rows = []
    try:
        if "roles" in only:
            check_roles(out, orders, oru)
        if "c11" in only:
            log("pass 1: every Bundle of the dataset")
            pass1, locations = submit_all(token, orders, oru, "pass1")
            all_rows += pass1
            check_c11(pass1)
            if only & {"c24", "c54"}:
                before = counts()
                log("pass 2: the same Bundles again")
                pass2, _ = submit_all(token, orders, oru, "pass2")
                all_rows += pass2
                after = counts()
                write_csv(out / "c24_counts.csv", [{"type": t, "after_pass1": before[t], "after_pass2": after[t],
                                                    "difference": after[t] - before[t]} for t in TYPES])
                if "c24" in only:
                    check_c24(before, after)
                if "c54" in only:
                    check_c54(pass2, pass1)
            if "c31" in only:
                check_c31(out, locations)
        bundles = []
        if only & {"c32", "c72"}:
            per, idents, bundles = expected_resources(orders, oru)
            if "c32" in only:
                check_c32(out, per, idents)
        if "c33" in only:
            check_c33(out)
        if "c72" in only:
            check_c72(out, token, bundles, a.invalid_code, a.keep)
        if "c73" in only:
            check_c73(out, orders, oru)
    finally:
        if all_rows:
            write_csv(out / "c11_bundles_and_times.csv", all_rows)
        if sampler:
            sampler[1].write_text("stop")
            try:
                sampler[0].wait(timeout=60)
            except subprocess.TimeoutExpired:
                sampler[0].kill()
    if sampler:
        check_c53("stack_checks")
    if a.startup:
        check_c52(out, a.startup, a.startup_volumes)

    write_json(out / "stack_checks.json", {"summary": summary, **raw})
    md = ["# Stack checks (Chapter 6)", "", f"Dataset: " + ", ".join(f"{s} {len(v)} orders" for s, v in orders.items())
          + f", {len(oru)} ORU^R01 messages.", "", markdown_table(summary, ["criterion", "value", "threshold", "status"]),
          "Details: one CSV per check in this folder; c11_bundles_and_times.csv holds every submission of every pass."]
    (out / "SUMMARY.md").write_text("\n".join(md), encoding="utf-8")
    print()
    print(markdown_table(summary, ["criterion", "value", "threshold", "status"]))
    log(f"-> {out / 'SUMMARY.md'}")


if __name__ == "__main__":
    main()
