'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const theme = require('../src/theme');
const store = require('../src/store');

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'dash-theme-'));

test('first launch defaults to System', () => {
  assert.equal(theme.DEFAULT_MODE, 'system');
  assert.equal(theme.load(tmp()), 'system');
  assert.deepEqual([...theme.MODES], ['light', 'dark', 'system']);
});

test('the chosen mode is saved and survives a restart (a fresh load of the same folder)', () => {
  const dir = tmp();
  for (const mode of ['light', 'dark', 'system', 'dark']) {
    assert.equal(theme.save(dir, mode), mode);
    assert.equal(theme.load(dir), mode, `restart sees ${mode}`);
  }
});

test('missing, corrupt, empty or unknown stored values all fall back to System', () => {
  const dir = tmp();
  for (const body of ['', '{', 'null', '[]', '{"mode":"neon"}', '{"mode":42}', '{"other":1}', '"dark"']) {
    fs.writeFileSync(path.join(dir, theme.THEME_FILE), body);
    assert.equal(theme.load(dir), 'system', body);
  }
  assert.equal(theme.load(path.join(dir, 'does-not-exist')), 'system');
});

test('an invalid mode is refused and never stored', () => {
  const dir = tmp();
  theme.save(dir, 'dark');
  for (const bad of ['neon', '', null, undefined, 3, {}, '../x']) assert.throws(() => theme.save(dir, bad), /unknown appearance mode/);
  assert.equal(theme.load(dir), 'dark');
});

test('saving writes atomically and touches nothing else (snapshot, notification flags)', () => {
  const dir = tmp();
  store.saveMeta(dir, { notifiedGates: { 'G-X': '2026-11-17' } });
  fs.writeFileSync(path.join(dir, store.SNAPSHOT_FILE), '{"keep":"me"}');
  theme.save(dir, 'light');
  assert.equal(fs.readdirSync(dir).some((n) => n.endsWith('.tmp')), false);
  assert.deepEqual(store.loadMeta(dir), { notifiedGates: { 'G-X': '2026-11-17' } });
  assert.equal(fs.readFileSync(path.join(dir, store.SNAPSHOT_FILE), 'utf8'), '{"keep":"me"}');
  store.saveMeta(dir, { notifiedGates: {} });                 // the controller rewriting its own file must not lose the theme
  assert.equal(theme.load(dir), 'light');
});

test('apply drives Electron nativeTheme and normalizes bad input', () => {
  const fake = { themeSource: 'system' };
  assert.equal(theme.apply(fake, 'dark'), 'dark');
  assert.equal(fake.themeSource, 'dark');
  assert.equal(theme.apply(fake, 'light'), 'light');
  assert.equal(theme.apply(fake, 'garbage'), 'system');
  assert.equal(fake.themeSource, 'system');
});

test('the main process applies the saved theme before it creates the window, and only through the theme module', () => {
  const main = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  assert.ok(main.indexOf('theme.apply(nativeTheme, themeMode)') < main.indexOf('createWindow();'), 'no flash of the wrong theme');
  assert.match(main, /theme\.save\(/);
  assert.doesNotMatch(main, /nativeTheme\.themeSource\s*=/);        // only src/theme.js assigns it
  const preload = fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8');
  assert.match(preload, /setTheme/);
  assert.doesNotMatch(preload, /require\('fs'\)|require\('path'\)|child_process/);   // the page gets no file or process access
});

test('theme code does not touch progress, sync, registry or gate logic: those modules never mention the theme', () => {
  for (const f of ['model.js', 'sync.js', 'github.js', 'controller.js']) {
    assert.doesNotMatch(fs.readFileSync(path.join(__dirname, '..', 'src', f), 'utf8'), /theme|nativeTheme|appearance/i, f);
  }
});
