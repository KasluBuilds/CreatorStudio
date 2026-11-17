'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const github = require('../src/github');
const { syncFromGitHub, SyncError, waveRefs, laneRefs } = require('../src/sync');
const store = require('../src/store');
const { Controller } = require('../src/controller');
const { makeFixtureFetch, pktLines, pkt } = require('../src/fixture');
const { fixtureText } = require('./helpers');

const FROZEN = path.join(__dirname, 'fixtures', 'frozen-2026-11-17');
const MAIN = 'a'.repeat(40);
const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'dash-test-'));
const fixtureFiles = () => ({
  'PROJECT_STATUS.json': fixtureText('PROJECT_STATUS.json'), 'ACTIVE_TASKS.json': fixtureText('ACTIVE_TASKS.json'),
  'DEVELOPMENT_TRACKER.json': fixtureText('DEVELOPMENT_TRACKER.json'), 'AGENT_REGISTRY.json': fixtureText('AGENT_REGISTRY.json'),
  'AI_Creator_Studio_Documentation/FIRST_REAL_VIDEO_CHECKLIST.md': fixtureText('FIRST_REAL_VIDEO_CHECKLIST.md'),
});
const fx = (o = {}) => makeFixtureFetch({ root: null, mainSha: MAIN, files: fixtureFiles(), ...o });

// ---- github.js -----------------------------------------------------------------------------------------------
test('parseInfoRefs reads the smart-HTTP advertisement and skips peeled tags and headers', () => {
  const refs = { 'refs/heads/main': MAIN, 'refs/heads/wave/w01-x': 'b'.repeat(40), 'refs/tags/v1': 'c'.repeat(40) };
  assert.deepEqual(github.parseInfoRefs(pktLines(refs)), refs);
  const withPeeled = pktLines({ 'refs/heads/main': MAIN }).replace(/0000$/, '') + pkt(`${'d'.repeat(40)} refs/tags/v1^{}\n`) + '0000';
  assert.deepEqual(github.parseInfoRefs(withPeeled), { 'refs/heads/main': MAIN });
  assert.throws(() => github.parseInfoRefs('zzzz'), /Malformed/);
  assert.deepEqual(github.parseInfoRefs(''), {});
});

test('fetchFile pins to a commit, encodes the path, returns null on 404 and refuses unsafe paths', async () => {
  const f = fx({ files: { 'a b/c.json': '{"x":1}', 'gone.json': null } });
  assert.equal(await github.fetchFile(f, MAIN, 'a b/c.json'), '{"x":1}');
  assert.ok(f.calls.some((u) => u.includes(`/${MAIN}/a%20b/c.json`)));
  assert.equal(await github.fetchFile(f, MAIN, 'gone.json'), null);
  await assert.rejects(() => github.fetchFile(f, 'main', 'x'), /full commit SHA/);
  for (const bad of ['../x', '/etc/passwd', 'a\\b', 'a?b', 'a#b', '']) await assert.rejects(() => github.fetchFile(f, MAIN, bad), /unsafe/, bad);
});

test('a refused or failing request carries its HTTP status and is not retried; a network error is marked offline', async () => {
  let n = 0;
  const f403 = async () => { n++; return { status: 403, ok: false, text: async () => '' }; };
  await assert.rejects(() => github.request(f403, 'https://x.invalid/'), (e) => e.status === 403 && /rate limit/.test(e.message));
  assert.equal(n, 1);
  let m = 0;
  const down = async () => { m++; throw new TypeError('fetch failed'); };
  await assert.rejects(() => github.request(down, 'https://x.invalid/?token=SECRET'), (e) => e.offline === true && !e.message.includes('SECRET'));
  assert.equal(m, 2, 'a network error is retried once');
});

test('refs fall back to the REST API when the git endpoint gives nothing useful', async () => {
  const api = JSON.stringify([{ ref: 'refs/heads/main', object: { sha: MAIN } }, { ref: 'bogus', object: { sha: 'zz' } }]);
  const f = async (url) => {
    if (url.includes('info/refs')) return { status: 200, ok: true, text: async () => '0000' };
    return { status: 200, ok: true, json: async () => JSON.parse(api), text: async () => api };
  };
  const r = await github.fetchRefs(f);
  assert.equal(r.via, 'api');
  assert.deepEqual(r.refs, { 'refs/heads/main': MAIN });
});

