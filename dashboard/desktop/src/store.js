'use strict';
// The last-known snapshot and a few small flags live in one folder (the app's own user-data folder). Writes are atomic
// (temporary file, then rename) so a crash or power loss never leaves a half-written snapshot. Nothing here ever
// touches the repository.
const fs = require('fs');
const path = require('path');

const SNAPSHOT_FILE = 'last-snapshot.json';
const META_FILE = 'meta.json';

function atomicWrite(file, text) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, text, 'utf8');
  fs.renameSync(tmp, file);
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (err) {
    return null;                            // missing or corrupt: treated as "nothing saved"
  }
}

function saveSnapshot(dir, snapshot) {
  atomicWrite(path.join(dir, SNAPSHOT_FILE), JSON.stringify(snapshot));
}

function loadSnapshot(dir) {
  const s = readJson(path.join(dir, SNAPSHOT_FILE));
  if (!s || typeof s !== 'object' || s.schema !== 1 || typeof s.fetchedAt !== 'string' || !s.files || typeof s.files !== 'object'
    || !s.source || !/^[0-9a-f]{40}$/.test(s.source.commit || '') || typeof s.files['PROJECT_STATUS.json'] !== 'string') return null;
  return s;
}

function loadMeta(dir) {
  const m = readJson(path.join(dir, META_FILE));
  return m && typeof m === 'object' ? m : {};
}

function saveMeta(dir, meta) {
  atomicWrite(path.join(dir, META_FILE), JSON.stringify(meta));
}

module.exports = { saveSnapshot, loadSnapshot, loadMeta, saveMeta, atomicWrite, SNAPSHOT_FILE, META_FILE };
