// file: first-party/applications/aiBuddy/pack/ui/panels/breakpoint.mjs ; version: 1
import {h} from '../dom.mjs';
import {TRACKS, breakpointModel} from '../model.mjs';
import {act} from './common.mjs';

export function breakpoint(session) {
  const title = h('h2', {}, 'Breakpoint');
  const detail = h('div', {class: 'note'});
  const text = h('textarea', {class: 'code', id: 'breakpoint-text', spellcheck: false});
  const note = h('span', {class: 'note'});
  const replaceButton = h('button', {id: 'breakpoint-replace'});
  const continueButton = h('button', {class: 'primary', id: 'breakpoint-continue'}, 'Continue');
  const section = h('section', {class: 'breakpoint hidden', id: 'breakpoint'}, title, detail, text,
    h('div', {class: 'row'}, replaceButton, continueButton, note));
  let model = null, shownPause = null;
  replaceButton.addEventListener('click', () => {
    if (model) act(session, note, model.replaceTarget, {text: text.value}, {expect: {pause: model.pauseId}}).catch(() => {});
  });
  continueButton.addEventListener('click', () => {
    if (model) act(session, note, 'aiBuddy.continue', {}, {expect: {pause: model.pauseId}}).catch(() => {});
  });
  session.tracks.watch(TRACKS.breakpoint, track => {
    model = breakpointModel(track?.value ?? null);
    section.classList.toggle('hidden', !model);
    if (!model) { shownPause = null; return; }
    title.textContent = 'Breakpoint - ' + model.title;
    detail.textContent = `${model.inferenceId}${model.workId ? ' for ' + model.workId : ''} (pause ${model.pauseId})`;
    replaceButton.textContent = model.replaceLabel;
    if (shownPause !== model.pauseId || document.activeElement !== text) text.value = model.text;
    shownPause = model.pauseId;
  });
  return section;
}
