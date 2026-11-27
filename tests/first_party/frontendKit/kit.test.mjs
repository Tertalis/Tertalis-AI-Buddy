// file: tests/first_party/frontendKit/kit.test.mjs ; version: 3
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync, readdirSync} from 'node:fs';
import {MAX_BODY, BodyError, ConnectionError, readBoundedJson, requestOptions, requireLoopbackOrigin, isCredential} from '../../../first-party/frontendKit/http.mjs';
import {STORAGE_KEY, forgetCredential, launchCredential, rememberedCredential, takeFragmentCredential} from '../../../first-party/frontendKit/credential.mjs';
import {readEvents, StreamFormatError} from '../../../first-party/frontendKit/eventStream.mjs';
import {TrackStore, INVOCATIONS_TRACK} from '../../../first-party/frontendKit/tracks.mjs';
import {StreamClient, streamUrl} from '../../../first-party/frontendKit/stream.mjs';
import {InvocationClient, FlowError} from '../../../first-party/frontendKit/invoke.mjs';
import {openSession} from '../../../first-party/frontendKit/session.mjs';
import {boot} from '../../../first-party/frontendKit/bootstrap.mjs';

const ORIGIN = 'http://127.0.0.1:4100';
const TOKEN = 'A'.repeat(43);
const KIT = new URL('../../../first-party/frontendKit/', import.meta.url);
const flush = () => new Promise(resolve => setImmediate(resolve));
const json = (value, status = 200) => new Response(JSON.stringify(value), {status, headers: {'content-type': 'application/json'}});

/** A controllable text/event-stream response. */
function eventResponse() {
  let controller;
  const body = new ReadableStream({start(c) { controller = c; }});
  const encoder = new TextEncoder();
  return {
    response: new Response(body, {status: 200, headers: {'content-type': 'text/event-stream'}}),
    send(event, data) { controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)); },
    raw(text) { controller.enqueue(encoder.encode(text)); },
    end() { controller.close(); },
  };
}
function clock() {
  const tasks = new Map(); let next = 1;
  return {tasks, schedule: (fn, delay) => { const id = next++; tasks.set(id, {fn, delay}); return id; }, cancel: id => tasks.delete(id),
    run() { const items = [...tasks.entries()]; tasks.clear(); for (const [, task] of items) task.fn(); }};
}

