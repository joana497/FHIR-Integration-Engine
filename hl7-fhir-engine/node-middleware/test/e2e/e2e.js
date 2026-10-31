'use strict';
// End-to-end test of the middleware against the real Python engine, a real
// PostgreSQL (initialised with postgres/01..07, i.e. with the least-privilege
// roles) and mock HAPI / terminology servers (mocks.js). See README.md here.
const net = require('net');
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const M = process.env.MIDDLEWARE_URL || 'http://127.0.0.1:3000';
const HAPI = process.env.MOCK_HAPI_URL || 'http://127.0.0.1:8080/fhir';
const ORU = path.resolve(__dirname, '../../../samples/oru');
const PGHOST = process.env.PGHOST || '127.0.0.1';
const PGPORT = process.env.PGPORT || '5432';
const PSQL = `PGPASSWORD=${process.env.PG_OWNER_PASSWORD || 'sil_pass'} psql -h ${PGHOST} -p ${PGPORT} -U ${process.env.PG_OWNER || 'sil_user'} -d sildb -qtAc`;
let pass = 0, fail = 0;
const check = (name, cond, extra) => {
  if (cond) { pass++; console.log('  PASS', name); } else { fail++; console.log('  FAIL', name, extra !== undefined ? JSON.stringify(extra).slice(0, 400) : ''); }
};
const sql = (q) => execSync(`${PSQL} "${q}"`).toString().trim();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function call(method, path, token, body, headers = {}) {
  const h = { ...headers };
  if (token) h.Authorization = `Bearer ${token}`;
  let b;
  if (body !== undefined) {
    if (typeof body === 'string') { b = body; h['Content-Type'] ||= 'text/plain'; }
    else { b = JSON.stringify(body); h['Content-Type'] = 'application/json'; }
  }
  const r = await fetch(M + path, { method, headers: h, body: b });
  let data = null; const t = await r.text(); try { data = JSON.parse(t); } catch { data = t; }
  return { status: r.status, data };
}
const hapiCalls = async () => (await fetch(`${HAPI}/__calls`)).json();
const login = async (u, p) => (await call('POST', '/auth/login', null, { username: u, password: p })).data.access_token;

function mllp(er7) {
  return new Promise((resolve, reject) => {
    const s = net.connect(2575, '127.0.0.1', () => s.write(Buffer.concat([Buffer.from([0x0b]), Buffer.from(er7), Buffer.from([0x1c, 0x0d])])));
    let buf = Buffer.alloc(0);
    s.on('data', (c) => { buf = Buffer.concat([buf, c]); const e = buf.indexOf(Buffer.from([0x1c, 0x0d])); if (e > 0) { s.end(); resolve(buf.slice(1, e).toString()); } });
    s.on('error', reject);
    setTimeout(() => reject(new Error('MLLP timeout')), 20000);
  });
}

const STARTED = new Date(Date.now() - 1000);

