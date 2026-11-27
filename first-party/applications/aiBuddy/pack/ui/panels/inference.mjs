// file: first-party/applications/aiBuddy/pack/ui/panels/inference.mjs ; version: 1
import {h} from '../dom.mjs';
import {TRACKS, inferenceSummary} from '../model.mjs';

export function inference(session) {
  const live = h('pre', {id: 'inference-live', class: 'hidden'});
  const summary = h('div', {class: 'note', id: 'inference-last'});
  const raw = h('pre', {id: 'inference-raw'});
  const details = h('details', {}, h('summary', {}, 'Last response'), raw);
  session.tracks.watch(TRACKS.inference, track => {
    const value = track?.value ?? {};
    live.classList.toggle('hidden', !value.running);
    live.textContent = value.running ? (value.partial || '(waiting for the first tokens...)') : '';
    const last = inferenceSummary(value.last);
    summary.textContent = last ? `${last.id}: ${last.state}${last.seconds ? ' in ' + last.seconds : ''}` : 'No inference finished yet.';
    raw.textContent = last ? last.rawOutput : '';
  });
  return h('section', {}, h('h2', {}, 'Inference'), live, summary, details);
}
