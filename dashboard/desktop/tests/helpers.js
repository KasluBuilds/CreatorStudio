'use strict';
// Test helpers: build snapshots from the repository's own canonical files, and a fake fetch for the GitHub client.
const fs = require('fs');
const path = require('path');

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..');
const SHA = 'a'.repeat(40);

function readRepo(rel) {
  return fs.readFileSync(path.join(REPO_ROOT, rel), 'utf8');
}

const FROZEN = path.join(__dirname, 'fixtures', 'frozen-2026-11-17');
const fixtureText = (name) => fs.readFileSync(path.join(FROZEN, name), 'utf8');

/** A frozen copy of the canonical files (2026-11-17): state-specific expectations are written against this. */
function fixtureSnapshot(overrides = {}) {
  const files = {};
  for (const f of ['PROJECT_STATUS.json', 'ACTIVE_TASKS.json', 'DEVELOPMENT_TRACKER.json', 'AGENT_REGISTRY.json']) files[f] = fixtureText(f);
  files['AI_Creator_Studio_Documentation/FIRST_REAL_VIDEO_CHECKLIST.md'] = fixtureText('FIRST_REAL_VIDEO_CHECKLIST.md');
  return Object.assign({ schema: 1, fetchedAt: '2026-11-17T12:00:00.000Z', source: { commit: SHA, branch: 'main', refsVia: 'git', repo: 'KasluBuilds/CreatorStudio' },
    files, waves: {}, laneRefs: {}, latestRelease: null, warnings: [] }, overrides);
}

/** The repository's CURRENT canonical files: only invariants that must hold at every commit belong here. */
function repoSnapshot(overrides = {}) {
  const files = {};
  for (const f of ['PROJECT_STATUS.json', 'ACTIVE_TASKS.json', 'DEVELOPMENT_TRACKER.json', 'AGENT_REGISTRY.json', 'SESSION_HANDOFF.md',
    'AI_Creator_Studio_Documentation/FIRST_REAL_VIDEO_CHECKLIST.md']) {
    files[f] = readRepo(f);
  }
  return Object.assign({ schema: 1, fetchedAt: '2026-11-17T12:00:00.000Z', source: { commit: SHA, branch: 'main', refsVia: 'git', repo: 'KasluBuilds/CreatorStudio' },
    files, waves: {}, laneRefs: {}, latestRelease: null, warnings: [] }, overrides);
}

module.exports = { REPO_ROOT, SHA, readRepo, repoSnapshot, fixtureSnapshot, fixtureText };
