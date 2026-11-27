// file: first-party/frontendKit/http.mjs ; version: 1
/** Shared bounded, non-leaking HTTP helpers for local Tertalis browser frontends. */
export const MAX_BODY = 2 * 1024 * 1024;

/** Fixed public connection failure with no URL, credential or server body attached. */
export class ConnectionError extends Error {
  constructor(code) { super(code); this.code = code; }
}
/** Response body was not bounded, well-typed JSON. */
export class BodyError extends Error {
  constructor() { super('invalid-body'); this.code = 'invalid-body'; }
}
/** Accept only a literal IPv4 loopback http origin with an explicit port and no path, query or userinfo. */
export function requireLoopbackOrigin(endpoint) {
  let url;
  try { url = new URL(endpoint); } catch { throw new ConnectionError('invalid-connection'); }
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || (url.pathname !== '/' && url.pathname !== '')
      || url.search || url.hash || url.username || url.password) throw new ConnectionError('invalid-connection');
  return url;
}
/** Credential shape shared by the observation bridge and the frontend host (urlsafe base64 of 256 bits). */
export function isCredential(value) { return typeof value === 'string' && /^[A-Za-z0-9_-]{16,256}$/.test(value); }
/** Fetch options that never send cookies, follow redirects, cache, or leak a referrer. */
export function requestOptions(credential, extra = {}) {
  const headers = {...(extra.headers ?? {})};
  if (credential !== undefined) headers.Authorization = 'Bearer ' + credential;
  return {...extra, headers, credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer'};
}
/** Read a JSON response incrementally up to limit bytes; no unbounded text(). */
export async function readBoundedJson(response, {limit = MAX_BODY, invalid = () => new BodyError()} = {}) {
  if (!response.headers.get('content-type')?.toLowerCase().startsWith('application/json')) { await response.body?.cancel(); throw invalid(); }
  const length = response.headers.get('content-length');
  if (length !== null && (!/^\d+$/.test(length) || Number(length) > limit)) { await response.body?.cancel(); throw invalid(); }
  if (!response.body) throw invalid();
  const reader = response.body.getReader(); const chunks = []; let size = 0;
  try {
    while (true) {
      const {done, value} = await reader.read(); if (done) break;
      size += value.byteLength;
      if (size > limit) { await reader.cancel(); throw invalid(); }
      chunks.push(value);
    }
    const bytes = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    try { return JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(bytes)); } catch { throw invalid(); }
  } finally { reader.releaseLock(); }
}
