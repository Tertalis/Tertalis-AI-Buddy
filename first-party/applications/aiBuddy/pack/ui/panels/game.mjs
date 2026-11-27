// file: first-party/applications/aiBuddy/pack/ui/panels/game.mjs ; version: 1
import {h} from '../dom.mjs';
import {TRACKS} from '../model.mjs';

export function game(session) {
  const summary = h('div', {class: 'note'});
  const board = h('pre', {class: 'board', id: 'game-board'});
  const section = h('section', {class: 'hidden', id: 'game'}, h('h2', {}, 'Minesweeper'), summary, board);
  session.tracks.watch(TRACKS.game, track => {
    const value = track?.value;
    section.classList.toggle('hidden', !value);
    if (!value) return;
    summary.textContent = `${value.id}: ${value.summary ?? ''} [${value.status}]`;
    board.textContent = Array.isArray(value.board) ? value.board.join('\n') : '';
  });
  return section;
}
