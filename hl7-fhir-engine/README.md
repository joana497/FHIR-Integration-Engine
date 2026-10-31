# HL7 FHIR R4 Integration Engine for Legacy Laboratory Systems

An integration engine that exposes the data of legacy laboratory information systems as
HL7 FHIR R4 resources without modifying those systems. It reads two relational laboratory
schemas through database roles that cannot write, receives HL7 version 2.5.1 ORU^R01 result
messages over MLLP or HTTP, maps every source to FHIR R4 through one canonical model, adds
LOINC, SNOMED CT and UCUM codes next to the local ones, and stores the result in an
independent HAPI FHIR server. Authentication, role-based access control, terminology
validation and a fail-closed audit trail are part of the architecture.

The engine was developed within a master's dissertation on interoperability between legacy
information systems of the Portuguese National Health Service (Serviço Nacional de Saúde, SNS).

> **Demonstration data only.** All patients, names and values are synthetic. Every identifier
> and local code system lies under `http://example.org/`, a placeholder that belongs to no
> hospital or public body. The default passwords and secrets are demonstration values; read
> [Security notes](#security-notes) before any other use.

---

## Features

- **Non-intrusive extraction.** The engine reads the legacy databases as `sil_reader` and
  `lisb_reader`, roles with `SELECT` privileges only and read-only transactions by default.
  The Python service never writes anywhere; every write goes through the audited pipeline of
  the middleware.
- **Three sources, one canonical model.** Adapters for SIL, LIS-B and HL7 v2 ORU^R01 turn
  their records into the same canonical objects (`canonical.py`); the FHIR mappers are shared
  by all sources.
- **FHIR R4 output.** Patient, ServiceRequest, ImagingStudy, DiagnosticReport and Observation,
  assembled per order into a transaction Bundle. Every entry is a `PUT` on a deterministic
  identifier, so sending the same order twice updates the same resources instead of
  duplicating them.
- **Dual coding.** A FHIR `ConceptMap` generated from version 7.0 of the *Catálogo Português
  de Análises de Laboratório* (CPAL) adds LOINC, SNOMED CT and UCUM codes next to each local
  code, which is kept for traceability.
- **Terminology validation.** Every standard code is checked with `CodeSystem/$validate-code`
  on a FHIR terminology server (by default `tx.fhir.org`), with an in-memory and a PostgreSQL
  cache and an offline mode.
- **Security.** JSON Web Tokens (HS256), bcrypt-hashed passwords, four roles with distinct
  permissions, and a per-client rate limit.
- **Fail-closed audit.** A transfer of health data is refused unless its audit record can be
  written first; access and security events fall back to a local outbox when the audit
  database is down.
- **Reception from other institutions.** FHIR Bundles posted by another system go through a
  four-stage pipeline (reception, structural validation, terminology, persistence) whose trace
  is returned and audited.
- **Web dashboard** (in Portuguese) for browsing, synchronising, validating and auditing.
- **Reproducible deployment and evaluation.** Five containers started with one command, plus
  scripts that measure conformance, value fidelity, terminology validity, resource use and
  the behaviour of the audit trail under failure.

## Architecture

```
  SIL    (database sildb,  schema sil) ──SELECT only──┐
  LIS-B  (database lisbdb, schema lis) ──SELECT only──┤   python-fhir-engine :8000
                                                      ├──► adapters → canonical records → FHIR mappers
  HL7 v2 ORU^R01 (MLLP :2575 or HTTP) ───────────────►┘          + ConceptMap (LOINC, SNOMED CT, UCUM)
                                                                   │  (never writes)
                                                                   ▼
  web dashboard / API clients ──JWT──► node-middleware :3000
                                       RBAC · terminology check · fail-closed audit · MLLP listener
                                                                   │
                                                                   ▼
                                                 HAPI FHIR R4 server :8080 (own PostgreSQL)
```

| Service (container) | Host port | Role |
|---|---|---|
| `postgres` (`sil-postgres`) | 5432 | SIL (`sildb`) and LIS-B (`lisbdb`) databases, audit and terminology schemas, application roles |
| `python-fhir-engine` | 8000 | FastAPI service: source adapters, canonical model, FHIR mapping, ConceptMap; read-only |
| `node-middleware` | 3000, 2575 | Express service: API, dashboard, authentication, access control, terminology validation, audit, writes to HAPI; MLLP listener |
| `hapi-fhir` | 8080 | HAPI FHIR R4 server (JPA) |
| `hapi-postgres` | 5433 | HAPI's own database |

### How an order is processed

1. A user or a script asks the middleware to synchronise an order
   (`POST /sync/{source}/order/{id}`), an HL7 v2 message arrives over MLLP or HTTP, or another
   system posts a FHIR Bundle to `/api/fhir/ingest`.
2. The middleware verifies the token and the permission the request needs.
3. The Python engine reads the order through the adapter of its source, or parses the HL7 v2
   message, and returns a transaction Bundle.
4. The middleware validates every LOINC, SNOMED CT and UCUM code. A coding that is invalid, or
   that cannot be checked, is flagged with an extension
   (`http://example.org/fhir/StructureDefinition/terminology-validation-issue`); the Bundle is
   not blocked.
5. The middleware commits an audit *intent* row containing the Bundle. If that row cannot be
   written, the transfer is refused (HTTP 503, or ACK `AE` for HL7 v2) and nothing is sent.
6. The Bundle is sent to HAPI FHIR as one transaction: either every entry is stored or none is.
7. The *outcome* is recorded in the audit database, or in the local outbox if the database has
   become unavailable.

For Bundles received through `/api/fhir/ingest`, HAPI's `$validate` runs before step 4 as a
structural check; its result is reported in the trace and does not prevent storage. For HL7 v2
messages this check is off by default (`ORU_STRUCTURAL_VALIDATION`).

## Data sources

| Source | Kind | Adapter | Demonstration data |
|---|---|---|---|
| `sil` | Relational schema of a Portuguese laboratory and imaging information system | `adapters/sil.py` | 25 orders for 25 patients |
| `lisb` | A second, differently structured laboratory schema | `adapters/lisb.py` | 8 requisitions for 6 patients |
| `oru` | HL7 v2.5.1 ORU^R01 messages pushed by an external laboratory | `adapters/oru_r01.py`, `hl7v2.py` | 5 messages in `samples/oru/` |

**SIL.** The schema has four tables: `dpedidos` (one row per requisition), `pedidos`
(requested lines), `dexames` (examinations) and `exames` (analysis lines). Operational
installations run on Oracle; the demonstration reproduces the schema in PostgreSQL 16. The
table `resultados` was added to hold result values, which the schema does not contain. The
15 original orders cover ten modalities (laboratory, computed tomography, ultrasound,
radiography, magnetic resonance, haemodynamics, nuclear medicine, mammography,
electrocardiography and anatomical pathology).

**LIS-B.** A second schema in its own database, `lisbdb`, with the tables `patient`,
`requisition`, `req_test` and `result`. It differs from SIL in the ways real laboratory
systems differ: English names and integer keys, timestamps instead of dates, priority and
status as letter codes, no examination header, typed result columns, reference intervals held
as text (for example `13.0-17.0`), and its own local codes and unit spellings.

**HL7 v2 ORU^R01.** Segments MSH, PID, ORC, OBR, OBX and NTE are read following the published
HL7 v2-to-FHIR mappings: PID becomes the Patient, ORC the order, each OBR a DiagnosticReport and
a grouping Observation, each OBX a result Observation, and NTE comments are added to the
report's conclusion. Coding systems `LN`, `SCT` and `UCUM` are recognised as LOINC, SNOMED CT
and UCUM; any other identifier is treated as a local code of the sending laboratory. A message
that cannot be mapped is rejected with an explanation (ACK `AR`) rather than partly stored.

### Adding a source

A new source is one module in `python-fhir-engine/adapters/` that implements one of the two
protocols in `adapters/base.py` (`DatabaseAdapter` with `list_orders`, `get_order` and
`health`, or `MessageAdapter` with `parse`) and returns `canonical.OrderRec` objects, plus its
registration in `adapters/__init__.py`. Its local codes need groups in the ConceptMap
(`tools/build_conceptmap.py`). The FHIR mappers do not change. For reference, the LIS-B
adapter has 104 logical lines and the ORU^R01 adapter with its parser 251, against 667 lines
of shared code (`scripts/adapter_line_counts.py`).

## FHIR mapping

| Resource | Built from | Notes |
|---|---|---|
| `Patient` | Patient data of the order | The identifiers the source holds: its local patient number and, where present, the SNS number |
| `ServiceRequest` | Requisition and its requested lines | Requested lines in `orderDetail`; cancelled lines left out and reported in `note`; the national requisition number in `requisition` when the SIL lines share one |
| `ImagingStudy` | Imaging examinations of SIL only | DICOM modality codes (`DX`, `CT`, `MR`, `US`, `MG`, `NM`, `XA`); none for laboratory, ECG or pathology |
| `DiagnosticReport` | Each examination (SIL), requisition (LIS-B) or OBR segment (ORU) | Report text of imaging, ECG and pathology in `conclusion` |
| `Observation` | Requested tests and their results | One grouping Observation per test (`hasMember`) and one per result, with `valueQuantity`, `valueCodeableConcept` or `valueString`, `referenceRange` and `interpretation` |

- **Bundles.** One transaction Bundle per order; resource identifiers are UUID v5 values
  derived from the source keys, so the same source record always yields the same identifier.
- **Narratives.** Every resource carries a generated XHTML narrative (`text.div`), escaped so
  that the source data cannot break the markup.
- **Dates and times.** FHIR requires a time zone whenever a time is present, but the source
  timestamps have none. The engine takes them as local time of the source, set by
  `SOURCE_TIMEZONE` (default `Europe/Lisbon`); a deployment in the Azores, for example, sets
  `Atlantic/Azores`.
- **Identifiers and local code systems** are defined in `python-fhir-engine/systems.py`, all
  under `http://example.org/fhir`. A real deployment replaces them with URIs assigned by the
  institution.
- **Mapping table.** Every column of the SIL tables is listed with its FHIR target, or the
  reason it is not mapped, in `docs/h2/sil_fhir_mapping.csv` (generated from
  `tools/sil_mapping.py`); a test checks the table against the schema and the adapter.

## Terminology

- `python-fhir-engine/conceptmaps/lab-catalogue.json` is generated by
  `tools/build_conceptmap.py` from CPAL v7.0. It holds 13 groups and 238 source codes: the
  local test, analyte, unit and qualitative-result codes of the three sources, and the SNS and
  convention codes, mapped to LOINC, SNOMED CT and UCUM. No standard code is typed by hand, and
  only targets of equivalence `equivalent` or `equal` are applied automatically.
- The ConceptMap covers a demonstration catalogue of tests, not a laboratory's full
  repertoire. The CPAL gives its units as examples, so the UCUM codes derived from them must be
  confirmed against the national unit value set before production use.
- The middleware checks each standard code with `CodeSystem/$validate-code` on
  `TERMINOLOGY_SERVER_URL`. Only the code system and the code are sent, never patient data.
  Results are cached in memory and in `audit.terminology_validation` for 24 hours, and a daily
  job (03:33) revalidates expired entries. Local codes are counted separately, since no public
  server can validate them.
- If the server cannot be reached, the codes are marked `offline` and the data are still
  stored. Any server that implements FHIR R4 `$validate-code` can replace `tx.fhir.org`.
- The dashboard page *Validação terminológica* and `GET /terminology/metrics` show validity
  per code system and the most frequent unresolved codes.

## Security and audit

**Demonstration accounts.** Passwords are stored only as bcrypt hashes; a login attempt for an
unknown user is compared against a dummy hash, so that response times do not reveal which
users exist.

| User | Password | Role | Permissions |
|---|---|---|---|
| `admin` | `admin123` | admin | read, write, delete, synchronise, administration, audit |
| `medico` | `med123` | medico | read |
| `tecnico` | `tec123` | tecnico | read, write, synchronise |
| `auditor` | `aud123` | auditor | read, audit |

**Database roles** (`postgres/07_roles.sql`):

| Role | Privileges | Used by |
|---|---|---|
| `sil_reader` | `SELECT` on `sildb.sil`, read-only transactions | Python engine |
| `lisb_reader` | `SELECT` on `lisbdb.lis`, read-only transactions | Python engine |
| `audit_writer` | `INSERT` and `SELECT` on the audit tables; cache writes on the terminology table | Middleware |
| `sil_user` | Owner | Initialisation scripts and the order generator only |

`scripts/check_db_roles.py` attempts 26 allowed and forbidden operations and records the
server's answer to each.

**Audit trail** (`postgres/03_audit_schema.sql`):

- `audit.exchange_log` records every transfer in two rows, an *intent* with the Bundle,
  written before anything is sent, and an *outcome* with HAPI's response, linked by a
  correlation identifier. The view `audit.exchange_summary` joins them.
- `audit.security_event` records logins, failed logins, permission denials and rejected
  messages.
- When the audit database is unavailable, outcome rows and security events are written to a
  local outbox file (`AUDIT_OUTBOX_PATH`) and replayed every 30 seconds. A request is refused
  if neither the database nor the outbox can be written.
- Payloads are purged after 90 days by a daily job (03:17); the metadata of each exchange is
  kept.
- Users with the `audit` permission can browse the trail in the dashboard and export it as CSV
  or JSON.

## Getting started

### Requirements

- Docker with Docker Compose v2 (Docker Desktop on macOS and Windows). Allow Docker at least
  4 GB of memory; the stack peaked at about 1.7 GB in the reference evaluation, most of it used
  by HAPI FHIR.
- Free ports 3000, 2575, 8000, 8080, 5432 and 5433.
- Internet access to pull the images and to reach the terminology server.
- Python 3.9 or later, only for the scripts in `scripts/`.

### Start the stack

```bash
git clone https://github.com/joanaoliveira/hl7-fhir-engine.git
cd hl7-fhir-engine
docker compose up -d --build
```

The engine and the middleware are ready within about 30 seconds; HAPI FHIR needs one to two
minutes on the first start. Then:

```bash
curl http://localhost:3000/health        # middleware, Python engine, HAPI and audit status
open http://localhost:3000/app           # dashboard (on Linux: xdg-open)
```

The database initialisation scripts in `postgres/` run only on an empty volume. After changing
any of them, recreate the volumes, which deletes all stored data:

```bash
docker compose down -v
docker compose up -d --build
```

**Pinning HAPI FHIR.** `docker-compose.yml` uses `hapiproject/hapi:latest` unless `HAPI_IMAGE`
is set. For reproducible results, pin the image; the reference evaluation used HAPI FHIR 8.12.0:

```bash
HAPI_IMAGE=hapiproject/hapi@sha256:0ff86aac4c7bd13a1ee226444a70263bb46ed1c28ef543c30a44f865b80ca04e \
  docker compose up -d --build
```

### First steps with the API

```bash
# log in and keep the token
TOKEN=$(curl -s -X POST http://localhost:3000/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"tecnico","password":"tec123"}' \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["access_token"])')

# list the sources and some orders
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:3000/api/sources
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:3000/api/sources/sil/orders?limit=5"

# see the Bundle of one order, then store it in HAPI FHIR
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:3000/api/fhir/sample-bundle/sil/PED-2024-00001
curl -s -X POST -H "Authorization: Bearer $TOKEN" http://localhost:3000/sync/sil/order/PED-2024-00001

# read what HAPI FHIR now holds
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:3000/hapi/fhir/Observation?_count=5"
```

### Sending HL7 v2 messages

Over MLLP, which frames each message with `0x0B … 0x1C 0x0D` on TCP port 2575:

```bash
python3 scripts/send_oru_mllp.py samples/oru
```

Over HTTP, with a token that has the `write` permission:

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: x-application/hl7-v2+er7' \
  --data-binary @samples/oru/oru_r01_01.hl7 http://localhost:3000/api/v2/oru
```

The answer is an HL7 acknowledgement: `AA` stored, `AR` rejected because the message cannot
be mapped, `AE` not stored because of a temporary failure (the sender should retry).

### Dashboard

Open `http://localhost:3000/app` and log in with one of the demonstration accounts. The pages
are:

| Page | Shows |
|---|---|
| *Dashboard* | Patients and orders of the selected source, patients stored in HAPI, the session's role, and the state of the audit database and outbox |
| *Utentes* | Patients of the selected source, as mapped by the engine |
| *Requisições* | Orders of the selected source, with HAPI validation and synchronisation of each order or of all |
| *Recursos FHIR* | Number of resources of each type stored in HAPI |
| *Recepção externa* | Posting a Bundle as another institution and following the four stages of the pipeline |
| *Validação terminológica* | Validity per code system and the unresolved codes |
| *Histórico de trocas* | The exchanges with HAPI and their Bundles, with CSV and JSON export |
| *Auditoria* | Security events, with CSV and JSON export |

Actions that a role lacks are not offered, and the middleware refuses them in any case
(HTTP 403).

## API reference

### Python engine (port 8000)

The engine has no authentication of its own and must stay on the internal network. Its
interactive documentation is at `http://localhost:8000/docs`.

| Method | Path | Returns |
|---|---|---|
| GET | `/health` | Service status, including the database connection |
| GET | `/sources` | Available sources |
| GET | `/sources/{source}/orders` | Order identifiers of a database source |
| GET | `/conceptmap` | Summary of the loaded ConceptMap |
| GET | `/fhir/bundle/{source}/order/{id}` | Transaction Bundle of one order |
| POST | `/v2/oru/transform` | Bundle built from an ORU^R01 message (HTTP 422 with the reason if it cannot be mapped) |
| GET | `/fhir/{Patient, ServiceRequest, ImagingStudy, DiagnosticReport, Observation}?source=` | Searchset Bundles of mapped resources |
| GET | `/fhir/ServiceRequest/{id}`, `/fhir/{type}/by-order/{id}` | Resources of one order |

### Middleware (port 3000)

Every route except login and health requires `Authorization: Bearer <token>`.

| Method | Path | Permission |
|---|---|---|
| POST | `/auth/login` | none |
| GET | `/health` | none |
| GET | `/auth/me`; POST `/auth/logout` | authenticated |
| GET | `/api/sources`, `/api/sources/:source/orders` | read |
| GET | `/fhir/{Patient, ServiceRequest, ImagingStudy, DiagnosticReport, Observation}` (mapped by the engine) | read |
| GET | `/hapi/fhir/:type`, `/hapi/fhir/:type/:id` (stored in HAPI FHIR) | read |
| GET | `/api/fhir/sample-bundle/:source/:id`, `/fhir/bundle/:source/order/:id/hapi-validation` | read |
| POST | `/sync/:source/order/:id`, `/sync/all?source=` | sync |
| POST | `/api/fhir/ingest` (four-stage pipeline), `/api/fhir/bundle` (direct storage), `/api/v2/oru` | write |
| GET | `/audit`, `/audit/exchanges`, `/audit/exchanges/:id`, `/audit/exchanges.{csv,json}`, `/audit/security.{csv,json}` | audit |
| GET | `/terminology/metrics` | read |
| POST | `/terminology/revalidate` | admin |

`/ingest` accepts an optional `X-Source-Institution` header naming the sender, which is
recorded in the audit trail.

## Configuration

Set in `docker-compose.yml`.

| Variable | Service | Default | Meaning |
|---|---|---|---|
| `DATABASE_URL`, `LISB_DATABASE_URL` | engine | read-only roles | Connections to the SIL and LIS-B databases; LIS-B is enabled only when its URL is set |
| `CONCEPTMAP_PATH` | engine | `/app/conceptmaps/lab-catalogue.json` | ConceptMap used for dual coding |
| `SOURCE_TIMEZONE` | engine | `Europe/Lisbon` | Time zone of source timestamps that have none |
| `PYTHON_ENGINE_URL`, `HAPI_FHIR_URL` | middleware | internal addresses | Python engine and FHIR server |
| `JWT_SECRET` | middleware | demonstration value | Token signing secret; **must be changed** |
| `TOKEN_TTL` | middleware | `1800` | Token lifetime in seconds |
| `RATE_LIMIT_PER_MIN` | middleware | `300` | Requests per minute per client |
| `CORS_ORIGINS` | middleware | localhost | Allowed browser origins |
| `AUTO_SYNC` | middleware | `false` | When `true`, synchronises every 5 minutes as the audited actor `system:cron` |
| `AUDIT_DATABASE_URL`, `AUDIT_OUTBOX_PATH` | middleware | `audit_writer`, volume path | Audit database and local outbox |
| `MLLP_PORT`, `MLLP_ALLOWED_HOSTS` | middleware | `2575`, any | MLLP listener and the sender addresses it accepts |
| `ORU_STRUCTURAL_VALIDATION` | middleware | `false` | Run HAPI `$validate` on Bundles built from HL7 v2 messages |
| `TERMINOLOGY_SERVER_URL`, `TERMINOLOGY_ENABLED`, `TERMINOLOGY_TIMEOUT_MS` | middleware | `https://tx.fhir.org/r4`, `true`, `5000` | Terminology validation |
| `HAPI_IMAGE` | compose | `hapiproject/hapi:latest` | HAPI FHIR image; pin a digest for reproducible runs |

## Tests

```bash
cd python-fhir-engine
pytest -q                     # unit tests; the database tests are skipped without a database

# with the stack running, including the database tests
DATABASE_URL=postgresql://sil_reader:sil_reader_pass@localhost:5432/sildb \
LISB_DATABASE_URL=postgresql://lisb_reader:lisb_reader_pass@localhost:5432/lisbdb pytest -q
```

The 28 Python tests cover the mappers, the ConceptMap, the HL7 v2 parser and adapter, both
database sources and the SIL mapping table. The middleware's end-to-end test (43 checks:
login, access control, both database sources, HL7 v2 over HTTP and MLLP, external reception,
the audit trail and its failure modes, and the privileges of `audit_writer`) runs against the
real Python engine and PostgreSQL with mocks of HAPI FHIR and the terminology server; see
[`node-middleware/test/e2e/README.md`](node-middleware/test/e2e/README.md).

