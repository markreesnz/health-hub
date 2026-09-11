/* Single save queue with a persisted base for safe, offline three-way merging. */
(function (root) {
  'use strict';
  const copy = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const mutationId = () => typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('');
  const object = x => x !== null && typeof x === 'object' && !Array.isArray(x);

  function merge(base, local, remote, path = '', conflicts = [], preference = null) {
    if (equal(local, base)) return copy(remote);
    if (equal(remote, base) || equal(local, remote)) return copy(local);
    if (path === 'savedAt') return remote;
    if (object(local) && object(remote) && (base === undefined || object(base))) {
      const result = Object.create(null);
      for (const key of new Set([...Object.keys(base || {}), ...Object.keys(local), ...Object.keys(remote)])) {
        const value = merge((base || {})[key], local[key], remote[key], path ? path + '.' + key : key, conflicts, preference);
        if (value !== undefined) result[key] = value;
      }
      return result;
    }
    const identity = path === 'transactions' ? 'id' : path === 'snapshots' ? 'date' : null;
    if (identity && Array.isArray(local) && Array.isArray(remote) && Array.isArray(base)) {
      const index = rows => Object.fromEntries(rows.map(row => [row[identity], row]));
      return Object.values(merge(index(base), index(local), index(remote), path + '.rows', conflicts, preference));
    }
    conflicts.push(path);
    return copy(preference === 'remote' ? remote : local);
  }

  class Client {
    constructor(options) {
      Object.assign(this, options);
      this.revision = null;
      this.base = null;
      this.local = copy(options.initial);
      this.ready = false;
      this.running = null;
      this.timer = null;
      this.conflict = null;
      this.pending = null;
      try {
        const cache = JSON.parse(this.storage.getItem(this.key));
        if (cache && Number.isInteger(cache.revision) && cache.base && cache.local) {
          this.revision = cache.revision; this.base = cache.base; this.local = cache.local;
        }
      } catch (_) {}
    }
    persist() {
      try {
        this.storage.setItem(this.key, JSON.stringify({revision: this.revision, base: this.base, local: this.local}));
        return true;
      } catch (_) {
        this.onStatus('Storage full — keep this page open and export a backup', true);
        return false;
      }
    }
    change(state) {
      this.local = copy(state);
      this.persist();
      this.onStatus('Changes waiting to save', true);
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.sync(), 750);
    }
    async request(path, payload) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 30000);
      try {
        const response = await this.fetch(this.api + path, {
          method: payload ? 'POST' : 'GET', cache: 'no-store', signal: controller.signal,
          headers: {'Content-Type': 'application/json', 'X-Finance-Client': '2.0.1'},
          ...(payload ? {body: JSON.stringify(payload)} : {})
        });
        const data = await response.json();
        if (!response.ok && !(response.status === 409 && data.current)) throw new Error(data.error || 'Server unavailable');
        return data.current || data;
      } finally { clearTimeout(timeout); }
    }
    accept(remote, preference = null) {
      if (!Number.isInteger(remote.revision) || !Object.prototype.hasOwnProperty.call(remote, 'state')) throw new Error('Invalid server response');
      if (remote.state === null) {
        if (remote.revision !== 0 || this.base) throw new Error('Server state is missing; recovery is required');
        this.base = copy(this.local); this.revision = 0; this.ready = true;
        this.onEmpty();
        return;
      }
      const conflicts = [];
      let merged;
      if (this.base === null) {
        // Legacy caches cannot establish which edits are newer. Preserve a recovery copy,
        // then adopt the server; never union an unversioned browser into the shared ledger.
        try { this.storage.setItem(this.key + '-legacy-recovery', JSON.stringify(this.local)); } catch (_) {}
        merged = copy(remote.state);
      } else merged = merge(this.base, this.local, remote.state, '', conflicts, preference);
      if (conflicts.length && !preference) {
        this.conflict = remote;
        this.onConflict(conflicts);
        this.onStatus('Changes need review before saving', true);
        return false;
      }
      this.base = copy(remote.state); this.local = merged; this.revision = remote.revision;
      this.ready = true; this.conflict = null;
      this.persist(); this.onState(copy(this.local));
      return true;
    }
    resolve(preference) {
      if (!this.conflict) return;
      this.accept(this.conflict, preference);
      return this.sync();
    }
    sync() {
      if (this.running) return this.running;
      this.running = this.run().finally(() => { this.running = null; });
      return this.running;
    }
    async run() {
      clearTimeout(this.timer);
      if (this.conflict) return false;
      try {
        // Retry the identical mutation after an ambiguous response before pulling/rebasing.
        if (this.pending) {
          const reply = await this.request('/state', this.pending);
          if (reply.mutationId === this.pending.mutationId) this.base = copy(this.pending.state);
          if (!this.accept(reply)) return false;
          this.pending = null;
        }
        const remote = await this.request('/state');
        if (this.accept(remote) === false) return false;
        if (remote.state === null) return false; // explicit initialise/import action only
        // Bound continuous contention; never spin indefinitely if several tabs are active.
        for (let attempt = 0; attempt < 5; attempt++) {
          if (equal(this.local, this.base)) {
            this.onStatus('Saved to Home Assistant', false); return true;
          }
          this.pending = {revision: this.revision, state: copy(this.local), mutationId: mutationId()};
          const sent = this.pending.state;
          const reply = await this.request('/state', this.pending);
          if (reply.mutationId === this.pending.mutationId) {
            // Local edits made during the POST are changes relative to the sent state.
            this.base = sent;
          }
          if (!this.accept(reply)) { this.pending = null; return false; }
          this.pending = null;
        }
        throw new Error('Another device is saving; retry shortly');
      } catch (error) {
        this.onStatus('Not saved to Home Assistant — ' + error.message, true);
        this.timer = setTimeout(() => this.sync(), 15000);
        return false;
      }
    }
    async initialise() {
      if (this.revision !== 0) return false;
      this.pending = {revision: 0, state: copy(this.local), mutationId: mutationId()};
      return this.sync();
    }
  }
  const api = {merge, Client};
  if (typeof module !== 'undefined') module.exports = api;
  root.FinanceSync = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
