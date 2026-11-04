'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { strings, loadLocale, saveLocale, installLocale, LOCALE_CHANNEL } = require('./locale');
const { menuTemplate } = require('./menu');
const { chooseProjectFolder } = require('./project-picker');

test('locale persists in its own private preference, preserving old profiles and system defaults', (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'beanest-locale-'));
  t.after(() => fs.rmSync(directory, { recursive: true }));
  const filename = path.join(directory, 'locale.json');
  assert.equal(loadLocale(filename, 'en-US', true), 'zh-CN');
  assert.equal(loadLocale(filename, 'zh-Hans'), 'zh-CN');
  assert.equal(loadLocale(filename, 'en-GB'), 'en');
  saveLocale(filename, 'en');
  assert.equal(loadLocale(filename, 'zh-CN', true), 'en');
  assert.equal(fs.statSync(filename).mode & 0o777, 0o600);
  assert.throws(() => saveLocale(filename, '/private/path'), /invalid_locale/);
  fs.writeFileSync(filename, JSON.stringify({ locale: 'invalid' }));
  assert.equal(loadLocale(filename, 'zh-CN'), 'zh-CN');
  assert.equal(fs.readdirSync(directory).length, 1);
});

test('English menus and actual folder dialog preserve native roles and selection safeguards', async () => {
  const menu = menuTemplate('darwin', () => {}, 'en');
  assert.equal(menu[0].label, 'Beanest');
  const entries = menu.flatMap((item) => item.submenu);
  assert.equal(entries.find((item) => item.role === 'quit').label, 'Quit Beanest');
  assert.equal(entries.find((item) => item.role === 'quit').click, undefined);
  for (const role of ['undo', 'redo', 'cut', 'copy', 'paste', 'selectAll']) assert.ok(entries.some((item) => item.role === role));
  let options;
  assert.deepEqual(await chooseProjectFolder({ showOpenDialog: async (_window, value) => {
    options = value; return { canceled: true };
  } }, {}, () => true, 'en'), { cancelled: true });
  assert.equal(options.title, 'Choose a Project Folder');
  assert.deepEqual(options.properties, ['openDirectory', 'noResolveAliases']);
  assert.ok(!/[\u3400-\u9fff]/u.test(JSON.stringify(menu)));
  assert.equal(strings('en').startupFailed, 'Beanest could not start');
});

test('only trusted top-level renderer and supported locale can change native language', async () => {
  const frame = { url: 'http://127.0.0.1:12345/' };
  const webContents = { mainFrame: frame, isDestroyed: () => false };
  const window = { webContents, isDestroyed: () => false };
  const event = { sender: webContents, senderFrame: frame };
  let handler;
  const changes = [];
  installLocale({ handle(channel, callback) { assert.equal(channel, LOCALE_CHANNEL); handler = callback; } },
    () => window, () => frame.url, (locale) => changes.push(locale));
  assert.deepEqual(await handler(event, 'en'), { ok: true, locale: 'en' });
  for (const args of [[], ['en', 'extra'], ['unsupported'], ['zh-CN<script>']]) assert.deepEqual(await handler(event, ...args), { ok: false });
  assert.deepEqual(await handler({ ...event, senderFrame: { url: frame.url } }, 'zh-CN'), { ok: false });
  assert.deepEqual(changes, ['en']);
});

test('preload exposes only bounded locale values and does not relay native errors', async () => {
  let bridge;
  const calls = [];
  let answer = { ok: true, locale: 'en' };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'preload.js'), 'utf8'), {
    process: { argv: ['electron', '--beanest-locale=en'] },
    require: () => ({ contextBridge: { exposeInMainWorld(_name, value) { bridge = value; } }, ipcRenderer: {
      invoke: async (...args) => { calls.push(args); if (answer instanceof Error) throw answer; return answer; },
    } }),
  });
  assert.equal(bridge.currentLocale, 'en');
  assert.equal((await bridge.setLocale('en')).ok, true);
  assert.deepEqual(calls, [['beanest:set-locale', 'en']]);
  assert.equal((await bridge.setLocale('../private')).ok, false);
  assert.equal(calls.length, 1);
  answer = new Error('private native details');
  assert.equal((await bridge.setLocale('zh-CN')).ok, false);
});
