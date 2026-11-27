// file: first-party/applications/aiBuddy/pack/ui/panels/inference.mjs ; version: 2
import {h, replace} from '../dom.mjs';
import {TRACKS, inferenceSummary} from '../model.mjs';

/** Live output of the running inference and the last finished one (details open in the record viewer). */
export function inference(session, openRecord) {
  const live = h('pre', {id: 'inference-live', class: 'hidden'});
  const summary = h('div', {class: 'note', id: 'inference-last'});
  session.tracks.watch(TRACKS.inference, track => {
    const value = track?.value ?? {};
    live.classList.toggle('hidden', !value.running);
    live.textContent = value.running ? (value.partial || '(waiting for the first tokens...)') : '';
    const last = inferenceSummary(value.last);
    if (!last) { replace(summary, 'No inference finished yet.'); return; }
    replace(summary, `Last: ${last.id} ${last.state}${last.seconds ? ' in ' + last.seconds : ''} `,
      h('button', {class: 'ref', onclick: () => openRecord(last.id)}, 'show sent / received'));
  });
  return h('section', {}, h('h2', {}, 'Inference'), live, summary);
}
