'use strict';

const { contextBridge, ipcRenderer } = require('electron');

const selectionErrors = new Set([
  'project_selection_unavailable', 'project_selection_outside_directory',
  'project_selection_unsafe_path', 'invalid_project_selection',
]);

const initialLocale = typeof process !== 'undefined' && Array.isArray(process.argv)
  && process.argv.includes('--beanest-locale=en') ? 'en' : 'zh-CN';

contextBridge.exposeInMainWorld('beanestDesktop', Object.freeze({
  currentLocale: initialLocale,
  setLocale: async (locale) => {
    if (locale !== 'zh-CN' && locale !== 'en') return { ok: false };
    try {
      const result = await ipcRenderer.invoke('beanest:set-locale', locale);
      return result?.ok === true && result.locale === locale ? { ok: true, locale } : { ok: false };
    } catch { return { ok: false }; }
  },
  keepsAutonomousWorkAwake: true,
  openWebPage: async (url) => {
    try {
      const result = await ipcRenderer.invoke('beanest:open-web-page', url);
      return result?.ok === true;
    } catch {
      return false;
    }
  },
  onOpened: (listener) => {
    if (typeof listener !== 'function') throw new TypeError('listener must be a function');
    const notify = () => listener();
    ipcRenderer.on('beanest:opened', notify);
    return () => ipcRenderer.removeListener('beanest:opened', notify);
  },
  chooseProjectFolder: async () => {
    try {
      const result = await ipcRenderer.invoke('beanest:choose-project-folder');
      if (result?.ok === true) return result.value;
      throw new Error(selectionErrors.has(result?.error) ? result.error : 'project_selection_unavailable');
    } catch (error) {
      // Electron may include remote exception details. Only stable codes cross
      // the isolated bridge; no native paths, stack, IPC or fs access is exposed.
      throw new Error(selectionErrors.has(error?.message) ? error.message : 'project_selection_unavailable');
    }
  },
  chooseProjectFiles: async () => {
    try {
      const result = await ipcRenderer.invoke('beanest:choose-project-files');
      if (result?.ok === true) return result.value;
      throw new Error(selectionErrors.has(result?.error) ? result.error : 'project_selection_unavailable');
    } catch (error) {
      // Electron may include remote exception details. Only stable codes cross
      // the isolated bridge; no native paths, stack, IPC or fs access is exposed.
      throw new Error(selectionErrors.has(error?.message) ? error.message : 'project_selection_unavailable');
    }
  },
}));
