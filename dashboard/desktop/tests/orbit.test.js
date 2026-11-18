'use strict';
// Orbit (supervisor view): live / cached / stale / offline / invalid rendering from a snapshot produced by
// orchestrator/dev_supervisor/snapshot.py (synthetic fixture), the OBSERVED-only account rule, and the read-only guarantee.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const orbit = require('../src/orbit');
const orbitFile = require('../src/orbit-file');

const FIXTURE = path.join(__dirname, 'fixtures', 'orbit', 'snapshot.json');
const raw = () => JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
const GEN = Date.parse(raw().generated_at);
const sec = (n) => GEN + n * 1000;
const section = (v, id) => v.sections.find((s) => s.id === id);
const row = (v, id, name) => section(v, id).rows.find((r) => r.name === name);

test('orbit: a fresh snapshot is live, with an age on every value', () => {
  const v = orbit.evaluate(raw(), sec(10));
  assert.equal(v.state, 'cached');                         // the registry values and the missing PR are not live
  const status = row(v, 'main', 'status');
  assert.equal(status.freshness, 'live');
  assert.equal(status.label, 'OBSERVED');
  assert.match(status.ageText, /just now/);
  assert.ok(v.sections.every((s) => s.freshness === null || orbit.FRESHNESS.includes(s.freshness)));
  for (const s of v.sections) for (const r of s.rows) { assert.ok(r.ageText); assert.ok(orbit.FRESHNESS.includes(r.freshness)); }
  assert.equal(section(v, 'workers').items[0].status.freshness, 'cached');       // persisted registry record
});

test('orbit: a purely live snapshot reports live', () => {
  const r = raw();
  const keep = ['main', 'account_usage'];
  for (const k of Object.keys(r)) if (!keep.includes(k) && typeof r[k] === 'object' && r[k] && r[k].label) { r[k] = { label: 'OBSERVED', value: 'x', timestamp: r.generated_at, source: 't', freshness: 'live', age_seconds: 0 }; }
  r.workers = []; r.tasks = []; r.last_github_checkpoint = r.branch;
  r.model_provider = { model: r.branch, providers: [] };
  r.main = { present: true, status: r.branch, context_pressure: {} };
  r.account_usage = { provider: 'none', usage: { label: 'UNAVAILABLE', value: null, reason: 'none', timestamp: r.generated_at, source: 's', freshness: 'offline' } };
  const v = orbit.evaluate(r, sec(5));
  assert.equal(v.state, 'cached');                          // the unavailable account usage keeps it from claiming "live"
  r.account_usage = { provider: 'p', usage: { five_hour_used_percentage: { label: 'OBSERVED', value: 1, timestamp: r.generated_at, source: 't', freshness: 'live' } } };
  assert.equal(orbit.evaluate(r, sec(5)).state, 'live');
});

test('orbit: the same file read later turns cached, then stale (ages recomputed at display time)', () => {
  const later = orbit.evaluate(raw(), sec(300));
  assert.equal(row(later, 'main', 'status').freshness, 'cached');
  assert.match(row(later, 'main', 'status').ageText, /min ago/);
  const old = orbit.evaluate(raw(), sec(7200));
  assert.equal(old.state, 'stale');
  assert.equal(row(old, 'main', 'status').freshness, 'stale');
  assert.match(old.message, /Stale/);
  assert.match(old.generatedAtText, /h ago/);
});

