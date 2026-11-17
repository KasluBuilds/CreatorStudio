'use strict';
// Turns a snapshot of the canonical records into the view model the UI draws. Pure: no I/O, no clock unless passed in.
//
// The rules below are the SAME rules as orchestrator/governance/status.py (implementation credit, verification ladder,
// half-up rounding to one decimal). They are used only for parts and subsets (a milestone, an agent, a phase). The
// project-wide figures shown on the dashboard are the canonical `computed` block of PROJECT_STATUS.json, copied as is;
// the recalculation is compared with them and a mismatch is reported instead of silently replacing them.

const REPO_URL = 'https://github.com/KasluBuilds/CreatorStudio';

// credits in twentieths so every sum is an exact integer (1/4 = 5, 1/2 = 10, 9/10 = 18, 1 = 20)
const IMPL = { NOT_STARTED: 0, IN_PROGRESS: 10, IMPLEMENTED: 20, UNDER_REVIEW: 20, VERIFIED: 20, BLOCKED: 0, DEFERRED: 0, FAILED: 0 };
const VER = { NONE: 0, UNIT_ISOLATED: 5, INTEGRATION: 10, REAL_INPUT_REVIEWED: 18, REPEATED_REAL_USE: 20 };
const LEVELS = ['NONE', 'UNIT_ISOLATED', 'INTEGRATION', 'REAL_INPUT_REVIEWED', 'REPEATED_REAL_USE'];
const VERIFIED_LEVELS = ['REAL_INPUT_REVIEWED', 'REPEATED_REAL_USE'];
const COUNTED_VER_STATES = ['IN_PROGRESS', 'UNDER_REVIEW', 'VERIFIED'];
const IMPLEMENTED_STATES = ['IMPLEMENTED', 'UNDER_REVIEW', 'VERIFIED'];
const STALE_AFTER_MS = 60 * 60 * 1000;

// ---------------------------------------------------------------- helpers
function parseJson(text, name, problems) {
  if (typeof text !== 'string') return null;
  try {
    return JSON.parse(text);
  } catch (err) {
    problems.push(`${name} is not valid JSON (${err.message})`);
    return null;
  }
}

const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const arr = (v) => (Array.isArray(v) ? v : []);
const str = (v, d = '') => (typeof v === 'string' ? v : d);

/** Half-up rounding of numerator/denominator * 100 to one decimal, exact (integers only). */
function pct(num, den) {
  if (!den) return null;
  const tenths = Math.floor((2 * num * 1000 + den * 20) / (2 * den * 20));
  return tenths / 10;
}

function weightedPercent(items) {
  let w = 0; let i = 0; let v = 0;
  for (const m of items) { w += m.weight; i += m.weight * m.implCredit; v += m.weight * m.verCredit; }
  return { weight: w, implementation: pct(i, w), verification: pct(v, w), count: items.length };
}

function commitUrl(sha) { return `${REPO_URL}/commit/${sha}`; }
function isHttps(u) { return typeof u === 'string' && /^https:\/\/[^\s]+$/.test(u); }
function safeLink(url) {
  return isHttps(url) && /^https:\/\/(github\.com\/KasluBuilds\/CreatorStudio|raw\.githubusercontent\.com\/KasluBuilds\/CreatorStudio)(\/|$)/.test(url) ? url : null;
}

