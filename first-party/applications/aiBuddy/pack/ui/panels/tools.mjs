// file: first-party/applications/aiBuddy/pack/ui/panels/tools.mjs ; version: 1
import {h} from '../dom.mjs';
import {INTERVENTIONS, READS, parsePayload, pretty} from '../model.mjs';
import {act} from './common.mjs';

/** Developer reads and interventions, limited to the bridge's allow-lists. */
export function tools(session) {
  const option = ([capability, label], kind) => h('option', {value: kind + ' ' + capability}, label);
  const select = h('select', {id: 'tool-select'},
    h('optgroup', {label: 'Inspect'}, ...READS.map(item => option(item, 'read'))),
    h('optgroup', {label: 'Intervene'}, ...INTERVENTIONS.map(item => option(item, 'intervene'))));
  const payload = h('textarea', {class: 'code', id: 'tool-payload', rows: 4, spellcheck: false});
  const result = h('pre', {id: 'tool-result'});
  const note = h('span', {class: 'note'});
  const defaults = new Map([...READS.map(item => ['read ' + item[0], item[2]]), ...INTERVENTIONS.map(item => ['intervene ' + item[0], item[2]])]);
  const fill = () => { const value = defaults.get(select.value); payload.value = value ? pretty(value) : ''; };
  select.addEventListener('change', fill);
  fill();
  const run = h('button', {id: 'tool-run', class: 'primary', onclick: async () => {
    const [kind, capability] = select.value.split(' ');
    let parsed;
    try { parsed = parsePayload(payload.value); } catch (error) { note.className = 'error'; note.textContent = error.message; return; }
    try {
      const value = await act(session, note, kind === 'read' ? 'aiBuddy.read' : 'aiBuddy.intervene', {capability, payload: parsed});
      result.textContent = pretty(value);
    } catch { result.textContent = ''; }
  }}, 'Run');
  return h('section', {}, h('h2', {}, 'Developer tools'), select, payload, h('div', {class: 'row'}, run, note), result);
}
