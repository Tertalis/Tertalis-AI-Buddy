// file: first-party/applications/aiBuddy/pack/ui/main.mjs ; version: 1
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

export function mount(root, session) {
  adoptStyles(document);
  root.replaceChildren(h('div', {class: 'app'},
    header(session),
    h('div', {class: 'column'}, conversation(session), inference(session)),
    h('div', {class: 'column'}, breakpoint(session), status(session), game(session), diagnostics(session), tools(session))));
  root.dataset.ready = '1';
}
