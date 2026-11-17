'use strict';
// Read-only GitHub access for a PUBLIC repository. No login, no token, no credentials anywhere in this file.
//   - ref listing:  https://github.com/<owner>/<repo>/info/refs?service=git-upload-pack  (smart-HTTP advertisement; not
//     subject to the REST API's 60 requests per hour limit), with the REST API as a fallback;
//   - file content: https://raw.githubusercontent.com/<owner>/<repo>/<commit sha>/<path>  (pinned to a commit, so one
//     refresh sees one consistent state of the repository);
//   - update check: the public "latest release" endpoint.
// Every function takes the fetch implementation as a parameter so tests never touch the network.

const REPO = Object.freeze({ owner: 'KasluBuilds', name: 'CreatorStudio', branch: 'main' });
const REPO_URL = `https://github.com/${REPO.owner}/${REPO.name}`;
const TIMEOUT_MS = 15000;

class GitHubError extends Error {
  constructor(message, { status = null, offline = false } = {}) {
    super(message);
    this.name = 'GitHubError';
    this.status = status;
    this.offline = offline;
  }
}

async function request(fetchFn, url, { headers = {}, timeoutMs = TIMEOUT_MS, retries = 1 } = {}) {
  let lastErr;
  for (let attempt = 0; attempt <= retries; attempt++) {
    const ctl = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = ctl ? setTimeout(() => ctl.abort(), timeoutMs) : null;
    try {
      const res = await fetchFn(url, { headers: { 'User-Agent': 'CreatorStudioDashboard', ...headers }, signal: ctl ? ctl.signal : undefined });
      if (timer) clearTimeout(timer);
      if (res.status === 404) return { status: 404, res };
      if (res.status === 403 || res.status === 429) {
        throw new GitHubError(`GitHub refused the request (HTTP ${res.status}): rate limit or access policy`, { status: res.status });
      }
      if (!res.ok) throw new GitHubError(`GitHub answered HTTP ${res.status} for ${redact(url)}`, { status: res.status });
      return { status: res.status, res };
    } catch (err) {
      if (timer) clearTimeout(timer);
      lastErr = err instanceof GitHubError ? err
        : new GitHubError(err && err.name === 'AbortError' ? `Timed out after ${timeoutMs / 1000}s: ${redact(url)}`
          : `Network error: ${(err && err.message) || err}`, { offline: true });
      if (lastErr.status) break;               // an HTTP answer is not retried
    }
  }
  throw lastErr;
}

function redact(url) {
  return String(url).replace(/(token|access_token|key)=[^&]+/gi, '$1=***');
}

// ---------------------------------------------------------------- refs
/** Parse the git smart-HTTP ref advertisement (protocol v0). Returns { 'refs/heads/main': '<sha>', ... }. */
function parseInfoRefs(text) {
  const refs = {};
  let i = 0;
  while (i + 4 <= text.length) {
    const len = parseInt(text.slice(i, i + 4), 16);
    if (Number.isNaN(len)) throw new GitHubError('Malformed ref advertisement from GitHub');
    if (len === 0) { i += 4; continue; }
    const payload = text.slice(i + 4, i + len);
    i += len;
    if (payload.startsWith('#')) continue;
    const line = payload.split('\0')[0].replace(/\n$/, '');
    const sp = line.indexOf(' ');
    if (sp !== 40) continue;
    const sha = line.slice(0, 40);
    const name = line.slice(41);
    if (!/^[0-9a-f]{40}$/.test(sha) || !name.startsWith('refs/') || name.endsWith('^{}')) continue;
    refs[name] = sha;
  }
  return refs;
}

async function fetchRefsViaGit(fetchFn) {
  const { res } = await request(fetchFn, `${REPO_URL}/info/refs?service=git-upload-pack`);
  return parseInfoRefs(await res.text());
}

async function fetchRefsViaApi(fetchFn) {
  const { res, status } = await request(fetchFn, `https://api.github.com/repos/${REPO.owner}/${REPO.name}/git/matching-refs/heads/`,
    { headers: { Accept: 'application/vnd.github+json' } });
  if (status === 404) throw new GitHubError('Repository not found (is it still public?)', { status: 404 });
  const list = await res.json();
  if (!Array.isArray(list)) throw new GitHubError('Unexpected answer from the GitHub API');
  const refs = {};
  for (const r of list) if (r && typeof r.ref === 'string' && r.object && /^[0-9a-f]{40}$/.test(r.object.sha)) refs[r.ref] = r.object.sha;
  return refs;
}

async function fetchRefs(fetchFn) {
  try {
    const refs = await fetchRefsViaGit(fetchFn);
    if (Object.keys(refs).length) return { refs, via: 'git' };
  } catch (err) {
    if (err.offline) throw err;               // no network at all: do not try again through another door
  }
  return { refs: await fetchRefsViaApi(fetchFn), via: 'api' };
}

// ---------------------------------------------------------------- files
function safePath(p) {
  if (typeof p !== 'string' || !p || p.startsWith('/') || p.includes('..') || p.includes('\\') || /[?#]/.test(p)) {
    throw new GitHubError(`Refusing an unsafe repository path: ${p}`);
  }
  return p.split('/').map(encodeURIComponent).join('/');
}

/** Raw text of a repository file at a commit, or null when the file does not exist there. */
async function fetchFile(fetchFn, sha, path) {
  if (!/^[0-9a-f]{40}$/.test(sha)) throw new GitHubError('A full commit SHA is required to read files');
  const url = `https://raw.githubusercontent.com/${REPO.owner}/${REPO.name}/${sha}/${safePath(path)}`;
  const { res, status } = await request(fetchFn, url);
  if (status === 404) return null;
  return res.text();
}

async function fetchLatestRelease(fetchFn) {
  try {
    const { res, status } = await request(fetchFn, `https://api.github.com/repos/${REPO.owner}/${REPO.name}/releases/latest`,
      { headers: { Accept: 'application/vnd.github+json' }, retries: 0 });
    if (status === 404) return null;
    const j = await res.json();
    return j && typeof j.tag_name === 'string' ? { tag: j.tag_name, url: typeof j.html_url === 'string' ? j.html_url : `${REPO_URL}/releases` } : null;
  } catch (err) {
    return null;                              // an update check must never break a refresh
  }
}

module.exports = { REPO, REPO_URL, GitHubError, parseInfoRefs, fetchRefs, fetchRefsViaGit, fetchRefsViaApi, fetchFile, fetchLatestRelease, safePath, request };
