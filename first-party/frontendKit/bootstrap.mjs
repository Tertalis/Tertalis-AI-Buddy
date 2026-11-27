// file: first-party/frontendKit/bootstrap.mjs ; version: 2
/** Generic View bootstrap: credential (fragment, else this tab's), session, then the Application's browser main. */
import {forgetCredential, launchCredential, tabStorage} from './credential.mjs';
import {openSession} from './session.mjs';

function show(document, text) {
  const element = document.getElementById('boot-status');
  if (element) element.textContent = text;
}
export async function boot({document = globalThis.document, location = globalThis.location, history = globalThis.history,
                            fetcher = (...args) => globalThis.fetch(...args), importer = url => import(url), storage = tabStorage()} = {}) {
  const credential = launchCredential(location, history, storage);
  if (!credential) { show(document, 'Open the full link printed by the launcher (it carries the access token).'); return null; }
  let session;
  try { session = await openSession({origin: location.origin, credential, fetcher}); }
  catch (error) {
    if (error?.code === 'unauthorized') {
      forgetCredential(storage);
      show(document, 'This access token is no longer valid (the host restarted). Open the newest link printed by the launcher.');
    } else show(document, 'Could not connect. Is AI Buddy / the host still running? Reload to retry.');
    return null;
  }
  session.onState(state => { if (state === 'unauthorized') forgetCredential(storage); });
  const main = session.descriptor.main;
  if (typeof main !== 'string' || !main.startsWith('/pack/')) { show(document, 'No user interface is configured.'); session.close(); return null; }
  if (typeof session.descriptor.title === 'string') document.title = session.descriptor.title;
  const module = await importer(main);
  const root = document.getElementById('app');
  show(document, '');
  await module.mount(root, session);
  return session;
}
