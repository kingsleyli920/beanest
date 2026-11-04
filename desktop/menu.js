'use strict';

const DISPLAY_NAME = '豆窝 · Beanest';
const APP_NAME = '豆窝';
const { strings } = require('./locale');

function menuTemplate(platform, showHelp, locale = 'zh-CN') {
  const text = strings(locale);
  const mac = platform === 'darwin';
  const template = [];
  if (mac) template.push({
    label: text.name,
    submenu: [
      { label: text.about, role: 'about' },
      { type: 'separator' },
      { label: text.services, role: 'services' },
      { type: 'separator' },
      { label: text.hide, role: 'hide' },
      { label: text.hideOthers, role: 'hideOthers' },
      { label: text.showAll, role: 'unhide' },
      { type: 'separator' },
      { label: text.quit, role: 'quit' },
    ],
  });
  template.push(
    { label: text.file, submenu: [{ label: text.close, role: 'close' },
      ...(!mac ? [{ label: text.quit, role: 'quit' }] : [])] },
    { label: text.edit, submenu: [
      { label: text.undo, role: 'undo' }, { label: text.redo, role: 'redo' },
      { type: 'separator' },
      { label: text.cut, role: 'cut' }, { label: text.copy, role: 'copy' },
      { label: text.paste, role: 'paste' }, { label: text.pasteStyle, role: 'pasteAndMatchStyle' },
      { label: text.selectAll, role: 'selectAll' },
    ] },
    { label: text.view, submenu: [
      { label: text.reload, role: 'reload' },
      { type: 'separator' },
      { label: text.actualSize, role: 'resetZoom' },
      { label: text.zoomIn, role: 'zoomIn' }, { label: text.zoomOut, role: 'zoomOut' },
      { type: 'separator' },
      { label: text.fullscreen, role: 'togglefullscreen' },
    ] },
    { label: text.window, submenu: [
      { label: text.minimize, role: 'minimize' },
      ...(mac ? [{ label: text.zoomWindow, role: 'zoom' }, { type: 'separator' },
        { label: text.front, role: 'front' }] : []),
    ] },
    { label: text.help, submenu: [{ label: text.howTo, click: showHelp }] },
  );
  return template;
}

module.exports = { APP_NAME, DISPLAY_NAME, menuTemplate };
