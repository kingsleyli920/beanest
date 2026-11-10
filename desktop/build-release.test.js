'use strict';

// Catches: shipping a Python tree that cannot start or drags in dev-only weight,
// and a bundle that still links to libraries only the build machine has.
const test = require('node:test');
const { execFileSync } = require('node:child_process');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const {
  archiveApp, copyNotices, filesLeakingText, keepPythonPath, keepBackendPath, missingNotices, nonPortableLinks,
  prepareBundleForSigning, pruneBackendStage, releasePackagerOptions, requiredNoticePaths, verifyBundledRuntime,
  NOTICES_SOURCE, RELEASE_NOTICES, RELEASE_BUNDLE_ID, PYTHON_MINOR,
} = require('./build-release');

const lib = `lib/python${PYTHON_MINOR}`;

test('the runtime keeps what the server imports', () => {
  for (const kept of ['bin', `bin/python${PYTHON_MINOR}`, 'lib', lib, `lib/libpython${PYTHON_MINOR}.dylib`,
    `${lib}/asyncio/events.py`, `${lib}/sqlite3/__init__.py`, `${lib}/lib-dynload/_ssl.cpython-313-darwin.so`,
    `${lib}/encodings/utf_8.py`, `${lib}/LICENSE.txt`, `${lib}/ctypes/__init__.py`]) assert.equal(keepPythonPath(kept), true, kept);
});

test('the runtime drops development and GUI weight', () => {
  for (const dropped of ['bin/pip', 'bin/idle3', 'bin/python3-config', 'include', 'include/python3.13/Python.h', 'share', 'BUILD',
    `${lib}/site-packages`, `${lib}/site-packages/pip/__init__.py`, `${lib}/ensurepip/__init__.py`, `${lib}/idlelib/run.py`,
    `${lib}/tkinter/__init__.py`, `${lib}/test/__init__.py`, `${lib}/config-${PYTHON_MINOR}-darwin`,
    `${lib}/lib-dynload/_tkinter.cpython-313-darwin.so`, `${lib}/lib-dynload/_ctypes_test.cpython-313-darwin.so`,
    `${lib}/__pycache__/os.cpython-313.pyc`,
    'lib/libtcl8.6.dylib', 'lib/libtk8.6.dylib', 'lib/tcl8.6', 'lib/pkgconfig']) {
    assert.equal(keepPythonPath(dropped), false, dropped);
  }
});

test('the backend ships code and interface assets only', () => {
  for (const kept of ['room_server.py', 'room_ui', 'room_ui/app.js', 'room_ui/index.html', 'room_ui/style.css']) {
    assert.equal(keepBackendPath(kept), true, kept);
  }
  for (const dropped of ['__pycache__', '__pycache__/room_server.cpython-314.pyc', 'room_ui/notes.md', 'secrets.env',
    'mcp_server.py', 'room_mcp.py']) {
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

// Catches: a home-directory path baked into the packaged shell going unnoticed.
test('a single file such as app.asar is searched for the build-machine path', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'beanest-leak-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const asar = path.join(root, 'app.asar');
  await fs.writeFile(asar, Buffer.concat([Buffer.from([1, 0, 255]), Buffer.from('require("/Users/someone/x")')]));
  assert.deepEqual(await filesLeakingText(asar, '/Users/someone'), [asar]);
  assert.deepEqual(await filesLeakingText(asar, '/Users/other'), []);
});

// Catches: uv's lock file and console scripts shipping inside the app.
test('install leftovers are removed from the backend and packages stay', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'beanest-stage-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  await fs.mkdir(path.join(root, 'bin'));
  await fs.mkdir(path.join(root, 'pathspec'));
  await fs.writeFile(path.join(root, 'bin', 'pathspec'), '#!');
  await fs.writeFile(path.join(root, '.lock'), '');
  await fs.writeFile(path.join(root, 'pathspec', '__init__.py'), '');
  await pruneBackendStage(root);
  assert.deepEqual((await fs.readdir(root)).sort(), ['pathspec']);
});

// Catches: a notice listed for the build but never added to the repository.
test('every listed notice exists in the repository with content', async () => {
  assert.equal(RELEASE_NOTICES.length, 12);
  for (const name of RELEASE_NOTICES) {
    assert.ok((await fs.stat(path.join(NOTICES_SOURCE, name))).size > 0, name);
  }
});

async function completeResources(root) {
  for (const relative of requiredNoticePaths()) {
    await fs.mkdir(path.dirname(path.join(root, relative)), { recursive: true });
    await fs.writeFile(path.join(root, relative), 'text');
  }
}

// Catches: a release that passes the build while a required license is absent or empty.
test('a missing or empty notice is reported and a complete set passes', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'beanest-notices-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  await completeResources(root);
  assert.deepEqual(await missingNotices(root), []);
  await fs.rm(path.join(root, 'licenses', 'openssl-LICENSE.txt'));
  await fs.writeFile(path.join(root, 'ELECTRON-LICENSE'), '');
  assert.deepEqual(await missingNotices(root), ['ELECTRON-LICENSE', 'licenses/openssl-LICENSE.txt']);
});

test('the required list names third-party and project notices by their bundle paths', () => {
  const required = requiredNoticePaths();
  for (const relative of ['ELECTRON-LICENSE', 'LICENSES.chromium.html', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
    'python/lib/python3.13/LICENSE.txt', 'backend/pathspec-1.1.1.dist-info/licenses/LICENSE',
    'licenses/RELEASE-SOURCES.md', 'licenses/cpython-license.rst', 'licenses/mantle-LICENSE.md']) {
    assert.ok(required.includes(relative), relative);
  }
  assert.equal(required.some((relative) => relative.includes('RELEASE-NOTES')), false);
});

