// file: first-party/frontendKit/bootstrap.mjs ; version: 1
/** Generic View bootstrap: credential from fragment, session, then the Application's browser main. */
import {takeFragmentCredential} from './credential.mjs';
import {openSession} from './session.mjs';

function show(document, text) {
  const element = document.getElementById('boot-status');
  if (element) element.textContent = text;
}
export async function boot({document = globalThis.document, location = globalThis.location, history = globalThis.history,
                            fetcher = (...args) => globalThis.fetch(...args), importer = url => import(url)} = {}) {
  const credential = takeFragmentCredential(location, history);
  if (!credential) { show(document, 'Open the full link printed by the launcher (it carries the access token).'); return null; }
  let session;
  try { session = await openSession({origin: location.origin, credential, fetcher}); }
  catch { show(document, 'Could not connect. The link may be from an earlier run; open the newest one.'); return null; }
  const main = session.descriptor.main;
  if (typeof main !== 'string' || !main.startsWith('/pack/')) { show(document, 'No user interface is configured.'); session.close(); return null; }
  if (typeof session.descriptor.title === 'string') document.title = session.descriptor.title;
  const module = await importer(main);
  const root = document.getElementById('app');
  show(document, '');
  await module.mount(root, session);
  return session;
}
