'use strict';
// `CreatorStudioDashboard.exe --selftest [--offline] [--out=DIR] [--userdata=DIR]` drives the real application (the same
// binary users run) through its screens, checks what is on screen against the data it loaded, takes screenshots, writes
// selftest-report.json and exits 0 (all checks passed) or 1. It reads GitHub like normal use; it writes only to --out.
const fs = require('fs');
const path = require('path');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Runs inside the page: the WCAG contrast of every visible text element against the colour it is really drawn on.
const AUDIT_JS = `(() => {
  const parse = (str) => {
    let m = /^rgba?\\(([^)]+)\\)$/.exec(str);
    if (m) { const p = m[1].split(/[ ,\\/]+/).filter(Boolean).map(Number); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; }
    m = /^color\\(srgb ([^)]+)\\)$/.exec(str);
    if (m) { const p = m[1].split(/[ \\/]+/).filter(Boolean).map(Number); return [p[0] * 255, p[1] * 255, p[2] * 255, p.length > 3 ? p[3] : 1]; }
    return null;
  };
  const over = (top, bot) => [0, 1, 2].map((i) => top[i] * top[3] + bot[i] * (1 - top[3])).concat([1]);
  const lin = (c) => { const s = c / 255; return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4); };
  const lum = (c) => 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2]);
  const ratio = (a, b) => { const x = lum(a); const y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const bgOf = (el) => {
    const layers = [];
    for (let e = el; e; e = e.parentElement) {
      const c = parse(getComputedStyle(e).backgroundColor);
      if (c && c[3] > 0) { layers.push(c); if (c[3] >= 1) break; }
    }
    let base = layers.length && layers[layers.length - 1][3] >= 1 ? layers.pop() : (parse(getComputedStyle(document.body).backgroundColor) || [255, 255, 255, 1]);
    for (let i = layers.length - 1; i >= 0; i--) base = over(layers[i], base);
    return base;
  };
  const failures = [];
  let checked = 0;
  for (const el of document.querySelectorAll('body *')) {
    if (!Array.from(el.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim())) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    const svgText = el instanceof SVGElement;
    const fg = parse(svgText ? cs.fill : cs.color);
    if (!fg) continue;
    let backgrounds = [bgOf(el)];
    // an SVG glyph is drawn on the shape drawn just before it (the node's circle); text on a CSS gradient is measured against every colour stop
    const prev = el.previousElementSibling;
    if (svgText && prev && prev.tagName.toLowerCase() === 'circle') { const f = parse(getComputedStyle(prev).fill); if (f) backgrounds = [f]; }
    const gradient = getComputedStyle(el).backgroundImage;
    if (gradient && gradient !== 'none') {
      const stops = (gradient.match(/rgba?\\([^)]*\\)|color\\(srgb [^)]*\\)/g) || []).map(parse).filter(Boolean);
      if (stops.length) backgrounds = stops.map((c) => over(c, backgrounds[0]));
    }
    const bg = backgrounds[0];
    const eff = fg[3] < 1 ? over(fg, bg) : fg;
    const size = parseFloat(cs.fontSize); const bold = parseInt(cs.fontWeight, 10) >= 700;
    const min = (size >= 24 || (size >= 18.66 && bold)) ? 3 : 4.5;
    checked += 1;
    const ra = Math.min.apply(null, backgrounds.map((b) => ratio(fg[3] < 1 ? over(fg, b) : fg, b)));
    if (ra < min) failures.push({ text: el.textContent.trim().slice(0, 40), ratio: Math.round(ra * 100) / 100, min, tag: el.tagName.toLowerCase() + '.' + (el.getAttribute('class') || '') });
  }
  return { checked, failures: failures.slice(0, 8), failureCount: failures.length };
})()`;


