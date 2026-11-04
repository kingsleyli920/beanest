'use strict';

const { trustedProjectSender } = require('./project-picker');

const OPEN_WEB_PAGE = 'beanest:open-web-page';

function webPageUrl(value) {
  if (typeof value !== 'string' || value.length > 4096 || /[\x00-\x20]/.test(value)) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : null;
  } catch {
    return null;
  }
}

function installExternalPages(ipcMain, shell, currentWindow, currentUrl) {
  ipcMain.handle(OPEN_WEB_PAGE, async (event, ...args) => {
    const url = args.length === 1 ? webPageUrl(args[0]) : null;
    if (!url || !trustedProjectSender(event, currentWindow(), currentUrl())) {
      return { ok: false };
    }
    try {
      // The renderer uses this only for explicit setup-link clicks. Never hand
      // file paths or arbitrary URI protocols to the operating system.
      await shell.openExternal(url);
      return { ok: true };
    } catch {
      return { ok: false };
    }
  });
}

module.exports = { OPEN_WEB_PAGE, webPageUrl, installExternalPages };