// ---------------------------------------------------------------- agents
function normalizeRegistry(reg, status, tasks, problems) {
  const agents = [];
  if (isObj(reg) && Array.isArray(reg.agents)) {
    for (const a of reg.agents) {
      if (!isObj(a) || typeof a.id !== 'string' || !/^[a-z][a-z0-9-]*$/.test(a.id)) { problems.push('AGENT_REGISTRY.json has an entry without a valid id; it was skipped'); continue; }
      agents.push({
        id: a.id, name: str(a.name, a.id), role: str(a.role), status: str(a.status, 'planned'), registered: str(a.registered),
        chat: str(a.chat), streams: arr(a.streams).filter((s) => typeof s === 'string'), packageKeys: arr(a.package_keys),
        milestones: arr(a.milestones).filter((s) => typeof s === 'string'), remainder: a.remainder === true,
        docs: arr(a.docs).filter((s) => typeof s === 'string'), notes: str(a.notes),
      });
    }
    return { agents, source: 'registry' };
  }
  // No registry (older repository state, or the file is unreadable): derive agents from the package owners so tabs still
  // appear, and say so. The derived Main agent answers for every milestone nobody else names (none, here).
  const owners = isObj(tasks) && isObj(tasks.package_owners) ? tasks.package_owners : {};
  const byStream = new Map();
  for (const [key, p] of Object.entries(owners)) {
    if (!isObj(p) || typeof p.owner !== 'string' || p.owner === 'UNRESOLVED') continue;
    const id = key.replace(/_/g, '-');
    const cur = byStream.get(p.owner) || { id, name: key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()), role: '', status: 'active', registered: '', chat: '', streams: [p.owner], packageKeys: [], milestones: [], remainder: false, docs: [], notes: 'Derived from ACTIVE_TASKS.json package owners because AGENT_REGISTRY.json is unavailable.' };
    cur.packageKeys.push(key);
    byStream.set(p.owner, cur);
  }
  agents.push({ id: 'main', name: 'Main', role: 'Everything not owned by a specialist agent', status: 'active', registered: '', chat: '', streams: [], packageKeys: [], milestones: [], remainder: true, docs: [], notes: 'Derived: AGENT_REGISTRY.json is unavailable.' });
  for (const a of byStream.values()) agents.push(a);
  return { agents, source: 'derived' };
}

function agentForMilestone(agents, mid) {
  let rem = null;
  for (const a of agents) {
    if (a.status !== 'active') continue;
    if (a.milestones.includes(mid)) return a.id;
    if (a.remainder) rem = a.id;
  }
  return rem;
}

function agentForStream(agents, owner) {
  let rem = null;
  for (const a of agents) {
    if (a.status !== 'active') continue;
    if (a.streams.includes(owner)) return a.id;
    if (a.remainder) rem = a.id;
  }
  return rem;
}

// ---------------------------------------------------------------- milestones
function buildMilestones(status, evidenceIndex) {
  const out = [];
  for (const m of arr(status && status.milestones)) {
    if (!isObj(m) || typeof m.id !== 'string') continue;
    const impl = isObj(m.implementation) ? m.implementation : {};
    const ver = isObj(m.verification) ? m.verification : {};
    const implState = str(impl.state, 'NOT_STARTED');
    const implCredit = IMPL[implState] !== undefined ? IMPL[implState] : 0;
    const level = str(ver.level, 'NONE');
    const verCounted = COUNTED_VER_STATES.includes(ver.state);
    const verCredit = verCounted && VER[level] !== undefined ? Math.min(VER[level], implCredit) : 0;
    const weight = Number.isInteger(m.weight) && m.weight > 0 ? m.weight : 0;
    const evidenceIds = arr(ver.evidence).filter((e) => typeof e === 'string');
    const evidence = evidenceIds.map((id) => evidenceIndex(id)).filter(Boolean);
    const blockers = arr(m.blockers).filter((b) => typeof b === 'string');
    out.push({
      id: m.id, title: str(m.title, m.id), phase: str(m.phase, 'Unphased'), description: str(m.description), weight,
      implState, implSummary: str(impl.summary), implCredit, verState: str(ver.state, 'NOT_STARTED'), verLevel: level,
      verSummary: str(ver.summary), verCredit, verified: verCounted && VERIFIED_LEVELS.includes(level) && verCredit > 0,
      dependsOn: arr(m.depends_on).filter((d) => typeof d === 'string'), openQuestions: arr(m.open_questions).filter((q) => typeof q === 'string'),
      blockers, commit: /^[0-9a-f]{40}$/.test(m.commit || '') ? m.commit : null, evidence,
      readinessEvidence: isObj(m.readiness_evidence) ? m.readiness_evidence : {},
    });
  }
  return out;
}