test('orbit: missing file, unreadable file and no file chosen are explicit states, never an empty success', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'orbit-'));
  try {
    const none = orbit.fromLoad(orbitFile.load(null));
    assert.equal(none.state, 'unconfigured');
    assert.match(none.message, /No snapshot file is selected/);
    const missing = orbit.fromLoad(orbitFile.load(path.join(dir, 'absent.json')));
    assert.equal(missing.state, 'offline');
    assert.match(missing.message, /not there/);
    fs.writeFileSync(path.join(dir, 'bad.json'), '{ not json');
    assert.equal(orbit.fromLoad(orbitFile.load(path.join(dir, 'bad.json'))).state, 'invalid');
    fs.writeFileSync(path.join(dir, 'other.json'), JSON.stringify({ hello: 1 }));
    const other = orbit.fromLoad(orbitFile.load(path.join(dir, 'other.json')));
    assert.equal(other.state, 'invalid');
    assert.equal(orbit.fromLoad(orbitFile.load(dir)).state, 'invalid');               // a folder
    for (const v of [none, missing, other]) assert.equal(v.sections.length, 0);       // nothing that looks like data
    const r = raw(); delete r.generated_at;
    assert.equal(orbit.evaluate(r, sec(1)).state, 'invalid');
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('orbit: a snapshot where nothing answered is offline and says why on each row', () => {
  const r = raw();
  const off = (why) => ({ label: 'UNAVAILABLE', value: null, timestamp: r.generated_at, source: 's', freshness: 'offline', reason: why });
  const snap = { schema_version: 1, product: 'Orbit', read_only: true, generated_at: r.generated_at, main: { present: false, reason: 'no telemetry source' },
    account_usage: { provider: 'none', usage: off('no source') }, registry: { present: false, reason: 'no supervisor registry available' }, workers: [], tasks: [],
    model_provider: { model: off('x'), providers: [] }, branch: off('b'), pull_request: off('p'), last_github_checkpoint: off('c'), next_actions: ['start the supervisor'] };
  const v = orbit.evaluate(snap, sec(1));
  assert.equal(v.state, 'offline');
  assert.match(section(v, 'workers').empty, /No worker registry/);
  assert.equal(row(v, 'main', 'main session').label, 'UNAVAILABLE');
  assert.equal(row(v, 'git', 'pull request').reason, 'p');
});

test('orbit: account usage is shown only when OBSERVED; REPORTED or INFERRED values are UNAVAILABLE with a reason', () => {
  const r = raw();
  const mk = (label) => ({ label, value: 42, timestamp: r.generated_at, source: 's', freshness: 'live' });
  for (const label of ['REPORTED', 'INFERRED']) {
    r.account_usage = { provider: 'statusline', usage: { five_hour_used_percentage: mk(label) } };
    const rw = section(orbit.evaluate(r, sec(1)), 'account').rows[0];
    assert.equal(rw.label, 'UNAVAILABLE');
    assert.equal(rw.value, null);
    assert.match(rw.reason, new RegExp(`labelled ${label}`));
    assert.ok(!JSON.stringify(rw).includes('42'));
  }
  r.account_usage = { provider: 'p', usage: { five_hour_used_percentage: mk('OBSERVED') } };
  const ok = section(orbit.evaluate(r, sec(1)), 'account').rows[0];
  assert.equal(ok.label, 'OBSERVED');
  assert.equal(ok.text, '42');
  // the real Python-built fixture carries a REPORTED statusline payload and must not leak its number
  const fx = section(orbit.evaluate(raw(), sec(1)), 'account').rows[0];
  assert.equal(fx.label, 'UNAVAILABLE');
});

test('orbit: malformed values degrade to unavailable instead of throwing', () => {
  const r = raw();
  r.workers = [null, { worker_id: 7 }].filter(Boolean); r.branch = 'oops'; r.blockers = ['plain', { reason: 'r' }]; r.next_actions = [1, null];
  assert.doesNotThrow(() => orbit.evaluate(r, sec(1)));
  assert.equal(row(orbit.evaluate(r, sec(1)), 'git', 'branch').label, 'UNAVAILABLE');
});

test('orbit: blockers, failures, retries and next actions come through from the fixture', () => {
  const v = orbit.evaluate(raw(), sec(10));
  assert.ok(section(v, 'failures').items.some((i) => /worker_failed/.test(i.text)));
  assert.ok(section(v, 'next').items.some((i) => /w-synth-2/.test(i.text)));
  assert.match(row(v, 'git', 'last GitHub checkpoint').text, /abcd1234/);
  assert.equal(section(v, 'workers').items.length, 2);
});

test('orbit: read-only. Reading a snapshot leaves the file unchanged; only the app setting is ever written', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'orbit-ro-'));
  try {
    const file = path.join(dir, 'snap.json');
    fs.copyFileSync(FIXTURE, file);
    const sha = (f) => crypto.createHash('sha256').update(fs.readFileSync(f)).digest('hex');
    const before = { sha: sha(file), m: fs.statSync(file).mtimeMs, names: fs.readdirSync(dir) };
    orbit.fromLoad(orbitFile.load(file), sec(5));
    assert.equal(sha(file), before.sha);
    assert.equal(fs.statSync(file).mtimeMs, before.m);
    assert.deepEqual(fs.readdirSync(dir), before.names);
    const appDir = path.join(dir, 'app');
    orbitFile.saveSetting(appDir, file);
    assert.equal(orbitFile.loadSetting(appDir), file);
    assert.deepEqual(fs.readdirSync(appDir), [orbitFile.SETTING_FILE]);
    assert.equal(sha(file), before.sha);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('orbit: source code has no network, process or repository-writing code path', () => {
  const read = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
  const strip = (s) => s.replace(/\/\/.*$/gm, '');
  const evaluator = strip(read('src/orbit.js'));
  assert.ok(!/require\(/.test(evaluator), 'orbit.js is pure: it requires nothing');
  const file = strip(read('src/orbit-file.js'));
  for (const bad of ['fetch', 'http', 'net.', 'child_process', 'spawn', 'exec(', 'XMLHttpRequest', 'WebSocket', 'unlink', 'rmSync', 'appendFile', 'writeFile', 'rename', 'copyFile', 'git ']) {
    assert.ok(!file.includes(bad), `orbit-file.js must not use ${bad}`);
  }
  assert.equal((file.match(/atomicWrite\(/g) || []).length, 1);          // the single setting write (the app's own folder)
  const ui = strip(read('ui/views.js') + read('ui/app.js'));
  assert.ok(!/fetch\(|XMLHttpRequest|WebSocket|\.write|method:/.test(ui), 'UI code makes no request and writes nothing');
  const preload = read('preload.js');
  assert.deepEqual([...preload.matchAll(/orbit[A-Za-z]*: \(\) => ipcRenderer\.invoke\('([a-z:]+)'\)/g)].map((m) => m[1]).sort(), ['orbit:reload', 'orbit:select']);
  const main = strip(read('main.js'));
  const orbitHandlers = main.slice(main.indexOf("'orbit:reload'"), main.indexOf("'link:open'"));
  assert.ok(!/writeFile|execFile|spawn|fetch\(|net\./.test(orbitHandlers));
});

test('orbit: product naming is Orbit in the window title, the new view and the About text; existing identifiers are unchanged', () => {
  const read = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
  assert.match(read('main.js'), /title: 'Orbit · Creator Studio Dashboard'/);
  assert.match(read('ui/index.html'), /<title>Orbit · Creator Studio Dashboard<\/title>/);
  assert.match(read('ui/views.js'), /h1', null, 'Orbit: operation and supervisor'/);
  assert.match(read('ui/views.js'), /'About Orbit'/);
  const pkg = JSON.parse(read('package.json'));
  assert.equal(pkg.name, 'creator-studio-dashboard');
  assert.equal(pkg.productName, 'Creator Studio Dashboard');
  assert.equal(pkg.build.appId, 'studio.creator.dashboard');
  assert.equal(pkg.build.win.artifactName, 'CreatorStudio-Dashboard-${version}-portable.exe');
  assert.match(read('main.js'), /setAppUserModelId\('studio\.creator\.dashboard'\)/);
});

// ---- rendering through the real ui/dom.js + ui/views.js, with a minimal DOM stand-in (no browser or Electron needed)
function renderWithFakeDom(view) {
  class FakeNode { constructor(tag, text) { this.tag = tag; this.text = text; this.children = []; this.attrs = {}; this.handlers = {}; }
    appendChild(c) { this.children.push(c); return c; } setAttribute(k, v) { this.attrs[k] = v; } addEventListener(k, f) { this.handlers[k] = f; }
    get dataset() { return (this._d = this._d || {}); } get firstChild() { return this.children[0]; } removeChild() {} }
  const saved = { Node: global.Node, document: global.document, window: global.window };
  global.Node = FakeNode;
  global.document = { createElement: (t) => new FakeNode(t), createElementNS: (_n, t) => new FakeNode(t), createTextNode: (t) => new FakeNode('#text', String(t)) };
  global.window = { Dashboard: {}, dashboard: { orbitSelect() {}, orbitReload() {}, openLink() {} } };
  try {
    for (const f of ['../ui/dom.js', '../ui/views.js']) { delete require.cache[require.resolve(f)]; require(f); }
    const root = global.window.Dashboard.views.renderOrbit(view);
    const out = []; const walk = (n) => { if (n.tag === '#text') out.push(n.text); else n.children.forEach(walk); };
    walk(root);
    return { root, text: out.join(' ') };
  } finally { Object.assign(global, saved); if (!saved.Node) delete global.Node; if (!saved.document) delete global.document; if (!saved.window) delete global.window; }
}

test('orbit UI: live, cached, stale and offline states each render a distinct, labelled banner with ages', () => {
  const states = {};
  states.cached = orbit.evaluate(raw(), sec(10));
  states.stale = orbit.evaluate(raw(), sec(7200));
  states.unconfigured = orbit.fromLoad(orbitFile.load(null));
  states.offline = orbit.fromLoad({ status: 'missing', name: 'x.json' });
  states.invalid = orbit.fromLoad({ status: 'error', name: 'x.json', reason: 'not valid JSON' });
  const banner = (r) => { let hit = null; const walk = (n) => { if (n.attrs && n.attrs.id === 'orbit-state') hit = n; (n.children || []).forEach(walk); }; walk(r.root); return hit; };
  const seen = new Set();
  for (const [name, view] of Object.entries(states)) {
    const r = renderWithFakeDom(view);
    const b = banner(r);
    assert.ok(b, name);
    assert.equal(b.attrs['data-orbit-state'], view.state);
    seen.add(view.state);
    assert.match(r.text, /Orbit: operation and supervisor/);
    assert.match(r.text, /About Orbit/);
    if (view.sections.length) assert.match(r.text, /ago|just now/);                  // an age next to values
    if (name === 'stale') assert.match(r.text, /Stale/);
    if (name === 'offline') assert.match(r.text, /Unavailable/);
    if (!view.sections.length) assert.ok(!/Main session/.test(r.text), `${name}: no data sections when there is no data`);
  }
  assert.equal(seen.size, 5);
  const full = renderWithFakeDom(states.cached).text;
  assert.ok(full.includes("UNAVAILABLE: usage is not OBSERVED (labelled REPORTED)"));               // account usage is not shown
  assert.ok(!full.includes('42'));
  assert.match(full, /Cached/);
  assert.match(full, /Live/);
});
