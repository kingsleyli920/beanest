'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { menuTemplate } = require('./menu');

test('localized menus retain native text editing and graceful quit roles', () => {
  const template = menuTemplate('darwin', () => {});
  assert.equal(template[0].label, '豆窝');
  const entries = template.flatMap((item) => item.submenu);
  const roles = new Map(entries.filter((item) => item.role).map((item) => [item.role, item]));
  assert.equal(roles.get('about').label, '关于豆窝');
  assert.equal(roles.get('quit').label, '退出豆窝');
  assert.equal(roles.get('quit').click, undefined, 'quit must use Electron/app.before-quit cleanup');
  for (const role of ['undo', 'redo', 'cut', 'copy', 'paste', 'selectAll']) {
    assert.ok(roles.has(role), `native ${role} must keep working in text inputs`);
  }
  assert.ok(entries.filter((item) => item.label).every((item) => !item.label.includes('Local Agent Coordinator')));
});

test('the actual main entry pins the old storage and lock directory before branded startup', () => {
  const paths = new Map([['appData', '/virtual/Application Support']]);
  const operations = [];
  const legacy = '/virtual/Application Support/Local Agent Coordinator';
  let name;
  const app = {
    getPath(key) { assert.ok(paths.has(key)); return paths.get(key); },
    setPath(key, value) { paths.set(key, value); operations.push(`path:${key}`); },
    setName(value) { name = value; },
    commandLine: { appendSwitch() {} },
    requestSingleInstanceLock() {
      assert.equal(paths.get('userData'), legacy);
      assert.equal(paths.get('sessionData'), legacy);
      assert.equal(name, '豆窝');
      operations.push('lock'); return true;
    },
    on() {}, whenReady: () => new Promise(() => {}),
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'main.js'), 'utf8'), {
    __dirname, process: { platform: 'darwin' },
    require(name) {
      if (name === 'electron') return { app };
      if (name === 'node:fs') return {
        mkdirSync(directory) { assert.equal(directory, legacy); operations.push('mkdir'); },
        chmodSync() { throw new Error('must not modify existing storage permissions'); },
        readFileSync() { throw new Error('startup test must not read private runtime'); },
      };
      if (name === 'node:child_process') return { spawn() { throw new Error('must not launch a service in this test'); } };
      return require(name);
    },
  });
  assert.deepEqual(operations, ['mkdir', 'path:userData', 'path:sessionData', 'lock']);
});