function topoOrder(ms) {
  const byId = new Map(ms.map((m) => [m.id, m]));
  const depth = new Map();
  const visiting = new Set();
  function d(id) {
    if (depth.has(id)) return depth.get(id);
    if (visiting.has(id)) return 0;            // a cycle cannot be drawn; the canonical validator rejects it
    visiting.add(id);
    const m = byId.get(id);
    const v = m && m.dependsOn.length ? 1 + Math.max(...m.dependsOn.filter((x) => byId.has(x)).map(d), -1) : 0;
    visiting.delete(id);
    depth.set(id, v);
    return v;
  }
  ms.forEach((m) => { m.depth = d(m.id); });
  return [...ms].sort((a, b) => a.depth - b.depth || a.id.localeCompare(b.id));
}


/** Roadmap position of a phase name: "Phase N" is N, "Before Phase N" is just before it, foundation phases come first. */
function phaseKey(name) {
  let m = /^Before Phase (\d+)/i.exec(name);
  if (m) return Number(m[1]) - 0.5;
  m = /^Phase (\d+)/i.exec(name);
  if (m) return Number(m[1]);
  const foundation = ['governance', 'state foundation'].indexOf(String(name).toLowerCase());
  return foundation >= 0 ? -2 + foundation * 0.1 : -1;        // named foundation phases first, in a fixed order; others just after them
}

/** Kahn's algorithm that always takes the available milestone with the smallest (phase, depth, id). */
function roadmapOrder(ms) {
  const byId = new Map(ms.map((m) => [m.id, m]));
  const done = new Set();
  const out = [];
  const rest = new Set(ms);
  while (rest.size) {
    const ready = [...rest].filter((m) => m.dependsOn.every((d) => !byId.has(d) || done.has(d)));
    const pool = ready.length ? ready : [...rest];          // a cycle (rejected by the canonical validator) must not hang the app
    pool.sort((a, b) => phaseKey(a.phase) - phaseKey(b.phase) || a.depth - b.depth || a.id.localeCompare(b.id));
    const next = pool[0];
    out.push(next); done.add(next.id); rest.delete(next);
  }
  return out;
}

function classify(m, byId) {
  const depsDone = m.dependsOn.every((d) => byId.has(d) && IMPLEMENTED_STATES.includes(byId.get(d).implState));
  m.depsDone = depsDone;
  if (m.verified) m.state = 'verified';
  else if (IMPLEMENTED_STATES.includes(m.implState)) m.state = 'done';
  else if (m.implState === 'BLOCKED' || m.blockers.length) m.state = 'blocked';
  else if (m.implState === 'IN_PROGRESS') m.state = 'active';
  else m.state = depsDone ? 'ready' : 'upcoming';
  return m;
}

function checklist(m, byId) {
  const items = [];
  items.push({ text: 'Work started', done: m.implState !== 'NOT_STARTED' && m.implState !== 'BLOCKED' && m.implState !== 'DEFERRED' });
  items.push({ text: 'Implemented (code exists)', done: IMPLEMENTED_STATES.includes(m.implState) });
  const reached = LEVELS.indexOf(m.verLevel);
  const counted = COUNTED_VER_STATES.includes(m.verState);
  for (const [lvl, label] of [['UNIT_ISOLATED', 'Verified in isolation (unit tests, test_run evidence)'], ['INTEGRATION', 'Verified together with other parts'],
    ['REAL_INPUT_REVIEWED', 'Verified on real input and reviewed by a human'], ['REPEATED_REAL_USE', 'Verified in repeated real use']]) {
    items.push({ text: label, done: counted && reached >= LEVELS.indexOf(lvl) });
  }
  for (const d of m.dependsOn) {
    const dm = byId.get(d);
    items.push({ text: `Depends on ${d}${dm ? ` (${dm.title})` : ''}`, done: !!dm && IMPLEMENTED_STATES.includes(dm.implState), kind: 'dependency' });
  }
  for (const q of m.openQuestions) items.push({ text: `Open question: ${q}`, done: false, kind: 'question' });
  for (const b of m.blockers) items.push({ text: `Blocker: ${b}`, done: false, kind: 'blocker' });
  return { items, done: items.filter((i) => i.done).length, total: items.length };
}

