'use strict';
// Tiny DOM helpers. Text is only ever set with textContent / createTextNode, so nothing read from the repository can
// become markup. Links never navigate the window: they ask the main process to open a whitelisted https address.
(function () {
  const D = (window.Dashboard = window.Dashboard || {});
  const SVG_NS = 'http://www.w3.org/2000/svg';

  function apply(el, attrs) {
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'class') el.setAttribute('class', v);
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else el.setAttribute(k, v === true ? '' : String(v));
    }
  }

  function add(el, children) {
    for (const c of children.flat(Infinity)) {
      if (c === null || c === undefined || c === false) continue;
      el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    apply(el, attrs);
    return add(el, children);
  }

  function svg(tag, attrs, ...children) {
    const el = document.createElementNS(SVG_NS, tag);
    apply(el, attrs);
    return add(el, children);
  }

  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

  function fmtPct(v) { return v === null || v === undefined ? '—' : `${v.toFixed(1)}%`; }

  function ago(iso, nowMs) {
    const t = new Date(iso).getTime();
    if (!Number.isFinite(t)) return 'unknown';
    const s = Math.max(0, Math.round(((nowMs || Date.now()) - t) / 1000));
    if (s < 45) return 'just now';
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  }

  function when(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? 'unknown' : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  }

  /** A progress ring. value 0..100 or null (untracked). */
  function ring(value, { size = 96, stroke = 10, cls = 'ring-fill', label = '', sub = '' } = {}) {
    const r = (size - stroke) / 2;
    const c = 2 * Math.PI * r;
    const frac = value === null || value === undefined ? 0 : Math.max(0, Math.min(100, value)) / 100;
    return svg('svg', { class: 'ring', viewBox: `0 0 ${size} ${size}`, width: size, height: size, role: 'img', 'aria-label': `${label} ${fmtPct(value)}` },
      svg('circle', { class: 'ring-track', cx: size / 2, cy: size / 2, r, 'stroke-width': stroke, fill: 'none' }),
      svg('circle', { class: cls, cx: size / 2, cy: size / 2, r, 'stroke-width': stroke, fill: 'none', 'stroke-linecap': 'round',
        'stroke-dasharray': `${(c * frac).toFixed(2)} ${c.toFixed(2)}`, transform: `rotate(-90 ${size / 2} ${size / 2})` }),
      svg('text', { class: 'ring-text', 'font-size': size >= 100 ? 20 : 16, x: size / 2, y: size / 2 + 1, 'text-anchor': 'middle', 'dominant-baseline': 'middle' }, value === null || value === undefined ? '—' : (Number.isInteger(value) ? `${value}%` : `${value.toFixed(1)}%`)),
      sub ? svg('text', { class: 'ring-sub', x: size / 2, y: size / 2 + size * 0.2, 'text-anchor': 'middle' }, sub) : null);
  }

  function link(url, label, cls) {
    return h('a', { href: url, class: `link ${cls || ''}`, rel: 'noreferrer', onclick: (e) => { e.preventDefault(); window.dashboard.openLink(url); } }, label);
  }

  D.dom = { h, svg, clear, fmtPct, ago, when, ring, link };
})();
