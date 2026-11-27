// file: first-party/applications/aiBuddy/pack/ui/model.mjs ; version: 2
/** Pure view-model functions from track values (DOM-free, Node-testable). */
export const TRACKS = {
  loop: 'aibuddy.loop', status: 'aibuddy.status', breakpoint: 'aibuddy.breakpoint', inference: 'aibuddy.inference',
  game: 'aibuddy.game', conversation: 'aibuddy.conversation', diagnostics: 'aibuddy.diagnostics', inferences: 'aibuddy.inferences',
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
/** List/summary view of a finished inference (the bridge publishes summaries, not prompts). */
export function inferenceSummary(summary) {
  if (!summary) return null;
  const ns = summary.durationNs ?? summary.timing?.durationNs;
  const seconds = ns ? (ns / 1e9).toFixed(2) + ' s' : null;
  const edits = [summary.promptEdited ? 'prompt edited' : null, summary.outputEdited ? 'response replaced' : null].filter(Boolean);
  return {id: summary.id, state: summary.state, seconds, workId: summary.workId ?? null, purpose: summary.purpose ?? null,
    edits, error: summary.error ?? null, at: summary.at ?? null};
}
/** Record ids as written in diagnostics and records (e.g. i000012, w000003, m000001). */
export const RECORD_ID = /\b[a-z]\d{6}\b/g;
/** Splits text into plain parts and record-id parts, for clickable references. */
export function splitRecordIds(text) {
  const parts = []; let last = 0;
  for (const match of String(text).matchAll(RECORD_ID)) {
    if (match.index > last) parts.push({text: text.slice(last, match.index)});
    parts.push({id: match[0]}); last = match.index + match[0].length;
  }
  if (last < String(text).length) parts.push({text: String(text).slice(last)});
  return parts;
}
/** Detail view of a stored record: what an inference sent and received, with developer edits shown. */
export function recordView(record) {
  if (!record || typeof record !== 'object') return {kind: 'missing', sections: [], meta: []};
  if (typeof record.id !== 'string' || record.id[0] !== 'i') return {kind: 'record', sections: [], meta: [], json: record};
  const sent = record.submittedPrompt ?? record.prompt ?? null;
  const sections = [];
  if (sent !== null) sections.push({title: record.submittedPrompt ? 'Sent to the model' : 'Prompt (not sent yet)', text: sent});
  if (record.builtPrompt && sent !== null && record.builtPrompt !== sent) sections.push({title: 'Built prompt (before developer edit)', text: record.builtPrompt});
  if (record.generatedOutput !== undefined) sections.push({title: 'Received from the model', text: record.generatedOutput});
  if (record.rawOutput !== undefined && record.rawOutput !== record.generatedOutput) sections.push({title: 'Acted on (developer-replaced response)', text: record.rawOutput});
  if (record.error) sections.push({title: 'Error', text: String(record.error)});
  const ns = record.timing?.durationNs;
  const meta = [
    ['state', record.state], ['undertaking', record.workId], ['purpose', record.purpose],
    ['model', [record.provider, record.model].filter(Boolean).join(' / ') || null],
    ['duration', ns ? (ns / 1e9).toFixed(2) + ' s' : null], ['generated', record.generatedAt],
    ['interventions', Array.isArray(record.interventions) && record.interventions.length ? String(record.interventions.length) : null],
  ].filter(([, value]) => value !== null && value !== undefined && value !== '');
  return {kind: 'inference', sections, meta, json: record};
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
