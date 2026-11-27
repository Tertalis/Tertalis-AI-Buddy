// file: first-party/applications/aiBuddy/pack/ui/panels/status.mjs ; version: 1
import {h, replace} from '../dom.mjs';
import {TRACKS, openWork, situation} from '../model.mjs';

export function status(session) {
  const work = h('tbody', {id: 'open-work'});
  const facts = h('div', {class: 'note'});
  const info = h('ul', {id: 'situation'});
  session.tracks.watch(TRACKS.status, track => {
    const value = track?.value;
    const items = openWork(value);
    replace(work, ...(items.length ? items.map(item => h('tr', {}, h('td', {}, item.id), h('td', {}, item.kind), h('td', {}, item.state), h('td', {}, item.purpose ?? '')))
      : [h('tr', {}, h('td', {colSpan: 4, class: 'note'}, 'Nothing to do.'))]));
    replace(info, ...situation(value).map(item => h('li', {}, (item.important ? '[important] ' : '') + item.text, ' ', h('span', {class: 'note'}, item.id))));
    const buddy = value?.buddy;
    facts.textContent = buddy?.error ? buddy.error
      : buddy ? `${buddy.experiences} experiences, ${buddy.memories} memories, ${buddy.inferences} inferences, ${buddy.rules} rules` : 'waiting for status...';
  });
  return h('section', {}, h('h2', {}, 'Things to do'),
    h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'id'), h('th', {}, 'kind'), h('th', {}, 'state'), h('th', {}, 'purpose'))), work),
    h('h2', {}, 'Situation'), info, facts);
}
