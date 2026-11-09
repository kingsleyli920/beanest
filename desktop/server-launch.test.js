'use strict';

// Catches: a release app starting the wrong interpreter, writing user data into
// its bundle, or losing the checkout behaviour existing installs rely on.
const test = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {
  BUNDLED_BOOTSTRAP, parseInstallation, dataDirectoryName, runtimeDirectoryFor, serverLaunch,
} = require('./server-launch');

test('metadata selects bundled or source installs and rejects anything else', () => {
  assert.deepEqual(parseInstallation('{"bundled":true}'), { kind: 'bundled' });
  assert.deepEqual(parseInstallation('{"projectRoot":"/repo"}'), { kind: 'source', root: '/repo' });
  for (const raw of ['not json', '{}', 'null', '{"projectRoot":"relative/path"}', '{"bundled":"yes"}']) {
    assert.deepEqual(parseInstallation(raw), { kind: 'invalid' }, raw);
  }
});

test('a bundled install keeps its data outside the app and apart from a source install', () => {
  const bundled = { kind: 'bundled' };
  const source = { kind: 'source', root: '/repo' };
  assert.equal(dataDirectoryName(bundled), 'io.github.kingsleyli920.beanest');
  assert.equal(dataDirectoryName(source), 'Local Agent Coordinator');
  assert.equal(runtimeDirectoryFor(bundled, '/data/Beanest'), '/data/Beanest/desktop-rooms');
  assert.equal(runtimeDirectoryFor(source, '/ignored'), '/repo/runtime/desktop-rooms');
  assert.throws(() => runtimeDirectoryFor({ kind: 'invalid' }, '/data'), /Invalid local installation/);
});

test('a bundled launch uses only Resources, is isolated and leaves the environment alone', () => {
  const launch = serverLaunch({ kind: 'bundled' }, {
    resourcesPath: '/App/Resources', runtime: '/data/rooms', env: { PATH: '/bin' }, exists: () => assert.fail('no probing'),
  });
  assert.equal(launch.command, '/App/Resources/python/bin/python3');
  assert.deepEqual(launch.args.slice(0, 3), ['-I', '-B', '-c']);
  assert.deepEqual(launch.args.slice(4), ['/App/Resources/backend', '--runtime', '/data/rooms', '--port', '0']);
  assert.equal(launch.cwd, '/data/rooms');
  assert.deepEqual(launch.env, { PATH: '/bin' });
});

test('a source launch prefers the checkout virtualenv and falls back to python3', () => {
  const options = { resourcesPath: '/unused', runtime: '/repo/rt', env: { PATH: '/bin' } };
  const venv = serverLaunch({ kind: 'source', root: '/repo' }, { ...options, exists: (p) => p === '/repo/.venv/bin/python' });
  assert.equal(venv.command, '/repo/.venv/bin/python');
  assert.deepEqual(venv.args, ['-m', 'local_agent_coordinator.room_server', '--runtime', '/repo/rt', '--port', '0']);
  assert.equal(venv.cwd, '/repo');
  assert.deepEqual(venv.env, { PATH: '/bin', PYTHONPATH: '/repo/src' });
  assert.equal(serverLaunch({ kind: 'source', root: '/repo' }, { ...options, exists: () => false }).command, 'python3');
  assert.throws(() => serverLaunch({ kind: 'invalid' }, { ...options, exists: () => false }), /Invalid local installation/);
});

test('the bootstrap runs the server as __main__ with its arguments, ignoring a planted PYTHONPATH', (t) => {
  const probe = spawnSync('python3', ['--version']);
  if (probe.error) return t.skip('python3 is not available');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'beanest bootstrap '));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const write = (base) => {
    const directory = path.join(root, base, 'local_agent_coordinator');
    fs.mkdirSync(directory, { recursive: true });
    fs.writeFileSync(path.join(directory, '__init__.py'), '');
    fs.writeFileSync(path.join(directory, 'room_server.py'),
      `import json,os,sys\nprint(json.dumps(["${base}",__name__,os.path.basename(sys.argv[0]),sys.argv[1:]]))\n`);
  };
  write('backend');
  write('planted');
  const result = spawnSync('python3', ['-I', '-B', '-c', BUNDLED_BOOTSTRAP, path.join(root, 'backend'), '--runtime', '/rt', '--port', '0'],
    { encoding: 'utf8', cwd: path.join(root, 'planted'), env: { ...process.env, PYTHONPATH: path.join(root, 'planted') } });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), ['backend', '__main__', 'room_server.py', ['--runtime', '/rt', '--port', '0']]);
});
