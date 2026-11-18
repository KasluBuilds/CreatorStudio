'use strict';
// All screens. Each function takes the view model (see src/model.js) and returns DOM nodes. Nothing here changes any
// project record: the only actions are navigation, refresh, and opening whitelisted GitHub links.
(function () {
  const D = (window.Dashboard = window.Dashboard || {});
  const { h, svg, link, ring, fmtPct, ago, when } = D.dom;

  const STATE_LABEL = {
    verified: ['Verified on real input', 'verified'], done: ['Implemented', 'done'], active: ['In progress', 'active'],
    blocked: ['Blocked', 'blocked'], ready: ['Ready to start', 'ready'], upcoming: ['Waiting on dependencies', 'upcoming'],
  };
  const LANE_LABEL = { not_started: 'not started', working: 'working', done: 'done', blocked: 'blocked' };

  const pill = (text, cls) => h('span', { class: `pill pill-${cls}` }, text);
  const statePill = (s) => pill(STATE_LABEL[s][0], STATE_LABEL[s][1]);
  const card = (title, ...kids) => h('section', { class: 'card' }, title ? h('h2', null, title) : null, ...kids);
  const empty = (t) => h('p', { class: 'muted' }, t);

  // ------------------------------------------------------------ road
  function wrap(text, width, maxLines) {
    const words = String(text).split(/\s+/);
    const lines = [];
    let cur = '';
    for (const w of words) {
      if ((cur + ' ' + w).trim().length > width && cur) { lines.push(cur); cur = w; } else cur = (cur + ' ' + w).trim();
    }
    if (cur) lines.push(cur);
    if (lines.length > maxLines) { lines.length = maxLines; lines[maxLines - 1] = lines[maxLines - 1].replace(/.{0,3}$/, '…'); }
    return lines;
  }

  const ICON = { verified: '★', done: '✓', active: '●', blocked: '!', ready: '▶', upcoming: '' };

  /**
   * entries: [{ kind: 'phase', name, stats } | { kind: 'milestone', m }]; selected: milestone id; onSelect(id);
   * showAgent: add the responsible agent to each label (global roadmap).
   */
  function renderRoad(entries, { selected, onSelect, showAgent = false, agentName = () => '' }) {
    const lay = D.road.layout(entries.map((e) => ({ kind: e.kind })));
    const W = lay.width;
    const root = svg('svg', { class: 'road-svg', viewBox: `0 0 ${W} ${lay.height}`, role: 'group', 'aria-label': 'Roadmap' });
    root.appendChild(svg('path', { class: 'road-edge', d: lay.path }));
    root.appendChild(svg('path', { class: 'road-surface', d: lay.path }));
    root.appendChild(svg('path', { class: 'road-line', d: lay.path }));
    const prog = svg('path', { class: 'road-progress', d: lay.path });
    root.appendChild(prog);
    let lastDone = -1;
    entries.forEach((e, i) => { if (e.kind === 'milestone' && (e.m.state === 'done' || e.m.state === 'verified')) lastDone = i; });
    entries.forEach((e, i) => {
      const p = lay.points[i];
      if (e.kind === 'phase') {
        const label = `${e.name.toUpperCase()}  ·  ${fmtPct(e.stats.implementation)} / ${fmtPct(e.stats.verification)}`;
        root.appendChild(svg('g', { class: 'phase-sign', transform: `translate(${p.x.toFixed(1)} ${p.y})` },
          svg('rect', { x: -150, y: -17, width: 300, height: 34, rx: 9 }), svg('text', { x: 0, y: 5, 'text-anchor': 'middle' }, label)));
        return;
      }
      const m = e.m;
      const left = p.side === 'left';                         // the node is right of centre, so its label goes to the right
      const lx = left ? 40 : -40;
      const anchor = left ? 'start' : 'end';
      const titleLines = wrap(m.title, 24, 3);
      const g = svg('g', { class: `node node-${m.state}${selected === m.id ? ' selected' : ''}`, transform: `translate(${p.x.toFixed(1)} ${p.y})`,
        tabindex: 0, role: 'button', 'aria-label': `${m.id} ${m.title}, ${STATE_LABEL[m.state][0]}`, 'data-id': m.id,
        onclick: () => onSelect(m.id), onkeydown: (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onSelect(m.id); } } });
      g.appendChild(svg('circle', { class: 'node-halo', r: 34 }));
      g.appendChild(svg('circle', { class: 'node-dot', r: 23 }));
      g.appendChild(svg('text', { class: 'node-icon', y: 7, 'text-anchor': 'middle' }, ICON[m.state]));
      const t = svg('text', { class: 'node-label', x: lx, 'text-anchor': anchor });
      t.appendChild(svg('tspan', { class: 'node-id', x: lx, dy: '-1.6em' }, `${m.id}${showAgent && m.agent ? ' · ' + agentName(m.agent) : ''}`));
      titleLines.forEach((ln, k) => t.appendChild(svg('tspan', { class: 'node-title', x: lx, dy: k === 0 ? '1.35em' : '1.2em' }, ln)));
      t.appendChild(svg('tspan', { class: 'node-sub', x: lx, dy: '1.5em' }, `impl ${fmtPct(m.implCredit * 5)} · ver ${fmtPct(m.verCredit * 5)}`));
      g.appendChild(t);
      root.appendChild(g);
    });
    // light up the part of the road that has been travelled (needs real geometry, so after attaching)
    root.__afterAttach = () => {
      try {
        const total = prog.getTotalLength();
        if (lastDone < 0) { prog.style.strokeDasharray = `0 ${total}`; return; }
        const target = lay.points[lastDone].y;
        let lo = 0; let hi = total;
        for (let k = 0; k < 28; k++) { const mid = (lo + hi) / 2; if (prog.getPointAtLength(mid).y < target) lo = mid; else hi = mid; }
        prog.style.strokeDasharray = `${hi} ${total}`;
      } catch (err) { /* geometry unavailable: the plain road is still drawn */ }
    };
    return root;
  }

  function roadFor(milestones, phases, opts) {
    const entries = [];
    const byId = new Map(milestones.map((m) => [m.id, m]));
    if (opts.groupByPhase) {
      // dependency order inside phases is already guaranteed by the model; keep the global order of the model for the road
      const out = [];
      let seenPhase = null;
      for (const m of milestones) {
        if (m.phase !== seenPhase) { const ph = phases.find((p) => p.name === m.phase); out.push({ kind: 'phase', name: m.phase, stats: ph ? ph.stats : { implementation: null, verification: null } }); seenPhase = m.phase; }
        out.push({ kind: 'milestone', m });
      }
      return renderRoad(out, opts);
    }
    for (const m of milestones) entries.push({ kind: 'milestone', m });
    return renderRoad(entries, opts);
  }

  // ------------------------------------------------------------ milestone detail
  function evidenceList(evs) {
    if (!evs.length) return empty('No evidence recorded yet.');
    return h('ul', { class: 'evidence' }, evs.map((e) => h('li', null,
      h('div', { class: 'ev-head' }, pill(e.type, 'type'), e.date ? h('span', { class: 'muted' }, e.date) : null, ...e.links.map((l) => link(l.url, l.label))),
      h('p', null, e.summary || '(no summary)'),
      e.command ? h('p', { class: 'mono' }, `${e.command}${e.result ? '  →  ' + e.result : ''}`) : null)));
  }

  function renderDetail(model, m, nav) {
    if (!m) return card('Milestone', empty('Select a milestone on the road to see its checklist, evidence and links.'));
    const cl = m.checklist;
    const lanes = model.waves.flatMap((w) => w.lanes.filter((l) => l.milestones.includes(m.id)).map((l) => ({ ...l, waveId: w.id })));
    const agent = model.agents.find((a) => a.id === m.agent);
    return h('section', { class: 'card detail' },
      h('div', { class: 'detail-head' }, h('div', null, h('div', { class: 'muted mono' }, `${m.id} · ${m.phase} · weight ${m.weight}`), h('h2', null, m.title), statePill(m.state)),
        h('div', { class: 'rings' }, ring(m.implCredit * 5, { size: 84, stroke: 9, cls: 'ring-impl', label: 'Implementation', sub: 'built' }), ring(m.verCredit * 5, { size: 84, stroke: 9, cls: 'ring-ver', label: 'Verification', sub: 'verified' }))),
      m.description ? h('p', null, m.description) : null,
      agent ? h('p', { class: 'muted' }, 'Responsible: ', h('a', { href: `#/agent/${agent.id}`, class: 'link' }, agent.name)) : null,
      h('h3', null, `Checklist · ${cl.done} of ${cl.total}`),
      h('ul', { class: 'check' }, cl.items.map((it) => h('li', { class: `${it.done ? 'done' : ''} ${it.kind || ''}` }, h('span', { class: 'box', 'aria-hidden': 'true' }, it.done ? '✓' : ''), it.text))),
      m.implSummary ? h('p', null, h('strong', null, 'Implementation: '), m.implSummary) : null,
      m.verSummary ? h('p', null, h('strong', null, `Verification (${m.verLevel.toLowerCase().replace(/_/g, ' ')}): `), m.verSummary) : null,
      m.dependsOn.length ? h('p', null, h('strong', null, 'Depends on: '), ...m.dependsOn.flatMap((d, i) => [i ? ', ' : '', h('a', { href: `#/roadmap?m=${d}`, class: 'link' }, d)])) : null,
      lanes.length ? h('div', null, h('h3', null, 'Assigned wave work'), h('ul', { class: 'plain' }, lanes.map((l) => h('li', null,
        link(l.url, `${l.waveId}/${l.id}`), ` ${l.title} `, pill(LANE_LABEL[l.state], l.state), l.criteria.length ? h('span', { class: 'muted' }, ` ${l.criteria.length} acceptance criteria`) : null)))) : null,
      h('h3', null, 'Evidence'), evidenceList(m.evidence),
      h('div', { class: 'links' }, m.commit ? link(`https://github.com/KasluBuilds/CreatorStudio/commit/${m.commit}`, `Milestone commit ${m.commit.slice(0, 7)}`) : null,
        link('https://github.com/KasluBuilds/CreatorStudio/blob/main/PROJECT_STATUS.json', 'PROJECT_STATUS.json')));
  }

  // ------------------------------------------------------------ agent tab
  function renderAgent(model, agent, route, nav) {
    const ms = agent.milestones.map((id) => model.milestones.find((m) => m.id === id)).filter(Boolean);
    const sel = ms.find((m) => m.id === route.milestone)
      || (agent.next && ms.find((m) => m.id === agent.next.id)) || ms[0] || null;      // never an empty detail panel
    const doneCount = ms.filter((m) => m.state === 'done' || m.state === 'verified').length;
    const stats = agent.stats;
    const progressBlock = agent.tracked
      ? h('div', { class: 'rings' }, ring(stats.implementation, { size: 110, stroke: 11, cls: 'ring-impl', label: 'Implementation', sub: 'built' }), ring(stats.verification, { size: 110, stroke: 11, cls: 'ring-ver', label: 'Verification', sub: 'verified' }))
      : h('div', null, pill('untracked', 'upcoming'), h('p', { class: 'muted' }, agent.criteria.total ? `${agent.criteria.done} of ${agent.criteria.total} wave acceptance criteria reported done.` : 'No milestones are assigned to this agent in AGENT_REGISTRY.json, so no percentage can be derived.'));
    const current = agent.current.length
      ? h('ul', { class: 'plain' }, agent.current.map((c) => h('li', null, pill(c.kind, c.kind === 'lane' ? 'active' : 'type'), ' ', c.url ? link(c.url, c.id) : h('strong', null, c.id), ` ${c.title}`, h('div', { class: 'muted' }, c.detail))))
      : empty('No active task or wave lane is registered for this agent right now.');
    const next = agent.next ? h('p', null, h('a', { href: `#/agent/${agent.id}?m=${agent.next.id}`, class: 'link' }, `${agent.next.id} ${agent.next.title}`), ' ', statePill(agent.next.state))
      : empty(agent.tracked ? 'Every milestone of this agent is implemented.' : 'Nothing is planned for this agent in the canonical records.');
    return h('div', { class: 'agent-view' },
      h('section', { class: 'card agent-head' },
        h('div', null, h('h1', null, agent.name), h('p', { class: 'role' }, agent.role || 'No role description in the registry.'),
          h('div', { class: 'chips' }, ...agent.streams.map((s) => pill(s, 'type')), agent.chat ? pill(`ChatGPT chat: ${agent.chat}`, 'type') : null),
          agent.tracked ? h('p', { class: 'muted' }, `${doneCount} of ${ms.length} milestones implemented`) : null,
          h('div', { class: 'links' }, ...agent.docs.map((d) => link(d.url, d.path.split('/').pop()))),
          agent.notes ? h('p', { class: 'muted note' }, agent.notes) : null),
        progressBlock),
      h('div', { class: 'two' }, card('Current assignment', current), card('Next substantial task', next)),
      h('div', { class: 'road-layout' },
        h('div', { class: 'road-wrap' }, ms.length ? roadFor(ms, model.phases, { selected: sel && sel.id, onSelect: (id) => nav({ view: 'agent', agent: agent.id, milestone: id }) }) : card('Roadmap', empty('This agent has no milestones in the registry, so there is no road to draw.'))),
        h('div', { class: 'detail-wrap' }, renderDetail(model, sel, nav))));
  }

  // ------------------------------------------------------------ global roadmap
  function renderRoadmap(model, route, nav) {
    const sel = model.milestones.find((m) => m.id === route.milestone)
      || model.milestones.find((m) => m.id === model.summary.major) || model.milestones[0] || null;
    const name = (id) => (model.agents.find((a) => a.id === id) || { name: id }).name;
    return h('div', { class: 'agent-view' },
      h('section', { class: 'card agent-head' },
        h('div', null, h('h1', null, 'Global roadmap'), h('p', { class: 'role' }, `All ${model.milestones.length} milestones in dependency order, grouped by phase. No milestone appears before the milestones it depends on.`),
          h('p', { class: 'muted' }, `Canonical progress: ${fmtPct(model.global.implementation)} implemented, ${fmtPct(model.global.verification)} verified. Weights are ${model.global.weightsStatus.toLowerCase()}.`)),
        h('div', { class: 'rings' }, ring(model.global.implementation, { size: 110, stroke: 11, cls: 'ring-impl', label: 'Implementation', sub: 'built' }), ring(model.global.verification, { size: 110, stroke: 11, cls: 'ring-ver', label: 'Verification', sub: 'verified' }))),
      h('div', { class: 'road-layout' },
        h('div', { class: 'road-wrap' }, roadFor(model.milestones, model.phases, { groupByPhase: true, selected: sel && sel.id, showAgent: true, agentName: name, onSelect: (id) => nav({ view: 'roadmap', milestone: id }) })),
        h('div', { class: 'detail-wrap' }, renderDetail(model, sel, nav))));
  }

  // ------------------------------------------------------------ gate
  function renderMarkdown(blocks) {
    const inline = (nodes) => nodes.map((n) => {
      if (n.t === 'text') return n.v;
      if (n.t === 'code') return h('code', null, n.v);
      if (n.t === 'bold') return h('strong', null, ...inline(n.c));
      if (n.t === 'italic') return h('em', null, ...inline(n.c));
      if (n.t === 'link') return link(n.href, ...inline(n.c));
      return '';
    });
    const out = [];
    let table = null;
    const endTable = () => { if (table) { out.push(h('table', { class: 'md-table' }, h('tbody', null, ...table))); table = null; } };
    for (const b of blocks) {
      if (b.t !== 'row') endTable();
      if (b.t === 'h') out.push(h(`h${Math.min(4, b.level + 1)}`, null, ...inline(b.c)));
      else if (b.t === 'p') out.push(h('p', null, ...inline(b.c)));
      else if (b.t === 'quote') out.push(h('blockquote', null, ...inline(b.c)));
      else if (b.t === 'row') { table = table || []; table.push(h('tr', null, ...b.cells.map((c) => h('td', null, ...inline(c))))); }
      else if (b.t === 'list') {
        out.push(h(b.kind === 'number' ? 'ol' : 'ul', { class: b.kind === 'task' ? 'check' : '' }, b.items.map((it) => h('li', { class: it.checked ? 'done' : '' },
          b.kind === 'task' ? h('span', { class: 'box', 'aria-hidden': 'true' }, it.checked ? '✓' : '') : null, ...inline(it.c)))));
      }
    }
    endTable();
    return out;
  }

  function renderGate(model, g) {
    const head = g.display === 'ready'
      ? h('section', { class: 'card gate-ready' }, h('h1', null, 'Time to film real videos'), h('p', { class: 'big' }, g.message),
        h('p', null, 'This is the point where development-only work should pause so that real recordings can show whether the editing is good. It is readiness to begin validation, not proof of production quality.'))
      : g.display === 'acknowledged'
        ? h('section', { class: 'card gate-ack' }, h('h1', null, g.title), h('p', null, `Acknowledged by ${g.acknowledgedBy || 'Lukas'} on ${g.acknowledgedDate}.`))
        : h('section', { class: 'card gate-pending' }, h('h1', null, 'Filming readiness gate: pending'), h('p', { class: 'big' }, 'Keep building. This turns into a reminder to start filming real videos when the readiness milestone is implemented.'), h('p', null, h('strong', null, 'When it fires: '), g.title),
          h('p', { class: 'muted' }, g.triggerDescription));
    const trig = g.triggers.map((t) => h('li', null, h('strong', null, `${t.id} ${t.title}`), ' ', pill(t.state.replace(/_/g, ' ').toLowerCase(), t.state === 'IMPLEMENTED' || t.state === 'VERIFIED' || t.state === 'UNDER_REVIEW' ? 'done' : 'upcoming'),
      h('ul', { class: 'check' }, t.dependsOn.map((d) => h('li', { class: d.done ? 'done' : '' }, h('span', { class: 'box' }, d.done ? '✓' : ''), `${d.id} ${d.title}`))),
      t.evidence.length ? h('div', null, h('div', { class: 'muted' }, 'Evidence'), evidenceList(t.evidence)) : null));
    const metCount = g.criteria.filter((c) => c.met).length;
    return h('div', { class: 'gate-view' }, head,
      g.inconsistent ? h('section', { class: 'card warn' }, h('strong', null, 'Record mismatch: '), `the gate is recorded as ${g.recorded} but the trigger ${g.fired ? 'has' : 'has not'} fired. The canonical validator rejects this state; the record needs updating.`) : null,
      h('div', { class: 'two' },
        card('What fires this gate', h('ul', { class: 'plain' }, trig)),
        card(`Readiness criteria · ${metCount} of ${g.criteria.length} evidenced`, h('ul', { class: 'check' }, g.criteria.map((c) => h('li', { class: c.met ? 'done' : '' }, h('span', { class: 'box' }, c.met ? '✓' : ''), h('strong', { class: 'nowrap' }, c.id), ' ', c.text))))),
      card('Procedure recorded for when it fires', h('ol', null, g.steps.map((s) => h('li', null, s)))),
      card('First real video checklist', g.checklistText ? h('div', { class: 'md' }, ...renderMarkdown(D.markdown.parse(g.checklistText))) : empty('The checklist file could not be loaded from GitHub.'),
        g.checklistUrl ? h('div', { class: 'links' }, link(g.checklistUrl, 'Open FIRST_REAL_VIDEO_CHECKLIST.md on GitHub')) : null),
      h('p', { class: 'muted' }, 'This view only displays the existing gate. It does not trigger, replace or acknowledge it: the trigger is the milestone evidence in PROJECT_STATUS.json, and only Lukas acknowledges it. The weekly ChatGPT Phase 6 readiness automation is unchanged.'));
  }

  // ------------------------------------------------------------ overview
  function gateCard(g) {
    const metCount = g.criteria.filter((c) => c.met).length;
    const cls = g.display === 'ready' ? 'gate-ready' : g.display === 'acknowledged' ? 'gate-ack' : 'gate-pending';
    return h('a', { class: `card gate-card ${cls}`, href: `#/gate/${g.id}` },
      h('h2', null, 'Filming readiness gate'),
      g.display === 'ready' ? h('p', { class: 'big' }, 'READY — stop development-only work and start producing real videos.')
        : g.display === 'acknowledged' ? h('p', { class: 'big' }, 'Acknowledged')
          : h('p', { class: 'big' }, 'Pending'),
      h('p', { class: 'muted' }, g.display === 'pending' ? `Fires when ${g.triggers.map((t) => t.id).join(', ')} is implemented. ${metCount} of ${g.criteria.length} readiness criteria evidenced so far.` : g.title),
      h('span', { class: 'link' }, 'Open checklist and details →'));
  }

  function renderOverview(model, nav) {
    const g = model.global;
    const major = model.milestones.find((m) => m.id === model.summary.major);
    const wave = model.waves.find((w) => w.id === model.summary.activeWave) || null;
    const upcoming = model.summary.upcoming.map((id) => model.milestones.find((m) => m.id === id)).filter(Boolean);
    const hero = h('section', { class: 'card hero' },
      h('button', { class: 'metric', title: 'Open the complete global roadmap', 'aria-label': 'Global progress. Open the complete global roadmap.', onclick: () => nav({ view: 'roadmap' }) },
        svg('svg', { viewBox: '0 0 240 240', class: 'metric-svg', role: 'img', 'aria-hidden': 'true' },
          (() => {
            const mk = (r, v, cls) => { const c = 2 * Math.PI * r; return svg('g', null, svg('circle', { class: 'ring-track', cx: 120, cy: 120, r, 'stroke-width': 16, fill: 'none' }),
              svg('circle', { class: cls, cx: 120, cy: 120, r, 'stroke-width': 16, fill: 'none', 'stroke-linecap': 'round', 'stroke-dasharray': `${(c * Math.max(0, Math.min(100, v || 0)) / 100).toFixed(1)} ${c.toFixed(1)}`, transform: 'rotate(-90 120 120)' })); };
            return svg('g', null, mk(100, g.implementation, 'ring-impl'), mk(76, g.verification, 'ring-ver'),
              svg('text', { class: 'metric-big', x: 120, y: 118, 'text-anchor': 'middle' }, fmtPct(g.implementation)),
              svg('text', { class: 'metric-small', x: 120, y: 146, 'text-anchor': 'middle' }, `${fmtPct(g.verification)} verified`));
          })())),
      h('div', { class: 'hero-text' }, h('h1', null, 'Creator Studio development'),
        h('div', { class: 'legend' }, h('span', { class: 'dot dot-impl' }), `Implementation ${fmtPct(g.implementation)}`, h('span', { class: 'dot dot-ver' }), `Verification ${fmtPct(g.verification)}`),
        h('p', { class: 'muted' }, `${g.milestoneCount} milestones, total weight ${g.totalWeight}. Weights are ${g.weightsStatus.toLowerCase()}${g.weightsStatus === 'PROPOSED' ? ': the percentages are provisional until Lukas reviews them' : ''}. Implementation means code exists; verification means it was demonstrated, and almost nothing is verified on real footage yet.`),
        h('p', { class: 'muted' }, 'These figures are copied from PROJECT_STATUS.json. This app never recalculates the global percentage.'),
        h('button', { class: 'btn', onclick: () => nav({ view: 'roadmap' }) }, 'Open the global roadmap →')));
    const majorCard = card('Current major milestone', major ? h('div', null, h('h3', { class: 'tight' }, h('a', { href: `#/roadmap?m=${major.id}`, class: 'link' }, `${major.id} ${major.title}`)), statePill(major.state),
      h('p', { class: 'muted' }, model.summary.majorWhy), h('p', null, major.implSummary || major.description)) : empty('Every milestone is implemented.'));
    const waveCard = card('Current development wave', wave
      ? h('div', null, h('p', null, link(wave.url, wave.id), ` ${wave.title}`), h('p', { class: 'muted' }, `${wave.done} of ${wave.total} lanes reported done`),
        h('ul', { class: 'plain lanes' }, wave.lanes.map((l) => h('li', null, pill(LANE_LABEL[l.state], l.state), ' ', h('strong', null, l.id), ` ${l.title}`, h('div', { class: 'muted' }, `${(model.agents.find((a) => a.id === l.agent) || { name: l.owner }).name} · ${l.owner}`)))))
      : empty('No wave is running. Waves appear here when a wave branch exists on GitHub.'),
      model.tasks.filter((t) => t.status === 'ACTIVE' || t.status === 'UNDER_REVIEW').length ? h('div', null, h('h3', null, 'Active tasks'), h('ul', { class: 'plain' }, model.tasks.filter((t) => t.status === 'ACTIVE' || t.status === 'UNDER_REVIEW').map((t) => h('li', null, h('strong', null, t.id), ` ${t.title} `, h('span', { class: 'muted' }, t.owner))))) : null);
    const activeAgents = model.agents.filter((a) => a.active);
    const agentsCard = card('Active agents', activeAgents.length ? h('ul', { class: 'plain' }, activeAgents.map((a) => h('li', null, h('a', { class: 'link', href: `#/agent/${a.id}` }, a.name), h('div', { class: 'muted' }, a.current.map((c) => c.title).join('; '))))) : empty('No agent has an active task or lane in the canonical records.'));
    const blockers = model.summary.blockers;
    const blockCard = card(`Blockers and open decisions · ${blockers.length}`, blockers.length ? h('ul', { class: 'plain blockers' }, blockers.slice(0, 7).map((b) => h('li', null, pill(b.kind, b.kind === 'milestone' ? 'blocked' : 'type'), ' ', b.id ? h('strong', null, `${b.id} `) : null, b.text.length > 220 ? b.text.slice(0, 217) + '…' : b.text)))
      : empty('No blockers recorded.'), blockers.length > 7 ? h('p', { class: 'muted' }, `${blockers.length - 7} more in PROJECT_STATUS.json.`) : null);
    const upCard = card('Upcoming milestones', upcoming.length ? h('ul', { class: 'plain' }, upcoming.map((m) => h('li', null, h('a', { class: 'link', href: `#/roadmap?m=${m.id}` }, `${m.id} ${m.title}`), h('div', { class: 'muted' }, `${m.agent ? (model.agents.find((a) => a.id === m.agent) || { name: m.agent }).name : ''} · weight ${m.weight}`)))) : empty('Nothing is ready to start.'));
    const strip = card('Progress by agent', h('div', { class: 'strip' }, model.agents.map((a) => h('a', { class: 'strip-item', href: `#/agent/${a.id}` }, ring(a.stats.implementation, { size: 72, stroke: 8, cls: 'ring-impl', label: a.name }), h('span', null, a.name), h('span', { class: 'muted' }, a.tracked ? `${fmtPct(a.stats.verification)} verified` : 'untracked')))));
    return h('div', { class: 'overview' }, hero, model.gates.length ? h('div', { class: 'gates' }, ...model.gates.map(gateCard)) : null, h('div', { class: 'grid' }, majorCard, waveCard, agentsCard, upCard, blockCard), strip);
  }

  // ------------------------------------------------------------ Orbit (operation / supervisor), read-only
  const FRESH = { live: ['Live', 'done'], cached: ['Cached', 'active'], stale: ['Stale', 'ready'], offline: ['Unavailable', 'blocked'], invalid: ['Invalid', 'blocked'], unconfigured: ['No file', 'upcoming'] };
  const freshPill = (f) => pill(FRESH[f][0], FRESH[f][1]);

  function orbitRow(r) {
    const val = r.label === 'UNAVAILABLE' ? h('span', { class: 'muted' }, `UNAVAILABLE: ${r.reason}`) : h('span', null, r.text);
    return h('tr', { class: `orbit-row orbit-${r.freshness}` }, h('th', { scope: 'row' }, r.name), h('td', null, val),
      h('td', null, h('span', { class: 'muted' }, r.label)), h('td', null, freshPill(r.freshness), ' ', h('span', { class: 'muted' }, r.ageText)));
  }

  function orbitSection(s) {
    const body = [];
    if (s.rows.length) body.push(h('table', { class: 'orbit-table' }, h('tbody', null, s.rows.map(orbitRow))));
    if (s.items.length) {
      body.push(h('ul', { class: 'plain' }, s.items.map((i) => h('li', null, i.text,
        i.status ? h('span', null, ' ', freshPill(i.status.freshness), ' ', h('span', { class: 'muted' }, `${i.status.label === 'UNAVAILABLE' ? 'status unavailable' : i.status.text} · ${i.status.ageText}`)) : null))));
    }
    if (!s.rows.length && !s.items.length) body.push(empty(s.empty || 'Nothing recorded.'));
    if (s.note) body.push(h('p', { class: 'muted' }, s.note));
    return h('section', { class: 'card orbit-card', 'data-orbit-section': s.id }, h('h2', null, s.title, ' ', s.freshness ? freshPill(s.freshness) : null), ...body);
  }

  /** orbit: the view model from src/orbit.js (built in the main process). Choosing a file and re-reading it are the only actions. */
  function renderOrbit(orbit) {
    const o = orbit || { state: 'unconfigured', message: 'Orbit data is not available.', sections: [] };
    const cls = o.state === 'live' ? 'info' : (o.state === 'cached' || o.state === 'stale' || o.state === 'unconfigured') ? 'warn' : 'bad';
    return h('div', { class: 'orbit', id: 'orbit' },
      h('section', { class: 'hero' }, h('div', { class: 'hero-text' }, h('h1', null, 'Orbit: operation and supervisor'),
        h('p', { class: 'muted' }, 'Read-only. This view shows a snapshot file written by the development supervisor. It never changes GitHub, the repository or the supervisor.'))),
      h('div', { class: `banner banner-${cls}`, id: 'orbit-state', 'data-orbit-state': o.state },
        freshPill(o.state), ' ', o.message, o.generatedAtText ? ` Snapshot written ${o.generatedAtText}.` : '', o.fileName ? ` File: ${o.fileName}.` : ''),
      h('p', null, h('button', { class: 'btn primary', id: 'orbit-select', type: 'button', onclick: () => window.dashboard.orbitSelect() }, 'Choose snapshot file…'), ' ',
        h('button', { class: 'btn', id: 'orbit-reload', type: 'button', onclick: () => window.dashboard.orbitReload() }, 'Re-read file')),
      h('div', { class: 'grid' }, (o.sections || []).map(orbitSection)),
      h('section', { class: 'card', id: 'orbit-about' }, h('h2', null, 'About Orbit'),
        h('p', null, 'Orbit is the name of the development supervisor and its observability view. This window is still the Creator Studio Dashboard: file names, release names and identifiers keep their existing names. Account usage is shown only when the snapshot labels it OBSERVED; otherwise it says unavailable and why.')));
  }

  D.views = { renderOrbit, renderOverview, renderAgent, renderRoadmap, renderGate, renderMarkdown, statePill, STATE_LABEL };
})();
