"""
common.py - shared helpers for the measurement scripts (standard library only).

Every script writes into a results directory, by default
results/<UTC timestamp>/; set RESULTS_DIR to reuse one directory across
scripts (run_all.py does this).

Endpoints (override with environment variables):
  MIDDLEWARE_URL  http://localhost:3000
  ENGINE_URL      http://localhost:8000
  HAPI_URL        http://localhost:8080/fhir
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIDDLEWARE_URL = os.environ.get("MIDDLEWARE_URL", "http://localhost:3000").rstrip("/")
ENGINE_URL = os.environ.get("ENGINE_URL", "http://localhost:8000").rstrip("/")
HAPI_URL = os.environ.get("HAPI_URL", "http://localhost:8080/fhir").rstrip("/")
PG_CONTAINER = os.environ.get("PG_CONTAINER", "sil-postgres")
PG_OWNER = os.environ.get("PG_OWNER", "sil_user")
PG_OWNER_PASSWORD = os.environ.get("PG_OWNER_PASSWORD", "sil_pass")

DEMO_PASSWORDS = {"admin": "admin123", "medico": "med123", "tecnico": "tec123", "auditor": "aud123"}


def results_dir(sub: str | None = None) -> Path:
    base = os.environ.get("RESULTS_DIR")
    if not base:
        base = str(ROOT / "results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        os.environ["RESULTS_DIR"] = base
    p = Path(base) / sub if sub else Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ── HTTP ─────────────────────────────────────────────────────────────────────
class HttpError(Exception):
    def __init__(self, status: int, body):
        super().__init__(f"HTTP {status}: {str(body)[:300]}")
        self.status, self.body = status, body


def http(method: str, url: str, body=None, token: str | None = None, headers: dict | None = None,
         timeout: float = 120, raw: bool = False):
    h = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        if isinstance(body, (bytes, str)):
            data = body.encode("utf-8") if isinstance(body, str) else body
            h.setdefault("Content-Type", "text/plain")
        else:
            data = json.dumps(body).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        payload, status = e.read(), e.code
    text = payload.decode("utf-8", errors="replace")
    try:
        parsed = text if raw else json.loads(text)
    except json.JSONDecodeError:
        parsed = text
    if status >= 400:
        raise HttpError(status, parsed)
    return parsed


def login(user: str = "admin", password: str | None = None) -> str:
    r = http("POST", f"{MIDDLEWARE_URL}/auth/login",
             {"username": user, "password": password or DEMO_PASSWORDS[user]})
    return r["access_token"]


def wait_for(url: str, timeout: float = 300, label: str | None = None) -> None:
    t0 = time.time()
    while True:
        try:
            http("GET", url, timeout=10)
            return
        except Exception as exc:  # noqa: BLE001
            if time.time() - t0 > timeout:
                raise SystemExit(f"timed out waiting for {label or url}: {exc}")
            time.sleep(3)


# ── Database (through the Postgres container, as the owner role) ────────────
def psql(sql: str, db: str = "sildb", user: str | None = None, password: str | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    """Run SQL as `user` over TCP, so the role's own password is used.

    By default psql runs inside the Postgres container (docker exec). With
    PSQL_DIRECT=1 a local psql client is used instead (PGHOST, PGPORT).
    """
    user = user or PG_OWNER
    password = password or (PG_OWNER_PASSWORD if user == PG_OWNER else "")
    if os.environ.get("PSQL_DIRECT"):
        cmd = ["psql", "-h", os.environ.get("PGHOST", "localhost"), "-p", os.environ.get("PGPORT", "5432"),
               "-U", user, "-d", db, "-v", "ON_ERROR_STOP=1", "-qtA"]
        return subprocess.run(cmd, input=sql, text=True, capture_output=True, check=check,
                               env={**os.environ, "PGPASSWORD": password})
    cmd = ["docker", "exec", "-i", "-e", f"PGPASSWORD={password}", PG_CONTAINER,
           "psql", "-h", "127.0.0.1", "-U", user, "-d", db, "-v", "ON_ERROR_STOP=1", "-qtA"]
    return subprocess.run(cmd, input=sql, text=True, capture_output=True, check=check)


def query_json(sql: str, db: str = "sildb", user: str | None = None, password: str | None = None) -> list:
    """Rows of a SELECT as a list of dicts (numbers as Decimal, dates and timestamps as ISO strings)."""
    from decimal import Decimal
    r = psql(f"SELECT coalesce(json_agg(t), '[]'::json) FROM ({sql}) t;", db=db, user=user, password=password)
    return json.loads(r.stdout.strip() or "[]", parse_float=Decimal)


# ── Files ───────────────────────────────────────────────────────────────────
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def write_csv(path: Path, rows: list[dict], columns: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = columns or (list(rows[0].keys()) if rows else [])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def markdown_table(rows: list[dict], columns: list[str] | None = None) -> str:
    if not rows:
        return "_(no rows)_\n"
    columns = columns or list(rows[0].keys())
    out = ["| " + " | ".join(columns) + " |", "|" + "|".join("---" for _ in columns) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(r.get(c, "")).replace("|", "\\|").replace("\n", " ") for c in columns) + " |")
    return "\n".join(out) + "\n"


def require_docker() -> None:
    try:
        subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, check=True)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Docker is required and must be running: {exc}")
