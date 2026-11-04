'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { webPageUrl, installExternalPages, OPEN_WEB_PAGE } = require('./external-pages');

test('setup pages accept HTTPS and reject executable protocols and embedded credentials', () => {
  assert.equal(webPageUrl('https://www.kimi.com/code/authorize_device?user_code=TEST-1234'),
    'https://www.kimi.com/code/authorize_device?user_code=TEST-1234');
  for (const value of ['file:///tmp/example', 'javascript:alert(1)', 'ssh://host',
    'https://user:password@example.com', 'https://example.com/\nsecret', 'http://example.com', null]) {
    assert.equal(webPageUrl(value), null);
  }
});

test('only the owned main frame can open a setup page and failures stay recoverable', async () => {
  let handler;
  const frame = { url: 'http://127.0.0.1:12345/' };
  const contents = { isDestroyed: () => false, mainFrame: frame };
  const window = { isDestroyed: () => false, webContents: contents };
  const opened = [];
  installExternalPages({ handle(channel, fn) { assert.equal(channel, OPEN_WEB_PAGE); handler = fn; } },
    { async openExternal(url) { opened.push(url); if (url.endsWith('/failure')) throw Error('private detail'); } },
    () => window, () => 'http://127.0.0.1:12345/');
  const event = { sender: contents, senderFrame: frame };
  assert.deepEqual(await handler(event, 'https://www.kimi.com/'), { ok: true });
  assert.deepEqual(opened, ['https://www.kimi.com/']);
  for (const untrusted of [{}, { ...event, senderFrame: { url: frame.url } },
    { ...event, sender: {} }]) {
    assert.deepEqual(await handler(untrusted, 'https://www.kimi.com/'), { ok: false });
  }
  assert.deepEqual(await handler(event, 'file:///tmp/example'), { ok: false });
  assert.deepEqual(await handler(event, 'https://www.kimi.com/', 'extra'), { ok: false });
  assert.equal(opened.length, 1);
  assert.deepEqual(await handler(event, 'https://www.kimi.com/failure'), { ok: false });
});
