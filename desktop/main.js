'use strict';

const { app, BrowserWindow, dialog, Menu, ipcMain, powerSaveBlocker, shell } = require('electron');
const { spawn } = require('node:child_process');
const { mkdirSync, chmodSync, readFileSync, existsSync } = require('node:fs');
const path = require('node:path');
const { parseServerUrl, stopOwnedChild } = require('./lifecycle');
const { APP_NAME, DISPLAY_NAME, menuTemplate } = require('./menu');
const { installProjectPicker } = require('./project-picker');
const { createPowerGuard, readOwnedActivity } = require('./power-guard');
const { createChatLocation } = require('./chat-location');
const { installExternalPages } = require('./external-pages');
const { strings, loadLocale, saveLocale, installLocale } = require('./locale');

let serverProcess;
let quitting = false;
let serverUrl;
let mainWindow;
let powerGuard;
let currentLocale = 'zh-CN';

// Pin existing Chromium storage and the macOS single-instance lock before
// changing the product name. No records are moved or copied to a second profile.
const legacyDataPath = path.join(app.getPath('appData'), 'Local Agent Coordinator');
mkdirSync(legacyDataPath, { recursive: true, mode: 0o700 });
app.setPath('userData', legacyDataPath);
app.setPath('sessionData', legacyDataPath);
app.setName(APP_NAME);
app.commandLine.appendSwitch('force-renderer-accessibility');
if (!app.requestSingleInstanceLock()) app.exit(0);
app.on('second-instance', () => {
  if (mainWindow) presentWindow(mainWindow);
});

function presentWindow(window) {
  if (window.isMinimized()) window.restore();
  window.show();
  // A repeated launch refreshes installation discovery without reloading the
  // renderer, losing its draft, or starting any model work.
  window.webContents.send('beanest:opened');
  // Only explicit launches call this. Background task updates never activate
  // the application or take keyboard focus from the user's other work.
  if (process.platform === 'darwin' && !app.isActive()) app.focus({ steal: true });
  window.focus();
}

app.on('activate', () => {
  if (mainWindow && !mainWindow.isDestroyed()) presentWindow(mainWindow);
});

function projectRoot() {
  if (app.isPackaged) {
    const metadata = JSON.parse(readFileSync(path.join(process.resourcesPath, 'coordinator.json'), 'utf8'));
    if (typeof metadata.projectRoot !== 'string' || !path.isAbsolute(metadata.projectRoot)) {
      throw new Error('Invalid local installation metadata');
    }
    return metadata.projectRoot;
  }
  return path.resolve(__dirname, '..');
}

function runtimeDirectory() {
  const dir = path.join(projectRoot(), 'runtime', 'desktop-rooms');
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  chmodSync(dir, 0o700);
  return dir;
}

function pythonCommand(root) {
  const venvPython = path.join(root, '.venv', 'bin', 'python');
  return require('node:fs').existsSync(venvPython) ? venvPython : 'python3';
}

function startRoomServer() {
  const root = projectRoot();
  const runtime = runtimeDirectory();
  const env = { ...process.env, PYTHONPATH: path.join(root, 'src') };
  serverProcess = spawn(pythonCommand(root), [
    '-m', 'local_agent_coordinator.room_server', '--runtime', runtime, '--port', '0',
  ], { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });

  return new Promise((resolve, reject) => {
    let output = '';
    let settled = false;
    const timeout = setTimeout(() => fail(new Error('Room server did not announce its URL in time')), 15000);
    const fail = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      reject(error);
    };
    serverProcess.once('error', fail);
    serverProcess.once('exit', (code) => {
      if (!settled) fail(new Error(`Room server exited before startup (${code ?? 'signal'})`));
    });
    serverProcess.stdout.setEncoding('utf8');
    serverProcess.stdout.on('data', (chunk) => {
      if (settled) return;
      output += chunk;
      const newline = output.indexOf('\n');
      if (newline < 0) {
        if (output.length > 512) fail(new Error('Room server startup line is too long'));
        return;
      }
      try {
        serverUrl = parseServerUrl(output.slice(0, newline));
        settled = true;
        clearTimeout(timeout);
        resolve(serverUrl);
      } catch (error) {
        fail(error);
      }
    });
    // Deliberately do not relay stderr; the server owns private runtime details.
    serverProcess.stderr.on('data', () => {});
  });
}

