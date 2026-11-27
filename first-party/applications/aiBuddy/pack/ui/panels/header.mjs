// file: first-party/applications/aiBuddy/pack/ui/panels/header.mjs ; version: 1
import {h, replace} from '../dom.mjs';
import {TRACKS, connectionLabel, lifecycleText, loopBadges} from '../model.mjs';
import {act} from './common.mjs';

export function header(session) {
  const title = h('h1', {}, 'AI Buddy');
  const badges = h('span', {class: 'badges'});
  const connection = h('span', {class: 'badge muted', id: 'connection'});
  const lifecycle = h('span', {class: 'badge muted', id: 'lifecycle'});
  const note = h('span', {class: 'note'});
  let flags = null;
  const breakToggle = h('button', {id: 'toggle-breakpoints', onclick: () => act(session, note, 'aiBuddy.breakpoints', {enabled: !flags?.breakpoints}).catch(() => {})}, 'Breakpoints');
  const pause = h('button', {id: 'pause', onclick: () => act(session, note, flags?.paused ? 'aiBuddy.continue' : 'aiBuddy.pause').catch(() => {})}, 'Pause');
  const stop = h('button', {id: 'stop', title: 'Cancel the running inference', onclick: () => act(session, note, 'aiBuddy.interrupt').catch(() => {})}, 'Stop');
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
  session.tracks.watch(TRACKS.status, track => {
    const name = track?.value?.buddy?.personalityName;
    title.textContent = name ? name : 'AI Buddy';
    lifecycle.textContent = lifecycleText(track?.value);
  });
  session.onState(state => {
    connection.textContent = connectionLabel(state);
    connection.className = 'badge ' + (state === 'connected' ? 'live' : ['unauthorized', 'forbidden', 'unavailable', 'closed'].includes(state) ? 'bad' : 'warn');
  });
  return h('header', {}, title, badges, lifecycle, connection, h('span', {class: 'spacer'}), note, breakToggle, pause, stop, quiesce, save);
}
