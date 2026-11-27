// file: first-party/applications/aiBuddy/pack/ui/panels/diagnostics.mjs ; version: 1
import {h, replace} from '../dom.mjs';
import {TRACKS} from '../model.mjs';
import {time} from './common.mjs';

export function diagnostics(session) {
  const list = h('ol', {id: 'diagnostics'});
  session.tracks.watch(TRACKS.diagnostics, track => {
    const stick = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
    replace(list, ...(track?.items ?? []).map(item => h('li', {}, h('time', {}, time(item.at)), item.text)));
    if (stick) list.scrollTop = list.scrollHeight;
  });
  return h('section', {class: 'diagnostics'}, h('h2', {}, 'Diagnostics (never AI Buddy speech)'), list);
}
