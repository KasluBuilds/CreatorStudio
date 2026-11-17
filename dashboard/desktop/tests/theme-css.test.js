'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const t = require('./theme-css');

const themes = t.parseThemes();
const COLOR_TOKENS = ['--bg', '--bg2', '--panel', '--panel2', '--line', '--text', '--muted', '--accent', '--on-accent', '--good', '--warn', '--bad', '--info',
  '--on-good', '--on-bad', '--road', '--road-edge', '--road-line', '--road-done'];
const c = (theme, k) => t.hex(themes[theme][k]);

test('both themes define every colour token, and the dark theme overrides every one deliberately (it is not inherited from light)', () => {
  for (const k of COLOR_TOKENS) {
    assert.match(themes.light[k], /^#[0-9a-f]{6}$/i, `light ${k}`);
    assert.ok(Object.prototype.hasOwnProperty.call(themes.darkOverrides, k), `dark overrides ${k}`);
    assert.match(themes.dark[k], /^#[0-9a-f]{6}$/i, `dark ${k}`);
  }
});

test('the dark theme is designed, not a plain inversion of the light one', () => {
  for (const k of ['--bg', '--panel', '--text']) assert.notEqual(themes.dark[k].toLowerCase(), themes.light[k].toLowerCase(), k);
  assert.ok(t.contrast(c('dark', '--bg'), c('light', '--bg')) > 10, 'dark surfaces are dark, light surfaces are light');
  // Dark surfaces are blue-slate (blue leads), never neutral grey or pure black; an inversion would not do that.
  for (const k of ['--bg', '--bg2', '--panel', '--panel2']) {
    const [r, g, b] = c('dark', k);
    assert.ok(b > r && b >= g, `${k} keeps a blue-slate hue`);
    assert.ok(r + g + b > 20, `${k} is not pure black`);
  }
  assert.ok(c('dark', '--text').some((v) => v < 250), 'dark text is soft, not pure white');
  assert.notDeepEqual(c('dark', '--good'), c('light', '--good'), 'status colours are retuned per theme');
});

function checks(theme) {
  const out = [];
  const add = (label, fg, bg, min) => out.push({ label, ratio: t.contrast(fg, bg), min });
  for (const b of ['--bg', '--panel', '--panel2']) {
    add(`text on ${b}`, c(theme, '--text'), c(theme, b), 7);
    add(`muted on ${b}`, c(theme, '--muted'), c(theme, b), 4.5);
  }
  for (const b of ['--bg', '--panel']) add(`accent (links) on ${b}`, c(theme, '--accent'), c(theme, b), 4.5);
  add('primary button text', c(theme, '--on-accent'), c(theme, '--accent'), 4.5);
  add('check mark on implemented node', c(theme, '--on-good'), c(theme, '--good'), 4.5);
  add('! on blocked node', c(theme, '--on-bad'), c(theme, '--bad'), 4.5);
  for (const s of ['--good', '--warn', '--bad', '--info']) {
    add(`${s} text on panel`, c(theme, s), c(theme, '--panel'), 4.5);
    add(`${s} text on its pill`, c(theme, s), t.mix(c(theme, s), 14, c(theme, '--panel2')), 4.5);
    add(`${s} text on its chip`, c(theme, s), t.mix(c(theme, s), 18, c(theme, '--bg2')), 4.5);
  }
  for (const [s, pct] of [['--bad', 16], ['--warn', 16], ['--info', 10]]) {
    add(`body text in the ${s} banner`, c(theme, '--text'), t.mix(c(theme, s), pct, c(theme, '--bg')), 7);
  }
  add('link inside the info banner', c(theme, '--accent'), t.mix(c(theme, '--info'), 10, c(theme, '--bg')), 4.5);
  add('text on the filming-gate banner (green end)', c(theme, '--text'), t.mix(c(theme, '--good'), 38, c(theme, '--bg')), 4.5);
  add('text on the filming-gate banner (amber end)', c(theme, '--text'), t.mix(c(theme, '--warn'), 30, c(theme, '--bg')), 4.5);
  add('text on the ready gate card', c(theme, '--text'), t.mix(c(theme, '--good'), 22, c(theme, '--panel')), 7);
  add('ready-to-start icon on its node', c(theme, '--warn'), c(theme, '--panel'), 3);
  add('upcoming milestone title (muted) on the road background', c(theme, '--muted'), c(theme, '--panel2'), 4.5);
  add('road against the page', c(theme, '--road'), c(theme, '--bg'), 1.2);
  add('road edge against the road', c(theme, '--road-edge'), c(theme, '--road'), 1.15);
  add('travelled road against the road', c(theme, '--road-done'), c(theme, '--road'), 3);
  add('centre line against the road (decorative)', c(theme, '--road-line'), c(theme, '--road'), 1.4);
  add('card border against the card', c(theme, '--line'), c(theme, '--panel'), 1.15);
  return out;
}

for (const theme of ['light', 'dark']) {
  test(`${theme} theme: every text, control and status pairing meets its contrast minimum`, () => {
    const bad = checks(theme).filter((x) => x.ratio < x.min).map((x) => `${x.label}: ${x.ratio.toFixed(2)} < ${x.min}`);
    assert.deepEqual(bad, []);
  });

  test(`${theme} theme: status colours are clearly distinguishable from each other`, () => {
    const names = ['--good', '--warn', '--bad', '--accent'];
    for (let i = 0; i < names.length; i++) {
      for (let j = i + 1; j < names.length; j++) {
        assert.ok(t.deltaE(c(theme, names[i]), c(theme, names[j])) >= 30, `${names[i]} vs ${names[j]}`);
      }
    }
  });
}

test('status is never carried by colour alone: every milestone state has its own glyph or shape', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'ui', 'views.js'), 'utf8');
  const icon = new Function(`return ${/const ICON = (\{[^}]*\})/.exec(src)[1]}`)();
  const glyphs = ['verified', 'done', 'active', 'blocked', 'ready'].map((k) => icon[k]);
  assert.equal(new Set(glyphs).size, glyphs.length, 'distinct glyphs for verified, implemented, in progress, blocked, ready');
  assert.ok(glyphs.every((g) => g.length > 0));
  assert.match(t.readCss(), /\.node-upcoming \.node-dot \{[^}]*stroke-dasharray/, 'upcoming milestones are dashed, not just grey');
});

test('visual consistency: every colour in the stylesheet comes from a token (no hard-coded colours outside the two palettes)', () => {
  let css = t.readCss();
  css = css.replace(/:root\s*\{[^}]*\}/g, '').replace(/@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{[^}]*\}\s*\}/, '');
  css = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const literals = [...css.matchAll(/#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)/g)].map((m) => m[0]);
  assert.deepEqual(literals.filter((l) => l !== '#000'), []);       // a pure-black darkening mix is the only literal
  const usedVars = new Set([...css.matchAll(/var\((--[a-z0-9-]+)\)/g)].map((m) => m[1]));
  for (const v of usedVars) assert.ok(v in themes.light, `${v} is defined`);
});

test('colour-scheme is declared so native scrollbars, form controls and the title bar follow the theme', () => {
  assert.match(t.readCss(), /color-scheme:\s*light dark/);
});

test('the theme switch is keyboard and screen-reader friendly', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'ui', 'app.js'), 'utf8');
  assert.match(src, /role: 'group', 'aria-label': 'Appearance'/);
  assert.match(src, /'aria-pressed'/);
  assert.match(src, /\['light'.*\['dark'.*\['system'/);
  assert.match(t.readCss(), /\.theme-switch button:focus-visible/);
});
