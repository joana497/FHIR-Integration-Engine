# Middleware end-to-end test (without Docker)

Exercises the middleware against the real Python engine and a real
PostgreSQL, with HAPI FHIR and the terminology server replaced by small
mocks (`mocks.js`), so that failures can be injected deterministically.
It covers: bcrypt/JWT login, RBAC, synchronisation of both SQL sources,
ORU^R01 over HTTP and MLLP (ACK AA/AR/AE), external FHIR ingest, the
intent/outcome audit trail, the fail-closed refusal (HTTP 503 / ACK AE
and nothing sent to HAPI), the outbox fallback and replay, and the
privileges of `audit_writer`. 43 checks.

Requirements: PostgreSQL 16 initialised with `postgres/01_schema.sql` …
`07_roles.sql` (in that order, as the owner `sil_user`), Python 3.12 with
`python-fhir-engine/requirements.txt`, Node.js 20+, and `psql` on the PATH.

```bash
# 1. mocks (HAPI on :8080, terminology on :8090)
node node-middleware/test/e2e/mocks.js &
# 2. Python engine (read-only roles)
cd python-fhir-engine && DATABASE_URL=postgresql://sil_reader:sil_reader_pass@localhost:5432/sildb \
  LISB_DATABASE_URL=postgresql://lisb_reader:lisb_reader_pass@localhost:5432/lisbdb \
  uvicorn main:app --port 8000 & cd ..
# 3. middleware
cd node-middleware && PYTHON_ENGINE_URL=http://127.0.0.1:8000 HAPI_FHIR_URL=http://127.0.0.1:8080/fhir \
  AUDIT_DATABASE_URL=postgresql://audit_writer:audit_writer_pass@localhost:5432/sildb \
  AUDIT_OUTBOX_PATH=/tmp/audit-outbox.jsonl MLLP_PORT=2575 RATE_LIMIT_PER_MIN=5000 \
  TERMINOLOGY_SERVER_URL=http://127.0.0.1:8090/r4 node src/index.js & cd ..
# 4. the test (about 45 s, of which 32 s wait for the outbox replay job)
PGHOST=localhost PGPORT=5432 node node-middleware/test/e2e/e2e.js
```

The test revokes and re-grants INSERT privileges of `audit_writer`; run
it only against a disposable database.
