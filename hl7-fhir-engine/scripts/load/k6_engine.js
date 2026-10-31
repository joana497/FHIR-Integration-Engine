// k6_engine.js - load scenarios for the FHIR engine (run by scripts/run_load_test.py).
//
// SCENARIO
//   engine  GET  python-fhir-engine /fhir/bundle/:source/order/:id   (transformation only, no auth)
//   bundle  GET  middleware /api/fhir/sample-bundle/:source/:id      (+ JWT, RBAC, access log)
//   sync    POST middleware /sync/:source/order/:id                  (+ terminology, audit, HAPI write)
//   oru     POST middleware /api/v2/oru                              (HL7 v2 -> FHIR -> audit -> HAPI)
// Each virtual user repeats its request as fast as the server answers
// (closed model, constant-vus); orders are drawn at random.
import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';
import { SharedArray } from 'k6/data';

const BASE = __ENV.BASE_URL || 'http://node-middleware:3000';
const ENGINE = __ENV.ENGINE_URL || 'http://python-fhir-engine:8000';
const SCENARIO = __ENV.SCENARIO || 'bundle';
const SOURCE = __ENV.SOURCE || 'sil';
const ORU = new SharedArray('oru', () =>
  (__ENV.ORU_FILES || '').split(',').filter(Boolean).map((f) => open(f).trim().replace(/\r\n|\n|\r/g, '\r') + '\r'));
const rateLimited = new Counter('rate_limited_429');
const auditRefused = new Counter('audit_refused_503');

export const options = {
  scenarios: {
    main: { executor: 'constant-vus', vus: Number(__ENV.VUS || 1), duration: __ENV.DURATION || '60s' },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  // Always-true thresholds: they make k6 report sub-metrics restricted to the
  // measured scenario, i.e. without the two requests made in setup().
  thresholds: {
    'http_req_duration{scenario:main}': ['max>=0'],
    'http_reqs{scenario:main}': ['count>=0'],
    'http_req_failed{scenario:main}': ['rate>=0'],
  },
  setupTimeout: '300s',
};

export function setup() {
  const login = http.post(`${BASE}/auth/login`, JSON.stringify({ username: 'admin', password: 'admin123' }),
    { headers: { 'Content-Type': 'application/json' } });
  const token = login.json('access_token');
  const list = http.get(`${BASE}/api/sources/${SOURCE}/orders?limit=5000`, { headers: { Authorization: `Bearer ${token}` } });
  const orders = list.json('orders');
  if (!token || !orders || !orders.length) throw new Error(`setup failed: login ${login.status}, orders ${list.status}`);
  return { token, orders };
}

export default function (d) {
  const auth = { Authorization: `Bearer ${d.token}` };
  const id = encodeURIComponent(d.orders[Math.floor(Math.random() * d.orders.length)]);
  let r;
  if (SCENARIO === 'engine') {
    r = http.get(`${ENGINE}/fhir/bundle/${SOURCE}/order/${id}`, { tags: { name: 'engine_bundle' } });
  } else if (SCENARIO === 'bundle') {
    r = http.get(`${BASE}/api/fhir/sample-bundle/${SOURCE}/${id}`, { headers: auth, tags: { name: 'mw_bundle' } });
  } else if (SCENARIO === 'sync') {
    r = http.post(`${BASE}/sync/${SOURCE}/order/${id}`, null, { headers: auth, tags: { name: 'mw_sync' }, timeout: '120s' });
  } else if (SCENARIO === 'oru') {
    const msg = ORU[Math.floor(Math.random() * ORU.length)];
    r = http.post(`${BASE}/api/v2/oru`, msg, {
      headers: { ...auth, 'Content-Type': 'x-application/hl7-v2+er7' }, tags: { name: 'mw_oru' }, timeout: '120s' });
  } else {
    throw new Error(`unknown SCENARIO ${SCENARIO}`);
  }
  if (r.status === 429) rateLimited.add(1);
  if (r.status === 503) auditRefused.add(1);
  check(r, { 'status 200': (x) => x.status === 200 });
}

export function handleSummary(data) {
  return { [__ENV.SUMMARY_PATH || '/out/summary.json']: JSON.stringify(data, null, 1) };
}
