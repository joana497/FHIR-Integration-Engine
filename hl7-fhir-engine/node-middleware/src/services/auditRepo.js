'use strict';

/**
 * auditRepo.js — persistent audit trail (Postgres schema `audit`).
 *
 * Two policies, chosen per kind of record (see docs/audit-policy):
 *
 *  * Exchanges that send health data to HAPI are FAIL-CLOSED. An
 *    'intent' row must be committed before any data leaves the engine;
 *    if it cannot be written, the operation is refused (AuditUnavailable).
 *    The 'outcome' row is written afterwards; if the database fails at
 *    that point the outcome goes to the local outbox, because the data
 *    has already been sent and cannot be recalled.
 *
 *  * Security and access events (logins, denials, authorised requests)
 *    fall back to the local outbox when the database is down. If both
 *    the database and the outbox fail, recordSecurity() returns false and
 *    callers that serve health data refuse the request.
 *
 * The middleware connects as audit_writer (INSERT/SELECT only), so the
 * tables are append-only from the application's point of view.
 */

const crypto = require('crypto');
const { Pool } = require('pg');
const logger = require('../utils/logger');
const outbox = require('./outbox');

const DATABASE_URL =
  process.env.AUDIT_DATABASE_URL ||
  'postgresql://audit_writer:audit_writer_pass@postgres:5432/sildb';

const pool = new Pool({
  connectionString: DATABASE_URL,
  max: parseInt(process.env.AUDIT_POOL_MAX || '10', 10),
  idleTimeoutMillis: 30_000,
  connectionTimeoutMillis: 5_000,
});
pool.on('error', (err) => logger.error('audit_pool_error', { error: err.message }));

class AuditUnavailable extends Error {
  constructor(cause) {
    super(`audit trail unavailable — operation refused (fail-closed): ${cause}`);
    this.name = 'AuditUnavailable';
    this.statusCode = 503;
  }
}

// ─── Bootstrap ───────────────────────────────────────────────────────────────
let ready = false;
async function bootstrap() {
  for (let attempt = 1; attempt <= 30; attempt++) {
    try {
      const r = await pool.query("SELECT 1 FROM information_schema.schemata WHERE schema_name='audit'");
      if (r.rowCount === 0) throw new Error('audit schema not present yet');
      ready = true;
      logger.info('audit_db_ready', { attempt });
      return;
    } catch (err) {
      logger.warn('audit_db_not_ready', { attempt, error: err.message });
      await new Promise((r) => setTimeout(r, 2_000));
    }
  }
  logger.error('audit_db_bootstrap_failed');
}
if (process.env.NODE_ENV !== 'test') bootstrap();

// ─── Inserts (idempotent: ids are generated here) ───────────────────────────
const EXCHANGE_COLS = ['exchange_id', 'occurred_at', 'user_sub', 'user_name', 'role', 'phase', 'correlation_id',
  'operation', 'order_id', 'bundle_id', 'resource_count', 'status', 'error_message', 'payload_json', 'response_json'];
const SECURITY_COLS = ['event_id', 'occurred_at', 'level', 'user_sub', 'message', 'ip_address', 'method', 'path',
  'status_code'];

function insertSql(table, cols, pk) {
  const ph = cols.map((_, i) => `$${i + 1}`).join(',');
  return `INSERT INTO audit.${table} (${cols.join(',')}) VALUES (${ph}) ON CONFLICT (${pk}) DO NOTHING`;
}
const SQL = {
  exchange_log: insertSql('exchange_log', EXCHANGE_COLS, 'exchange_id'),
  security_event: insertSql('security_event', SECURITY_COLS, 'event_id'),
};

async function insertRow(table, row) {
  const cols = table === 'exchange_log' ? EXCHANGE_COLS : SECURITY_COLS;
  const v = cols.map((c) => {
    const x = row[c];
    if (x === undefined) return null;
    if ((c === 'payload_json' || c === 'response_json') && x !== null) return JSON.stringify(x);
    return x;
  });
  await pool.query(SQL[table], v);
}

function exchangeRow(base, extra) {
  return {
    exchange_id: crypto.randomUUID(),
    occurred_at: new Date().toISOString(),
    user_sub: base.user_sub || null,
    user_name: base.user_name || null,
    role: base.role || null,
    operation: base.operation,
    order_id: base.order_id || null,
    bundle_id: base.bundle_id || null,
    resource_count: base.resource_count != null ? base.resource_count : null,
    ...extra,
  };
}

/**
 * Fail-closed step 1: record the intent to send `payload` to HAPI.
 * Throws AuditUnavailable if the row cannot be committed.
 */
