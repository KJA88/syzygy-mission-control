// Run with: node --test tests/test_ui_access.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const code = fs.readFileSync(path.join(__dirname, '../ui/app.js'), 'utf8');
const ids = ['dhras-mcp', 'fitbit-mcp', 'tv-mcp', 'pi-git-mcp', 'polar-h10-mcp', 'jetson-git-mcp'];

function snapshot() {
  return {guardian: {status: 'green', heartbeat_at: new Date().toISOString()},
    system: {status: 'green'}, services: ids.map(id => ({id, status: 'green'})),
    auth: ids.map(id => ({id, status: 'green', required: false, class: null, duration_ms: 1250}))};
}

async function render(snap, fail = false) {
  const elements = {}, requests = [];
  const context = {Date, Intl, Map, Number, String, Object, Array, Promise,
    document: {getElementById(id) {
      return elements[id] ||= {textContent: '', innerHTML: '', classList: {add() {}, remove() {}}};
    }},
    fetch: async url => {
      requests.push(url);
      if (fail) throw new Error('offline');
      return {ok: true, json: async () => url === '/api/snapshot' ? snap : {events: []}};
    }, setInterval() {},
  };
  vm.runInNewContext(code, context);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(requests, ['/api/snapshot', '/api/events?limit=20']);
  return elements;
}

test('all six services show health and verified access without false probe-off labels', async () => {
  const el = await render(snapshot());
  assert.equal(el['auth-summary'].textContent, '6 of 6 healthy and verified');
  assert.equal((el.auth.innerHTML.match(/Authenticated access/g) || []).length, 6);
  assert.equal((el.auth.innerHTML.match(/>Verified</g) || []).length, 6);
  assert.match(el.auth.innerHTML, /Does not block system health/);
  assert.doesNotMatch(el.auth.innerHTML, /AUTH_PROBE_OFF/);
});

test('auth denial stays separate from healthy service and explains the failure', async () => {
  const snap = snapshot();
  snap.auth[0] = {...snap.auth[0], status: 'red', class: 'AUTH_DENIED'};
  const el = await render(snap);
  assert.equal(el['auth-summary'].textContent, '5 of 6 healthy and verified');
  assert.match(el.auth.innerHTML, /access gateway rejected Guardian/);
  assert.equal((el.auth.innerHTML.match(/>Healthy</g) || []).length, 6);
  assert.match(el.auth.innerHTML, />Failed</);
});

test('missing auth is unverified and service failure has a plain reason', async () => {
  const snap = snapshot();
  snap.auth = [];
  snap.services[0] = {...snap.services[0], status: 'red', class: 'SVC_INACTIVE'};
  const el = await render(snap);
  assert.match(el.auth.innerHTML, /service is not running/);
  assert.equal((el.auth.innerHTML.match(/>Unverified</g) || []).length, 6);
  assert.equal(el['auth-summary'].textContent, '0 of 6 healthy and verified');
});

test('stale heartbeat overrides previously green results', async () => {
  const snap = snapshot();
  snap.guardian.heartbeat_at = new Date(Date.now() - 180000).toISOString();
  const el = await render(snap);
  assert.match(el['auth-summary'].textContent, /Waiting/);
  assert.doesNotMatch(el.auth.innerHTML, /status-chip GREEN/);
  assert.match(el.auth.innerHTML, /out of date/);
});

test('failed dashboard fetch does not leave the MCP overview green', async () => {
  const el = await render(snapshot(), true);
  assert.match(el['auth-summary'].textContent, /Connection lost/);
  assert.doesNotMatch(el.auth.innerHTML, /status-chip GREEN/);
});

test('untrusted reason and server metadata are escaped in details', async () => {
  const snap = snapshot();
  snap.auth[0] = {...snap.auth[0], status: 'red', class: '<script>bad()</script>',
    server_info: {name: '<img src=x onerror=bad()>'}};
  const el = await render(snap);
  assert.doesNotMatch(el.auth.innerHTML, /<script>|<img/);
  assert.match(el.auth.innerHTML, /&lt;script&gt;/);
  assert.match(el.auth.innerHTML, /&lt;img/);
});
