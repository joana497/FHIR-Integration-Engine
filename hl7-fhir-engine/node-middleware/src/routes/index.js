'use strict';

const express = require('express');
const { hapiService, pythonService } = require('../services/fhirClient');
const syncService = require('../services/syncService');
const auth = require('../services/authService');
const auditRepo = require('../services/auditRepo');
const terminologyService = require('../services/terminologyService');
const mllp = require('../services/mllpServer');
const logger = require('../utils/logger');

const router = express.Router();
const { requireAuth, requirePermission } = auth;

const actorOf = (req) => ({ user_sub: req.user.sub, user_name: req.user.name, role: req.user.role });
const SOURCE_RE = /^[a-z][a-z0-9_-]{0,20}$/;

// ════════════════════════════════════════════════════════════════════════════
//  AUTH (public)
// ════════════════════════════════════════════════════════════════════════════
router.post('/auth/login', async (req, res, next) => {
  try {
    const { username, password, role } = req.body || {};
    const result = await auth.login(username, password, role);
    if (!result.ok) {
      auth.securityEvent(req, 'DENY', `Login falhado — utilizador: ${String(username).slice(0, 40)}`, 401,
        String(username || '').slice(0, 64));
      return res.status(401).json(result);
    }
    auth.securityEvent(req, 'AUTH', `Login bem-sucedido — ${result.name} [${result.role}]`, 200, username);
    res.json(result);
  } catch (e) { next(e); }
});

router.get('/auth/me', requireAuth, (req, res) => {
  res.json({
    sub: req.user.sub, name: req.user.name, role: req.user.role,
    scopes: (req.user.scope || '').split(' ').filter(Boolean),
    exp: req.user.exp, iat: req.user.iat, permissions: auth.PERMISSIONS[req.user.role] || {},
  });
});

router.post('/auth/logout', requireAuth, (req, res) => {
  auth.securityEvent(req, 'AUTH', `Sessão terminada — ${req.user.name}`, 200);
  res.json({ ok: true });
});

// ════════════════════════════════════════════════════════════════════════════
//  HEALTH (public)
// ════════════════════════════════════════════════════════════════════════════
router.get('/health', async (req, res) => {
  const [hapi, python] = await Promise.all([hapiService.healthCheck(), pythonService.healthCheck()]);
  res.json({
    service: 'node-middleware',
    uptime: process.uptime(),
    hapi,
    python,
    audit_db: auditRepo.isReady() ? 'ready' : 'not-ready',
    audit_outbox_pending: auditRepo.outboxPending(),
  });
});

// ════════════════════════════════════════════════════════════════════════════
//  AUDIT (requires 'audit')
// ════════════════════════════════════════════════════════════════════════════
const audit = [requireAuth, requirePermission('audit')];

router.get('/audit', audit, async (req, res, next) => {
  try {
    res.json(await auditRepo.listSecurity({
      limit: parseInt(req.query.limit) || 200, offset: parseInt(req.query.offset) || 0,
      level: req.query.level || undefined, userSub: req.query.user || undefined,
    }));
  } catch (e) { next(e); }
});

router.get('/audit/exchanges', audit, async (req, res, next) => {
  try {
    res.json(await auditRepo.listExchanges({
      limit: parseInt(req.query.limit) || 100, offset: parseInt(req.query.offset) || 0,
      userSub: req.query.user || undefined, orderId: req.query.order || undefined,
      status: req.query.status || undefined,
    }));
  } catch (e) { next(e); }
});

router.get('/audit/exchanges/:id', audit, async (req, res, next) => {
  try {
    const row = await auditRepo.getExchange(req.params.id);
    if (!row) return res.status(404).json({ error: 'Exchange not found' });
    res.json(row);
  } catch (e) { next(e); }
});

