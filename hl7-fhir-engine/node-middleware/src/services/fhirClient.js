'use strict';

const axios = require('axios');
const logger = require('../utils/logger');

const HAPI_BASE = process.env.HAPI_FHIR_URL || 'http://hapi-fhir:8080/fhir';
const PYTHON_ENGINE = process.env.PYTHON_ENGINE_URL || 'http://python-fhir-engine:8000';

// ─── Axios instance for HAPI FHIR ────────────────────────────────────────────
const hapiClient = axios.create({
  baseURL: HAPI_BASE,
  timeout: 30_000,
  headers: {
    'Content-Type': 'application/fhir+json',
    Accept: 'application/fhir+json',
  },
});

// ─── Axios instance for Python Engine ────────────────────────────────────────
const pythonClient = axios.create({
  baseURL: PYTHON_ENGINE,
  timeout: 30_000,
  headers: { 'Content-Type': 'application/fhir+json' },
});

// ─── Retry helper ────────────────────────────────────────────────────────────
// 3 attempts, waiting 1 s then 2 s; client errors (4xx) are not retried.
async function withRetry(fn, retries = 3, delay = 1000) {
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      return await fn();
    } catch (err) {
      logger.warn('request_failed', {
        attempt,
        retries,
        error: err.message,
        status: err.response?.status,
      });
      const st = err.response?.status;
      if (attempt === retries || (st >= 400 && st < 500)) throw err;
      await new Promise((r) => setTimeout(r, delay * attempt));
    }
  }
}

// ─── HAPI FHIR Operations ─────────────────────────────────────────────────────
const hapiService = {
  /**
   * POST a transaction Bundle to HAPI FHIR.
   */
  async postBundle(bundle) {
    return withRetry(() =>
      hapiClient.post('/', bundle).then((r) => r.data)
    );
  },

  /**
   * GET any FHIR resource by type + id.
   */
  async getResource(resourceType, id) {
    return withRetry(() =>
      hapiClient.get(`/${resourceType}/${id}`).then((r) => r.data)
    );
  },

  /**
   * Search FHIR resources.
   */
  async searchResource(resourceType, params = {}) {
    return withRetry(() =>
      hapiClient.get(`/${resourceType}`, { params }).then((r) => r.data)
    );
  },

  /**
   * PUT (upsert) a FHIR resource.
   */
  async putResource(resourceType, id, resource) {
    return withRetry(() =>
      hapiClient.put(`/${resourceType}/${id}`, resource).then((r) => r.data)
    );
  },

  /**
   * DELETE a FHIR resource.
   */
  async deleteResource(resourceType, id) {
    return withRetry(() =>
      hapiClient.delete(`/${resourceType}/${id}`).then((r) => r.data)
    );
  },

  /**
   * Validate a resource against HAPI's FhirValidator.
   *
   * Calls the FHIR R4 $validate operation on the appropriate resource
   * type endpoint. HAPI returns an OperationOutcome resource with an
   * issue array; we wrap it in a normalised summary so callers don't
   * have to walk the FHIR structure themselves.
   *
   * @param resourceType - e.g. "Bundle", "Patient"
   * @param resource     - the resource to validate
   * @returns {ok, issues, counts, raw}
   *    ok      = true iff no error/fatal-severity issues
   *    issues  = [{severity, code, diagnostics, location}, ...]
   *    counts  = {error, warning, information, fatal, total}
   *    raw     = the OperationOutcome resource returned by HAPI
   */
  async validateResource(resourceType, resource) {
    try {
      const res = await hapiClient.post(`/${resourceType}/$validate`, resource);
      return _parseOperationOutcome(res.data);
    } catch (err) {
      // HAPI returns 4xx + OperationOutcome on validation failure;
      // axios throws, but the response body still carries the outcome.
      if (err.response?.data?.resourceType === 'OperationOutcome') {
        return _parseOperationOutcome(err.response.data);
      }
      // True transport failure — surface it as a single fatal issue
      return {
        ok: false,
        issues: [{
          severity: 'fatal',
          code: 'exception',
          diagnostics: `HAPI $validate unreachable: ${err.message}`,
          location: null,
        }],
        counts: { fatal: 1, error: 0, warning: 0, information: 0, total: 1 },
        raw: null,
      };
    }
  },

  /**
   * Check HAPI FHIR health.
   */
  async healthCheck() {
    return hapiClient
      .get('/metadata')
      .then((r) => ({ ok: true, version: r.data?.fhirVersion }))
      .catch((e) => ({ ok: false, error: e.message }));
  },
};

function _parseOperationOutcome(outcome) {
  const issues = (outcome?.issue || []).map((iss) => ({
    severity: iss.severity || 'information',
    code: iss.code || 'unknown',
    diagnostics: iss.diagnostics || iss.details?.text || '',
    location: iss.location?.[0] || iss.expression?.[0] || null,
  }));
  const counts = { fatal: 0, error: 0, warning: 0, information: 0, total: issues.length };
  for (const i of issues) {
    if (counts[i.severity] != null) counts[i.severity]++;
  }
  const ok = counts.fatal === 0 && counts.error === 0;
  return { ok, issues, counts, raw: outcome };
}

// ─── Python Engine Operations ─────────────────────────────────────────────────
const pythonService = {
  /** Transaction Bundle for one order of a database source ('sil', 'lisb'). */
  async getBundle(source, orderId) {
    if (orderId === undefined) { orderId = source; source = 'sil'; } // old signature getBundle(numpedido)
    return pythonClient
      .get(`/fhir/bundle/${encodeURIComponent(source)}/order/${encodeURIComponent(orderId)}`)
      .then((r) => r.data);
  },

  async listOrders(source, params = {}) {
    return pythonClient.get(`/sources/${encodeURIComponent(source)}/orders`, { params }).then((r) => r.data);
  },

  async sources() {
    return pythonClient.get('/sources').then((r) => r.data);
  },

  /** HL7 v2 ORU^R01 (ER7 text) -> { control_id, orders, bundle }. */
  async transformOru(er7) {
    return pythonClient
      .post('/v2/oru/transform', er7, { headers: { 'Content-Type': 'x-application/hl7-v2+er7' } })
      .then((r) => r.data);
  },

  async getPatients(params = {}) {
    return pythonClient.get('/fhir/Patient', { params }).then((r) => r.data);
  },

  async getServiceRequests(params = {}) {
    return pythonClient.get('/fhir/ServiceRequest', { params }).then((r) => r.data);
  },

  async getImagingStudies(params = {}) {
    return pythonClient.get('/fhir/ImagingStudy', { params }).then((r) => r.data);
  },

  async getDiagnosticReports(params = {}) {
    return pythonClient.get('/fhir/DiagnosticReport', { params }).then((r) => r.data);
  },

  async getObservations(params = {}) {
    return pythonClient.get('/fhir/Observation', { params }).then((r) => r.data);
  },

  async healthCheck() {
    return pythonClient
      .get('/health')
      .then((r) => ({ ok: r.data?.status === 'ok', ...r.data }))
      .catch((e) => ({ ok: false, error: e.message }));
  },
};

module.exports = { hapiService, pythonService };
