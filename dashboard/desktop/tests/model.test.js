'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { repoSnapshot: liveSnapshot, fixtureSnapshot: repoSnapshot, fixtureText, readRepo } = require('./helpers');
const model = require('../src/model');

const NOW = () => new Date('2026-11-17T12:30:00Z');
const build = (snap) => model.buildModel(snap || repoSnapshot(), { now: NOW });
const clone = (o) => JSON.parse(JSON.stringify(o));

function withStatus(mutate, extra = {}) {
  const status = JSON.parse(fixtureText('PROJECT_STATUS.json'));
  mutate(status);
  const snap = repoSnapshot();
  snap.files['PROJECT_STATUS.json'] = JSON.stringify(status);
  Object.assign(snap.files, extra);
  return snap;
}

// ---- progress calculation against the canonical records -------------------------------------------------
test('global figures are the canonical computed block, and the recalculation agrees with them', () => {
  const m = build();
  const status = JSON.parse(fixtureText('PROJECT_STATUS.json'));
  assert.equal(m.ok, true);
  assert.equal(m.global.implementation, status.computed.implementation_percent);
  assert.equal(m.global.verification, status.computed.verification_percent);
  assert.equal(m.global.recomputedImplementation, status.computed.implementation_percent);
  assert.equal(m.global.recomputedVerification, status.computed.verification_percent);
  assert.equal(m.global.consistent, true);
  assert.equal(m.global.milestoneCount, status.milestones.length);
  assert.equal(m.milestones.length, status.milestones.length);
});

test('the app never overwrites the canonical figure: a tampered milestone is reported, the canonical number is still shown', () => {
  const snap = withStatus((s) => { s.milestones.find((x) => x.implementation.state === 'NOT_STARTED').implementation.state = 'IMPLEMENTED'; });
  const m = build(snap);
  const status = JSON.parse(snap.files['PROJECT_STATUS.json']);
  assert.equal(m.global.implementation, status.computed.implementation_percent);        // canonical, untouched
  assert.notEqual(m.global.recomputedImplementation, m.global.implementation);
  assert.equal(m.global.consistent, false);
  assert.ok(m.problems.some((p) => p.includes('do not match')));
});

test('per-milestone credits follow the canonical tables', () => {
  const m = build();
  const by = new Map(m.milestones.map((x) => [x.id, x]));
  assert.equal(by.get('M-MED-01').implCredit, 10);                                       // IN_PROGRESS = 1/2
  assert.equal(by.get('M-DNA-01').implCredit, 20);                                       // IMPLEMENTED = 1
  assert.equal(by.get('M-DNA-01').verCredit, 5);                                         // UNIT_ISOLATED = 1/4
  assert.equal(by.get('M-RSV-02').implCredit, 0);
  for (const x of m.milestones) assert.ok(x.verCredit <= x.implCredit, `${x.id}: verification never exceeds implementation`);
});

test('verification credit counts only in counted states and is capped by implementation', () => {
  const snap = withStatus((s) => {
    const a = s.milestones.find((x) => x.id === 'M-DNA-01');
    a.verification.state = 'NOT_STARTED';                                               // level without a counted state earns nothing
    const b = s.milestones.find((x) => x.id === 'M-MED-01');
    b.verification.state = 'IN_PROGRESS'; b.verification.level = 'REAL_INPUT_REVIEWED'; // capped at the half implementation credit
  });
  const by = new Map(build(snap).milestones.map((x) => [x.id, x]));
  assert.equal(by.get('M-DNA-01').verCredit, 0);
  assert.equal(by.get('M-MED-01').verCredit, 10);
});

test('pct(sum of weight*credit in twentieths, total weight) rounds half up to one decimal, exactly', () => {
  assert.equal(model.pct(1, 3), 1.7);             // 1/60 of full credit = 1.666...% -> 1.7
  assert.equal(model.pct(25, 10), 12.5);
  assert.equal(model.pct(13, 100), 0.7);          // exactly 0.65 rounds half UP
  assert.equal(model.pct(1, 8), 0.6);             // 0.625 -> 0.6 (6.25 tenths rounds down)
  assert.equal(model.pct(0, 5), 0);
  assert.equal(model.pct(20, 1), 100);
  assert.equal(model.pct(1, 0), null);            // no weight: no percentage
});

