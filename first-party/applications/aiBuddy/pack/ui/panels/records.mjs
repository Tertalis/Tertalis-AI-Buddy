// file: first-party/applications/aiBuddy/pack/ui/panels/records.mjs ; version: 1
import {h, replace} from '../dom.mjs';
import {pretty, recordView} from '../model.mjs';
import {act} from './common.mjs';

/**
 * Shows one stored record, loaded on demand from AI Buddy state (aiBuddy.read).
 * Only the open record is held; Close (or opening another) releases it.
 */
export function recordViewer(session) {
  const title = h('h2', {}, 'Record');
  const note = h('span', {class: 'note'});
  const body = h('div', {id: 'record-body'});
  const close = h('button', {id: 'record-close'}, 'Close');
  const element = h('section', {class: 'viewer hidden', id: 'record-viewer'}, h('div', {class: 'row'}, title, h('span', {class: 'spacer'}), note, close), body);
  let request = 0;
  const release = () => { request++; replace(body); element.classList.add('hidden'); };
  close.addEventListener('click', release);
  async function open(id) {
    const mine = ++request;
    element.classList.remove('hidden');
    title.textContent = 'Record ' + id;
    replace(body, h('div', {class: 'note'}, 'loading...'));
    element.scrollIntoView?.({block: 'nearest'});
    let record;
    try {
      record = await act(session, note, 'aiBuddy.read', {capability: 'aiBuddy.debug.record.describe@1', payload: {id}});
    } catch { if (mine === request) replace(body); return; }
    if (mine !== request) return;
    const view = recordView(record);
    if (view.kind === 'missing') { replace(body, h('div', {class: 'note'}, 'No such record.')); return; }
    replace(body,
      view.meta.length ? h('dl', {}, ...view.meta.flatMap(([name, value]) => [h('dt', {}, name), h('dd', {}, value)])) : null,
      ...view.sections.map(section => [h('h3', {}, section.title), h('pre', {}, section.text)]),
      h('details', {}, h('summary', {}, 'Full record (JSON)'), h('pre', {}, pretty(view.json))));
  }
  return {element, open};
}
