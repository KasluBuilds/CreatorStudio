'use strict';
// Orbit operation views (lane L2 of wave w05): next actions, waiting, blocked, idle reason, warnings, supervisor health, handoff and
// per-worker context pressure, rendered from hand-made synthetic snapshots that follow the shape lane L1 adds to snapshot.py.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const orbit = require('../src/orbit');

const FIXTURE = path.join(__dirname, 'fixtures', 'orbit', 'snapshot.json');
const raw = () => JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
const GEN = Date.parse(raw().generated_at);
const sec = (n) => GEN + n * 1000;
const section = (v, id) => v.sections.find((s) => s.id === id);
const row = (v, id, name) => section(v, id).rows.find((r) => r.name === name);
const items = (v, id) => section(v, id).items.map((i) => i.text);

const ev = (value, extra = {}) => ({ label: 'OBSERVED', value, timestamp: raw().generated_at, source: 'synthetic', freshness: 'live', age_seconds: 0, ...extra });
const unav = (reason) => ({ label: 'UNAVAILABLE', value: null, timestamp: raw().generated_at, source: 'synthetic', freshness: 'offline', reason });

const withOperation = () => {
  const r = raw();
  r.operation = {
    actionable: [{ id: 'T-1', kind: 'review', priority: 1, summary: 'review lane L2', inspect: 'git log' }, { id: 'T-2', kind: 'merge', priority: 2, summary: 'merge lane L1', inspect: 'wave status' }],
    others_waiting: [{ id: 'T-3', kind: 'worker', priority: 3, summary: 'worker is running', inspect: 'worker status' }],
    blocked: [{ id: 'T-4', kind: 'task', summary: 'restart budget used up' }],
    idle_reason: ev('lanes are still being worked on'), warnings: ['registry is older than the handoff'], fingerprint: 'abcdef0123456789abcdef',
  };
  r.supervisor_health = { lock_alive: ev(true), last_tick_at: ev('2026-11-18T11:59:50+00:00'), last_event_kind: ev('tick') };
  r.handoff = { latest_id: ev('H-1'), verified: ev(true), created_at: ev('2026-11-18T11:00:00+00:00') };
  for (const w of r.workers) w.context_pressure = { tokens: ev(1200), window: ev(200000), ratio: ev(0.006), level: ev('low') };
  return r;
};

test('orbit operation: actions render ordered, waiting and blocked separately, with idle reason and warnings', () => {
  const v = orbit.evaluate(withOperation(), sec(5));
  assert.equal(section(v, 'operation_actionable').title, 'Next actions for Main');
  const act = items(v, 'operation_actionable');
  assert.match(act[0], /^1\. T-1 \[review\] priority 1 review lane L2/);
  assert.match(act[1], /^2\. T-2/);
  assert.equal(section(v, 'operation_actionable').items[0].inspect, 'git log');
  assert.equal(section(v, 'operation_waiting').title, 'Waiting on others');
  assert.match(items(v, 'operation_waiting')[0], /T-3/);
  assert.match(items(v, 'operation_blocked')[0], /^BLOCKED T-4/);
  const idle = row(v, 'operation_idle', 'idle reason');
  assert.equal(idle.text, 'lanes are still being worked on');
  assert.equal(idle.quality, 'live');
  assert.match(items(v, 'operation_idle')[0], /^warning: registry is older/);
  assert.match(section(v, 'operation_idle').note, /abcdef0123456789/);
});

test('orbit operation: supervisor health and handoff keep their data-quality badge', () => {
  const r = withOperation();
  r.supervisor_health.last_tick_at = ev('x', { freshness: 'cached', timestamp: new Date(GEN - 400 * 1000).toISOString() });
  r.handoff.verified = unav('handoff file not found');
  const v = orbit.evaluate(r, sec(5));
  assert.equal(row(v, 'supervisor_health', 'lock alive').quality, 'live');
  assert.equal(row(v, 'supervisor_health', 'last tick at').quality, 'cached');
  assert.equal(row(v, 'supervisor_health', 'last event kind').text, 'tick');
  assert.equal(row(v, 'handoff', 'handoff latest id').text, 'H-1');
  const ver = row(v, 'handoff', 'handoff verified');
  assert.equal(ver.quality, 'unavailable');
  assert.equal(ver.reason, 'handoff file not found');
  for (const id of ['operation_idle', 'supervisor_health', 'handoff']) for (const x of section(v, id).rows) assert.ok(['live', 'cached', 'stale', 'estimated', 'unavailable'].includes(x.quality));
});