// ---------------------------------------------------------------- evidence
function makeEvidenceIndex(status) {
  const ev = isObj(status && status.evidence) ? status.evidence : {};
  return (id) => {
    const e = ev[id];
    if (!isObj(e)) return { id, type: 'missing', summary: 'Evidence record not found in PROJECT_STATUS.json', links: [] };
    const links = [];
    if (/^[0-9a-f]{40}$/.test(e.commit || '')) links.push({ label: `commit ${e.commit.slice(0, 7)}`, url: commitUrl(e.commit) });
    if (safeLink(e.ref)) links.push({ label: 'link', url: e.ref });
    return { id, type: str(e.type, 'unknown'), date: str(e.date), summary: str(e.summary), command: str(e.command), result: str(e.result), ref: str(e.ref), links };
  };
}

// ---------------------------------------------------------------- tasks, tracker, waves
function buildTasks(tasks, agents, tracker) {
  const stepsByTask = new Map();
  for (const t of arr(tracker && tracker.tasks)) {
    if (!isObj(t) || typeof t.task_id !== 'string') continue;
    const steps = arr(t.steps).filter(isObj);
    stepsByTask.set(t.task_id, {
      total: steps.length, completed: steps.filter((s) => s.status === 'COMPLETED').length,
      working: steps.filter((s) => s.status === 'IN_PROGRESS' || s.status === 'READY_FOR_REVIEW').map((s) => ({ id: s.id, title: str(s.title), status: s.status })),
      steps: steps.map((s) => ({ id: str(s.id), title: str(s.title), status: str(s.status), criteria: arr(s.acceptance_criteria).length,
        evidence: arr(s.evidence).map((e) => ({ type: str(e && e.type), summary: str(e && e.summary), links: ((e && /^[0-9a-f]{40}$/.test(e.commit || '')) ? [{ label: `commit ${e.commit.slice(0, 7)}`, url: commitUrl(e.commit) }] : []).concat(e && safeLink(e.ref) ? [{ label: 'pull request', url: e.ref }] : []) })) })),
    });
  }
  return arr(tasks && tasks.tasks).filter((t) => isObj(t) && typeof t.id === 'string').map((t) => ({
    id: t.id, title: str(t.title), owner: str(t.owner), status: str(t.status), branch: str(t.branch), agent: agentForStream(agents, str(t.owner)),
    leaseExpires: isObj(t.lease) ? str(t.lease.expires) : null, acceptance: arr(t.acceptance_criteria).filter((s) => typeof s === 'string'),
    steps: stepsByTask.get(t.id) || null,
  }));
}

function buildWaves(snapshot, agents, problems) {
  const out = [];
  for (const [id, w] of Object.entries(snapshot.waves || {})) {
    const m = parseJson(w.manifest, `waves/${id}/wave.json`, problems);
    if (!isObj(m)) continue;
    const lanes = arr(m.lanes).filter((l) => isObj(l) && typeof l.id === 'string').map((l) => {
      const reportText = (w.reports || {})[l.id];
      const rep = reportText ? parseJson(reportText, `waves/${id}/lanes/${l.id}/report.json`, problems) : null;
      const pushed = !!(snapshot.laneRefs || {})[`${id}/${l.id}`];
      const reportStatus = isObj(rep) ? str(rep.status, 'INVALID') : null;
      const criteria = arr(l.acceptance_criteria).filter((s) => typeof s === 'string');
      let state = 'not_started';
      if (reportStatus === 'DONE') state = 'done';
      else if (reportStatus === 'BLOCKED') state = 'blocked';
      else if (reportStatus === 'IN_PROGRESS' || pushed) state = 'working';
      return {
        id: l.id, taskId: str(l.task_id), title: str(l.title), owner: str(l.owner), agent: agentForStream(agents, str(l.owner)),
        branch: str(l.branch), milestones: arr(l.milestones).filter((s) => typeof s === 'string'), criteria, reportStatus, state, pushed,
        summary: isObj(rep) ? str(rep.summary) : '', unresolved: isObj(rep) ? arr(rep.unresolved).filter((s) => typeof s === 'string') : [],
        testModules: arr(l.test_modules).filter((s) => typeof s === 'string'),
        url: `${REPO_URL}/tree/${encodeURI(str(l.branch))}`,
      };
    });
    const done = lanes.filter((l) => l.state === 'done').length;
    out.push({ id, title: str(m.title), created: str(m.created), sha: w.sha, lanes, done, total: lanes.length,
      state: lanes.length && done === lanes.length ? 'complete' : lanes.some((l) => l.state !== 'not_started') ? 'running' : 'launched',
      url: `${REPO_URL}/tree/wave/${encodeURI(id)}` });
  }
  return out;
}

