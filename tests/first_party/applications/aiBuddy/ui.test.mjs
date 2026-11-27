// file: tests/first_party/applications/aiBuddy/ui.test.mjs ; version: 2
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync, readdirSync} from 'node:fs';
import {breakpointModel, connectionLabel, inferenceSummary, INTERVENTIONS, lifecycleText, loopBadges, openWork, parsePayload, READS, recordView, splitRecordIds} from '../../../../first-party/applications/aiBuddy/pack/ui/model.mjs';

const UI = new URL('../../../../first-party/applications/aiBuddy/pack/ui/', import.meta.url);
const PACK = new URL('../../../../first-party/applications/aiBuddy/pack/', import.meta.url);

test('loop badges and labels', () => {
  assert.deepEqual(loopBadges(null), [['starting', 'muted']]);
  assert.deepEqual(loopBadges({busy: true, breakpoints: true}), [['working', 'live'], ['breakpoints on', 'warn']]);
  assert.deepEqual(loopBadges({quiescent: true, paused: true}), [['quiescent', 'warn']]);
  assert.match(connectionLabel('unauthorized'), /newest launch link/);
  assert.equal(lifecycleText({buddy: {lifecycle: {state: 'resting', reason: 'tired'}}}), 'resting (tired)');
  assert.deepEqual(openWork({}), []);
});
test('breakpoint model maps phases to the right action', () => {
  assert.equal(breakpointModel(null), null);
  const prompt = breakpointModel({pauseId: 'p1', phase: 'prompt', inferenceId: 'i1', text: 'P'});
  assert.equal(prompt.replaceTarget, 'aiBuddy.replacePrompt'); assert.equal(prompt.text, 'P');
  const output = breakpointModel({pauseId: 'p2', phase: 'output', inferenceId: 'i1', text: null});
  assert.equal(output.replaceTarget, 'aiBuddy.replaceOutput'); assert.equal(output.text, '');
});
test('inference summary and payload parsing', () => {
  assert.deepEqual(inferenceSummary({id: 'i1', state: 'completed', durationNs: 1.5e9, workId: 'w1', purpose: 'task', promptEdited: true, outputEdited: false, at: 'T'}),
    {id: 'i1', state: 'completed', seconds: '1.50 s', workId: 'w1', purpose: 'task', edits: ['prompt edited'], error: null, at: 'T'});
  assert.equal(parsePayload('  '), null);
  assert.deepEqual(parsePayload('{"a": 1}'), {a: 1});
  assert.throws(() => parsePayload('[1]'), SyntaxError);
  assert.throws(() => parsePayload('{'), SyntaxError);
});
test('tool lists match the bridge allow-lists', () => {
  const bridge = readFileSync(new URL('../../../../first-party/applications/aiBuddy/host/frontendBridge.py', import.meta.url), 'utf8');
  for (const [capability] of [...READS, ...INTERVENTIONS]) assert.ok(bridge.includes('"' + capability + '"'), capability);
});
test('every UI module is declared, and sources stay text-only and ASCII', () => {
  const manifest = JSON.parse(readFileSync(new URL('manifest.json', PACK), 'utf8'));
  const declared = new Set(manifest.codeEntries.map(entry => entry.source));
  const files = [...readdirSync(UI).filter(name => name.endsWith('.mjs')).map(name => 'ui/' + name),
    ...readdirSync(new URL('panels/', UI)).filter(name => name.endsWith('.mjs')).map(name => 'ui/panels/' + name)];
  for (const file of files) {
    assert.ok(declared.has(file), file + ' is not declared');
    const source = readFileSync(new URL(file, PACK), 'utf8');
    assert.doesNotMatch(source, /innerHTML|outerHTML|insertAdjacentHTML|localStorage|sessionStorage|document\.cookie|console\.|eval\(/, file);
    assert.doesNotMatch(source, /[^\x00-\x7F]/, file + ' must be ASCII');
  }
});

test('record ids in diagnostics become references', () => {
  assert.deepEqual(splitRecordIds('inference i000012 for w000003 done'),
    [{text: 'inference '}, {id: 'i000012'}, {text: ' for '}, {id: 'w000003'}, {text: ' done'}]);
  assert.deepEqual(splitRecordIds('no ids here 1234567'), [{text: 'no ids here 1234567'}]);
});
test('inference records show what was sent and received, and developer edits', () => {
  const plain = recordView({id: 'i000001', state: 'completed', builtPrompt: 'P', prompt: 'P', submittedPrompt: 'P', generatedOutput: 'O', rawOutput: 'O', timing: {durationNs: 2e9}});
  assert.deepEqual(plain.sections.map(section => section.title), ['Sent to the model', 'Received from the model']);
  assert.ok(plain.meta.some(([name, value]) => name === 'duration' && value === '2.00 s'));
  const edited = recordView({id: 'i000002', builtPrompt: 'P', prompt: 'E', submittedPrompt: 'E', generatedOutput: 'O', rawOutput: 'R'});
  assert.deepEqual(edited.sections.map(section => section.text), ['E', 'P', 'O', 'R']);
  const prepared = recordView({id: 'i000003', state: 'prepared', builtPrompt: 'P', prompt: 'P'});
  assert.deepEqual(prepared.sections.map(section => section.title), ['Prompt (not sent yet)']);
  assert.equal(recordView({id: 'w000001'}).kind, 'record');
  assert.equal(recordView(null).kind, 'missing');
});