function isOwnOrigin(candidate) {
  try {
    return new URL(candidate).origin === new URL(serverUrl).origin;
  } catch {
    return false;
  }
}

async function readAutonomousStatus() {
  // The published MCP credential is scoped to this owned service instance. It
  // stays in the main process and is never exposed through the renderer bridge.
  const connection = JSON.parse(readFileSync(path.join(runtimeDirectory(), 'connection.json'), 'utf8'));
  return readOwnedActivity(serverUrl, connection);
}

async function openWindow(url) {
  const window = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 760,
    minHeight: 560,
    title: strings(currentLocale).displayName,
    show: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      preload: path.join(__dirname, 'preload.js'),
      additionalArguments: [`--beanest-locale=${currentLocale}`],
    },
  });
  mainWindow = window;
  window.on('closed', () => { mainWindow = null; });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event, target) => {
    if (!isOwnOrigin(target)) event.preventDefault();
  });
  const chatLocation = createChatLocation(path.join(legacyDataPath, 'last-chat.json'));
  window.webContents.on('did-navigate-in-page', (_event, target, isMainFrame) => {
    if (isMainFrame) chatLocation.remember(target, url);
  });
  await window.loadURL(chatLocation.initialUrl(url));
  // Explicit launches should present the workspace; retain the window for its
  // full lifetime rather than relying on a function-local wrapper staying alive.
  presentWindow(window);
}

function applyLocale(locale) {
  const text = strings(locale);
  currentLocale = locale;
  app.setAboutPanelOptions({ applicationName: text.displayName, applicationVersion: app.getVersion(), credits: text.credits });
  Menu.setApplicationMenu(Menu.buildFromTemplate(menuTemplate(process.platform, () => {
    const copy = strings(currentLocale);
    const options = { type: 'info', title: copy.displayName, message: copy.helpTitle,
      detail: copy.helpDetail, buttons: [copy.ok] };
    void (mainWindow && !mainWindow.isDestroyed()
      ? dialog.showMessageBox(mainWindow, options) : dialog.showMessageBox(options));
  }, locale)));
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.setTitle(text.displayName);
}

app.whenReady().then(async () => {
  try {
    const preference = path.join(legacyDataPath, 'locale.json');
    const legacyProfile = existsSync(path.join(legacyDataPath, 'Local Storage')) || existsSync(path.join(legacyDataPath, 'last-chat.json'));
    applyLocale(loadLocale(preference, app.getLocale(), legacyProfile));
    installLocale(ipcMain, () => mainWindow, () => serverUrl, (locale) => {
      saveLocale(preference, locale);
      applyLocale(locale);
    });
    installProjectPicker(ipcMain, dialog, () => mainWindow, () => serverUrl, () => currentLocale);
    installExternalPages(ipcMain, shell, () => mainWindow, () => serverUrl);
    await openWindow(await startRoomServer());
    powerGuard = createPowerGuard({ powerSaveBlocker, readStatus: readAutonomousStatus });
    powerGuard.start();
  } catch {
    const cleanup = await stopOwnedChild(serverProcess);
    const detail = cleanup.forced || !cleanup.exited
      ? strings(currentLocale).cleanupUnknown
      : strings(currentLocale).startupRetry;
    await dialog.showMessageBox({ type: 'error', title: strings(currentLocale).displayName,
      message: strings(currentLocale).startupFailed, detail, buttons: [strings(currentLocale).ok] });
    quitting = true;
    app.quit();
  }
});

app.on('before-quit', (event) => {
  if (quitting) return;
  event.preventDefault();
  quitting = true;
  powerGuard?.dispose();
  void stopOwnedChild(serverProcess).then(async (cleanup) => {
    if (cleanup.forced || !cleanup.exited || serverProcess?.exitCode !== 0) {
      await dialog.showMessageBox({
        type: 'warning',
        title: strings(currentLocale).displayName,
        message: strings(currentLocale).stopUnknown,
        detail: strings(currentLocale).stopDetail,
        buttons: [strings(currentLocale).ok],
      });
    }
  }).finally(() => app.quit());
});

app.on('window-all-closed', () => app.quit());