// ---------------------------------------------------------------- gate
function buildGate(status, byId, checklistText, milestonesById) {
  const gates = arr(status && status.user_action_gates).filter(isObj);
  return gates.map((g) => {
    const triggers = arr(g.trigger_milestones).filter((s) => typeof s === 'string');
    const fired = triggers.some((t) => byId.has(t) && IMPLEMENTED_STATES.includes(byId.get(t).implState));
    const recorded = str(g.status, 'PENDING');
    let display = 'pending';
    if (recorded === 'ACKNOWLEDGED') display = 'acknowledged';
    else if (fired) display = 'ready';
    const criteria = arr(g.readiness_criteria).filter(isObj).map((c) => {
      const have = triggers.some((t) => { const re = (byId.get(t) || {}).readinessEvidence || {}; return arr(re[c.id]).length > 0 || (re[c.id] && !Array.isArray(re[c.id])); });
      return { id: str(c.id), text: str(c.text), met: have };
    });
    return {
      id: str(g.id), title: str(g.title), message: str(g.message), triggerDescription: str(g.trigger_description), recorded, fired, display,
      inconsistent: (fired && recorded === 'PENDING') || (!fired && recorded !== 'PENDING'),
      triggers: triggers.map((t) => ({ id: t, title: byId.has(t) ? byId.get(t).title : t, state: byId.has(t) ? byId.get(t).implState : 'UNKNOWN',
        dependsOn: byId.has(t) ? byId.get(t).dependsOn.map((d) => ({ id: d, title: byId.has(d) ? byId.get(d).title : d, done: byId.has(d) && IMPLEMENTED_STATES.includes(byId.get(d).implState) })) : [],
        evidence: byId.has(t) ? byId.get(t).evidence : [] })),
      criteria, steps: arr(g.steps).filter((s) => typeof s === 'string'), checklistPath: str(g.checklist),
      checklistText: typeof checklistText === 'string' ? checklistText : null,
      checklistUrl: g.checklist ? `${REPO_URL}/blob/main/${str(g.checklist)}` : null,
      acknowledgedBy: str(g.acknowledged_by), acknowledgedDate: str(g.acknowledged_date),
      readyKey: `${str(g.id)}:${fired ? 'fired' : 'pending'}:${recorded}`,
    };
  });
}

