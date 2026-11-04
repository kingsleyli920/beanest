'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { createRequire } = require('node:module');
const { localPackagerOptions, verifyLocalBundle, APP_NAME, BUNDLE_ID } = require('./build-local');

const packagerEntry = require.resolve('@electron/packager');
const plist = createRequire(packagerEntry)('plist');

async function createElectronTemplate(root) {
  const contents = path.join(root, 'Electron.app', 'Contents');
  const integrity = { 'Resources/app.asar': { algorithm: 'SHA256', hash: 'a'.repeat(64) } };
  await fs.mkdir(path.join(contents, 'Resources'), { recursive: true });
  await fs.mkdir(path.join(contents, 'MacOS'));
  await fs.writeFile(path.join(contents, 'MacOS', 'Electron'), 'not a real executable', { mode: 0o755 });
  await fs.writeFile(path.join(contents, 'Info.plist'), plist.build({
    CFBundleName: 'Electron', CFBundleDisplayName: 'Electron', CFBundleExecutable: 'Electron',
    CFBundleIdentifier: 'com.github.Electron', NSHighResolutionCapable: true,
  }));
  for (const suffix of ['', ' (Renderer)', ' (GPU)', ' (Plugin)']) {
    const helperName = `Electron Helper${suffix}`;
    const helperContents = path.join(contents, 'Frameworks', `${helperName}.app`, 'Contents');
    await fs.mkdir(path.join(helperContents, 'MacOS'), { recursive: true });
    await fs.writeFile(path.join(helperContents, 'MacOS', helperName), 'not a real executable', { mode: 0o755 });
    await fs.writeFile(path.join(helperContents, 'Info.plist'), plist.build({
      CFBundleName: helperName, CFBundleDisplayName: helperName, CFBundleExecutable: helperName,
      CFBundleIdentifier: 'com.github.Electron.helper',
    }));
  }
  return integrity;
}

async function packagedFixture(t) {
  const root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'beanest-bundle-')));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const buildPath = path.join(root, 'staged-output');
  const integrity = await createElectronTemplate(buildPath);
  const metadataPath = path.join(root, 'coordinator.json');
  await fs.mkdir(path.join(root, 'desktop', 'assets'), { recursive: true });
  await Promise.all([
    fs.writeFile(metadataPath, '{}'), fs.writeFile(path.join(root, 'LICENSE'), 'fixture license'),
    fs.writeFile(path.join(root, 'THIRD_PARTY_NOTICES.md'), 'fixture notice'),
    fs.copyFile(path.join(__dirname, 'assets', 'beanest.icns'),
      path.join(root, 'desktop', 'assets', 'beanest.icns')),
  ]);
  const options = localPackagerOptions({ stage: root, build: root, root,
    metadata: metadataPath, electronVersion: '44.9.3' });
  const { MacApp } = await import(pathToFileURL(path.join(path.dirname(packagerEntry), 'mac.js')).href);
  const app = new MacApp(options, 'unused-electron-template');
  Object.defineProperty(app, 'stagingPath', { value: buildPath });
  // Use the locked Packager's real plist generation, main/helper binary and
  // bundle renaming, and resource copy. Extraction/signing are omitted because
  // these are metadata fixtures, never runnable Electron binaries.
  app.initialize = async () => {};
  app.asarIntegrity = integrity;
  app.applyIntegrityDigest = async () => {};
  app.signAppIfSpecified = async () => {};
  app.notarizeAppIfSpecified = async () => {};
  app.move = async () => buildPath;
  await app.create();
  return { buildPath, contents: path.join(buildPath, `${APP_NAME}.app`, 'Contents'), integrity };
}

test('installed Packager creates matching branded main and Helper lookup paths without plist patches', async (t) => {
  const { buildPath, integrity } = await packagedFixture(t);
  const metadata = await verifyLocalBundle(buildPath);
  assert.equal(metadata.CFBundleName, APP_NAME);
  assert.equal(metadata.CFBundleDisplayName, APP_NAME);
  assert.equal(metadata.CFBundleExecutable, APP_NAME);
  assert.equal(metadata.CFBundleIdentifier, BUNDLE_ID);
  assert.deepEqual(metadata.ElectronAsarIntegrity, integrity);
  assert.equal(metadata.NSHighResolutionCapable, true);
  assert.equal(metadata.CFBundleName, '豆窝');
  assert.equal(metadata.CFBundleIconFile, 'beanest.icns');
});

test('output verification rejects a generic replacement or missing branded icon', async (t) => {
  const { buildPath, contents } = await packagedFixture(t);
  const icon = path.join(contents, 'Resources', 'beanest.icns');
  await fs.writeFile(icon, 'generic replacement');
  await assert.rejects(verifyLocalBundle(buildPath), { message: 'local_bundle_icon_mismatch' });
  await fs.rm(icon);
  await assert.rejects(verifyLocalBundle(buildPath), { message: 'local_bundle_icon_missing' });
});

test('output verification rejects the observed missing Helper layout and executable identity drift', async (t) => {
  const { buildPath, contents } = await packagedFixture(t);
  const helper = path.join(contents, 'Frameworks', `${APP_NAME} Helper.app`);
  const misplaced = path.join(contents, 'Frameworks', 'Local Agent Coordinator Helper.app');
  await fs.rename(helper, misplaced);
  await assert.rejects(verifyLocalBundle(buildPath), { message: 'local_bundle_helper_missing' });
  await fs.rename(misplaced, helper);
  const filename = path.join(contents, 'Info.plist');
  const metadata = plist.parse(await fs.readFile(filename, 'utf8'));
  await fs.writeFile(filename, plist.build({ ...metadata, CFBundleExecutable: 'Different' }));
  await assert.rejects(verifyLocalBundle(buildPath), { message: 'local_bundle_identity_changed' });
});
