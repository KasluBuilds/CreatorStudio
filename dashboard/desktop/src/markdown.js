'use strict';
// A small, safe Markdown subset parser for the filming checklist: headings, paragraphs, bullet and numbered lists, task
// list checkboxes, tables (as plain rows), bold, italic, inline code and links. It produces a plain data tree, never
// HTML, so the renderer builds DOM nodes with textContent and nothing from the repository can inject markup.

const SAFE_LINK = /^https:\/\/[^\s]+$/;

function parseInline(text) {
  const out = [];
  let i = 0;
  let buf = '';
  const flush = () => { if (buf) { out.push({ t: 'text', v: buf }); buf = ''; } };
  while (i < text.length) {
    const rest = text.slice(i);
    let m;
    if ((m = /^`([^`]+)`/.exec(rest))) { flush(); out.push({ t: 'code', v: m[1] }); i += m[0].length; continue; }
    if ((m = /^\*\*([^*]+)\*\*/.exec(rest))) { flush(); out.push({ t: 'bold', c: parseInline(m[1]) }); i += m[0].length; continue; }
    if ((m = /^\*([^*\s][^*]*)\*/.exec(rest))) { flush(); out.push({ t: 'italic', c: parseInline(m[1]) }); i += m[0].length; continue; }
    if ((m = /^\[([^\]]+)\]\(([^)\s]+)\)/.exec(rest))) {
      flush();
      if (SAFE_LINK.test(m[2])) out.push({ t: 'link', href: m[2], c: parseInline(m[1]) });
      else out.push({ t: 'text', v: m[1] });       // relative or unsafe links keep their text only
      i += m[0].length; continue;
    }
    buf += text[i];
    i += 1;
  }
  flush();
  return out;
}

function parse(md) {
  const blocks = [];
  const lines = String(md || '').replace(/\r\n/g, '\n').split('\n');
  let para = [];
  let list = null;
  const endPara = () => { if (para.length) { blocks.push({ t: 'p', c: parseInline(para.join(' ')) }); para = []; } };
  const endList = () => { if (list) { blocks.push(list); list = null; } };
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, '');
    let m;
    if (!line.trim()) { endPara(); endList(); continue; }
    if ((m = /^(#{1,4})\s+(.*)$/.exec(line))) { endPara(); endList(); blocks.push({ t: 'h', level: m[1].length, c: parseInline(m[2]) }); continue; }
    if ((m = /^\s*[-*]\s+\[([ xX])\]\s+(.*)$/.exec(line))) {
      endPara();
      if (!list || list.kind !== 'task') { endList(); list = { t: 'list', kind: 'task', items: [] }; }
      list.items.push({ checked: m[1] !== ' ', c: parseInline(m[2]) });
      continue;
    }
    if ((m = /^\s*[-*]\s+(.*)$/.exec(line))) {
      endPara();
      if (!list || list.kind !== 'bullet') { endList(); list = { t: 'list', kind: 'bullet', items: [] }; }
      list.items.push({ c: parseInline(m[1]) });
      continue;
    }
    if ((m = /^\s*(\d+)[.)]\s+(.*)$/.exec(line))) {
      endPara();
      if (!list || list.kind !== 'number') { endList(); list = { t: 'list', kind: 'number', items: [] }; }
      list.items.push({ c: parseInline(m[2]) });
      continue;
    }
    if (/^\|.*\|$/.test(line)) {
      endPara(); endList();
      if (/^\|[\s:|-]+\|$/.test(line)) continue;
      blocks.push({ t: 'row', cells: line.slice(1, -1).split('|').map((c) => parseInline(c.trim())) });
      continue;
    }
    if (/^\s*>\s?/.test(line)) { endPara(); endList(); blocks.push({ t: 'quote', c: parseInline(line.replace(/^\s*>\s?/, '')) }); continue; }
    if (list && /^\s+\S/.test(line)) { list.items[list.items.length - 1].c.push({ t: 'text', v: ' ' }, ...parseInline(line.trim())); continue; }
    endList();
    para.push(line.trim());
  }
  endPara(); endList();
  return blocks;
}

const markdownApi = { parse, parseInline };
if (typeof module !== 'undefined' && module.exports) module.exports = markdownApi;
else { (window.Dashboard = window.Dashboard || {}).markdown = markdownApi; }
