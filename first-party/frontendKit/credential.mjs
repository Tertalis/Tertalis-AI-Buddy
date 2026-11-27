// file: first-party/frontendKit/credential.mjs ; version: 2
/**
 * Launch credential handling.
 *
 * The launcher's link carries the credential in the URL fragment, which is never sent to servers,
 * logs or Referer. It is removed from the address bar at once. To let F5 / Ctrl+F5 reload the
 * page, it is then kept in this tab's sessionStorage (per origin and per tab; gone when the tab
 * closes; not shared with other tabs or windows). It is forgotten as soon as the host rejects it
 * (for example after a host restart rotated it). This module is the only place that uses storage.
 */
import {isCredential} from './http.mjs';

export const STORAGE_KEY = 'tertalis.frontend.credential';

/** This tab's sessionStorage, or null where storage is unavailable or blocked. */
export function tabStorage() {
  try { return globalThis.sessionStorage ?? null; } catch { return null; }
}
/** Takes a credential from the fragment (removing the fragment) and remembers it for this tab. */
export function takeFragmentCredential(location = globalThis.location, history = globalThis.history, storage = tabStorage()) {
  const hash = typeof location?.hash === 'string' ? location.hash : '';
  const value = new URLSearchParams(hash.startsWith('#') ? hash.slice(1) : hash).get('token');
  if (hash) history?.replaceState?.(null, '', location.pathname + location.search);
  if (!isCredential(value)) return null;
  try { storage?.setItem(STORAGE_KEY, value); } catch { /* reload will need the link again */ }
  return value;
}
/** The credential remembered for this tab by an earlier load, if any. */
export function rememberedCredential(storage = tabStorage()) {
  try { const value = storage?.getItem(STORAGE_KEY); return isCredential(value) ? value : null; } catch { return null; }
}
/** Forgets this tab's credential (after the host rejected it, or on request). */
export function forgetCredential(storage = tabStorage()) {
  try { storage?.removeItem(STORAGE_KEY); } catch { /* nothing remembered */ }
}
/** Fragment first (a fresh link wins), otherwise this tab's remembered credential. */
export function launchCredential(location = globalThis.location, history = globalThis.history, storage = tabStorage()) {
  return takeFragmentCredential(location, history, storage) ?? rememberedCredential(storage);
}
