'use strict';
// Application state: the last-known snapshot, the connection state, and the filming-gate notification. No Electron
// here, so everything is testable. Read-only: the only things written are the app's own snapshot and meta files.
const store = require('./store');
const { syncFromGitHub, SyncError } = require('./sync');
const { buildModel } = require('./model');

const AUTO_REFRESH_MS = 10 * 60 * 1000;

class Controller {
  constructor({ fetchFn, dir, now = () => new Date(), notify = () => {}, onChange = () => {} }) {
    this.fetchFn = fetchFn;
    this.dir = dir;
    this.now = now;
    this.notify = notify;
    this.onChange = onChange;
    this.snapshot = null;
    this.connection = { state: 'never', message: 'Not synchronized yet', lastAttemptAt: null, lastSuccessAt: null, lastError: null };
    this.refreshing = false;
    this.inflight = null;
    this.meta = {};
  }

  init() {
    this.meta = store.loadMeta(this.dir);
    this.snapshot = store.loadSnapshot(this.dir);
    if (this.snapshot) {
      this.connection = { state: 'cached', message: 'Showing the last saved snapshot; not refreshed yet', lastAttemptAt: null,
        lastSuccessAt: this.snapshot.fetchedAt, lastError: null };
    }
    return this.getState();
  }

  getState() {
    const model = this.snapshot ? buildModel(this.snapshot, { now: this.now }) : null;
    return {
      refreshing: this.refreshing,
      hasSnapshot: !!this.snapshot,
      connection: { ...this.connection },
      model,
      attention: model && model.ok ? model.gates.filter((g) => g.display === 'ready').map((g) => ({ id: g.id, title: g.title })) : [],
      appVersion: this.appVersion || null,
    };
  }

  /** One refresh at a time; a second call while one is running returns the same promise. */
  refresh() {
    if (this.inflight) return this.inflight;
    this.refreshing = true;
    this.onChange(this.getState());
    this.inflight = this._refresh().finally(() => { this.refreshing = false; this.inflight = null; this.onChange(this.getState()); });
    return this.inflight;
  }

  async _refresh() {
    const attemptAt = this.now().toISOString();
    try {
      const snap = await syncFromGitHub(this.fetchFn, { now: this.now });
      // a snapshot that cannot be turned into a model is never saved over a good one
      const probe = buildModel(snap, { now: this.now });
      if (!probe.ok) throw new SyncError(`GitHub data is unusable: ${probe.problems.join('; ')}`);
      this.snapshot = snap;
      try { store.saveSnapshot(this.dir, snap); } catch (err) { snap.warnings.push(`Could not save the snapshot for offline use: ${err.message}`); }
      this.connection = { state: 'online', message: 'Synchronized with GitHub', lastAttemptAt: attemptAt, lastSuccessAt: snap.fetchedAt, lastError: null };
      this._checkGates(probe);
    } catch (err) {
      const offline = err instanceof SyncError ? err.offline : true;
      this.connection = {
        state: offline ? 'offline' : 'error',
        message: offline ? 'GitHub cannot be reached. Showing the last saved snapshot.' : `GitHub data could not be used. Showing the last saved snapshot. (${err.message})`,
        lastAttemptAt: attemptAt, lastSuccessAt: this.snapshot ? this.snapshot.fetchedAt : null, lastError: String(err.message || err),
      };
    }
    return this.getState();
  }

  /** Tell Lukas once when a gate becomes ready; the in-window banner stays for as long as it is ready. */
  _checkGates(model) {
    if (!model || !model.ok) return;
    const notified = this.meta.notifiedGates || {};
    let changed = false;
    for (const g of model.gates) {
      if (g.display === 'ready' && !notified[g.id]) {
        try { this.notify({ title: 'Creator Studio: time to film real videos', body: `${g.title}. Open the dashboard for the checklist.`, gateId: g.id }); } catch (e) { /* a failed toast must not break a refresh */ }
        notified[g.id] = this.now().toISOString();
        changed = true;
      } else if (g.display !== 'ready' && notified[g.id]) {
        delete notified[g.id];                      // re-arm if the record is ever reverted
        changed = true;
      }
    }
    if (changed) {
      this.meta.notifiedGates = notified;
      try { store.saveMeta(this.dir, this.meta); } catch (e) { /* non-fatal */ }
    }
  }
}

module.exports = { Controller, AUTO_REFRESH_MS };
