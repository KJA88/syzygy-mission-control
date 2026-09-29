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

async function render(snap, fail = false, extra = null) {
  const elements = {}, requests = [];
  const context = {Date, Intl, Map, Number, String, Object, Array, Promise,
    document: {getElementById(id) {
      return elements[id] ||= {textContent: '', innerHTML: '', classList: {add() {}, remove() {}}};
    }},
    fetch: async url => {
      requests.push(url);
      if (fail) throw new Error('offline');
      if (extra && Object.prototype.hasOwnProperty.call(extra, url)) {
        return {ok: true, json: async () => extra[url]};
      }
      return {ok: true, json: async () => url === '/api/snapshot' ? snap : {events: []}};
    }, setInterval() {},
  };
  vm.runInNewContext(code, context);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(requests.slice(0, 2), ['/api/snapshot', '/api/events?limit=20']);
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
  assert.match(el.roarm.innerHTML, /status-chip UNKNOWN/);
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

test('missing RoArm observation renders UNKNOWN without crashing', async () => {
  const el = await render(snapshot());
  assert.match(el.roarm.innerHTML, /UNKNOWN/);
  assert.match(el.roarm.innerHTML, /No RoArm observation/);
});

test('RoArm observation renders state from the Guardian snapshot', async () => {
  const snap = snapshot();
  snap.roarm = {
    status: 'green', class: null, connected: true, fresh: true,
    transport: 'http', endpoint: 'http://192.168.4.1',
    observed_at: new Date().toISOString(),
    pose: {x: 1, y: 2, z: 3, tilt: 4},
    joints: {base: 5, shoulder: 6, elbow: 7, wrist: 8, roll: 9, gripper: 10},
  };
  const el = await render(snap);
  assert.match(el.roarm.innerHTML, /status-chip GREEN/);
  assert.match(el.roarm.innerHTML, /http:\/\/192\.168\.4\.1/);
  assert.match(el.roarm.innerHTML, /Gripper: <strong>10<\/strong>/);
});

test('stale Guardian heartbeat overrides previously green RoArm state', async () => {
  const snap = snapshot();
  snap.guardian.heartbeat_at = new Date(Date.now() - 180000).toISOString();
  snap.roarm = {
    status: 'green', class: null, connected: true, fresh: true,
    transport: 'http', endpoint: 'http://192.168.4.1',
    observed_at: new Date(Date.now() - 180000).toISOString(),
  };
  const el = await render(snap);
  assert.match(el.roarm.innerHTML, /status-chip UNKNOWN/);
  assert.match(el.roarm.innerHTML, /SNAPSHOT_STALE · T105 stale/);
  assert.doesNotMatch(el.roarm.innerHTML, /status-chip GREEN/);
});

test('training view shows synthetic workout fields from the network', async () => {
  const recentUrl = '/api/workouts/recent?limit=8';
  const summaryUrl = '/api/workouts/summary?days=7';
  const workout = {
    source: 'fitbit', name: 'Sample Walk', exercise_type: 'WALKING',
    start: '2020-01-02T15:00:00-08:00', end: '2020-01-02T15:30:00-08:00',
    duration_minutes: 30, calories: 100, steps: 3000, distance_miles: 1.5,
    average_heart_rate_bpm: 110, active_zone_minutes: 12,
    heart_rate_zones: {light_minutes: 10, moderate_minutes: 8, vigorous_minutes: 2, peak_minutes: 0},
    average_pace_minutes_per_mile: 13.41, has_gps: false,
    device: 'Sample Tracker', platform: 'FITBIT', recording_method: 'AUTOMATIC',
  };
  const el = await render(snapshot(), false, {
    [recentUrl]: {available: true, source: 'fitbit', workouts: [workout]},
    [summaryUrl]: {
      available: true, source: 'fitbit', workout_count: 1,
      start_date: '2020-01-02', end_date: '2020-01-08',
      totals: {
        duration_minutes: 30, calories: 100, distance_miles: 1.5, steps: 3000,
        active_zone_minutes: 12,
        heart_rate_zones: workout.heart_rate_zones,
      },
      daily_active_zone_minutes: {
        available: true,
        totals: {active_zone_minutes: 20, fat_burn_zone_minutes: 10, cardio_zone_minutes: 8, peak_zone_minutes: 2},
      },
      daily_time_in_heart_rate_zone: {available: true, duration_seconds: {FAT_BURN: 120}},
    },
  });
  assert.equal(el['training-status'].textContent, 'Fitbit read-only · 1 recent');
  assert.match(el['training-latest'].innerHTML, /Sample Walk/);
  assert.match(el['training-latest'].innerHTML, /3000/);
  assert.match(el['training-summary'].innerHTML, /fat burn 10/);
  assert.match(el['training-summary'].innerHTML, /FAT_BURN 120 s/);
  assert.doesNotMatch(el['training-latest'].innerHTML, /max_heart_rate|additional_metrics|Authorization/);
});

test('health view shows synthetic Fitbit metrics and labels HRV as Fitbit', async () => {
  const el = await render(snapshot(), false, {
    '/api/health/today': {
      available: true, source: 'fitbit', date: '2020-01-08', generated_at: '2020-01-08T12:00:00-08:00',
      metrics: [{
        source: 'fitbit', name: 'average_hrv_ms', value: 42, unit: 'ms', date: '2020-01-08',
        tool: 'get_fitbit_hrv', freshness: 'present',
      }],
      recovery: {
        source: 'fitbit', series: 'fitbit_nightly_hrv',
        note: 'Fitbit HRV is the Fitbit nightly series, not Polar H10 morning HRV.',
        metrics: [{
          source: 'fitbit', name: 'average_hrv_ms', value: 42, unit: 'ms', date: '2020-01-08',
          tool: 'get_fitbit_hrv', freshness: 'present',
        }],
      },
      watchlist: [{source: 'fitbit', name: 'weight_pounds', reason: 'MISSING', date: null, tool: 'get_fitbit_weight'}],
      freshness: {source: 'fitbit', tools: {get_fitbit_hrv: 'ok', get_fitbit_weight: 'unavailable'}},
    },
    '/api/health/summary?days=7': {
      available: true, source: 'fitbit', series: {average_hrv_ms: {total: null, sample_count: 3}},
    },
    '/api/health/trends?days=30': {
      available: true, source: 'fitbit',
      series: {average_hrv_ms: {source: 'fitbit', unit: 'ms', sample_count: 10, latest: {value: 42}, total: null}},
    },
  });
  assert.equal(el['health-status'].textContent, 'Fitbit read-only · 2020-01-08');
  assert.match(el['health-recovery'].innerHTML, /Fitbit nightly series/);
  assert.match(el['health-today'].innerHTML, /average_hrv_ms/);
  assert.match(el['health-today'].innerHTML, /42 ms/);
  assert.match(el['health-watchlist'].innerHTML, /weight_pounds · MISSING/);
  assert.match(el['health-trends'].innerHTML, /10 days in 30/);
  assert.doesNotMatch(el['health-today'].innerHTML, /Polar H10/);
  assert.doesNotMatch(el['health-today'].innerHTML, /Authorization/);
});