## Evaluation

[`scripts/README.md`](scripts/README.md) describes the evaluation scripts and how to read their
results. `python3 scripts/run_all.py --generate 500 --startup 5` records the environment, checks
the database roles, generates synthetic orders, exports every Bundle, runs the official HL7
FHIR Validator and HAPI `$validate`, checks the terminology, measures start-up time, memory and
processing time, compares every mapped SIL value with the FHIR output, injects faults into the
audit trail and counts the lines of each adapter. Each run writes into
`results/<UTC timestamp>/`. A k6 load test is provided separately (`scripts/run_load_test.py`).

**Reference results** (30 October 2026; MacBook Pro with a quad-core Intel Core i7 and 16 GB of
memory, Docker Desktop with 8 logical processors and 7.75 GiB available; HAPI FHIR 8.12.0):

| Check | Result |
|---|---|
| Bundles of the demonstration dataset accepted by HAPI FHIR | 38 of 38 (292 entries) |
| Validation errors in 133 Bundles (dataset and 95 generated orders); HL7 FHIR Validator 7.0.3 with `-allow-example-urls`, and HAPI `$validate` | 0 |
| Stored resources read and found by a generic client | 290 of 290 reads; 108 of 108 searches by patient; 38 of 38 by identifier |
| Discrepancies between mapped SIL values and FHIR elements | 0 |
| Distinct LOINC, SNOMED CT and UCUM codes valid on tx.fhir.org | 119 of 119 |
| Start-up time, median of five cold starts | 70 s |
| Peak memory of the five containers | 1,683 MiB |
| Processing time per order, median (warm terminology cache) | 0.12 s |
| Audit fault-injection checks passed | 13 of 13 |

