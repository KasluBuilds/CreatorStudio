'use strict';
// Reads the colour tokens out of ui/styles.css and measures them: WCAG contrast ratios and colour distance. Used by the
// theme tests so the palettes are checked as numbers, not by eye. Light is the default :root block; dark is the
// `@media (prefers-color-scheme: dark)` override. (The app sets Electron's nativeTheme, which drives that media query.)
const fs = require('fs');
const path = require('path');

const CSS_PATH = path.join(__dirname, '..', 'ui', 'styles.css');

function readCss() { return fs.readFileSync(CSS_PATH, 'utf8'); }

function tokensOf(block) {
  const out = {};
  for (const m of block.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/gi)) out[m[1]] = m[2].trim();
  return out;
}

/** { light: {--bg: '#...'}, dark: {...} }; dark inherits any token it does not override. */
function parseThemes(css = readCss()) {
  const root = /:root\s*\{([^}]*)\}/.exec(css);
  const dark = /@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}\s*\}/.exec(css);
  if (!root || !dark) throw new Error('styles.css must define :root (light) and @media (prefers-color-scheme: dark) :root');
  const light = tokensOf(root[1]);
  return { light, dark: { ...light, ...tokensOf(dark[1]) }, darkOverrides: tokensOf(dark[1]) };
}

function hex(h) {
  const m = /^#([0-9a-f]{6})$/i.exec(h || '');
  if (!m) throw new Error(`not a #rrggbb colour: ${h}`);
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

const lin = (c) => { const s = c / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; };
function luminance(rgb) { return 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2]); }
function contrast(a, b) {
  const la = luminance(a); const lb = luminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}
/** `color-mix(in srgb, fg pct%, bg)` computed as the browser does. */
function mix(fg, pct, bg) { return fg.map((c, i) => Math.round(c * pct / 100 + bg[i] * (1 - pct / 100))); }

function lab(rgb) {
  const [r, g, b] = rgb.map(lin);
  let x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047;
  let y = r * 0.2126 + g * 0.7152 + b * 0.0722;
  let z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883;
  const f = (t) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116);
  x = f(x); y = f(y); z = f(z);
  return [116 * y - 16, 500 * (x - y), 200 * (y - z)];
}
function deltaE(a, b) { const p = lab(a); const q = lab(b); return Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]); }

module.exports = { CSS_PATH, readCss, parseThemes, hex, contrast, mix, deltaE, luminance };
