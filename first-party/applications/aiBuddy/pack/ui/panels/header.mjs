// file: first-party/applications/aiBuddy/pack/ui/panels/header.mjs ; version: 2
import {h, replace} from '../dom.mjs';
import {TRACKS, connectionLabel, lifecycleText, loopBadges, modelChoices} from '../model.mjs';
import {act} from './common.mjs';

export function header(session) {
  const title = h('h1', {}, 'AI Buddy');
  const badges = h('span', {class: 'badges'});
  const connection = h('span', {class: 'badge muted', id: 'connection'});
  const lifecycle = h('span', {class: 'badge muted', id: 'lifecycle'});
  const note = h('span', {class: 'note'});
  let flags = null, managed = null, loading = false;
  const model = h('select', {id: 'model-select', class: 'hidden', title: 'Model (platform-managed llama.cpp)'});
  async function refreshModels() {
    try {
      const choices = modelChoices(await session.invoke('aiBuddy.read', {capability: 'aiBuddy.debug.llm.describe@1'}));
      managed = Boolean(choices);
      model.classList.toggle('hidden', !choices);
      if (!choices) return;
      replace(model, ...choices.options.map(option => h('option', {value: option.name, selected: option.name === choices.selected}, option.label)));
      model.value = choices.selected;
    } catch { /* shown again on the next status change */ }
  }
  model.addEventListener('change', async () => {
    const name = model.value;
    loading = true; model.disabled = true; note.className = 'note'; note.textContent = 'loading model ' + name + '...';
    try { await act(session, note, 'aiBuddy.intervene', {capability: 'aiBuddy.debug.llm.select@1', payload: {model: name}}); }
    catch { /* act shows the error */ }
    finally { loading = false; model.disabled = false; refreshModels(); }
  });
  const breakToggle = h('button', {id: 'toggle-breakpoints', onclick: () => act(session, note, 'aiBuddy.breakpoints', {enabled: !flags?.breakpoints}).catch(() => {})}, 'Breakpoints');
  const pause = h('button', {id: 'pause', onclick: () => act(session, note, flags?.paused ? 'aiBuddy.continue' : 'aiBuddy.pause').catch(() => {})}, 'Pause');
  const stop = h('button', {id: 'stop', title: 'Cancel the running inference', onclick: () => {
    // Known limitation: with the managed server a mid-answer cancel can leave it unusable until restart.
    // Until model metadata arrives, managed mode is still possible.
    if (managed !== false && !globalThis.confirm('Cancelling mid-answer can leave the managed model server unusable until AI Buddy restarts. Cancel anyway?')) return;
    act(session, note, 'aiBuddy.interrupt').catch(() => {});
  }}, 'Stop');
  const quiesce = h('button', {id: 'quiesce', onclick: () => act(session, note, flags?.quiescent ? 'aiBuddy.unquiesce' : 'aiBuddy.quiesce').catch(() => {})}, 'Quiesce');
  const save = h('button', {id: 'save', onclick: () => act(session, note, 'aiBuddy.save').catch(() => {})}, 'Save');
  session.tracks.watch(TRACKS.loop, track => {
    flags = track?.value ?? null;
    replace(badges, ...loopBadges(flags).map(([label, tone]) => h('span', {class: 'badge ' + tone}, label)));
    breakToggle.textContent = flags?.breakpoints ? 'Breakpoints: on' : 'Breakpoints: off';
    pause.textContent = flags?.paused ? 'Resume' : 'Pause';
    quiesce.textContent = flags?.quiescent ? 'Unquiesce' : 'Quiesce';
    stop.disabled = !flags?.busy;
  });
  let shownModel;
  session.tracks.watch(TRACKS.status, track => {
    const name = track?.value?.buddy?.personalityName;
    title.textContent = name ? name : 'AI Buddy';
    lifecycle.textContent = lifecycleText(track?.value);
    const current = track?.value?.buddy?.llm?.model;
    if (current !== shownModel && !loading) { shownModel = current; refreshModels(); }
  });
  session.onState(state => {
    connection.textContent = connectionLabel(state);
    connection.className = 'badge ' + (state === 'connected' ? 'live' : ['unauthorized', 'forbidden', 'unavailable', 'closed'].includes(state) ? 'bad' : 'warn');
  });
  return h('header', {}, title, badges, lifecycle, connection, h('span', {class: 'spacer'}), note, model, breakToggle, pause, stop, quiesce, save);
}
