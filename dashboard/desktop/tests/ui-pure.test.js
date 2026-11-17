'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const md = require('../src/markdown');
const road = require('../src/road');
const { isAllowedLink } = require('../src/links');
const { fixtureText } = require('./helpers');

test('markdown: headings, lists, task items, tables and inline formatting become a data tree, never HTML', () => {
  const blocks = md.parse('# Title\n\nText with **bold**, *it*, `code` and [a link](https://github.com/x/y).\n\n- [ ] one\n- [x] two\n\n1. first\n2. second\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n> quote');
  assert.deepEqual(blocks.map((b) => b.t), ['h', 'p', 'list', 'list', 'row', 'row', 'quote']);
  assert.equal(blocks[2].kind, 'task');
  assert.deepEqual(blocks[2].items.map((i) => i.checked), [false, true]);
  assert.equal(blocks[3].kind, 'number');
  assert.equal(blocks[4].cells.length, 2);
  const kinds = blocks[1].c.map((n) => n.t);
  assert.ok(['bold', 'italic', 'code', 'link'].every((k) => kinds.includes(k)));
});

test('markdown: scripts and unsafe links stay inert text', () => {
  const blocks = md.parse('<script>alert(1)</script> [x](javascript:alert(1)) [y](http://insecure.example) [z](relative/path.md)');
  const flat = JSON.stringify(blocks);
  assert.ok(!blocks[0].c.some((n) => n.t === 'link'));
  assert.match(flat, /<script>alert\(1\)<\/script>/);          // present only as a text node's value
  assert.ok(blocks[0].c.every((n) => n.t === 'text'));
  const ok = md.parse('[safe](https://github.com/KasluBuilds/CreatorStudio)');
  assert.equal(ok[0].c[0].t, 'link');
});

test('markdown: the real filming checklist parses into a useful structure', () => {
  const blocks = md.parse(fixtureText('FIRST_REAL_VIDEO_CHECKLIST.md'));
  assert.ok(blocks.length > 5);
  assert.ok(blocks.some((b) => b.t === 'h'));
  assert.ok(blocks.some((b) => b.t === 'list'));
  assert.doesNotThrow(() => md.parse(null));
  assert.deepEqual(md.parse(''), []);
});

test('road: points are spaced, alternate sides, and the path passes through them', () => {
  const l = road.layout(6);
  assert.equal(l.points.length, 6);
  for (let i = 1; i < l.points.length; i++) assert.equal(l.points[i].y - l.points[i - 1].y, road.DEFAULTS.gap);
  assert.ok(l.points.some((p) => p.side === 'left') && l.points.some((p) => p.side === 'right'));
  assert.ok(l.points.every((p) => p.x > 0 && p.x < l.width));
  assert.match(l.path, /^M [\d.-]+ [\d.-]+( C [\d., -]+)+$/);
  assert.ok(l.height > l.points[5].y);
});

test('road: phase signposts use their own spacing and do not shift the curve of the milestones', () => {
  const plain = road.layout([{ kind: 'milestone' }, { kind: 'milestone' }, { kind: 'milestone' }]);
  const withPhase = road.layout([{ kind: 'phase' }, { kind: 'milestone' }, { kind: 'milestone' }, { kind: 'phase' }, { kind: 'milestone' }]);
  const ms = withPhase.points.filter((p) => p.kind === 'milestone');
  assert.deepEqual(ms.map((p) => Math.round(p.x)), plain.points.map((p) => Math.round(p.x)));
  assert.equal(withPhase.points[1].y - withPhase.points[0].y, 96);
});

test('road: empty and single-milestone roads do not throw', () => {
  assert.equal(road.layout(0).points.length, 0);
  assert.equal(road.layout(1).points.length, 1);
  assert.equal(road.smoothPath([]), '');
});

test('links: only this project\'s GitHub pages may be opened', () => {
  for (const ok of ['https://github.com/KasluBuilds/CreatorStudio', 'https://github.com/KasluBuilds/CreatorStudio/commit/abc', 'https://github.com/KasluBuilds/CreatorStudio/releases/tag/v1',
    'https://raw.githubusercontent.com/KasluBuilds/CreatorStudio/main/README.md']) assert.equal(isAllowedLink(ok), true, ok);
  for (const bad of ['http://github.com/KasluBuilds/CreatorStudio', 'https://github.com/other/repo', 'https://github.com.evil.example/KasluBuilds/CreatorStudio', 'https://evil.example/https://github.com/KasluBuilds/CreatorStudio',
    'file:///C:/Windows/System32/calc.exe', 'javascript:alert(1)', '', null, undefined, 42, 'https://github.com/KasluBuilds/CreatorStudio-evil', `https://github.com/KasluBuilds/CreatorStudio/${'a'.repeat(3000)}`]) assert.equal(isAllowedLink(bad), false, String(bad));
});
