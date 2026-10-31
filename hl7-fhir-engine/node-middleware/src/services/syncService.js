'use strict';

const { hapiService, pythonService } = require('./fhirClient');
const terminologyService = require('./terminologyService');
const auditRepo = require('./auditRepo');
const logger = require('../utils/logger');

/**
 * syncService — the one path by which health data reaches HAPI.
 *
 *   1. obtain a FHIR transaction Bundle (from a legacy source, or received)
 *   2. optionally validate its structure with HAPI $validate
 *   3. validate every Coding / UCUM unit against the terminology server
 *   4. FAIL-CLOSED: commit an audit 'intent' row (with the Bundle);
 *      if that fails, stop here and send nothing
 *   5. POST the Bundle to HAPI
 *   6. record the outcome (database, or local outbox if the database fails)
 *
 * `actor` = { user_sub, user_name, role } of the user or system acting.
 */

async function persistBundle(bundle, actor, meta) {
  const base = {
    ...actor,
    operation: meta.operation,
    order_id: meta.order_id || null,
    bundle_id: bundle.id || null,
    resource_count: Array.isArray(bundle.entry) ? bundle.entry.length : 0,
  };
  const intent = await auditRepo.beginExchange(base, bundle); // throws AuditUnavailable -> nothing sent
  try {
    const result = await hapiService.postBundle(bundle);
    const audit = await auditRepo.completeExchange(intent, { status: 'ok', response_json: result });
    return { result, audit, exchange_id: intent.exchange_id };
  } catch (err) {
    const audit = await auditRepo.completeExchange(intent, {
      status: 'error',
      error_message: err.message,
      response_json: err.response?.data || null,
    });
    err.audit = audit;
    err.exchange_id = intent.exchange_id;
    throw err;
  }
}

const syncService = {
  persistBundle,

  /** Full pipeline for one order of a database source ('sil', 'lisb'). */
  async syncOrder(source, orderId, actor) {
    logger.info('sync_order_start', { source, orderId });
    const bundle = await pythonService.getBundle(source, orderId);
    const terminology = await terminologyService.validateBundle(bundle);
    const { result, audit, exchange_id } = await persistBundle(bundle, actor,
      { operation: 'sync_order', order_id: `${source}:${orderId}` });
    logger.info('sync_order_success', { source, orderId, entries: bundle.entry?.length, terminology, audit });
    return { success: true, source, orderId, entriesIn: bundle.entry?.length, terminology, audit, exchange_id,
      result, bundle };
  },

  /**
   * Synchronise up to `limit` orders of one source, each through the full
   * pipeline (and therefore each with its own audit intent and outcome).
   */
  async syncAll(source, limit, actor) {
    const { orders } = await pythonService.listOrders(source, { limit });
    const results = [];
    for (const id of orders) {
      try {
        const r = await this.syncOrder(source, id, actor);
        results.push({ orderId: id, ok: true, entries: r.entriesIn, audit: r.audit });
      } catch (err) {
        results.push({ orderId: id, ok: false, error: err.message });
        if (err instanceof auditRepo.AuditUnavailable) break; // fail-closed: stop the batch
      }
    }
    return { source, total: orders.length, ok: results.filter((r) => r.ok).length, results };
  },

  /**
   * Received content (a FHIR Bundle from another institution, or the
   * Bundle produced from an HL7 v2 message): structural validation,
   * terminology, persistence. Persists even when validation reports
   * issues (they are recorded in the trace), so data is never silently
   * dropped; set `rejectInvalid` to refuse Bundles with errors instead.
   */
  async ingest(bundle, actor, meta, { structural = true, rejectInvalid = false } = {}) {
    const trace = { stages: {} };
    if (structural) {
      const s = await hapiService.validateResource('Bundle', bundle);
      trace.stages.structural = { ok: s.ok, counts: s.counts, issues: s.issues };
      if (rejectInvalid && !s.ok) {
        trace.overall = 'rejected_structural';
        return trace;
      }
    }
    if (terminologyService.enabled()) {
      trace.stages.terminology = { ok: true, ...(await terminologyService.validateBundle(bundle)) };
    } else {
      trace.stages.terminology = { ok: true, skipped: true, reason: 'TERMINOLOGY_ENABLED=false' };
    }
    const { result, audit, exchange_id } = await persistBundle(bundle, actor, meta);
    const entries = (result?.entry || []).map((e) => ({ status: e.response?.status || 'unknown',
      location: e.response?.location || null }));
    trace.stages.persist = { ok: true, entries_created: entries.filter((e) => /^20[01]/.test(e.status)).length,
      entries_total: entries.length, hapi_response_summary: entries, hapi_response: result };
    trace.audit = audit;
    trace.exchange_id = exchange_id;
    trace.overall = 'ok';
    return trace;
  },
};

module.exports = syncService;
