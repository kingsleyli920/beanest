'use strict';

// Catches: shipping a Python tree that cannot start or drags in dev-only weight,
// and a bundle that still links to libraries only the build machine has.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const {
  filesLeakingText, keepPythonPath, keepBackendPath, nonPortableLinks, releasePackagerOptions, RELEASE_BUNDLE_ID, PYTHON_MINOR,
} = require('./build-release');

const lib = `lib/python${PYTHON_MINOR}`;

test('the runtime keeps what the server imports', () => {
  for (const kept of ['bin', `bin/python${PYTHON_MINOR}`, 'lib', lib, `lib/libpython${PYTHON_MINOR}.dylib`,
    `${lib}/asyncio/events.py`, `${lib}/sqlite3/__init__.py`, `${lib}/lib-dynload/_ssl.cpython-313-darwin.so`,
    `${lib}/encodings/utf_8.py`, `${lib}/LICENSE.txt`]) assert.equal(keepPythonPath(kept), true, kept);
});

test('the runtime drops development and GUI weight', () => {
  for (const dropped of ['bin/pip', 'bin/idle3', 'bin/python3-config', 'include', 'include/python3.13/Python.h', 'share', 'BUILD',
    `${lib}/site-packages`, `${lib}/site-packages/pip/__init__.py`, `${lib}/ensurepip/__init__.py`, `${lib}/idlelib/run.py`,
    `${lib}/tkinter/__init__.py`, `${lib}/test/__init__.py`, `${lib}/config-${PYTHON_MINOR}-darwin`,
    `${lib}/lib-dynload/_tkinter.cpython-313-darwin.so`, `${lib}/__pycache__/os.cpython-313.pyc`,
    'lib/libtcl8.6.dylib', 'lib/libtk8.6.dylib', 'lib/tcl8.6', 'lib/pkgconfig']) {
    assert.equal(keepPythonPath(dropped), false, dropped);
  }
});

test('the backend ships code and interface assets only', () => {
  for (const kept of ['room_server.py', 'room_ui', 'room_ui/app.js', 'room_ui/index.html', 'room_ui/style.css']) {
    assert.equal(keepBackendPath(kept), true, kept);
  }
  for (const dropped of ['__pycache__', '__pycache__/room_server.cpython-314.pyc', 'room_ui/notes.md', 'secrets.env']) {
    assert.equal(keepBackendPath(dropped), false, dropped);
  }
});

test('only links a clean Mac can satisfy pass', () => {
  const output = `/App/python3:
	@executable_path/../lib/libpython3.13.dylib (compatibility version 3.13.0, current version 3.13.0)
	@rpath/libz.dylib (compatibility version 1.0.0, current version 1.0.0)
	/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1351.0.0)
	/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation (compatibility version 150.0.0, current version 3502.1.255)
	/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib (compatibility version 3.0.0, current version 3.5.0)
	/usr/local/lib/libffi.8.dylib (compatibility version 9.0.0, current version 9.0.0)
`;
  assert.deepEqual(nonPortableLinks(output),
    ['/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib', '/usr/local/lib/libffi.8.dylib']);
});

test('the release has its own identity and carries python and backend as resources', () => {
  const options = releasePackagerOptions({ stage: '/s', build: '/b', root: '/r', metadata: '/b/coordinator.json',
    python: '/b/python', backend: '/b/backend', electronVersion: '44.9.3' });
  assert.equal(options.appBundleId, 'io.github.kingsleyli920.beanest');
  assert.equal(RELEASE_BUNDLE_ID, 'io.github.kingsleyli920.beanest');
  assert.deepEqual(options.extraResource,
    ['/b/coordinator.json', '/r/LICENSE', '/r/THIRD_PARTY_NOTICES.md', '/b/python', '/b/backend']);
});

test('the build-machine path is found in nested text and binary files', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'beanest-leak-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  await fs.mkdir(path.join(root, 'nested'));
  await fs.writeFile(path.join(root, 'clean.py'), 'prefix = "/beanest-python"');
  await fs.writeFile(path.join(root, 'nested', 'leak.pyc'), Buffer.concat([Buffer.from([0, 255]), Buffer.from('/Users/someone/x')]));
  assert.deepEqual(await filesLeakingText(root, '/Users/someone'), [path.join(root, 'nested', 'leak.pyc')]);
});
