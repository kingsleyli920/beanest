'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createChatLocation } = require('./chat-location');

test('restart restores only the chosen chat on the new server; new task clears it', (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'beanest-location-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const filename = path.join(directory, 'last-chat.json');
  const location = createChatLocation(filename);
  const id = '1a10086d-51ab-42a2-b884-22e294492986';
  location.remember(`http://127.0.0.1:1111/#room=${id}`, 'http://127.0.0.1:1111/');
  assert.equal(location.initialUrl('http://127.0.0.1:2222/'), `http://127.0.0.1:2222/#room=${id}`);
  assert.deepEqual(JSON.parse(fs.readFileSync(filename)), { roomId: id });
  for (const invalid of ['https://example.com/', 'http://127.0.0.1:3333/',
    'http://127.0.0.1:1111/#room=not-a-room', 'http://127.0.0.1:1111/other']) {
    location.remember(invalid, 'http://127.0.0.1:1111/');
    assert.equal(location.initialUrl('http://127.0.0.1:2222/'), `http://127.0.0.1:2222/#room=${id}`);
  }
  location.remember('http://127.0.0.1:1111/', 'http://127.0.0.1:1111/');
  assert.equal(location.initialUrl('http://127.0.0.1:2222/'), 'http://127.0.0.1:2222/');
  fs.writeFileSync(filename, '{broken');
  assert.equal(location.initialUrl('http://127.0.0.1:2222/'), 'http://127.0.0.1:2222/');
});