async function runSelftest({ app, win, controller, opts }) {
  const out = path.resolve(opts.out || path.join(app.getPath('userData'), 'selftest'));
  fs.mkdirSync(out, { recursive: true });
  const checks = [];
  const consoleErrors = [];
  win.webContents.on('console-message', (e) => {
    const lvl = e && e.level;
    if (lvl === 'error' || lvl === 3) consoleErrors.push(String(e.message));
  });
  const ok = (name, pass, detail = '') => { checks.push({ name, ok: !!pass, detail: String(detail) }); };
  const exec = async (js) => {
    try { return await win.webContents.executeJavaScript(js, true); }
    catch (err) { ok(`script ran: ${js.slice(0, 70)}`, false, err.message); return null; }
  };
  const waitFor = async (js, ms = 8000) => { const end = Date.now() + ms; while (Date.now() < end) { if (await exec(js)) return true; await sleep(100); } return false; };
  const shot = async (name) => {
    let lastErr = '';
    for (let attempt = 0; attempt < 4; attempt++) {
      await sleep(450);
      try {
        const img = await win.webContents.capturePage();
        if (img.isEmpty()) { lastErr = 'empty image'; continue; }
        fs.writeFileSync(path.join(out, `${name}.png`), img.toPNG());
        ok(`screenshot ${name}`, true, JSON.stringify(img.getSize()));
        return;
      } catch (err) { lastErr = err.message; }
    }
    ok(`screenshot ${name}`, false, lastErr);
  };
  const go = async (hash) => {
    await exec(`location.hash = ${JSON.stringify(hash)}`);
    await waitFor(`window.__renderedHash === ${JSON.stringify(hash)}`, 4000);
    await sleep(250);
  };
  const t0 = Date.now();

  ok('renderer started', await waitFor('window.__ready === true'));
  const state = controller.getState();
  const model = state.model;
  ok('data is available', !!model && model.ok, model ? model.problems.join('; ') : 'no model');
  if (!model || !model.ok) return finish();

  win.setContentSize(1440, 940);
  const snapshot = controller.snapshot;
  ok('source commit is a full sha', /^[0-9a-f]{40}$/.test(snapshot.source.commit), snapshot.source.commit);
  await waitFor('!!document.querySelector("#sync-commit")', 15000);
  const header = await exec('document.querySelector("#sync-commit") ? document.querySelector("#sync-commit").textContent : ""');
  ok('header shows the source commit', header.includes(model.sync.shortCommit), header);
  ok('header shows a sync time', (await exec('document.querySelector("#sync-time").textContent')).includes('Last synchronized'));

  if (opts.offline) {
    ok('offline banner is shown', await exec('!!document.querySelector("#offline-banner")'));
    ok('connection state is offline', state.connection.state === 'offline', state.connection.state);
    ok('last-known snapshot is still shown', await exec('document.querySelectorAll(".tab").length') > 2);
  } else {
    ok('connection state is online', state.connection.state === 'online', state.connection.state);
    ok('no offline banner when online', !(await exec('!!document.querySelector("#offline-banner")')));
  }

  // tabs: one per active agent, plus overview, roadmap, gate
  const tabs = await exec('[...document.querySelectorAll(".tab")].map(t => t.dataset.tab)');
  ok('one tab per registered agent', model.agents.every((a) => tabs.includes(`agent:${a.id}`)), tabs.join(','));
  ok('overview, roadmap and gate tabs exist', ['overview', 'roadmap', 'gate'].every((t) => tabs.includes(t)), tabs.join(','));

  // overview: the canonical global figure is on screen, unchanged, and clicking it opens the roadmap
  await go('#/overview');
  const big = await exec('document.querySelector(".metric-big") && document.querySelector(".metric-big").textContent');
  ok('global metric equals the canonical figure', big === `${model.global.implementation.toFixed(1)}%`, `${big} vs ${model.global.implementation}`);
  await exec('document.querySelector(".metric").click()');
  await sleep(300);
  ok('clicking the metric opens the global roadmap', (await exec('location.hash')).startsWith('#/roadmap'));
  ok('roadmap shows every milestone', (await exec('document.querySelectorAll("svg.road-svg .node").length')) === model.milestones.length);
  const full = await exec('document.querySelector("main").scrollHeight');
  win.setContentSize(1440, Math.min(Math.max(full + 190, 940), 6200)); await sleep(500);
  await shot('global-roadmap-full');
  win.setContentSize(1440, 940); await sleep(400);
  await exec('document.querySelector(".node") && document.querySelector(".node").dispatchEvent(new MouseEvent("click", {bubbles: true}))');
  await sleep(300);
  ok('selecting a milestone shows its detail', await exec('!!document.querySelector(".detail h2")'));
  await shot('global-roadmap-detail');
  await go('#/overview');
  await shot('overview');

  // each agent tab
  for (const a of model.agents) {
    await go(`#/agent/${a.id}`);
    const nodes = await exec('document.querySelectorAll("svg.road-svg .node").length');
    ok(`tab ${a.id} draws its ${a.milestones.length} milestones`, nodes === a.milestones.length, nodes);
    ok(`tab ${a.id} shows its name`, (await exec('document.querySelector(".agent-head h1").textContent')) === a.name);
    await shot(`agent-${a.id}`);
  }
  // an unknown agent does not crash
  await go('#/agent/does-not-exist');
  ok('unknown agent route is handled', await exec('document.body.textContent.includes("No active agent")'));

  // gate
  for (const g of model.gates) {
    await go(`#/gate/${g.id}`);
    const text = await exec('document.querySelector("main").textContent');
    ok(`gate ${g.id} shows its state (${g.display})`, g.display === 'ready' ? text.includes('Time to film real videos') : (g.display === 'acknowledged' ? /acknowledged/i.test(text) : /pending/i.test(text)));
    ok(`gate ${g.id} shows the checklist`, g.checklistText ? await exec('!!document.querySelector(".md") && document.querySelector(".md").children.length > 3') : true);
    await shot(`gate-${g.id}`);
  }

  // manual refresh (only meaningful with a network)
  if (!opts.offline) {
    await go('#/overview');
    const before = await exec('document.querySelector("#sync-time").textContent');
    await sleep(1100);
    await exec('document.querySelector("#refresh").click()');
    const done = await waitFor('!document.querySelector("#refresh").disabled', 20000);
    await sleep(300);
    const after = await exec('document.querySelector("#sync-time").textContent');
    ok('manual refresh completes', done);
    ok('refresh updates the sync time', after !== before || before.includes('just now'), `${before} -> ${after}`);
  }


  // ---- appearance: Light / Dark / System (presentation only; added after the original checks, which are unchanged)
  {
    const themeFile = path.join(app.getPath('userData'), 'theme.json');
    const startPref = await exec('window.__debug.state.theme');
    ok('appearance preference is valid', ['light', 'dark', 'system'].includes(startPref), startPref);
    if (opts.expectTheme) ok(`the saved appearance (${opts.expectTheme}) was restored at startup`, startPref === opts.expectTheme, startPref);
    ok('appearance switch has Light, Dark and System', JSON.stringify(await exec('[...document.querySelectorAll("#theme-switch button")].map(b => b.dataset.themeMode)')) === '["light","dark","system"]');
    const firstAgent = model.agents[0].id;
    const views = [['overview', '#/overview'], ['roadmap', '#/roadmap'], [`agent-${firstAgent}`, `#/agent/${firstAgent}`], ['gate', `#/gate/${model.gates[0] ? model.gates[0].id : ''}`]];
    for (const mode of ['dark', 'light', 'system']) {
      await go('#/overview');
      await exec(`document.querySelector('[data-theme-mode="${mode}"]').click()`);
      await waitFor(`window.__debug.state.theme === ${JSON.stringify(mode)}`, 6000);
      await sleep(600);
      ok(`${mode}: the ${mode} button is the pressed one`, JSON.stringify(await exec('[...document.querySelectorAll("#theme-switch button")].filter(b => b.getAttribute("aria-pressed") === "true").map(b => b.dataset.themeMode)')) === JSON.stringify([mode]));
      const wantDark = mode === 'dark' ? true : mode === 'light' ? false : (await exec('window.__debug.state.resolvedTheme')) === 'dark';
      ok(`${mode}: the page really is ${wantDark ? 'dark' : 'light'} (prefers-color-scheme)`, (await exec('matchMedia("(prefers-color-scheme: dark)").matches')) === wantDark);
      let saved = null;
      try { saved = JSON.parse(fs.readFileSync(themeFile, 'utf8')).mode; } catch (e) { saved = String(e.message); }
      ok(`${mode}: the choice is saved to disk`, saved === mode, saved);
      const bodyBg = await exec('getComputedStyle(document.body).backgroundColor');
      ok(`${mode}: the page background matches the theme`, (bodyBg.match(/\d+/g) || [255, 255, 255]).slice(0, 3).map(Number).reduce((a, b) => a + b, 0) < 200 === wantDark, bodyBg);
      for (const [name, hash] of views) {
        await go(hash);
        const r = await exec(AUDIT_JS);
        ok(`${mode}: text contrast on ${name} (${r ? r.checked : 0} text elements)`, !!r && r.checked > 15 && r.failureCount === 0, r ? JSON.stringify(r.failures) : 'no result');
        if (mode !== 'system') await shot(`theme-${mode}-${name}`);
      }
      if (mode === 'dark' || mode === 'light') {
        const nodes = await exec('document.querySelectorAll("svg.road-svg .node").length');
        await go('#/roadmap');
        ok(`${mode}: the roadmap still draws every milestone`, (await exec('document.querySelectorAll("svg.road-svg .node").length')) === model.milestones.length, nodes);
        const globalText = await exec('document.querySelector(".agent-head p.muted").textContent');
        ok(`${mode}: the canonical figure is unchanged`, globalText.includes(`${model.global.implementation.toFixed(1)}% implemented`), globalText);
      }
    }
    // leave the profile as it was found, unless asked to leave the last choice in place so a second launch can prove it persisted
    const finalMode = opts.leaveTheme ? (opts.expectTheme === 'dark' ? 'light' : 'dark') : startPref;
    await go('#/overview');
    await exec(`document.querySelector('[data-theme-mode="${finalMode}"]').click()`);
    await waitFor(`window.__debug.state.theme === ${JSON.stringify(finalMode)}`, 6000);
    if (opts.leaveTheme) ok(`the final choice (${finalMode}) stays saved for the next launch`, JSON.parse(fs.readFileSync(themeFile, 'utf8')).mode === finalMode);
  }

  ok('no renderer console errors', consoleErrors.length === 0, consoleErrors.join(' | '));
  return finish();

  function finish() {
    const failed = checks.filter((c) => !c.ok);
    const report = { ok: failed.length === 0, version: app.getVersion(), packaged: app.isPackaged, offline: !!opts.offline, durationMs: Date.now() - t0,
      source: model && model.ok ? { commit: model.sync.commit, fetchedAt: model.sync.fetchedAt } : null,
      global: model && model.ok ? model.global : null, agents: model && model.ok ? model.agents.map((a) => a.id) : [], checks };
    fs.writeFileSync(path.join(out, 'selftest-report.json'), JSON.stringify(report, null, 2));
    console.log(`SELFTEST ${report.ok ? 'PASSED' : 'FAILED'}: ${checks.length - failed.length}/${checks.length} checks`);
    failed.forEach((c) => console.log(`  FAIL ${c.name}: ${c.detail}`));
    app.exit(report.ok ? 0 : 1);
    return report;
  }
}

module.exports = { runSelftest };