test('no credentials or tokens appear anywhere in the app source', () => {
  const dirs = [path.join(__dirname, '..', 'src'), path.join(__dirname, '..', 'ui'), path.join(__dirname, '..')];
  const files = [];
  for (const d of dirs) for (const n of fs.readdirSync(d)) if (/\.(js|html|css|json)$/.test(n) && n !== 'package-lock.json') files.push(path.join(d, n));
  for (const f of files) {
    const text = fs.readFileSync(f, 'utf8');
    assert.doesNotMatch(text, /Authorization|Bearer\s|ghp_[A-Za-z0-9]{20}|github_pat_|access_token\s*[:=]/i, f);
  }
});

// ---- sync.js -------------------------------------------------------------------------------------------------
test('sync builds one snapshot pinned to the main commit, with every canonical file', async () => {
  const f = fx();
  const snap = await syncFromGitHub(f, { now: () => new Date('2026-11-17T12:00:00Z') });
  assert.equal(snap.source.commit, MAIN);
  assert.equal(snap.fetchedAt, '2026-11-17T12:00:00.000Z');
  for (const name of ['PROJECT_STATUS.json', 'ACTIVE_TASKS.json', 'DEVELOPMENT_TRACKER.json', 'AGENT_REGISTRY.json']) assert.ok(snap.files[name], name);
  assert.ok(f.calls.filter((u) => u.includes('raw.githubusercontent')).every((u) => u.includes(`/${MAIN}/`)), 'every file is read at the same commit');
  assert.deepEqual(snap.warnings.filter((w) => !w.includes('SESSION_HANDOFF')), []);
});

test('sync reads wave manifests and lane reports at the wave branch commit and records pushed lane branches', async () => {
  const manifest = JSON.stringify({ wave_id: 'w01-demo', title: 't', lanes: [{ id: 'L1', task_id: 'T-X-001' }, { id: 'L2', task_id: 'T-Y-001' }] });
  const f = fx({ waves: { 'w01-demo': { sha: 'b'.repeat(40), files: { 'waves/w01-demo/wave.json': manifest, 'waves/w01-demo/lanes/L1/report.json': '{"status":"DONE"}' } } },
    extraRefs: { 'refs/heads/lane/w01-demo/L1': 'c'.repeat(40), 'refs/heads/task/other': 'd'.repeat(40) } });
  const snap = await syncFromGitHub(f);
  assert.deepEqual(Object.keys(snap.waves), ['w01-demo']);
  assert.deepEqual(Object.keys(snap.waves['w01-demo'].reports), ['L1']);
  assert.deepEqual(snap.laneRefs, { 'w01-demo/L1': 'c'.repeat(40) });
  assert.deepEqual(waveRefs({ 'refs/heads/wave/w01-a': 'x'.repeat(40), 'refs/heads/wave/not-a-wave': 'y'.repeat(40) }).map((w) => w.id), ['w01-a']);
  assert.deepEqual(laneRefs({ 'refs/heads/lane/w01-a/L1': '1', 'refs/heads/lane/bad': '2' }), { 'w01-a/L1': '1' });
});

test('a wave with an unreadable manifest becomes a warning, not a failure', async () => {
  const f = fx({ waves: { 'w02-bad': { sha: 'b'.repeat(40), files: { 'waves/w02-bad/wave.json': 'not json' } } } });
  const snap = await syncFromGitHub(f);
  assert.deepEqual(snap.waves, {});
  assert.ok(snap.warnings.some((w) => w.includes('w02-bad')));
});

test('offline: sync throws a SyncError marked offline', async () => {
  await assert.rejects(() => syncFromGitHub(fx({ fail: 'offline' })), (e) => e instanceof SyncError && e.offline === true);
});

test('a refused request (rate limit) is not "offline"', async () => {
  await assert.rejects(() => syncFromGitHub(fx({ fail: 403 })), (e) => e instanceof SyncError && e.offline === false);
});

