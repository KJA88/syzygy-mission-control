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

test('home page lists switches before useful sensors and collapses other entities', () => {
  const page = fs.readFileSync(path.join(__dirname, '../ui/index.html'), 'utf8');
  const controls = page.indexOf('id="home-controls"');
  const useful = page.indexOf('id="home-useful"');
  const other = page.indexOf('<details id="home-other"');
  assert.ok(controls > 0 && useful > controls && other > useful);
  assert.match(page, /Switches and lights/);
  assert.match(page, /Useful sensors/);
  assert.match(page, /Other Home Assistant entities/);
  const details = page.slice(other, page.indexOf('</details>', other));
  assert.doesNotMatch(details, /\sopen(?:\s|>)/);
});

function homeEntities(porchState) {
  return {
    available: true,
    counts: {switch: 2, light: 1, sensor: 2, binary_sensor: 2},
    entities: [
      {entity_id: 'sensor.sun_next_dawn', name: 'Next dawn', domain: 'sensor', state: '2020-01-08T07:00:00', available: true, device_class: 'timestamp', writable: false, capabilities: ['read']},
      {entity_id: 'binary_sensor.helper_status', name: 'Helper status', domain: 'binary_sensor', state: 'on', available: true, writable: false, capabilities: ['read']},
      {entity_id: 'sensor.hall_temp', name: 'Hall', domain: 'sensor', state: '21', unit: 'C', available: true, device_class: 'temperature', writable: false, capabilities: ['read']},
      {entity_id: 'binary_sensor.front_door', name: 'Front door', domain: 'binary_sensor', state: 'off', available: true, device_class: 'door', writable: false, capabilities: ['read']},
      {entity_id: 'switch.porch', name: 'Porch', domain: 'switch', state: porchState, available: true, writable: true, capabilities: ['read', 'turn_on', 'turn_off', 'brightness']},
      {entity_id: 'light.desk', name: 'Desk', domain: 'light', state: 'unavailable', available: false, writable: true, capabilities: ['read', 'turn_on', 'turn_off']},
      {entity_id: 'switch.mystery', name: 'Mystery', domain: 'switch', state: 'unknown', available: false, writable: false, capabilities: ['read']},
    ],
  };
}

function makeHomeControls() {
  const box = {
    attributes: {}, cards: {}, buttons: [], _html: '',
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return this.attributes[name] == null ? null : this.attributes[name]; },
    querySelector(sel) { const found = this.querySelectorAll(sel); return found[0] || null; },
    querySelectorAll(sel) {
      const card = /^\[data-home-card="([^"]+)"\]$/.exec(sel);
      if (card) return this.cards[card[1]] ? [this.cards[card[1]]] : [];
      if (sel === '[data-home-action]' || sel === 'button') return this.buttons.slice();
      return [];
    },
  };
  Object.defineProperty(box, 'innerHTML', {
    get() { return box._html; },
    set(html) {
      box._html = String(html);
      box.cards = {};
      box.buttons = [];
      const articles = /<article class="card" data-home-card="([^"]+)">([\s\S]*?)<\/article>/g;
      let match;
      while ((match = articles.exec(box._html))) {
        const chunk = match[2];
        const stateMatch = /<p class="home-state">([^<]*)<\/p>/.exec(chunk);
        const state = {textContent: stateMatch ? stateMatch[1] : ''};
        const buttons = [];
        const buttonRe = /<button type="button"([^>]*)>([^<]*)<\/button>/g;
        let button;
        while ((button = buttonRe.exec(chunk))) {
          const attrs = button[1];
          buttons.push({
            disabled: /\sdisabled(?:\s|=|$)/.test(attrs),
            listeners: {},
            getAttribute(name) {
              const found = new RegExp('(?:^|\\s)' + name + '="([^"]*)"').exec(attrs);
              return found ? found[1] : null;
            },
            addEventListener(type, fn) { (this.listeners[type] || (this.listeners[type] = [])).push(fn); },
          });
        }
        const card = {
          state, buttons,
          querySelector(sel) { return sel === '.home-state' ? state : (this.querySelectorAll(sel)[0] || null); },
          querySelectorAll(sel) { return sel === 'button' || sel === '[data-home-action]' ? buttons.slice() : []; },
        };
        box.cards[match[1]] = card;
        buttons.forEach(item => box.buttons.push(item));
      }
    },
  });
  return box;
}

async function settle() {
  for (let step = 0; step < 8; step += 1) await new Promise(resolve => setImmediate(resolve));
}

