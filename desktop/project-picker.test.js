'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const vm = require('node:vm');
const { trustedProjectSender, chooseProjectFolder, chooseProjectFiles, installProjectPicker, PROJECT_PICKER_CHANNEL, PROJECT_FOLDER_CHANNEL } = require('./project-picker');

function senderFixture() {
  const frame = { url: 'http://127.0.0.1:12345/' };
  const contents = { mainFrame: frame, isDestroyed: () => false };
  const window = { webContents: contents, isDestroyed: () => false };
  return { window, event: { sender: contents, senderFrame: frame }, url: frame.url };
}

async function filesFixture(t, count = 1) {
  const parent = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'beanest-picker-')));
  t.after(() => fs.rm(parent, { recursive: true, force: true }));
  const directory = path.join(parent, 'sample-project');
  await fs.mkdir(directory);
  const files = Array.from({ length: count }, (_, index) => path.join(directory, `file${index}.js`));
  await Promise.all(files.map((file) => fs.writeFile(file, 'must remain unread by the picker')));
  return { parent, directory, files };
}

function dialogs(directory, files, { cancelFolder = false, cancelFiles = false } = {}) {
  const calls = [];
  const responses = [
    { canceled: cancelFolder, filePaths: cancelFolder ? [] : [directory] },
    { canceled: cancelFiles, filePaths: cancelFiles ? [] : files },
  ];
  return { calls, showOpenDialog: async (window, options) => { calls.push({ window, options }); return responses.shift(); } };
}

test('only the active top-level local renderer may open a chooser', () => {
  const { window, event, url } = senderFixture();
  assert.equal(trustedProjectSender(event, window, url), true);
  assert.equal(trustedProjectSender({ ...event, sender: {} }, window, url), false);
  assert.equal(trustedProjectSender({ ...event, senderFrame: { url } }, window, url), false);
  for (const unsafe of ['https://example.invalid/', 'http://127.0.0.1:12346/', 'file:///tmp/private']) {
    event.senderFrame.url = unsafe;
    assert.equal(trustedProjectSender(event, window, url), false);
  }
  event.senderFrame.url = url;
  window.isDestroyed = () => true;
  assert.equal(trustedProjectSender(event, window, url), false);
  assert.equal(trustedProjectSender(event, null, url), false);
});

test('choosing directory and 1..12 explicit files returns metadata without reading or listing files', async (t) => {
  const { directory, files } = await filesFixture(t, 12);
  const dialog = dialogs(directory, files);
  t.mock.method(fs, 'readFile', () => { throw new Error('picker must not read content'); });
  t.mock.method(fs, 'readdir', () => { throw new Error('picker must not scan directories'); });
  const result = await chooseProjectFiles(dialog, {}, () => true);
  assert.deepEqual(result, { cancelled: false, directory, suggestedName: 'sample-project',
    files: files.map((file) => path.basename(file)) });
  assert.equal(dialog.calls.length, 2);
  assert.deepEqual(dialog.calls[0].options.properties, ['openDirectory', 'noResolveAliases']);
  assert.equal(dialog.calls[1].options.defaultPath, directory);
  assert.ok(dialog.calls[1].options.properties.includes('multiSelections'));
});

test('cancelling either dialog returns only cancelled without a selection', async (t) => {
  const { directory, files } = await filesFixture(t);
  const first = dialogs(directory, files, { cancelFolder: true });
  assert.deepEqual(await chooseProjectFiles(first, {}, () => true), { cancelled: true });
  assert.equal(first.calls.length, 1);
  const second = dialogs(directory, files, { cancelFiles: true });
  assert.deepEqual(await chooseProjectFiles(second, {}, () => true), { cancelled: true });
  assert.equal(second.calls.length, 2);
});

test('rejects outside, duplicate, unsafe, empty and oversized selections', async (t) => {
  const { parent, directory, files } = await filesFixture(t, 13);
  const outside = path.join(parent, 'outside.js');
  await fs.writeFile(outside, 'outside');
  const cases = [
    [[outside], 'project_selection_outside_directory'],
    [[files[0], files[0]], 'invalid_project_selection'],
    [[path.join(directory, '.env')], 'project_selection_unsafe_path'],
    [[], 'invalid_project_selection'],
    [files, 'invalid_project_selection'],
  ];
  for (const [selection, code] of cases) {
    await assert.rejects(chooseProjectFiles(dialogs(directory, selection), {}, () => true), { message: code });
  }
});

