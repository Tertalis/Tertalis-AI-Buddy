// file: first-party/frontendKit/stream.mjs ; version: 1
/** One resumable Emit stream per View, with a single outstanding connection and generation guard. */
import {ConnectionError, requestOptions} from './http.mjs';
import {readEvents} from './eventStream.mjs';

const FATAL = {401: 'unauthorized', 403: 'forbidden', 404: 'unavailable'};
export const RETRY_DELAYS = [250, 1000, 2000, 5000];

export function streamUrl(origin, tracks, positions, runId = null) {
  const params = new URLSearchParams();
  params.set('tracks', tracks.join(','));
  if (runId) params.set('run', runId);
  for (const [name, position] of Object.entries(positions)) params.append('after', name + ':' + position);
  return origin + '/stream?' + params.toString();
}

export class StreamClient {
  constructor({origin, credential, tracks, positions, runId = () => null, onEvent, onState, fetcher = (...args) => globalThis.fetch(...args),
               schedule = (callback, delay) => globalThis.setTimeout(callback, delay), cancel = id => globalThis.clearTimeout(id),
               retryDelays = RETRY_DELAYS}) {
    Object.assign(this, {origin, credential, tracks, positions, runId, onEvent, onState, fetcher, schedule, cancel, retryDelays});
    this.generation = 0; this.running = false; this.controller = null; this.timer = null; this.failures = 0;
  }
  start() { if (this.running) return; this.running = true; this.connect(); }
  close() {
    this.running = false; this.generation++; this.controller?.abort(); this.controller = null;
    if (this.timer !== null) this.cancel(this.timer); this.timer = null; this.onState('closed');
  }
  async connect() {
    const generation = ++this.generation;
    this.controller = new AbortController(); this.onState('connecting');
    let code = 'disconnected';
    try {
      const response = await this.fetcher(streamUrl(this.origin, this.tracks, this.positions(), this.runId()),
        requestOptions(this.credential, {method: 'GET', signal: this.controller.signal, headers: {Accept: 'text/event-stream'}}));
      if (!response.ok) {
        await response.body?.cancel();
        throw new ConnectionError(FATAL[response.status] ?? (response.status === 429 ? 'busy' : 'disconnected'));
      }
      if (!response.headers.get('content-type')?.toLowerCase().startsWith('text/event-stream') || !response.body) throw new ConnectionError('invalid-stream');
      const reader = response.body.getReader();
      try {
        for await (const event of readEvents(reader)) {
          if (generation !== this.generation) return;
          if (this.failures) this.failures = 0;
          if (event.event === 'run') this.onState('connected');
          this.onEvent(event);
        }
      } finally { reader.releaseLock(); }
    } catch (error) {
      code = error?.code ?? 'disconnected';
    }
    if (generation !== this.generation || !this.running) return;
    if (code === 'unauthorized' || code === 'forbidden' || code === 'unavailable') { this.running = false; this.onState(code); return; }
    this.onState(code);
    const delay = this.retryDelays[Math.min(this.failures, this.retryDelays.length - 1)] * (code === 'busy' ? 2 : 1);
    this.failures++;
    this.timer = this.schedule(() => { this.timer = null; if (this.running && generation === this.generation) this.connect(); }, delay);
  }
}
