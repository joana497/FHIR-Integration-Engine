'use strict';

/**
 * outbox.js — local, durable fallback for audit records.
 *
 * When the audit database cannot be reached, records are appended to a
 * JSON-lines file on a Docker volume and flushed to disk (fsync) before
 * the caller continues. A replay job (index.js, every 30 s) moves them
 * into Postgres. Each record carries its own primary key, so a record
 * that is replayed twice is inserted once (ON CONFLICT DO NOTHING).
 *
 * The outbox protects against a database outage, not against the loss
 * of the host's disk: the file lives on the middleware's volume.
 */

const fs = require('fs');
const path = require('path');
const logger = require('../utils/logger');

const OUTBOX_PATH = process.env.AUDIT_OUTBOX_PATH || '/var/lib/fhir-middleware/audit-outbox.jsonl';

function ensureDir() {
  fs.mkdirSync(path.dirname(OUTBOX_PATH), { recursive: true });
}

/** Append one record durably. Returns true on success, false otherwise. */
function append(table, row) {
  try {
    ensureDir();
    const fd = fs.openSync(OUTBOX_PATH, 'a');
    try {
      fs.writeSync(fd, JSON.stringify({ table, row, queued_at: new Date().toISOString() }) + '\n');
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    logger.warn('audit_outbox_append', { table, id: row.event_id || row.exchange_id });
    return true;
  } catch (err) {
    logger.error('audit_outbox_append_failed', { error: err.message });
    return false;
  }
}

/** Number of records waiting to be replayed. */
function pending() {
  try {
    return fs.readFileSync(OUTBOX_PATH, 'utf8').split('\n').filter(Boolean).length;
  } catch {
    return 0;
  }
}

/**
 * Replay queued records with insertFn(table, row) (which must be
 * idempotent). The file is first renamed, so records appended during
 * the replay go to a fresh file; anything that fails is appended back.
 */
async function replay(insertFn) {
  const working = `${OUTBOX_PATH}.replaying`;
  try {
    if (!fs.existsSync(working)) {
      if (!fs.existsSync(OUTBOX_PATH)) return { replayed: 0, failed: 0 };
      fs.renameSync(OUTBOX_PATH, working);
    }
  } catch (err) {
    logger.error('audit_outbox_rename_failed', { error: err.message });
    return { replayed: 0, failed: 0, error: err.message };
  }
  const lines = fs.readFileSync(working, 'utf8').split('\n').filter(Boolean);
  let replayed = 0;
  const failed = [];
  for (const line of lines) {
    let rec;
    try {
      rec = JSON.parse(line);
    } catch {
      logger.error('audit_outbox_corrupt_line', { line: line.slice(0, 120) });
      continue;
    }
    try {
      await insertFn(rec.table, rec.row);
      replayed++;
    } catch (err) {
      failed.push(rec);
    }
  }
  for (const rec of failed) append(rec.table, rec.row);
  fs.unlinkSync(working);
  if (replayed || failed.length) logger.info('audit_outbox_replay', { replayed, failed: failed.length });
  return { replayed, failed: failed.length };
}

module.exports = { append, replay, pending, OUTBOX_PATH };
