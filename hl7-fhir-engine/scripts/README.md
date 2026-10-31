# Evaluation scripts

Scripts that produce the evidence for the dissertation's hypotheses and
requirements on your own computer. They need **Docker** (Desktop on
Windows/macOS) and **Python 3.9 or later** (with Anaconda: `conda activate
fhir312`; `value_fidelity.py` refuses to run on 3.8); they use only the Python
standard library. The HL7 Validator (Java) and k6 run inside containers,
so neither needs to be installed.

Every script writes into `results/<UTC timestamp>/`. To make several
scripts write into the same folder, run them through `run_all.py`, or set
`RESULTS_DIR` yourself.

On Windows use `py` instead of `python3`, and run the commands from the
repository root in PowerShell.

## 0. Start from a clean stack

The database initialisation scripts changed (second schema, results
table, roles), and they only run on an empty volume:

```bash
docker compose down -v
docker compose up -d --build
python3 scripts/record_environment.py      # optional on its own; run_all.py does it
```

HAPI FHIR needs one to two minutes before `http://localhost:8080/fhir/metadata` answers.

## 1. Everything except the load test

```bash
python3 scripts/run_all.py --generate 500 --startup 5
```

`--generate 500` adds 500 synthetic SIL orders, 250 LIS-B requisitions and
50 ORU^R01 messages (see `generate_orders.py`). Without it, only the seed
(25 SIL orders, 8 LIS-B requisitions, 5 ORU messages) is evaluated.
`--startup 5` adds the five cold starts of C5.2 (about 10–15 minutes; the
whole stack is restarted five times, data kept).
Allow 30–60 minutes in all, mostly for the HL7 Validator and the
tx.fhir.org look-ups (one request per second, once per distinct code).

The functional checks (step 3 and 4 of `run_all.py`) can also be run on
their own, on a clean stack, before any generated order exists:

```bash
python3 scripts/stack_checks.py --startup 5       # roles, C1.1, C2.4, C3.1–C3.3, C5.2–C5.4, C7.2, C7.3
python3 scripts/value_fidelity.py --hapi --edge-cases   # C2.2
```

`stack_checks.py` takes 5–15 minutes without `--startup` (pass 1 waits for
tx.fhir.org); `value_fidelity.py` under a minute.

## 2. Load test

The middleware limits each client to 300 requests per minute; lift the
limit for the test and restore it afterwards:

```bash
RATE_LIMIT_PER_MIN=1000000 docker compose up -d node-middleware      # PowerShell: $env:RATE_LIMIT_PER_MIN=1000000; docker compose up -d node-middleware
python3 scripts/run_load_test.py --vus 1,5,10,20 --duration 60s
docker compose up -d node-middleware
```

## What each script produces

| Script | Output | Evidence for |
|---|---|---|
| `record_environment.py` | `environment.json`: OS, CPUs and memory visible to Docker, image digests, SHA-256 of the sources | reproducibility of every figure |
| `check_db_roles.py` | `db_roles/db_roles.md`: 26 allowed/forbidden operations per role, with the server's message | R1 (no writes to legacy systems), least privilege of the audit role |
| `generate_orders.py` | `generated/`: SQL, ORU files, manifest; `--delete` removes them | scale for H1, H7 and the load test |
| `export_bundles.py` | `bundles/<source>/<order>.json`, `bundles/summary.csv` | the exact Bundles every other script evaluates |
| `run_hl7_validator.py` → `summarise_validator.py` | `validator/SUMMARY.md`, `h1_by_source`, `h1_by_resource`, `h1_messages`, `issues.csv` | H1 (conformance), with the official HL7 Validator |
| `hapi_validate_all.py` | `hapi_validation/by_source.md` | H1, second opinion: HAPI's `$validate` |
| `terminology_resolution.py` | `terminology/SUMMARY.md`: coverage, ConceptMap resolution, validity per code system | H7 (LOINC, SNOMED CT, UCUM) |
| `adapter_line_counts.py` | `h6/line_counts.md` (`--baseline <old copy>` adds changed lines of shared code) | H6 (cost of a new source) |
| `audit_fault_injection.py` | `fault_injection/fault_injection.md` (`--hapi-outage` also stops HAPI) | fail-closed audit and the outbox |
| `capture_docker_stats.py` | `docker_stats/<label>.csv` and `_summary.md` | resource use at rest and under load |
| `run_load_test.py` (k6) | `load/load_results.md`: requests, throughput, error %, p50/p90/p95/p99, CPU and memory per container | performance |
| `send_oru_mllp.py` | `mllp/mllp_acks.csv` | MLLP round trip, ACK codes |
| `stack_checks.py` | `stack_checks/SUMMARY.md` (criterion, value, threshold, status) and one CSV per check: `roles.csv`, `c11_bundles_and_times.csv`, `c24_counts.csv`, `c31_read.csv`, `c32_search_by_*.csv`, `c33_capability.csv`, `c72_invalid_code.json`, `c73_unreachable.csv`, `c52_startup.csv` | role matrix (Table 6.2), C1.1, C2.4, C3.1–C3.3, C5.1 (partial), C5.2–C5.4, C7.2, C7.3 |
| `value_fidelity.py` | `value_fidelity/SUMMARY.md`, `checks_by_column.csv`, `discrepancies.csv`, `fallbacks.csv` | C2.2 (SIL source) |

