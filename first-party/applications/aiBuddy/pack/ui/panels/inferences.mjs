// file: first-party/applications/aiBuddy/pack/ui/panels/inferences.mjs ; version: 1
import {h, replace} from '../dom.mjs';
import {TRACKS, inferenceSummary} from '../model.mjs';
import {time} from './common.mjs';

/** The most recent finished inferences (bounded by the host); each opens what was sent and received. */
export function inferences(session, openRecord) {
  const list = h('ol', {id: 'inference-history'});
  const count = h('span', {class: 'note'});
  session.tracks.watch(TRACKS.inferences, track => {
    const items = (track?.items ?? []).map(inferenceSummary).filter(Boolean).reverse();
    count.textContent = items.length ? `${items.length} most recent` : '';
    replace(list, ...(items.length ? items.map(item => h('li', {},
      h('button', {class: 'ref', onclick: () => openRecord(item.id)}, item.id),
      h('time', {}, time(item.at)),
      h('span', {class: 'state-' + item.state}, item.state),
      item.seconds ? h('span', {class: 'note'}, item.seconds) : null,
      h('span', {}, [item.purpose, item.workId].filter(Boolean).join(' ')),
      item.edits.length ? h('span', {class: 'badge warn'}, item.edits.join(', ')) : null))
      : [h('li', {class: 'note'}, 'No inference finished yet.')]));
  });
  return h('section', {class: 'inferences'}, h('div', {class: 'row'}, h('h2', {}, 'Inferences'), count), list);
}