test('rejects selected symlinks, hardlinks and directory symlinks', async (t) => {
  const { parent, directory, files } = await filesFixture(t);
  const symlink = path.join(directory, 'linked.js');
  await fs.symlink(files[0], symlink);
  await assert.rejects(chooseProjectFiles(dialogs(directory, [symlink]), {}, () => true),
    { message: 'project_selection_unsafe_path' });
  const hardlink = path.join(directory, 'hard.js');
  await fs.link(files[0], hardlink);
  await assert.rejects(chooseProjectFiles(dialogs(directory, [hardlink]), {}, () => true),
    { message: 'project_selection_unsafe_path' });
  const alias = path.join(parent, 'directory-alias');
  await fs.symlink(directory, alias);
  await assert.rejects(chooseProjectFiles(dialogs(alias, []), {}, () => true),
    { message: 'project_selection_unsafe_path' });
});

test('navigation during either native dialog prevents returning local metadata', async (t) => {
  const { directory, files } = await filesFixture(t);
  for (const invalidateAt of [1, 2]) {
    let trusted = true;
    const dialog = dialogs(directory, files);
    const original = dialog.showOpenDialog;
    dialog.showOpenDialog = async (...args) => {
      const result = await original(...args);
      if (dialog.calls.length === invalidateAt) trusted = false;
      return result;
    };
    await assert.rejects(chooseProjectFiles(dialog, {}, () => trusted), { message: 'project_selection_unavailable' });
    assert.equal(dialog.calls.length, invalidateAt);
  }
});

test('sender invalidation after every metadata await stops subsequent path handling', async (t) => {
  const { directory, files } = await filesFixture(t);
  const lstat = fs.lstat;
  const realpath = fs.realpath;
  for (const boundary of ['directory-lstat', 'directory-realpath', 'file-lstat', 'file-realpath']) {
    let trusted = true;
    const operations = [];
    const statMock = t.mock.method(fs, 'lstat', async (filename) => {
      operations.push(filename === directory ? 'directory-lstat' : 'file-lstat');
      const value = await lstat(filename);
      if (operations.at(-1) === boundary) trusted = false;
      return value;
    });
    const pathMock = t.mock.method(fs, 'realpath', async (filename) => {
      operations.push(filename === directory ? 'directory-realpath' : 'file-realpath');
      const value = await realpath(filename);
      if (operations.at(-1) === boundary) trusted = false;
      return value;
    });
    await assert.rejects(chooseProjectFiles(dialogs(directory, files), {}, () => trusted),
      { message: 'project_selection_unavailable' });
    assert.equal(operations.at(-1), boundary, 'must not start another metadata operation after authority changed');
    statMock.mock.restore(); pathMock.mock.restore();
  }
});

test('IPC denies unknown senders and concurrent choosers, and sanitizes unexpected errors', async (t) => {
  const { window, event, url } = senderFixture();
  let handler;
  let release;
  let calls = 0;
  const dialog = { showOpenDialog: async () => { calls += 1; return new Promise((resolve) => { release = resolve; }); } };
  installProjectPicker({ handle(channel, callback) { if (channel === PROJECT_PICKER_CHANNEL) handler = callback; } },
    dialog, () => window, () => url);
  assert.deepEqual(await handler({}), { ok: false, error: 'project_selection_unavailable' });
  assert.deepEqual(await handler(event, 'arbitrary path'), { ok: false, error: 'project_selection_unavailable' });
  assert.equal(calls, 0);
  const pending = handler(event);
  assert.deepEqual(await handler(event), { ok: false, error: 'project_selection_unavailable' });
  assert.equal(calls, 1);
  release({ canceled: true, filePaths: [] });
  assert.deepEqual(await pending, { ok: true, value: { cancelled: true } });
  let failing;
  installProjectPicker({ handle(channel, callback) { if (channel === PROJECT_PICKER_CHANNEL) failing = callback; } },
    { showOpenDialog: async () => { throw new Error('/private/SECRET'); } }, () => window, () => url);
  assert.deepEqual(await failing(event), { ok: false, error: 'project_selection_unavailable' });
});