async function bootHome(states, results) {
  const elements = {};
  const requests = [];
  const posts = [];
  const waits = [];
  let reads = 0;
  let resultIndex = 0;
  const controls = makeHomeControls();
  const context = {
    Date, Intl, Map, Number, String, Object, Array, Promise,
    document: {getElementById(id) {
      if (id === 'home-controls') return controls;
      return elements[id] ||= {textContent: '', innerHTML: '', classList: {add() {}, remove() {}}};
    }},
    fetch: async (url, options) => {
      requests.push(url);
      if (url === '/api/home/entities') {
        const state = states[Math.min(reads, states.length - 1)];
        reads += 1;
        return {ok: true, json: async () => homeEntities(state)};
      }
      if (url === '/api/home/action') {
        posts.push(JSON.parse(options.body));
        await new Promise(resolve => waits.push(resolve));
        const result = results[Math.min(resultIndex, results.length - 1)];
        resultIndex += 1;
        return {ok: true, json: async () => result};
      }
      return {ok: true, json: async () => url === '/api/snapshot' ? snapshot() : {events: []}};
    },
    setInterval() {},
  };
  vm.runInNewContext(code, context);
  await settle();
  assert.deepEqual(requests.slice(0, 2), ['/api/snapshot', '/api/events?limit=20']);
  return {elements, controls, posts, waits, requests};
}

function clickHome(controls, action) {
  const button = controls.buttons.find(item => item.getAttribute('data-home-action') === action);
  assert.ok(button);
  button.listeners.click.forEach(fn => fn());
  return button;
}

test('home confirms switch state after a pending action and keeps noisy entities collapsed', async () => {
  const home = await bootHome(
    ['off', 'on', 'off'],
    [{accepted: true, reason: 'ACTION_ACCEPTED'}, {accepted: true, reason: 'ACTION_ACCEPTED'}],
  );
  assert.match(home.elements['home-useful'].innerHTML, /Hall/);
  assert.match(home.elements['home-useful'].innerHTML, /Front door/);
  assert.doesNotMatch(home.elements['home-useful'].innerHTML, /Next dawn|Helper status/);
  assert.match(home.elements['home-other-list'].innerHTML, /Next dawn/);
  assert.match(home.elements['home-other-list'].innerHTML, /Helper status/);
  assert.doesNotMatch(home.elements['home-other-list'].innerHTML, /Hall|Front door|Porch/);
  assert.equal(home.controls.cards['switch.porch'].state.textContent, 'OFF');
  assert.equal(home.controls.cards['light.desk'].state.textContent, 'UNAVAILABLE');
  assert.equal(home.controls.cards['switch.mystery'].state.textContent, 'UNKNOWN');
  clickHome(home.controls, 'set_brightness');
  assert.equal(home.elements['home-status'].textContent, 'MALFORMED_PARAMETERS');
  assert.equal(home.posts.length, 0);
  const onButton = clickHome(home.controls, 'turn_on');
  await settle();
  assert.equal(home.controls.cards['switch.porch'].state.textContent, 'Turning on…');
  assert.equal(onButton.disabled, true);
  assert.equal(home.posts.length, 1);
  clickHome(home.controls, 'turn_on');
  await settle();
  assert.equal(home.posts.length, 1);
  assert.equal(home.posts[0].action, 'turn_on');
  home.waits.shift()();
  await settle();
  assert.equal(home.controls.cards['switch.porch'].state.textContent, 'ON');
  assert.equal(onButton.disabled, false);
  const offButton = clickHome(home.controls, 'turn_off');
  await settle();
  assert.equal(home.controls.cards['switch.porch'].state.textContent, 'Turning off…');
  assert.equal(offButton.disabled, true);
  clickHome(home.controls, 'turn_off');
  assert.equal(home.posts.length, 2);
  home.waits.shift()();
  await settle();
  assert.equal(home.controls.cards['switch.porch'].state.textContent, 'OFF');
  assert.equal(offButton.disabled, false);
});

test('failed home action shows the reason and the confirmed state', async () => {
  const home = await bootHome(
    ['off', 'off'],
    [{accepted: false, reason: 'WRITE_REJECTED'}],
  );
  clickHome(home.controls, 'turn_on');
  await settle();
  assert.equal(home.controls.cards['switch.porch'].state.textContent, 'Turning on…');
  home.waits.shift()();
  await settle();
  assert.equal(home.elements['home-status'].textContent, 'WRITE_REJECTED');
  assert.equal(home.controls.cards['switch.porch'].state.textContent, 'OFF');
});
