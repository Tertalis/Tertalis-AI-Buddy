// file: first-party/applications/aiBuddy/pack/ui/main.mjs ; version: 2
/** AI Buddy browser main CodeEntry: mount(element, session) per the frontend kit contract. */
import {h} from './dom.mjs';
import {adoptStyles} from './style.mjs';
import {header} from './panels/header.mjs';
import {conversation} from './panels/conversation.mjs';
import {breakpoint} from './panels/breakpoint.mjs';
import {inference} from './panels/inference.mjs';
import {status} from './panels/status.mjs';
import {game} from './panels/game.mjs';
import {diagnostics} from './panels/diagnostics.mjs';
import {tools} from './panels/tools.mjs';
import {recordViewer} from './panels/records.mjs';
import {inferences} from './panels/inferences.mjs';

export function mount(root, session) {
  adoptStyles(document);
  const viewer = recordViewer(session);
  root.replaceChildren(h('div', {class: 'app'},
    header(session),
    h('div', {class: 'column'}, conversation(session), inference(session, viewer.open), inferences(session, viewer.open)),
    h('div', {class: 'column'}, breakpoint(session), viewer.element, status(session), game(session),
      diagnostics(session, viewer.open), tools(session))));
  root.dataset.ready = '1';
}
