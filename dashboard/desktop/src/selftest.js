'use strict';
// `CreatorStudioDashboard.exe --selftest [--offline] [--out=DIR] [--userdata=DIR]` drives the real application (the same
// binary users run) through its screens, checks what is on screen against the data it loaded, takes screenshots, writes
// selftest-report.json and exits 0 (all checks passed) or 1. It reads GitHub like normal use; it writes only to --out.
const fs = require('fs');
const path = require('path');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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
