'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');

const LOCALES = new Set(['zh-CN', 'en']);
const LOCALE_CHANNEL = 'beanest:set-locale';
const COPY = {
  'zh-CN': {
    name: '豆窝', displayName: '豆窝 · Beanest', about: '关于豆窝', services: '服务', hide: '隐藏豆窝', hideOthers: '隐藏其他应用',
    showAll: '显示全部', quit: '退出豆窝', file: '文件', close: '关闭窗口', edit: '编辑', undo: '撤销', redo: '重做',
    cut: '剪切', copy: '复制', paste: '粘贴', pasteStyle: '粘贴并匹配样式', selectAll: '全选', view: '显示', reload: '重新载入页面',
    actualSize: '实际大小', zoomIn: '放大', zoomOut: '缩小', fullscreen: '切换全屏', window: '窗口', minimize: '最小化',
    zoomWindow: '缩放窗口', front: '前置全部窗口', help: '帮助', howTo: '如何使用豆窝', ok: '知道了',
    credits: '本机 AI 伙伴协作工作台', helpTitle: '和小伙伴一起完成目标',
    helpDetail: '先选择本次参与的伙伴，再输入目标。伙伴会在同一对话里交流。\n\n在“工具与状态”中查看接入和最近用量。使用已有项目时，选择项目文件夹并点击“添加项目”；发送任务后伙伴会按需查找文件，修改保存在隔离副本。',
    startupFailed: '豆窝暂时无法启动', startupRetry: '请通过项目里的 scripts/start-coordinator.command 重新启动。如果仍然失败，请检查项目 Python 环境是否已安装。',
    cleanupUnknown: '后台服务的停止状态尚未确认。请检查豆窝的活动进程，确认结束后再试。',
    stopUnknown: '豆窝的后台任务尚未确认停止', stopDetail: '请检查豆窝的活动进程。关闭窗口不代表后台任务已经结束。',
    folderTitle: '选择项目文件夹', folderButton: '选择此文件夹', folderDetail: '选择完整项目；伙伴会按任务需要查找文件，修改保存在隔离副本。',
    filesTitle: '选择本次项目文件', filesButton: '使用所选文件', filesDetail: '请选择此文件夹里的 1–12 个具体文件。登记时会检查文本格式，选择后还需点击“添加项目”确认。',
    buildFailed: '豆窝桌面程序构建失败。请检查 Node.js 和 Electron 是否已正确安装。',
  },
  en: {
    name: 'Beanest', displayName: 'Beanest', about: 'About Beanest', services: 'Services', hide: 'Hide Beanest', hideOthers: 'Hide Others',
    showAll: 'Show All', quit: 'Quit Beanest', file: 'File', close: 'Close Window', edit: 'Edit', undo: 'Undo', redo: 'Redo',
    cut: 'Cut', copy: 'Copy', paste: 'Paste', pasteStyle: 'Paste and Match Style', selectAll: 'Select All', view: 'View', reload: 'Reload Page',
    actualSize: 'Actual Size', zoomIn: 'Zoom In', zoomOut: 'Zoom Out', fullscreen: 'Toggle Full Screen', window: 'Window', minimize: 'Minimize',
    zoomWindow: 'Zoom Window', front: 'Bring All to Front', help: 'Help', howTo: 'How to Use Beanest', ok: 'OK',
    credits: 'A local workspace for collaborating with AI partners', helpTitle: 'Work toward a goal with your partners',
    helpDetail: 'Choose the partners for this task, then enter a goal. They collaborate in the same chat.\n\nOpen Tools & Status to check connections and recent usage. To use an existing project, choose its folder and click Add Project. Partners inspect files as needed after you send the task; changes are kept in isolated copies.',
    startupFailed: 'Beanest could not start', startupRetry: 'Restart using scripts/start-coordinator.command in the project. If this still fails, check that the project Python environment is installed.',
    cleanupUnknown: 'The background service has not confirmed that it stopped. Check Beanest’s active processes and confirm they have ended before trying again.',
    stopUnknown: 'Beanest’s background tasks have not confirmed that they stopped', stopDetail: 'Check Beanest’s active processes. Closing the window does not mean background tasks have ended.',
    folderTitle: 'Choose a Project Folder', folderButton: 'Choose This Folder', folderDetail: 'Choose the complete project. Partners inspect files as needed and keep changes in isolated copies.',
    filesTitle: 'Choose Project Files', filesButton: 'Use Selected Files', filesDetail: 'Choose 1–12 specific files inside this folder. Text formats are checked during registration. Click Add Project after choosing to confirm.',
    buildFailed: 'The Beanest desktop build failed. Check that Node.js and Electron are installed correctly.',
  },
};

function strings(locale) { return COPY[LOCALES.has(locale) ? locale : 'zh-CN']; }
function systemLocale(value) { return typeof value === 'string' && /^zh(?:[-_]|$)/i.test(value) ? 'zh-CN' : 'en'; }

function loadLocale(filename, systemLanguage, legacyProfile = false) {
  try {
    const info = fs.lstatSync(filename);
    if (info.isFile() && info.nlink === 1 && info.size <= 1024) {
      const value = JSON.parse(fs.readFileSync(filename, 'utf8'));
      if (Object.keys(value).length === 1 && LOCALES.has(value.locale)) return value.locale;
    }
  } catch { /* A missing or invalid preference never blocks startup. */ }
  return legacyProfile ? 'zh-CN' : systemLocale(systemLanguage);
}

function saveLocale(filename, locale) {
  if (!LOCALES.has(locale)) throw new TypeError('invalid_locale');
  const temporary = path.join(path.dirname(filename), `.locale-${randomUUID()}.tmp`);
  try {
    fs.writeFileSync(temporary, JSON.stringify({ locale }) + '\n', { flag: 'wx', mode: 0o600 });
    fs.renameSync(temporary, filename);
  } finally {
    if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
  }
}

function installLocale(ipcMain, currentWindow, currentUrl, apply) {
  const { trustedProjectSender } = require('./project-picker');
  ipcMain.handle(LOCALE_CHANNEL, async (event, ...args) => {
    if (args.length !== 1 || !LOCALES.has(args[0]) || !trustedProjectSender(event, currentWindow(), currentUrl())) {
      return { ok: false };
    }
    try {
      apply(args[0]);
      return { ok: true, locale: args[0] };
    } catch { return { ok: false }; }
  });
}

module.exports = { LOCALES, LOCALE_CHANNEL, strings, systemLocale, loadLocale, saveLocale, installLocale };