function rowsToCsv(rows, columns) {
  const esc = (v) => {
    if (v == null) return '';
    const s = v instanceof Date ? v.toISOString() : typeof v === 'string' ? v
      : typeof v === 'number' || typeof v === 'boolean' ? String(v) : JSON.stringify(v);
    return '"' + s.replace(/"/g, '""') + '"';
  };
  return columns.join(',') + '\n' + rows.map((r) => columns.map((c) => esc(r[c])).join(',')).join('\n') + '\n';
}
const EXCHANGE_CSV = ['exchange_id', 'occurred_at', 'completed_at', 'user_sub', 'user_name', 'role', 'operation',
  'order_id', 'bundle_id', 'resource_count', 'status', 'error_message', 'payload_purged'];
const SECURITY_CSV = ['event_id', 'occurred_at', 'level', 'user_sub', 'message', 'ip_address', 'method', 'path',
  'status_code'];

router.get('/audit/exchanges.csv', audit, async (req, res, next) => {
  try {
    const { entries } = await auditRepo.listExchanges({ limit: 100000 });
    res.set('Content-Type', 'text/csv; charset=utf-8');
    res.set('Content-Disposition', `attachment; filename="exchanges-${Date.now()}.csv"`);
    res.send(rowsToCsv(entries, EXCHANGE_CSV));
  } catch (e) { next(e); }
});
router.get('/audit/exchanges.json', audit, async (req, res, next) => {
  try {
    res.set('Content-Disposition', `attachment; filename="exchanges-${Date.now()}.json"`);
    res.json(await auditRepo.listExchanges({ limit: 100000 }));
  } catch (e) { next(e); }
});
router.get('/audit/security.csv', audit, async (req, res, next) => {
  try {
    const { entries } = await auditRepo.listSecurity({ limit: 100000 });
    res.set('Content-Type', 'text/csv; charset=utf-8');
    res.set('Content-Disposition', `attachment; filename="security-${Date.now()}.csv"`);
    res.send(rowsToCsv(entries, SECURITY_CSV));
  } catch (e) { next(e); }
});
router.get('/audit/security.json', audit, async (req, res, next) => {
  try {
    res.set('Content-Disposition', `attachment; filename="security-${Date.now()}.json"`);
    res.json(await auditRepo.listSecurity({ limit: 100000 }));
  } catch (e) { next(e); }
});

// ════════════════════════════════════════════════════════════════════════════
//  TERMINOLOGY
// ════════════════════════════════════════════════════════════════════════════
router.get('/terminology/metrics', requireAuth, requirePermission('read'), async (req, res, next) => {
  try { res.json(await terminologyService.getMetrics()); } catch (e) { next(e); }
});
router.post('/terminology/revalidate', requireAuth, requirePermission('admin'), async (req, res, next) => {
  try { res.json(await terminologyService.revalidateAll()); } catch (e) { next(e); }
});

// ════════════════════════════════════════════════════════════════════════════
//  SOURCES AND FHIR READS (requires 'read')
// ════════════════════════════════════════════════════════════════════════════
const read = [requireAuth, requirePermission('read')];

router.get('/api/sources', read, async (req, res, next) => {
  try { res.json(await pythonService.sources()); } catch (e) { next(e); }
});
router.get('/api/sources/:source/orders', read, async (req, res, next) => {
  try {
    if (!SOURCE_RE.test(req.params.source)) return res.status(400).json({ error: 'invalid source' });
    res.json(await pythonService.listOrders(req.params.source, {
      limit: Math.min(parseInt(req.query.limit) || 50, 5000), offset: parseInt(req.query.offset) || 0 }));
  } catch (e) { next(e); }
});

router.get('/fhir/Patient', read, async (req, res, next) => {
  try { res.json(await pythonService.getPatients(req.query)); } catch (e) { next(e); }
});
router.get('/fhir/ServiceRequest', read, async (req, res, next) => {
  try { res.json(await pythonService.getServiceRequests(req.query)); } catch (e) { next(e); }
});
router.get('/fhir/ImagingStudy', read, async (req, res, next) => {
  try { res.json(await pythonService.getImagingStudies(req.query)); } catch (e) { next(e); }
});
router.get('/fhir/DiagnosticReport', read, async (req, res, next) => {
  try { res.json(await pythonService.getDiagnosticReports(req.query)); } catch (e) { next(e); }
});
router.get('/fhir/Observation', read, async (req, res, next) => {
  try { res.json(await pythonService.getObservations(req.query)); } catch (e) { next(e); }
});

/** The FHIR transaction Bundle the engine builds for one order (no write). */
router.get('/api/fhir/sample-bundle/:source/:orderId', read, async (req, res, next) => {
  try {
    if (!SOURCE_RE.test(req.params.source)) return res.status(400).json({ error: 'invalid source' });
    res.json(await pythonService.getBundle(req.params.source, req.params.orderId));
  } catch (e) { next(e); }
});
router.get('/api/fhir/sample-bundle/:numpedido', read, async (req, res, next) => {
  try { res.json(await pythonService.getBundle('sil', req.params.numpedido)); } catch (e) { next(e); }
});

// ── HAPI $validate on the Bundle of one order ───────────────────────────────
async function hapiValidation(req, res, next) {
  try {
    const source = req.params.source || 'sil';
    const orderId = req.params.orderId || req.params.numpedido;
    const bundle = await pythonService.getBundle(source, orderId);
    const result = await hapiService.validateResource('Bundle', bundle);
    res.json({ source, numpedido: orderId, validator: 'HAPI FHIR $validate (Bundle)', ok: result.ok,
      counts: result.counts, issues: result.issues });
  } catch (e) { next(e); }
}
router.get('/fhir/bundle/:source/order/:orderId/hapi-validation', read, hapiValidation);
router.get('/fhir/bundle/order/:numpedido/hapi-validation', read, hapiValidation);

// ── HAPI read proxy ─────────────────────────────────────────────────────────
router.get('/hapi/fhir/:resourceType', read, async (req, res, next) => {
  try { res.json(await hapiService.searchResource(req.params.resourceType, req.query)); } catch (e) { next(e); }
});
router.get('/hapi/fhir/:resourceType/:id', read, async (req, res, next) => {
  try { res.json(await hapiService.getResource(req.params.resourceType, req.params.id)); } catch (e) { next(e); }
});

// ════════════════════════════════════════════════════════════════════════════
//  WRITES — every one goes through syncService (fail-closed audit)
// ════════════════════════════════════════════════════════════════════════════
const write = [requireAuth, requirePermission('write')];
const sync = [requireAuth, requirePermission('sync')];

function writeError(res, next, e) {
  if (e instanceof auditRepo.AuditUnavailable) {
    return res.status(503).json({ error: e.message, code: 'AUDIT_UNAVAILABLE' });
  }
  return next(e);
}

/** Store a FHIR Bundle as received (no validation stages). */
router.post('/api/fhir/bundle', write, async (req, res, next) => {
  const bundle = req.body;
  if (!bundle || bundle.resourceType !== 'Bundle') return res.status(400).json({ error: 'Expected a FHIR Bundle' });
  try {
    const { result, audit: where, exchange_id } = await syncService.persistBundle(bundle, actorOf(req),
      { operation: 'bundle_post' });
    res.json({ ok: true, exchange_id, audit: where, hapiResponse: result });
  } catch (e) { writeError(res, next, e); }
});

/**
 * External reception: another institution sends a FHIR Bundle. Runs
 * structural validation, terminology validation and persistence and
 * returns the trace of every stage. ?reject_invalid=true refuses Bundles
 * that HAPI's validator reports as invalid.
 */
router.post('/api/fhir/ingest', write, async (req, res, next) => {
  const bundle = req.body;
  if (!bundle || bundle.resourceType !== 'Bundle') return res.status(400).json({ error: 'Expected a FHIR Bundle' });
  const source = String(req.get('X-Source-Institution') || 'external').slice(0, 60);
  const started = new Date().toISOString();
  try {
    const trace = await syncService.ingest(bundle, actorOf(req),
      { operation: 'bundle_ingest', order_id: `ext:${source}` },
      { structural: true, rejectInvalid: req.query.reject_invalid === 'true' });
    Object.assign(trace, { started_at: started, finished_at: new Date().toISOString(), source,
      received_by: actorOf(req), bundle_id: bundle.id || null,
      entry_count: Array.isArray(bundle.entry) ? bundle.entry.length : 0 });
    trace.stages = { receive: { ok: true, message: `Bundle received from '${source}'` }, ...trace.stages };
    res.status(trace.overall === 'ok' ? 200 : 422).json({ ok: trace.overall === 'ok', trace });
  } catch (e) {
    if (e instanceof auditRepo.AuditUnavailable) return writeError(res, next, e);
    res.status(502).json({ ok: false, error: e.message, exchange_id: e.exchange_id || null });
  }
});

/** HL7 v2 ORU^R01 over HTTP (same handler as the MLLP listener). */
router.post('/api/v2/oru', write, async (req, res) => {
  const er7 = typeof req.body === 'string' ? req.body : '';
  if (!er7.startsWith('MSH')) return res.status(400).json({ error: 'Expected an HL7 v2 message (ER7) starting with MSH' });
  const r = await mllp.handleMessage(er7, actorOf(req));
  const status = r.code === 'AA' ? 200 : r.code === 'AR' ? 422 : r.error === 'audit trail unavailable' ? 503 : 502;
  res.status(status).json({ ack_code: r.code, ack: r.ack, trace: r.trace || null, error: r.error || null });
});

router.post('/sync/:source/order/:orderId', sync, async (req, res, next) => {
  if (!SOURCE_RE.test(req.params.source)) return res.status(400).json({ error: 'invalid source' });
  try {
    const r = await syncService.syncOrder(req.params.source, req.params.orderId, actorOf(req));
    res.json({ success: true, source: r.source, orderId: r.orderId, entriesIn: r.entriesIn,
      terminology: r.terminology, audit: r.audit, exchange_id: r.exchange_id, result: r.result });
  } catch (e) { writeError(res, next, e); }
});
router.post('/sync/order/:numpedido', sync, async (req, res, next) => {
  try {
    const r = await syncService.syncOrder('sil', req.params.numpedido, actorOf(req));
    res.json({ success: true, source: 'sil', orderId: r.orderId, entriesIn: r.entriesIn,
      terminology: r.terminology, audit: r.audit, exchange_id: r.exchange_id, result: r.result });
  } catch (e) { writeError(res, next, e); }
});

/** Every order of a source (default SIL) through the full pipeline. */
router.post('/sync/all', sync, async (req, res, next) => {
  const source = req.query.source || 'sil';
  if (!SOURCE_RE.test(source)) return res.status(400).json({ error: 'invalid source' });
  try {
    const summary = await syncService.syncAll(source, Math.min(parseInt(req.query.limit) || 50, 1000), actorOf(req));
    res.json({ ok: summary.ok === summary.total, summary });
  } catch (e) { writeError(res, next, e); }
});

// ════════════════════════════════════════════════════════════════════════════
//  ERROR HANDLER
// ════════════════════════════════════════════════════════════════════════════
router.use((err, req, res, _next) => {
  logger.error('route_error', { path: req.path, method: req.method, error: err.message });
  const status = err.response?.status || err.statusCode || 500;
  res.status(status).json({ error: err.message, upstream: err.response?.data || null });
});

module.exports = router;
