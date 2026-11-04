'use strict';

// Only explicitly marked interface copy is translated. Callers pass authored
// content as parameters, so messages, paths and source files stay unchanged.
(() => {
  const preferenceKey = 'beanest:locale';
  const supported = new Set(['zh-CN', 'en']);
  const english = window.BeanestI18nEnglish || Object.create(null);
  const listeners = new Set();
  const proxies = new WeakMap();
  let saved = null;
  try { saved = localStorage.getItem(preferenceKey); } catch (_) { /* Storage can be denied. */ }
  let locale;
  if (supported.has(saved)) locale = saved;
  else if (supported.has(window.beanestDesktop?.currentLocale)) locale = window.beanestDesktop.currentLocale;
  else {
    let returning = /^#room=/.test(window.location?.hash || '');
    try { returning ||= !!sessionStorage.getItem('lac-rooms-pending-v1'); } catch (_) { /* Optional migration hint. */ }
    const language = typeof navigator === 'object' ? navigator.language : 'zh-CN';
    locale = returning || /^zh\b/i.test(language) ? 'zh-CN' : 'en';
  }
  try { localStorage.setItem(preferenceKey, locale); } catch (_) { /* Session-only preference remains usable. */ }
  function t(key, params = {}) {
    const copy = locale === 'en' && Object.hasOwn(english, key) ? english[key] : key;
    return copy.replace(/\{(p\d+)\}/g, (token, name) => Object.hasOwn(params, name) ? String(params[name]) : token);
  }
  function labels(value) {
    if (!value || typeof value !== 'object') return value;
    if (proxies.has(value)) return proxies.get(value);
    const original = new Map(Reflect.ownKeys(value).map(key => [key, value[key]]));
    const proxy = new Proxy(value, { get(target, property, receiver) {
      const item = Reflect.get(target, property, receiver);
      if (!original.has(property) || original.get(property) !== item) return item;
      return typeof item === 'string' ? t(item) : item && typeof item === 'object' ? labels(item) : item;
    } });
    proxies.set(value, proxy); return proxy;
  }
  function applyStatic(root = document) {
    for (const node of root.querySelectorAll('[data-i18n]')) node.textContent = t(node.getAttribute('data-i18n'));
    for (const attribute of ['aria-label', 'title', 'placeholder']) {
      for (const node of root.querySelectorAll(`[data-i18n-${attribute}]`)) node.setAttribute(attribute, t(node.getAttribute(`data-i18n-${attribute}`)));
    }
    if (document.documentElement) document.documentElement.lang = locale;
    document.title = locale === 'en' ? 'Beanest' : '豆窝 · Beanest';
  }
  function setLocale(next) {
    if (!supported.has(next) || next === locale) return;
    locale = next;
    try { localStorage.setItem(preferenceKey, next); } catch (_) { /* Session-only preference remains usable. */ }
    if (typeof window.beanestDesktop?.setLocale === 'function') Promise.resolve(window.beanestDesktop.setLocale(next)).catch(() => {});
    applyStatic();
    for (const listener of listeners) listener(next);
  }
  window.BeanestI18n = Object.freeze({ t, labels, applyStatic, setLocale, getLocale: () => locale,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); } });
  applyStatic();
})();