test('loopback origin and credential shape are strict', () => {
  assert.equal(requireLoopbackOrigin('http://127.0.0.1:9/').origin, 'http://127.0.0.1:9');
  for (const bad of ['http://localhost:9', 'https://127.0.0.1:9', 'http://127.0.0.1', 'http://127.0.0.1:9/x', 'http://u:p@127.0.0.1:9', 'http://127.0.0.1:9/?a', 'nope'])
    assert.throws(() => requireLoopbackOrigin(bad), ConnectionError);
  assert.equal(isCredential(TOKEN), true);
  for (const bad of ['short', 'a b'.repeat(10), null, 42]) assert.equal(isCredential(bad), false);
});
test('request options never send cookies, follow redirects, cache or referrers', () => {
  const options = requestOptions(TOKEN, {method: 'POST', headers: {'Content-Type': 'application/json'}});
  assert.deepEqual(options.headers, {'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN});
  assert.equal(options.credentials, 'omit'); assert.equal(options.cache, 'no-store'); assert.equal(options.redirect, 'error'); assert.equal(options.referrerPolicy, 'no-referrer');
});
test('bounded JSON rejects wrong type, oversized length, oversized stream and bad UTF-8', async () => {
  assert.deepEqual(await readBoundedJson(json({a: 1})), {a: 1});
  await assert.rejects(readBoundedJson(new Response('{}', {headers: {'content-type': 'text/plain'}})), BodyError);
  await assert.rejects(readBoundedJson(new Response('{}', {headers: {'content-type': 'application/json', 'content-length': String(MAX_BODY + 1)}})), BodyError);
  await assert.rejects(readBoundedJson(json({a: 'x'.repeat(100)}), {limit: 10}), BodyError);
  await assert.rejects(readBoundedJson(new Response(new Uint8Array([0xff]), {headers: {'content-type': 'application/json'}})), BodyError);
  class Custom extends Error {}
  await assert.rejects(readBoundedJson(json('x'), {limit: 1, invalid: () => new Custom()}), Custom);
});
/** Minimal per-tab Storage stand-in. */
function memoryStorage() {
  const map = new Map();
  return {map, getItem: key => map.has(key) ? map.get(key) : null, setItem: (key, value) => map.set(key, String(value)), removeItem: key => map.delete(key)};
}
test('fragment credential is taken once, removed from the address bar, and remembered for this tab', () => {
  const calls = [];
  const history = {replaceState: (...args) => calls.push(args)};
  const storage = memoryStorage();
  assert.equal(takeFragmentCredential({hash: '#token=' + TOKEN, pathname: '/', search: ''}, history, storage), TOKEN);
  assert.deepEqual([...storage.map], [[STORAGE_KEY, TOKEN]]);
  assert.equal(rememberedCredential(storage), TOKEN, 'F5 / Ctrl+F5 reuse it');
  assert.equal(launchCredential({hash: '', pathname: '/', search: ''}, history, storage), TOKEN);
  const other = 'B'.repeat(43);
  assert.equal(launchCredential({hash: '#token=' + other, pathname: '/', search: ''}, history, storage), other, 'a fresh link wins');
  forgetCredential(storage);
  assert.equal(rememberedCredential(storage), null);
  storage.setItem(STORAGE_KEY, 'bad'); assert.equal(rememberedCredential(storage), null, 'malformed remembered values are ignored');
  const throwing = {getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }, removeItem() { throw new Error('blocked'); }};
  assert.equal(takeFragmentCredential({hash: '#token=' + TOKEN, pathname: '/', search: ''}, history, throwing), TOKEN, 'blocked storage only costs reload');
  assert.equal(rememberedCredential(throwing), null); forgetCredential(throwing);
  calls.length = 0;
  assert.equal(takeFragmentCredential({hash: '#token=' + TOKEN, pathname: '/', search: ''}, history, null), TOKEN);
  assert.deepEqual(calls, [[null, '', '/']]);
  assert.equal(takeFragmentCredential({hash: '#token=bad', pathname: '/', search: ''}, history), null);
  assert.equal(calls.length, 2, 'malformed fragments are removed too');
  assert.equal(takeFragmentCredential({hash: '', pathname: '/', search: ''}, history), null);
  assert.equal(calls.length, 2);
});
test('event stream framing handles chunks, comments, CRLF and limits', async () => {
  const source = eventResponse();
  const events = [];
  const done = (async () => { for await (const event of readEvents(source.response.body.getReader())) events.push(event); })();
  source.raw(': ping\n\nevent: emit\r\ndata: {"a"'); source.raw(':1}\n\n'); source.send('run', {runId: 'r'}); source.end();
  await done;
  assert.deepEqual(events, [{event: 'emit', data: {a: 1}}, {event: 'run', data: {runId: 'r'}}]);
  const big = eventResponse();
  const failing = (async () => { for await (const _ of readEvents(big.response.body.getReader(), {limit: 8})) { /* */ } })();
  big.raw('data: "' + 'x'.repeat(20) + '"\n\n'); big.end();
  await assert.rejects(failing, StreamFormatError);
});
test('track store applies in order, ignores stale Emits and reports gaps', () => {
  const store = new TrackStore({maxLogItems: 3}); const seen = [];
  store.subscribe('log', track => seen.push(track?.items.length ?? null));
  assert.equal(store.apply({track: 'log', position: 1, kind: 'append', value: 'a'}), 'gap', 'unknown track needs a snapshot');
  store.applySnapshot({track: 'log', style: 'log', position: 2, items: ['a', 'b']});
  assert.equal(store.apply({track: 'log', position: 2, kind: 'append', value: 'b'}), 'stale');
  assert.equal(store.apply({track: 'log', position: 4, kind: 'append', value: 'd'}), 'gap');
  for (const [position, value] of [[3, 'c'], [4, 'd']]) assert.equal(store.apply({track: 'log', position, kind: 'append', value}), 'applied');
  assert.deepEqual(store.get('log').items, ['b', 'c', 'd'], 'log tail is bounded');
  assert.equal(store.applySnapshot({track: 'log', style: 'log', position: 3, items: []}), false, 'older snapshot ignored');
  store.applySnapshot({track: 'st', style: 'state', position: 1, value: {x: 1}});
  assert.equal(store.apply({track: 'st', position: 2, kind: 'append', value: 1}), 'gap', 'wrong kind for style');
  assert.equal(store.apply({track: 'st', position: 2, kind: 'set', value: {x: 2}}), 'applied');
  assert.deepEqual(store.positions(), {log: 4, st: 2});
  store.reset('run-2'); assert.equal(store.get('log'), null); assert.equal(seen.at(-1), null);
  const watched = []; store.applySnapshot({track: 'w', style: 'state', position: 1, value: 5});
  store.watch('w', track => watched.push(track?.value)); store.apply({track: 'w', position: 2, kind: 'set', value: 6});
  assert.deepEqual(watched, [5, 6]);
});
test('stream URL carries tracks and positions, never the credential', () => {
  const url = streamUrl(ORIGIN, ['a', 'b.c'], {a: 3});
  assert.equal(url, ORIGIN + '/stream?tracks=a%2Cb.c&after=a%3A3');
  assert.equal(streamUrl(ORIGIN, ['a'], {}, 'r1'), ORIGIN + '/stream?tracks=a&run=r1');
  assert.doesNotMatch(url, /token|Bearer/);
});
test('stream client resumes with positions, backs off, and stops on authentication failure', async () => {
  const c = clock(); const states = []; const events = []; const requests = [];
  let positions = {};
  const sources = [];
  const fetcher = async (url, options) => {
    requests.push({url, options});
    if (requests.length === 3) return new Response('', {status: 401});
    const source = eventResponse(); sources.push(source); return source.response;
  };
  const client = new StreamClient({origin: ORIGIN, credential: TOKEN, tracks: ['a'], positions: () => positions,
    onEvent: e => events.push(e), onState: s => states.push(s), fetcher, schedule: c.schedule, cancel: c.cancel});
  client.start(); await flush();
  assert.equal(requests[0].options.headers.Authorization, 'Bearer ' + TOKEN);
  sources[0].send('run', {runId: 'r'}); sources[0].send('emit', {track: 'a', position: 1}); await flush(); await flush();
  assert.equal(states.at(-1), 'connected'); assert.equal(events.length, 2);
  positions = {a: 1}; sources[0].end(); await flush(); await flush();
  assert.equal(states.at(-1), 'disconnected'); assert.equal([...c.tasks.values()][0].delay, 250);
  c.run(); await flush();
  assert.match(requests[1].url, /after=a%3A1/);
  sources[1].end(); await flush(); await flush(); c.run(); await flush(); await flush();
  assert.equal(states.at(-1), 'unauthorized'); assert.equal(client.running, false); assert.equal(c.tasks.size, 0);
});
test('a closed stream client ignores late events and schedules nothing', async () => {
  const c = clock(); const events = []; let source;
  const client = new StreamClient({origin: ORIGIN, credential: TOKEN, tracks: ['a'], positions: () => ({}),
    onEvent: e => events.push(e), onState: () => {}, fetcher: async () => { source = eventResponse(); return source.response; }, schedule: c.schedule, cancel: c.cancel});
  client.start(); await flush(); client.close();
  try { source.send('run', {runId: 'r'}); source.end(); } catch { /* aborted */ }
  await flush(); await flush();
  assert.equal(events.length, 0); assert.equal(c.tasks.size, 0);
});
test('invocations correlate Results, surface FlowErrors and report unknown outcomes', async () => {
  let id = 0; const bodies = [];
  const replies = [json({role: 'accepted', invocationId: 'i1'}, 202), json({role: 'flowError', invocationId: 'i2', code: 'conflict', message: 'stale pause'}, 409), Promise.reject(new TypeError('network'))];
  const client = new InvocationClient({origin: ORIGIN, credential: TOKEN, newId: () => 'i' + ++id,
    fetcher: async (url, options) => { bodies.push({url, body: JSON.parse(options.body), auth: options.headers.Authorization}); return replies.shift(); }});
  const first = client.invoke('aiBuddy.submit', {text: 'hi'});
  const second = assert.rejects(client.invoke('aiBuddy.continue', {}, {expect: {pause: 'p1'}}), e => e instanceof FlowError && e.code === 'conflict' && e.message === 'stale pause');
  const third = assert.rejects(client.invoke('x', {}), e => e instanceof FlowError && e.code === 'unknown');
  await flush();
  client.observe({invocationId: 'i1', role: 'result', value: {ok: true}});
  client.observe({invocationId: 'zz', role: 'result', value: 1});
  assert.deepEqual(await first, {ok: true});
  await second; await third;
  assert.deepEqual(bodies[1].body, {invocationId: 'i2', target: 'aiBuddy.continue', arguments: {}, expect: {pause: 'p1'}});
  assert.equal(bodies[0].url, ORIGIN + '/invoke'); assert.equal(bodies[0].auth, 'Bearer ' + TOKEN);
  replies.push(new Promise(() => {}));
  const pending = assert.rejects(client.invoke('y'), e => e.code === 'unknown'); await flush();
  client.runChanged();
  await pending;
});
test('a session snapshots every track, applies Emits, and resolves invocations from the stream', async () => {
  const c = clock(); let source; const posted = [];
  const snapshots = {status: {style: 'state', position: 1, value: {busy: false}}, [INVOCATIONS_TRACK]: {style: 'log', position: 0, items: []}};
  const fetcher = async (url, options = {}) => {
    const path = url.slice(ORIGIN.length);
    if (path === '/session') return json({runId: 'r1', title: 'Buddy', main: '/pack/app/ui/main.mjs', tracks: [{name: 'status', style: 'state'}]});
    if (path.startsWith('/snapshot')) { const name = new URL(url).searchParams.get('track'); return json({runId: 'r1', track: name, ...snapshots[name]}); }
    if (path.startsWith('/stream')) { source = eventResponse(); return source.response; }
    if (path === '/invoke') { posted.push(JSON.parse(options.body)); return json({role: 'accepted'}, 202); }
    throw new Error('unexpected ' + path);
  };
  const session = await openSession({origin: ORIGIN, credential: TOKEN, fetcher, schedule: c.schedule, cancel: c.cancel});
  await flush();
  source.send('run', {runId: 'r1'}); await flush(); await flush(); await flush();
  assert.deepEqual(session.tracks.get('status').value, {busy: false});
  source.send('emit', {track: 'status', position: 2, kind: 'set', value: {busy: true}}); await flush(); await flush();
  assert.deepEqual(session.tracks.get('status').value, {busy: true});
  const outcome = session.invoke('app.do', {n: 1}); await flush();
  source.send('emit', {track: INVOCATIONS_TRACK, position: 1, kind: 'append', value: {invocationId: posted[0].invocationId, role: 'result', value: 7}});
  assert.equal(await outcome, 7);
  const lost = assert.rejects(session.invoke('app.do', {}), e => e.code === 'unknown'); await flush();
  source.send('run', {runId: 'r2'}); await flush();
  await lost;
  assert.equal(session.runId, 'r2');
  session.close();
});
test('a gap during an in-flight snapshot triggers recovery without another Emit', async () => {
    const c = clock(); const source = eventResponse(); let release, snapshots = 0;
    const fetcher = async url => {
        if (url.endsWith('/session')) return json({runId: 'r', tracks: [{name: 'status', style: 'state'}]});
        if (url.includes('/stream')) return source.response;
        if (url.includes('track=status')) {
            if (++snapshots === 1) return new Promise(resolve => {
                release = () => resolve(json({runId: 'r', track: 'status', style: 'state', position: 0, value: 'old'}));
            });
            return json({runId: 'r', track: 'status', style: 'state', position: 1, value: 'new'});
        }
        return json({runId: 'r', track: INVOCATIONS_TRACK, style: 'log', position: 0, items: []});
    };
    const session = await openSession({origin: ORIGIN, credential: TOKEN, fetcher, schedule: c.schedule, cancel: c.cancel});
    try {
        source.send('run', {runId: 'r'}); await flush(); await flush();
        source.send('emit', {track: 'status', position: 1, kind: 'set', value: 'new'}); await flush();
        release(); await flush(); await flush(); c.run(); await flush(); await flush();
        assert.equal(snapshots, 2);
        assert.deepEqual(session.tracks.get('status'), {style: 'state', position: 1, value: 'new'});
    } finally { session.close(); source.end(); }
});

test('a failed initial snapshot is retried and close cancels recovery', async () => {
    const c = clock(); const source = eventResponse(); let attempts = 0;
    const fetcher = async url => {
        if (url.endsWith('/session')) return json({runId: 'r', tracks: []});
        if (url.includes('/stream')) return source.response;
        attempts++;
        if (attempts === 1) throw new TypeError('connection reset');
        return json({runId: 'r', track: INVOCATIONS_TRACK, style: 'log', position: 0, items: []});
    };
    const session = await openSession({origin: ORIGIN, credential: TOKEN, fetcher, schedule: c.schedule, cancel: c.cancel});
    source.send('run', {runId: 'r'}); await flush(); await flush();
    assert.equal(c.tasks.size, 1); c.run(); await flush(); await flush();
    assert.equal(session.tracks.position(INVOCATIONS_TRACK), 0);
    session.close(); source.end(); assert.equal(c.tasks.size, 0);
});

test('credential rotation on restart settles accepted actions as unknown', async () => {
    const c = clock(); const source = eventResponse(); let streams = 0;
    const fetcher = async url => {
        if (url.endsWith('/session')) return json({runId: 'r', tracks: []});
        if (url.includes('/stream')) return ++streams === 1 ? source.response : json({error: 'credential-required'}, 401);
        if (url.endsWith('/invoke')) return json({role: 'accepted'}, 202);
        return json({runId: 'r', track: INVOCATIONS_TRACK, style: 'log', position: 0, items: []});
    };
    const session = await openSession({origin: ORIGIN, credential: TOKEN, fetcher, schedule: c.schedule, cancel: c.cancel});
    source.send('run', {runId: 'r'}); await flush(); await flush();
    let settled = 'pending'; const action = session.invoke('app.slow').catch(error => { settled = error.code; });
    await flush(); source.end(); await flush(); c.run(); await flush(); await flush();
    assert.equal(settled, 'unknown'); await action;
    assert.equal(c.tasks.size, 0); session.close();
});

test('bootstrap reuses this tab\'s credential on reload and forgets a rejected one', async () => {
  const elements = {'boot-status': {textContent: ''}, app: {}};
  const document = {title: '', getElementById: id => elements[id]};
  const storage = memoryStorage(); storage.setItem(STORAGE_KEY, TOKEN);
  const seen = [];
  const rejected = await boot({document, location: {hash: '', origin: ORIGIN, pathname: '/', search: ''}, history: {}, storage,
    fetcher: async (url, options) => { seen.push(options.headers.Authorization); return new Response('', {status: 401}); }});
  assert.equal(rejected, null); assert.deepEqual(seen, ['Bearer ' + TOKEN]);
  assert.equal(storage.getItem(STORAGE_KEY), null); assert.match(elements['boot-status'].textContent, /newest link/);
  storage.setItem(STORAGE_KEY, TOKEN);
  const down = await boot({document, location: {hash: '', origin: ORIGIN, pathname: '/', search: ''}, history: {}, storage,
    fetcher: async () => { throw new TypeError('offline'); }});
  assert.equal(down, null); assert.equal(storage.getItem(STORAGE_KEY), TOKEN, 'an unreachable host does not forget the credential');
});
test('bootstrap needs the fragment credential and mounts the configured main entry', async () => {
  const elements = {'boot-status': {textContent: ''}, app: {}};
  const document = {title: '', getElementById: id => elements[id]};
  const none = await boot({document, location: {hash: '', origin: ORIGIN, pathname: '/', search: ''}, history: {}, storage: memoryStorage(), fetcher: async () => { throw new Error('no fetch'); }});
  assert.equal(none, null); assert.match(elements['boot-status'].textContent, /full link/);
  let mounted = null; let source;
  const fetcher = async url => {
    if (url.endsWith('/session')) return json({runId: 'r', title: 'T', main: '/pack/app/ui/main.mjs', tracks: []});
    if (url.includes('/snapshot')) return json({runId: 'r', track: INVOCATIONS_TRACK, style: 'log', position: 0, items: []});
    source = eventResponse(); return source.response;
  };
  const replaced = [];
  const session = await boot({document, location: {hash: '#token=' + TOKEN, origin: ORIGIN, pathname: '/', search: ''},
    history: {replaceState: (...a) => replaced.push(a)}, fetcher, storage: memoryStorage(),
    importer: async url => { assert.equal(url, '/pack/app/ui/main.mjs'); return {mount: (root, s) => { mounted = {root, s}; }}; }});
  assert.equal(mounted.root, elements.app); assert.equal(mounted.s, session); assert.equal(document.title, 'T'); assert.equal(replaced.length, 1);
  session.close();
});
test('kit sources have no storage (except the tab credential), cookies, console logging or HTML injection sinks', () => {
  for (const name of readdirSync(KIT).filter(item => item.endsWith('.mjs'))) {
    const source = readFileSync(new URL(name, KIT), 'utf8');
    assert.doesNotMatch(source, /localStorage|indexedDB|document\.cookie|console\.|innerHTML|outerHTML|insertAdjacentHTML|sendBeacon|eval\(/, name);
    if (name !== 'credential.mjs') assert.doesNotMatch(source, /sessionStorage/, name);
  }
});
test('per-track log limits from the descriptor bound the client mirror', () => {
  const store = new TrackStore({maxLogItems: 10});
  store.limit('small', 2); store.limit('huge', 50);
  store.applySnapshot({track: 'small', style: 'log', position: 3, items: [1, 2, 3]});
  assert.deepEqual(store.get('small').items, [2, 3]);
  store.apply({track: 'small', position: 4, kind: 'append', value: 4});
  assert.deepEqual(store.get('small').items, [3, 4]);
  assert.equal(store.maxItems('huge'), 10, 'never above the store-wide bound');
});
test('every kit module is declared in the manifest so the host can serve it', () => {
  const manifest = JSON.parse(readFileSync(new URL('manifest.json', KIT), 'utf8'));
  const declared = new Set(manifest.codeEntries.map(entry => entry.source));
  for (const name of readdirSync(KIT).filter(item => item.endsWith('.mjs'))) assert.ok(declared.has(name), name);
  assert.ok(manifest.codeEntries.every(entry => entry.format === 'js-module-source@1' && entry.role === 'library'));
});