test('preload exposes bounded chooser and open notifications without raw IPC', async () => {
  let exposed;
  const calls = [];
  const listeners = new Map();
  let result = { ok: true, value: { cancelled: true } };
  const source = await fs.readFile(path.join(__dirname, 'preload.js'), 'utf8');
  vm.runInNewContext(source, {
    require(name) {
      assert.equal(name, 'electron');
      return { contextBridge: { exposeInMainWorld(key, api) { assert.equal(key, 'beanestDesktop'); exposed = api; } },
        ipcRenderer: {
          on(channel, listener) { listeners.set(channel, listener); },
          removeListener(channel, listener) { if (listeners.get(channel) === listener) listeners.delete(channel); },
          async invoke(...args) { calls.push(args); if (result instanceof Error) throw result; return result; },
        } };
    },
  });
  assert.deepEqual(Object.keys(exposed).sort(), ['chooseProjectFiles', 'chooseProjectFolder', 'currentLocale', 'keepsAutonomousWorkAwake', 'onOpened', 'openWebPage', 'setLocale']);
  assert.equal(exposed.keepsAutonomousWorkAwake, true);
  assert.equal(Object.isFrozen(exposed), true);
  const notifications = [];
  const unsubscribe = exposed.onOpened((...args) => notifications.push(args));
  assert.deepEqual([...listeners.keys()], ['beanest:opened']);
  listeners.get('beanest:opened')({ arbitraryIPC: true }, '/private/path');
  assert.deepEqual(notifications, [[]]);
  unsubscribe();
  assert.equal(listeners.size, 0);
  assert.equal((await exposed.chooseProjectFiles('ignored arbitrary input')).cancelled, true);
  assert.deepEqual(calls, [[PROJECT_PICKER_CHANNEL]]);
  assert.equal((await exposed.chooseProjectFolder()).cancelled, true);
  assert.deepEqual(calls.at(-1), [PROJECT_FOLDER_CHANNEL]);
  result = { ok: false, error: 'project_selection_outside_directory' };
  await assert.rejects(exposed.chooseProjectFiles(), { message: 'project_selection_outside_directory' });
  result = new Error('native failure with /private/SECRET');
  await assert.rejects(exposed.chooseProjectFiles(), { message: 'project_selection_unavailable' });
  assert.equal(await exposed.openWebPage('https://www.kimi.com/'), false);
  result = { ok: true };
  assert.equal(await exposed.openWebPage('https://www.kimi.com/'), true);
  assert.deepEqual(calls.at(-1), ['beanest:open-web-page', 'https://www.kimi.com/']);
});


test('whole folder selection uses one dialog and returns only directory metadata', async (t) => {
  const { directory, files } = await filesFixture(t, 13);
  const dialog = dialogs(directory, files);
  t.mock.method(fs, 'readFile', () => { throw new Error('must not read content'); });
  t.mock.method(fs, 'readdir', () => { throw new Error('must not list content'); });
  assert.deepEqual(await chooseProjectFolder(dialog, {}, () => true), {
    cancelled: false, path: directory, name: 'sample-project',
  });
  assert.equal(dialog.calls.length, 1);
  assert.deepEqual(await chooseProjectFolder(dialogs(directory, [], { cancelFolder: true }), {}, () => true), { cancelled: true });
  await assert.rejects(chooseProjectFolder(dialogs(files[0], []), {}, () => true), { message: 'project_selection_unsafe_path' });
});

test('folder and explicit file IPC share one chooser lock and recheck sender navigation', async () => {
  const { window, event, url } = senderFixture();
  const handlers = new Map(); let release;
  installProjectPicker({ handle: (channel, handler) => handlers.set(channel, handler) },
    { showOpenDialog: () => new Promise((resolve) => { release = resolve; }) }, () => window, () => url);
  const pending = handlers.get(PROJECT_FOLDER_CHANNEL)(event);
  assert.deepEqual(await handlers.get(PROJECT_PICKER_CHANNEL)(event), { ok: false, error: 'project_selection_unavailable' });
  event.senderFrame.url = 'https://example.invalid/';
  release({ canceled: true });
  assert.deepEqual(await pending, { ok: false, error: 'project_selection_unavailable' });
});
