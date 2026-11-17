'use strict';
// The user's appearance choice: 'light', 'dark' or 'system' (follow Windows). It is stored in its own small file in the
// app's user-data folder, apart from the snapshot and the notification flags, so no other part of the app can overwrite
// it. Anything missing, unreadable or unknown means 'system'. The page never reads this file: the main process applies
// the choice to Electron's nativeTheme, which drives the stylesheet's prefers-color-scheme rules.
const fs = require('fs');
const path = require('path');
const { atomicWrite } = require('./store');

const MODES = Object.freeze(['light', 'dark', 'system']);
const DEFAULT_MODE = 'system';
const THEME_FILE = 'theme.json';

function normalize(value) {
  return MODES.includes(value) ? value : DEFAULT_MODE;
}

function load(dir) {
  try {
    const parsed = JSON.parse(fs.readFileSync(path.join(dir, THEME_FILE), 'utf8'));
    return normalize(parsed && parsed.mode);
  } catch (err) {
    return DEFAULT_MODE;                     // first launch, missing or corrupt file
  }
}

/** Saves a valid mode and returns it; an invalid mode is refused rather than stored. */
function save(dir, mode) {
  if (!MODES.includes(mode)) throw new Error(`unknown appearance mode: ${String(mode).slice(0, 20)}`);
  atomicWrite(path.join(dir, THEME_FILE), JSON.stringify({ mode }));
  return mode;
}

/** Applies a mode to an Electron nativeTheme (or any object with a themeSource property). */
function apply(nativeTheme, mode) {
  nativeTheme.themeSource = normalize(mode);
  return nativeTheme.themeSource;
}

module.exports = { MODES, DEFAULT_MODE, THEME_FILE, normalize, load, save, apply };
