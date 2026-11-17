'use strict';
// Application shell: header (sync status, refresh), banners (offline, stale, filming gate, problems, update), dynamic tabs
// (one per active agent in AGENT_REGISTRY.json), and hash routing.
(function () {
  const D = window.Dashboard;
  const { h, clear, ago, when, link } = D.dom;
  let state = null;
  let route = { view: 'overview' };
  const els = {};

  function parseHash() {
    const raw = (location.hash || '#/overview').slice(1);
    const [pathPart, query] = raw.split('?');
    const parts = pathPart.split('/').filter(Boolean);
    const q = new URLSearchParams(query || '');
    const r = { view: parts[0] || 'overview', milestone: q.get('m') || null };
    if (r.view === 'agent') r.agent = parts[1] || null;
    if (r.view === 'gate') r.gate = parts[1] || null;
    return r;
  }

  function nav(to) {
    let hash = `#/${to.view}`;
    if (to.agent) hash += `/${to.agent}`;
    if (to.gate) hash += `/${to.gate}`;
    if (to.milestone) hash += `?m=${to.milestone}`;
    if (location.hash === hash) render(); else location.hash = hash;
  }

  function header() {
    const c = state.connection;
    const m = state.model && state.model.ok ? state.model : null;
    const ageText = c.lastSuccessAt ? `${ago(c.lastSuccessAt)} (${when(c.lastSuccessAt)})` : 'never';
    const chipCls = c.state === 'online' ? 'ok' : (c.state === 'offline' || c.state === 'error') ? 'bad' : c.state === 'cached' ? 'warn' : 'warn';
    return h('header', { class: 'topbar' },
      h('div', { class: 'brand' }, h('span', { class: 'logo', 'aria-hidden': 'true' }, '◆'), h('div', null, h('div', { class: 'brand-name' }, 'Creator Studio'), h('div', { class: 'brand-sub' }, 'Development dashboard · read-only'))),
      h('div', { class: 'sync' },
        h('span', { class: `chip chip-${chipCls}`, id: 'conn-chip' }, c.state === 'online' ? 'Live' : c.state === 'cached' ? 'Saved copy' : c.state === 'never' ? 'Not synced' : 'Offline'),
        h('div', { class: 'sync-text' }, h('div', { id: 'sync-time' }, `Last synchronized: ${ageText}`),
          m ? h('div', { class: 'muted', id: 'sync-commit' }, 'Source commit ', link(m.sync.commitUrl, m.sync.shortCommit), ` on ${m.sync.repo}`) : h('div', { class: 'muted' }, 'No data yet'))),
      h('div', { class: 'topbar-actions' }, themeSwitch(),
        h('button', { class: 'btn primary', id: 'refresh', disabled: state.refreshing, onclick: () => window.dashboard.refresh() }, state.refreshing ? 'Refreshing…' : '⟳ Refresh')));
  }

  const THEMES = [['light', '☀', 'Light'], ['dark', '☾', 'Dark'], ['system', '◐', 'System']];

  /** Light / Dark / System. Presentation only: it changes colours and nothing about the data. */
  function themeSwitch() {
    const current = state.theme || 'system';
    return h('div', { class: 'theme-switch', id: 'theme-switch', role: 'group', 'aria-label': 'Appearance' },
      THEMES.map(([mode, icon, label]) => h('button', {
        type: 'button', 'data-theme-mode': mode, 'aria-pressed': current === mode ? 'true' : 'false',
        title: mode === 'system' ? `Follow Windows (currently ${state.resolvedTheme || 'light'})` : `${label} appearance`,
        onclick: () => window.dashboard.setTheme(mode),
      }, h('span', { 'aria-hidden': 'true' }, icon), label)));
  }

  function banners() {
    const out = [];
    const c = state.connection;
    const m = state.model;
    if (state.attention.length) {
      for (const a of state.attention) out.push(h('a', { class: 'banner banner-gate', href: `#/gate/${a.id}`, id: 'gate-banner' }, h('strong', null, 'Time to film real videos. '), `${a.title}. Open the checklist →`));
    }
    if (c.state === 'offline' || c.state === 'error') {
      out.push(h('div', { class: 'banner banner-bad', id: 'offline-banner' }, h('strong', null, c.state === 'offline' ? 'Offline: ' : 'Data problem: '), c.message,
        state.hasSnapshot ? ` The data below is the snapshot saved ${when(c.lastSuccessAt)} and may be out of date.` : ' There is no saved snapshot yet; connect to the internet and press Refresh.'));
    } else if (m && m.ok && m.sync.stale) {
      out.push(h('div', { class: 'banner banner-warn', id: 'stale-banner' }, h('strong', null, 'Stale: '), `this data was synchronized ${ago(m.sync.fetchedAt)}. Press Refresh for the latest.`));
    } else if (c.state === 'cached') {
      out.push(h('div', { class: 'banner banner-warn', id: 'cached-banner' }, h('strong', null, 'Saved copy: '), 'showing the last snapshot while the first refresh runs.'));
    }
    if (m && m.ok && m.sync.latestRelease && state.appVersion && newer(m.sync.latestRelease.tag, state.appVersion)) {
      out.push(h('div', { class: 'banner banner-info', id: 'update-banner' }, h('strong', null, 'A newer dashboard is available: '), m.sync.latestRelease.tag, ' ', link(m.sync.latestRelease.url, 'Download it from GitHub')));
    }
    if (m && m.ok && m.problems.length) {
      out.push(h('details', { class: 'banner banner-info', id: 'problems' }, h('summary', null, `${m.problems.length} data note${m.problems.length === 1 ? '' : 's'}`), h('ul', null, m.problems.map((p) => h('li', null, p)))));
    }
    if (m && !m.ok) out.push(h('div', { class: 'banner banner-bad', id: 'model-problem' }, m.problems.join(' ')));
    return h('div', { class: 'banners' }, out);
  }

  function newer(tag, current) {
    const n = (v) => String(v).replace(/^[^0-9]*/, '').split('.').map((x) => parseInt(x, 10) || 0);
    const a = n(tag); const b = n(current);
    for (let i = 0; i < 3; i++) { if ((a[i] || 0) > (b[i] || 0)) return true; if ((a[i] || 0) < (b[i] || 0)) return false; }
    return false;
  }

  function tabs() {
    const m = state.model && state.model.ok ? state.model : null;
    const items = [['overview', 'Overview', '#/overview'], ['orbit', 'Orbit', '#/orbit']];
    if (m) {
      for (const a of m.agents) items.push([`agent:${a.id}`, a.name, `#/agent/${a.id}`, a.active]);
      items.push(['roadmap', 'Global roadmap', '#/roadmap']);
      if (m.gates.length) items.push(['gate', m.gates.some((g) => g.display === 'ready') ? 'Filming gate ●' : 'Filming gate', `#/gate/${m.gates[0].id}`]);
    }
    const cur = route.view === 'agent' ? `agent:${route.agent}` : route.view;
    return h('nav', { class: 'tabs', role: 'tablist' }, items.map(([key, label, href, active]) =>
      h('a', { class: `tab${key === cur ? ' current' : ''}`, href, role: 'tab', 'aria-selected': key === cur ? 'true' : 'false', 'data-tab': key }, label, active ? h('span', { class: 'live-dot', title: 'Has active work' }) : null)));
  }

  function body() {
    const m = state.model;
    if (route.view === 'orbit') return h('main', { id: 'view', 'data-view': 'orbit' }, D.views.renderOrbit(state.orbit));
    if (!m) return h('main', { class: 'empty' }, h('h1', null, 'Waiting for data'), h('p', null, state.refreshing ? 'Fetching the canonical records from GitHub…' : 'No data has been loaded yet. Check your internet connection and press Refresh.'));
    if (!m.ok) return h('main', { class: 'empty' }, h('h1', null, 'The data cannot be shown'), h('p', null, m.problems.join(' ')));
    let view;
    if (route.view === 'agent') {
      const a = m.agents.find((x) => x.id === route.agent);
      view = a ? D.views.renderAgent(m, a, route, nav) : h('div', { class: 'card' }, h('h2', null, 'Unknown agent'), h('p', null, `No active agent "${route.agent}" is registered in AGENT_REGISTRY.json.`));
    } else if (route.view === 'roadmap') view = D.views.renderRoadmap(m, route, nav);
    else if (route.view === 'gate') {
      const g = m.gates.find((x) => x.id === route.gate) || m.gates[0];
      view = g ? D.views.renderGate(m, g) : h('div', { class: 'card' }, h('h2', null, 'No gate'), h('p', null, 'No user-action gate is defined in PROJECT_STATUS.json.'));
    } else view = D.views.renderOverview(m, nav);
    return h('main', { id: 'view', 'data-view': route.view }, view);
  }

  function render() {
    if (!state) return;
    route = parseHash();
    const scroller = els.root.querySelector('main');
    const top = scroller ? scroller.scrollTop : 0;
    clear(els.root);
    els.root.appendChild(header());
    els.root.appendChild(banners());
    els.root.appendChild(tabs());
    els.root.appendChild(body());
    document.title = state.attention.length ? 'ACTION REQUIRED — Orbit · Creator Studio Dashboard' : 'Orbit · Creator Studio Dashboard';
    // draw the travelled part of every road now that the geometry exists
    els.root.querySelectorAll('svg.road-svg').forEach((s) => { if (s.__afterAttach) s.__afterAttach(); });
    const sel = els.root.querySelector('.node.selected');
    if (sel && route.milestone) sel.scrollIntoView({ block: 'center', behavior: 'instant' });
    else { const mn = els.root.querySelector('main'); if (mn) mn.scrollTop = top; }
    window.__rendered = (window.__rendered || 0) + 1;
    window.__renderedHash = location.hash;
  }

  function setState(s) { state = s; render(); }

  // Ages on the Orbit view are recomputed from the snapshot's own timestamps, so re-read it every 30 s while it is open.
  setInterval(async () => { if (route.view === 'orbit' && window.dashboard) setState(await window.dashboard.getState()); }, 30000);
  window.addEventListener('hashchange', render);
  window.addEventListener('DOMContentLoaded', async () => {
    els.root = document.getElementById('root');
    window.dashboard.onState(setState);
    setState(await window.dashboard.getState());
    window.__ready = true;
  });
  window.__debug = { nav, get state() { return state; }, get route() { return route; } };
})();
