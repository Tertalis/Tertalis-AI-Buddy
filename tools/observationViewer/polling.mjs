// file: tools/observationViewer/polling.mjs ; version: 2
/** Read-only transport and generation-safe polling, independent of graphs and DOM. */
import {parseSnapshot, SchemaError} from './model.mjs';
// Shared helpers live in the frontend kit Pack. This relative path resolves on disk (Node) and,
// in the browser, clamps at the URL root to the fixed asset /first-party/frontendKit/http.mjs.
import {MAX_BODY, ConnectionError, isCredential, readBoundedJson, requestOptions, requireLoopbackOrigin} from '../../first-party/frontendKit/http.mjs';
export {MAX_BODY, ConnectionError};

/** Accept only an explicit G-01 literal-loopback endpoint and selected opaque publication. */
export function connectionConfig(endpoint, publication, credential) {
  try {
    const url = requireLoopbackOrigin(endpoint);
    let selected = publication;
    if (publication.startsWith('http')) {
      const target = new URL(publication);
      if (target.origin !== url.origin || target.search || target.hash || target.username || target.password) throw new Error();
      selected = target.pathname.replace(/^\/publications\//, '');
    }
    if (!/^[0-9a-f]{32}$/.test(selected) || !isCredential(credential)) throw new Error();
    return Object.freeze({url: url.origin + '/publications/' + selected, publicationId: selected, credential});
  } catch { throw new ConnectionError('invalid-connection'); }
}
/** Read a response incrementally up to the accepted bridge limit; no unbounded text(). */
const boundedJson = response => readBoundedJson(response, {limit: MAX_BODY, invalid: () => new SchemaError()});
/** Fetch one authorized snapshot only; no discovery, source mutation or raw error propagation. */
export async function readPublication(config, signal, fetcher = globalThis.fetch) {
  const response = await fetcher(config.url, requestOptions(config.credential, {method: 'GET', signal}));
  if (!response.ok) {
    await response.body?.cancel();
    throw new ConnectionError(({401: 'unauthorized', 403: 'forbidden', 404: 'withdrawn', 410: 'revoked', 503: 'unavailable'})[response.status] ?? 'disconnected');
  }
  const model = parseSnapshot(await boundedJson(response));
  if (model.publicationId !== config.publicationId) throw new SchemaError();
  return model;
}

/** One outstanding request including across reconnects; old responses are never accepted. */
export class Poller {
  constructor({onModel, onState, read = readPublication, schedule = (callback, delay) => globalThis.setTimeout(callback, delay), cancel = id => globalThis.clearTimeout(id), interval = 1000, timeout = 5000}) {
    this.onModel = onModel; this.onState = onState; this.read = read; this.schedule = schedule; this.cancel = cancel;
    this.interval = Math.max(250, interval); this.timeout = Math.max(250, timeout);
    this.generation = 0; this.pending = false; this.running = false; this.config = null; this.timer = null; this.controller = null;
  }
  /** Start an explicitly supplied connection, cancelling old work before replacing credentials. */
  connect(config) { this.stopWork(); this.config = config; this.running = true; this.onState('connecting'); this.tick(); }
  /** Stop just this viewer's polling and retain its in-memory connection/evidence for resume. */
  pause() { this.stopWork(); this.onState('paused'); }
  /** Resume only with an existing explicit connection; no source subscription is acquired. */
  resume() { if (!this.config || this.running) return; this.running = true; this.onState('connecting'); this.tick(); }
  /** Clear credentials and cancel local transport only; no bridge/source mutation. */
  disconnect() { this.stopWork(); this.config = null; this.onState('disconnected'); }
  /** Invalidate both pending and scheduled generations without starting another concurrent request. */
  stopWork() {
    this.running = false; this.generation++; this.controller?.abort();
    if (this.timer !== null) this.cancel(this.timer); this.timer = null;
  }
  /** Complete or settle aborted work before scheduling exactly one subsequent request. */
  async tick() {
    if (this.pending || !this.running || !this.config) return;
    const generation = this.generation, config = this.config;
    this.pending = true; this.controller = new AbortController();
    const timeout = this.schedule(() => this.controller?.abort(), this.timeout);
    try {
      const model = await this.read(config, this.controller.signal);
      if (generation === this.generation && this.running) { this.onModel(model); this.onState('connected'); }
    } catch (error) {
      if (generation === this.generation && this.running) {
        const code = error instanceof SchemaError ? 'invalid-schema' : error instanceof ConnectionError ? error.code : 'disconnected';
        if (['unauthorized', 'forbidden', 'withdrawn', 'revoked', 'invalid-schema'].includes(code)) this.running = false;
        this.onState(code);
      }
    } finally {
      this.cancel(timeout); this.pending = false; this.controller = null;
      if (this.running && this.config) this.timer = this.schedule(() => { this.timer = null; this.tick(); }, generation === this.generation ? this.interval : 0);
    }
  }
}