async function beginExchange(base, payload) {
  const row = exchangeRow(base, { phase: 'intent', status: 'pending', payload_json: payload || null });
  try {
    await insertRow('exchange_log', row);
    return row;
  } catch (err) {
    logger.error('audit_intent_failed', { operation: base.operation, error: err.message });
    recordSecurity({ level: 'ERROR', user_sub: base.user_sub,
      message: `Operação recusada (fail-closed): ${base.operation} ${base.order_id || ''} — ${err.message}` });
    throw new AuditUnavailable(err.message);
  }
}

/**
 * Fail-closed step 2: record the outcome. Never throws: the data has
 * already left, so a database failure here is absorbed by the outbox.
 * Returns where the record went: 'db' | 'outbox' | 'lost'.
 */
async function completeExchange(intent, outcome) {
  const row = exchangeRow(intent, {
    phase: 'outcome',
    correlation_id: intent.exchange_id,
    status: outcome.status,
    error_message: outcome.error_message || null,
    response_json: outcome.response_json || null,
    bundle_id: outcome.bundle_id || intent.bundle_id || null,
    resource_count: outcome.resource_count != null ? outcome.resource_count : intent.resource_count,
  });
  try {
    await insertRow('exchange_log', row);
    return 'db';
  } catch (err) {
    logger.error('audit_outcome_db_failed', { error: err.message });
    return outbox.append('exchange_log', row) ? 'outbox' : 'lost';
  }
}

/** Security/access event. Returns 'db' | 'outbox' | false (nothing durable). */
async function recordSecurity(entry) {
  const row = {
    event_id: crypto.randomUUID(),
    occurred_at: new Date().toISOString(),
    level: entry.level,
    user_sub: entry.user_sub || null,
    message: entry.message,
    ip_address: entry.ip_address || null,
    method: entry.method || null,
    path: entry.path || null,
    status_code: entry.status_code != null ? entry.status_code : null,
  };
  try {
    await insertRow('security_event', row);
    return 'db';
  } catch (err) {
    return outbox.append('security_event', row) ? 'outbox' : false;
  }
}

/** Move outbox records into Postgres (called by a cron job). */
async function replayOutbox() {
  return outbox.replay(insertRow);
}

// ─── Reads ───────────────────────────────────────────────────────────────────
async function listExchanges({ limit = 100, offset = 0, userSub, orderId, status } = {}) {
  const where = [];
  const params = [];
  if (userSub) { params.push(userSub); where.push(`user_sub = $${params.length}`); }
  if (orderId) { params.push(orderId); where.push(`order_id = $${params.length}`); }
  if (status)  { params.push(status);  where.push(`status = $${params.length}`); }
  const w = where.length ? `WHERE ${where.join(' AND ')}` : '';
  const cnt = await pool.query(`SELECT COUNT(*)::int AS c FROM audit.exchange_summary ${w}`, params);
  params.push(limit, offset);
  const r = await pool.query(
    `SELECT * FROM audit.exchange_summary ${w}
     ORDER BY occurred_at DESC LIMIT $${params.length - 1} OFFSET $${params.length}`, params);
  return { total: cnt.rows[0].c, entries: r.rows };
}

async function getExchange(id) {
  const r = await pool.query(
    `SELECT * FROM audit.exchange_log WHERE exchange_id = $1 OR correlation_id = $1 ORDER BY occurred_at`, [id]);
  return r.rows.length ? { exchange_id: id, rows: r.rows } : null;
}

async function listSecurity({ limit = 200, offset = 0, level, userSub } = {}) {
  const where = [];
  const params = [];
  if (level)   { params.push(level);   where.push(`level = $${params.length}`); }
  if (userSub) { params.push(userSub); where.push(`user_sub = $${params.length}`); }
  const w = where.length ? `WHERE ${where.join(' AND ')}` : '';
  const cnt = await pool.query(`SELECT COUNT(*)::int AS c FROM audit.security_event ${w}`, params);
  params.push(limit, offset);
  const r = await pool.query(
    `SELECT event_id, occurred_at, level, user_sub, message, ip_address, method, path, status_code
     FROM audit.security_event ${w}
     ORDER BY occurred_at DESC LIMIT $${params.length - 1} OFFSET $${params.length}`, params);
  return { total: cnt.rows[0].c, entries: r.rows };
}

// ─── Retention ───────────────────────────────────────────────────────────────
async function purgeOldPayloads() {
  try {
    const r = await pool.query('SELECT audit.purge_old_payloads() AS affected');
    return r.rows[0].affected;
  } catch (err) {
    logger.error('audit_purge_failed', { error: err.message });
    return -1;
  }
}

module.exports = {
  AuditUnavailable,
  beginExchange,
  completeExchange,
  recordSecurity,
  replayOutbox,
  listExchanges,
  getExchange,
  listSecurity,
  purgeOldPayloads,
  outboxPending: outbox.pending,
  isReady: () => ready,
  _pool: pool,
};
