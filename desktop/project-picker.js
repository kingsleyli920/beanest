'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const { strings } = require('./locale');

const PROJECT_PICKER_CHANNEL = 'beanest:choose-project-files';
const PROJECT_FOLDER_CHANNEL = 'beanest:choose-project-folder';
const ERRORS = new Set([
  'project_selection_unavailable', 'project_selection_outside_directory',
  'project_selection_unsafe_path', 'invalid_project_selection',
]);

function selectionError(code) {
  return new Error(code);
}

function trustedProjectSender(event, window, serverUrl) {
  try {
    if (!window || window.isDestroyed() || window.webContents.isDestroyed()
        || event?.sender !== window.webContents || event?.senderFrame !== window.webContents.mainFrame) return false;
    const expected = new URL(serverUrl);
    const actual = new URL(event.senderFrame.url);
    return expected.protocol === 'http:' && expected.hostname === '127.0.0.1'
      && actual.origin === expected.origin && !actual.username && !actual.password;
  } catch {
    return false;
  }
}

function absoluteSelection(value) {
  if (typeof value !== 'string' || !path.isAbsolute(value) || value.includes('\0')
      || path.resolve(value) !== value) throw selectionError('invalid_project_selection');
  return value;
}

function relativeSelection(directory, filename) {
  absoluteSelection(filename);
  const relative = path.relative(directory, filename);
  if (!relative || relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw selectionError('project_selection_outside_directory');
  }
  const parts = relative.split(path.sep);
  if (relative.length > 240 || parts.length > 8 || relative.includes('\\')
      || parts.some((part) => !/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$/.test(part) || part.includes('..'))) {
    throw selectionError('project_selection_unsafe_path');
  }
  return parts.join('/');
}

async function chooseProjectFolder(dialog, window, stillTrusted, locale = 'zh-CN') {
  const text = strings(locale);
  const authorize = () => {
    if (!stillTrusted()) throw selectionError('project_selection_unavailable');
  };
  authorize();
  const folder = await dialog.showOpenDialog(window, {
    title: text.folderTitle, buttonLabel: text.folderButton,
    message: text.folderDetail,
    properties: ['openDirectory', 'noResolveAliases'],
  });
  authorize();
  if (folder?.canceled === true) return { cancelled: true };
  if (!Array.isArray(folder?.filePaths) || folder.filePaths.length !== 1) throw selectionError('invalid_project_selection');
  const directory = absoluteSelection(folder.filePaths[0]);
  if (directory === path.parse(directory).root) throw selectionError('project_selection_unsafe_path');
  const directoryStat = await fs.lstat(directory);
  authorize();
  if (!directoryStat.isDirectory()) throw selectionError('project_selection_unsafe_path');
  const canonicalDirectory = await fs.realpath(directory);
  authorize();
  if (canonicalDirectory !== directory) {
    throw selectionError('project_selection_unsafe_path');
  }
  return { cancelled: false, path: directory, name: path.basename(directory) };
}

async function chooseProjectFiles(dialog, window, stillTrusted, locale = 'zh-CN') {
  const text = strings(locale);
  const folder = await chooseProjectFolder(dialog, window, stillTrusted, locale);
  if (folder.cancelled) return folder;
  const directory = folder.path;
  const authorize = () => {
    if (!stillTrusted()) throw selectionError('project_selection_unavailable');
  };
  authorize();
  const selected = await dialog.showOpenDialog(window, {
    title: text.filesTitle, buttonLabel: text.filesButton, defaultPath: directory,
    message: text.filesDetail,
    properties: ['openFile', 'multiSelections', 'noResolveAliases'],
  });
  authorize();
  if (selected?.canceled === true) return { cancelled: true };
  if (!Array.isArray(selected?.filePaths) || selected.filePaths.length < 1 || selected.filePaths.length > 12) {
    throw selectionError('invalid_project_selection');
  }
  const files = selected.filePaths.map((filename) => relativeSelection(directory, filename));
  if (new Set(files.map((filename) => filename.toLowerCase())).size !== files.length) {
    throw selectionError('invalid_project_selection');
  }
  for (const filename of selected.filePaths) {
    const metadata = await fs.lstat(filename);
    authorize();
    if (!metadata.isFile() || metadata.nlink !== 1) throw selectionError('project_selection_unsafe_path');
    const canonicalFile = await fs.realpath(filename);
    authorize();
    if (canonicalFile !== filename) {
      throw selectionError('project_selection_unsafe_path');
    }
    authorize();
  }
  return { cancelled: false, directory, files, suggestedName: path.basename(directory) };
}

function installProjectPicker(ipcMain, dialog, currentWindow, currentUrl, currentLocale = () => 'zh-CN') {
  let choosing = false;
  for (const [channel, chooser] of [[PROJECT_PICKER_CHANNEL, chooseProjectFiles], [PROJECT_FOLDER_CHANNEL, chooseProjectFolder]]) {
    ipcMain.handle(channel, async (event, ...args) => {
      const window = currentWindow();
      const authorized = () => trustedProjectSender(event, window, currentUrl()) && currentWindow() === window;
      if (args.length || choosing || !authorized()) return { ok: false, error: 'project_selection_unavailable' };
      choosing = true;
      try {
        return { ok: true, value: await chooser(dialog, window, authorized, currentLocale()) };
      } catch (error) {
        return { ok: false, error: ERRORS.has(error?.message) ? error.message : 'project_selection_unavailable' };
      } finally {
        choosing = false;
      }
    });
  }
}

module.exports = { PROJECT_PICKER_CHANNEL, PROJECT_FOLDER_CHANNEL, trustedProjectSender, chooseProjectFolder, chooseProjectFiles, installProjectPicker };
