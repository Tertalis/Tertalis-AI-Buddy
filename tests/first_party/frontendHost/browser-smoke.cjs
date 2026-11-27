// file: tests/first_party/frontendHost/browser-smoke.cjs ; version: 2
/** Real browser driver; the launch URL arrives via private stdin and is never printed or logged. */
const {createInterface} = require('node:readline');
const {createRequire} = require('node:module');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const lines = createInterface({input: process.stdin});
const buffered = [], waiting = [];
lines.on('line', line => { const value = JSON.parse(line); if (waiting.length) waiting.shift()(value); else buffered.push(value); });
const next = () => buffered.length ? Promise.resolve(buffered.shift()) : new Promise(resolve => waiting.push(resolve));
async function control(action) { process.stdout.write(JSON.stringify({action}) + '\n'); assert.equal((await next()).ok, true); }

/** Credential lifetime and privacy: gone from the address bar, page, storage and cookies. */
async function privacy(page, token) {
  assert.equal(page.url().includes(token), false);
  assert.equal((await page.content()).includes(token), false);
  assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
  assert.equal(await page.evaluate(() => document.cookie), '');
}
const text = (page, id) => page.locator('#' + id).textContent();

(async () => {
  const config = await next();
  const token = config.url.split('#token=')[1];
  const playwright = createRequire(path.resolve(process.argv[2], 'package.json'))(process.argv[2]);
  const browser = await playwright.chromium.launch({executablePath: process.argv[3], headless: true});
  const context = await browser.newContext({viewport: {width: 1100, height: 700}});
  const errors = [], requested = [];
  context.on('request', request => requested.push(request.url()));
  fs.mkdirSync(process.argv[4], {recursive: true});
  const pages = [];
  try {
    const open = async () => {
      const page = await context.newPage(); pages.push(page);
      page.on('pageerror', error => errors.push(error.name));
      await page.goto(config.url);
      await page.waitForSelector('#app[data-ready="1"]');
      await page.waitForFunction(() => document.getElementById('connection').textContent === 'connected');
      return page;
    };
    const first = await open();
    await privacy(first, token);
    assert.equal(await text(first, 'status'), '{"step":0}');
    assert.equal(await first.title(), 'Frontend smoke');

    await first.locator('#echo').click();
    await first.waitForFunction(() => document.getElementById('result').textContent.startsWith('result:'));
    assert.equal(await text(first, 'result'), 'result:{"n":1}');

    const second = await open();
    await control('advance-status');
    for (const page of [first, second]) await page.waitForFunction(() => document.getElementById('status').textContent === '{"step":1}');
    await first.screenshot({path: path.join(process.argv[4], 'two-views.png')});

    await first.locator('#continue').click();
    await first.waitForFunction(() => document.getElementById('result').textContent !== 'result:{"n":1}');
    await second.locator('#continue').click();
    await second.waitForFunction(() => document.getElementById('result').textContent.startsWith('error:'));
    assert.equal(await text(first, 'result'), 'result:"continued"');
    assert.equal(await text(second, 'result'), 'error:conflict');

    const noToken = await context.newPage(); pages.push(noToken);
    await noToken.goto(config.origin + '/');
    await noToken.waitForFunction(() => document.getElementById('boot-status').textContent.includes('full link'));

    const third = await open();
    const fourth = await open();
    const fifth = await open();
    await control('advance-status');
    for (const page of [first, second, third, fourth, fifth])
      await page.waitForFunction(() => document.getElementById('status').textContent === '{"step":2}');
    const sixth = await context.newPage(); pages.push(sixth);
    await sixth.goto(config.url);
    await sixth.waitForSelector('#app[data-ready="1"]');
    await sixth.waitForFunction(() => document.getElementById('connection').textContent === 'busy', null, {timeout: 10000});
    await sixth.close(); pages.pop();
    await third.close();
    await fourth.close();
    await fifth.close();

    const accepted = first.waitForResponse(response => response.url().endsWith('/invoke') && response.status() === 202);
    await first.locator('#pending').click(); await accepted;
    await control('restart-host');
    await first.waitForFunction(() => document.getElementById('result').textContent === 'error:unknown');
    for (const page of [first, second])
      await page.waitForFunction(() => document.getElementById('connection').textContent === 'unauthorized');
    await first.screenshot({path: path.join(process.argv[4], 'host-restarted.png')});
    await control('close-host');

    assert.equal(errors.length, 0, 'page errors: ' + errors.join(','));
    assert.ok(requested.every(url => !url.includes(token) || url.endsWith('#token=' + token)), 'credential only ever in the navigation fragment');
    assert.ok(requested.filter(url => url.includes('?')).every(url => !/token|credential/i.test(new URL(url).search)));
    process.stdout.write(JSON.stringify({result: 'passed', browser: await browser.version(), views: 5, viewLimitEnforced: true,
      conflictResolved: true, restartPendingUnknown: true, screenshots: ['two-views', 'host-restarted']}) + '\n');
  } catch (error) {
    await pages[0]?.screenshot({path: path.join(process.argv[4], 'diagnostic.png')}).catch(() => {});
    process.stderr.write(JSON.stringify({error: error.message.replaceAll(token, '<token>'), pageErrorTypes: errors}) + '\n');
    throw error;
  } finally { await context.close(); await browser.close(); }
  process.exit(0);
})().catch(error => { process.stderr.write('Browser smoke failed: ' + error.name + '\n'); process.exit(1); });
