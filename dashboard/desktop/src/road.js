'use strict';
// Layout of the winding road. Pure geometry so it can be tested: given N milestones it returns a smooth path (as SVG
// path data), a point and a label side for every milestone, and the overall size. The road runs top to bottom in
// gentle S-curves; milestones sit on the road centre line.

const DEFAULTS = { width: 980, top: 110, gap: 150, amplitude: 250, bottom: 130 };

/** Catmull-Rom to cubic Bezier through the points: a smooth curve that passes through every milestone. */
function smoothPath(points) {
  if (points.length === 0) return '';
  if (points.length === 1) return `M ${points[0].x.toFixed(1)} ${points[0].y.toFixed(1)}`;
  let d = `M ${points[0].x.toFixed(1)} ${points[0].y.toFixed(1)}`;
  for (let i = 0; i < points.length - 1; i++) {
    const p0 = points[i - 1] || points[i];
    const p1 = points[i];
    const p2 = points[i + 1];
    const p3 = points[i + 2] || p2;
    const c1 = { x: p1.x + (p2.x - p0.x) / 6, y: p1.y + (p2.y - p0.y) / 6 };
    const c2 = { x: p2.x - (p3.x - p1.x) / 6, y: p2.y - (p3.y - p1.y) / 6 };
    d += ` C ${c1.x.toFixed(1)} ${c1.y.toFixed(1)}, ${c2.x.toFixed(1)} ${c2.y.toFixed(1)}, ${p2.x.toFixed(1)} ${p2.y.toFixed(1)}`;
  }
  return d;
}

/**
 * items: a number (that many milestones) or an array of { kind: 'milestone' | 'phase' }. Milestones are spaced `gap`
 * apart and phase signposts `phaseGap`, all on one S-shaped road.
 */
function layout(items, opts = {}) {
  const o = { ...DEFAULTS, phaseGap: 96, ...opts };
  const list = typeof items === 'number' ? Array.from({ length: items }, () => ({ kind: 'milestone' })) : items;
  const cx = o.width / 2;
  const points = [];
  let y = o.top;
  let n = 0;                                    // index among milestones: drives the sine, so signposts do not shift the curve
  list.forEach((it, i) => {
    const phase = it.kind === 'phase';
    if (i > 0) y += phase ? o.phaseGap : (list[i - 1].kind === 'phase' ? o.phaseGap : o.gap);
    const t = phase ? n - 0.5 : n;
    const x = cx + o.amplitude * Math.sin(t * Math.PI / 2.2 + 0.4);
    points.push({ x, y, kind: it.kind || 'milestone', side: x >= cx ? 'left' : 'right' });
    if (!phase) n += 1;
  });
  const first = points[0] || { x: cx, y: o.top };
  const last = points[points.length - 1] || first;
  const ext = [{ x: first.x + (first.x < cx ? -90 : 90), y: first.y - 70 }, ...points, { x: last.x + (last.x < cx ? 90 : -90), y: last.y + 80 }];
  return { points, path: smoothPath(ext), width: o.width, height: (last.y || o.top) + o.bottom, options: o };
}

const roadApi = { layout, smoothPath, DEFAULTS };
if (typeof module !== 'undefined' && module.exports) module.exports = roadApi;
else { (window.Dashboard = window.Dashboard || {}).road = roadApi; }