test('a missing required file fails the sync; a missing optional file only warns', async () => {
  await assert.rejects(() => syncFromGitHub(fx({ files: { ...fixtureFiles(), 'PROJECT_STATUS.json': null } })), /PROJECT_STATUS.json does not exist/);
  const snap = await syncFromGitHub(fx({ files: { ...fixtureFiles(), 'AGENT_REGISTRY.json': null } }));
  assert.ok(snap.warnings.some((w) => w.includes('AGENT_REGISTRY.json')));
  assert.equal(snap.files['AGENT_REGISTRY.json'], undefined);
});

test('the latest release is read when present and never breaks a refresh', async () => {
  const snap = await syncFromGitHub(fx({ release: { tag_name: 'dashboard-v1.2.0', html_url: 'https://github.com/KasluBuilds/CreatorStudio/releases/tag/dashboard-v1.2.0' } }));
  assert.equal(snap.latestRelease.tag, 'dashboard-v1.2.0');
  const none = await syncFromGitHub(fx());
  assert.equal(none.latestRelease, null);
});

// ---- store.js ------------------------------------------------------------------------------------------------
test('the snapshot round-trips, and a corrupt or incomplete file counts as nothing saved', async () => {
  const dir = tmp();
  const snap = await syncFromGitHub(fx());
  store.saveSnapshot(dir, snap);
  assert.equal(store.loadSnapshot(dir).source.commit, MAIN);
  assert.equal(fs.readdirSync(dir).some((n) => n.endsWith('.tmp')), false, 'no temporary files left behind');
  fs.writeFileSync(path.join(dir, store.SNAPSHOT_FILE), '{"schema":1, "truncat');
  assert.equal(store.loadSnapshot(dir), null);
  fs.writeFileSync(path.join(dir, store.SNAPSHOT_FILE), JSON.stringify({ schema: 1, fetchedAt: 'x', files: {}, source: { commit: MAIN } }));
  assert.equal(store.loadSnapshot(dir), null, 'a snapshot without PROJECT_STATUS.json is unusable');
  assert.equal(store.loadSnapshot(tmp()), null);
  assert.deepEqual(store.loadMeta(tmp()), {});
});

// ---- controller.js -------------------------------------------------------------------------------------------
const NOW = () => new Date('2026-11-17T12:00:00Z');

test('first refresh online: state is online, the snapshot is saved, nothing is written to the repository', async () => {
  const dir = tmp();
  const c = new Controller({ fetchFn: fx(), dir, now: NOW });
  const s0 = c.init();
  assert.equal(s0.hasSnapshot, false);
  assert.equal(s0.model, null);
  const s = await c.refresh();
  assert.equal(s.connection.state, 'online');
  assert.equal(s.model.ok, true);
  assert.equal(s.model.sync.commit, MAIN);
  assert.ok(store.loadSnapshot(dir));
});

test('offline after a good refresh: the last-known snapshot stays usable and the warning says so', async () => {
  const dir = tmp();
  await new Controller({ fetchFn: fx(), dir, now: NOW }).init() && null;
  const online = new Controller({ fetchFn: fx(), dir, now: NOW }); online.init(); await online.refresh();
  const off = new Controller({ fetchFn: fx({ fail: 'offline' }), dir, now: () => new Date('2026-11-17T20:00:00Z') });
  assert.equal(off.init().connection.state, 'cached');
  const s = await off.refresh();
  assert.equal(s.connection.state, 'offline');
  assert.match(s.connection.message, /cannot be reached/);
  assert.equal(s.hasSnapshot, true);
  assert.equal(s.model.ok, true);
  assert.equal(s.model.sync.stale, true, 'eight hours old is stale');
  assert.equal(s.connection.lastSuccessAt, '2026-11-17T12:00:00.000Z');
});

test('offline with no saved snapshot: no model, and a clear state', async () => {
  const c = new Controller({ fetchFn: fx({ fail: 'offline' }), dir: tmp(), now: NOW }); c.init();
  const s = await c.refresh();
  assert.equal(s.connection.state, 'offline');
  assert.equal(s.hasSnapshot, false);
  assert.equal(s.model, null);
});