test('every phase and agent subset uses the same weighting and the union of agents is the whole project', () => {
  const m = build();
  const total = m.agents.reduce((n, a) => n + a.stats.weight, 0);
  assert.equal(total, m.global.totalWeight);
  const implSum = m.agents.reduce((n, a) => n + a.stats.implementation * a.stats.weight, 0) / total;
  assert.ok(Math.abs(implSum - m.global.implementation) < 0.15, 'weighted agent figures reproduce the global figure within rounding');
  assert.equal(m.phases.reduce((n, p) => n + p.milestones.length, 0), m.milestones.length);
});

// ---- ordering ---------------------------------------------------------------------------------------------
test('roadmap order never places a milestone before its dependencies and follows phase numbers', () => {
  const m = build();
  const pos = new Map(m.milestones.map((x, i) => [x.id, i]));
  for (const x of m.milestones) for (const d of x.dependsOn) if (pos.has(d)) assert.ok(pos.get(d) < pos.get(x.id), `${d} before ${x.id}`);
  const names = m.phases.map((p) => p.name);
  assert.ok(names.indexOf('Phase 1') < names.indexOf('Phase 3') && names.indexOf('Phase 3') < names.indexOf('Before Phase 4') && names.indexOf('Before Phase 4') < names.indexOf('Phase 4'));
  assert.equal(model.phaseKey('Before Phase 4'), 3.5);
  assert.equal(model.phaseKey('Phase 7'), 7);
  assert.ok(model.phaseKey('Governance') < model.phaseKey('State foundation') && model.phaseKey('State foundation') < model.phaseKey('Phase 0'));
  assert.equal(model.phaseKey('Something else'), -1);
});

test('each phase appears as one contiguous block on the road (no repeated signposts) in the real data', () => {
  const m = build();
  const seen = []; let prev = null;
  for (const x of m.milestones) { if (x.phase !== prev) { assert.ok(!seen.includes(x.phase), `${x.phase} is split`); seen.push(x.phase); prev = x.phase; } }
});

test('a dependency cycle cannot hang the app', () => {
  const ms = [{ id: 'A', phase: 'Phase 1', depth: 0, dependsOn: ['B'] }, { id: 'B', phase: 'Phase 1', depth: 0, dependsOn: ['A'] }];
  assert.equal(model.roadmapOrder(ms).length, 2);
});

// ---- states, checklists, evidence ---------------------------------------------------------------------------
test('milestone states and checklist counts come from the records, never invented', () => {
  const m = build();
  const by = new Map(m.milestones.map((x) => [x.id, x]));
  assert.equal(by.get('M-MED-01').state, 'active');
  assert.equal(by.get('M-DNA-01').state, 'done');
  assert.equal(by.get('M-MED-02').state, 'blocked');
  assert.ok(['ready', 'upcoming'].includes(by.get('M-RSV-02').state));
  const c = by.get('M-DNA-01').checklist;
  assert.equal(c.done, c.items.filter((i) => i.done).length);
  assert.ok(c.items.some((i) => i.text.startsWith('Verified in isolation') && i.done));
  assert.ok(by.get('M-MED-02').checklist.items.some((i) => i.kind === 'blocker' && !i.done));
});

test('evidence links are real GitHub commit links and external links are filtered', () => {
  const m = build();
  const withCommit = m.milestones.flatMap((x) => x.evidence).find((e) => e.links.some((l) => /\/commit\/[0-9a-f]{40}$/.test(l.url)));
  assert.ok(withCommit);
  assert.equal(model.safeLink('https://evil.example/x'), null);
  assert.equal(model.safeLink('http://github.com/KasluBuilds/CreatorStudio/x'), null);
  assert.ok(model.safeLink('https://github.com/KasluBuilds/CreatorStudio/pull/1'));
  const snap = withStatus((s) => { s.evidence['E-OBS-REMOTE-0001'].ref = 'https://evil.example/phish'; });
  const e = build(snap).milestones.flatMap((x) => x.evidence).find((x) => x.id === 'E-OBS-REMOTE-0001');
  assert.ok(e && !e.links.some((l) => l.url.includes('evil')));
});