test('orbit operation: a missing section renders as unavailable with a reason, never as an empty success', () => {
  const v = orbit.evaluate(raw(), sec(5));                 // the older fixture has none of the new sections
  for (const [id, title] of [['operation_actionable', 'Next actions for Main'], ['operation_waiting', 'Waiting on others'], ['operation_blocked', 'Blocked'],
    ['operation_idle', 'Idle reason and warnings'], ['supervisor_health', 'Supervisor health'], ['handoff', 'Handoff']]) {
    const s = section(v, id);
    assert.equal(s.title, title);
    assert.equal(s.unavailable, true);
    assert.equal(s.items.length, 0);
    assert.equal(s.rows[0].label, 'UNAVAILABLE');
    assert.equal(s.rows[0].quality, 'unavailable');
    assert.ok(s.rows[0].reason.length > 10);
    assert.equal(s.freshness, 'offline');
  }
  assert.match(section(v, 'operation_actionable').rows[0].reason, /no operation section/);
  const r = withOperation(); r.operation = 'broken'; delete r.handoff;
  const w = orbit.evaluate(r, sec(5));
  assert.equal(section(w, 'operation_blocked').unavailable, true);
  assert.equal(section(w, 'handoff').unavailable, true);
  assert.ok(!section(w, 'supervisor_health').unavailable);
});

test('orbit operation: an empty but present operation section says so explicitly', () => {
  const r = withOperation(); r.operation.actionable = []; r.operation.blocked = []; r.operation.others_waiting = [];
  const v = orbit.evaluate(r, sec(5));
  assert.ok(!section(v, 'operation_actionable').unavailable);
  assert.match(section(v, 'operation_actionable').empty, /Nothing is actionable/);
  assert.match(row(v, 'operation_idle', 'idle reason').text, /lanes/);
});

test('orbit operation: a registered-only worker is never active, and a blocked task is never running', () => {
  const r = withOperation();
  const base = r.workers[0];
  r.workers = [{ ...base, worker_id: 'w-a', task_id: 'T-A', status: ev('ASSIGNED') }, { ...base, worker_id: 'w-b', task_id: 'T-B', status: ev('BLOCKED') },
    { ...base, worker_id: 'w-c', task_id: 'T-C', status: ev('RUNNING') }, { ...base, worker_id: 'w-d', task_id: 'T-D', status: unav('no registry') }];
  r.tasks = [{ task_id: 'T-A', workers: ['w-a'], results: [] }, { task_id: 'T-B', workers: ['w-b'], results: [] }, { task_id: 'T-C', workers: ['w-c'], results: [] }, { task_id: 'T-D', workers: ['w-d'], results: [] }];
  const v = orbit.evaluate(r, sec(5));
  const acts = section(v, 'workers').items.map((i) => i.activity);
  assert.equal(acts[0].kind, 'registered');
  assert.match(acts[0].text, /not active/);
  assert.equal(acts[1].kind, 'blocked');
  assert.equal(acts[2].kind, 'active');
  assert.equal(acts[3].kind, 'unknown');
  const t = items(v, 'tasks');
  assert.match(t[0], /T-A \[no worker running\]/);
  assert.match(t[1], /T-B \[BLOCKED, not running\]/);
  assert.match(t[2], /T-C \[running\]/);
  // a task named in operation.blocked is never "running", even when its worker says RUNNING
  r.operation.blocked = [{ id: 'T-C', kind: 'task', summary: 'waiting for a decision' }];
  assert.match(items(orbit.evaluate(r, sec(5)), 'tasks')[2], /T-C \[BLOCKED, not running\]/);
  for (const s of ['ASSIGNED', 'STOPPED', 'FAILED']) assert.notEqual(orbit.activityOf({ label: 'OBSERVED', value: s }).kind, 'active');
});

test('orbit operation: context pressure shows each value with its badge; UNAVAILABLE with a reason when no transcript exists', () => {
  const r = withOperation();
  r.workers.push({ ...r.workers[0], worker_id: 'w-x', context_pressure: { tokens: unav('transcript not found'), window: unav('transcript not found'), ratio: unav('transcript not found'), level: unav('transcript not found') } });
  r.workers.push({ ...r.workers[0], worker_id: 'w-y', context_pressure: undefined });
  const ws = section(orbit.evaluate(r, sec(5)), 'workers').items;
  const first = ws[0].context;
  assert.deepEqual(first.map((c) => c.name), ['context tokens', 'context window', 'context ratio', 'context level']);
  assert.equal(first[0].text, '1200');
  assert.equal(first[3].text, 'low');
  assert.ok(first.every((c) => c.quality === 'live'));
  assert.ok(ws[ws.length - 2].context.every((c) => c.quality === 'unavailable' && c.reason === 'transcript not found'));
  const none = ws[ws.length - 1].context;
  assert.equal(none.length, 1);
  assert.equal(none[0].quality, 'unavailable');
  assert.ok(none[0].reason);
});

test('orbit operation: the view stays read-only (no file, network or process access, input not mutated)', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'orbit.js'), 'utf8');
  assert.ok(!/require\(|child_process|fetch\(|writeFile|https?:/.test(src));
  const r = withOperation(); const before = JSON.stringify(r);
  orbit.evaluate(r, sec(5));
  assert.equal(JSON.stringify(r), before);
});
