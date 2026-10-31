'use strict';

/**
 * terminologyService.js
 *
 * Validates FHIR Coding entries against an external terminology server
 * (default: tx.fhir.org/r4, configurable via TERMINOLOGY_SERVER_URL).
 *
 * Implements:
 *  - In-memory cache (fast path, per-process)
 *  - Postgres cache (audit.terminology_validation, durable across restarts)
 *  - Graceful offline mode: if the server is unreachable, the code is
 *    recorded as unknown (NOT invalid) so we don't penalise codes for
 *    a transient network issue.
 *  - Bundle walker: extracts every Coding from a FHIR Bundle, validates
 *    each unique (system, code) pair, and tags unresolved ones with a
 *    FHIR extension on the parent CodeableConcept.
 */

const { Pool } = require('pg');
const axios = require('axios');
const logger = require('../utils/logger');

const TERMINOLOGY_SERVER_URL =
  process.env.TERMINOLOGY_SERVER_URL || 'https://tx.fhir.org/r4';
const TERMINOLOGY_ENABLED = process.env.TERMINOLOGY_ENABLED !== 'false';
const VALIDATION_TIMEOUT_MS = parseInt(process.env.TERMINOLOGY_TIMEOUT_MS || '5000', 10);

const DATABASE_URL =
  process.env.AUDIT_DATABASE_URL ||
  process.env.DATABASE_URL ||
  'postgresql://sil_user:sil_pass@postgres:5432/sildb';

const pool = new Pool({ connectionString: DATABASE_URL, max: 5 });
pool.on('error', (err) => logger.error('term_pool_error', { error: err.message }));

const memCache = new Map(); // key: "system|code" → { is_valid, display_canonical, message }
// Code systems submitted to the terminology server. Local systems
// (http://example.org/fhir/CodeSystem/...) are never sent: no external
// server can know them; they are counted as 'local' instead.
const KNOWN_SYSTEMS = new Set([
  'http://loinc.org',
  'http://snomed.info/sct',
  'http://unitsofmeasure.org', // UCUM
  'http://terminology.hl7.org/CodeSystem/v2-0074',
  'http://terminology.hl7.org/CodeSystem/observation-category',
  'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation',
  'http://dicom.nema.org/resources/ontology/DCM',
]);
const UCUM = 'http://unitsofmeasure.org';

