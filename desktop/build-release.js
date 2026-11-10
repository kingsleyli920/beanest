'use strict';

// A release bundle needs no checkout, Python or Node on the user's machine: it
// carries a pinned standalone CPython and the backend in Resources. Unlike the
// local bundle it has its own identity and data directory, so both can coexist.
const { execFileSync } = require('node:child_process');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { localPackagerOptions, stageShell, verifyLocalBundle, APP_NAME } = require('./build-local');

const RELEASE_BUNDLE_ID = 'io.github.kingsleyli920.beanest';
const PYTHON_VERSION = '3.13.9';
const PYTHON_MINOR = PYTHON_VERSION.split('.').slice(0, 2).join('.');
const BACKEND_PACKAGE = 'local_agent_coordinator';
const ELECTRON_NOTICES = [['LICENSE', 'ELECTRON-LICENSE'], ['LICENSES.chromium.html', 'LICENSES.chromium.html']];
// Copied into Contents/Resources/licenses; each is listed in THIRD_PARTY_NOTICES.md.
const NOTICES_SOURCE = path.join(__dirname, 'release-notices');
const RELEASE_NOTICES = ['RELEASE-SOURCES.md', 'bzip2-LICENSE.txt', 'cpython-license.rst', 'expat-COPYING.txt',
  'hacl-star-LICENSE.txt', 'libffi-LICENSE.txt', 'libmpdec-COPYRIGHT.txt', 'libuuid-LICENSE.txt', 'mantle-LICENSE.md',
  'mimalloc-LICENSE.txt', 'openssl-LICENSE.txt', 'reactiveobjc-LICENSE.md'];
const PATHSPEC_LICENSE = 'backend/pathspec-1.1.1.dist-info/licenses/LICENSE';
const BACKEND_ASSET_SUFFIXES = new Set(['.py', '.html', '.js', '.css']);
// Standalone MCP servers: nothing in the app starts them and their dependencies are not bundled.
const BACKEND_DROPS = new Set(['mcp_server.py', 'room_mcp.py']);
const LIBRARY_DROPS = new Set(['idlelib', 'ensurepip', 'tkinter', 'turtledemo', 'test', 'site-packages',
  `config-${PYTHON_MINOR}-darwin`]);
// What a shared library may link against and still work on another Mac.
const PORTABLE_LINK = /^(@(rpath|loader_path|executable_path)\/|\/usr\/lib\/|\/System\/Library\/)/;

// Relative POSIX path inside a standalone CPython tree -> copy it or not.
function keepPythonPath(relative) {
  if (relative === 'lib' || relative === 'bin') return true;
  const parts = relative.split('/');
  if (parts[0] === 'bin') return relative === `bin/python${PYTHON_MINOR}`;
  if (parts[0] !== 'lib') return false;
  if (parts.length === 2) return parts[1] === `python${PYTHON_MINOR}` || /^libpython.*\.dylib$/.test(parts[1]);
  if (parts.includes('__pycache__')) return false;
  if (LIBRARY_DROPS.has(parts[2])) return false;
  return !(parts[2] === 'lib-dynload' && /^_(tkinter|ctypes_test)\./.test(parts[3] ?? ''));
}

function keepBackendPath(relative) {
  if (relative.split('/').includes('__pycache__') || BACKEND_DROPS.has(relative)) return false;
  const extension = path.posix.extname(relative);
  return extension === '' || BACKEND_ASSET_SUFFIXES.has(extension);
}

function nonPortableLinks(otoolOutput) {
  return otoolOutput.split('\n').slice(1).map((line) => line.trim().split(' ')[0])
    .filter((link) => link && !PORTABLE_LINK.test(link));
}

function releasePackagerOptions({ stage, build, root, metadata, python, backend, electronVersion }) {
  const local = localPackagerOptions({ stage, build, root, metadata, electronVersion });
  return { ...local, appBundleId: RELEASE_BUNDLE_ID, extraResource: [...local.extraResource, python, backend] };
}