// ---- agents -------------------------------------------------------------------------------------------------
test('agent tabs come from AGENT_REGISTRY.json and a new active agent appears without code changes', () => {
  const base = build();
  assert.deepEqual(base.agents.map((a) => a.id), ['main', 'creative-director', 'creator-dna', 'research', 'assets']);
  const reg = JSON.parse(fixtureText('AGENT_REGISTRY.json'));
  reg.agents.push({ id: 'editor', name: 'Editor', role: 'Plans edits', status: 'active', registered: '2026-12-01', streams: ['claude:editor-stream'], package_keys: [], milestones: ['M-EDT-01'], docs: [] });
  const snap = repoSnapshot(); snap.files['AGENT_REGISTRY.json'] = JSON.stringify(reg);
  const m = build(snap);
  assert.deepEqual(m.agents.map((a) => a.id), ['main', 'creative-director', 'creator-dna', 'research', 'assets', 'editor']);
  const editor = m.agents.find((a) => a.id === 'editor');
  assert.deepEqual(editor.milestones, ['M-EDT-01']);
  assert.equal(m.agents.find((a) => a.id === 'main').milestones.includes('M-EDT-01'), false, 'a milestone moves from the remainder agent to its new owner');
});

test('planned and retired agents get no tab until they are active', () => {
  const reg = JSON.parse(fixtureText('AGENT_REGISTRY.json'));
  reg.agents.push({ id: 'qc', name: 'QC', role: 'Quality control', status: 'planned', registered: '2026-12-01', streams: ['claude:qc'], package_keys: [], milestones: [], docs: [] });
  reg.agents.push({ id: 'old', name: 'Old', role: 'x', status: 'retired', registered: '2026-12-01', streams: [], package_keys: [], milestones: [], docs: [] });
  const snap = repoSnapshot(); snap.files['AGENT_REGISTRY.json'] = JSON.stringify(reg);
  assert.equal(build(snap).agents.some((a) => a.id === 'qc' || a.id === 'old'), false);
});

test('an agent without milestones is untracked, with checklist counts instead of a percentage', () => {
  const reg = JSON.parse(fixtureText('AGENT_REGISTRY.json'));
  reg.agents.push({ id: 'newbie', name: 'Newbie', role: 'x', status: 'active', registered: '2026-12-01', streams: ['claude:newbie'], package_keys: [], milestones: [], docs: [] });
  const snap = repoSnapshot(); snap.files['AGENT_REGISTRY.json'] = JSON.stringify(reg);
  const a = build(snap).agents.find((x) => x.id === 'newbie');
  assert.equal(a.tracked, false);
  assert.equal(a.stats.implementation, null);
  assert.deepEqual(a.criteria, { done: 0, total: 0 });
});

test('without a registry the tabs are derived from package owners and the data note says so', () => {
  const snap = repoSnapshot(); delete snap.files['AGENT_REGISTRY.json'];
  const m = build(snap);
  assert.equal(m.agentSource, 'derived');
  assert.ok(m.agents.length >= 3);
  assert.ok(m.problems.some((p) => p.includes('AGENT_REGISTRY.json')));
  assert.equal(m.agents.reduce((n, a) => n + a.milestones.length, 0), m.milestones.length);
});

test('tasks and lanes are attributed to the agent that registers their stream', () => {
  const m = build();
  const crd = m.agents.find((a) => a.id === 'creative-director');
  assert.ok(crd.tasks.some((t) => t.id === 'T-CRD-001'));
  assert.ok(m.agents.find((a) => a.id === 'assets').tasks.some((t) => t.id === 'T-AST-002'), 'asset-recovery is a registered asset stream');
  assert.equal(m.agents.find((a) => a.id === 'main').tasks.some((t) => t.id === 'T-GOV-004'), true);
});

// ---- waves --------------------------------------------------------------------------------------------------
function waveSnapshot(reports = {}, lanePushed = {}) {
  const manifest = { schema_version: 1, wave_id: 'w01-demo', title: 'Demo wave', created: '2026-11-17', lanes: [
    { id: 'L1', task_id: 'T-AST-003', title: 'Acquisition', owner: 'claude:asset-recovery', branch: 'lane/w01-demo/L1', milestones: ['M-AST-02'], acceptance_criteria: ['a', 'b', 'c'], test_modules: ['test_x.py'] },
    { id: 'L2', task_id: 'T-RSR-001', title: 'Providers', owner: 'claude:research-stream', branch: 'lane/w01-demo/L2', acceptance_criteria: ['a'], test_modules: ['test_y.py'] }] };
  const snap = repoSnapshot();
  snap.waves = { 'w01-demo': { sha: 'b'.repeat(40), manifest: JSON.stringify(manifest), reports } };
  snap.laneRefs = lanePushed;
  return snap;
}