test('a bad answer from GitHub never replaces a good saved snapshot', async () => {
  const dir = tmp();
  const good = new Controller({ fetchFn: fx(), dir, now: NOW }); good.init(); await good.refresh();
  const bad = new Controller({ fetchFn: fx({ files: { ...fixtureFiles(), 'PROJECT_STATUS.json': '{"milestones": "broken"' } }), dir, now: NOW });
  bad.init();
  const s = await bad.refresh();
  assert.equal(s.connection.state, 'error');
  assert.equal(s.model.ok, true, 'the previous snapshot is still shown');
  assert.equal(JSON.parse(store.loadSnapshot(dir).files['PROJECT_STATUS.json']).milestones.length > 5, true);
});

test('a rate-limited refresh is an error state with the saved data still shown', async () => {
  const dir = tmp();
  const good = new Controller({ fetchFn: fx(), dir, now: NOW }); good.init(); await good.refresh();
  const limited = new Controller({ fetchFn: fx({ fail: 403 }), dir, now: NOW }); limited.init();
  const s = await limited.refresh();
  assert.equal(s.connection.state, 'error');
  assert.equal(s.model.ok, true);
});

test('concurrent refresh calls share one request', async () => {
  const f = fx();
  const c = new Controller({ fetchFn: f, dir: tmp(), now: NOW }); c.init();
  const [a, b] = [c.refresh(), c.refresh()];
  assert.equal(a, b);
  await a;
  const infoCalls = f.calls.filter((u) => u.includes('info/refs')).length;
  assert.equal(infoCalls, 1);
  assert.equal(c.getState().refreshing, false);
});

function firedFiles() {
  const status = JSON.parse(fixtureText('PROJECT_STATUS.json'));
  status.milestones.find((m) => m.id === 'M-LFE-03').implementation.state = 'IMPLEMENTED';
  status.user_action_gates[0].status = 'TRIGGERED';
  return { ...fixtureFiles(), 'PROJECT_STATUS.json': JSON.stringify(status) };
}

test('filming gate: no notification and no attention while pending', async () => {
  const notes = [];
  const c = new Controller({ fetchFn: fx(), dir: tmp(), now: NOW, notify: (n) => notes.push(n) }); c.init();
  const s = await c.refresh();
  assert.deepEqual(s.attention, []);
  assert.deepEqual(notes, []);
});

test('filming gate: notifies once when it becomes ready, keeps the in-app attention, and re-arms if reverted', async () => {
  const dir = tmp();
  const notes = [];
  const run = async (files) => { const c = new Controller({ fetchFn: fx({ files }), dir, now: NOW, notify: (n) => notes.push(n) }); c.init(); return c.refresh(); };
  await run(fixtureFiles());
  assert.equal(notes.length, 0);
  let s = await run(firedFiles());
  assert.equal(notes.length, 1);
  assert.match(notes[0].title, /real videos/);
  assert.deepEqual(s.attention.map((a) => a.id), ['G-FIRST-REAL-VIDEO']);
  s = await run(firedFiles());                                // opening again must not notify again, but the banner stays
  assert.equal(notes.length, 1);
  assert.equal(s.attention.length, 1);
  await run(fixtureFiles());                                  // record reverted: re-armed
  await run(firedFiles());
  assert.equal(notes.length, 2);
});

test('a notification that throws does not break the refresh', async () => {
  const c = new Controller({ fetchFn: fx({ files: firedFiles() }), dir: tmp(), now: NOW, notify: () => { throw new Error('toast failed'); } }); c.init();
  const s = await c.refresh();
  assert.equal(s.connection.state, 'online');
});

test('the controller is read-only toward the repository: it only ever issues GET-style reads', async () => {
  const f = fx(); const c = new Controller({ fetchFn: f, dir: tmp(), now: NOW }); c.init(); await c.refresh();
  assert.ok(f.calls.every((u) => /^https:\/\/(github\.com|raw\.githubusercontent\.com|api\.github\.com)\//.test(u)));
  const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'github.js'), 'utf8');
  assert.doesNotMatch(src, /method:\s*['"](POST|PUT|PATCH|DELETE)/i);
});