function run(command, args, options = {}) {
  return execFileSync(command, args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'], ...options });
}

async function copyTree(from, to, keep) {
  await fs.cp(from, to, {
    recursive: true, verbatimSymlinks: true,
    filter: (source) => source === from || keep(path.relative(from, source).split(path.sep).join('/')),
  });
}

async function stagePython(pythonHome, target) {
  await copyTree(pythonHome, target, keepPythonPath);
  // One real executable: no symlink chain for packaging or signing to mangle.
  await fs.rename(path.join(target, 'bin', `python${PYTHON_MINOR}`), path.join(target, 'bin', 'python3'));
  // uv records the build machine's absolute path as the library's own name.
  const library = path.join(target, 'lib', `libpython${PYTHON_MINOR}.dylib`);
  run('install_name_tool', ['-id', `@rpath/libpython${PYTHON_MINOR}.dylib`, library]);
  run('codesign', ['--force', '--sign', '-', library]);
  const sysconfig = path.join(target, 'lib', `python${PYTHON_MINOR}`, `_sysconfigdata__darwin_darwin.py`);
  await fs.writeFile(sysconfig, (await fs.readFile(sysconfig, 'utf8')).replaceAll(pythonHome, '/beanest-python'));
  return path.join(target, 'bin', 'python3');
}

async function stageBackend(root, python, target) {
  await copyTree(path.join(root, 'src', BACKEND_PACKAGE), path.join(target, BACKEND_PACKAGE), keepBackendPath);
  const dependencies = JSON.parse(run(python, ['-I', '-c',
    'import json,sys,tomllib;print(json.dumps(tomllib.load(open(sys.argv[1],"rb"))["project"]["dependencies"]))',
    path.join(root, 'pyproject.toml')]));
  run('uv', ['pip', 'install', '--quiet', '--no-deps', '--python', python, '--target', target, ...dependencies]);
  await pruneBackendStage(target);
}

// uv leaves console scripts and its own lock file in the install target.
async function pruneBackendStage(target) {
  for (const name of ['bin', '.lock']) await fs.rm(path.join(target, name), { recursive: true, force: true });
}

// target: a directory, walked recursively, or a single file such as app.asar.
async function filesLeakingText(target, text) {
  const needle = Buffer.from(text);
  const files = (await fs.stat(target)).isFile() ? [target]
    : (await fs.readdir(target, { recursive: true, withFileTypes: true })).filter((item) => item.isFile())
      .map((entry) => path.join(entry.parentPath, entry.name));
  const leaks = [];
  for (const file of files) {
    if ((await fs.readFile(file)).includes(needle)) leaks.push(file);
  }
  return leaks;
}

function requiredNoticePaths() {
  return [...ELECTRON_NOTICES.map(([, to]) => to), 'LICENSE', 'THIRD_PARTY_NOTICES.md',
    `python/lib/python${PYTHON_MINOR}/LICENSE.txt`, PATHSPEC_LICENSE, ...RELEASE_NOTICES.map((name) => `licenses/${name}`)];
}

// An empty file counts as missing: a failed download must not pass as a notice.
async function missingNotices(resources) {
  const missing = [];
  for (const relative of requiredNoticePaths()) {
    const stat = await fs.stat(path.join(resources, relative)).catch(() => null);
    if (!stat?.isFile() || stat.size === 0) missing.push(relative);
  }
  return missing;
}

// Packager puts Electron's notices beside the app, where a zip of the app would lose them.
async function copyNotices(outputPath, resources) {
  for (const [from, to] of ELECTRON_NOTICES) await fs.copyFile(path.join(outputPath, from), path.join(resources, to));
  await fs.mkdir(path.join(resources, 'licenses'), { recursive: true });
  for (const name of RELEASE_NOTICES) await fs.copyFile(path.join(NOTICES_SOURCE, name), path.join(resources, 'licenses', name));
}

// Extended attributes would otherwise be sealed into the signature or end up in the zip.
function prepareBundleForSigning(appPath) {
  return fs.rm(path.join(appPath, 'Contents', 'Resources', 'electron.icns'), { force: true })
    .then(() => run('xattr', ['-cr', appPath]));
}

// Resource forks and extended attributes would otherwise appear as __MACOSX entries.
function archiveApp(appPath, archive) {
  run('ditto', ['-c', '-k', '--norsrc', '--noextattr', '--keepParent', appPath, archive]);
}

async function machOFiles(directory) {
  const entries = await fs.readdir(directory, { recursive: true, withFileTypes: true });
  return entries.filter((entry) => entry.isFile()
    && (/\.(so|dylib)$/.test(entry.name) || entry.name === 'python3'))
    .map((entry) => path.join(entry.parentPath, entry.name));
}

// Runs the packaged interpreter with an empty environment from an unrelated
// directory, the way it will start on a machine with nothing else installed.
async function verifyBundledRuntime(appPath) {
  const resources = path.join(appPath, 'Contents', 'Resources');
  const python = path.join(resources, 'python');
  for (const file of await machOFiles(python)) {
    const links = nonPortableLinks(run('otool', ['-L', file]));
    if (links.length) throw new Error(`release_bundle_external_link:${file}:${links.join(',')}`);
  }
  const missing = await missingNotices(resources);
  if (missing.length) throw new Error(`release_bundle_missing_notice:${missing.join(',')}`);
  for (const target of [python, path.join(resources, 'backend'), path.join(resources, 'app.asar')]) {
    const leaks = await filesLeakingText(target, os.homedir());
    if (leaks.length) throw new Error(`release_bundle_build_path:${leaks.join(',')}`);
  }
  const check = 'import sys;sys.path.insert(0,sys.argv[1]);import asyncio,fcntl,sqlite3,ssl,pathspec;'
    + `import ${BACKEND_PACKAGE}.room_server as s;print(s.__file__)`;
  const loaded = run(path.join(python, 'bin', 'python3'),
    ['-I', '-B', '-c', check, path.join(resources, 'backend')], { cwd: '/', env: {} }).trim();
  if (!loaded.startsWith(path.join(resources, 'backend') + path.sep)) throw new Error('release_bundle_backend_mismatch');
}

async function main() {
  if (process.platform !== 'darwin') throw new Error('The release bundle currently supports macOS.');
  const { packager } = await import('@electron/packager');
  const root = path.resolve(__dirname, '..');
  const build = process.env.BEANEST_RELEASE_DIR
    ? path.resolve(process.env.BEANEST_RELEASE_DIR) : path.join(root, 'runtime', 'desktop-release');
  await fs.rm(build, { recursive: true, force: true });
  const stage = path.join(build, 'source');
  const config = await stageShell(stage);

  run('uv', ['python', 'install', '--quiet', PYTHON_VERSION]);
  const managed = run('uv', ['python', 'find', '--managed-python', PYTHON_VERSION]).trim();
  const pythonHome = path.dirname(path.dirname(await fs.realpath(managed)));
  const pythonStage = path.join(build, 'python');
  const backendStage = path.join(build, 'backend');
  const python = await stagePython(pythonHome, pythonStage);
  await stageBackend(root, python, backendStage);
  // unchecked-hash: copying and signing change mtimes, and the bundle is read-only.
  // -s/-p: tracebacks carry a neutral prefix rather than this machine's build path.
  for (const [tree, prefix] of [[pythonStage, '/beanest-python'], [backendStage, '/beanest-backend']]) {
    run(python, ['-I', '-m', 'compileall', '-q', '-f', '-j', '0', '--invalidation-mode', 'unchecked-hash',
      '-s', tree, '-p', prefix, tree]);
  }

  const metadata = path.join(build, 'coordinator.json');
  await fs.writeFile(metadata, JSON.stringify({ bundled: true }), { mode: 0o644 });
  const [outputPath] = await packager(releasePackagerOptions({ stage, build, root, metadata,
    python: pythonStage, backend: backendStage, electronVersion: config.devDependencies.electron }));
  await verifyLocalBundle(outputPath, RELEASE_BUNDLE_ID);
  const appPath = path.join(outputPath, `${APP_NAME}.app`);
  await copyNotices(outputPath, path.join(appPath, 'Contents', 'Resources'));
  await prepareBundleForSigning(appPath);
  // Packager leaves a partial signature, which macOS reports as a damaged app
  // after download. Seal the whole bundle; a Developer ID replaces this later.
  run('codesign', ['--force', '--deep', '--sign', '-', appPath]);
  run('codesign', ['--verify', '--deep', '--strict', appPath]);
  await verifyBundledRuntime(appPath);
  const archive = path.join(build, `Beanest-${config.version}-macos-${process.arch}.zip`);
  archiveApp(appPath, archive);
  console.log(appPath);
  console.log(archive);
}

if (require.main === module) main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});

module.exports = {
  archiveApp, copyNotices, filesLeakingText, keepPythonPath, keepBackendPath, missingNotices, nonPortableLinks,
  prepareBundleForSigning, pruneBackendStage, releasePackagerOptions, requiredNoticePaths, verifyBundledRuntime,
  NOTICES_SOURCE, RELEASE_NOTICES, RELEASE_BUNDLE_ID, PYTHON_VERSION, PYTHON_MINOR,
};
