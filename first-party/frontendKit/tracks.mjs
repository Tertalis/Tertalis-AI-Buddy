// file: first-party/frontendKit/tracks.mjs ; version: 1
/**
 * Client-side track mirror. The host is authoritative; this only orders what it was told.
 * Positions are per track and per run. A snapshot at position p contains every Emit up to p.
 */
export const INVOCATIONS_TRACK = 'flow.invocations';
export const MAX_LOG_ITEMS = 2000;

export class TrackStore {
  constructor({maxLogItems = MAX_LOG_ITEMS} = {}) {
    this.maxLogItems = maxLogItems; this.runId = null; this.tracks = new Map(); this.listeners = new Map();
  }
  /** Forget everything from a previous run; listeners stay subscribed and are told. */
  reset(runId) {
    this.runId = runId; const names = [...this.tracks.keys()]; this.tracks.clear();
    for (const name of names) this.notify(name);
  }
  position(name) { return this.tracks.get(name)?.position ?? null; }
  positions() { const result = {}; for (const [name, track] of this.tracks) result[name] = track.position; return result; }
  get(name) { return this.tracks.get(name) ?? null; }
  /** Replace a track with an authoritative snapshot unless it is older than what is held. */
  applySnapshot(snapshot) {
    const held = this.tracks.get(snapshot.track);
    if (held && held.position >= snapshot.position) return false;
    const track = snapshot.style === 'log'
      ? {style: 'log', position: snapshot.position, items: [...snapshot.items].slice(-this.maxLogItems)}
      : {style: 'state', position: snapshot.position, value: snapshot.value};
    this.tracks.set(snapshot.track, track); this.notify(snapshot.track); return true;
  }
  /** Apply the next Emit, ignore an old one, or report a gap that needs a snapshot. */
  apply(emit) {
    const track = this.tracks.get(emit.track);
    if (!track) return 'gap';
    if (emit.position <= track.position) return 'stale';
    if (emit.position !== track.position + 1) return 'gap';
    if (emit.kind === 'set' && track.style === 'state') track.value = emit.value;
    else if (emit.kind === 'append' && track.style === 'log') { track.items.push(emit.value); if (track.items.length > this.maxLogItems) track.items.splice(0, track.items.length - this.maxLogItems); }
    else if (emit.kind === 'reset' && track.style === 'log') track.items = [];
    else return 'gap';
    track.position = emit.position;
    this.notify(emit.track, emit); return 'applied';
  }
  /** Subscribe to one track; the listener receives (track | null, emit | undefined). */
  subscribe(name, listener) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name).add(listener);
    return () => this.listeners.get(name)?.delete(listener);
  }
  /** Like subscribe, but first calls the listener with the current track (null if not yet known). */
  watch(name, listener) { const stop = this.subscribe(name, listener); listener(this.tracks.get(name) ?? null); return stop; }
  notify(name, emit) { for (const listener of this.listeners.get(name) ?? []) listener(this.tracks.get(name) ?? null, emit); }
}
