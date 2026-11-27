// file: first-party/frontendKit/invoke.mjs ; version: 1
/** Invocations with stable client ids; Results arrive later on the invocations track. */
import {readBoundedJson, requestOptions} from './http.mjs';

export class FlowError extends Error {
  constructor(code, message = code) { super(message); this.code = code; }
}
export function newInvocationId() { return globalThis.crypto.randomUUID(); }

export class InvocationClient {
  constructor({origin, credential, fetcher = (...args) => globalThis.fetch(...args), newId = newInvocationId}) {
    Object.assign(this, {origin, credential, fetcher, newId}); this.pending = new Map();
  }
  /** Resolves with the Result value or rejects with FlowError; 'unknown' means the outcome cannot be known. */
  invoke(target, args = {}, {expect = null} = {}) {
    const invocationId = this.newId();
    const outcome = new Promise((resolve, reject) => this.pending.set(invocationId, {resolve, reject}));
    const body = JSON.stringify(expect === null ? {invocationId, target, arguments: args} : {invocationId, target, arguments: args, expect});
    this.send(invocationId, body);
    return outcome;
  }
  async send(invocationId, body) {
    let reply;
    try {
      const response = await this.fetcher(this.origin + '/invoke',
        requestOptions(this.credential, {method: 'POST', body, headers: {'Content-Type': 'application/json'}}));
      reply = await readBoundedJson(response, {limit: 64 * 1024});
    } catch {
      this.settle({invocationId, role: 'flowError', code: 'unknown', message: 'The outcome of this action is unknown.'}); return;
    }
    if (reply?.role === 'accepted') return;
    this.settle({invocationId, ...reply});
  }
  /** Feed every item of the invocations track; ids this client did not send are ignored. */
  observe(item) { if (item && typeof item === 'object') this.settle(item); }
  settle(item) {
    const entry = this.pending.get(item.invocationId);
    if (!entry || item.role === 'accepted') return;
    this.pending.delete(item.invocationId);
    if (item.role === 'result') entry.resolve(item.value);
    else entry.reject(new FlowError(typeof item.code === 'string' ? item.code : 'failed', typeof item.message === 'string' ? item.message : undefined));
  }
  /** The host run changed: nothing still pending can be known any more. */
  runChanged() {
    for (const invocationId of [...this.pending.keys()])
      this.settle({invocationId, role: 'flowError', code: 'unknown', message: 'The host restarted; the outcome of this action is unknown.'});
  }
}