(async () => {
  sql('DELETE FROM audit.terminology_validation'); // cold cache, so that terminology lookups are made
  console.log('1. Authentication (bcrypt, JWT)');
  const bad = await call('POST', '/auth/login', null, { username: 'admin', password: 'wrong' });
  check('wrong password -> 401', bad.status === 401);
  const ghost = await call('POST', '/auth/login', null, { username: 'nobody', password: 'x' });
  check('unknown user -> 401 (same message)', ghost.status === 401 && ghost.data.error === bad.data.error);
  const admin = await login('admin', 'admin123');
  const medico = await login('medico', 'med123');
  const auditor = await login('auditor', 'aud123');
  check('demo accounts log in', !!admin && !!medico && !!auditor);
  const payload = JSON.parse(Buffer.from(admin.split('.')[1], 'base64url').toString());
  check('JWT iss/aud are example.org', payload.iss === 'https://auth.example.org' && payload.aud === 'https://fhir.example.org', payload);
  check('no token -> 401', (await call('GET', '/fhir/Patient')).status === 401);

  console.log('2. Health and sources');
  const health = (await call('GET', '/health')).data;
  check('audit db ready, outbox empty', health.audit_db === 'ready' && health.audit_outbox_pending === 0, health);
  check('MLLP listener up (MLLP_PORT=2575)', await new Promise((r) => { const s = net.connect(2575, '127.0.0.1', () => { s.end(); r(true); }); s.on('error', () => r(false)); }));
  check('python engine ok', health.python && health.python.ok, health.python);
  const sources = (await call('GET', '/api/sources', admin)).data;
  check('two database sources (sil, lisb) and the ORU message adapter',
    sources.database.map((s) => s.name).join() === 'sil,lisb' && sources.message[0].name === 'oru', sources);
  const lisbOrders = (await call('GET', '/api/sources/lisb/orders', admin)).data.orders;
  check('LIS-B lists its orders (8 in the seed)', lisbOrders.length >= 8, lisbOrders.length);

  console.log('3. Synchronisation (fail-closed audit, terminology)');
  const s1 = await call('POST', '/sync/sil/order/PED-2024-00002', admin);
  check('sync SIL order -> 200, audit in db', s1.status === 200 && s1.data.audit === 'db', s1.data);
  check('terminology: standard codes checked, local codes counted', s1.data.terminology && s1.data.terminology.validated > 0 && s1.data.terminology.local > 0, s1.data.terminology);
  const tx = (await hapiCalls()).tx;
  check('no display parameter sent to the terminology server', tx.length > 0 && tx.every((q) => !('display' in q)), tx[0]);
  const s2 = await call('POST', `/sync/lisb/order/${encodeURIComponent(lisbOrders[0])}`, admin);
  check('sync LIS-B order -> 200', s2.status === 200 && s2.data.entriesIn > 0, s2.data);
  const alias = await call('POST', '/sync/order/PED-2024-00003', admin);
  check('legacy alias /sync/order/:id still works (SIL)', alias.status === 200, alias.data);
  const all = await call('POST', '/sync/all?source=sil&limit=25', admin);
  check('sync all SIL (first 25) -> 25/25', all.status === 200 && all.data.summary.total === 25 && all.data.summary.ok === 25, all.data.summary && { total: all.data.summary.total, ok: all.data.summary.ok });
  const allB = await call('POST', '/sync/all?source=lisb&limit=8', admin);
  check('sync all LIS-B (first 8) -> 8/8', allB.status === 200 && allB.data.summary.ok === 8, allB.data.summary && allB.data.summary.ok);
  const denied = await call('POST', '/sync/sil/order/PED-2024-00001', medico);
  check('medico cannot sync -> 403', denied.status === 403);

  console.log('4. HL7 v2 ORU^R01 (HTTP and MLLP)');
  const er7a = fs.readFileSync(`${ORU}/oru_r01_03.hl7`, 'utf8');
  const o1 = await call('POST', '/api/v2/oru', admin, er7a, { 'Content-Type': 'x-application/hl7-v2+er7' });
  check('ORU over HTTP -> AA', o1.status === 200 && o1.data.ack_code === 'AA', o1.data);
  check('ACK echoes the control id in MSA-2', /\rMSA\|AA\|LISC00000003\|/.test(o1.data.ack || ''), o1.data.ack);
  const broken = er7a.replace(/OBX\|1\|CWE\|/, 'OBX|1|ZZ|');
  const o2 = await call('POST', '/api/v2/oru', admin, broken, { 'Content-Type': 'x-application/hl7-v2+er7' });
  check('unsupported OBX value type -> 422 AR with ERR segment', o2.status === 422 && o2.data.ack_code === 'AR' && /\rERR\|/.test(o2.data.ack), o2.data);
  check('ORU over HTTP requires write permission (medico -> 403)', (await call('POST', '/api/v2/oru', medico, er7a, { 'Content-Type': 'x-application/hl7-v2+er7' })).status === 403);
  const ack = await mllp(fs.readFileSync(`${ORU}/oru_r01_02.hl7`, 'utf8'));
  check('ORU over MLLP -> framed ACK AA', /^MSH\|/.test(ack) && /\rMSA\|AA\|LISC00000002\|/.test(ack), ack);

  console.log('5. External FHIR ingest');
  const bundle = (await call('GET', '/api/fhir/sample-bundle/sil/PED-2024-00007', admin)).data;
  const ing = await call('POST', '/api/fhir/ingest', admin, bundle, { 'X-Source-Institution': 'Hospital Exemplo' });
  check('ingest -> 200 with structural, terminology, persist stages', ing.status === 200 && ing.data.trace.stages.structural && ing.data.trace.stages.persist.ok, ing.data.trace && ing.data.trace.stages && Object.keys(ing.data.trace.stages));
  const hv = await call('GET', '/fhir/bundle/lisb/order/' + encodeURIComponent(lisbOrders[1]) + '/hapi-validation', admin);
  check('HAPI $validate route per source', hv.status === 200 && hv.data.ok === true, hv.data);

  console.log('6. Audit trail (intent + outcome)');
  const ex = (await call('GET', '/audit/exchanges?limit=500', auditor)).data;
  const expected = 1 + 1 + 1 + 25 + 8 + 1 + 1 + 1; // syncs, alias, all, all lisb, oru http, mllp, ingest
  const fresh = ex.entries.filter((e) => new Date(e.occurred_at) >= STARTED);
  check(`exchanges recorded by this run = ${expected}, none pending`, fresh.length === expected && fresh.every((e) => e.status === 'ok'), { fresh: fresh.length, statuses: [...new Set(fresh.map((e) => e.status))] });
  const one = (await call('GET', `/audit/exchanges/${ex.entries[0].exchange_id}`, auditor)).data;
  check('exchange detail = intent row (payload) + outcome row (response)', one.rows.length === 2 && one.rows[0].phase === 'intent' && one.rows[0].payload_json && one.rows[1].phase === 'outcome', one.rows && one.rows.map((r) => r.phase));
  check('mllp actor recorded', ex.entries.some((e) => e.operation === 'oru_ingest' && /^mllp:/.test(e.user_sub)));
  const secTotal = Number(sql('SELECT count(*) FROM audit.security_event'));
  check('security events persisted (logins, denials, accesses)', secTotal > 20, secTotal);
  check('denials persisted', Number(sql("SELECT count(*) FROM audit.security_event WHERE level='DENY'")) >= 4);
  const csv = await call('GET', '/audit/exchanges.csv', auditor);
  check('CSV export', csv.status === 200 && typeof csv.data === 'string' && csv.data.startsWith('exchange_id,'));
  check('medico cannot read audit -> 403', (await call('GET', '/audit', medico)).status === 403);

  console.log('7. Failure modes');
  // 7a. HAPI fails: outcome 'error' recorded, error returned to caller
  await fetch(`${HAPI}/__fail`);
  const hf = await call('POST', '/sync/sil/order/PED-2024-00004', admin);
  await fetch(`${HAPI}/__ok`);
  const lastOut = sql("SELECT status FROM audit.exchange_log WHERE phase='outcome' ORDER BY occurred_at DESC LIMIT 1");
  check('HAPI failure -> error to caller and outcome=error in audit', hf.status >= 500 && lastOut === 'error', { status: hf.status, lastOut });

  // 7b. Fail-closed: audit intent cannot be written -> nothing sent to HAPI
  const before = (await hapiCalls()).hapiPosts;
  sql('REVOKE INSERT ON audit.exchange_log FROM audit_writer');
  const fc = await call('POST', '/sync/sil/order/PED-2024-00005', admin);
  const fcOru = await call('POST', '/api/v2/oru', admin, er7a, { 'Content-Type': 'x-application/hl7-v2+er7' });
  const ackFc = await mllp(fs.readFileSync(`${ORU}/oru_r01_04.hl7`, 'utf8'));
  const after = (await hapiCalls()).hapiPosts;
  sql('GRANT INSERT ON audit.exchange_log TO audit_writer');
  check('sync refused with 503 AUDIT_UNAVAILABLE', fc.status === 503 && fc.data.code === 'AUDIT_UNAVAILABLE', fc.data);
  check('ORU over HTTP refused (503, ACK AE)', fcOru.status === 503 && fcOru.data.ack_code === 'AE', fcOru.data);
  check('ORU over MLLP -> ACK AE (sender retries)', /\rMSA\|AE\|/.test(ackFc), ackFc);
  check('no Bundle reached HAPI while the audit was unavailable', after === before, { before, after });
  check('the refusal itself is recorded as a security event',
    Number(sql("SELECT count(*) FROM audit.security_event WHERE level='ERROR' AND message LIKE '%fail-closed%'")) >= 1);

  // 7c. Security events fall back to the outbox, then replay
  sql('REVOKE INSERT ON audit.security_event FROM audit_writer');
  const r1 = await call('GET', '/fhir/Patient?limit=5', medico);
  const h1 = (await call('GET', '/health')).data;
  sql('GRANT INSERT ON audit.security_event TO audit_writer');
  check('read served while DB is down (access logged to outbox)', r1.status === 200, r1.status);
  check('outbox holds the pending records', h1.audit_outbox_pending >= 1, h1.audit_outbox_pending);
  const nBefore = Number(sql('SELECT count(*) FROM audit.security_event'));
  console.log('   (waiting 32 s for the outbox replay job)');
  await sleep(32000);
  const h2 = (await call('GET', '/health')).data;
  const nAfter = Number(sql('SELECT count(*) FROM audit.security_event'));
  check('outbox replayed into Postgres', h2.audit_outbox_pending === 0 && nAfter > nBefore, { pending: h2.audit_outbox_pending, nBefore, nAfter });

  // 7d. Least privilege of the middleware's audit role
  let denyUpd = '';
  try { execSync(`PGPASSWORD=audit_writer_pass psql -h ${PGHOST} -p ${PGPORT} -U audit_writer -d sildb -qtAc "UPDATE audit.exchange_log SET status='ok'"`, { stdio: 'pipe' }); } catch (e) { denyUpd = e.stderr.toString(); }
  check('audit_writer cannot UPDATE the audit trail', /permission denied/.test(denyUpd), denyUpd);
  let denyRead = '';
  try { execSync(`PGPASSWORD=audit_writer_pass psql -h ${PGHOST} -p ${PGPORT} -U audit_writer -d sildb -qtAc "SELECT count(*) FROM sil.dpedidos"`, { stdio: 'pipe' }); } catch (e) { denyRead = e.stderr.toString(); }
  check('audit_writer cannot read clinical tables', /permission denied/.test(denyRead), denyRead);

  const c = await hapiCalls();
  console.log('\nmock HAPI store:', JSON.stringify(c.store), '| posts:', c.hapiPosts, '| $validate:', c.validate, '| tx lookups:', c.tx.length);
  console.log(`\nRESULT: ${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error('E2E crashed', e); process.exit(2); });
