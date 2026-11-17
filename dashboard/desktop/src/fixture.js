'use strict';
// A fake GitHub for tests and for `--fixture=<checkout>`: answers the same three kinds of request the app makes (ref
// advertisement, raw file at a commit, latest release) from local data. It lets the real application, including the
// packaged one, run end to end without the network and without depending on what is currently on GitHub.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

function pkt(s) { return (s.length + 4).toString(16).padStart(4, '0') + s; }

/** The smart-HTTP v0 advertisement for { 'refs/heads/main': sha, ... }. */
function pktLines(refs) {
  const names = Object.keys(refs);
  let out = pkt('# service=git-upload-pack\n') + '0000';
  names.forEach((n, i) => { out += pkt(`${refs[n]} ${n}${i === 0 ? '\0multi_ack thin-pack side-band agent=git/fixture' : ''}\n`); });
  return out + '0000';
}

function response(status, body) {
  return { status, ok: status >= 200 && status < 300, text: async () => body, json: async () => JSON.parse(body) };
}

/**
 * options: { root, mainSha, refs, files (override map path -> text), waves: { id: { sha, files: { path: text } } },
 *            release, fail }  `fail` makes every request reject (offline) or answer a status (number).
 */
function makeFixtureFetch(options) {
  const { root, files = {}, waves = {}, release = null, fail = null, extraRefs = {} } = options;
  const mainSha = options.mainSha || crypto.createHash('sha1').update(String(root)).digest('hex');
  const refs = { 'refs/heads/main': mainSha, ...extraRefs };
  for (const [id, w] of Object.entries(waves)) refs[`refs/heads/wave/${id}`] = w.sha;
  const calls = [];
  const fetchFn = async (url) => {
    calls.push(url);
    if (fail === 'offline') throw new TypeError('fetch failed');
    if (typeof fail === 'number') return response(fail, '');
    let m;
    if (/\/info\/refs\?service=git-upload-pack$/.test(url)) return response(200, pktLines(refs));
    if ((m = /^https:\/\/raw\.githubusercontent\.com\/[^/]+\/[^/]+\/([0-9a-f]{40})\/(.+)$/.exec(url))) {
      const sha = m[1]; const rel = decodeURIComponent(m[2]);
      const wave = Object.values(waves).find((w) => w.sha === sha);
      if (wave) return wave.files[rel] !== undefined ? response(200, wave.files[rel]) : response(404, '');
      if (files[rel] !== undefined) return response(files[rel] === null ? 404 : 200, files[rel] === null ? '' : files[rel]);
      if (root) { const p = path.join(root, rel); if (fs.existsSync(p) && fs.statSync(p).isFile()) return response(200, fs.readFileSync(p, 'utf8')); }
      return response(404, '');
    }
    if (/\/releases\/latest$/.test(url)) return release ? response(200, JSON.stringify(release)) : response(404, '');
    return response(404, '');
  };
  fetchFn.calls = calls;
  fetchFn.mainSha = mainSha;
  return fetchFn;
}

module.exports = { makeFixtureFetch, pktLines, pkt };