## Reading the results honestly

* **H1.** The validator's verdict depends on its version and on the
  terminology server it consults; both are recorded in
  `validator/validator_run.json` and must be reported with the table.
  Every local URI lies under example.org on purpose, which the validator
  reports as an error ("Example URLs are not allowed in this context")
  unless `-allow-example-urls` is set; the script sets it by default and
  `--forbid-example-urls` shows the count without it. Report the setting.
  `--tx n/a` gives a structure-only run, which is useful to separate
  structural errors from terminology errors.
* **H7.** tx.fhir.org reflects the code-system versions it holds on the
  day of the run; record the date (it is in `tools/tx-cache.json`). The
  order-level LOINC codes in `fhir_mappers.MODALITY_LOINC` were inherited
  and have not been verified: read their rows in `tx_results.csv` and
  replace any code the server rejects. `display_matches` is informational
  (our displays come from the CPAL, in Portuguese or as SNOMED FSNs).
* **Local codes** (`http://example.org/...`) cannot be validated by any
  public server; the ConceptMap table reports how many of them have an
  automatic standard equivalent. The codes `sil-protocol`,
  `sil-analysis`, `sil-exam-group` and `lisb-department` are kept as
  provenance and have no target by design.
* **Load test.** Synthetic data, one machine, a closed workload model
  (each virtual user waits for its answer). Every authorised request is
  written to the access log, and every synchronisation commits an audit
  intent row before HAPI is called: both costs are part of what is
  measured. The terminology cache is warmed first, so the figures do not
  include tx.fhir.org latency.
* **Line counts** measure size, not effort or difficulty.
* **C2.2.** `value_fidelity.py` restates the rules of the H2 table
  (`docs/h2/sil_fhir_mapping.csv`) in its own code and compares, for every
  record, the value the rule predicts with the value in the Bundle (and,
  with `--hapi`, in HAPI). It shows that the implementation does what the
  table says; it cannot show that the table itself is clinically right,
  and both were written by the same person. The section "Rule branches
  exercised" lists how many records reached each branch: the seed does not
  reach some (cancelled lines, comparators, TX results…), which is why
  `--edge-cases` adds two orders that do (and deletes them afterwards).
  It covers the SIL source only. Numbers travel as JSON floats, so a value
  with more than about 15 significant digits would not be kept exactly;
  no laboratory value in the data comes close.
* **C5.2.** Measured from `docker compose up -d` (images present, Docker
  Desktop already running) until `/health` and HAPI's `/metadata` answer
  200. Volumes are kept, so HAPI's database is already initialised; with
  `--startup-volumes` every start begins from empty databases (and all
  stored data is deleted). Report which one you used.
* **C5.3.** The memory each container uses as `docker stats` reports it,
  summed per sample; on macOS and Windows this is memory inside Docker's
  virtual machine, not the virtual machine's own footprint. The load test reports
  the same peak under load (`all_mem_max_mib` in `load/load_results.md`,
  and the "ALL" row of each `docker_stats/<scenario>_vus*_summary.md`);
  report the larger.
* **C5.4.** Pass 2 of `stack_checks.py`, when every code is already in
  the terminology cache; pass 1 (cache filling, tx.fhir.org latency) is
  reported beside it for information.
* **C7.2.** The test assumes that `00000-0` is not a LOINC code; the
  server's own message is stored in `c72_invalid_code.json`; check it.
  `--invalid-code` chooses another code.
* **C7.3.** The middleware is restarted with `TERMINOLOGY_SERVER_URL`
  pointing at a closed port and an empty terminology cache, then restored;
  pass 4 sends the data once more so that HAPI and the cache end as in
  normal use.

## Troubleshooting

* `HTTP 429` in the load test: the rate limit was not lifted (step 2).
  `stack_checks.py` waits and retries when it meets the limit; with
  `--include-generated` lift the limit first, as for the load test.
* `stack_checks.py` reports C3.2 "Not met" with more resources than
  expected: other orders of the same patients were synchronised before
  (for instance generated ones). Rerun it on a clean stack.
* `permission denied` from `generate_orders.py`: it must run as the
  owner role (`sil_user`), which it does through `docker exec`; check
  that the container is called `sil-postgres` (or set `PG_CONTAINER`).
* The validator downloads its FHIR packages on first use into
  `tools/validator/fhir-cache`; a first run without Internet access fails.
