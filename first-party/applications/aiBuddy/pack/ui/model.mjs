// file: first-party/applications/aiBuddy/pack/ui/model.mjs ; version: 1
/** Pure view-model functions from track values (DOM-free, Node-testable). */
export const TRACKS = {
  loop: 'aibuddy.loop', status: 'aibuddy.status', breakpoint: 'aibuddy.breakpoint', inference: 'aibuddy.inference',
  game: 'aibuddy.game', conversation: 'aibuddy.conversation', diagnostics: 'aibuddy.diagnostics',
};
export const READS = [
  ['aiBuddy.debug.status@1', 'Status', null],
  ['aiBuddy.debug.schedule@1', 'Schedule', null],
  ['aiBuddy.debug.record.describe@1', 'Record', {id: 'w000001'}],
  ['aiBuddy.debug.memory.search@1', 'Memory search', {query: ''}],
  ['aiBuddy.debug.memory.provenance@1', 'Memory provenance', {id: 'm000001'}],
  ['aiBuddy.debug.rule.list@1', 'Rules', {includeRetired: false}],
  ['aiBuddy.debug.personality.describe@1', 'Personality', null],
  ['aiBuddy.game.minesweeper.state@1', 'Game (developer view)', {showMines: true}],
];
export const INTERVENTIONS = [
  ['aiBuddy.debug.work.add@1', 'Add task', {kind: 'task', purpose: '', important: false}],
  ['aiBuddy.debug.work.choose@1', 'Choice', {candidateIds: ['w000001', 'w000002']}],
  ['aiBuddy.debug.work.change@1', 'Change work', {workId: 'w000001', action: 'suspend'}],
  ['aiBuddy.debug.situation.change@1', 'Situation', {action: 'add', text: '', important: false}],
  ['aiBuddy.debug.game.start@1', 'Start Minesweeper', {width: 5, height: 5, mines: 3, important: false}],
  ['aiBuddy.debug.lifecycle.change@1', 'Lifecycle', {action: 'rest'}],
  ['aiBuddy.debug.rule.add@1', 'Add rule', {scope: 'general', text: '', editable: false}],
  ['aiBuddy.debug.rule.change@1', 'Change rule', {ruleId: 'r000001', action: 'retire'}],
  ['aiBuddy.debug.passive.start@1', 'Passive processing', {activity: 'memory-processing'}],
  ['aiBuddy.debug.memory.inject@1', 'Inject memory', {text: ''}],
];

/** Badges for the loop flags; [label, tone] pairs. */
export function loopBadges(flags) {
  if (!flags) return [['starting', 'muted']];
  const badges = [];
  if (flags.quiescent) badges.push(['quiescent', 'warn']);
  else if (flags.paused) badges.push(['paused', 'warn']);
  else if (flags.busy) badges.push(['working', 'live']);
  else badges.push(['idle', 'muted']);
  if (flags.breakpoints) badges.push(['breakpoints on', 'warn']);
  return badges;
}
/** Connection state label for the header. */
export function connectionLabel(state) {
  return ({connected: 'connected', connecting: 'connecting...', disconnected: 'reconnecting...',
    busy: 'too many tabs; retrying', unauthorized: 'link expired - open the newest launch link',
    forbidden: 'refused', unavailable: 'host unavailable', closed: 'closed'})[state] ?? state;
}
export function lifecycleText(status) {
  const lifecycle = status?.buddy?.lifecycle;
  return lifecycle ? lifecycle.state + (lifecycle.reason ? ' (' + lifecycle.reason + ')' : '') : 'unknown';
}
export function openWork(status) { return Array.isArray(status?.buddy?.openWork) ? status.buddy.openWork : []; }
export function situation(status) { return Array.isArray(status?.buddy?.situation) ? status.buddy.situation : []; }
/** Breakpoint editor model: which action an edit maps to and what it is called. */
export function breakpointModel(pause) {
  if (!pause) return null;
  const output = pause.phase === 'output';
  return {pauseId: pause.pauseId, output, inferenceId: pause.inferenceId, workId: pause.workId, text: pause.text ?? '',
    title: output ? 'Response received, not yet acted on' : 'About to run inference',
    replaceTarget: output ? 'aiBuddy.replaceOutput' : 'aiBuddy.replacePrompt', replaceLabel: output ? 'Replace response' : 'Replace prompt'};
}
export function inferenceSummary(record) {
  if (!record) return null;
  const seconds = record.timing?.durationNs ? (record.timing.durationNs / 1e9).toFixed(2) + ' s' : null;
  return {id: record.id, state: record.state, seconds, workId: record.workId ?? null, rawOutput: record.rawOutput ?? record.generatedOutput ?? ''};
}
/** Parses the developer payload box; empty means no payload. Throws SyntaxError with a readable message. */
export function parsePayload(text) {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const value = JSON.parse(trimmed);
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new SyntaxError('The payload must be a JSON object.');
  return value;
}
export function pretty(value) { return JSON.stringify(value, null, 2); }