test('wave lanes: not started, working, done, blocked, and unresolved items surface as blockers', () => {
  const m = build(waveSnapshot({ L1: JSON.stringify({ lane: 'L1', status: 'DONE', summary: 'ok', unresolved: ['Need a decision about X'] }), L2: JSON.stringify({ lane: 'L2', status: 'BLOCKED', summary: 'b', unresolved: [] }) },
    { 'w01-demo/L1': 'c'.repeat(40) }));
  const w = m.waves[0];
  assert.equal(w.id, 'w01-demo');
  assert.deepEqual(w.lanes.map((l) => l.state), ['done', 'blocked']);
  assert.equal(w.done, 1);
  assert.equal(w.state, 'running');
  assert.ok(m.summary.blockers.some((b) => b.kind === 'lane' && b.text.includes('decision about X')));
  assert.equal(m.summary.activeWave, 'w01-demo');
  const assets = m.agents.find((a) => a.id === 'assets');
  assert.equal(assets.lanes.length, 1);
  assert.deepEqual(assets.criteria, { done: 3, total: 3 });
});

test('a lane that has pushed a branch is working; one that has not is not started', () => {
  const m = build(waveSnapshot({}, { 'w01-demo/L1': 'c'.repeat(40) }));
  assert.deepEqual(m.waves[0].lanes.map((l) => l.state), ['working', 'not_started']);
  assert.equal(m.waves[0].state, 'running');
  assert.equal(build(waveSnapshot()).waves[0].state, 'launched');
});

test('a malformed lane report or manifest is reported, never thrown', () => {
  const snap = waveSnapshot({ L1: '{not json' });
  const m = build(snap);
  assert.ok(m.problems.some((p) => p.includes('report.json is not valid JSON')));
  snap.waves['w01-demo'].manifest = 'garbage';
  assert.doesNotThrow(() => build(snap));
});

test('the milestone detail can link a lane through the optional milestones field', () => {
  const m = build(waveSnapshot());
  assert.deepEqual(m.waves[0].lanes[0].milestones, ['M-AST-02']);
});

// ---- the filming gate ---------------------------------------------------------------------------------------
test('gate is pending until the canonical trigger fires, then ready', () => {
  const pending = build().gates[0];
  assert.equal(pending.id, 'G-FIRST-REAL-VIDEO');
  assert.equal(pending.display, 'pending');
  assert.equal(pending.fired, false);
  assert.equal(pending.inconsistent, false);
  assert.equal(pending.criteria.length, 5);
  assert.ok(pending.checklistText && pending.checklistText.length > 100);
  assert.deepEqual(pending.triggers.map((t) => t.id), ['M-LFE-03']);

  const fired = withStatus((s) => {
    s.milestones.find((x) => x.id === 'M-LFE-03').implementation.state = 'IMPLEMENTED';
    s.user_action_gates[0].status = 'TRIGGERED';
  });
  const g = build(fired).gates[0];
  assert.equal(g.fired, true);
  assert.equal(g.display, 'ready');
  assert.equal(g.inconsistent, false);
});

test('gate: fired but still PENDING in the record is flagged as inconsistent; acknowledged is acknowledged', () => {
  const g = build(withStatus((s) => { s.milestones.find((x) => x.id === 'M-LFE-03').implementation.state = 'UNDER_REVIEW'; })).gates[0];
  assert.equal(g.display, 'ready');
  assert.equal(g.inconsistent, true);
  const ack = build(withStatus((s) => {
    s.milestones.find((x) => x.id === 'M-LFE-03').implementation.state = 'IMPLEMENTED';
    Object.assign(s.user_action_gates[0], { status: 'ACKNOWLEDGED', acknowledged_by: 'Lukas', acknowledged_date: '2026-12-01' });
  })).gates[0];
  assert.equal(ack.display, 'acknowledged');
  assert.equal(ack.acknowledgedBy, 'Lukas');
});

test('a long-form assembly milestone alone does not fire the gate', () => {
  const g = build(withStatus((s) => { s.milestones.find((x) => x.id === 'M-LFE-01').implementation.state = 'IMPLEMENTED'; })).gates[0];
  assert.equal(g.display, 'pending');
});

test('readiness criteria are marked met only from milestone readiness evidence', () => {
  const g = build(withStatus((s) => { s.milestones.find((x) => x.id === 'M-LFE-03').readiness_evidence = { 'RC-1': ['E-X'], 'RC-2': [] }; })).gates[0];
  assert.deepEqual(g.criteria.map((c) => c.met), [true, false, false, false, false]);
});

