// file: first-party/frontendKit/session.mjs ; version: 2
/** Assembles a View session: descriptor, track mirror, resumable stream, and invocations. */
import {ConnectionError, readBoundedJson, requestOptions} from './http.mjs';
import {StreamClient} from './stream.mjs';
import {INVOCATIONS_TRACK, TrackStore} from './tracks.mjs';
import {InvocationClient} from './invoke.mjs';

export async function fetchJson(fetcher, url, credential) {
  const response = await fetcher(url, requestOptions(credential, {method: 'GET'}));
  if (!response.ok) { await response.body?.cancel(); throw new ConnectionError(response.status === 401 ? 'unauthorized' : 'unavailable'); }
  return readBoundedJson(response);
}

export async function openSession({origin, credential, fetcher = (...args) => globalThis.fetch(...args), schedule, cancel}) {
  const descriptor = await fetchJson(fetcher, origin + '/session', credential);
  const store = new TrackStore();
  const invocations = new InvocationClient({origin, credential, fetcher});
  const states = new Set(); const snapshotting = new Map(); const retries = new Map();
  const defer = schedule ?? ((fn, delay) => globalThis.setTimeout(fn, delay));
  const undefer = cancel ?? (id => globalThis.clearTimeout(id));
  let current = 'connecting', closed = false;
  const names = [...descriptor.tracks.map(track => track.name), INVOCATIONS_TRACK];
  store.runId = descriptor.runId;

  async function snapshot(name) {
    if (closed) return;
    const previous = snapshotting.get(name);
    if (previous?.runId === store.runId) { previous.again = true; return; }
    if (retries.has(name)) { undefer(retries.get(name)); retries.delete(name); }
    const request = {runId: store.runId, again: false};
    snapshotting.set(name, request);
    try {
      const value = await fetchJson(fetcher, origin + '/snapshot?track=' + encodeURIComponent(name), credential);
      if (closed || request.runId !== store.runId || value.runId !== store.runId) return;
      if (store.applySnapshot(value) && name === INVOCATIONS_TRACK) for (const item of value.items) invocations.observe(item);
    } catch { request.again = true; }
    finally {
      if (snapshotting.get(name) === request) {
        snapshotting.delete(name);
        if (!closed && request.again && request.runId === store.runId) {
          retries.set(name, defer(() => { retries.delete(name); snapshot(name); }, 250));
        }
      }
    }
  }
  function stop() {
    closed = true;
    for (const timer of retries.values()) undefer(timer);
    retries.clear();
    invocations.runChanged();
  }
  function onEvent({event, data}) {
    if (event === 'run') {
      if (data.runId !== store.runId) { store.reset(data.runId); invocations.runChanged(); }
      for (const name of names) if (store.position(name) === null) snapshot(name);
    } else if (event === 'emit') {
      const outcome = store.apply(data);
      if (outcome === 'gap') snapshot(data.track);
      else if (outcome === 'applied' && data.track === INVOCATIONS_TRACK && data.kind === 'append') invocations.observe(data.value);
    } else if (event === 'gap') snapshot(data.track);
  }
  const stream = new StreamClient({origin, credential, tracks: names, positions: () => store.positions(), runId: () => store.runId, onEvent,
    onState: state => {
      if (['unauthorized', 'forbidden', 'unavailable', 'closed'].includes(state)) stop();
      current = state; for (const listener of states) listener(state);
    }, fetcher,
    ...(schedule ? {schedule} : {}), ...(cancel ? {cancel} : {})});
  stream.start();
  return Object.freeze({
    descriptor, tracks: store,
    get runId() { return store.runId; },
    invoke: (target, args, options) => invocations.invoke(target, args, options),
    /** Calls the listener now with the current connection state, then on every change. */
    onState(listener) { states.add(listener); listener(current); return () => states.delete(listener); },
    close() { stream.close(); invocations.runChanged(); },
  });
}
