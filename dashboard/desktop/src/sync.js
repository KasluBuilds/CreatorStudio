'use strict';
// Builds one consistent snapshot of the canonical records from GitHub. Everything is pinned to the commit SHA that main
// had when the refresh started. Reads only; never writes to the repository.
const github = require('./github');

const REQUIRED = ['PROJECT_STATUS.json'];
const OPTIONAL = ['ACTIVE_TASKS.json', 'DEVELOPMENT_TRACKER.json', 'AGENT_REGISTRY.json',
  'AI_Creator_Studio_Documentation/FIRST_REAL_VIDEO_CHECKLIST.md', 'SESSION_HANDOFF.md'];
const MAX_WAVES = 8;
const MAX_LANES = 50;
const SNAPSHOT_SCHEMA = 1;

class SyncError extends Error {
  constructor(message, { offline = false, cause = null } = {}) {
    super(message);
    this.name = 'SyncError';
    this.offline = offline;
    this.cause = cause;
  }
}

function isJson(text) {
  try { JSON.parse(text); return true; } catch (e) { return false; }
}

async function mapLimit(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i], i);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

/** Wave branches on GitHub: [{ id, sha }] from the ref list. */
function waveRefs(refs) {
  return Object.keys(refs).filter((r) => /^refs\/heads\/wave\/w\d{2,3}-[a-z0-9][a-z0-9-]*$/.test(r))
    .map((r) => ({ id: r.slice('refs/heads/wave/'.length), sha: refs[r] })).sort((a, b) => a.id.localeCompare(b.id)).slice(0, MAX_WAVES);
}

function laneRefs(refs) {
  const out = {};
  for (const [name, sha] of Object.entries(refs)) {
    const m = /^refs\/heads\/lane\/(w\d{2,3}-[a-z0-9][a-z0-9-]*)\/(L\d{1,2})$/.exec(name);
    if (m) out[`${m[1]}/${m[2]}`] = sha;
  }
  return out;
}

async function syncFromGitHub(fetchFn, { now = () => new Date() } = {}) {
  const warnings = [];
  let refsResult;
  try {
    refsResult = await github.fetchRefs(fetchFn);
  } catch (err) {
    throw new SyncError(`Could not reach GitHub: ${err.message}`, { offline: !!err.offline, cause: err });
  }
  const main = refsResult.refs[`refs/heads/${github.REPO.branch}`];
  if (!main) throw new SyncError('GitHub did not list the main branch');
  const files = {};
  async function get(path, required) {
    let text;
    try {
      text = await github.fetchFile(fetchFn, main, path);
    } catch (err) {
      if (required) throw new SyncError(`Could not read ${path}: ${err.message}`, { offline: !!err.offline, cause: err });
      warnings.push(`Could not read ${path}: ${err.message}`);
      return;
    }
    if (text === null) {
      if (required) throw new SyncError(`${path} does not exist at ${main.slice(0, 7)}`);
      warnings.push(`${path} does not exist in the repository`);
      return;
    }
    files[path] = text;
  }
  await mapLimit([...REQUIRED.map((p) => [p, true]), ...OPTIONAL.map((p) => [p, false])], 4, ([p, req]) => get(p, req));

  // wave branches: manifest and lane reports at the wave branch's own commit
  const waves = {};
  for (const w of waveRefs(refsResult.refs)) {
    const mpath = `waves/${w.id}/wave.json`;
    let manifestText = null;
    try { manifestText = await github.fetchFile(fetchFn, w.sha, mpath); } catch (err) { warnings.push(`Wave ${w.id}: ${err.message}`); }
    if (manifestText === null || !isJson(manifestText)) {
      warnings.push(`Wave ${w.id}: no readable manifest on its branch`);
      continue;
    }
    const manifest = JSON.parse(manifestText);
    const lanes = Array.isArray(manifest.lanes) ? manifest.lanes.slice(0, MAX_LANES) : [];
    const reports = {};
    await mapLimit(lanes, 4, async (lane) => {
      if (!lane || !/^L\d{1,2}$/.test(lane.id || '')) return;
      try {
        const t = await github.fetchFile(fetchFn, w.sha, `waves/${w.id}/lanes/${lane.id}/report.json`);
        if (t !== null) reports[lane.id] = t;
      } catch (err) { warnings.push(`Wave ${w.id} lane ${lane.id}: ${err.message}`); }
    });
    waves[w.id] = { sha: w.sha, manifest: manifestText, reports };
  }

  const release = await github.fetchLatestRelease(fetchFn);
  return {
    schema: SNAPSHOT_SCHEMA,
    fetchedAt: now().toISOString(),
    source: { commit: main, branch: github.REPO.branch, refsVia: refsResult.via, repo: `${github.REPO.owner}/${github.REPO.name}` },
    files,
    waves,
    laneRefs: laneRefs(refsResult.refs),
    latestRelease: release,
    warnings,
  };
}

module.exports = { syncFromGitHub, SyncError, waveRefs, laneRefs, REQUIRED, OPTIONAL, SNAPSHOT_SCHEMA, mapLimit };