// ─── Bootstrap: check schema is present ─────────────────────────────────────
let ready = false;
async function bootstrap() {
  if (!TERMINOLOGY_ENABLED) {
    logger.info('terminology_disabled');
    return;
  }
  for (let attempt = 1; attempt <= 30; attempt++) {
    try {
      const r = await pool.query(
        "SELECT 1 FROM information_schema.tables " +
        "WHERE table_schema='audit' AND table_name='terminology_validation'"
      );
      if (r.rowCount > 0) {
        ready = true;
        logger.info('terminology_db_ready', {
          server: TERMINOLOGY_SERVER_URL,
          attempt,
        });
        return;
      }
    } catch (err) {
      // table not present yet
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
  logger.error('terminology_db_bootstrap_failed');
}
bootstrap();

// ─── Cache lookup ───────────────────────────────────────────────────────────
async function cacheGet(system, code) {
  const k = `${system}|${code}`;
  if (memCache.has(k)) return memCache.get(k);
  if (!ready) return null;

  try {
    const r = await pool.query(
      `SELECT is_valid, display_canonical, message, revalidate_after
       FROM audit.terminology_validation
       WHERE code_system=$1 AND code=$2`,
      [system, code]
    );
    if (r.rowCount === 0) return null;

    const row = r.rows[0];
    // Cache hit, but is it still fresh?
    if (new Date(row.revalidate_after) > new Date()) {
      const entry = {
        is_valid: row.is_valid,
        display_canonical: row.display_canonical,
        message: row.message,
      };
      memCache.set(k, entry);
      return entry;
    }
    // Stale — return null to force revalidation
    return null;
  } catch (err) {
    logger.error('term_cache_get_failed', { error: err.message });
    return null;
  }
}

async function cachePut(system, code, displayLocal, result) {
  const k = `${system}|${code}`;
  memCache.set(k, {
    is_valid: result.is_valid,
    display_canonical: result.display_canonical,
    message: result.message,
  });
  if (!ready) return;

  try {
    await pool.query(
      `INSERT INTO audit.terminology_validation
         (code_system, code, display_local, is_valid, display_canonical,
          message, server_url, validated_at, revalidate_after, times_seen, last_seen_at)
       VALUES ($1,$2,$3,$4,$5,$6,$7, now(), now() + INTERVAL '24 hours', 1, now())
       ON CONFLICT (code_system, code) DO UPDATE
         SET is_valid          = EXCLUDED.is_valid,
             display_canonical = EXCLUDED.display_canonical,
             message           = EXCLUDED.message,
             server_url        = EXCLUDED.server_url,
             validated_at      = now(),
             revalidate_after  = now() + INTERVAL '24 hours',
             times_seen        = audit.terminology_validation.times_seen + 1,
             last_seen_at      = now()`,
      [system, code, displayLocal || null, result.is_valid,
       result.display_canonical || null, result.message || null,
       TERMINOLOGY_SERVER_URL]
    );
  } catch (err) {
    logger.error('term_cache_put_failed', { error: err.message });
  }
}

// Bump times_seen even on cache hits (so dashboard counts reflect reality)
async function bumpSeen(system, code) {
  if (!ready) return;
  try {
    await pool.query(
      `UPDATE audit.terminology_validation
         SET times_seen = times_seen + 1, last_seen_at = now()
       WHERE code_system=$1 AND code=$2`,
      [system, code]
    );
  } catch (err) {
    // best-effort
  }
}

// ─── Validate one code against the terminology server ──────────────────────
async function callTerminologyServer(system, code, displayLocal) {
  const url = `${TERMINOLOGY_SERVER_URL}/CodeSystem/$validate-code`;
  try {
    // Only the code is validated. Displays are not sent: the engine's
    // displays are often Portuguese, and a display mismatch would be
    // reported as a failure although the code itself resolves.
    const params = { url: system, code };
    const res = await axios.get(url, {
      params,
      timeout: VALIDATION_TIMEOUT_MS,
      headers: { Accept: 'application/fhir+json' },
    });
    return parseValidateCodeResponse(res.data);
  } catch (err) {
    return {
      reachable: false,
      is_valid: null,
      message: err.message,
    };
  }
}

function parseValidateCodeResponse(parametersResource) {
  // The server returns a FHIR Parameters resource:
  // { resourceType: "Parameters", parameter: [ {name,valueBoolean}, ... ] }
  const params = (parametersResource && parametersResource.parameter) || [];
  const find = (name) => params.find((p) => p.name === name);

  const result = find('result');
  const message = find('message');
  const display = find('display');

  return {
    reachable: true,
    is_valid: result ? !!result.valueBoolean : null,
    message: message ? message.valueString : null,
    display_canonical: display ? display.valueString : null,
  };
}

// ─── Public: validate one code ──────────────────────────────────────────────
/**
 * Returns { is_valid, display_canonical, message, source, fresh }
 *   source = "cache" | "server" | "offline" | "disabled" | "unknown-system"
 *   fresh  = true if just queried the server, false if cache hit
 */
async function validateCode(system, code, displayLocal) {
  if (!TERMINOLOGY_ENABLED) {
    return { is_valid: null, source: 'disabled', fresh: false };
  }
  if (!system || !code) {
    return { is_valid: false, source: 'invalid-input', fresh: false,
             message: 'system or code missing' };
  }
  // Codes from the legacy SIL local namespace (http://sil.hospital.pt/...)
  // are not in any external terminology and we won't ask the server about them.
  if (!KNOWN_SYSTEMS.has(system)) {
    return {
      is_valid: null,
      source: 'local',
      fresh: false,
      message: `Local code system '${system}' (not submitted to the terminology server)`,
    };
  }

  const cached = await cacheGet(system, code);
  if (cached) {
    await bumpSeen(system, code);
    return { ...cached, source: 'cache', fresh: false };
  }

  const fromServer = await callTerminologyServer(system, code, displayLocal);
  if (!fromServer.reachable) {
    // Offline — DO NOT cache as invalid, return as unknown
    logger.warn('terminology_offline', { system, code, error: fromServer.message });
    return {
      is_valid: null,
      source: 'offline',
      fresh: false,
      message: `Terminology server unreachable: ${fromServer.message}`,
    };
  }

  const result = {
    is_valid: !!fromServer.is_valid,
    display_canonical: fromServer.display_canonical,
    message: fromServer.message,
  };
  await cachePut(system, code, displayLocal, result);
  return { ...result, source: 'server', fresh: true };
}

// ─── Public: walk a FHIR Bundle, validate all Codings, tag the unresolved ──
/**
 * Mutates `bundle` in place: every CodeableConcept whose Coding(s) fail
 * validation receives a FHIR extension flagging the issue.
 *
 * Returns { totalCodings, uniqueCodings, validated, unresolved, offline }
 */
async function validateBundle(bundle) {
  const stats = {
    totalCodings: 0,
    uniqueCodings: 0,
    validated: 0,
    unresolved: 0,
    offline: 0,
    local: 0,
    perSystem: {},
  };
  if (!bundle || bundle.resourceType !== 'Bundle' || !Array.isArray(bundle.entry)) {
    return stats;
  }

  const uniqueKeys = new Set();
  const tasks = [];

  walkValue(bundle, (codeableConcept, parent) => {
    // A Quantity with a UCUM code is validated like a single Coding.
    const codings = codeableConcept.__quantity
      ? [{ system: codeableConcept.system, code: codeableConcept.code, display: codeableConcept.unit }]
      : codeableConcept.coding || [];
    let anyUnresolved = false;
    const issues = [];

    for (const coding of codings) {
      stats.totalCodings++;
      const k = `${coding.system}|${coding.code}`;
      if (!uniqueKeys.has(k)) {
        uniqueKeys.add(k);
        stats.uniqueCodings++;
      }
      stats.perSystem[coding.system] = (stats.perSystem[coding.system] || 0) + 1;

      // We validate synchronously-but-batched: collect promises
      tasks.push(
        validateCode(coding.system, coding.code, coding.display).then((r) => {
          if (r.is_valid === true) {
            stats.validated++;
            // Optionally replace display with canonical
            if (r.display_canonical && !coding.display) {
              coding.display = r.display_canonical;
            }
          } else if (r.is_valid === false) {
            stats.unresolved++;
            anyUnresolved = true;
            issues.push({
              system: coding.system,
              code: coding.code,
              reason: r.message || 'unresolved',
              source: r.source,
            });
          } else if (r.source === 'local') {
            stats.local++;
          } else {
            // is_valid === null → offline / disabled
            stats.offline++;
          }
        })
      );
    }

    // We'll attach the extension after all validations finish (see below)
    codeableConcept.__pendingIssues = issues; // marker, removed after await
  });

  // Wait for all validations
  await Promise.all(tasks);

  // Second pass: attach extensions where needed
  walkValue(bundle, (codeableConcept) => {
    const issues = codeableConcept.__pendingIssues;
    if (issues && issues.length > 0) {
      codeableConcept.extension = codeableConcept.extension || [];
      codeableConcept.extension.push({
        url: 'http://example.org/fhir/StructureDefinition/terminology-validation-issue',
        extension: issues.map((i) => ({
          url: 'issue',
          valueString: `${i.system}|${i.code} - ${i.reason}`,
        })),
      });
    }
    delete codeableConcept.__pendingIssues;
    delete codeableConcept.__quantity;
  });

  return stats;
}

/** Recursively walk a JSON tree, calling visit() on every CodeableConcept and UCUM Quantity. */
function walkValue(node, visit, parent = null) {
  if (Array.isArray(node)) {
    for (const item of node) walkValue(item, visit, parent);
    return;
  }
  if (node && typeof node === 'object') {
    // Heuristic: it's a CodeableConcept if it has a `coding` array
    if (Array.isArray(node.coding)) {
      visit(node, parent);
    } else if (node.system === UCUM && typeof node.code === 'string' && 'value' in node) {
      Object.defineProperty(node, '__quantity', { value: true, configurable: true, enumerable: false });
      visit(node, parent);
    }
    for (const key of Object.keys(node)) {
      walkValue(node[key], visit, node);
    }
  }
}

// ─── Public: metrics ─────────────────────────────────────────────────────────
async function getMetrics() {
  if (!ready) return { ready: false };
  try {
    const summary = await pool.query('SELECT * FROM audit.terminology_summary');
    const unresolved = await pool.query(
      `SELECT code_system, code, display_local, message, times_seen, last_seen_at
       FROM audit.terminology_validation
       WHERE is_valid = false
       ORDER BY times_seen DESC, last_seen_at DESC
       LIMIT 50`
    );
    return {
      ready: true,
      server: TERMINOLOGY_SERVER_URL,
      enabled: TERMINOLOGY_ENABLED,
      summary: summary.rows,
      topUnresolved: unresolved.rows,
    };
  } catch (err) {
    logger.error('term_metrics_failed', { error: err.message });
    return { ready: false, error: err.message };
  }
}

// ─── Public: batch revalidation (called by daily cron) ──────────────────────
async function revalidateAll() {
  if (!ready || !TERMINOLOGY_ENABLED) return { revalidated: 0 };
  let count = 0;
  try {
    const stale = await pool.query(
      `SELECT code_system, code, display_local
       FROM audit.terminology_validation
       WHERE revalidate_after < now()
       LIMIT 500`
    );
    for (const row of stale.rows) {
      // clear the mem cache entry so validateCode goes through to the server
      memCache.delete(`${row.code_system}|${row.code}`);
      await validateCode(row.code_system, row.code, row.display_local);
      count++;
    }
    return { revalidated: count };
  } catch (err) {
    logger.error('term_revalidate_failed', { error: err.message });
    return { revalidated: count, error: err.message };
  }
}

module.exports = {
  validateCode,
  validateBundle,
  getMetrics,
  revalidateAll,
  isReady: () => ready,
  serverUrl: () => TERMINOLOGY_SERVER_URL,
  enabled: () => TERMINOLOGY_ENABLED,
};