// ---------------------------------------------------------------- main entry
function buildModel(snapshot, opts = {}) {
  const now = opts.now ? opts.now() : new Date();
  const problems = [];
  const files = (snapshot && snapshot.files) || {};
  const status = parseJson(files['PROJECT_STATUS.json'], 'PROJECT_STATUS.json', problems);
  if (!isObj(status) || !Array.isArray(status.milestones)) {
    return { ok: false, problems: problems.concat(['PROJECT_STATUS.json is missing or has no milestones: nothing can be shown']) };
  }
  const tasksDoc = parseJson(files['ACTIVE_TASKS.json'], 'ACTIVE_TASKS.json', problems);
  const tracker = parseJson(files['DEVELOPMENT_TRACKER.json'], 'DEVELOPMENT_TRACKER.json', problems);
  const registryDoc = parseJson(files['AGENT_REGISTRY.json'], 'AGENT_REGISTRY.json', problems);
  if (files['ACTIVE_TASKS.json'] === undefined) problems.push('ACTIVE_TASKS.json was not available: tasks and agent assignments are incomplete');
  if (files['DEVELOPMENT_TRACKER.json'] === undefined) problems.push('DEVELOPMENT_TRACKER.json was not available: step progress is incomplete');

  const evidenceIndex = makeEvidenceIndex(status);
  const ordered = topoOrder(buildMilestones(status, evidenceIndex));
  const byId = new Map(ordered.map((m) => [m.id, m]));
  ordered.forEach((m) => classify(m, byId));
  const { agents: agentDefs, source: agentSource } = normalizeRegistry(registryDoc, status, tasksDoc, problems);
  if (agentSource === 'derived') problems.push('AGENT_REGISTRY.json was not available: agent tabs are derived from package owners');
  const known = new Set(agentDefs.filter((a) => a.status === 'active').map((a) => a.id));
  for (const m of ordered) {
    m.agent = agentForMilestone(agentDefs, m.id);
    m.checklist = checklist(m, byId);
  }
  const tasks = buildTasks(tasksDoc, agentDefs, tracker);
  const waves = buildWaves(snapshot, agentDefs, problems);

  // canonical global figures, copied; recalculation only as a cross-check
  const computed = isObj(status.computed) ? status.computed : {};
  const recomputed = weightedPercent(ordered);
  const global = {
    implementation: typeof computed.implementation_percent === 'number' ? computed.implementation_percent : null,
    verification: typeof computed.verification_percent === 'number' ? computed.verification_percent : null,
    totalWeight: computed.total_weight, milestoneCount: computed.milestone_count, weightsStatus: str(status.weights_status),
    recomputedImplementation: recomputed.implementation, recomputedVerification: recomputed.verification,
  };
  global.consistent = global.implementation === recomputed.implementation && global.verification === recomputed.verification
    && computed.milestone_count === ordered.length && computed.total_weight === recomputed.weight;
  if (global.implementation === null || global.verification === null) problems.push('PROJECT_STATUS.json has no computed progress block');
  else if (!global.consistent) problems.push('The canonical progress figures do not match this app\'s recalculation from the milestones: the data may be mid-update or this app is out of date. The canonical figures are shown.');

  // roadmap order: phases by their roadmap number, milestones by dependency (a milestone never precedes its dependencies)
  const roadmap = roadmapOrder(ordered);
  roadmap.forEach((m, i) => { m.order = i; });
  const phaseMap = new Map();
  for (const m of roadmap) {
    if (!phaseMap.has(m.phase)) phaseMap.set(m.phase, []);
    phaseMap.get(m.phase).push(m);
  }
  const phases = [...phaseMap.entries()].map(([name, ms]) => ({ name, key: phaseKey(name), milestones: ms.map((m) => m.id), stats: weightedPercent(ms) }))
    .sort((a, b) => a.key - b.key || a.name.localeCompare(b.name));

  // agents
  const agents = agentDefs.filter((a) => a.status === 'active').map((a) => {
    const ms = ordered.filter((m) => m.agent === a.id);
    const aTasks = tasks.filter((t) => t.agent === a.id);
    const aLanes = waves.flatMap((w) => w.lanes.filter((l) => l.agent === a.id).map((l) => ({ ...l, waveId: w.id, waveTitle: w.title })));
    const stats = ms.length ? weightedPercent(ms) : { weight: 0, implementation: null, verification: null, count: 0 };
    const current = [
      ...aTasks.filter((t) => t.status === 'ACTIVE' || t.status === 'UNDER_REVIEW').map((t) => ({ kind: 'task', id: t.id, title: t.title, detail: `${t.status}${t.leaseExpires ? `, lease until ${t.leaseExpires}` : ''}`, branch: t.branch })),
      ...aLanes.filter((l) => l.state !== 'done').map((l) => ({ kind: 'lane', id: `${l.waveId}/${l.id}`, title: l.title, detail: `wave lane, ${l.state.replace('_', ' ')}${l.reportStatus ? `, report ${l.reportStatus}` : ''}`, branch: l.branch, url: l.url })),
    ];
    const next = ms.find((m) => m.state === 'ready' || m.state === 'active') || ms.find((m) => m.state === 'upcoming') || null;
    const laneDone = aLanes.reduce((n, l) => n + (l.state === 'done' ? l.criteria.length : 0), 0);
    const laneTotal = aLanes.reduce((n, l) => n + l.criteria.length, 0);
    return {
      id: a.id, name: a.name, role: a.role, chat: a.chat, registered: a.registered, streams: a.streams, notes: a.notes,
      docs: a.docs.map((d) => ({ path: d, url: `${REPO_URL}/blob/main/${d}` })), milestones: ms.map((m) => m.id), stats,
      tracked: ms.length > 0, current, next: next ? { id: next.id, title: next.title, state: next.state } : null,
      tasks: aTasks, lanes: aLanes, criteria: { done: laneDone, total: laneTotal },
      active: current.length > 0,
    };
  });

  // dashboard summaries
  const incomplete = ordered.filter((m) => !IMPLEMENTED_STATES.includes(m.implState));
  const activeMs = ordered.filter((m) => m.state === 'active');
  let major = null;
  let majorWhy = '';
  if (activeMs.length) {
    major = [...activeMs].sort((a, b) => b.weight - a.weight || a.depth - b.depth || a.id.localeCompare(b.id))[0];
    majorWhy = 'The heaviest milestone currently in progress.';
  } else {
    major = incomplete.find((m) => m.state === 'ready') || incomplete[0] || null;
    majorWhy = major ? 'No milestone is in progress; this is the earliest one whose dependencies are done.' : 'Every milestone is implemented.';
  }
  const upcoming = incomplete.filter((m) => m.state === 'ready' && m !== major).slice(0, 6).map((m) => m.id);
  const blockers = [];
  for (const m of ordered) {
    if (m.state === 'blocked') blockers.push({ kind: 'milestone', id: m.id, title: m.title, text: (m.blockers[0] || m.implSummary || 'Blocked'), agent: m.agent });
  }
  for (const d of arr(status.open_decisions).filter((x) => typeof x === 'string')) blockers.push({ kind: 'decision', id: '', title: 'Open decision', text: d });
  for (const w of waves) for (const l of w.lanes) for (const u of l.unresolved) blockers.push({ kind: 'lane', id: `${w.id}/${l.id}`, title: l.title, text: u, agent: l.agent });

  const checklistText = files['AI_Creator_Studio_Documentation/FIRST_REAL_VIDEO_CHECKLIST.md'];
  const gates = buildGate(status, byId, checklistText, byId);

  const fetchedAt = snapshot.fetchedAt;
  const ageMs = Math.max(0, now.getTime() - new Date(fetchedAt).getTime());
  return {
    ok: true,
    problems: problems.concat(arr(snapshot.warnings)),
    sync: { fetchedAt, ageMs, stale: ageMs > STALE_AFTER_MS, commit: snapshot.source.commit, shortCommit: snapshot.source.commit.slice(0, 7), commitUrl: commitUrl(snapshot.source.commit),
      repo: snapshot.source.repo, refsVia: snapshot.source.refsVia, latestRelease: snapshot.latestRelease || null },
    global, weightsNote: str(status.weights_note), baselineDate: isObj(status.baseline) ? str(status.baseline.date) : '',
    milestones: roadmap, phases, agents, agentSource, tasks, waves, gates,
    summary: { major: major ? major.id : null, majorWhy, upcoming, blockers, activeAgents: agents.filter((a) => a.active).map((a) => a.id),
      activeWave: waves.find((w) => w.state !== 'complete') ? waves.find((w) => w.state !== 'complete').id : null },
    knownAgents: [...known],
  };
}

module.exports = { buildModel, phaseKey, roadmapOrder, pct, weightedPercent, IMPL, VER, LEVELS, IMPLEMENTED_STATES, STALE_AFTER_MS, agentForMilestone, agentForStream,
  normalizeRegistry, buildMilestones, topoOrder, classify, checklist, makeEvidenceIndex, parseJson, safeLink };
