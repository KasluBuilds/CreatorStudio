'use strict';
// Where Orbit's snapshot comes from: a local JSON file the user selects (or a path given with --orbit=<file>). READ ONLY:
// the snapshot file is only ever read. The one thing written is the app's own setting (which file was chosen), in the app's
// user-data folder, never the repository, GitHub or the supervisor's state. No network.
const fs = require('fs');
const path = require('path');
const { atomicWrite } = require('./store');

const SETTING_FILE = 'orbit-source.json';
const MAX_BYTES = 2 * 1024 * 1024;

function loadSetting(dir) {
  try {
    const p = JSON.parse(fs.readFileSync(path.join(dir, SETTING_FILE), 'utf8'));
    return p && typeof p.path === 'string' && p.path ? p.path : null;
  } catch (err) { return null; }
}

/** The only write in this module: remembers the chosen file path in the app's own folder. */
function saveSetting(dir, file) {
  atomicWrite(path.join(dir, SETTING_FILE), JSON.stringify({ path: String(file) }));
}

/** Reads and parses the snapshot file. Returns {status:'unconfigured'|'missing'|'error'|'ok', ...}; never throws. */
function load(file) {
  if (!file) return { status: 'unconfigured' };
  const name = path.basename(file);
  let st;
  try { st = fs.statSync(file); } catch (err) { return err && err.code === 'ENOENT' ? { status: 'missing', name } : { status: 'error', name, reason: 'cannot be opened' }; }
  if (!st.isFile()) return { status: 'error', name, reason: 'not a file' };
  if (st.size > MAX_BYTES) return { status: 'error', name, reason: 'file is larger than 2 MB' };
  try { return { status: 'ok', name, data: JSON.parse(fs.readFileSync(file, 'utf8')) }; } catch (err) { return { status: 'error', name, reason: 'not valid JSON' }; }
}

module.exports = { load, loadSetting, saveSetting, SETTING_FILE, MAX_BYTES };
