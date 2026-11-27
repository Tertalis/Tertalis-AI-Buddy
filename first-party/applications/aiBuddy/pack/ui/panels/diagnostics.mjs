// file: first-party/applications/aiBuddy/pack/ui/panels/diagnostics.mjs ; version: 2
import {h, replace} from '../dom.mjs';
import {TRACKS, splitRecordIds} from '../model.mjs';
import {time} from './common.mjs';

/** Diagnostics log; record ids (i000012, w000003, ...) open the record viewer. */
export function diagnostics(session, openRecord) {
  const list = h('ol', {id: 'diagnostics'});
  session.tracks.watch(TRACKS.diagnostics, track => {
    const stick = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
    replace(list, ...(track?.items ?? []).map(item => h('li', {}, h('time', {}, time(item.at)),
      ...splitRecordIds(item.text).map(part => part.id ? h('button', {class: 'ref', onclick: () => openRecord(part.id)}, part.id) : part.text))));
    if (stick) list.scrollTop = list.scrollHeight;
  });
  return h('section', {class: 'diagnostics'}, h('h2', {}, 'Diagnostics (never AI Buddy speech)'), list);
}
