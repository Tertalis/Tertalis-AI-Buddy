// file: first-party/frontendKit/credential.mjs ; version: 1
/** Takes the launch credential from the URL fragment once and removes it from the address bar. */
import {isCredential} from './http.mjs';

/**
 * Fragments are never sent to servers, logs or Referer. The credential is kept only in the
 * caller's memory: no storage, no cookie. Returns null when absent or malformed.
 */
export function takeFragmentCredential(location = globalThis.location, history = globalThis.history) {
  const hash = typeof location?.hash === 'string' ? location.hash : '';
  const value = new URLSearchParams(hash.startsWith('#') ? hash.slice(1) : hash).get('token');
  if (hash) history?.replaceState?.(null, '', location.pathname + location.search);
  return isCredential(value) ? value : null;
}
