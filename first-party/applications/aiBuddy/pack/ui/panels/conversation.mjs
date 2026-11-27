// file: first-party/applications/aiBuddy/pack/ui/panels/conversation.mjs ; version: 1
import {h, replace} from '../dom.mjs';
import {TRACKS} from '../model.mjs';
import {act, time} from './common.mjs';

export function conversation(session) {
  const messages = h('div', {class: 'messages', id: 'messages', role: 'log', 'aria-live': 'polite'});
  const input = h('textarea', {id: 'say-text', rows: 2, placeholder: 'Message (Enter sends, Shift+Enter for a new line)'});
  const note = h('span', {class: 'note'});
  const send = async event => {
    event?.preventDefault();
    const text = input.value;
    if (!text.trim()) return;
    input.value = '';
    try { await act(session, note, 'aiBuddy.say', {text}); } catch { input.value = text; }
  };
  input.addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey) send(event); });
  session.tracks.watch(TRACKS.conversation, track => {
    const stick = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 40;
    replace(messages, ...(track?.items ?? []).map(item =>
      h('div', {class: 'message ' + (item.from === 'user' ? 'user' : 'buddy')}, h('time', {}, time(item.at)), item.text)));
    if (stick) messages.scrollTop = messages.scrollHeight;
  });
  return h('section', {class: 'conversation'}, h('h2', {}, 'Conversation'), messages,
    h('form', {class: 'say', onsubmit: send}, input, h('button', {class: 'primary', id: 'say-send', type: 'submit'}, 'Send')), note);
}
