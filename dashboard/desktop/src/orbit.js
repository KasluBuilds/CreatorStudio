'use strict';
// Orbit (the supervisor/operation view): turns a snapshot JSON, written by orchestrator/dev_supervisor/snapshot.py, into a
// view model. Pure functions only: no file, network or process access, nothing is written anywhere. Ages are recomputed from
// each value's own timestamp at display time, so a value that was "live" when the file was written is shown as "cached" or
// "stale" when it is read later. Missing, unreadable or invalid data is an explicit state, never an empty success view.
const FRESHNESS = Object.freeze(['live', 'cached', 'stale', 'offline']);       // best to worst
const LABELS = Object.freeze(['OBSERVED', 'REPORTED', 'INFERRED', 'UNAVAILABLE']);
const DEFAULT_LIVE_S = 120;
const DEFAULT_STALE_S = 900;
const MAX_TEXT = 160;
const TITLE = 'Orbit: operation and supervisor';

function ageText(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return 'age unknown';
  const s = Math.max(0, Math.round(seconds));
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

function text(value) {
  if (value === null || value === undefined) return '';
  const t = typeof value === 'object' ? JSON.stringify(value) : String(value);
  return t.length > MAX_TEXT ? `${t.slice(0, MAX_TEXT - 1)}…` : t;
}

function thresholds(raw) {
  const t = (raw && raw.thresholds) || {};
  const num = (v, d) => (typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : d);
  return { live: num(t.live_after_seconds, DEFAULT_LIVE_S), stale: num(t.stale_after_seconds, DEFAULT_STALE_S) };
}

const UNAVAILABLE_VIEW = (reason) => ({ label: 'UNAVAILABLE', freshness: 'offline', quality: 'unavailable', value: null, text: 'unavailable', reason, ageText: 'age unknown', ageSeconds: null });

/** Data-quality badge: live | cached | stale | estimated (label INFERRED) | unavailable. One per value. */
const QUALITY = Object.freeze(['live', 'cached', 'stale', 'estimated', 'unavailable']);
const qualityOf = (label, freshness) => (label === 'UNAVAILABLE' || freshness === 'offline' ? 'unavailable' : (label === 'INFERRED' && freshness !== 'stale' ? 'estimated' : freshness));

/** One snapshot field -> display model. `opts.observedOnly` applies the account-usage rule. */
function fieldView(f, nowMs, thr, opts = {}) {
  if (!f || typeof f !== 'object' || !LABELS.includes(f.label)) return UNAVAILABLE_VIEW('missing or malformed value');
  let label = f.label;
  let reason = typeof f.reason === 'string' ? f.reason : '';
  let value = f.value === undefined ? null : f.value;
  if (opts.observedOnly && label !== 'OBSERVED' && label !== 'UNAVAILABLE') {
    reason = `labelled ${label}, not OBSERVED; account usage is shown only when OBSERVED`;
    label = 'UNAVAILABLE';
    value = null;
  }
  if (label === 'UNAVAILABLE' || value === null) return UNAVAILABLE_VIEW(reason || 'no source');
  const t = Date.parse(f.timestamp);
  let freshness; let ageSeconds = null;
  if (!Number.isFinite(t)) freshness = 'stale';
  else {
    ageSeconds = Math.max(0, (nowMs - t) / 1000);
    freshness = ageSeconds > thr.stale ? 'stale' : (ageSeconds > thr.live || f.freshness === 'cached' ? 'cached' : 'live');
  }
  return { label, freshness, quality: qualityOf(label, freshness), value, text: text(value), reason, ageText: ageText(ageSeconds), ageSeconds, source: text(f.source) };
}

const worst = (list) => FRESHNESS[Math.max(0, ...list.map((x) => FRESHNESS.indexOf(x)))];
const arr = (x) => (Array.isArray(x) ? x : []);
const named = (obj, names, nowMs, thr, prefix = '') => names.filter((n) => obj && obj[n] !== undefined)
  .map((n) => ({ name: prefix + n.replace(/_/g, ' '), ...fieldView(obj[n], nowMs, thr) }));
const row = (name, f, nowMs, thr, opts) => ({ name, ...fieldView(f, nowMs, thr, opts) });

const pad2 = (n) => String(n).padStart(2, '0');
const utcClock = (ms) => { const d = new Date(ms); return `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())} UTC`; };

/** Relative phrase for a future time ("in 1 h 3 min"); a time at or before nowMs is "window already reset". Deterministic. */
function relativeReset(resetMs, nowMs) {
  const mins = Math.floor((resetMs - nowMs) / 60000);
  if (resetMs <= nowMs) return 'window already reset';
  if (mins < 1) return 'in under 1 min';
  const d = Math.floor(mins / 1440); const h = Math.floor((mins % 1440) / 60); const m = mins % 60;
  if (d > 0) return `in ${d} d${h ? ` ${h} h` : ''}`;
  if (h > 0) return `in ${h} h${m ? ` ${m} min` : ''}`;
  return `in ${m} min`;
}

const USAGE_KEYS = Object.freeze({
  five_hour_used_percentage: ['5-hour window: used', 'percent'],
  five_hour_resets_at: ['5-hour window: resets', 'reset'],
  five_hour_status: ['5-hour window: status', 'plain'],
  weekly_used_percentage: ['7-day window: used', 'percent'],
  weekly_resets_at: ['7-day window: resets', 'reset'],
  weekly_status: ['7-day window: status', 'plain'],
  status: ['rate-limit status', 'plain'],
});

/** Readable account-usage row. Unknown keys keep the generic rendering. Only a value that survived the OBSERVED-only rule is formatted. */
function usageRow(key, f, nowMs, thr) {
  const base = row(key.replace(/_/g, ' '), f, nowMs, thr, { observedOnly: true });
  const known = USAGE_KEYS[key];
  if (!known) return base;
  const out = { ...base, name: known[0] };
  if (base.label === 'UNAVAILABLE') return out;
  if (known[1] === 'percent' && typeof base.value === 'number' && Number.isFinite(base.value)) {
    out.text = `${Math.round(base.value * 10) / 10}%`;
  } else if (known[1] === 'reset') {
    const t = Date.parse(base.value);
    if (typeof base.value === 'string' && Number.isFinite(t)) out.text = `${utcClock(t)} (${relativeReset(t, nowMs)})`;
  }
  return out;
}

/** Plain-language source summary for the account-usage section. Never includes worker ids or file names. */
function usageProvenance(au, rows, nowMs) {
  const provider = text(au.provider) || 'none';
  const live = rows.filter((r) => r.label !== 'UNAVAILABLE' && r.source);
  if (!live.length) return `provider: ${provider}. No usage value is available, so there is no source or age to show.`;
  const src = live[0].source;
  const where = /^rate_limit_event/.test(src) ? "from the last worker run's rate_limit_event" : `from ${src.replace(/[\/].*$/, '')}`;
  const ages = live.map((r) => r.ageSeconds).filter((a) => a !== null);
  const age = ages.length ? `observed ${ageText(Math.min(...ages))}` : 'age unknown';
  return `provider: ${provider}. ${where}; ${age}.`;
}

function invalid(message) {
  return { state: 'invalid', title: TITLE, message, sections: [], generatedAtText: 'unknown' };
}

/**
 * raw: parsed snapshot JSON. Returns { state, title, message, generatedAtText, sections:[{id,title,rows,items,freshness,empty}] }.
 * state: live | cached | stale | offline | invalid.
 */
function evaluate(raw, nowMs = Date.now()) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw) || raw.schema_version !== 1 || raw.product !== 'Orbit' || raw.read_only !== true) {
    return invalid('The file is not an Orbit snapshot (schema_version 1, read_only).');
  }
  const gen = Date.parse(raw.generated_at);
  if (!Number.isFinite(gen)) return invalid('The snapshot has no valid generated_at time, so its age cannot be shown.');
  const thr = thresholds(raw);
  const fileAge = Math.max(0, (nowMs - gen) / 1000);
  const sections = [];
  const add = (s) => { sections.push({ items: [], rows: [], ...s }); };

  const m = raw.main || {};
  add({ id: 'main', title: 'Main session',
    rows: m.present === false ? [{ name: 'main session', ...UNAVAILABLE_VIEW(text(m.reason) || 'not present') }]
      : [...named(m, ['status', 'alive', 'updated_at', 'started_at', 'model'], nowMs, thr),
        ...named(m.context_pressure || {}, ['tokens', 'window', 'ratio', 'level'], nowMs, thr, 'context ')] });
  add({ id: 'model', title: 'Model and provider', rows: named(raw.model_provider || {}, ['model'], nowMs, thr),
    items: arr((raw.model_provider || {}).providers).map((p) => ({ text: `${text(p.provider)} / ${text(p.model) || 'model not recorded'}` })) });
  const au = raw.account_usage || {};
  const usage = au.usage;
  const usageRows = usage && usage.label ? [row('account usage', usage, nowMs, thr, { observedOnly: true })]
    : Object.keys(usage || {}).map((k) => usageRow(k, usage[k], nowMs, thr));
  const accountRows = usageRows.length ? usageRows : [row('account usage', null, nowMs, thr)];
  add({ id: 'account', title: 'Account usage', rows: accountRows, note: usageProvenance(au, accountRows, nowMs) });
  add({ id: 'workers', title: 'Workers', items: arr(raw.workers).map((w) => ({
    text: `${text(w.worker_id)} (${text(w.role)}) on ${text(w.task_id)}: restarts ${Number(w.restart_count) || 0}, failures ${Number(w.failure_count) || 0}`,
    status: fieldView(w.status, nowMs, thr), model: fieldView(w.model, nowMs, thr), branch: fieldView(w.branch, nowMs, thr) })),
  empty: raw.registry && raw.registry.present === false ? `No worker registry: ${text(raw.registry.reason)}` : 'No workers recorded.' });
  add({ id: 'tasks', title: 'Tasks', items: arr(raw.tasks).map((t) => ({
    text: `${text(t.task_id)}: workers ${arr(t.workers).map(text).join(', ') || 'none'}; results ${arr(t.results).map((r) => `${text(r.outcome)} (${text(r.status)}${r.contradiction ? ', contradiction' : ''})`).join('; ') || 'none'}` })),
  empty: 'No tasks recorded.' });
  add({ id: 'git', title: 'Branch, pull request and last GitHub checkpoint', rows: [
    row('branch', raw.branch, nowMs, thr), row('pull request', raw.pull_request, nowMs, thr), row('last GitHub checkpoint', raw.last_github_checkpoint, nowMs, thr)] });
  add({ id: 'blockers', title: 'Blockers', items: arr(raw.blockers).map((b) => ({ text: text(b && typeof b === 'object' ? (b.reason || b.worker_id || b) : b) })), empty: 'No blockers recorded.' });
  add({ id: 'failures', title: 'Failures', items: arr(raw.failures).map((f) => ({ text: `${text(f.kind)} ${text(f.worker_id)} ${text(f.detail && f.detail.value)}`.trim() })), empty: 'No failures recorded.' });
  add({ id: 'retries', title: 'Retries', items: arr(raw.retries).map((f) => ({ text: `${text(f.kind)} ${text(f.worker_id)}`.trim() })), empty: 'No retries recorded.' });
  add({ id: 'next', title: 'Next actions', items: arr(raw.next_actions).map((a) => ({ text: text(a) })), empty: 'No next actions listed.' });

  const seen = [];
  for (const s of sections) {
    const own = [...s.rows.map((r) => r.freshness), ...s.items.filter((i) => i.status).map((i) => i.status.freshness)];
    s.freshness = own.length ? worst(own) : null;
    seen.push(...own);
  }
  const fileFresh = fileAge > thr.stale ? 'stale' : (fileAge > thr.live ? 'cached' : 'live');
  const allOffline = seen.length > 0 && seen.every((f) => f === 'offline');
  const someOffline = seen.includes('offline');
  // A single unavailable value is shown on its own row; it does not make the whole view "offline" unless nothing answered.
  const state = allOffline ? 'offline' : worst([fileFresh, ...seen.filter((f) => f !== 'offline'), ...(someOffline ? ['cached'] : [])]);
  const messages = {
    live: 'Live: every available value was read recently from its source.',
    cached: 'Cached: some values come from saved records or are older than the live window.',
    stale: `Stale: the snapshot or some values are older than ${Math.round(thr.stale / 60)} min. Do not rely on them.`,
    offline: 'Offline: no source supplied any value. The supervisor or its sources are not reachable.',
  };
  return { state, title: TITLE, message: messages[state], generatedAt: raw.generated_at, generatedAtText: ageText(fileAge), fileAgeSeconds: fileAge, sections };
}

/** Result of reading the file (see orbit-file.js) -> view model. Every failure is an explicit state. */
function fromLoad(load, nowMs = Date.now()) {
  if (!load || load.status === 'unconfigured') {
    return { state: 'unconfigured', title: TITLE, message: 'No snapshot file is selected. Choose the Orbit snapshot JSON written by the supervisor. Nothing is shown until you do.', sections: [] };
  }
  if (load.status === 'missing') return { state: 'offline', title: TITLE, message: `Offline: the snapshot file is not there (${load.name || 'unknown file'}). The supervisor may not be running.`, sections: [], fileName: load.name };
  if (load.status === 'error') return { ...invalid(`The snapshot file could not be read: ${load.reason}`), fileName: load.name };
  return { ...evaluate(load.data, nowMs), fileName: load.name };
}

module.exports = { evaluate, fromLoad, fieldView, ageText, relativeReset, FRESHNESS, QUALITY, LABELS, thresholds, TITLE };
