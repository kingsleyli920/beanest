'use strict';

// A local development bundle has its own macOS identity. Only the shell and an
// installation pointer go into it; private runtime data and project files do not.
const fs = require('node:fs/promises');
const path = require('node:path');
const { createRequire } = require('node:module');
const { APP_NAME } = require('./menu');
const { strings, systemLocale } = require('./locale');

const BUNDLE_ID = 'local.agent.coordinator';
const ICON_NAME = 'beanest.icns';
const SOURCE_ICON = path.join(__dirname, 'assets', ICON_NAME);

function plistParser() {
  // Reuse the parser already required and locked by Electron Packager. It is
  // build-only; no extra dependency or parser enters the desktop runtime.
  return createRequire(require.resolve('@electron/packager'))('plist');
}

async function verifyLocalBundle(outputPath) {
  const plist = plistParser();
  const contents = path.join(outputPath, `${APP_NAME}.app`, 'Contents');
  const metadata = plist.parse(await fs.readFile(path.join(contents, 'Info.plist'), 'utf8'));
  if (metadata.CFBundleExecutable !== APP_NAME || metadata.CFBundleIdentifier !== BUNDLE_ID) {
    throw new Error('local_bundle_identity_changed');
  }
  if (metadata.CFBundleName !== APP_NAME || metadata.CFBundleDisplayName !== APP_NAME) {
    throw new Error('local_bundle_branding_missing');
  }
  if (metadata.CFBundleIconFile !== ICON_NAME) throw new Error('local_bundle_icon_missing');
  let bundledIcon;
  try {
    bundledIcon = await fs.readFile(path.join(contents, 'Resources', ICON_NAME));
  } catch {
    throw new Error('local_bundle_icon_missing');
  }
  const sourceIcon = await fs.readFile(SOURCE_ICON);
  if (sourceIcon.subarray(0, 4).toString('ascii') !== 'icns'
      || !bundledIcon.equals(sourceIcon)) throw new Error('local_bundle_icon_mismatch');
  const executable = await fs.lstat(path.join(contents, 'MacOS', metadata.CFBundleExecutable));
  if (!executable.isFile() || !(executable.mode & 0o111)) throw new Error('local_bundle_executable_missing');
  // Electron 44 resolves child processes from CFBundleName, not the visible
  // menu label. Check the exact default/Renderer/GPU/Plugin helper lookup paths.
  for (const suffix of ['', ' (Renderer)', ' (GPU)', ' (Plugin)']) {
    const helperName = `${metadata.CFBundleName} Helper${suffix}`;
    const helperContents = path.join(contents, 'Frameworks', `${helperName}.app`, 'Contents');
    let helperMetadata;
    let helperExecutable;
    try {
      helperMetadata = plist.parse(await fs.readFile(path.join(helperContents, 'Info.plist'), 'utf8'));
      helperExecutable = await fs.lstat(path.join(helperContents, 'MacOS', helperName));
    } catch {
      throw new Error('local_bundle_helper_missing');
    }
    if (helperMetadata.CFBundleExecutable !== helperName
        || helperMetadata.CFBundleIdentifier !== `${BUNDLE_ID}.helper`
        || !helperExecutable.isFile() || !(helperExecutable.mode & 0o111)) {
      throw new Error('local_bundle_helper_mismatch');
    }
  }
  return metadata;
}

function localPackagerOptions({ stage, build, root, metadata, electronVersion }) {
  return {
    // Let Packager name the app and every Helper together. Electron derives
    // Helper paths from CFBundleName, so post-editing that field breaks startup.
    dir: stage, name: APP_NAME, executableName: APP_NAME,
    out: path.join(build, 'app'), platform: 'darwin', arch: process.arch,
    electronVersion, appBundleId: BUNDLE_ID, asar: true,
    icon: path.join(root, 'desktop', 'assets', ICON_NAME),
    extendInfo: { CFBundleIconFile: ICON_NAME },
    extraResource: [metadata, path.join(root, 'LICENSE'), path.join(root, 'THIRD_PARTY_NOTICES.md')],
    overwrite: true, quiet: true,
  };
}

async function main() {
  if (process.platform !== 'darwin') throw new Error('This local desktop bundle currently supports macOS.');
  const { packager } = await import('@electron/packager');
  const root = path.resolve(__dirname, '..');
  // An explicit output directory permits inspecting a new shell without
  // replacing the bundle that is currently supervising native tasks.
  const build = process.env.BEANEST_BUILD_DIR
    ? path.resolve(process.env.BEANEST_BUILD_DIR) : path.join(root, 'runtime', 'desktop-build');
  const stage = path.join(build, 'source');
  const config = require('./package.json');
  await fs.mkdir(stage, { recursive: true, mode: 0o700 });
  for (const name of ['main.js', 'lifecycle.js', 'menu.js', 'locale.js', 'project-picker.js', 'power-guard.js', 'chat-location.js', 'external-pages.js', 'preload.js', 'shell-path.js']) {
    await fs.copyFile(path.join(__dirname, name), path.join(stage, name));
  }
  await fs.writeFile(path.join(stage, 'package.json'), JSON.stringify({
    name: 'local-agent-coordinator-desktop', productName: APP_NAME,
    version: config.version, main: 'main.js', private: true, license: 'MIT',
  }));
  const metadata = path.join(build, 'coordinator.json');
  await fs.writeFile(metadata, JSON.stringify({ projectRoot: root }), { mode: 0o600 });
  const outputs = await packager(localPackagerOptions({ stage, build, root, metadata,
    electronVersion: config.devDependencies.electron }));
  for (const outputPath of outputs) await verifyLocalBundle(outputPath);
  await fs.writeFile(path.join(build, 'built-stamp'), '');
  console.log(path.join(outputs[0], `${APP_NAME}.app`));
}

if (require.main === module) main().catch(() => {
  console.error(strings(systemLocale(process.env.LC_ALL || process.env.LANG || '')).buildFailed);
  process.exitCode = 1;
});

module.exports = { verifyLocalBundle, localPackagerOptions, APP_NAME, BUNDLE_ID };