// ---- missing and malformed data ------------------------------------------------------------------------------
test('missing or malformed PROJECT_STATUS.json yields a clear failure, not an exception', () => {
  for (const text of [undefined, '', '{', '[]', '{"milestones": 3}']) {
    const snap = repoSnapshot(); if (text === undefined) delete snap.files['PROJECT_STATUS.json']; else snap.files['PROJECT_STATUS.json'] = text;
    const m = build(snap);
    assert.equal(m.ok, false);
    assert.ok(m.problems.length > 0);
  }
});

test('missing optional files degrade with notes', () => {
  const snap = repoSnapshot();
  delete snap.files['ACTIVE_TASKS.json']; delete snap.files['DEVELOPMENT_TRACKER.json']; delete snap.files['AI_Creator_Studio_Documentation/FIRST_REAL_VIDEO_CHECKLIST.md'];
  const m = build(snap);
  assert.equal(m.ok, true);
  assert.deepEqual(m.tasks, []);
  assert.equal(m.gates[0].checklistText, null);
  assert.ok(m.problems.some((p) => p.includes('ACTIVE_TASKS.json')));
  assert.ok(m.problems.some((p) => p.includes('DEVELOPMENT_TRACKER.json')));
});

test('malformed optional files are reported and skipped', () => {
  const snap = repoSnapshot(); snap.files['ACTIVE_TASKS.json'] = '{oops'; snap.files['AGENT_REGISTRY.json'] = '[1,2';
  const m = build(snap);
  assert.equal(m.ok, true);
  assert.ok(m.problems.some((p) => p.includes('ACTIVE_TASKS.json is not valid JSON')));
  assert.equal(m.agentSource, 'derived');
});

test('hostile values are inert data: odd types never throw', () => {
  const snap = withStatus((s) => {
    s.milestones[0].title = { x: 1 }; s.milestones[1].depends_on = 'nope'; s.milestones[2].weight = 'heavy'; s.milestones[3].blockers = [1, 2];
    s.open_decisions = [null, 5, '<img src=x onerror=alert(1)>']; s.evidence = null;
  });
  assert.doesNotThrow(() => build(snap));
});

// ---- staleness -----------------------------------------------------------------------------------------------
test('data older than an hour is marked stale', () => {
  const fresh = model.buildModel(repoSnapshot({ fetchedAt: '2026-11-17T12:10:00.000Z' }), { now: NOW });
  const old = model.buildModel(repoSnapshot({ fetchedAt: '2026-11-17T09:00:00.000Z' }), { now: NOW });
  assert.equal(fresh.sync.stale, false);
  assert.equal(old.sync.stale, true);
  assert.equal(fresh.sync.shortCommit.length, 7);
  assert.match(fresh.sync.commitUrl, /^https:\/\/github\.com\/KasluBuilds\/CreatorStudio\/commit\/[0-9a-f]{40}$/);
});

test('summary: a major milestone, upcoming list, blockers and open decisions are derived from the records', () => {
  const m = build();
  assert.equal(m.summary.major, 'M-MED-01');
  assert.ok(m.summary.upcoming.length > 0 && m.summary.upcoming.every((id) => m.milestones.find((x) => x.id === id).state === 'ready'));
  assert.ok(m.summary.blockers.some((b) => b.kind === 'milestone' && b.id === 'M-MED-02'));
  assert.ok(m.summary.blockers.some((b) => b.kind === 'decision'));
});

// ---- invariants that must hold on the repository's CURRENT canonical files at every commit -------------------
test('LIVE: the app agrees with the current canonical progress and ordering', () => {
  const m = model.buildModel(liveSnapshot(), { now: NOW });
  const status = JSON.parse(readRepo('PROJECT_STATUS.json'));
  assert.equal(m.ok, true);
  assert.equal(m.global.consistent, true, 'the recalculation matches PROJECT_STATUS.json computed');
  assert.equal(m.global.implementation, status.computed.implementation_percent);
  assert.equal(m.global.verification, status.computed.verification_percent);
  const pos = new Map(m.milestones.map((x, i) => [x.id, i]));
  for (const x of m.milestones) for (const d of x.dependsOn) if (pos.has(d)) assert.ok(pos.get(d) < pos.get(x.id));
  assert.ok(m.agents.length >= 1 && m.agents.every((a) => a.id));
  assert.equal(m.milestones.every((x) => x.agent), true, 'every milestone has a responsible agent');
  assert.equal(m.agents.reduce((n, a) => n + a.milestones.length, 0), m.milestones.length);
  assert.equal(m.gates.length >= 1, true);
});
