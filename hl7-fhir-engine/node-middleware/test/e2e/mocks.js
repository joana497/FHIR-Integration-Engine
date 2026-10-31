'use strict';
// Mock HAPI FHIR (port 8080, base /fhir) and mock terminology server
// (port 8090, base /r4) for the sandbox end-to-end test.
const http = require('http');

const store = {};             // resourceType -> Set(id)
const calls = { hapiPosts: 0, validate: 0, tx: [] };
let failPosts = false;

function body(req) {
  return new Promise((res) => { let b = ''; req.on('data', (c) => (b += c)); req.on('end', () => res(b)); });
}
function send(res, status, obj) {
  res.writeHead(status, { 'Content-Type': 'application/fhir+json' });
  res.end(JSON.stringify(obj));
}

http.createServer(async (req, res) => {
  const u = new URL(req.url, 'http://x');
  const p = u.pathname.replace(/^\/fhir/, '') || '/';
  if (p === '/__fail') { failPosts = true; return send(res, 200, { failPosts }); }
  if (p === '/__ok') { failPosts = false; return send(res, 200, { failPosts }); }
  if (p === '/__calls') return send(res, 200, { ...calls, store: Object.fromEntries(Object.entries(store).map(([k, v]) => [k, v.size])) });
  if (req.method === 'GET' && p === '/metadata') return send(res, 200, { resourceType: 'CapabilityStatement', fhirVersion: '4.0.1', software: { name: 'mock-hapi' } });
  if (req.method === 'POST' && p === '/Bundle/$validate') {
    calls.validate++;
    await body(req);
    return send(res, 200, { resourceType: 'OperationOutcome', issue: [{ severity: 'information', code: 'informational', diagnostics: 'mock: no issues' }] });
  }
  if (req.method === 'POST' && p === '/') {
    const b = JSON.parse(await body(req));
    calls.hapiPosts++;
    if (failPosts) return send(res, 500, { resourceType: 'OperationOutcome', issue: [{ severity: 'fatal', code: 'exception', diagnostics: 'mock failure' }] });
    const entry = (b.entry || []).map((e) => {
      const r = e.resource; (store[r.resourceType] ||= new Set()).add(r.id);
      return { response: { status: '201 Created', location: `${r.resourceType}/${r.id}/_history/1` } };
    });
    return send(res, 200, { resourceType: 'Bundle', type: 'transaction-response', entry });
  }
  const m = p.match(/^\/(\w+)$/);
  if (req.method === 'GET' && m) return send(res, 200, { resourceType: 'Bundle', type: 'searchset', total: (store[m[1]] || new Set()).size });
  send(res, 404, { error: 'mock: not found', path: p });
}).listen(8080, () => console.log('mock HAPI on 8080'));

http.createServer((req, res) => {
  const u = new URL(req.url, 'http://x');
  calls.tx.push(Object.fromEntries(u.searchParams));
  const code = u.searchParams.get('code');
  const ok = code !== 'BAD';
  send(res, 200, { resourceType: 'Parameters', parameter: [{ name: 'result', valueBoolean: ok }, { name: 'display', valueString: `display of ${code}` }] });
}).listen(8090, () => console.log('mock tx on 8090'));