The validators also report warnings, mostly because the local code systems under
`example.org` are unknown to any public server. The load test has not yet been run.

## Regenerating generated files

```bash
pip install openpyxl
python3 tools/build_conceptmap.py --cpal /path/to/CPAL_V7.0.xlsx   # ConceptMap and catalogue_resolved.json
python3 tools/build_seed.py                                         # seed SQL of SIL results and LIS-B, samples/oru
python3 tools/build_mapping_table.py                                # docs/h2/*.csv
```

`tools/build_seed.py` is deterministic, so the same catalogue always produces the same seed.

## Repository layout

```
docker-compose.yml           the five services
postgres/                    schemas, seeds, audit and terminology schemas, roles (run in order)
python-fhir-engine/
  main.py                    FastAPI application
  adapters/                  sil.py, lisb.py, oru_r01.py and the adapter contract
  canonical.py               canonical model shared by all sources
  fhir_mappers.py            canonical records → FHIR R4 resources and Bundles
  concept_map.py             dual coding from the ConceptMap
  conceptmaps/               lab-catalogue.json (generated)
  hl7v2.py                   HL7 v2 (ER7) parser
  systems.py                 identifier and code-system URIs
  tests/                     pytest suite
node-middleware/
  src/routes/                REST API
  src/services/              authentication, sync pipeline, terminology, audit, outbox, MLLP, HAPI client
  public/index.html          dashboard
  test/e2e/                  end-to-end test and mocks
hapi-fhir/application.yaml   HAPI FHIR configuration
catalogue/                   demonstration catalogue and synthetic data generator
samples/oru/                 HL7 v2.5.1 ORU^R01 sample messages
tools/                       generators of the ConceptMap, seed and mapping table
docs/h2/                     SIL-to-FHIR mapping table
scripts/                     evaluation scripts
```

## Security notes

This is a demonstration and research artefact, not a certified medical device or a
production-ready system. Before using it with real data:

- Replace every default password and set a strong `JWT_SECRET`. The demonstration users are
  defined in `node-middleware/src/services/authService.js`; a deployment would delegate
  authentication to the institution's identity provider.
- Put the HTTP services behind a reverse proxy with TLS. MLLP has no encryption of its own:
  restrict it with `MLLP_ALLOWED_HOSTS` and the network, or tunnel it.
- Do not publish ports 8000, 5432 and 5433 outside the host; the Python engine has no
  authentication of its own.
- `tx.fhir.org` is a public test server without a service-level agreement; use an
  institutional terminology server.
- Replace the `example.org` identifiers and code systems with those assigned by the
  institution, and confirm the ConceptMap and the UCUM units against the current national
  catalogues.
- Check the legal basis, records of processing and retention period with the institution's
  data protection officer; the 90-day payload retention is a design value, not a legal one.