async function fixtureApp(root) {
  const resources = path.join(root, 'App.app', 'Contents', 'Resources');
  await completeResources(resources);
  await fs.writeFile(path.join(resources, 'app.asar'), 'shell');
  return { app: path.join(root, 'App.app'), resources };
}

// Catches: the build finishing normally when a required license did not make it into the app.
test('the bundle check fails the build when a notice is missing', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'beanest-verify-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const { app, resources } = await fixtureApp(root);
  await fs.rm(path.join(resources, 'licenses', 'libffi-LICENSE.txt'));
  await assert.rejects(verifyBundledRuntime(app), /^Error: release_bundle_missing_notice:licenses\/libffi-LICENSE\.txt$/);
});

// Catches: app.asar being left out of the build-path scan.
test('the bundle check fails the build when app.asar holds the build-machine path', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'beanest-verify-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const { app, resources } = await fixtureApp(root);
  await fs.writeFile(path.join(resources, 'app.asar'), Buffer.concat([Buffer.from([0, 255]), Buffer.from(os.homedir())]));
  await assert.rejects(verifyBundledRuntime(app), /^Error: release_bundle_build_path:.*app\.asar$/);
});

// Catches: Electron's or our notices left beside the app, where the zip would lose them.
test('notices land inside the app resources', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'beanest-copy-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const resources = path.join(root, 'app', 'Contents', 'Resources');
  await fs.mkdir(resources, { recursive: true });
  await fs.writeFile(path.join(root, 'LICENSE'), 'electron');
  await fs.writeFile(path.join(root, 'LICENSES.chromium.html'), 'chromium');
  await copyNotices(root, resources);
  assert.equal(await fs.readFile(path.join(resources, 'ELECTRON-LICENSE'), 'utf8'), 'electron');
  assert.deepEqual((await fs.readdir(path.join(resources, 'licenses'))).sort(), [...RELEASE_NOTICES].sort());
});

// Catches: adding files to the bundle after sealing it, which breaks the signature.
test('notices and clean-up happen before the bundle is signed', async () => {
  const source = await fs.readFile(path.join(__dirname, 'build-release.js'), 'utf8');
  const signing = source.indexOf("run('codesign', ['--force', '--deep'");
  assert.ok(signing > 0);
  for (const step of ['await copyNotices(', 'await prepareBundleForSigning(']) {
    const at = source.indexOf(step);
    assert.ok(at > 0 && at < signing, step);
  }
});

const macOnly = { skip: process.platform !== 'darwin' };

// Catches: the unused Electron icon and quarantine-style attributes reaching the seal.
test('signing preparation removes the stock icon and extended attributes', macOnly, async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'beanest-prep-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const resources = path.join(root, 'App.app', 'Contents', 'Resources');
  await fs.mkdir(resources, { recursive: true });
  await fs.writeFile(path.join(resources, 'electron.icns'), 'icon');
  await fs.writeFile(path.join(resources, 'beanest.icns'), 'icon');
  const app = path.join(root, 'App.app');
  execFileSync('xattr', ['-w', 'com.example.test', 'x', path.join(resources, 'beanest.icns')]);
  assert.match(execFileSync('xattr', [path.join(resources, 'beanest.icns')], { encoding: 'utf8' }), /com\.example\.test/);
  await prepareBundleForSigning(app);
  assert.deepEqual(await fs.readdir(resources), ['beanest.icns']);
  assert.doesNotMatch(execFileSync('xattr', [path.join(resources, 'beanest.icns')], { encoding: 'utf8' }), /com\.example\.test/);
});

// Catches: a release zip whose __MACOSX entries appear when people open it on other tools.
test('the archive carries no __MACOSX entries even for files with attributes', macOnly, async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'beanest-zip-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const app = path.join(root, '豆窝.app');
  await fs.mkdir(path.join(app, 'Contents'), { recursive: true });
  await fs.writeFile(path.join(app, 'Contents', 'file.txt'), 'content');
  execFileSync('xattr', ['-w', 'com.example.test', 'x', path.join(app, 'Contents', 'file.txt')]);
  const archive = path.join(root, 'out.zip');
  archiveApp(app, archive);
  const listing = execFileSync('zipinfo', ['-1', archive], { encoding: 'latin1', env: { ...process.env, LC_ALL: 'C' } });
  assert.ok(listing.includes('Contents/file.txt'), listing);
  assert.equal(listing.includes('__MACOSX'), false, listing);
});

// Catches: bumping the version in one place and shipping another number elsewhere.
test('every version declaration agrees with the desktop package', async () => {
  const root = path.join(__dirname, '..');
  const { version } = JSON.parse(await fs.readFile(path.join(__dirname, 'package.json'), 'utf8'));
  const lock = JSON.parse(await fs.readFile(path.join(__dirname, 'package-lock.json'), 'utf8'));
  assert.equal(lock.version, version);
  assert.equal(lock.packages[''].version, version);
  const pyproject = await fs.readFile(path.join(root, 'pyproject.toml'), 'utf8');
  assert.match(pyproject, new RegExp(`^version = "${version}"$`, 'm'));
  for (const [file, declaration] of [
    ['kimi_acp.py', `"version": "${version}"`], ['room_mcp.py', `version="${version}"`],
    ['mcp_server.py', `version="${version}"`], ['adapters.py', `'version': '${version}'`]]) {
    const source = await fs.readFile(path.join(root, 'src', 'local_agent_coordinator', file), 'utf8');
    assert.ok(source.includes(declaration), file);
  }
});
