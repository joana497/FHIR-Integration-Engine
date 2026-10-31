'use strict';

/**
 * mllpServer.js — HL7 v2 receiver (MLLP over TCP, and the shared handler
 * used by the HTTP endpoint POST /api/v2/oru).
 *
 * For each ORU^R01 message: the Python engine maps it to a FHIR Bundle,
 * the Bundle goes through the same governed pipeline as every other
 * write (terminology, fail-closed audit, HAPI), and only then is the
 * HL7 acknowledgement returned:
 *   AA  stored and audited
 *   AR  rejected: not a usable ORU^R01 (the sender must fix the message)
 *   AE  not stored: audit trail or HAPI unavailable (the sender should retry)
 *
 * MLLP carries no user identity. The listener must only be reachable
 * from the sending laboratory's network (MLLP_ALLOWED_HOSTS restricts the
 * client addresses); the audit trail records the client address as actor.
 */

const net = require('net');
const crypto = require('crypto');
const { pythonService } = require('./fhirClient');
const syncService = require('./syncService');
const auditRepo = require('./auditRepo');
const logger = require('../utils/logger');

const SB = 0x0b;            // start block
const EB = Buffer.from([0x1c, 0x0d]); // end block + carriage return

function hl7Now() {
  return new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
}

function mshFields(er7) {
  const first = String(er7).replace(/^\x0b/, '').split(/\r\n|\r|\n/)[0] || '';
  if (!first.startsWith('MSH') || first.length < 8) return null;
  const fs = first[3];
  return { fs, f: first.split(fs) }; // f[n-1] is MSH-n for n >= 2
}

function buildAck(er7, code, text) {
  const m = mshFields(er7);
  const fs = m ? m.fs : '|';
  const get = (n) => (m && m.f[n - 1]) || '';
  const clean = (s) => String(s || '').replace(/[|^~\\&\r\n]/g, ' ').slice(0, 200);
  const segs = [
    ['MSH', '^~\\&', get(5) || 'FHIR-ENGINE', get(6) || 'EXAMPLE', get(3), get(4), hl7Now(), '',
      'ACK^R01^ACK', crypto.randomUUID().slice(0, 20), 'P', get(12) || '2.5.1'].join(fs),
    ['MSA', code, get(10), clean(text)].join(fs),
  ];
  if (code !== 'AA') {
    const err = code === 'AR' ? '102^Data type error^HL70357' : '207^Application internal error^HL70357';
    segs.push(['ERR', '', '', err, 'E', '', '', clean(text)].join(fs));
  }
  return segs.join('\r') + '\r';
}

/** Process one message; returns { code, ack, trace, error }. Never throws. */
async function handleMessage(er7, actor) {
  let transformed;
  try {
    transformed = await pythonService.transformOru(er7);
  } catch (err) {
    const status = err.response?.status;
    const detail = err.response?.data?.detail || err.message;
    const code = status === 422 ? 'AR' : 'AE';
    auditRepo.recordSecurity({ level: code === 'AR' ? 'WARN' : 'ERROR', user_sub: actor.user_sub,
      message: `HL7 v2 mensagem ${code === 'AR' ? 'rejeitada' : 'não processada'}: ${detail}` });
    return { code, ack: buildAck(er7, code, detail), error: detail };
  }
  try {
    const trace = await syncService.ingest(transformed.bundle, actor,
      { operation: 'oru_ingest', order_id: (transformed.orders || []).join(',') },
      { structural: process.env.ORU_STRUCTURAL_VALIDATION === 'true' });
    trace.control_id = transformed.control_id;
    return { code: 'AA', ack: buildAck(er7, 'AA', `stored ${trace.stages.persist.entries_total} resources`), trace };
  } catch (err) {
    const detail = err instanceof auditRepo.AuditUnavailable ? 'audit trail unavailable' : `HAPI: ${err.message}`;
    return { code: 'AE', ack: buildAck(er7, 'AE', detail), error: detail };
  }
}

function allowed(address) {
  const list = (process.env.MLLP_ALLOWED_HOSTS || '').split(',').map((s) => s.trim()).filter(Boolean);
  if (!list.length) return true;
  const a = String(address || '').replace(/^::ffff:/, '');
  return list.includes(a);
}

function start(port) {
  const server = net.createServer((socket) => {
    const remote = String(socket.remoteAddress || '').replace(/^::ffff:/, '');
    if (!allowed(remote)) {
      auditRepo.recordSecurity({ level: 'DENY', user_sub: `mllp:${remote}`, message: 'MLLP: endereço não autorizado' });
      socket.destroy();
      return;
    }
    const actor = { user_sub: `mllp:${remote}`, user_name: 'HL7 v2 MLLP listener', role: 'system' };
    let buf = Buffer.alloc(0);
    let chain = Promise.resolve(); // process messages of one connection in order
    socket.on('data', (chunk) => {
      buf = Buffer.concat([buf, chunk]);
      for (;;) {
        const s = buf.indexOf(SB);
        const e = buf.indexOf(EB, s + 1);
        if (s < 0 || e < 0) break;
        const er7 = buf.slice(s + 1, e).toString('utf8');
        buf = buf.slice(e + EB.length);
        chain = chain.then(async () => {
          const r = await handleMessage(er7, actor);
          logger.info('mllp_message', { remote, code: r.code, control_id: r.trace?.control_id, error: r.error });
          if (!socket.destroyed) socket.write(Buffer.concat([Buffer.from([SB]), Buffer.from(r.ack, 'utf8'), EB]));
        });
      }
      if (buf.length > 10 * 1024 * 1024) { // 10 MB without a complete frame: drop the connection
        logger.warn('mllp_oversized_frame', { remote });
        socket.destroy();
      }
    });
    socket.on('error', (err) => logger.warn('mllp_socket_error', { remote, error: err.message }));
  });
  server.listen(port, () => logger.info('mllp_listening', { port }));
  return server;
}

module.exports = { start, handleMessage, buildAck };
