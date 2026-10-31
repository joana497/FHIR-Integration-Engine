'use strict';

require('dotenv').config();

const path = require('path');
const express = require('express');
const cors = require('cors');
const morgan = require('morgan');
const cron = require('node-cron');
const rateLimit = require('express-rate-limit');

const logger = require('./utils/logger');
const routes = require('./routes/index');
const syncService = require('./services/syncService');
const auditRepo = require('./services/auditRepo');
const terminologyService = require('./services/terminologyService');

const app = express();
const PORT = process.env.PORT || 3000;

// ─── CORS ───────────────────────────────────────────────────────────────────
// The GUI is served from this same origin, so same-origin requests need no CORS.
// We still allow explicit dev origins (e.g. a separately-served GUI) via env.
const ALLOWED_ORIGINS = (process.env.CORS_ORIGINS || 'http://localhost:3000,http://localhost:5173')
  .split(',')
  .map((s) => s.trim());

// Same-origin requests (the dashboard served from this server, whatever host
// name is used to reach it) are always allowed; other origins must be listed.
app.use(cors((req, cb) => {
  const origin = req.header('Origin');
  const self = `${req.protocol}://${req.get('host')}`;
  const ok = !origin || origin === self || ALLOWED_ORIGINS.includes(origin);
  cb(ok ? null : new Error('Origin not allowed by CORS'), {
    origin: ok,
    methods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
    allowedHeaders: ['Content-Type', 'Authorization', 'X-Source-Institution'],
  });
}));

app.use(express.json({ limit: '50mb', type: ['application/json', 'application/fhir+json'] }));
app.use(express.text({ limit: '10mb', type: ['text/plain', 'x-application/hl7-v2+er7', 'application/hl7-v2'] }));
app.use(morgan('combined', {
  stream: { write: (msg) => logger.info(msg.trim()) },
}));

// Rate limiting (raise RATE_LIMIT_PER_MIN for load tests)
app.use(
  rateLimit({
    windowMs: 60 * 1000,
    max: parseInt(process.env.RATE_LIMIT_PER_MIN || '300', 10),
    message: { error: 'Too many requests' },
  })
);

// ─── Static GUI ───────────────────────────────────────────────────────────────
// Serves the dashboard at /app (the real client of this engine).
app.use('/app', express.static(path.join(__dirname, '..', 'public')));

// ─── API Routes ─────────────────────────────────────────────────────────────
app.use('/', routes);

// ─── Root info ────────────────────────────────────────────────────────────────
app.get('/', (req, res) => {
  res.json({
    service: 'HL7 FHIR Middleware (Node.js)',
    version: '2.0.0',
    gui: 'GET /app  (web dashboard)',
    endpoints: {
      login: 'POST /auth/login', me: 'GET /auth/me', health: 'GET /health',
      sources: 'GET /api/sources', orders: 'GET /api/sources/:source/orders',
      bundle: 'GET /api/fhir/sample-bundle/:source/:orderId',
      hapiValidation: 'GET /fhir/bundle/:source/order/:orderId/hapi-validation',
      syncOrder: 'POST /sync/:source/order/:orderId', syncAll: 'POST /sync/all?source=sil',
      ingestFhir: 'POST /api/fhir/ingest', ingestHl7v2: 'POST /api/v2/oru (and MLLP on MLLP_PORT)',
      audit: 'GET /audit, /audit/exchanges', terminology: 'GET /terminology/metrics',
    },
  });
});

// ─── Cron: optional automatic synchronisation (off unless AUTO_SYNC=true) ────
// Each order goes through the full pipeline and is audited with the actor
// 'system:cron', like any user-triggered synchronisation.
if (process.env.AUTO_SYNC === 'true') {
  cron.schedule('*/5 * * * *', async () => {
    const actor = { user_sub: 'system:cron', user_name: 'Sincronização automática', role: 'system' };
    for (const source of (process.env.AUTO_SYNC_SOURCES || 'sil').split(',')) {
      try {
        const s = await syncService.syncAll(source.trim(), 1000, actor);
        logger.info('cron_sync_complete', { source, total: s.total, ok: s.ok });
      } catch (err) {
        logger.error('cron_sync_error', { source, error: err.message });
      }
    }
  });
  logger.info('cron_scheduler_started', { schedule: 'every 5 minutes' });
}

// ─── Cron: replay the local audit outbox into Postgres (every 30 s) ──────────
setInterval(() => {
  auditRepo.replayOutbox()
    .catch((err) => logger.warn('audit_outbox_replay_failed', { error: err.message }));
}, 30_000).unref();

// ─── Cron: daily payload retention purge (90 days) ───────────────────────────
cron.schedule('17 3 * * *', async () => {
  logger.info('audit_purge_triggered');
  const n = await auditRepo.purgeOldPayloads();
  logger.info('audit_purge_done', { affected: n });
});
logger.info('audit_purge_scheduled', { schedule: 'daily 03:17' });

// ─── Cron: daily terminology revalidation (stale codes) ──────────────────────
cron.schedule('33 3 * * *', async () => {
  logger.info('terminology_revalidate_triggered');
  const r = await terminologyService.revalidateAll();
  logger.info('terminology_revalidate_done', r);
});
logger.info('terminology_revalidate_scheduled', { schedule: 'daily 03:33' });

// ─── Start ────────────────────────────────────────────────────────────────────
app.listen(PORT, () => {
  logger.info('server_started', { port: PORT, env: process.env.NODE_ENV || 'development' });
});
if (process.env.MLLP_PORT) {
  require('./services/mllpServer').start(parseInt(process.env.MLLP_PORT, 10));
}

module.exports = app;
