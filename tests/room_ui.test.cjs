'use strict';

// Run the shipped browser script against a small DOM and an in-memory HTTP peer.
// This checks event wiring and rendered output; it never starts a server or CLI.
const assert = require('node:assert/strict');
const { webcrypto, createHash } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const assetRoot = path.join(__dirname, '..', 'src', 'local_agent_coordinator', 'room_ui');
const html = fs.readFileSync(path.join(assetRoot, 'index.html'), 'utf8');
// The page's classic scripts, in the order index.html loads them. Each one runs as
// its own vm script so a load-order or duplicate-declaration fault fails here as it
// would in a browser.
const pageScripts = [...html.matchAll(/<script src="\/([^"]+)" defer><\/script>/g)]
  .map(([, file]) => ({ file, source: fs.readFileSync(path.join(assetRoot, file), 'utf8') }));
const script = pageScripts.filter(({ file }) => !/^(i18n|markdown)/.test(file)).map(({ source }) => source).join('\n');
const roomId = '11111111-1111-4111-8111-111111111111';
const jobId = '22222222-2222-4222-8222-222222222222';
const catalog = [
  { id: 'claude', name: 'Claude Code', avatar: 'C' },
  { id: 'codex', name: 'Codex', avatar: 'X' },
  { id: 'test_peer', name: '第三位测试伙伴', avatar: 'T' },
];

class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = new Map();
    this.dataset = {};
    this.listeners = new Map();
    this.className = '';
    this.id = '';
    this.value = '';
    this.hidden = false;
    this.disabled = false;
    this.scrollHeight = 1000;
    this.clientHeight = 600;
    this.scrollTop = 400;
    this._text = '';
    this.classList = {
      contains: (name) => this.className.split(/\s+/).includes(name),
      remove: (name) => { this.className = this.className.split(/\s+/).filter((value) => value !== name).join(' '); },
      toggle: (name, force) => {
        const enabled = force ?? !this.classList.contains(name);
        this.classList.remove(name);
        if (enabled) this.className = `${this.className} ${name}`.trim();
        return enabled;
      },
    };
  }

  set textContent(value) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this._text = String(value);
  }

  get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children.at(-1) || null; }
  set innerHTML(_value) { throw new Error('Browser UI must render untrusted content as text.'); }

  append(...nodes) { for (const node of nodes) this.insertBefore(node, null); }

  insertBefore(node, next) {
    if (node === next) return node;
    node.remove();
    const index = next === null ? this.children.length : this.children.indexOf(next);
    assert.ok(index >= 0, 'insertBefore requires an attached sibling');
    this.children.splice(index, 0, node);
    node.parentNode = this;
    return node;
  }

  replaceChildren(...nodes) {
    this.textContent = '';
    this.append(...nodes);
  }

  remove() {
    if (this.parentNode) {
      this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
      this.parentNode = null;
    }
  }

  replaceWith(node) {
    assert.ok(this.parentNode);
    this.parentNode.insertBefore(node, this);
    this.remove();
  }

  after(node) {
    assert.ok(this.parentNode);
    const siblings = this.parentNode.children;
    this.parentNode.insertBefore(node, siblings[siblings.indexOf(this) + 1] || null);
  }

  setAttribute(name, value) {
    assert.ok(!/^on/i.test(name) && name !== 'style', 'CSP-compatible DOM uses no inline handlers or styles');
    this.attributes.set(name, String(value));
    if (name === 'id') this.id = String(value);
    if (name === 'class') this.className = String(value);
    if (name.startsWith('data-')) this.dataset[name.slice(5)] = String(value);
    if (name === 'content') this.content = String(value);
    if (name === 'disabled' || name === 'hidden') this[name] = true;
  }

  getAttribute(name) { return this.attributes.get(name) ?? null; }

  removeAttribute(name) { this.attributes.delete(name); }
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }

  addEventListener(name, listener) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(listener);
  }

  fire(name, fields = {}) {
    if (name === 'click' && this.disabled) return;
    const event = { target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...fields };
    for (const listener of this.listeners.get(name) || []) listener(event);
  }

  focus() { this.focused = true; if (this.ownerDocument) this.ownerDocument.activeElement = this; }
  click() {
    if (this.tagName === 'A' && this.download && this.href) this.ownerDocument.downloads.push({ filename: this.download, blob: this.ownerDocument.blobs.get(this.href) });
    this.fire('click');
  }
}

function descendants(root) {
  return root.children.flatMap((node) => [node, ...descendants(node)]);
}

function matches(node, selector) {
  if (selector.startsWith('.')) return node.classList.contains(selector.slice(1));
  if (selector.startsWith('#')) return node.id === selector.slice(1);
  if (selector === '[data-target]') return Object.hasOwn(node.dataset, 'target');
  if (selector === '[data-participant]') return Object.hasOwn(node.dataset, 'participant');
  const attribute = selector.match(/^\[([a-z0-9-]+)\]$/);
  if (attribute) return node.getAttribute(attribute[1]) !== null && node.attributes.has(attribute[1]);
  if (selector === 'meta[name="lac-token"]') return node.tagName === 'META' && node.getAttribute('name') === 'lac-token';
  return node.tagName.toLowerCase() === selector;
}

function createDocument() {
  const root = new FakeElement('document');
  const stack = [root];
  const voidTags = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr']);
  // Parse only structure and attributes. The production script supplies the text
  // exercised below; this is not a general HTML parser or browser substitute.
  for (const token of html.matchAll(/<\/?([a-z][a-z0-9-]*)\b([^>]*)>/gi)) {
    const [, tag, attributes] = token;
    if (token[0].startsWith('</')) { stack.pop(); continue; }
    const node = new FakeElement(tag);
    for (const attribute of attributes.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) node.setAttribute(attribute[1], attribute[2] ?? '');
    stack.at(-1).append(node);
    if (!voidTags.has(tag.toLowerCase())) stack.push(node);
  }
  const document = {
    root,
    hidden: false,
    activeElement: null,
    downloads: [], blobs: new Map(),
    createElement: (tag) => { const node = new FakeElement(tag); node.ownerDocument = document; return node; },
    getElementById: (id) => descendants(root).find((node) => node.id === id) || null,
    querySelector: (selector) => descendants(root).find((node) => matches(node, selector)) || null,
    querySelectorAll: (selector) => descendants(root).filter((node) => matches(node, selector)),
    addEventListener: (...args) => root.addEventListener(...args),
  };
  for (const node of descendants(root)) node.ownerDocument = document;
  document.body = document.querySelector('body');
  document.documentElement = document.querySelector('html');
  return document;
}

function snapshot({ agents = catalog, status = 'completed', events = [], jobs = [] } = {}) {
  return {
    room: { id: roomId, goal: '完全虚构的文字目标', status, created_at: 1, updated_at: 2 },
    members: agents.map((agent) => ({ ...agent, provider: agent.id, status: 'idle', session_id: null, current_task: null, usage: null })),
    events, jobs, budget: { used: 0, limit: 8, deadline: 1000 },
  };
}

function message(seq, sender, text, recipient = 'all') {
  return { seq, kind: 'message', sender, recipient, text, created_at: seq };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

const pendingCryptoDigests = new Set();
function trackedDigest(...args) {
  // Return the real operation's result/rejection, while tracking only its
  // native computation. Deliberately held fetches are not part of settlement.
  const operation = Promise.resolve(webcrypto.subtle.digest(...args));
  const tracked = operation.finally(() => pendingCryptoDigests.delete(tracked));
  pendingCryptoDigests.add(tracked);
  return tracked;
}
async function settle() {
  const deadline = Date.now() + 2000;
  for (let turn = 0; turn < 5; turn += 1) {
    await new Promise(setImmediate);
    if (pendingCryptoDigests.size) {
      await waitUntil(() => pendingCryptoDigests.size === 0,
        'browser SHA computation did not settle within the harness deadline', Math.max(0, deadline - Date.now()));
    }
  }
}

async function waitUntil(predicate, message, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate() && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 5));
  assert.ok(predicate(), message);
}

// Minimal transactional IndexedDB peer. Writes are staged until commit, so a
// request's success callback is deliberately weaker than durable completion.
const outboxDatabases = new WeakMap();
function fakeIndexedDB(storage, failure) {
  let records = outboxDatabases.get(storage);
  if (!records) { records = new Map(); outboxDatabases.set(storage, records); }
  return { open() {
    const open = {};
    queueMicrotask(() => {
      if (failure?.open) { open.error = new Error('open failed'); open.onerror?.(); return; }
      open.result = { createObjectStore() {}, transaction(_name, mode) {
        const staged = new Map([...records].map(([key, value]) => [key, structuredClone(value)]));
        let pending = 0; let ended = false; let scheduled = false;
        const transaction = { error: null, abort() { if (ended) return; ended = true; transaction.error = new Error('aborted'); transaction.onabort?.(); },
          objectStore() {
            const request = (operation) => {
              const result = {}; pending++;
              queueMicrotask(() => {
                if (ended) return;
                try { result.result = operation(); result.onsuccess?.(); }
                catch (error) { transaction.error = error; result.onerror?.(); transaction.abort(); }
                pending--; finish();
              }); return result;
            };
            return { get: (key) => request(() => structuredClone(staged.get(key))),
              put: (value) => request(() => { if (failure?.put) throw new Error('put failed'); staged.set(value.scope, structuredClone(value)); }),
              delete: (key) => request(() => staged.delete(key)) };
          } };
        function finish() {
          if (pending || ended || scheduled) return; scheduled = true;
          queueMicrotask(() => {
            scheduled = false; if (pending || ended) return;
            if (mode === 'readwrite' && failure?.commit) { transaction.abort(); return; }
            ended = true;
            if (mode === 'readwrite') { records.clear(); for (const [key, value] of staged) records.set(key, value); }
            transaction.oncomplete?.();
          });
        }
        finish(); return transaction;
      } };
      open.onupgradeneeded?.(); open.onsuccess?.();
    }); return open;
  } };
}
function savedOutbox(storage) {
  const records = outboxDatabases.get(storage); const index = [...(records?.values() || [])].find((row) => Array.isArray(row.request_ids));
  if (!index) return undefined;
  const entries = index.request_ids.map((key) => records.get(`${index.scope}:${key}`)).filter(Boolean);
  const hint = JSON.parse(storage.get('lac-rooms-pending-v1') || 'null');
  const main = entries.find((entry) => entry.request.body.request_id === hint?.request_id) || entries.find((entry) => !entry.request.detached && !entry.request.intervened);
  return { scope: index.scope, request: main?.request || null, sidelined_requests: entries.filter((entry) => entry !== main), deleted_rooms: index.deleted_rooms };
}
async function browser({ language = 'zh-CN', languageStorage = new Map(), agents = catalog, rooms = [], projects = [], partners, saved, storage = new Map(), token = 'fictional-test-connection', route, taskModes = ['text'], desktopBridge, selectSaved = true, now = Date.now, selection = '', executionPolicySupport = false, continuousObjectiveSupport = executionPolicySupport, nativeProjectSupport = false, naturalContinuationSupport = false, roomLifecycleSupport = false, roomNativeSessionCleanupSupport = false, messageReceiptsSupport = false, projectDeletionSupport = false, naturalRuntimeSupport = false, partnerLifecycleSupport = false, locationHash = '', idbFailure, pagingSupport = false, hostOutboxScope = null, mediaSupport = false, codeStatusSupport = false, memberInvitationSupport = false, taskWithdrawalSupport = false, requestIdStart = 0 } = {}) {
  const document = createDocument();
  const location = { hash: locationHash, pathname: '/', search: '' };
  const history = { replaceState(_state, _title, url) { location.hash = url.includes('#') ? url.slice(url.indexOf('#')) : ''; } };
  document.querySelector('meta[name="lac-token"]').content = token;
  const requests = [];
  const polls = [];
  const windowEvents = new Map();
  const timeouts = new Map();
  let timeoutId = 0;
  let nextId = requestIdStart;
  const context = vm.createContext({
    navigator: { language }, localStorage: { getItem: key => languageStorage.get(key) ?? null, setItem: (key, value) => languageStorage.set(key, value) },
    document, window: { location, history, addEventListener(type, callback) { if (!windowEvents.has(type)) windowEvents.set(type, []); windowEvents.get(type).push(callback); }, getSelection: () => ({ toString: () => selection }), ...(desktopBridge ? { beanestDesktop: desktopBridge } : {}) }, console, TextEncoder, AbortController, Blob, atob,
    Date: class extends Date { static now() { return now(); } },
    URL: { createObjectURL: (blob) => { const key = `blob:fictional-${document.blobs.size}`; document.blobs.set(key, blob); return key; }, revokeObjectURL: (url) => document.blobs.delete(url) },
    crypto: { subtle: { digest: trackedDigest }, randomUUID: () => `00000000-0000-4000-8000-${String(++nextId).padStart(12, '0')}` },
    indexedDB: fakeIndexedDB(storage, idbFailure),
    sessionStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
    setTimeout: (callback, delay = 0) => { const id = ++timeoutId; timeouts.set(id, { callback, due: now() + delay }); return id; },
    clearTimeout: (id) => timeouts.delete(id),
    setInterval: (callback) => { polls.push(callback); return polls.length; },
    clearInterval: (id) => { polls[id - 1] = null; },
    fetch: async (url, options) => {
      assert.match(url, /^\/api\//, 'tests cannot perform real network requests');
      const request = { path: url, method: options.method, body: options.body === undefined ? undefined : JSON.parse(options.body), headers: options.headers };
      requests.push(request);
      let value = route ? await route(request, document, options) : undefined;
      if (value === undefined) {
        if (url === '/api/status') value = { app: 'local-agent-rooms', agents, task_modes: taskModes, ...(executionPolicySupport || nativeProjectSupport || naturalContinuationSupport || roomLifecycleSupport || roomNativeSessionCleanupSupport || messageReceiptsSupport || projectDeletionSupport || naturalRuntimeSupport || partnerLifecycleSupport || pagingSupport || hostOutboxScope || mediaSupport || codeStatusSupport || memberInvitationSupport || taskWithdrawalSupport ? { ...(hostOutboxScope ? { user_outbox_scope: hostOutboxScope } : {}), ...(pagingSupport ? { transport_limits: { max_message_utf8_bytes: 1048576, max_request_utf8_bytes: 8388608, event_page_default_limit: 100, event_page_default_bytes: 262144, event_page_max_limit: 500, event_page_max_bytes: 8388608 } } : {}), capabilities: { ...(taskWithdrawalSupport ? { task_withdrawal_v1: true } : {}), ...(memberInvitationSupport ? { room_member_invitation_v1: true } : {}), ...(codeStatusSupport ? { room_code_status_v1: true } : {}), ...(mediaSupport ? { room_media_v1: true } : {}), ...(hostOutboxScope ? { user_outbox_v1: true } : {}), ...(pagingSupport ? { room_paging_v1: true } : {}), ...(partnerLifecycleSupport ? { partner_lifecycle_operations: true } : {}), ...(naturalRuntimeSupport ? { natural_runtime_settings: true } : {}), ...(messageReceiptsSupport ? { message_receipts: true } : {}), ...(projectDeletionSupport ? { project_registration_deletion: true } : {}), ...(roomNativeSessionCleanupSupport ? { room_native_session_cleanup: true } : {}), ...(roomLifecycleSupport ? { room_lifecycle_operations: true } : {}), ...(executionPolicySupport ? { execution_policy: true } : {}), ...(continuousObjectiveSupport ? { continuous_objective: true } : {}), ...(nativeProjectSupport ? { native_project: true } : {}), ...(naturalContinuationSupport ? { natural_continuation: true } : {}) } } : {}) };
        else if (['/api/rooms', '/api/rooms?view=compact'].includes(url) && options.method === 'GET') value = { rooms };
        else if (url === '/api/projects' && options.method === 'GET') value = { projects };
        else if (['/api/partners', '/api/partners?view=compact'].includes(url) && options.method === 'GET') value = { partners: partners || agents, supported_adapters: [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }] };
        else if (url === `/api/rooms/${roomId}` && options.method === 'GET' && saved) value = saved;
        else throw new Error(`Unexpected fake route: ${options.method} ${url}`);
      }
      return { ok: true, status: 200, json: async () => structuredClone(value) };
    },
  });
  for (const [index, { file, source }] of pageScripts.entries()) {
    // A browser drains microtasks between deferred scripts; a script that starts async work too early must show it.
    if (index) await new Promise((resolve) => setImmediate(resolve));
    await vm.runInContext(source, context, { filename: `room_ui/${file}` });
  }
  // Most history tests explicitly open their saved fixture. The product itself
  // starts on the welcome page unless an exact room UUID is in the URL hash.
  if (selectSaved && saved && rooms.some((room) => room.id === saved.room.id)
      && document.getElementById('room-status').hidden && document.getElementById('pending-request').hidden) {
    const card = document.getElementById('rooms').children.find((node) => node.title === (saved.room.goal || saved.room.goal_preview));
    if (card) { card.fire('click'); await settle(); }
  }
  return {
    document, requests, storage, location,
    refresh: () => vm.runInContext("refresh()", context),
    dispatchPending(request) { context.testPending = structuredClone(request); return vm.runInContext("state.pending = testPending; submitPending()", context); },
    async windowEvent(type) { for (const listener of windowEvents.get(type) || []) listener({ type }); await settle(); },
    expireDetachedReceipts() { vm.runInContext("for (const entry of state.sidelinedRequests.values()) if (entry.receipt === 'sending') entry.receipt = 'unknown';", context); },
    get: (id) => { const node = document.getElementById(id); assert.ok(node, `missing #${id}`); return node; },
    async click(id) { this.get(id).fire('click'); await settle(); },
    async choose(id) {
      const button = document.querySelectorAll('[data-target]').find((node) => node.dataset.target === id);
      assert.ok(button, `missing target ${id}`);
      button.fire('click'); await settle();
    },
    async invite(id, checked = true) {
      const input = document.querySelectorAll('[data-participant]').find((node) => node.dataset.participant === id);
      assert.ok(input, `missing participant ${id}`);
      input.checked = checked; input.fire('change'); await settle();
    },
    selectText(text) { selection = text; },
    type(text) { this.get('message').value = text; this.get('message').setSelectionRange(text.length, text.length); this.get('message').fire('input'); },
    async submit() { this.get('composer').fire('submit'); await settle(); },
    async runTimers() {
      const due = [...timeouts].filter(([, timer]) => timer.due <= now());
      for (const [id, timer] of due) if (timeouts.delete(id)) timer.callback();
      await settle();
    },
    async poll() { for (const callback of [...polls]) if (callback) callback(); await settle(); await this.runTimers(); },
    posts() { return requests.filter((request) => request.method === 'POST'); },
    targets() { return document.querySelectorAll('[data-target]').map((node) => node.dataset.target); },
    members() { return this.get('member-cards').children.map((node) => node.id); },
  };
}

test('the directory lists every partner but only the default pair joins a new goal', async () => {
  const ui = await browser();
  assert.deepEqual(ui.members(), catalog.slice(0, 2).map((agent) => `member-${agent.id}`));
  assert.deepEqual(ui.targets(), ['all', 'claude', 'codex']);
  assert.equal(ui.get('participant-picker').children.length, 3);
  assert.equal(ui.get('discovery-hint').hidden, true, 'older backends without discovery keep the existing quiet flow');
  assert.equal(ui.get('empty-avatars').children.length, 2);
  for (const agent of catalog.slice(0, 2)) {
    assert.equal(ui.get(`name-${agent.id}`).textContent, agent.name);
    assert.equal(ui.get(`status-${agent.id}`).textContent, '未启动');
  }
  assert.equal(ui.get('member-claude').firstChild.firstChild.classList.contains('bean-style-peach'), true);
  assert.equal(ui.get('member-codex').firstChild.firstChild.classList.contains('bean-style-sprout'), true);
  await ui.poll();
  await ui.click('refresh');
  assert.equal(ui.posts().length, 0);
  assert.equal(ui.get('send').disabled, true);
});

test('compact history is collapsed initially, survives polling when expanded and keeps records and drafts accessible', async () => {
  const saved = snapshot({ events: [message(1, 'codex', '这段已保存的对话仍可查看。')] });
  const ui = await browser({ rooms: [saved.room], saved, selectSaved: false });
  assert.equal(ui.get('toggle-history').getAttribute('aria-controls'), 'rooms');
  assert.equal(ui.get('toggle-history').getAttribute('aria-expanded'), 'false');
  assert.equal(ui.get('rooms').children.length, 1, 'collapse preserves the actual history cards');
  ui.type('还没发送的新任务草稿');
  await ui.click('toggle-history');
  await ui.poll();
  assert.equal(ui.get('toggle-history').getAttribute('aria-expanded'), 'true');
  assert.equal(ui.get('rooms').classList.contains('history-expanded'), true);
  assert.match(ui.get('toggle-history').textContent, /收起记录.*1/);
  ui.get('rooms').firstChild.fire('click');
  await settle();
  assert.equal(ui.get('toggle-history').getAttribute('aria-expanded'), 'false', 'selecting a record returns space to its conversation');
  assert.match(ui.get('events').textContent, /已保存的对话仍可查看/);
  await ui.click('toggle-history');
  assert.equal(ui.get('rooms').children.length, 1);
  await ui.click('new-room');
  assert.equal(ui.get('message').value, '还没发送的新任务草稿');
  assert.equal(ui.get('toggle-history').getAttribute('aria-expanded'), 'false');
  assert.equal(ui.posts().length, 0, 'opening history and restoring a draft never dispatch models');
});

test('a third descriptor can receive a new goal and a later message, with double submit blocked', async () => {
  const pending = deferred();
  let creates = 0;
  const saved = snapshot();
  const ui = await browser({ route: async (request) => {
    if (request.method !== 'POST') return;
    if (request.path === '/api/rooms') { creates += 1; return pending.promise; }
    if (request.path.endsWith('/messages')) return saved;
  } });
  await ui.invite('test_peer');
  await ui.choose('test_peer');
  ui.type('请第三位测试伙伴核对这段虚构文本。');
  await ui.submit();
  await ui.submit();
  assert.equal(creates, 1);
  assert.equal(ui.get('new-room').disabled, true);
  assert.equal(ui.posts()[0].body.target, 'test_peer');
  assert.match(ui.posts()[0].body.request_id, /^[0-9a-f-]{36}$/);
  pending.resolve(saved);
  await settle();
  await ui.invite('test_peer');
  await ui.choose('test_peer');
  ui.type('补充：只比较措辞。');
  await ui.submit();
  assert.equal(ui.posts().length, 2);
  assert.equal(ui.posts()[1].path, `/api/rooms/${roomId}/messages`);
  assert.equal(ui.posts()[1].body.target, 'test_peer');
  assert.equal(ui.posts()[1].body.text, '补充：只比较措辞。');
  assert.equal(ui.get('pending-request').hidden, true);
});

test('an older room keeps its registered members while a new goal sees the expanded catalog', async () => {
  const saved = snapshot({ agents: catalog.slice(0, 2), events: [message(1, 'codex', '历史正文')] });
  const ui = await browser({ rooms: [saved.room], saved });
  assert.deepEqual(ui.members(), ['member-claude', 'member-codex']);
  assert.deepEqual(ui.targets(), ['all', 'claude', 'codex']);
  const firstMessage = ui.get('events').firstChild;
  ui.get('timeline').scrollTop = 20;
  await ui.poll();
  assert.equal(ui.get('events').firstChild, firstMessage, 'polling preserves existing message nodes');
  assert.equal(ui.get('timeline').scrollTop, 20, 'polling does not jump a reader to the bottom');
  assert.equal(ui.document.getElementById('member-test_peer'), null);
  await ui.click('new-room');
  assert.equal(ui.get('timeline').scrollTop, 0, 'a new goal starts at the top instead of inheriting a chat scroll position');
  assert.deepEqual(ui.targets(), ['all', 'claude', 'codex']);
  assert.equal(ui.get('member-cards').children.length, 2);
  await ui.invite('test_peer');
  await ui.choose('test_peer');
  ui.get('rooms').firstChild.fire('click');
  await settle();
  assert.deepEqual(ui.targets(), ['all', 'claude', 'codex']);
  assert.equal(ui.document.querySelectorAll('[data-target]').find((node) => node.dataset.target === 'all').getAttribute('aria-pressed'), 'true');
  assert.equal(ui.posts().length, 0);
});

test('usage stays in member details; real peer requests and MCP identity remain visible', async () => {
  const saved = snapshot({ jobs: [{ id: jobId, provider: 'test_peer', kind: 'review', status: 'completed', text: '审核摘要' }], events: [
    message(1, 'mcp', '来自明确 MCP 接入的目标', 'test_peer'),
    message(2, 'test_peer', '真实可见答复'),
    { seq: 3, kind: 'job_created', sender: 'test_peer', recipient: 'claude', job_id: jobId, job_kind: 'review', text: '请审核这段正文。', created_at: 3 },
    { seq: 4, kind: 'usage', sender: 'test_peer', input_tokens: 987654321, created_at: 4 },
    { seq: 5, kind: 'job_finished', sender: 'test_peer', status: 'completed', job_id: jobId, created_at: 5 },
  ] });
  saved.members[2].usage = { source: 'fictional-provider-snapshot', scope: 'provider_reported_latest_result',
    values: { input_tokens: 987654321, output_tokens: 42, cached_input_tokens: 3 }, job_id: jobId, observed_at: 4 };
  const ui = await browser({ rooms: [saved.room], saved });
  const chat = ui.get('events').textContent;
  assert.match(chat, /真实可见答复/);
  assert.match(chat, /MCP 接入/);
  assert.match(chat, /审核请求/);
  assert.match(chat, /请审核这段正文/);
  assert.doesNotMatch(chat, /987654321|用量快照|本轮任务结束/);
  assert.match(ui.get('usage-test_peer').textContent, /987654321/);
  assert.match(ui.get('usage-test_peer').textContent, /不可用/);
  assert.match(ui.get('usage-test_peer').textContent, /输入 Token987654321/);
  assert.match(ui.get('usage-test_peer').textContent, /输出 Token42/);
  assert.match(ui.get('usage-test_peer').textContent, /供应商最新一次结果快照/);
  assert.doesNotMatch(ui.get('usage-test_peer').textContent, /job_id|observed_at|values|\{"input_tokens"/);
  assert.match(ui.get('usage-claude').textContent, /不填零/);
  assert.equal(ui.get('activity-events').children.length, 3);
  assert.equal(ui.get('member-cards').children.length, 3, 'MCP is a source, not another agent member');
});

test('a round handoff and session rotation show one line each and the handoff expands into its labelled parts', async () => {
  // Catches: either timeline line missing, the handoff text unreachable or dumped as raw JSON, or English copy absent.
  const handoff = { goal: '把数值改成二', done: ['main.py 写好了'], decisions: ['备注不动'], unverified: ['没跑引擎'], key_files: ['src/main.py'] };
  const saved = snapshot({ events: [
    { seq: 1, kind: 'round_handoff_recorded', sender: 'claude', provider: 'claude', job_id: jobId, run_id: null, handoff, context_tokens: 350000, created_at: 1800000000 },
    { seq: 2, kind: 'partner_session_rotated', sender: 'system', provider: 'claude', run_id: null, handoff_seq: 1, context_tokens: 350000, created_at: 1800000001 },
  ] });
  const ui = await browser({ rooms: [saved.room], saved });
  const text = ui.get('events').textContent;
  assert.match(text, /Claude Code 写好了本轮交接/);
  assert.match(text, /Claude Code 换了新会话，只带这份交接/);
  assert.match(text, /查看交接内容/);
  assert.match(text, /目标把数值改成二/);
  assert.match(text, /已定决定备注不动/);
  assert.match(text, /没验证的部分没跑引擎/);
  assert.match(text, /关键文件src\/main\.py/);
  assert.doesNotMatch(text, /"goal"|context_tokens/);
  saved.events.push({ seq: 3, kind: 'run_started', number: 3, session_rotated: true, created_at: 1800000002 });
  const rotatedRun = await browser({ rooms: [saved.room], saved });
  assert.match(rotatedRun.get('activity-events').textContent, /第 3 轮协作已开始，保留此前交付，这一轮换了新会话/);

  const english = await browser({ language: 'en', rooms: [saved.room], saved });
  assert.match(english.get('events').textContent, /Claude Code wrote this round's handoff/);
  assert.match(english.get('events').textContent, /Claude Code moved to a new session and carries only this handoff/);

  // Catches: the between-task record staying invisible, unreadable, or without English copy.
  saved.events.push({ seq: 4, kind: 'task_handoff_recorded', sender: 'claude', provider: 'claude', job_id: jobId, run_id: null, task_id: 'T1', handoff, context_tokens: 500000, created_at: 1800000003 });
  const betweenTasks = await browser({ rooms: [saved.room], saved });
  assert.match(betweenTasks.get('events').textContent, /Claude Code 写好了任务交接/);
  assert.equal((betweenTasks.get('events').textContent.match(/查看交接内容/g) || []).length, 2);
  const betweenTasksEnglish = await browser({ language: 'en', rooms: [saved.room], saved });
  assert.match(betweenTasksEnglish.get('events').textContent, /Claude Code wrote a handoff for the next task/);
  assert.match(english.get('events').textContent, /Settled decisions/);
});

test('a message sent again after its reply call died shows one line naming the partner', async () => {
  // Catches: the redelivery hidden from the timeline, shown as a bare "进展", or without English copy.
  const saved = snapshot({ events: [
    { seq: 1, kind: 'user_message_redelivered', sender: 'system', recipient: 'claude', source_job_id: jobId, source_run_id: null, created_at: 1800000000 },
  ] });
  const ui = await browser({ rooms: [saved.room], saved });
  assert.match(ui.get('events').textContent, /Claude Code 上次没回完你的消息，已重新发给它。/);
  const english = await browser({ language: 'en', rooms: [saved.room], saved });
  assert.match(english.get('events').textContent, /Claude Code did not finish replying to your message, so it was sent again\./);
});

test('a cache miss shows one short note for the latest occurrence and stays out of the chat', async () => {
  // Catches: the note missing, listing every miss, leaking into the chat, or showing the wrong unit.
  const miss = (seq, written) => ({ seq, kind: 'native_cache_miss', sender: 'claude', provider: 'claude', job_id: jobId, run_id: null,
    phase: 'task', cache_creation_input_tokens: written, seconds_since_previous_call: 35, created_at: 1800000000 });
  const saved = snapshot({ events: [miss(1, 700000), miss(2, 785000)] });
  saved.latest_native_cache_miss = miss(2, 785000);
  const ui = await browser({ rooms: [saved.room], saved });
  const note = ui.get('cache-miss-note');
  assert.equal(note.hidden, false);
  assert.match(note.textContent, /Claude Code 这次调用没用上缓存，多写了约 79 万 token/);
  assert.equal(note.children.filter((child) => child.tagName === 'SPAN').length, 1, 'only the latest miss is listed');
  assert.doesNotMatch(ui.get('events').textContent, /缓存/);

  const english = await browser({ language: 'en', rooms: [saved.room], saved });
  assert.match(english.get('cache-miss-note').textContent, /Claude Code missed the cache on this call and wrote about 785K extra tokens/);

  const clean = snapshot({ events: [] });
  clean.latest_native_cache_miss = null;
  const none = await browser({ rooms: [clean.room], saved: clean });
  assert.equal(none.get('cache-miss-note').hidden, true);
  assert.equal(none.get('cache-miss-note').textContent, '');
});

test('subscription limit is shown as a system exception and monitoring reason, not chat diagnostics', async () => {
  const saved = snapshot({ events: [
    message(1, 'claude', '正常的虚构答复'),
    { seq: 2, kind: 'warning', sender: 'service', code: 'subscription_limit_reached', created_at: 2 },
  ] });
  saved.members[0].monitor = { status: 'needs_user', last_error: 'subscription_limit_reached' };
  const partners = catalog.map((agent, index) => ({ ...agent, monitor: index === 0 ? saved.members[0].monitor : {} }));
  const ui = await browser({ rooms: [saved.room], saved, partners });
  const hint = '订阅额度已用完，请在对应工具查看恢复时间。';
  const events = ui.get('events').textContent;
  assert.match(events, /正常的虚构答复/);
  assert.match(events, new RegExp(hint));
  assert.match(ui.get('partners-list').textContent, new RegExp(hint));
  const normalMessage = ui.get('events').children.find((node) => node.textContent.includes('正常的虚构答复'));
  assert.ok(normalMessage);
  assert.doesNotMatch(normalMessage.textContent, /额度|恢复时间|Token|异常/);
  assert.doesNotMatch(events, /余额|Token|恢复时间未知|剩余额度/);
});

test('unknown CLI and provider failures never claim subscription exhaustion', async () => {
  const saved = snapshot();
  saved.members[0].monitor = { status: 'unknown', last_error: 'cli_nonzero_exit' };
  saved.members[1].monitor = { status: 'unknown', last_error: 'unknown_provider_error' };
  const partners = catalog.map((agent, index) => ({ ...agent, monitor: saved.members[index]?.monitor || {} }));
  const ui = await browser({ rooms: [saved.room], saved, partners });
  const monitoring = ui.get('partners-list').textContent;
  assert.match(monitoring, /本地 CLI 未正常完成，请在伙伴与额度中核对异常。/);
  assert.match(monitoring, /供应商返回未知异常，请在伙伴与额度中核对记录。/);
  assert.doesNotMatch(monitoring, /额度已用完|恢复时间|检查登录、额度/);
});

test('unknown create is never replayed by polling or reload; explicit retry keeps the same request', async () => {
  const storage = new Map();
  const saved = snapshot({ events: [message(1, 'user', '待核对的虚构目标')] });
  const ui = await browser({ storage, route: (request) => {
    if (request.method === 'POST') throw new Error('fictional network loss after side effect');
  } });
  await ui.invite('test_peer');
  await ui.choose('test_peer');
  ui.type('待核对的虚构目标');
  await ui.submit();
  const original = ui.posts()[0].body;
  assert.equal(ui.get('pending-request').hidden, false);
  assert.equal(ui.get('send').disabled, true);
  assert.equal(ui.get('new-room').disabled, true);
  await ui.poll();
  await ui.click('verify-request');
  assert.equal(ui.posts().length, 1);

  const reloaded = await browser({ storage, rooms: [saved.room], saved, route: (request) => request.method === 'POST' ? saved : undefined });
  assert.equal(reloaded.posts().length, 0, 'restoring pending state performs reads only');
  assert.equal(reloaded.get('send').disabled, true);
  assert.equal(reloaded.get('retry-request').disabled, false);
  await reloaded.poll();
  assert.equal(reloaded.posts().length, 0);
  await reloaded.click('retry-request');
  assert.equal(reloaded.posts().length, 1);
  assert.deepEqual(reloaded.posts()[0].body, original);
  assert.equal(reloaded.get('pending-request').hidden, true);
});

test('a changed connection cannot replay a pending request, and a disconnect disables sending', async () => {
  const first = await browser({ route: (request) => {
    if (request.method === 'POST') throw new Error('fictional lost response');
  } });
  first.type('另一段虚构目标');
  await first.submit();
  const changed = await browser({ storage: first.storage, token: 'different-fictional-connection' });
  assert.equal(changed.get('retry-request').disabled, true);
  assert.match(changed.get('pending-description').textContent, /连接已改变/);
  await changed.click('retry-request');
  await changed.poll();
  assert.equal(changed.posts().length, 0);

  let offline = false;
  const disconnected = await browser({ route: () => { if (offline) throw new Error('fictional offline'); } });
  disconnected.type('不能离线派发');
  assert.equal(disconnected.get('send').disabled, false);
  offline = true;
  await disconnected.poll();
  assert.equal(disconnected.get('send').disabled, true);
  assert.equal(disconnected.posts().length, 0);
});

test('an unknown cancellation keeps independent Stop available and is not repeated by status reads', async () => {
  const saved = snapshot({ status: 'running' });
  const ui = await browser({ rooms: [saved.room], saved, route: (request) => {
    if (request.method === 'POST') throw new Error('fictional lost cancellation response');
  } });
  await ui.click('cancel-room');
  assert.equal(ui.posts().length, 1);
  assert.equal(ui.posts()[0].path, `/api/rooms/${roomId}/cancel`);
  assert.deepEqual(ui.posts()[0].body, {});
  assert.equal(ui.get('pending-request').hidden, true);
  assert.equal(ui.get('cancel-room').disabled, false);
  await ui.poll();
  assert.equal(ui.posts().length, 1, 'status polling never repeats cancellation');
  await ui.click('cancel-room');
  assert.equal(ui.posts().length, 2, 'an explicit retry uses the same idempotent cancel route');
});

test('stopped records and uncertain identity remain visible without restarting work', async () => {
  const saved = snapshot({ status: 'cancelled', events: [
    message(1, 'claude', '停止前的真实可见结果'),
    { seq: 2, kind: 'room_status', status: 'cancelled', cancellation_confirmed: false, created_at: 2 },
  ] });
  saved.members[0].identity_uncertain = true;
  const ui = await browser({ rooms: [saved.room], saved });
  assert.equal(ui.get('send').disabled, true);
  assert.equal(ui.get('cancel-room').disabled, false);
  assert.equal(ui.get('cancel-room').hidden, false);
  assert.equal(ui.get('message').disabled, true);
  assert.match(ui.get('status-claude').textContent, /结果待核对/);
  assert.match(ui.get('events').textContent, /停止尚未确认/);
  await ui.poll();
  await ui.click('cancel-room');
  await ui.submit();
  assert.equal(ui.posts().length, 1);
  assert.equal(ui.posts()[0].path, `/api/rooms/${roomId}/cancel`, 'only explicit Stop is sent, never model work');
});

test('invalid catalogs fail closed instead of leaving a formerly connected sender enabled', async (t) => {
  for (const invalid of [[{ id: 'mcp', name: 'reserved' }], [catalog[0], catalog[0]], [{ id: 'bad/id', name: 'invalid' }], [{ id: 'test_peer', name: '' }]]) {
    await t.test(JSON.stringify(invalid), async () => {
      let current = catalog;
      const ui = await browser({ route: (request) => request.path === '/api/status' ? { agents: current } : undefined });
      ui.type('清单失效时不要派发');
      assert.equal(ui.get('send').disabled, false);
      current = invalid;
      await ui.poll();
      assert.equal(ui.get('send').disabled, true);
      assert.equal(ui.get('message').disabled, true);
      assert.match(ui.get('connection').textContent, /未连接/);
      await ui.submit();
      assert.equal(ui.posts().length, 0);
    });
  }
});

test('code artifacts use an explicit mode, one first author and inert versioned files without execution controls', async () => {
  const saved = snapshot();
  saved.room.mode = 'python_artifacts';
  saved.project = { available: true, revision: 1, execution: 'disabled', files: [
    { path: 'toy.py', owner: 'claude', sha256: 'a'.repeat(64), content: 'MESSAGE = "<svg onload=alert(1)>"\n' },
  ] };
  const ui = await browser({ taskModes: ['text', 'python_artifacts'], route(request) {
    if (request.method === 'POST' && request.path === '/api/rooms') return saved;
    if (request.path === `/api/rooms/${roomId}`) return saved;
  } });
  await settle();
  assert.equal(ui.get('task-mode-control').hidden, false);
  ui.get('task-mode').value = 'python_artifacts';
  ui.get('task-mode').fire('change');
  await settle();
  assert.equal(ui.document.querySelectorAll('[data-target]').find((node) => node.dataset.target === 'all').disabled, true);
  ui.type('生成虚构的小函数和独立测试草案，不执行。');
  await ui.submit();
  assert.equal(ui.posts().length, 1);
  assert.equal(ui.posts()[0].body.mode, 'python_artifacts');
  assert.equal(ui.posts()[0].body.target, 'claude');
  assert.equal(ui.get('task-mode-control').hidden, true);
  assert.equal(ui.get('project-details').hidden, false);
  assert.match(ui.get('project-status').textContent, /版本 1.*未执行/);
  assert.match(ui.get('project-files').textContent, /toy\.py.*Claude Code/);
  assert.match(ui.get('project-files').textContent, /<svg onload=alert\(1\)>/);
  assert.equal(descendants(ui.get('project-files')).some((node) => ['BUTTON', 'SVG', 'SCRIPT', 'IFRAME'].includes(node.tagName)), false);
  saved.project = { available: false, error: 'project_unavailable', execution: 'disabled' };
  await ui.poll();
  assert.equal(ui.get('project-files').children.length, 0);
  assert.match(ui.get('project-status').textContent, /不会自动新建/);
  assert.equal(ui.posts().length, 1);
});

test('sandbox mode is available only when advertised and keeps program output outside chat', async () => {
  const saved = snapshot({ events: [message(1, 'claude', '已提交文件供隔离运行。')] });
  saved.room.mode = 'python_sandbox';
  saved.project = { available: true, revision: 2, execution: 'sandboxed_unittest', files: [] };
  saved.executions = [{ id: jobId, revision: 2, snapshot_sha256: 'a'.repeat(64), status: 'completed',
    result: { exit_code: 0, cleanup_confirmed: true, stdout: '{"status":"passed"}<svg onload=alert(1)>',
      stderr: '', truncated: true, report_trust: 'untrusted' } }];
  const ui = await browser({ taskModes: ['text', 'python_artifacts', 'python_sandbox'], route(request) {
    if (request.method === 'POST' && request.path === '/api/rooms') return saved;
    if (request.path === `/api/rooms/${roomId}`) return saved;
  } });
  assert.equal(ui.get('sandbox-mode').hidden, false);
  ui.get('task-mode').value = 'python_sandbox';
  ui.get('task-mode').fire('change');
  ui.type('新建简单Python函数，互审后在隔离环境运行标准库测试。');
  await ui.submit();
  assert.equal(ui.posts()[0].body.mode, 'python_sandbox');
  assert.equal(ui.posts()[0].body.target, 'claude');
  assert.match(ui.get('scope-summary').textContent, /隔离测试/);
  assert.match(ui.get('project-status').textContent, /隔离执行记录/);
  assert.equal(ui.get('executions-details').hidden, false);
  assert.match(ui.get('execution-records').textContent, /版本 2.*进程已结束/);
  assert.match(ui.get('execution-records').textContent, /退出码 0.*已确认清理/);
  assert.match(ui.get('execution-records').textContent, /程序输出.*截短/);
  assert.match(ui.get('execution-records').textContent, /"status":"passed"/);
  assert.doesNotMatch(ui.get('events').textContent, /"status":"passed"|<svg|测试通过/);
  assert.equal(descendants(ui.get('execution-records')).some((node) => ['SVG', 'SCRIPT', 'IFRAME'].includes(node.tagName)), false);
  await ui.poll();
  assert.equal(ui.posts().length, 1);
});

test('unavailable sandbox mode cannot be selected or dispatched', async () => {
  const ui = await browser({ taskModes: ['text', 'python_artifacts'] });
  assert.equal(ui.get('sandbox-mode').hidden, true);
  assert.equal(ui.get('sandbox-mode').disabled, true);
  ui.get('task-mode').value = 'python_sandbox';
  ui.get('task-mode').fire('change');
  assert.equal(ui.get('task-mode').value, 'text');
  ui.type('只做文字，不要求执行。');
  await ui.submit();
  assert.equal(ui.posts()[0].body.mode, undefined);
});

test('descriptor names, avatars and model text stay inert with CSP-compatible local assets', async () => {
  const unsafeName = '<img src=x onerror=alert(1)>';
  const unsafeText = '<svg onload=alert(2)>模型正文</svg>';
  const agents = [...catalog.slice(0, 2), { id: 'test_peer', name: unsafeName, avatar: '<>', bean_style: 'sky" onclick="alert(3)' }];
  const saved = snapshot({ agents, events: [message(1, 'test_peer', unsafeText)] });
  const ui = await browser({ agents, rooms: [saved.room], saved });
  assert.equal(ui.get('name-test_peer').textContent, unsafeName);
  assert.ok(ui.document.querySelectorAll('[data-target]').some((node) => node.textContent === `@${unsafeName}`));
  assert.match(ui.get('events').textContent, /<svg onload=alert\(2\)>模型正文<\/svg>/);
  const messageAvatar = ui.get('events').firstChild.firstChild;
  assert.equal(messageAvatar.tagName, 'SPAN');
  assert.equal(messageAvatar.textContent, '<>');
  assert.deepEqual(messageAvatar.children.map((node) => node.className), ['bean-body', 'bean-accessory', 'avatar-initial']);
  assert.equal(messageAvatar.classList.contains('bean-style-sprout'), true);
  assert.doesNotMatch(messageAvatar.className, /onclick|alert|"/);
  const [beanBody, accessory, avatarInitial] = messageAvatar.children;
  assert.deepEqual(beanBody.children.map((node) => node.className), ['bean-eyes', 'bean-smile']);
  assert.ok(beanBody.children.every((node) => node.children.length === 0 && node.textContent === ''));
  assert.equal(avatarInitial.textContent, '<>');
  assert.equal(avatarInitial.children.length, 0);
  assert.equal(accessory.children.length, 0);
  const avatarParts = descendants(messageAvatar);
  assert.equal(avatarParts.length, 5);
  assert.ok(avatarParts.every((node) => node.tagName === 'SPAN'
    && node.attributes.size === 0 && Object.keys(node.dataset).length === 0 && node.listeners.size === 0));
  assert.deepEqual([...messageAvatar.attributes], [['aria-hidden', 'true']]);
  assert.deepEqual(messageAvatar.dataset, { agent: 'test_peer' });
  assert.equal(descendants(ui.document.root).some((node) => ['IMG', 'SVG', 'IFRAME'].includes(node.tagName)), false);
  assert.deepEqual(ui.document.querySelectorAll('script').map((node) => node.getAttribute('src')), ['/i18n-en-a.js', '/i18n-en-b.js', '/i18n-en-c.js', '/i18n.js', '/markdown.js', '/app.js', '/view-account.js', '/view-manager.js', '/view-setup.js', '/view-timeline.js', '/boot.js']);
  assert.equal(ui.document.querySelectorAll('link')[0].getAttribute('href'), '/style.css');
  assert.equal(ui.document.querySelectorAll('style').length, 0);
  assert.doesNotMatch(html, /\bon\w+\s*=|\bstyle\s*=|https?:\/\//i);
  assert.doesNotMatch(script, /\.innerHTML\s*=|insertAdjacentHTML|eval\s*\(/);
  assert.equal(ui.posts().length, 0);
});

test('four registered partners can form a two-person room and broadcasts stay within that selection', async () => {
  const agents = [...catalog, { id: 'fourth_peer', name: '第四位测试伙伴', avatar: 'F' }];
  const selected = [agents[1], agents[3]];
  const saved = snapshot({ agents: selected });
  const ui = await browser({ agents, route: (request) => request.method === 'POST' && request.path === '/api/rooms' ? saved : undefined });
  await ui.invite('claude', false);
  await ui.invite('fourth_peer');
  assert.deepEqual(ui.targets(), ['all', 'codex', 'fourth_peer']);
  assert.deepEqual(ui.members(), ['member-codex', 'member-fourth_peer']);
  await ui.choose('all');
  ui.type('只请选中的两位比较两个虚构标题。');
  await ui.submit();
  assert.equal(ui.posts().length, 1);
  assert.deepEqual(ui.posts()[0].body.participants, ['codex', 'fourth_peer']);
  assert.equal(ui.posts()[0].body.target, 'all');
  assert.equal(ui.get('new-room-setup').hidden, true);
  assert.match(ui.get('frozen-participants').textContent, /Codex、第四位测试伙伴.*固定/);
  await ui.invite('test_peer');
  await ui.poll();
  assert.deepEqual(ui.targets(), ['all', 'codex', 'fourth_peer']);
  assert.equal(ui.posts().length, 1, 'membership changes and status polling never invoke other partners');
});

test('an explicit project selects one first author and displays isolated changes as inert text', async () => {
  const projectId = '44444444-4444-4444-8444-444444444444';
  const projects = [{ id: projectId, name: '虚构样例', path: '/fictional/project', files: ['README.md', 'src/example.js'], created_at: 3 }];
  const saved = snapshot({ agents: catalog.slice(0, 2) });
  saved.room.mode = 'project_files';
  saved.project = { available: true, revision: 1, execution: 'disabled',
    source: { id: projectId, name: projects[0].name, path: projects[0].path },
    files: [{ path: 'README.md', owner: 'claude', sha256: 'a'.repeat(64), content: '改稿正文' }],
    changes: [{ path: 'README.md', status: 'modified', diff: '-old\n+<svg onload=alert(1)>' }] };
  const ui = await browser({ projects, route: (request) => request.method === 'POST' && request.path === '/api/rooms' ? saved : undefined });
  ui.get('project-picker').value = projectId;
  ui.get('project-picker').fire('change');
  ui.type('请改进指定说明文件，并互审。');
  assert.equal(ui.document.querySelectorAll('[data-target]').find((node) => node.dataset.target === 'all').disabled, true);
  assert.equal(ui.get('advanced-execution').hidden, true);
  await ui.submit();
  assert.equal(ui.posts()[0].body.mode, 'project_files');
  assert.equal(ui.posts()[0].body.project_id, projectId);
  assert.equal(ui.posts()[0].body.target, 'claude');
  assert.deepEqual(ui.posts()[0].body.participants, ['claude', 'codex']);
  assert.match(ui.get('project-status').textContent, /不会改动原项目.*暂不运行/);
  assert.match(ui.get('project-files').textContent, /\+<svg onload=alert\(1\)>/);
  assert.equal(descendants(ui.get('project-files')).some((node) => node.tagName === 'SVG'), false);
});

test('partner card names the CLI in use and why another installation was skipped', async () => {
  // Catches: a stale CLI first on PATH surfaced only as "请刷新账号状态",
  // leaving users to compare installations by hand.
  const agents = catalog.slice(0, 2);
  const partners = agents.map((agent) => ({ ...agent, adapter: agent.id, enabled: true, health: { state: 'ready', version: '0.163.0', checked_at: 5 },
    monitor: { status: 'idle' }, installation: agent.id === 'codex'
      ? { state: 'found', executable: '/Applications/Fictional.app/codex',
          skipped: [{ path: '/fictional/nvm/bin/codex', version: '0.144.4', reason: 'config_unreadable' },
                    { path: '/fictional/other/codex', version: null, reason: 'not_a_known_reason' }] }
      : { state: 'found' } }));
  const ui = await browser({ agents, partners, route(request) {
    if (request.path === '/api/status') return { agents, task_modes: ['text'] };
    if (request.path === '/api/partners') return { partners, supported_adapters: [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }] };
  } });
  await ui.click('open-partners');
  const text = ui.get('partners-list').textContent;
  assert.match(text, /已跳过 \/fictional\/nvm\/bin\/codex（0\.144\.4）：读不了本机配置文件/);
  assert.match(text, /使用中的 CLI\/Applications\/Fictional\.app\/codex/);
  assert.doesNotMatch(text, /\/fictional\/other\/codex/, 'an unknown reason is never rendered as a guess');
  assert.equal((text.match(/已跳过/g) || []).length, 1);
});

test('a new partner starts with the look of its tool until the user picks one', async () => {
  // Catches Kimi Code defaulting to Codex's green sprout, so two partners looked alike in the task list.
  const agents = catalog.slice(0, 2);
  const partners = agents.map((agent) => ({ ...agent, adapter: agent.id, enabled: true, health: { state: 'unknown' }, monitor: { status: 'idle' } }));
  const ui = await browser({ agents, partners, route(request) {
    if (request.path === '/api/status') return { agents, task_modes: ['text'] };
    if (request.path === '/api/partners' && request.method === 'GET') return { partners,
      supported_adapters: [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }, { id: 'kimi', name: 'Kimi Code' }] };
  } });
  await ui.click('open-partners');
  const checked = () => ui.get('bean-style-picker').children.find((node) => node.getAttribute('aria-checked') === 'true')?.dataset.beanChoice;
  for (const [adapter, style] of [['kimi', 'sky'], ['codex', 'sprout'], ['claude', 'peach']]) {
    ui.get('partner-adapter').value = adapter; ui.get('partner-adapter').fire('change'); await settle();
    assert.equal(checked(), style, adapter);
  }
  ui.get('bean-style-picker').children.find((node) => node.dataset.beanChoice === 'cocoa').fire('click');
  ui.get('partner-adapter').value = 'kimi'; ui.get('partner-adapter').fire('change'); await settle();
  assert.equal(checked(), 'cocoa', 'a look the user picked survives a tool change');
  assert.equal(ui.posts().length, 0);
});

test('partner registration and explicit checks do not create goals or select the new partner', async () => {
  let agents = catalog.slice(0, 2);
  let partners = agents.map((agent) => ({ ...agent, adapter: agent.id, enabled: true, health: { state: 'unknown' }, monitor: { status: 'idle' } }));
  const response = () => ({ partners, supported_adapters: [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }] });
  const ui = await browser({ agents, partners, route(request) {
    if (request.path === '/api/status') return { agents, task_modes: ['text'] };
    if (request.path === '/api/partners' && request.method === 'GET') return response();
    if (request.path === '/api/partners' && request.method === 'POST') {
      const added = { ...request.body, enabled: true, health: { state: 'unknown' }, monitor: { status: 'idle' } };
      partners = [...partners, added]; agents = [...agents, added]; return response();
    }
    if (request.path === '/api/partners/review_peer/check') {
      partners[2].health = { state: 'ready', version: 'fictional-version', auth: { method: 'fictional-login' }, checked_at: 5 };
      return response();
    }
  } });
  await ui.click('open-partners');
  assert.match(ui.get('partners-list').textContent, /尚未检查接入/);
  assert.doesNotMatch(ui.get('partners-list').textContent, /接入检查就绪/);
  ui.get('partner-id').value = 'review_peer';
  ui.get('partner-name').value = '额外的审核搭子';
  ui.get('partner-avatar').value = 'R';
  ui.get('partner-adapter').value = 'claude';
  const cocoa = ui.get('bean-style-picker').children.find((node) => node.dataset.beanChoice === 'cocoa');
  cocoa.fire('click');
  await ui.poll();
  assert.equal(cocoa.getAttribute('aria-checked'), 'true', 'status polling preserves the selected outfit');
  assert.equal(ui.get('bean-style-picker').children.find((node) => node.dataset.beanChoice === 'cocoa'), cocoa, 'polling keeps the style control node');
  ui.get('partner-form').fire('submit'); await settle();
  assert.deepEqual(ui.posts()[0].body, { id: 'review_peer', name: '额外的审核搭子', avatar: 'R', adapter: 'claude', bean_style: 'cocoa' });
  // The next partner starts from the chosen tool's own look (Claude Code: peach).
  assert.equal(ui.get('bean-style-picker').children.find((node) => node.dataset.beanChoice === 'peach').getAttribute('aria-checked'), 'true', 'successful registration restores the tool default for the next partner');
  assert.equal(ui.get('partner-id').value, '', 'successful registration clears the previous identity before another addition');
  assert.equal(ui.get('partner-name').value, '');
  assert.equal(ui.get('partner-avatar').value, '');
  assert.equal(ui.get('partner-adapter').value, 'claude');
  assert.deepEqual(ui.targets(), ['all', 'claude', 'codex']);
  assert.equal(ui.get('participant-picker').children.length, 2);
  assert.equal(ui.get('extra-session-picker').children.length, 1);
  assert.match(ui.get('additional-partners-list').textContent, /额外的审核搭子/);
  const card = ui.get('additional-partners-list').firstChild;
  assert.equal(card.firstChild.firstChild.classList.contains('bean-style-cocoa'), true, 'a partner renders its saved outfit before a model session exists');
  descendants(card).find((node) => node.tagName === 'BUTTON' && node.textContent === '检查连接').fire('click'); await settle();
  assert.equal(ui.posts().length, 2);
  assert.match(ui.get('additional-partners-list').textContent, /接入检查就绪.*CLI 版本fictional-version.*fictional-login/);
  assert.ok(ui.posts().every((request) => request.path.startsWith('/api/partners')));
});

test('all-disabled catalogs keep management available and uncertain additions are not retried', async () => {
  const ui = await browser({ agents: [], partners: [], route(request) {
    if (request.path === '/api/projects' && request.method === 'POST') throw new Error('lost project response');
  } });
  ui.type('没有选中伙伴不能启动。');
  assert.equal(ui.get('send').disabled, true);
  assert.equal(ui.get('open-projects').disabled, false);
  await ui.click('open-projects');
  assert.equal(ui.get('workspace-manager').hidden, false);
  ui.get('project-name').value = '虚构项目';
  ui.get('project-path').value = '/fictional/project';
  ui.get('project-explicit-files').checked = true;
  ui.get('project-files-input').value = 'README.md\nsrc/example.js';
  ui.get('project-form').fire('submit'); await settle();
  assert.equal(ui.posts().length, 1);
  assert.deepEqual(ui.posts()[0].body.files, ['README.md', 'src/example.js']);
  assert.match(ui.get('manager-notice').textContent, /结果尚未确认.*不会自动重试/);
  await ui.poll();
  await ui.click('manager-projects-tab');
  assert.equal(ui.posts().length, 1);
});

test('adding a single file as a project says to choose a folder instead of reporting an unknown result', async () => {
  // Catches: project_unsafe_path missing from the request labels, so the page said
  // "result not confirmed, do not add again" for a plain wrong-path mistake.
  const ui = await browser({ route(request) {
    if (request.path === '/api/projects' && request.method === 'POST') return { error: 'project_unsafe_path' };
  } });
  await ui.click('open-projects');
  ui.get('project-name').value = '虚构项目';
  ui.get('project-path').value = '/fictional/notes.txt';
  ui.get('project-form').fire('submit'); await settle();
  assert.match(ui.get('manager-notice').textContent, /项目必须是一个普通文件夹/);
  assert.doesNotMatch(ui.get('manager-notice').textContent, /尚未确认/);
});

test('global monitors describe historical attention states without pretending that an empty task list is active work', async () => {
  const partners = catalog.slice(0, 2).map((agent, index) => ({ ...agent, adapter: agent.id,
    monitor: { status: index === 0 ? 'needs_user' : 'unknown', current_tasks: [] } }));
  const original = structuredClone(partners);
  const ui = await browser({ agents: catalog.slice(0, 2), route(request) {
    if (request.path === '/api/partners') return { partners, supported_adapters: [] };
  } });
  await ui.click('open-partners');
  const cards = ui.get('partners-list').children;
  assert.match(cards[0].textContent, /豆窝内：有历史任务待查看.*现在没有在做的任务；等你处理的事在任务记录里/);
  assert.match(cards[1].textContent, /豆窝内：有任务记录状态待核对.*现在没有在做的任务；以往情况可以在任务记录里看/);
  assert.doesNotMatch(ui.get('partners-list').textContent, /豆窝内：等你处理|现在没有在处理的目标/);
  assert.deepEqual(partners, original, 'contextual wording does not rewrite monitor status or historical records');
  for (const partner of partners) partner.monitor.current_tasks.push({ room_id: roomId,
    room_goal: '实际登记的当前目标', status: 'working', current_task: '正在核对指定文本' });
  await ui.poll();
  assert.match(ui.get('partners-list').children[0].textContent, /豆窝内：等你处理/);
  assert.match(ui.get('partners-list').children[1].textContent, /豆窝内：状态待确认/);
  assert.match(ui.get('partners-list').textContent, /实际登记的当前目标.*正在核对指定文本/);
  assert.doesNotMatch(ui.get('partners-list').textContent, /有历史任务待查看|核对历史状态/);
  for (const [kind, expected] of [
    ['team_plan', '讨论目标与规划分工'], ['team_coordinate', '确认分工并认领任务'],
    ['team_work', '推进已认领的子任务'], ['team_review', '独立审核伙伴的交付'],
    ['team_help', '回答伙伴的具体求助'], ['team_user', '处理你的补充说明'],
    ['team_future', '团队任务阶段待核对'],
  ]) {
    partners[0].monitor.current_tasks[0].kind = kind;
    partners[0].monitor.current_tasks[0].current_task = 'Work on your claimed task. Task private guessed title.';
    await ui.poll();
    const text = ui.get('partners-list').children[0].textContent;
    assert.ok(text.includes(expected), kind);
    assert.match(text, /实际登记的当前目标.*正在处理/);
    assert.doesNotMatch(text, /Work on your claimed task|private guessed title/);
  }
  partners[0].monitor.current_tasks[0].kind = 'task';
  partners[0].monitor.current_tasks[0].current_task = '旧模式原始目标说明';
  await ui.poll();
  assert.match(ui.get('partners-list').children[0].textContent, /旧模式原始目标说明/);
  assert.equal(ui.posts().length, 0);
});

test('monitors distinguish an inconclusive check and preserve expanded usage and focused input across polling', async () => {
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true,
    health: { state: 'unknown', checked_at: agent.id === 'claude' ? 5 : null, reason: agent.id === 'claude' ? 'check_timeout' : 'not_checked' },
    monitor: { status: 'idle', session_count: 0, latest_usage: null } }));
  const ui = await browser({ agents: catalog.slice(0, 2), partners });
  await ui.click('open-partners');
  let card = ui.get('partners-list').children[0];
  assert.match(card.textContent, /可选入新任务/, 'an unknown installation remains selectable when an older backend includes it in its catalog');
  assert.match(card.textContent, /接入尚未确认.*接入检查超时/);
  assert.doesNotMatch(card.textContent, /尚未检查接入/);
  assert.match(ui.get('partners-list').children[1].textContent, /尚未检查接入/);
  let usage = card.children.find((node) => node.dataset.detailKind === 'usage');
  usage.open = true;
  ui.get('partner-name').value = '正在填写的搭子';
  ui.get('partner-name').focus();
  await ui.poll();
  assert.equal(ui.get('partners-list').children[0], card, 'unchanged partner data preserves the complete card node');
  assert.equal(usage.open, true);
  assert.equal(ui.document.activeElement, ui.get('partner-name'));

  partners[0].monitor.session_count = 1;
  await ui.poll();
  card = ui.get('partners-list').children[0];
  usage = card.children.find((node) => node.dataset.detailKind === 'usage');
  assert.equal(usage.open, true, 'changed monitor data preserves expansion by partner ID');
  assert.equal(ui.document.activeElement, ui.get('partner-name'), 'monitor refresh does not take focus from the add form');
  assert.equal(ui.get('partner-name').value, '正在填写的搭子');

  const check = descendants(card).find((node) => node.dataset.focusKey === 'check');
  check.focus();
  partners[0].monitor.session_count = 2;
  await ui.poll();
  assert.notEqual(ui.document.activeElement, check);
  assert.equal(ui.document.activeElement.dataset.focusKey, 'check', 'a replaced card restores its focused control');
  assert.equal(ui.posts().length, 0);
});

test('style cards support roving keyboard selection and current appearance without changing room identity', async () => {
  const agents = [{ ...catalog[0], bean_style: 'sky' }, { ...catalog[1], bean_style: 'cocoa' }];
  const saved = snapshot({ agents: [{ ...catalog[0], bean_style: 'lavender' }, catalog[1]],
    events: [message(1, 'claude', '只有历史房间成员的装扮应出现在这里。')] });
  const ui = await browser({ agents, rooms: [saved.room], saved });
  const portrait = ui.get('events').firstChild.firstChild;
  assert.equal(portrait.classList.contains('bean-style-sky'), true);
  assert.equal(ui.get('member-claude').firstChild.firstChild.classList.contains('bean-style-sky'), true);
  assert.equal(ui.get('member-codex').firstChild.firstChild.classList.contains('bean-style-cocoa'), true);
  await ui.poll();
  assert.equal(ui.get('events').firstChild.firstChild, portrait);
  assert.equal(portrait.classList.contains('bean-style-sky'), true, 'appearance stays consistent across polling');
  await ui.click('new-room');
  assert.equal(ui.get('empty-avatars').firstChild.firstChild.classList.contains('bean-style-sky'), true);
  assert.equal(ui.get('member-codex').firstChild.firstChild.classList.contains('bean-style-cocoa'), true);

  await ui.click('open-partners');
  const picker = ui.get('bean-style-picker');
  assert.equal(picker.children.length, 10);
  picker.firstChild.fire('keydown', { key: 'ArrowRight' });
  assert.equal(picker.children[1].getAttribute('aria-checked'), 'true');
  assert.equal(ui.document.activeElement, picker.children[1]);
  assert.equal(picker.children[0].getAttribute('tabindex'), '-1');
  picker.children[1].fire('keydown', { key: 'End' });
  assert.equal(picker.children[9].getAttribute('aria-checked'), 'true');
  await ui.poll();
  assert.equal(picker.children[9].getAttribute('aria-checked'), 'true');
  assert.equal(ui.document.activeElement, picker.children[9]);
  assert.equal(ui.posts().length, 0, 'appearance selection and catalog reads never invoke a model');
});

test('a first-time user can send a goal without choosing project, routing or execution options', async () => {
  const saved = snapshot({ agents: catalog.slice(0, 2) });
  const ui = await browser({ route: (request) => request.path === '/api/rooms' && request.method === 'POST' ? saved : undefined });
  ui.type('请一起改进这段虚构公告的表达。');
  assert.equal(ui.get('send').disabled, false);
  await ui.submit();
  assert.equal(ui.posts().length, 1);
  assert.equal(ui.posts()[0].body.target, 'claude', 'the first selected partner starts, with peer collaboration managed by the service');
  assert.deepEqual(ui.posts()[0].body.participants, ['claude', 'codex']);
  assert.equal(ui.posts()[0].body.project_id, undefined);
  assert.equal(ui.posts()[0].body.mode, undefined);
  assert.equal(ui.posts()[0].body.collaboration, true, 'only newly created goals opt into the bounded team workflow');
});

function teamCollaboration({ state = 'in_progress', tasks = [], reason = '伙伴正在认领任务。', nextAction = '等待伙伴处理。' } = {}) {
  return { mode: 'team', phase: state === 'in_progress' ? 'working' : 'stopped', summary: '共同拆分并交叉检查。', tasks,
    outcome: { state, reason, next_action: nextAction,
      validation: { scope: 'not_run', summary: '尚未执行代码或测试。', revision: null }, artifacts: [], remaining: [] } };
}

function withCurrentRun(saved, options = {}) {
  saved.current_run = { id: '44444444-4444-4444-8444-444444444444', number: 1, goal: saved.room.goal,
    status: 'ready_for_user', revision: null, digest: 'd'.repeat(64), can_accept: true, can_continue: true,
    can_retry: false, accepted_at: null, budget: { used: 5, limit: 32, deadline: 1000 }, ...options };
  saved.runs = [{ ...saved.current_run }];
  saved.conversation_budget = { used: 5, run_count: 1, run_limit: 8, max_calls: 256 };
  return saved;
}

test('current-run acceptance records one exact delivery and the next goal starts a new run in the same conversation', async () => {
  const saved = withCurrentRun(snapshot({ agents: catalog.slice(0, 2), status: 'ready_for_user' }));
  saved.collaboration = teamCollaboration({ state: 'ready_for_user' });
  saved.members[0].session_id = '55555555-5555-4555-8555-555555555555';
  const initialRun = structuredClone(saved.current_run);
  const ui = await browser({ rooms: [saved.room], saved, route(request) {
    if (request.path === `/api/rooms/${roomId}/accept`) {
      saved.current_run.accepted_at = 10; saved.current_run.can_accept = false;
      saved.collaboration.outcome.state = 'accepted';
      saved.runs[0] = { ...saved.current_run };
      return saved;
    }
    if (request.path === `/api/rooms/${roomId}/runs`) {
      saved.current_run = { ...saved.current_run, id: '66666666-6666-4666-8666-666666666666', number: 2,
        goal: request.body.text, status: 'running', can_accept: false, can_continue: false, accepted_at: null };
      saved.collaboration = teamCollaboration(); saved.room.status = 'running';
      saved.runs.push({ ...saved.current_run }); return saved;
    }
  } });
  ui.type('在刚才交付上增加平局说明');
  assert.equal(ui.get('send-label').textContent, '开始下一轮');
  assert.equal(ui.get('send').disabled, false);
  await ui.click('accept-run');
  assert.equal(ui.posts().length, 1);
  assert.equal(ui.posts()[0].path, `/api/rooms/${roomId}/accept`);
  assert.deepEqual(Object.keys(ui.posts()[0].body).sort(), ['digest', 'request_id', 'revision', 'run_id']);
  assert.equal(ui.posts()[0].body.run_id, initialRun.id);
  assert.equal(ui.posts()[0].body.digest, initialRun.digest);
  assert.equal(ui.posts()[0].body.revision, null);
  assert.equal(ui.get('message').value, '在刚才交付上增加平局说明', 'acceptance preserves an unsent next-run draft');
  assert.equal(ui.get('room-status').textContent, '本轮已接受');
  assert.equal(ui.get('collaboration-outcome').classList.contains('outcome-accepted'), true);
  assert.equal(ui.document.getElementById('accept-run'), null);
  await ui.submit();
  assert.equal(ui.posts().length, 2);
  assert.equal(ui.posts()[1].path, `/api/rooms/${roomId}/runs`);
  assert.equal(ui.posts()[1].body.run_id, initialRun.id);
  assert.equal(ui.posts()[1].body.retry, false);
  assert.equal(ui.posts()[1].body.text, '在刚才交付上增加平局说明');
  assert.match(ui.get('room-subtitle').textContent, /第 2 轮/);
  assert.match(ui.get('runs').textContent, /第 1 轮 · 你已接受.*第 2 轮/);
  assert.match(ui.get('session-claude').textContent, /55555555/);
  assert.equal(ui.get('run-count').textContent, '2');
});

test('opening without a room hash stays on welcome and history remains an explicit choice even with active work', async () => {
  const saved = snapshot({ events: [message(1, 'codex', '旧测试回复')] });
  const ui = await browser({ rooms: [saved.room], saved, selectSaved: false });
  assert.equal(ui.get('room-title').textContent, '新任务');
  assert.equal(ui.get('room-status').hidden, true);
  assert.equal(ui.get('rooms').children.length, 1);
  assert.doesNotMatch(ui.get('events').textContent, /旧测试回复/);
  ui.get('rooms').firstChild.fire('click'); await settle();
  assert.match(ui.get('events').textContent, /旧测试回复/);
  await ui.poll();
  assert.equal(ui.get('room-status').hidden, false, 'polling preserves the user-selected historical room');
  saved.room.status = 'running';
  const active = await browser({ rooms: [saved.room], saved, selectSaved: false });
  assert.equal(active.get('room-title').textContent, '新任务');
  assert.doesNotMatch(active.get('events').textContent, /旧测试回复/);
  assert.equal(active.requests.some((request) => request.path === `/api/rooms/${roomId}`), false, 'no latest-running-room guess');
  assert.equal(active.posts().length, 0);
});

test('only an explicit permitted retry creates a fresh attempt and cleanup rejection never starts another run', async () => {
  const saved = withCurrentRun(snapshot({ status: 'needs_user' }), { status: 'stopped', can_accept: false, can_continue: false, can_retry: true });
  saved.team = { phase: 'stopped', tasks: [] };
  saved.collaboration = teamCollaboration({ state: 'stopped' });
  const ui = await browser({ rooms: [saved.room], saved, route(request) {
    if (request.path.endsWith('/runs') && request.method === 'POST') return { error: 'team_cleanup_unconfirmed' };
  } });
  assert.equal(ui.get('message').disabled, true);
  assert.match(ui.get('retry-run').textContent, /新的尝试.*新会话/);
  assert.equal(ui.posts().length, 0);
  await ui.click('retry-run');
  assert.equal(ui.posts().length, 1);
  assert.equal(ui.posts()[0].body.retry, true);
  assert.equal(ui.posts()[0].body.text, saved.current_run.goal);
  assert.match(ui.get('action-error').textContent, /清理尚未确认/);
  await ui.poll();
  assert.equal(ui.posts().length, 1);
  saved.current_run.can_retry = false;
  saved.team.phase = 'unknown'; saved.collaboration.outcome.state = 'unknown';
  await ui.poll();
  assert.equal(ui.document.getElementById('retry-run'), null);
  assert.equal(ui.get('message').disabled, true);
});

test('an uncertain next-run request is retained with its exact run binding across reload and is never replayed by reads', async () => {
  const storage = new Map();
  const saved = withCurrentRun(snapshot({ status: 'ready_for_user' }));
  saved.collaboration = teamCollaboration({ state: 'ready_for_user' });
  const route = (request) => {
    if (request.path.endsWith('/runs') && request.method === 'POST') throw new Error('fictional lost next-run response');
  };
  const ui = await browser({ rooms: [saved.room], saved, storage, route });
  ui.type('下一轮的完整范围'); await ui.submit();
  assert.equal(ui.posts().length, 1);
  const original = ui.posts()[0].body;
  await ui.poll();
  const reloaded = await browser({ rooms: [saved.room], saved, storage, route });
  assert.equal(reloaded.posts().length, 0);
  assert.match(reloaded.get('pending-description').textContent, /下一轮任务/);
  await reloaded.click('retry-request');
  assert.equal(reloaded.posts().length, 1);
  assert.deepEqual(reloaded.posts()[0].body, original);
  assert.equal(reloaded.posts()[0].path, `/api/rooms/${roomId}/runs`);
});

test('fork is capability gated and only an explicit new goal submission opens a different room', async () => {
  const saved = withCurrentRun(snapshot({ status: 'needs_user' }), { status: 'unknown', revision: 27,
    can_accept: false, can_continue: false, can_retry: false, can_fork: false });
  saved.team = { phase: 'unknown', tasks: [] }; saved.collaboration = teamCollaboration({ state: 'unknown' });
  const next = structuredClone(saved); next.room.id = '77777777-7777-4777-8777-777777777777';
  next.room.goal = '只修未完成的报告边界'; next.current_run.can_fork = false;
  const ui = await browser({ rooms: [saved.room], saved, route(request) {
    if (request.path.endsWith('/fork')) return next; // Even an unknown new snapshot must not be replayed.
  } });
  assert.equal(ui.document.getElementById('fork-room'), null);
  saved.current_run.can_fork = true; await ui.poll();
  assert.equal(ui.get('message').disabled, true);
  ui.type('只修未完成的报告边界');
  await ui.click('fork-room');
  assert.equal(ui.posts().length, 0, 'opening the goal editor never invokes the backend');
  assert.equal(ui.get('message').value, '只修未完成的报告边界');
  assert.equal(ui.get('send-label').textContent, '开始新任务');
  assert.equal(ui.get('message').disabled, false);
  assert.match(ui.get('composer-note').textContent, /已保存文件.*新会话.*未被验收/);
  await ui.submit();
  assert.equal(ui.posts().length, 1);
  assert.equal(ui.posts()[0].path, `/api/rooms/${roomId}/fork`);
  assert.deepEqual(Object.keys(ui.posts()[0].body).sort(), ['digest', 'request_id', 'revision', 'run_id', 'target', 'text']);
  assert.equal(ui.posts()[0].body.revision, 27);
  assert.equal(ui.posts()[0].body.digest, saved.current_run.digest);
  assert.equal(ui.posts()[0].body.text, '只修未完成的报告边界');
  assert.equal(ui.get('rooms').children.length, 2, 'the old history card is preserved');
  assert.equal(ui.get('room-title').title, next.room.goal);
  assert.equal(ui.get('message').disabled, true, 'unknown new-room state does not permit automatic continuation');
  await ui.poll(); assert.equal(ui.posts().length, 1);
});

test('fork cancellation and known rejection preserve the goal without dispatching another request', async () => {
  const saved = withCurrentRun(snapshot({ status: 'needs_user' }), { status: 'unknown', revision: 27,
    can_accept: false, can_continue: false, can_retry: false, can_fork: true });
  saved.team = { phase: 'unknown', tasks: [] }; saved.collaboration = teamCollaboration({ state: 'unknown' });
  const ui = await browser({ rooms: [saved.room], saved, route: (request) => request.path.endsWith('/fork') ? { error: 'fork_source_unavailable' } : undefined });
  await ui.click('fork-room');
  assert.equal(ui.get('message').value, saved.current_run.goal, 'the existing goal is editable, never immediately sent');
  await ui.click('fork-room');
  assert.equal(ui.get('message').value, ''); assert.equal(ui.get('message').disabled, true);
  assert.equal(ui.posts().length, 0);
  await ui.click('fork-room'); ui.type('保留这个新的恢复目标'); await ui.submit();
  assert.equal(ui.get('message').value, '保留这个新的恢复目标');
  assert.match(ui.get('action-error').textContent, /暂不可安全复用/);
  assert.equal(ui.get('pending-request').hidden, true);
  await ui.poll(); assert.equal(ui.posts().length, 1);
});

test('an uncertain fork retains its exact source binding across reload without replaying it', async () => {
  const storage = new Map();
  const saved = withCurrentRun(snapshot({ status: 'needs_user' }), { status: 'unknown', revision: 27,
    can_accept: false, can_continue: false, can_retry: false, can_fork: true });
  saved.team = { phase: 'unknown', tasks: [] }; saved.collaboration = teamCollaboration({ state: 'unknown' });
  const route = (request) => { if (request.path.endsWith('/fork')) throw new Error('fictional lost response'); };
  const ui = await browser({ rooms: [saved.room], saved, storage, route });
  await ui.click('fork-room'); ui.type('不要丢失的恢复范围'); await ui.submit();
  const original = ui.posts()[0];
  assert.equal(ui.get('message').value, '不要丢失的恢复范围');
  await ui.poll(); assert.equal(ui.posts().length, 1);
  const reloaded = await browser({ rooms: [saved.room], saved, storage, route });
  assert.equal(reloaded.posts().length, 0);
  assert.equal(reloaded.get('message').value, original.body.text);
  assert.match(reloaded.get('pending-description').textContent, /另开的新任务.*可能已经生效/);
  await reloaded.click('retry-request');
  assert.equal(reloaded.posts().length, 1);
  assert.deepEqual(reloaded.posts()[0].body, original.body);
  assert.equal(reloaded.posts()[0].path, original.path);
});

test('file downloads verify the requested revision, snapshot and archive bytes before creating a local download', async () => {
  const raw = Buffer.from('PK\x03\x04fictional zip bytes');
  const sha = Buffer.from(await webcrypto.subtle.digest('SHA-256', raw)).toString('hex');
  const saved = snapshot();
  saved.project = { available: true, revision: 2, snapshot_sha256: 'a'.repeat(64), files: [{ path: 'score.py', content: 'pass', sha256: 'b'.repeat(64) }] };
  let response = { filename: 'beanest-revision-2.zip', content_type: 'application/zip', content_base64: raw.toString('base64'),
    revision: 2, snapshot_sha256: saved.project.snapshot_sha256, sha256: sha, files_count: 1 };
  const ui = await browser({ rooms: [saved.room], saved, route: (request) => request.path.endsWith('/export?revision=2') ? response : undefined });
  await ui.click('download-artifacts');
  assert.equal(ui.document.downloads.length, 1);
  assert.equal(ui.document.downloads[0].filename, 'beanest-revision-2.zip');
  assert.equal(ui.document.downloads[0].blob.type, 'application/zip');
  assert.deepEqual(Buffer.from(await ui.document.downloads[0].blob.arrayBuffer()), raw);
  assert.equal(ui.document.blobs.size, 0, 'temporary object URL is revoked after the download action');
  assert.equal(ui.posts().length, 0);
  response = { ...response, revision: 1 };
  await ui.click('download-artifacts');
  assert.equal(ui.document.downloads.length, 1);
  assert.match(ui.get('export-note').textContent, /版本不一致/);
  response = { ...response, revision: 2, sha256: 'c'.repeat(64) };
  await ui.click('download-artifacts');
  assert.equal(ui.document.downloads.length, 1);
  assert.match(ui.get('export-note').textContent, /完整性核对失败/);
});

test('connection templates can be saved uninstalled, configured with literal arguments and remain excluded until ready', async () => {
  const templates = [{ id: 'kimi', adapter: 'kimi', name: 'Kimi', protocol: 'beanest-json-v1' },
    { id: 'deepseek', adapter: 'deepseek', name: 'DeepSeek', protocol: 'beanest-json-v1' }];
  let partners = catalog.slice(0, 2);
  const response = () => ({ partners, supported_adapters: [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }],
    connection_templates: templates, connection_protocol: { id: 'beanest-json-v1' } });
  const ui = await browser({ agents: catalog.slice(0, 2), route(request) {
    if (request.path === '/api/partners/discover') return response();
    if (request.path === '/api/partners' && request.method === 'GET') return response();
    if (request.path === '/api/partners' && request.method === 'POST') {
      partners = [...partners, { ...request.body, connection_state: 'not_configured', installation: { state: 'missing' }, selectable: false,
        health: { state: 'unknown', reason: 'connection_not_configured' } }];
      return response();
    }
    if (request.path.endsWith('/connection')) {
      partners.at(-1).connection = request.body.connection;
      partners.at(-1).connection_state = 'configured';
      return response();
    }
  } });
  assert.equal(ui.posts().filter((request) => request.path === '/api/partners/discover').length, 1);
  await ui.poll(); await ui.click('open-partners');
  assert.equal(ui.posts().filter((request) => request.path === '/api/partners/discover').length, 1, 'polling and management reads do not rescan');
  assert.match(ui.get('partner-adapter').textContent, /Kimi.*DeepSeek/);
  ui.get('partner-adapter').value = 'kimi'; ui.get('partner-adapter').fire('change');
  assert.equal(ui.get('partner-connection').hidden, false);
  ui.get('partner-form').fire('submit'); await settle();
  const addition = ui.posts().find((request) => request.path === '/api/partners');
  assert.equal(addition.body.adapter, 'kimi');
  assert.equal(addition.body.connection, null);
  assert.match(ui.get('partners-list').textContent, /Kimi 工具.*暂不能加入新任务/);
  assert.match(ui.get('partners-list').textContent, /工具模板已保存 · 待配置/);
  assert.deepEqual(ui.targets(), ['all', 'claude', 'codex']);
  const configure = descendants(ui.get('partners-list')).find((node) => node.tagName === 'BUTTON' && node.textContent === '配置本机连接器');
  configure.fire('click'); await settle();
  assert.equal(ui.get('partner-name').disabled, true, 'first configuration retains the saved identity');
  ui.get('connection-executable').value = '/fictional/missing-connector';
  ui.get('connection-args').value = '--json\nliteral value with spaces';
  ui.get('connection-billing').value = 'local';
  ui.get('partner-form').fire('submit'); await settle();
  const configured = ui.posts().find((request) => request.path.endsWith('/connection'));
  assert.equal(configured.path, `/api/partners/${addition.body.id}/connection`);
  assert.deepEqual(configured.body.connection, { protocol: 'beanest-json-v1', executable: '/fictional/missing-connector', args: ['--json', 'literal value with spaces'], billing: 'local' });
  assert.match(ui.get('partners-list').textContent, /连接配置已保存，需安装并通过检查/);
  assert.deepEqual(ui.targets(), ['all', 'claude', 'codex']);
  assert.equal(ui.get('events').textContent, '');
  assert.equal(ui.posts().some((request) => request.path === '/api/rooms'), false);
});

test('native opening notices refresh discovery once, coalesce duplicates and preserve drafts, choices and historical identity', async () => {
  let notifyOpened;
  let clockNow = 10000;
  let discoveries = 0;
  const waiting = deferred();
  const agents = catalog.slice(0, 2);
  const saved = snapshot({ agents });
  saved.members[0].session_id = '55555555-5555-4555-8555-555555555555';
  const originalMembers = structuredClone(saved.members);
  const response = () => ({ partners: agents, supported_adapters: agents,
    connection_templates: [], connection_protocol: { id: 'beanest-json-v1' } });
  const ui = await browser({ agents, rooms: [saved.room], saved, now: () => clockNow,
    desktopBridge: { onOpened(callback) { notifyOpened = callback; return () => {}; } }, route(request) {
      if (request.path === '/api/partners' && request.method === 'GET') return response();
      if (request.path === '/api/partners/discover') {
        discoveries += 1;
        return discoveries === 2 ? waiting.promise : response();
      }
    } });
  assert.equal(typeof notifyOpened, 'function');
  assert.equal(discoveries, 1);
  await ui.click('new-room'); await ui.invite('codex', false);
  ui.type('尚未发送的完整草稿');
  clockNow += 1001;
  notifyOpened(); notifyOpened(); await settle();
  assert.equal(discoveries, 2, 'parallel notifications share the in-flight discovery');
  await ui.poll();
  assert.equal(discoveries, 2, 'ordinary polling does not scan');
  waiting.resolve(response()); await settle();
  clockNow += 100;
  notifyOpened(); await settle();
  assert.equal(discoveries, 2, 'nearby notifications after completion are coalesced');
  assert.equal(ui.get('message').value, '尚未发送的完整草稿');
  assert.deepEqual(ui.targets(), ['all', 'claude']);
  assert.equal(ui.document.querySelectorAll('[data-participant]').find((input) => input.dataset.participant === 'codex').checked, false);
  clockNow += 1001;
  await ui.click('refresh');
  assert.equal(discoveries, 2, 'the chat refresh only reads task status');
  await ui.click('rediscover-partners');
  assert.equal(discoveries, 3, 'the named management action can update discovery in a Web page too');
  assert.equal(ui.get('message').value, '尚未发送的完整草稿');
  ui.get('rooms').firstChild.fire('click'); await settle();
  assert.match(ui.get('session-claude').textContent, /55555555/);
  assert.deepEqual(saved.members, originalMembers);
  assert.equal(ui.posts().every((request) => request.path === '/api/partners/discover'), true);
});

test('explicit tool rediscovery is single-flight, reports success and failure and never dispatches a goal', async () => {
  const waiting = deferred();
  let discoveries = 0;
  const agents = catalog.slice(0, 2);
  const response = () => ({ partners: agents, supported_adapters: agents,
    connection_protocol: { id: 'beanest-json-v1' }, discovery: { state: 'completed', observed_at: 42,
      scope: 'supported_cli_locations', tools: [{ adapter: 'claude', name: 'Claude Code', installed: true }] } });
  const ui = await browser({ agents, now: () => 10000, route(request) {
    if (request.path === '/api/partners' && request.method === 'GET') return response();
    if (request.path === '/api/partners/discover') {
      discoveries += 1;
      if (discoveries === 2) return waiting.promise;
      if (discoveries === 3) throw new Error('fictional connection interruption');
      return response();
    }
  } });
  await ui.click('open-partners');
  assert.equal(ui.get('partner-discovery').hidden, false);
  assert.equal(ui.get('rediscover-partners').textContent, '重新检测');
  await ui.invite('codex', false);
  ui.type('保留尚未发送的目标');
  await ui.click('rediscover-partners');
  assert.equal(discoveries, 2, 'an explicit action is usable immediately after startup');
  assert.equal(ui.get('rediscover-partners').disabled, true);
  assert.match(ui.get('discovery-feedback').textContent, /正在检查.*不会启动模型/);
  await ui.click('rediscover-partners'); await ui.click('refresh'); await ui.poll();
  assert.equal(discoveries, 2, 'double click, chat refresh and polling cannot add parallel scans');
  waiting.resolve(response()); await settle();
  assert.equal(ui.get('rediscover-partners').disabled, false);
  assert.match(ui.get('discovery-feedback').textContent, /安装观察已更新.*没有启动模型/);
  await ui.click('rediscover-partners');
  assert.match(ui.get('discovery-feedback').textContent, /检测未完成.*设置保持不变/);
  assert.equal(ui.get('rediscover-partners').disabled, false);
  assert.equal(ui.get('message').value, '保留尚未发送的目标');
  assert.deepEqual(ui.targets(), ['all', 'claude']);
  assert.equal(ui.posts().every((request) => request.path === '/api/partners/discover'), true);
  assert.equal(ui.get('events').textContent, '');
});

test('consecutive Kimi, DeepSeek and compatible CLI additions generate valid distinct identities after each successful reset', async () => {
  let partners = catalog.slice(0, 2);
  const templates = [{ adapter: 'kimi', name: 'Kimi' }, { adapter: 'deepseek', name: 'DeepSeek' }, { adapter: 'local_json', name: '兼容本机 CLI' }]
    .map((template) => ({ ...template, id: template.adapter, protocol: 'beanest-json-v1' }));
  const response = () => ({ partners, supported_adapters: [], connection_templates: templates });
  const ui = await browser({ agents: catalog.slice(0, 2), route(request) {
    if (request.path === '/api/partners' && request.method === 'GET') return response();
    if (request.path === '/api/partners' && request.method === 'POST') {
      partners = [...partners, { ...request.body, connection_state: 'not_configured', connection: null, selectable: false }];
      return response();
    }
  } });
  await ui.click('open-partners');
  for (const template of templates) {
    ui.get('partner-adapter').value = template.adapter; ui.get('partner-adapter').fire('change');
    ui.get('partner-name').value = template.name;
    assert.equal(ui.get('partner-id').value, ''); assert.equal(ui.get('partner-avatar').value, '');
    ui.get('partner-form').fire('submit'); await settle();
    const request = ui.posts().at(-1);
    assert.equal(request.body.adapter, template.adapter);
    assert.match(request.body.id, /^[a-z][a-z0-9_-]{0,31}$/);
    assert.ok(request.body.id.length <= 32);
    assert.ok([...request.body.avatar].length >= 1 && [...request.body.avatar].length <= 2);
    assert.equal(ui.get('partner-id').value, '', 'success resets the form without making the next identity invalid');
    assert.doesNotMatch(ui.get('manager-notice').textContent, /请填写有效标识/);
  }
  assert.equal(ui.posts().length, 3);
  assert.equal(new Set(ui.posts().map((request) => request.body.id)).size, 3);
  assert.deepEqual(ui.targets(), ['all', 'claude', 'codex']);
});

test('text artifact mode is advertised explicitly and SVG previews remain verified inert images with a source fallback', async () => {
  const source = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>';
  const raw = Buffer.from(source);
  const sha = Buffer.from(await webcrypto.subtle.digest('SHA-256', raw)).toString('hex');
  const saved = snapshot(); saved.room.mode = 'text_artifacts';
  saved.project = { available: true, revision: 2, snapshot_sha256: 'a'.repeat(64), execution: 'disabled',
    files: [{ path: 'diagram.svg', owner: 'claude', content: source, sha256: sha }] };
  let preview = { content_type: 'image/svg+xml', content_base64: raw.toString('base64'), sha256: sha, source_sha256: sha };
  const ui = await browser({ taskModes: ['text', 'text_artifacts'], route(request) {
    if (request.path === '/api/rooms' && request.method === 'POST') return saved;
    if (request.path.endsWith('/preview?revision=2&path=diagram.svg')) return preview;
  } });
  assert.equal(ui.get('text-artifacts-mode').hidden, false);
  ui.get('task-mode').value = 'text_artifacts'; ui.get('task-mode').fire('change');
  assert.match(ui.get('scope-description').textContent, /仅安全预览静态 SVG.*不是图片模型接入/);
  ui.type('请共同绘制一个静态矢量图'); await ui.submit();
  assert.equal(ui.posts()[0].body.mode, 'text_artifacts');
  assert.equal(ui.posts()[0].body.collaboration, true);
  const file = ui.get('project-files').firstChild;
  const button = descendants(file).find((node) => node.tagName === 'BUTTON' && node.textContent === '预览矢量图');
  button.fire('click');
  await waitUntil(() => !button.disabled, 'SVG preview must finish its async SHA verification');
  const img = descendants(file).find((node) => node.tagName === 'IMG');
  assert.ok(img);
  assert.equal(img.src, `data:image/svg+xml;base64,${raw.toString('base64')}`);
  assert.equal(descendants(file).some((node) => ['SVG', 'IFRAME', 'SCRIPT'].includes(node.tagName)), false);
  preview = { ...preview, source_sha256: 'b'.repeat(64) };
  button.fire('click');
  await waitUntil(() => !button.disabled, 'mismatched SVG preview must finish in the safe source fallback');
  assert.equal(descendants(file).some((node) => node.tagName === 'IMG'), false);
  assert.match(file.textContent, /暂不能安全预览，可查看源码.*已交付文件仍保留/);
  assert.match(file.textContent, /<svg xmlns=/);
  assert.equal(ui.posts().length, 1, 'preview reads do not dispatch further model work');
});

test('team assignments and waiting dependencies stay in the conversation without dispatching work', async () => {
  const saved = snapshot({ status: 'running', agents: catalog.slice(0, 2) });
  saved.collaboration = teamCollaboration({ tasks: [
    { id: 'draft', title: '实现计分规则', owner: 'claude', status: 'working', waiting_for: '', result: '' },
    { id: 'review', title: '检查边界条件', owner: 'codex', status: 'waiting_peer', waiting_for: 'Claude Code 提交版本 2', result: '' },
    { id: 'docs', title: '补充使用说明', owner: null, status: 'open', waiting_for: '', result: '' },
  ] });
  const ui = await browser({ rooms: [saved.room], saved });
  const plan = ui.get('collaboration-plan');
  assert.equal(plan.hidden, false);
  assert.match(plan.textContent, /任务分工.*实现计分规则正在处理负责人：Claude Code/);
  assert.match(plan.textContent, /检查边界条件等待伙伴负责人：Codex正在等待：Claude Code 提交版本 2/);
  assert.match(plan.textContent, /补充使用说明待认领负责人：等待伙伴认领/);
  const child = plan.firstChild;
  ui.get('timeline').scrollTop = 10;
  ui.get('message').focus();
  await ui.poll();
  assert.equal(plan.firstChild, child, 'unchanged projections preserve the card nodes');
  assert.equal(ui.get('timeline').scrollTop, 10, 'polls do not move a reader away from an earlier message');
  assert.equal(ui.document.activeElement, ui.get('message'));
  assert.equal(ui.posts().length, 0);
});

test('active collaboration leaves status to the composer and reveals delivery details only when attention is needed', async () => {
  const saved = snapshot({ status: 'running', events: [message(1, 'codex', '已找到边界问题，正在和伙伴确认。')] });
  saved.collaboration = teamCollaboration({ reason: '伙伴正在按共享清单推进。' });
  saved.collaboration.outcome.validation = { scope: 'static_review', summary: '完整检查说明', revision: 2 };
  saved.collaboration.outcome.artifacts = [{ path: 'score.py', revision: 2, sha256: 'a'.repeat(64) }];
  saved.collaboration.outcome.remaining = ['任务一', '任务二', '任务三', '任务四', '任务五'];
  saved.collaboration.outcome.next_action = '下一步详细说明';
  const ui = await browser({ rooms: [saved.room], saved });
  const result = ui.get('collaboration-outcome');
  assert.equal(result.classList.contains('compact-outcome'), false);
  assert.equal(result.hidden, true, 'without readable earlier files there is no outcome block below active chat');
  assert.equal(result.children.length, 0);
  assert.equal(result.textContent, '');
  assert.doesNotMatch(result.textContent, /检查范围|score.py|任务一|下一步详细说明/);
  assert.match(ui.get('events').textContent, /已找到边界问题/);
  for (const state of ['needs_user', 'stopped', 'unknown', 'ready_for_user']) {
    saved.collaboration.outcome.state = state;
    await ui.poll();
    assert.equal(result.classList.contains('compact-outcome'), false, state);
    assert.match(result.textContent, /检查范围.*文件版本 2/);
    assert.match(result.textContent, /score.py.*版本 2/);
    assert.match(result.textContent, /尚未完成任务一任务二任务三任务四任务五/);
    assert.match(result.textContent, state === 'ready_for_user' ? /可在下方继续提出要求/ : /下一步：下一步详细说明/);
  }
  saved.collaboration.mode = 'legacy';
  saved.collaboration.outcome.state = 'in_progress';
  await ui.poll();
  assert.equal(result.hidden, true, 'legacy active records also avoid a large outcome block; their original history remains in chat');
  assert.equal(ui.posts().length, 0);
});

test('task boards start folded, preserve an intentional expansion during updates and reset when changing rooms', async () => {
  const saved = snapshot({ status: 'running' });
  saved.collaboration = teamCollaboration({ tasks: [
    { id: 'draft', title: '实现计分规则', owner: 'claude', status: 'working', result: '' },
    { id: 'review', title: '检查边界条件', owner: 'codex', status: 'accepted', result: '已独立检查。' },
  ] });
  const second = structuredClone(saved);
  second.room.id = '33333333-3333-4333-8333-333333333333';
  second.room.goal = '另一个房间';
  const ui = await browser({ rooms: [saved.room, second.room], saved, route: (request) => request.path === `/api/rooms/${second.room.id}` ? second : undefined });
  const plan = ui.get('collaboration-plan');
  assert.equal(plan.getAttribute('aria-label'), '任务分工');
  assert.equal(plan.firstChild.tagName, 'DETAILS');
  assert.equal(plan.firstChild.open, false);
  assert.equal(plan.firstChild.firstChild.tagName, 'SUMMARY');
  assert.equal(plan.firstChild.firstChild.textContent, '任务分工 · 1/2 已完成，伙伴看过');
  assert.match(plan.textContent, /实现计分规则.*负责人：Claude Code.*检查边界条件.*负责人：Codex/);
  plan.firstChild.open = true;
  saved.collaboration.tasks[0].status = 'accepted';
  await ui.poll();
  assert.equal(plan.firstChild.open, true, 'a changed projection preserves the user expansion in the same room');
  assert.equal(plan.firstChild.firstChild.textContent, '任务分工 · 2/2 已完成，伙伴看过');
  const otherCard = ui.get('rooms').children.find((card) => card.firstChild.textContent === '另一个房间');
  otherCard.fire('click'); await settle();
  assert.equal(plan.firstChild.open, false, 'a different room does not inherit the expansion');
  assert.equal(plan.firstChild.firstChild.textContent, '任务分工 · 1/2 已完成，伙伴看过');
  plan.firstChild.open = true;
  await ui.click('new-room');
  assert.equal(plan.hidden, true);
  assert.equal(ui.posts().length, 0);
});

test('a single selected tool keeps the direct-answer request while multi-tool lists show acceptance state', async () => {
  const saved = snapshot({ agents: [catalog[0]] });
  const ui = await browser({ agents: catalog.slice(0, 2), route: (request) => request.path === '/api/rooms' && request.method === 'POST' ? saved : undefined });
  await ui.invite('codex', false);
  assert.match(ui.get('team-setup-note').textContent, /当前选了一个工具.*可再邀请伙伴共同拆分与互审/);
  ui.type('一个工具即可回答的虚构目标');
  await ui.submit();
  assert.equal(ui.posts().length, 1);
  assert.deepEqual(ui.posts()[0].body.participants, ['claude']);
  assert.equal(Object.hasOwn(ui.posts()[0].body, 'collaboration'), false);
  assert.match(ui.get('collaboration-outcome').textContent, /本轮已回复.*单伙伴任务.*在下方补充要求/);
  assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /历史记录|旧版协作|双方回复/);
  assert.match(ui.get('room-subtitle').textContent, /单伙伴任务/);
  assert.equal(ui.document.getElementById('accept-run'), null);
  assert.equal(ui.get('send-label').textContent, '发送补充');
  assert.equal(ui.get('message').disabled, false);
  saved.collaboration = { mode: 'legacy', phase: 'waiting', tasks: [], outcome: { state: 'in_progress',
    reason: '这份记录使用早期的草案与互审流程。', next_action: '查看实际改稿、审核及检查范围后，再决定下一步。' } };
  const projected = await browser({ rooms: [saved.room], saved });
  assert.equal(projected.get('collaboration-outcome').hidden, true, 'routine active status stays outside a delivery card');
  assert.match(projected.get('room-subtitle').textContent, /单伙伴任务/);
  assert.doesNotMatch(projected.get('collaboration-outcome').textContent, /历史|早期|旧版/);
  assert.equal(projected.posts().length, 0);
  const ready = { ...saved.room, outcome_state: 'ready_for_user' };
  const history = await browser({ rooms: [ready] });
  assert.match(history.get('rooms').textContent, /已交付，待你验收/);
  assert.doesNotMatch(history.get('rooms').textContent, /本轮已回复|目标已达成/);
  assert.equal(history.posts().length, 0);
});

test('team delivery stays pending user acceptance and names the exact checked version and remaining work', async () => {
  const saved = snapshot();
  saved.collaboration = teamCollaboration({ state: 'ready_for_user', reason: '双方已提交并互审改稿。', nextAction: '查看成果和检查范围；如需新的修改任务，请点“新任务”继续。' });
  saved.collaboration.outcome.validation = { scope: 'static_review', summary: '只检查三个指定文件，未运行项目。', revision: 2 };
  saved.collaboration.outcome.artifacts = [{ path: 'score.js', revision: 3, sha256: 'b'.repeat(64) }];
  saved.collaboration.outcome.remaining = ['版本 3 尚未运行测试。'];
  const ui = await browser({ rooms: [saved.room], saved });
  const result = ui.get('collaboration-outcome').textContent;
  assert.match(result, /已交付，待你验收/);
  assert.match(result, /是否达到你的目标仍由你验收/);
  assert.match(result, /检查范围：伙伴读过内容，没有运行项目或测试 · 文件版本 2/);
  assert.match(result, /score.js · 版本 3SHA256 b{64}/);
  assert.match(result, /尚未完成版本 3 尚未运行测试/);
  assert.match(result, /接下来需要调整时，直接在下方补充要求.*已有交付与聊天保留/);
  assert.doesNotMatch(result, /目标已达成|目标已验收|测试通过/);
  assert.equal(ui.get('room-status').textContent, '已交付，待你验收');
  ui.type('本轮不应重新启动的修改要求');
  assert.equal(ui.get('message').disabled, true);
  assert.equal(ui.get('send').disabled, true);
  assert.match(ui.get('composer-note').textContent, /本轮已交付.*新任务.*继续/);
  assert.doesNotMatch(ui.get('composer-note').textContent, /补充后继续处理/);
  await ui.submit();
  assert.equal(ui.posts().length, 0);
  await ui.click('new-room');
  ui.type('新的修改任务');
  assert.equal(ui.get('message').disabled, false);
  assert.equal(ui.get('send').disabled, false, 'the existing new-task entry remains available after delivery');
});

test('delivery cards keep hashes and long evidence folded while preserving deliberate inspection through polling', async () => {
  const saved = snapshot();
  saved.collaboration = teamCollaboration({ state: 'stopped', reason: '已保留改稿，仍有一处规则待你确认。' });
  saved.collaboration.outcome.validation = { scope: 'static_review', summary: '较长的检查说明。'.repeat(20), revision: 2 };
  saved.collaboration.outcome.artifacts = Array.from({ length: 5 }, (_, index) => ({ path: `draft${index}.py`, revision: 2, sha256: 'a'.repeat(64) }));
  saved.collaboration.outcome.remaining = ['平局规则待确认'];
  const ui = await browser({ rooms: [saved.room], saved });
  const result = ui.get('collaboration-outcome');
  const details = result.children.find((node) => node.classList.contains('delivery-details'));
  assert.ok(details);
  assert.equal(details.open, false);
  assert.match(details.firstChild.textContent, /查看检查与交付详情 · 5 个文件 · 1 项待处理/);
  const visible = result.children.filter((node) => node !== details).map((node) => node.textContent).join('');
  assert.match(visible, /已保留改稿.*检查：伙伴读过内容/);
  assert.doesNotMatch(visible, /SHA256|draft[0-4]\.py|较长的检查说明/);
  assert.match(details.textContent, /SHA256 a{64}/);
  assert.match(details.textContent, /平局规则待确认/);
  details.open = true;
  saved.collaboration.outcome.reason = '已保留最新改稿，仍待确认。';
  await ui.poll();
  assert.equal(result.children.find((node) => node.classList.contains('delivery-details')).open, true);
  await ui.click('new-room');
  assert.equal(result.hidden, true);
  assert.equal(ui.posts().length, 0);
});

test('team stopping and decision requests name their reason and next action instead of a completion claim', async () => {
  const saved = snapshot({ status: 'budget_exhausted' });
  saved.collaboration = teamCollaboration({ state: 'stopped', reason: '32 次调用上限已到；说明文件尚未交付。', nextAction: '先核对已保存的版本，再决定是否新开任务。' });
  const ui = await browser({ rooms: [saved.room], saved });
  assert.match(ui.get('collaboration-outcome').textContent, /协作已停止.*32 次调用上限已到.*尚未交付/);
  assert.match(ui.get('collaboration-outcome').textContent, /下一步：先核对已保存的版本/);
  assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /目标已验收|已交付，待你验收/);
  saved.room.status = 'needs_user';
  saved.collaboration = teamCollaboration({ state: 'needs_user', reason: '计分平局规则需要你选择。', nextAction: '请补充平局规则。' });
  await ui.poll();
  assert.match(ui.get('collaboration-outcome').textContent, /需要你回复.*计分平局规则.*下一步：请补充平局规则/);
  assert.equal(ui.get('collaboration-plan').hidden, false);
  assert.equal(ui.posts().length, 0);
});

test('terminal team state blocks sending and survives snapshot refresh in only its own sidebar entry', async () => {
  for (const [outcome, phase] of [['stopped', 'stopped'], ['unknown', 'unknown'], ['in_progress', 'cancelled'], ['in_progress', 'interrupted']]) {
    const saved = snapshot({ agents: catalog.slice(0, 2), status: 'needs_user' });
    saved.team = { phase, tasks: [] };
    saved.collaboration = teamCollaboration({ state: outcome, reason: '原生回复无法确认，任务已经停止。' });
    const other = { ...saved.room, id: '33333333-3333-4333-8333-333333333333', goal: '另一个仍等用户信息的房间', updated_at: 1 };
    const listed = { ...saved.room, outcome_state: outcome === 'unknown' ? 'unknown' : 'stopped' };
    const originalRoom = structuredClone(saved.room);
    const ui = await browser({ rooms: [listed, other], saved });
    const expected = outcome === 'unknown' ? '结果待确认' : '协作已停止';
    ui.type('不应派发的补充');
    assert.equal(ui.get('message').disabled, true, `${outcome}/${phase} input is closed despite room.needs_user`);
    assert.equal(ui.get('send').disabled, true);
    assert.match(ui.get('composer-note').textContent, /不会继续派发.*记录仍可查看.*不会自动重跑/);
    assert.equal(ui.get('room-status').textContent, expected);
    assert.equal(ui.get('rooms').children[0].lastChild.firstChild.textContent, expected);
    assert.equal(ui.get('rooms').children[1].lastChild.firstChild.textContent, '等你处理', 'one room projection cannot change another room status');
    await ui.submit();
    await ui.poll();
    assert.equal(ui.get('rooms').children[0].lastChild.firstChild.textContent, expected, 'list metadata is not lost when the selected room snapshot is reapplied');
    assert.equal(ui.get('message').disabled, true);
    assert.equal(ui.posts().length, 0);
    assert.deepEqual(saved.room, originalRoom, 'display projection does not rewrite the backend room row');
  }
});

test('a nonterminal blocked team can receive explicit clarification in the same room', async () => {
  const saved = snapshot({ agents: catalog.slice(0, 2), status: 'needs_user' });
  saved.team = { phase: 'working', tasks: [{ id: 'score', title: '确认平局规则', owner: 'claude', status: 'blocked' }] };
  saved.collaboration = teamCollaboration({ state: 'needs_user', reason: '需要说明平局规则。' });
  const ui = await browser({ rooms: [saved.room], saved, route: (request) =>
    request.path === `/api/rooms/${roomId}/messages` && request.method === 'POST' ? saved : undefined });
  ui.type('平局时双方都加一分。');
  assert.equal(ui.get('message').disabled, false);
  assert.equal(ui.get('send').disabled, false);
  assert.equal(ui.get('composer-note').textContent, '补充后继续处理');
  assert.equal(ui.get('rooms').firstChild.lastChild.firstChild.textContent, '需要你回复');
  await ui.submit();
  assert.equal(ui.posts().length, 1);
  assert.equal(ui.posts()[0].path, `/api/rooms/${roomId}/messages`);
  assert.equal(ui.posts()[0].body.text, '平局时双方都加一分。');
  assert.equal(Object.hasOwn(ui.posts()[0].body, 'collaboration'), false);
});

test('legacy project records explain the old reply boundary, original goal and dated history without rewriting data', async () => {
  const saved = snapshot();
  saved.room.goal = '改进计分规则。' + '请检查边界和旧版本。'.repeat(30);
  saved.room.mode = 'project_files';
  saved.room.created_at = 1793455680;
  saved.room.updated_at = 1793455740;
  saved.project = { available: true, revision: 2, source: { name: '虚构计分示例', path: '/fictional/project' }, files: [], changes: [] };
  const original = structuredClone(saved.room);
  const ui = await browser({ rooms: [saved.room], saved });
  const result = ui.get('collaboration-outcome').textContent;
  assert.equal(ui.get('collaboration-plan').hidden, true);
  assert.match(result, /本轮已回复，目标未验收.*历史记录 · 旧版协作流程/);
  assert.match(result, /没有记录整个目标的验收结果/);
  assert.match(result, /未运行项目或测试.*原项目未改动/);
  assert.match(result, /文件版本 2/);
  assert.equal(ui.get('room-title').textContent, '改进计分规则');
  assert.equal(ui.get('room-title').title, original.goal);
  assert.match(ui.get('rooms').firstChild.lastChild.textContent, /\d+\/\d+|\d+月\d+日/);
  assert.match(ui.get('room-subtitle').textContent, /创建.*历史记录.*不会重跑/);
  assert.deepEqual(saved.room, original);
  assert.equal(ui.posts().length, 0);
});

test('team progress uses a small event whitelist while projection text and unknown outcomes remain inert', async () => {
  const unsafe = '<img src=x onerror=alert(1)>';
  const saved = snapshot({ events: [
    { seq: 1, kind: 'team_task_claimed', sender: 'claude', title: '实现计分规则', commands: [{ shell: 'private command' }], created_at: 1 },
    { seq: 2, kind: 'team_help_requested', sender: 'codex', recipient: 'claude', created_at: 2 },
    { seq: 3, kind: 'team_internal_commands', commands: [{ shell: 'private command' }], created_at: 3 },
  ] });
  saved.collaboration = teamCollaboration({ state: 'goal_achieved', tasks: [{ title: unsafe, owner: 'codex', status: 'accepted', result: unsafe }] });
  saved.collaboration.outcome.reason = unsafe;
  const ui = await browser({ rooms: [saved.room], saved });
  assert.match(ui.get('events').textContent, /Claude Code 已认领「实现计分规则」/);
  assert.match(ui.get('events').textContent, /Codex 向 Claude Code 请求协助/);
  assert.doesNotMatch(ui.get('events').textContent, /private command|team_internal_commands/);
  assert.match(ui.get('collaboration-outcome').textContent, /结果待确认/);
  assert.match(ui.get('collaboration-outcome').textContent, /<img src=x onerror=alert\(1\)>/);
  assert.match(ui.get('collaboration-plan').textContent, /<img src=x onerror=alert\(1\)>/);
  assert.equal(descendants(ui.get('collaboration-plan')).some((node) => node.tagName === 'IMG'), false);
  await ui.click('new-room');
  assert.equal(ui.get('collaboration-plan').hidden, true);
  assert.equal(ui.get('collaboration-outcome').hidden, true);
  assert.equal(ui.get('collaboration-outcome').textContent, '');
  assert.equal(ui.posts().length, 0);
});

test('recorded team_command events show only useful collaboration actions and team reviews keep their identity', async () => {
  const commands = ['claim', 'complete', 'request_help', 'review', 'block', 'propose', 'plan_ready', 'private_unknown'];
  const saved = snapshot({ jobs: [{ id: jobId, provider: 'codex', kind: 'team_review', status: 'working', text: 'internal English review dispatch' }],
    events: commands.map((command, index) => ({ seq: index + 1, kind: 'team_command', sender: index % 2 ? 'codex' : 'claude',
      command, task_id: 'score', commands: [{ shell: 'must stay out of chat' }], created_at: index + 1 })) });
  saved.events.push({ seq: 9, kind: 'job_claimed', sender: 'codex', job_id: jobId, created_at: 9 });
  const ui = await browser({ rooms: [saved.room], saved });
  const chat = ui.get('events').textContent;
  assert.match(chat, /Claude Code 已认领子任务/);
  assert.match(chat, /Codex 已提交子任务，等伙伴检查/);
  assert.match(chat, /Claude Code 请求伙伴协助，具体问题见其消息/);
  assert.match(chat, /Codex 已提交审核结果/);
  assert.match(chat, /Claude Code 说明了当前阻碍，待处理项见任务清单/);
  assert.doesNotMatch(chat, /Codex 开始处理审核任务/);
  assert.match(ui.get('activity-events').textContent, /Codex 开始处理审核任务/);
  assert.deepEqual(ui.get('events').children.map((node) => node.dataset.seq), ['1', '2', '3', '4', '5']);
  assert.doesNotMatch(chat, /propose|plan_ready|private_unknown|must stay out|internal English|commands/);
  const first = ui.get('events').firstChild;
  await ui.poll();
  assert.equal(ui.get('events').children.length, 5);
  assert.equal(ui.get('events').firstChild, first);
  assert.equal(ui.posts().length, 0);
});

test('member cards describe team work and exact reviewer or helper roles using structured records', async () => {
  const saved = snapshot({ agents: catalog.slice(0, 2), status: 'running', jobs: [
    { id: jobId, provider: 'claude', kind: 'team_work', status: 'working', text: 'Work on your claimed task. Task implementation: score.' },
    { id: 'review-call', provider: 'codex', kind: 'team_review', status: 'working', text: 'Independently review this exact task result.' },
  ] });
  saved.members.forEach((member) => { member.status = 'working'; member.current_task = 'internal English current task'; });
  saved.team = { phase: 'working', tasks: [
    { id: 'implementation', title: '实现计分规则', owner: 'claude', status: 'working', capability: 'python', write_paths: ['score.py'] },
    { id: 'checks', title: '核对边界条件', owner: 'claude', reviewer: 'codex', status: 'review' },
  ] };
  const ui = await browser({ rooms: [saved.room], saved });
  assert.equal(ui.get('task-claude').textContent, '正在开发「实现计分规则」');
  assert.equal(ui.get('task-codex').textContent, '正在审核「核对边界条件」');
  assert.doesNotMatch(ui.get('member-cards').textContent, /internal English|claimed task|Independently/);
  saved.jobs[1].kind = 'team_help';
  saved.team.tasks[1] = { id: 'checks', title: '核对边界条件', owner: 'claude', help_to: 'codex', status: 'waiting_peer' };
  await ui.poll();
  assert.equal(ui.get('task-codex').textContent, '正在协助处理「核对边界条件」');
  saved.team.tasks.push({ id: 'ambiguous', title: '另一个等待协助项', owner: 'claude', help_to: 'codex', status: 'waiting_peer' });
  await ui.poll();
  assert.equal(ui.get('task-codex').textContent, '正在协助处理子任务（具体任务待核对）', 'ambiguous roles do not guess which task a public job belongs to');
  saved.members[1].identity_uncertain = true;
  await ui.poll();
  assert.equal(ui.get('task-codex').textContent, '本次执行结果尚未确认，已有记录保留。');
  assert.equal(ui.posts().length, 0);
});

test('planning, claiming and clarification cards translate active jobs without parsing their text', async () => {
  const saved = snapshot({ agents: [catalog[0]], status: 'running', jobs: [
    { id: jobId, provider: 'claude', kind: 'team_plan', status: 'working', text: 'claim review help task fake identity' },
  ] });
  saved.members[0].status = 'working';
  saved.members[0].current_task = 'Discuss the shared goal and your capabilities.';
  saved.team = { phase: 'planning', tasks: [] };
  const ui = await browser({ rooms: [saved.room], saved });
  assert.equal(ui.get('task-claude').textContent, '正在讨论目标与规划分工');
  saved.jobs[0].kind = 'team_coordinate';
  await ui.poll();
  assert.equal(ui.get('task-claude').textContent, '正在确认分工并认领任务');
  saved.jobs[0].kind = 'team_user';
  saved.jobs[0].status = 'queued';
  saved.members[0].status = 'queued';
  await ui.poll();
  assert.equal(ui.get('task-claude').textContent, '等待开始处理你的补充说明');
  assert.doesNotMatch(ui.get('member-cards').textContent, /fake identity|Discuss the shared goal/);
  assert.equal(ui.posts().length, 0);
});

test('idle team members name pending help and dependencies while legacy current tasks remain unchanged', async () => {
  const saved = snapshot({ agents: catalog.slice(0, 2), status: 'running' });
  saved.members.forEach((member) => { member.session_id = roomId; member.current_task = 'old English dispatch'; });
  saved.team = { phase: 'working', tasks: [
    { id: 'draft', title: '整理计分规则', owner: 'claude', help_to: 'codex', status: 'waiting_peer' },
    { id: 'guide', title: '整理使用说明', owner: 'codex', status: 'assigned', dependencies: ['draft'] },
  ] };
  const ui = await browser({ rooms: [saved.room], saved });
  assert.equal(ui.get('task-claude').textContent, '「整理计分规则」等待Codex协助');
  assert.equal(ui.get('task-codex').textContent, '「整理使用说明」等待依赖：整理计分规则');
  saved.team.tasks.forEach((task) => { task.status = 'accepted'; });
  await ui.poll();
  assert.equal(ui.get('task-claude').textContent, '现在没在做事。做完的内容在“交付”里看。');
  assert.equal(ui.get('status-claude').textContent, '空闲');
  assert.doesNotMatch(ui.get('task-claude').textContent, /目标完成|目标已验收|工作已完成/);
  delete saved.team;
  await ui.poll();
  assert.equal(ui.get('task-claude').textContent, 'old English dispatch', 'legacy tasks are not inferred from new team roles');
  assert.equal(ui.posts().length, 0);
});

test('idle task owners waiting for help show a peer-wait badge without hiding active or uncertain member state', async () => {
  const saved = snapshot({ agents: catalog.slice(0, 2), status: 'running' });
  saved.members[1].session_id = roomId;
  saved.team = { phase: 'working', tasks: [{ id: 'progress', title: '实现进度展示', owner: 'codex', help_to: 'claude', status: 'waiting_peer' }] };
  const ui = await browser({ rooms: [saved.room], saved });
  assert.equal(ui.get('status-codex').textContent, '等伙伴协助');
  assert.equal(ui.get('status-codex').classList.contains('waiting_peer'), true);
  assert.equal(ui.get('task-codex').textContent, '「实现进度展示」等待Claude Code协助');
  assert.equal(ui.get('status-claude').textContent, '未启动', 'the requested helper is not automatically marked as waiting');
  for (const [status, label] of [['working', '正在处理'], ['queued', '等待开始'], ['unknown', '状态待确认']]) {
    saved.members[1].status = status;
    saved.jobs = ['working', 'queued'].includes(status) ? [{ id: jobId, provider: 'codex', kind: 'team_work', status }] : [];
    await ui.poll();
    assert.equal(ui.get('status-codex').textContent, label);
    assert.equal(ui.get('status-codex').classList.contains(status), true);
    assert.equal(ui.get('status-codex').classList.contains('waiting_peer'), false);
  }
  saved.members[1].status = 'idle';
  saved.members[1].identity_uncertain = true;
  await ui.poll();
  assert.equal(ui.get('status-codex').textContent, '结果待核对');
  assert.equal(ui.get('status-codex').classList.contains('waiting_peer'), false);
  saved.members[1].identity_uncertain = false;
  delete saved.team;
  await ui.poll();
  assert.equal(ui.get('status-codex').textContent, '空闲', 'legacy idle status retains its original meaning');
  assert.equal(ui.get('status-codex').classList.contains('idle'), true);
  assert.equal(ui.posts().length, 0);
});

test('recovering an old uncertain request preserves its original non-team body', async () => {
  const storage = new Map();
  const ui = await browser({ storage, route: (request) => {
    if (request.path === '/api/rooms' && request.method === 'POST') throw new Error('fictional response loss');
  } });
  ui.type('保留旧请求内容');
  await ui.submit();
  const entry = savedOutbox(storage);
  delete entry.request.body.collaboration;
  outboxDatabases.get(storage).set(`${entry.scope}:${entry.request.body.request_id}`, structuredClone({ scope: `${entry.scope}:${entry.request.body.request_id}`, auth_scope: entry.scope, request: entry.request, receipt: 'unknown' }));
  const saved = snapshot();
  const reloaded = await browser({ storage, route: (request) => request.path === '/api/rooms' && request.method === 'POST' ? saved : undefined });
  assert.equal(reloaded.posts().length, 0);
  await reloaded.click('retry-request');
  assert.equal(reloaded.posts().length, 1);
  assert.deepEqual(reloaded.posts()[0].body, entry.request.body);
  assert.equal(Object.hasOwn(reloaded.posts()[0].body, 'collaboration'), false);
});

test('adding a partner needs no technical fields and generates a useful distinct name and identity', async () => {
  const existing = [...catalog.slice(0, 2), { id: 'existing_buddy', name: 'Claude 会话', avatar: 'C', adapter: 'claude' }];
  let partners = existing;
  const response = () => ({ partners, supported_adapters: [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }] });
  const ui = await browser({ agents: catalog.slice(0, 2), partners: existing, route(request) {
    if (request.path === '/api/partners' && request.method === 'GET') return response();
    if (request.path === '/api/partners' && request.method === 'POST') {
      partners = [...partners, { ...request.body, enabled: true }]; return response();
    }
  } });
  await ui.click('open-partners');
  assert.equal(ui.get('partner-adapter').value, 'claude');
  ui.get('partner-form').fire('submit'); await settle();
  assert.equal(ui.posts().length, 1);
  const body = ui.posts()[0].body;
  assert.match(body.id, /^claude-[0-9a-f]{24}$/);
  assert.equal(body.name, 'Claude 会话2');
  assert.equal(body.avatar, 'C');
  assert.equal(body.bean_style, 'sprout');
  assert.equal(ui.get('partner-id').value, '');
  assert.equal(ui.get('partner-name').value, '');
  assert.equal(ui.get('partner-avatar').value, '');
  assert.deepEqual(ui.targets(), ['all', 'claude', 'codex']);
});

test('an uncertain partner save keeps the same identity through reload and resolves only by reading saved records', async () => {
  const storage = new Map();
  let savedPartner;
  const response = () => ({ partners: [...catalog.slice(0, 2), ...(savedPartner ? [savedPartner] : [])],
    supported_adapters: [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }] });
  const route = (request) => {
    if (request.path === '/api/partners' && request.method === 'GET') return response();
    if (request.path === '/api/partners' && request.method === 'POST') {
      savedPartner = { ...request.body, enabled: true }; throw new Error('lost response after fictional save');
    }
  };
  const ui = await browser({ storage, route });
  await ui.click('open-partners');
  ui.get('partner-adapter').value = 'codex'; ui.get('partner-adapter').fire('change');
  const style = ui.get('bean-style-picker').children.find((node) => node.dataset.beanChoice === 'lavender'); style.fire('click');
  ui.get('partner-form').fire('submit'); await settle();
  const original = ui.posts()[0].body;
  assert.match(original.id, /^codex-[0-9a-f]{24}$/);
  assert.equal(original.avatar, 'X');
  assert.equal(ui.get('partner-id').value, original.id);
  assert.equal(ui.get('save-partner').disabled, true);
  ui.get('partner-form').fire('submit'); await ui.poll();
  assert.equal(ui.posts().length, 1, 'unknown addition cannot become a fresh POST');

  const reloaded = await browser({ storage, route });
  assert.equal(reloaded.posts().length, 0);
  assert.equal(reloaded.get('partner-id').value, original.id);
  assert.equal(reloaded.get('partner-name').value, original.name);
  assert.equal(reloaded.get('partner-adapter').value, 'codex');
  assert.equal(reloaded.get('bean-style-picker').children.find((node) => node.dataset.beanChoice === 'lavender').getAttribute('aria-checked'), 'true');
  await reloaded.click('verify-partner-save');
  assert.equal(reloaded.posts().length, 0);
  assert.equal(reloaded.get('partner-id').value, '');
  assert.equal(reloaded.get('partner-save-pending').hidden, true);
  assert.match(reloaded.get('manager-notice').textContent, /保存记录确认/);

  const refused = await browser({ route: (request) => request.path === '/api/partners' && request.method === 'POST' ? { error: 'invalid_partner_profile' } : undefined });
  refused.get('partner-name').value = '保留我的名字';
  refused.get('partner-form').fire('submit'); await settle();
  assert.equal(refused.get('partner-name').value, '保留我的名字');
  assert.match(refused.get('partner-id').value, /^claude-[0-9a-f]{24}$/);
  assert.equal(refused.get('save-partner').disabled, false, 'a definite refusal permits correcting the original form');
});

test('the optional desktop file picker only fills the project form and cancellation preserves it', async () => {
  const waiting = deferred();
  let next = () => waiting.promise;
  let calls = 0;
  const ui = await browser({ desktopBridge: { chooseProjectFiles: () => { calls += 1; return next(); } } });
  assert.equal(ui.get('choose-project-files').hidden, false);
  ui.get('project-name').value = '原来的名称';
  ui.get('project-path').value = '/fictional/old';
  ui.get('project-files-input').value = 'old.txt';
  await ui.click('choose-project-files');
  await ui.click('choose-project-files');
  assert.equal(calls, 1);
  assert.equal(ui.get('save-project').disabled, true);
  waiting.resolve({ cancelled: true }); await settle();
  assert.equal(ui.get('project-path').value, '/fictional/old');
  assert.equal(ui.get('project-files-input').value, 'old.txt');
  assert.equal(ui.get('project-name').value, '原来的名称');
  next = () => ({ cancelled: false, directory: '/fictional/new', files: ['README.md', 'src/example.js'], suggestedName: '虚构项目' });
  ui.get('project-name').value = '';
  await ui.click('choose-project-files');
  assert.equal(ui.get('project-name').value, '虚构项目');
  assert.equal(ui.get('project-path').value, '/fictional/new');
  assert.equal(ui.get('project-files-input').value, 'README.md\nsrc/example.js');
  assert.match(ui.get('project-picker-note').textContent, /已选 2 个文件.*添加项目.*才保存/);
  assert.equal(ui.posts().length, 0);
  const web = await browser();
  assert.equal(web.get('choose-project-files').hidden, true);
  assert.match(web.get('project-picker-help').textContent, /桌面版可直接选择文件夹/);
});

test('startup discovery reports installation separately from login and keeps manually disabled partners out of new goals', async () => {
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: agent.id === 'claude',
    installation: { state: 'found' }, health: { state: 'unknown', checked_at: null }, monitor: { status: 'idle' } }));
  const discovery = { state: 'completed', observed_at: 5, scope: 'supported_cli_locations', tools: [
    { adapter: 'claude', name: 'Claude Code', installed: true }, { adapter: 'codex', name: 'Codex', installed: true },
  ] };
  const ui = await browser({ agents: [partners[0]], route(request) {
    if (request.path === '/api/partners') return { partners, discovery, supported_adapters: [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }] };
  } });
  assert.equal(ui.get('discovery-hint').hidden, false);
  assert.match(ui.get('discovery-hint-text').textContent, /已找到 Claude Code、Codex/);
  assert.match(ui.get('discovery-hint-text').textContent, /已停用：Codex。要用的话，到“伙伴与额度”里点“启用”。/);
  assert.deepEqual(ui.targets(), ['all', 'claude']);
  await ui.click('open-partners');
  assert.match(ui.get('partners-list').textContent, /本机已找到/);
  assert.match(ui.get('partners-list').textContent, /连接检查：尚未检查接入/);
  assert.equal(descendants(ui.get('partners-list')).some((node) => node.classList.contains('health-state') && node.classList.contains('ready')), false);
  assert.match(ui.get('partners-list').children[1].textContent, /新任务中已停用/);
  await ui.poll();
  assert.deepEqual(ui.targets(), ['all', 'claude']);
  assert.equal(ui.get('events').textContent, '', 'discovery is not inserted into the conversation');
  assert.equal(ui.posts().length, 0, 'startup observations do not check login, re-enable partners or start work');
});

test('missing or unknown installations never restore default participants and discovery can direct the user to management', async () => {
  const saved = snapshot({ agents: catalog.slice(0, 2), events: [message(1, 'codex', '保存的历史答复')] });
  let discovery = { state: 'completed', observed_at: 5, scope: 'supported_cli_locations', tools: [
    { adapter: 'claude', name: 'Claude Code', installed: false }, { adapter: 'codex', name: 'Codex', installed: false },
  ] };
  let installation = 'missing';
  const ui = await browser({ agents: [], rooms: [saved.room], saved, route(request) {
    if (request.path === '/api/partners') return {
      partners: catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true,
        installation: { state: installation }, health: { state: 'unknown', checked_at: null } })),
      supported_adapters: [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }],
      ...(discovery ? { discovery } : {}),
    };
  } });
  assert.deepEqual(ui.members(), ['member-claude', 'member-codex'], 'historical room identity does not depend on installed new-goal tools');
  assert.match(ui.get('events').textContent, /保存的历史答复/);
  await ui.click('new-room');
  ui.type('暂时不能发出的目标');
  assert.equal(ui.get('send').disabled, true);
  assert.deepEqual(ui.targets(), ['all']);
  assert.equal(ui.get('participant-picker').children.length, 0);
  assert.match(ui.get('discovery-hint-text').textContent, /还没在本机找到/);
  assert.equal(ui.get('discovery-manage').hidden, false);
  await ui.click('discovery-manage');
  assert.equal(ui.get('partners-view').hidden, false);
  assert.match(ui.get('partners-list').textContent, /本机未找到.*暂不能加入新任务|暂不能加入新任务.*本机未找到/);

  installation = 'unknown';
  discovery = { ...discovery, tools: discovery.tools.map((tool) => ({ ...tool, installed: tool.adapter === 'claude' ? null : false })) };
  await ui.poll();
  assert.match(ui.get('discovery-hint-text').textContent, /还没确认/);
  assert.match(ui.get('partners-list').textContent, /本机未检测/);
  assert.match(ui.get('partners-list').textContent, /暂不能加入新任务/);
  assert.doesNotMatch(ui.get('partners-list').textContent, /可选入新任务/, 'unknown installation does not imply invitation when the actual catalog excludes it');
  assert.equal(ui.get('send').disabled, true);
  discovery = { ...discovery, state: 'unavailable', tools: discovery.tools.map((tool) => ({ ...tool, installed: null })) };
  await ui.poll();
  assert.match(ui.get('discovery-hint-text').textContent, /这次启动还没确认.*设置保持不变/);
  discovery = null;
  await ui.poll();
  assert.equal(ui.get('discovery-hint').hidden, true);
  assert.equal(ui.get('send').disabled, true);
  assert.equal(ui.posts().length, 0);
});

test('same-tool aliases stay in advanced sessions while history keeps their recorded names', async () => {
  const base = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id }));
  const aliases = [
    { id: 'claude_backup', name: 'Claude 备用', adapter: 'claude', avatar: 'C', monitor: { status: 'needs_user' } },
    { id: 'codex_reviewer', name: 'Codex 审核', adapter: 'codex', avatar: 'X', monitor: { status: 'working' } },
  ];
  let agents = [...base, ...aliases];
  const saved = snapshot({ agents: [base[1], aliases[0]], events: [message(1, 'claude_backup', '保存的旧会话答复。')] });
  const ui = await browser({ route(request) {
    if (request.path === '/api/status') return { agents, task_modes: ['text'] };
    if (request.path === '/api/partners') return { partners: [...base, ...aliases], supported_adapters: [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }] };
    if (request.path === '/api/rooms' && request.method === 'POST') return saved;
    if (request.path === `/api/rooms/${roomId}`) return saved;
  } });
  assert.equal(ui.get('participant-picker').children.length, 2);
  assert.equal(ui.get('extra-session-picker').children.length, 2);
  assert.deepEqual(ui.targets(), ['all', 'claude', 'codex']);
  assert.match(ui.get('extra-session-picker').textContent, /Claude Code 的独立会话/);
  await ui.click('open-partners');
  assert.equal(ui.get('partners-list').children.length, 2);
  assert.equal(ui.get('additional-partners-list').children.length, 2);
  assert.match(ui.get('advanced-sessions-label').textContent, /1 个有任务.*1 个等你回复/, 'folded extra sessions still surface active work and questions');
  assert.doesNotMatch(ui.get('partners-list').textContent, /Claude 备用|Codex 审核/);
  await ui.click('close-manager');
  ui.get('extra-session-choice').open = true;
  await ui.invite('claude', false);
  await ui.invite('claude_backup');
  await ui.choose('codex'); ui.type('只请选中的两个会话检查这段虚构内容。');
  await ui.submit();
  assert.deepEqual(ui.posts()[0].body.participants, ['codex', 'claude_backup']);
  assert.equal(ui.posts()[0].body.target, 'codex');
  agents = base;
  await ui.poll();
  assert.deepEqual(ui.members(), ['member-codex', 'member-claude_backup']);
  assert.match(ui.get('events').textContent, /Claude 备用.*保存的旧会话答复/);
  assert.equal(ui.posts().length, 1, 'moving extra sessions under an advanced fold does not change the dispatch or replay history');
});


test('default project registration accepts a folder without an explicit file list and starts folder mode', async () => {
  const project = { id: '33333333-3333-4333-8333-333333333333', name: '完整项目', path: '/fictional/whole', files: [], selection_kind: 'folder' };
  let registered = false;  // the folder is listed only once it has been added
  const ui = await browser({ route(request) {
    if (request.path === '/api/projects') { registered ||= request.method === 'POST'; return { projects: registered ? [project] : [] }; }
  } });
  ui.get('project-name').value = project.name;
  ui.get('project-path').value = project.path;
  ui.get('project-form').fire('submit'); await settle();
  assert.deepEqual(ui.posts()[0].body, { name: project.name, path: project.path, selection_kind: 'folder' });
  assert.match(ui.get('projects-list').textContent, /完整项目.*伙伴按需查找文件/);
  ui.get('project-picker').value = project.id;
  ui.get('project-picker').fire('change');
  ui.type('请修正跨文件关系'); await ui.submit();
  assert.equal(ui.posts()[1].body.mode, 'project_folder');
  assert.equal(ui.posts()[1].body.project_id, project.id);
});

test('folder desktop chooser preserves cancellation then fills metadata without launching work', async () => {
  let selection = { cancelled: true };
  const ui = await browser({ desktopBridge: { chooseProjectFolder: async () => selection } });
  ui.get('project-name').value = '原名'; ui.get('project-path').value = '/fictional/original';
  await ui.click('choose-project-folder');
  assert.equal(ui.get('project-path').value, '/fictional/original');
  selection = { cancelled: false, path: '/fictional/whole', name: '完整项目' };
  ui.get('project-name').value = ''; ui.get('project-explicit-files').checked = true;
  await ui.click('choose-project-folder');
  assert.equal(ui.get('project-path').value, selection.path);
  assert.equal(ui.get('project-name').value, selection.name);
  assert.equal(ui.get('project-explicit-files').checked, false);
  assert.equal(ui.get('save-project').disabled, false);
  assert.match(ui.get('project-picker-note').textContent, /完整项目文件夹.*按需.*原项目/);
  assert.equal(ui.posts().length, 0);
});


test('folder snapshots display only changed files and the project total', async () => {
  const saved = snapshot();
  saved.room.mode = 'project_folder';
  saved.project = { available: true, format: 'beanest-folder-project-v1', kind: 'folder', revision: 2,
    total_files: 1234, total_bytes: 90000, snapshot_sha256: 'a'.repeat(64),
    source: { name: '完整项目', path: '/fictional/whole' },
    files: [{ path: 'src/changed.js', owner: 'claude', content: 'changed text', sha256: 'b'.repeat(64) }],
    root_entries: ['src', 'assets'], excluded: ['.git'],
  };
  const ui = await browser({ rooms: [saved.room], saved });
  assert.match(ui.get('project-status').textContent, /项目共 1234 个文件.*已修改文件 1 个/);
  assert.match(ui.get('project-files').textContent, /src\/changed.js.*changed text/);
  assert.doesNotMatch(ui.get('project-files').textContent, /assets|\.git/);
  assert.equal(ui.get('download-artifacts').textContent, '下载已修改文件');
  assert.match(ui.get('scope-description').textContent, /隔离副本.*按需.*原项目不变.*暂不运行/);
});


test('a native room lists an imported image by size and a deleted source file instead of hiding them', async () => {
  // Catches a binary file shown as unreadable text, or a partner's deletion missing from the changed-files list.
  const saved = snapshot();
  saved.room.mode = 'native_project';
  saved.project = { available: true, format: 'beanest-folder-workspace-v1', kind: 'folder', revision: 3,
    total_files: 40, total_bytes: 9000, snapshot_sha256: 'a'.repeat(64), source: { name: '虚构游戏' },
    files: [{ path: 'art/new.png', owner: 'claude', sha256: 'b'.repeat(64), bytes: 2048, text: false, content: null }],
    deleted_files: [{ path: 'old.py', owner: 'claude', sha256: 'c'.repeat(64) }], excluded: {} };
  const ui = await browser({ rooms: [saved.room], saved });
  const panel = ui.get('project-files').textContent;
  assert.match(ui.get('project-status').textContent, /2 个已保存改动/);
  assert.match(panel, /art\/new\.png.*二进制文件（例如图片）· 2048 字节，这里不显示内容/);
  assert.match(panel, /old\.py · 已删除 · Claude Code/);
  assert.match(panel, /版本 3 里已没有这个文件；它在原项目里的 SHA256 是 c{64}/);
  const english = await browser({ language: 'en', rooms: [saved.room], saved });
  const englishPanel = english.get('project-files').textContent;
  assert.match(englishPanel, /Binary file \(such as an image\) · 2048 bytes/);
  assert.match(englishPanel, /old\.py · deleted · Claude Code/);
  assert.doesNotMatch(englishPanel, /[一-龥]/);
});


test('a folder project with one selected partner preserves the direct request contract', async () => {
  const project = { id: '33333333-3333-4333-8333-333333333333', name: '项目', path: '/fictional/whole', files: [], selection_kind: 'folder' };
  const ui = await browser({ projects: [project] });
  await ui.invite('codex', false);
  ui.get('project-picker').value = project.id; ui.get('project-picker').fire('change');
  ui.type('改进模块');
  assert.equal(ui.get('send').disabled, false);
  await ui.submit();
  assert.deepEqual(ui.posts()[0].body.participants, ['claude']);
  assert.equal(ui.posts()[0].body.mode, 'project_folder');
  assert.equal(Object.hasOwn(ui.posts()[0].body, 'collaboration'), false);
});

test('folder download accepts a verified large ZIP with a Unicode path while legacy limits stay bounded', async () => {
  const { randomBytes, createHash } = require('node:crypto');
  const { deflateRawSync, crc32 } = require('node:zlib');
  const payload = randomBytes(2 * 1024 * 1024);
  const compressed = deflateRawSync(payload);
  const name = Buffer.from('源码/修改.bin');
  const local = Buffer.alloc(30);
  local.writeUInt32LE(0x04034b50); local.writeUInt16LE(20, 4); local.writeUInt16LE(0x800, 6);
  local.writeUInt16LE(8, 8); local.writeUInt32LE(crc32(payload), 14);
  local.writeUInt32LE(compressed.length, 18); local.writeUInt32LE(payload.length, 22); local.writeUInt16LE(name.length, 26);
  const central = Buffer.alloc(46);
  central.writeUInt32LE(0x02014b50); central.writeUInt16LE(20, 4); central.writeUInt16LE(20, 6);
  central.writeUInt16LE(0x800, 8); central.writeUInt16LE(8, 10); central.writeUInt32LE(crc32(payload), 16);
  central.writeUInt32LE(compressed.length, 20); central.writeUInt32LE(payload.length, 24); central.writeUInt16LE(name.length, 28);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50); end.writeUInt16LE(1, 8); end.writeUInt16LE(1, 10);
  end.writeUInt32LE(central.length + name.length, 12); end.writeUInt32LE(local.length + name.length + compressed.length, 16);
  const raw = Buffer.concat([local, name, compressed, central, name, end]);
  assert.ok(raw.length > 2 * 1024 * 1024, 'the random payload remains incompressible');
  const saved = snapshot();
  saved.room.mode = 'project_folder';
  saved.project = { available: true, kind: 'folder', revision: 2, snapshot_sha256: 'a'.repeat(64), total_files: 1000,
    files: [{ path: '源码/修改.bin', content: '', sha256: 'b'.repeat(64) }] };
  const response = { filename: 'beanest-revision-2.zip', content_type: 'application/zip', content_base64: raw.toString('base64'),
    revision: 2, snapshot_sha256: saved.project.snapshot_sha256, sha256: createHash('sha256').update(raw).digest('hex'), files_count: 1 };
  const route = (request) => request.path.endsWith('/export?revision=2') ? response : undefined;
  const ui = await browser({ rooms: [saved.room], saved, route });
  await ui.click('download-artifacts');
  for (let count = 0; count < 100 && ui.get('download-artifacts').disabled; count += 1) await settle();
  assert.equal(ui.document.downloads.length, 1, ui.get('export-note').textContent);
  assert.deepEqual(Buffer.from(await ui.document.downloads[0].blob.arrayBuffer()), raw);
  const legacy = structuredClone(saved); legacy.room.mode = 'project_files'; delete legacy.project.kind;
  const old = await browser({ rooms: [legacy.room], saved: legacy, route });
  await old.click('download-artifacts');
  assert.equal(old.document.downloads.length, 0);
  assert.match(old.get('export-note').textContent, /版本不一致/);
});

test('project size errors describe folder limits without reintroducing the advanced file quota', async () => {
  const project = { id: '33333333-3333-4333-8333-333333333333', name: '项目', path: '/fictional/whole', files: [], selection_kind: 'folder' };
  const ui = await browser({ projects: [project], route: (request) => request.method === 'POST' && request.path === '/api/rooms' ? { error: 'project_limit_exceeded' } : undefined });
  ui.get('project-picker').value = project.id; ui.get('project-picker').fire('change');
  ui.type('修改文件'); await ui.submit();
  assert.match(ui.get('action-error').textContent, /项目或这批修改超过本机处理上限.*已有成果保留/);
  assert.doesNotMatch(ui.get('action-error').textContent, /12 个|16 KiB|48 KiB/);
});


test('folder exclusions show reason counts without guessing directory contents or exposing unknown codes', async () => {
  const saved = snapshot();
  saved.room.mode = 'project_folder';
  saved.project = { available: true, kind: 'folder', revision: 2, snapshot_sha256: 'a'.repeat(64), total_files: 28,
    excluded: { gitignore: 1, excluded_directory: 2, sensitive_path: 3, sensitive_content: 4, symlink: 5,
      invalid_path: 6, special_file: 7, 'UNTRUSTED-TECHNICAL-CODE': 8 },
    files: [{ path: 'changed.txt', content: 'changed', sha256: 'b'.repeat(64) }] };
  saved.collaboration = teamCollaboration({ state: 'ready_for_user' });
  const ui = await browser({ rooms: [saved.room], saved });
  const panel = ui.get('project-files').textContent;
  assert.match(panel, /跳过：忽略规则 1 项、缓存\/构建目录 2 项、凭据路径 3 项、敏感内容 4 项、符号链接 5 项/);
  assert.match(panel, /不支持的路径 6 项、特殊文件 7 项、其它排除项 8 项（目录按一项计）/);
  assert.doesNotMatch(panel, /UNTRUSTED-TECHNICAL-CODE/);
  assert.doesNotMatch(ui.get('events').textContent, /跳过：|目录按一项计/);
  assert.equal(ui.get('download-delivery').textContent, '导出 ZIP');
  const legacy = structuredClone(saved); legacy.room.mode = 'project_files'; delete legacy.project.kind;
  const old = await browser({ rooms: [legacy.room], saved: legacy });
  assert.equal(old.get('download-delivery').textContent, '导出 ZIP');
  assert.doesNotMatch(old.get('project-files').textContent, /跳过：/);
});

function readableDelivery({ workMode = 'build', mode = 'project_folder' } = {}) {
  const saved = withCurrentRun(snapshot({ agents: catalog.slice(0, 2), status: 'ready_for_user' }), { revision: 2, work_mode: workMode });
  saved.room.mode = mode;
  saved.collaboration = teamCollaboration({ state: 'ready_for_user', tasks: [
    { id: 'a', title: '说明设计', owner: 'claude', status: 'accepted', result: '# 设计方案\n一起完成这个功能。' },
  ] });
  saved.project = { available: true, kind: 'folder', revision: 2, snapshot_sha256: 'a'.repeat(64), files: [
    { path: '交付/说明.md', owner: 'claude', sha256: 'b'.repeat(64), content: '# 交付说明\n\n## 使用\n- 第一步\n- 第二步\n\n```js\nconst value = 1;\n```\n\n| 功能 | 结果 |\n| --- | --- |\n| 阅读 | 完成 |\n\n[正常链接](https://example.com/docs)\n[危险链接](javascript:alert)\n![外部图片](https://example.com/tracker.png)\n<script>alert(1)</script>\n<img src=x onerror=alert(2)>' },
    { path: '交付/notes.txt', owner: 'codex', sha256: 'c'.repeat(64), content: '另一份真实文件\n<iframe src=bad></iframe>' },
  ] };
  return saved;
}

function deliveryButton(ui, title) {
  const button = descendants(ui.get('collaboration-outcome')).find((node) => node.classList.contains('delivery-file') && (node.dataset.documentPath === title || node.textContent === title));
  assert.ok(button, `missing delivery ${title}`); return button;
}

test('delivery is readable in the main conversation with safe Markdown and no extra content request', async () => {
  const saved = readableDelivery();
  const ui = await browser({ rooms: [saved.room], saved });
  const reads = ui.requests.length;
  deliveryButton(ui, '交付/说明.md').fire('click'); await settle();
  assert.equal(ui.get('delivery-reader').hidden, false);
  assert.equal(ui.get('timeline').hidden, true);
  assert.equal(ui.get('composer').hidden, true);
  assert.equal(ui.get('reader-title').textContent, '交付说明');
  const nodes = descendants(ui.get('reader-body'));
  assert.ok(nodes.some((node) => node.tagName === 'H1' && node.textContent === '交付说明'));
  assert.ok(nodes.some((node) => node.tagName === 'UL'));
  assert.ok(nodes.some((node) => node.tagName === 'PRE' && node.textContent.includes('const value')));
  assert.ok(nodes.some((node) => node.tagName === 'TABLE'));
  const links = nodes.filter((node) => node.tagName === 'A');
  assert.equal(links.length, 1); assert.equal(links[0].href, 'https://example.com/docs');
  assert.equal(links[0].rel, 'noopener noreferrer'); assert.equal(links[0].referrerPolicy, 'no-referrer');
  assert.equal(nodes.some((node) => ['IMG', 'SCRIPT', 'IFRAME', 'SVG'].includes(node.tagName)), false);
  assert.match(ui.get('reader-body').textContent, /<script>alert\(1\)<\/script>/);
  assert.match(ui.get('reader-body').textContent, /危险链接.*javascript:alert/);
  assert.match(ui.get('reader-body').textContent, /图片：外部图片（不自动加载）/);
  assert.equal(ui.requests.length, reads, 'snapshot body is used; opening a document has no HTTP/model side effect');
  assert.equal(ui.posts().length, 0);
  await ui.click('reader-back');
  assert.equal(ui.get('delivery-reader').hidden, true); assert.equal(ui.get('timeline').hidden, false);
});

test('reading retains the selected file, DOM and scroll through polling and file switches, but never silently mixes revisions', async () => {
  const saved = readableDelivery();
  const ui = await browser({ rooms: [saved.room], saved });
  deliveryButton(ui, '交付/说明.md').fire('click'); await settle();
  const originalHeading = ui.get('reader-body').firstChild;
  ui.get('reader-body').scrollTop = 321;
  await ui.poll();
  assert.equal(ui.get('reader-body').firstChild, originalHeading);
  assert.equal(ui.get('reader-body').scrollTop, 321);
  assert.equal(ui.get('new-events').hidden, true);
  await ui.click('reader-source');
  assert.match(ui.get('reader-body').textContent, /^# 交付说明/);
  ui.get('reader-body').scrollTop = 87;
  await ui.poll(); assert.equal(ui.get('reader-body').scrollTop, 87);
  await ui.click('reader-source'); assert.equal(ui.get('reader-body').scrollTop, 321);
  ui.get('reader-files').children.find((node) => node.dataset.documentPath === '交付/notes.txt').fire('click'); await settle();
  assert.equal(ui.get('reader-title').textContent, 'notes.txt');
  assert.match(ui.get('reader-body').textContent, /<iframe/);
  ui.get('reader-files').children.find((node) => node.dataset.documentPath === '交付/说明.md').fire('click'); await settle();
  assert.equal(ui.get('reader-body').scrollTop, 321);
  saved.project.revision = 3; saved.project.files[0].content = '# 更新后的说明'; saved.project.files[0].sha256 = 'e'.repeat(64);
  saved.current_run.revision = 3; saved.current_run.digest = 'f'.repeat(64);
  await ui.poll();
  assert.equal(ui.get('reader-title').textContent, '内容有了新版本');
  assert.equal(ui.get('reader-body').textContent, '', 'stale body is not displayed as the current delivery');
  const latest = ui.get('reader-update').children.find((node) => node.tagName === 'BUTTON'); latest.fire('click'); await settle();
  assert.equal(ui.get('reader-body').firstChild.textContent, '更新后的说明');
  await ui.click('new-room'); assert.equal(ui.get('delivery-reader').hidden, true);
});

test('plan results can be read without a file and accepting them never starts implementation', async () => {
  const saved = readableDelivery({ workMode: 'plan', mode: 'text_artifacts' }); delete saved.project;
  const ui = await browser({ rooms: [saved.room], saved, taskModes: ['text', 'text_artifacts'], route(request) {
    if (request.path.endsWith('/accept')) { saved.current_run.can_accept = false; saved.current_run.accepted_at = 10; return saved; }
  } });
  deliveryButton(ui, '说明设计').fire('click'); await settle();
  assert.equal(ui.get('reader-body').firstChild.tagName, 'H1');
  assert.match(ui.get('reader-meta').textContent, /本轮答复.*Claude Code/);
  await ui.click('reader-back'); await ui.click('accept-run');
  assert.equal(ui.posts().length, 1); assert.ok(ui.posts()[0].path.endsWith('/accept'));
  assert.match(ui.get('collaboration-outcome').textContent, /按方案开始/);
  assert.match(ui.get('work-mode-note').textContent, /选择只影响下一轮/);
});

test('new goals default to actual file delivery and work mode is explicit, while choosing a plan keeps the same capable container', async () => {
  const saved = readableDelivery();
  const ui = await browser({ taskModes: ['text', 'text_artifacts'], route: (request) => request.method === 'POST' ? saved : undefined });
  assert.equal(ui.get('work-mode-build').getAttribute('aria-pressed'), 'true');
  assert.equal(ui.get('task-mode').value, 'text_artifacts');
  ui.type('做一份小文档'); await ui.submit();
  assert.equal(ui.posts()[0].body.work_mode, 'build'); assert.equal(ui.posts()[0].body.mode, 'text_artifacts');
  const plan = await browser({ taskModes: ['text', 'text_artifacts'], route: (request) => request.method === 'POST' ? saved : undefined });
  await plan.click('work-mode-plan'); plan.type('先商量一下');
  assert.match(plan.get('scope-description').textContent, /不创建或修改文件/);
  await plan.submit(); assert.equal(plan.posts()[0].body.work_mode, 'plan'); assert.equal(plan.posts()[0].body.mode, 'text_artifacts');
});

test('plan-to-build explicitly starts one bound next run and an uncertain response keeps its work mode and identity', async () => {
  const saved = readableDelivery({ workMode: 'plan', mode: 'text_artifacts' }); delete saved.project;
  const storage = new Map();
  const route = (request) => { if (request.method === 'POST' && request.path.endsWith('/runs')) throw new Error('lost build response'); };
  const ui = await browser({ rooms: [saved.room], saved, storage, route, taskModes: ['text', 'text_artifacts'] });
  await ui.click('build-from-plan'); const request = ui.posts()[0];
  assert.equal(request.path, `/api/rooms/${roomId}/runs`);
  assert.equal(request.body.work_mode, 'build'); assert.equal(request.body.retry, false);
  assert.equal(request.body.run_id, saved.current_run.id); assert.equal(request.body.revision, saved.current_run.revision);
  assert.equal(request.body.digest, saved.current_run.digest); assert.match(request.body.text, /按本轮已交付的方案继续实施/);
  await ui.poll(); await ui.click('build-from-plan'); assert.equal(ui.posts().length, 1);
  const reloaded = await browser({ rooms: [saved.room], saved, storage, route, taskModes: ['text', 'text_artifacts'] });
  assert.equal(reloaded.posts().length, 0);
  await reloaded.click('retry-request'); assert.equal(reloaded.posts()[0].path, request.path); assert.deepEqual(reloaded.posts()[0].body, request.body);
});

test('work mode drafts survive changing rooms and active work cannot change permissions midway', async () => {
  const saved = readableDelivery({ workMode: 'plan' }); saved.current_run.can_continue = false; saved.current_run.can_accept = false;
  saved.room.status = 'running'; saved.collaboration = teamCollaboration();
  const ui = await browser({ rooms: [saved.room], saved, selectSaved: false, locationHash: `#room=${roomId}`, taskModes: ['text', 'text_artifacts'] });
  assert.equal(ui.get('work-mode-plan').disabled, true); assert.equal(ui.get('work-mode-build').disabled, true);
  await ui.click('work-mode-build'); assert.equal(ui.posts().length, 0);
  await ui.click('new-room'); await ui.click('work-mode-plan'); ui.type('我的方案草稿');
  ui.get('rooms').firstChild.fire('click'); await settle(); await ui.click('new-room');
  assert.equal(ui.get('work-mode-plan').getAttribute('aria-pressed'), 'true');
  assert.equal(ui.get('message').value, '我的方案草稿');
});

test('the Markdown reader bounds pathological long input and never fetches remote images or executes HTML', async () => {
  const saved = readableDelivery();
  saved.project.files[0].content = '# 长文\n' + '['.repeat(300000) + '\n<script>bad()</script>';
  const ui = await browser({ rooms: [saved.room], saved });
  deliveryButton(ui, '交付/说明.md').fire('click'); await settle();
  assert.match(ui.get('reader-body').textContent, /只显示前一部分/);
  assert.ok(ui.get('reader-body').textContent.length < 263000);
  assert.equal(descendants(ui.get('reader-body')).some((node) => ['IMG', 'SCRIPT', 'IFRAME'].includes(node.tagName)), false);
  assert.equal(ui.posts().length, 0);
});

test('old text plans start a new file-capable task explicitly without mutating the old room', async () => {
  const saved = readableDelivery({ workMode: 'plan', mode: 'text' }); delete saved.project;
  const next = structuredClone(saved); next.room.id = '77777777-7777-4777-8777-777777777777'; next.room.mode = 'text_artifacts'; next.current_run.work_mode = 'build';
  const ui = await browser({ rooms: [saved.room], saved, taskModes: ['text', 'text_artifacts'], route(request) { if (request.method === 'POST') return next; } });
  assert.equal(ui.get('build-from-plan').textContent, '用此方案开文件任务');
  await ui.click('build-from-plan');
  assert.equal(ui.posts()[0].path, '/api/rooms'); assert.equal(ui.posts()[0].body.mode, 'text_artifacts');
  assert.equal(ui.posts()[0].body.work_mode, 'build'); assert.match(ui.posts()[0].body.goal, /设计方案/);
  assert.equal(saved.room.mode, 'text');
});

test('updating a different file preserves the open unchanged document and its scroll while binding the new revision', async () => {
  const saved = readableDelivery(); const ui = await browser({ rooms: [saved.room], saved });
  deliveryButton(ui, '交付/说明.md').fire('click'); await settle();
  const heading = ui.get('reader-body').firstChild; ui.get('reader-body').scrollTop = 241;
  saved.project.revision = 3; saved.project.files[1].content = '其它文件已更新'; saved.project.files[1].sha256 = 'e'.repeat(64);
  saved.current_run.revision = 3; saved.current_run.digest = 'f'.repeat(64); await ui.poll();
  assert.equal(ui.get('reader-body').firstChild, heading); assert.equal(ui.get('reader-body').scrollTop, 241);
  assert.equal(ui.get('reader-title').textContent, '交付说明'); assert.match(ui.get('reader-meta').textContent, /文件版本 3/);
  await ui.click('reader-source'); await ui.click('reader-source'); assert.equal(ui.get('reader-body').scrollTop, 241);
});

test('single-partner plan replies have a reader and an explicit new file task without pretending a run continuation exists', async () => {
  const saved = snapshot({ agents: catalog.slice(0, 1), status: 'completed', events: [message(1, 'claude', '# 单人方案\n先做一份说明。')] });
  saved.room.work_mode = 'plan'; saved.room.mode = 'text_artifacts';
  const next = structuredClone(saved); next.room.id = '77777777-7777-4777-8777-777777777777'; next.room.work_mode = 'build';
  const ui = await browser({ agents: catalog.slice(0, 1), rooms: [saved.room], saved, taskModes: ['text', 'text_artifacts'], route(request) { if (request.method === 'POST') return next; } });
  deliveryButton(ui, 'Claude Code 的答复').fire('click'); await settle();
  assert.equal(ui.get('reader-body').firstChild.textContent, '单人方案'); assert.match(ui.get('room-subtitle').textContent, /先聊方案/); await ui.click('reader-back');
  assert.equal(ui.get('build-from-plan').textContent, '用此方案开文件任务'); await ui.click('build-from-plan');
  assert.equal(ui.posts()[0].path, '/api/rooms'); assert.equal(ui.posts()[0].body.work_mode, 'build');
  assert.match(ui.posts()[0].body.goal, /单人方案/); assert.equal(Object.hasOwn(ui.posts()[0].body, 'run_id'), false);
});

test('a delivery after the first batch is selectable and readable without a ZIP download', async () => {
  const saved = readableDelivery();
  saved.project.files = Array.from({ length: 257 }, (_, index) => ({ path: `${index}.md`, sha256: 'a'.repeat(64), content: `# 成果 ${index}` }));
  const ui = await browser({ rooms: [saved.room], saved });
  assert.equal(descendants(ui.get('collaboration-outcome')).filter((node) => node.classList.contains('delivery-file') && /^\d+\.md$/.test(node.dataset.documentPath || '')).length, 255);
  assert.match(ui.get('more-delivery-files').textContent, /还有 2 份/);
  await ui.click('more-delivery-files');
  deliveryButton(ui, '256.md').fire('click'); await settle();
  assert.equal(ui.get('reader-body').firstChild.textContent, '成果 256');
  assert.match(ui.get('reader-files').textContent, /第 3 \/ 3 批/);
  assert.ok(descendants(ui.get('reader-files')).some((node) => node.tagName === 'BUTTON' && node.textContent === '上一批'));
  assert.equal(ui.document.downloads.length, 0); assert.equal(ui.posts().length, 0);
});

test('plan handoff accurately names partial context and does not claim the complete discussion was imported', async () => {
  const saved = readableDelivery(); saved.current_run.source_plan = { run_id: '77777777-7777-4777-8777-777777777777', revision: 1, truncated: false };
  const ui = await browser({ rooms: [saved.room], saved });
  assert.equal(ui.get('source-plan-note').hidden, false); assert.match(ui.get('source-plan-note').textContent, /已带上上一轮交付的方案，不含完整讨论/);
  saved.current_run.source_plan.truncated = true; await ui.poll();
  assert.match(ui.get('source-plan-note').textContent, /部分内容.*完整方案仍在上一轮/);
});

test('a file delivery shows its Markdown title and path without presenting the same task summary as another deliverable', async () => {
  const saved = readableDelivery();
  saved.team = { tasks: [{ id: 'a', result_files: [{ path: '交付/说明.md', sha256: 'b'.repeat(64) }] }] };
  const ui = await browser({ rooms: [saved.room], saved });
  const buttons = descendants(ui.get('collaboration-outcome')).filter((node) => node.classList.contains('delivery-file'));
  assert.equal(buttons.length, 2, 'two real files are the two deliverables; the task result is not a third file');
  const document = deliveryButton(ui, '交付/说明.md');
  assert.equal(document.firstChild.textContent, '交付说明');
  assert.equal(document.children[1].textContent, '交付/说明.md');
  assert.equal(document.getAttribute('aria-label'), '交付说明 · 交付/说明.md');
  assert.match(ui.get('collaboration-plan').textContent, /一起完成这个功能/, 'the task summary remains in task details');
  document.fire('click'); await settle(); assert.equal(ui.get('reader-title').textContent, '交付说明');

  saved.current_run.work_mode = 'plan'; saved.room.work_mode = 'plan';
  const plan = await browser({ rooms: [saved.room], saved });
  deliveryButton(plan, '说明设计').fire('click'); await settle();
  assert.equal(plan.get('reader-title').textContent, '说明设计', 'plan result remains readable even when this room carries older files');
});

test('large Markdown keeps bounded formatting while its complete source is readable without a download', async () => {
  const saved = readableDelivery();
  const marker = '这里是完整正文末尾标记';
  saved.project.files[0].content = '# 完整长文\n' + '正文内容'.repeat(86000) + '\n' + marker;
  assert.ok(Buffer.byteLength(saved.project.files[0].content, 'utf8') < 1024 * 1024, 'the sample remains within the host file limit');
  const ui = await browser({ rooms: [saved.room], saved });
  deliveryButton(ui, '交付/说明.md').fire('click'); await settle();
  assert.match(ui.get('reader-body').textContent, /排版只显示前一部分.*查看源文.*完整内容/);
  assert.equal(ui.get('reader-body').textContent.includes(marker), false);
  const requests = ui.requests.length;
  await ui.click('reader-source');
  assert.equal(ui.get('reader-body').children.length, 1, 'full source uses one inert text node container');
  assert.equal(ui.get('reader-body').firstChild.tagName, 'PRE');
  assert.equal(ui.get('reader-body').textContent, saved.project.files[0].content);
  assert.equal(ui.get('reader-body').textContent.endsWith(marker), true);
  assert.equal(ui.document.downloads.length, 0); assert.equal(ui.posts().length, 0);
  assert.equal(ui.requests.length, requests, 'the existing snapshot body is used without another read');
});

function lifecycleButton(root, { kind, id, action }) {
  const button = descendants(root).find((node) => node.tagName === 'BUTTON' && node.dataset.lifecycleKind === kind
    && node.dataset.lifecycleId === id && node.dataset.lifecycleAction === action);
  assert.ok(button, `missing ${kind}/${id}/${action}`);
  return button;
}
function selectLifecycleView(ui, kind, value) {
  const picker = ui.get(`${kind}-filter`); picker.value = value; picker.fire('change');
}
function registeredProject(id, name, lifecycle_state) {
  return { id, name, path: `/fictional/${name}`, files: [], selection_kind: 'folder', ...(lifecycle_state ? { lifecycle_state } : {}) };
}

test('project views count actual states, archive clears only project selection, and remove requires reversible preview', async () => {
  const active = registeredProject('33333333-3333-4333-8333-333333333333', '活动项目');
  const archived = registeredProject('44444444-4444-4444-8444-444444444444', '旧项目', 'archived');
  const removed = registeredProject('55555555-5555-4555-8555-555555555555', '移除项目', 'removed');
  const projects = [active, archived, removed];
  const ui = await browser({ projects, route(request) {
    if (request.path.endsWith('/lifecycle')) {
      const project = projects.find((entry) => request.path === `/api/projects/${entry.id}/lifecycle`);
      assert.ok(project); assert.deepEqual(Object.keys(request.body), ['action']);
      project.lifecycle_state = { archive: 'archived', remove: 'removed', restore: 'active' }[request.body.action];
      return { project, projects };
    }
  } });
  assert.deepEqual(ui.get('project-picker').children.map((node) => node.value), ['', active.id]);
  assert.deepEqual(ui.get('projects-filter').children.map((node) => node.textContent), ['当前 · 1', '已归档 · 1', '已移除 · 1']);
  ui.get('project-picker').value = active.id; ui.get('project-picker').fire('change'); ui.type('保留这段未发送的目标');
  await ui.click('open-projects');
  lifecycleButton(ui.get('projects-list'), { kind: 'projects', id: active.id, action: 'archive' }).fire('click'); await settle();
  assert.equal(ui.get('project-picker').value, '');
  assert.equal(ui.get('message').value, '保留这段未发送的目标');
  assert.equal(ui.get('projects-list').children.some((node) => node.textContent.includes('活动项目')), false);
  assert.deepEqual(ui.get('projects-filter').children.map((node) => node.textContent), ['当前 · 0', '已归档 · 2', '已移除 · 1']);
  selectLifecycleView(ui, 'projects', 'archived');
  assert.equal(ui.get('projects-list').children.length, 2);
  lifecycleButton(ui.get('projects-list'), { kind: 'projects', id: active.id, action: 'remove' }).fire('click'); await settle();
  assert.equal(ui.posts().length, 1, 'preview itself performs no request');
  assert.equal(ui.get('lifecycle-confirm').hidden, false);
  assert.match(ui.get('lifecycle-confirm-description').textContent, /磁盘上的项目文件和已有聊天都会保留/);
  await ui.click('lifecycle-confirm-cancel'); assert.equal(ui.posts().length, 1);
  lifecycleButton(ui.get('projects-list'), { kind: 'projects', id: active.id, action: 'remove' }).fire('click');
  await ui.click('lifecycle-confirm-submit');
  selectLifecycleView(ui, 'projects', 'removed');
  lifecycleButton(ui.get('projects-list'), { kind: 'projects', id: active.id, action: 'restore' }).fire('click'); await settle();
  assert.equal(ui.get('project-picker').children.some((option) => option.value === active.id), true);
  assert.equal(ui.get('project-picker').value, '', 'restoring does not silently reselect a project');
  assert.deepEqual(ui.posts().map((request) => request.body), [{ action: 'archive' }, { action: 'remove' }, { action: 'restore' }]);
  assert.ok(ui.posts().every((request) => request.path.endsWith('/lifecycle')), 'management never dispatches a goal');
});

test('archived and removed partners stay out of defaults after stale status and discovery; restoring is explicit', async () => {
  const partners = catalog.map((agent, index) => ({ ...agent, adapter: agent.id, enabled: true,
    lifecycle_state: ['archived', 'active', 'removed'][index] }));
  const ui = await browser({ partners, route(request) {
    if (request.path === '/api/partners/claude/lifecycle') {
      partners[0].lifecycle_state = request.body.action === 'restore' ? 'active' : 'archived';
      return { partners, supported_adapters: [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }] };
    }
  } });
  assert.deepEqual(ui.members(), ['member-codex']);
  assert.deepEqual(ui.get('participant-picker').children.map((node) => node.dataset.agent), ['codex']);
  await ui.poll(); await ui.click('open-partners');
  assert.deepEqual(ui.get('partners-filter').children.map((node) => node.textContent), ['当前 · 1', '已归档 · 1', '已移除 · 1']);
  assert.equal(ui.get('partners-list').children.length, 1);
  selectLifecycleView(ui, 'partners', 'archived');
  lifecycleButton(ui.get('partners-list'), { kind: 'partners', id: 'claude', action: 'restore' }).fire('click'); await settle();
  assert.deepEqual(ui.get('participant-picker').children.map((node) => node.dataset.agent), ['claude', 'codex']);
  assert.deepEqual(ui.members(), ['member-codex'], 'restored partner requires a fresh user selection');
  selectLifecycleView(ui, 'partners', 'removed');
  const removedCard = ui.get('partners-list').firstChild;
  assert.match(removedCard.textContent, /第三位测试伙伴/);
  assert.equal(descendants(removedCard).some((node) => node.textContent === '检查连接'), false);
  selectLifecycleView(ui, 'partners', 'active');
  lifecycleButton(ui.get('partners-list'), { kind: 'partners', id: 'claude', action: 'remove' }).fire('click'); await settle();
  assert.match(ui.get('lifecycle-confirm-description').textContent, /不会卸载工具、删除 Claude 或 Codex 的原生会话/);
  assert.equal(ui.posts().length, 1, 'remove preview does not delete anything');
  await ui.click('lifecycle-confirm-cancel');
  assert.ok(ui.posts().every((request) => request.path.endsWith('/lifecycle')));
});

test('room archive and restore preserve an open document and draft, and archived rooms cannot dispatch or accept', async () => {
  const saved = readableDelivery();
  const ui = await browser({ rooms: [saved.room], saved, route(request) {
    if (request.path === `/api/rooms/${roomId}/lifecycle`) {
      saved.room.lifecycle_state = { archive: 'archived', restore: 'active', remove: 'removed' }[request.body.action];
      return saved;
    }
  } });
  ui.type('下一项要求草稿');
  deliveryButton(ui, '交付/说明.md').fire('click'); await settle();
  const body = ui.get('reader-body').firstChild; ui.get('reader-body').scrollTop = 246;
  lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'archive' }).fire('click'); await settle();
  assert.equal(ui.get('rooms').children.length, 0);
  assert.equal(ui.get('room-count').textContent, '0');
  assert.match(ui.get('room-lifecycle-note').textContent, /已归档.*聊天和交付仍可阅读/);
  assert.equal(ui.get('reader-body').firstChild, body); assert.equal(ui.get('reader-body').scrollTop, 246);
  assert.equal(ui.get('message').value, '下一项要求草稿');
  assert.equal(ui.get('send').disabled, true); assert.equal(ui.get('accept-run').disabled, true);
  assert.equal(ui.get('work-mode-build').disabled, true);
  await ui.submit(); await ui.click('accept-run');
  assert.equal(ui.posts().length, 1, 'archived room guards all task-advancing controls');
  selectLifecycleView(ui, 'rooms', 'archived'); assert.equal(ui.get('rooms').children.length, 1);
  await ui.poll(); assert.equal(ui.get('reader-body').firstChild, body); assert.equal(ui.get('reader-body').scrollTop, 246);
  lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'restore' }).fire('click'); await settle();
  assert.equal(ui.get('room-lifecycle-note').hidden, true); assert.equal(ui.get('accept-run').disabled, false);
  assert.equal(ui.get('message').value, '下一项要求草稿');
  assert.equal(ui.get('reader-body').firstChild, body); assert.equal(ui.get('reader-body').scrollTop, 246);
  assert.ok(ui.posts().every((request) => request.path.endsWith('/lifecycle')));
});

test('room lifecycle respects host cleanup guards even when the visible work is already delivered', async () => {
  const saved = withCurrentRun(snapshot({ status: 'ready_for_user' }));
  saved.collaboration = teamCollaboration({ state: 'ready_for_user' });
  saved.room.lifecycle_can_change = false; saved.room.lifecycle_blocked_reason = 'team_cleanup_unconfirmed';
  const ui = await browser({ rooms: [saved.room], saved });
  const archive = lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'archive' });
  assert.equal(archive.disabled, true);
  assert.match(ui.get('room-lifecycle-actions').textContent, /执行清理尚未确认/);
  archive.fire('click'); await settle(); assert.equal(ui.posts().length, 0);
  saved.room.lifecycle_can_change = true; saved.room.lifecycle_blocked_reason = null; await ui.poll();
  assert.equal(lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'archive' }).disabled, false);
});

test('room removal is a confirmed reversible registration action and removed history stays readable', async () => {
  const saved = readableDelivery();
  const ui = await browser({ rooms: [saved.room], saved, route(request) {
    if (request.path === `/api/rooms/${roomId}/lifecycle`) { saved.room.lifecycle_state = 'removed'; return saved; }
  } });
  lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'remove' }).fire('click'); await settle();
  assert.equal(ui.posts().length, 0);
  assert.match(ui.get('lifecycle-confirm-description').textContent, /聊天、交付文件和原生会话会保留/);
  await ui.click('lifecycle-confirm-submit');
  assert.deepEqual(ui.posts().map((request) => [request.path, request.body]), [[`/api/rooms/${roomId}/lifecycle`, { action: 'remove' }]]);
  selectLifecycleView(ui, 'rooms', 'removed'); assert.equal(ui.get('rooms').children.length, 1);
  assert.match(ui.get('room-status').textContent, /已移除/);
  deliveryButton(ui, '交付/说明.md').fire('click'); await settle();
  assert.equal(ui.get('reader-title').textContent, '交付说明');
  await ui.poll(); assert.equal(ui.posts().length, 1, 'reading and polling never redo management or dispatch work');
});

test('legacy ready state explicitly waits for a new request instead of implying a stopped or automatic development task', async () => {
  const saved = withCurrentRun(snapshot({ status: 'ready_for_user' }), { work_mode: 'legacy' });
  saved.collaboration = teamCollaboration({ state: 'ready_for_user' });
  const ui = await browser({ rooms: [saved.room], saved });
  assert.match(ui.get('collaboration-outcome').textContent, /本轮已交付，正在等你的下一项要求/);
  assert.match(ui.get('collaboration-outcome').textContent, /排查报告不会自动变成开发任务/);
  assert.match(ui.get('collaboration-outcome').textContent, /下方写明接下来要做什么，再点“开始下一轮”/);
  assert.equal(ui.document.getElementById('build-from-plan'), null);
  await ui.poll(); assert.equal(ui.posts().length, 0);
});

test('remove confirmation keeps keyboard focus inside, Escape cancels and restores the invoking control', async () => {
  const saved = snapshot(); const ui = await browser({ rooms: [saved.room], saved });
  const remove = lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'remove' });
  remove.focus(); remove.fire('click'); await settle();
  assert.equal(ui.document.activeElement, ui.get('lifecycle-confirm-cancel'));
  ui.document.root.fire('keydown', { key: 'Tab' });
  assert.equal(ui.document.activeElement, ui.get('lifecycle-confirm-submit'));
  ui.document.root.fire('keydown', { key: 'Tab' });
  assert.equal(ui.document.activeElement, ui.get('lifecycle-confirm-cancel'));
  ui.document.root.fire('keydown', { key: 'Tab', shiftKey: true });
  assert.equal(ui.document.activeElement, ui.get('lifecycle-confirm-submit'));
  ui.document.root.fire('keydown', { key: 'Escape' });
  assert.equal(ui.get('lifecycle-confirm').hidden, true);
  assert.equal(ui.document.activeElement, remove);
  assert.equal(ui.posts().length, 0);
  await ui.click('open-partners');
  const partnerRemove = lifecycleButton(ui.get('partners-list'), { kind: 'partners', id: 'codex', action: 'remove' });
  partnerRemove.focus(); partnerRemove.fire('click'); await settle();
  ui.document.root.fire('keydown', { key: 'Escape' });
  assert.equal(ui.get('workspace-manager').hidden, false, 'Escape closes only the top confirmation');
  assert.equal(ui.document.activeElement, partnerRemove);
  assert.equal(ui.posts().length, 0);
});

function accountFixture(overrides = {}) {
  return { state: 'ready', email: 'reader@example.com', plan: 'plus', auth_method: 'chatgpt', source: 'codex_app_server',
    scope: 'default_cli_login', checked_at: 1000, stale: false, reason: null, shared_profile_ids: ['codex'],
    quota: { state: 'available', scope: 'provider_account', source: 'codex_app_server_account_rate_limits', checked_at: 1000, stale: false, reason: null,
      windows: [{ name: 'codex:primary', remaining_percent: 72, used_percent: 28, window_minutes: 300, resets_at: 1300 },
        { name: '本周账号额度', remaining_percent: 0, used_percent: 100, window_minutes: 10080, resets_at: 9000 }] }, ...overrides };
}
function accountRefreshButton(root) {
  const button = descendants(root).find((node) => node.tagName === 'BUTTON' && node.dataset.focusKey === 'account-refresh');
  assert.ok(button); return button;
}

test('provider quota displays observed account windows separately from task Token and never invents a single-model limit', async () => {
  const saved = snapshot({ agents: catalog.slice(0, 2) });
  saved.members[1].usage = { input_tokens: 987654321, output_tokens: 123456, source: 'provider' };
  saved.members[1].account = accountFixture({ email: 'old-session@example.com' });
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true,
    account: agent.id === 'codex' ? accountFixture({ shared_profile_ids: ['codex', 'codex_review'] }) : null }));
  partners.push({ id: 'codex_review', name: 'Codex 审核', adapter: 'codex', enabled: true, account: accountFixture({ shared_profile_ids: ['codex', 'codex_review'] }) });
  const ui = await browser({ rooms: [saved.room], saved, partners });
  // The card only says the partner is signed in; who it is stays under 账号与额度 (README screenshots use this page).
  assert.equal(ui.get('account-email-codex').textContent, '已登录');
  assert.match(ui.get('provider-account-codex').textContent, /reader@example.com/);
  assert.doesNotMatch(ui.get('provider-account-codex').textContent, /old-session/);
  const footer = ui.get('provider-account-codex'); footer.open = true;
  const meters = descendants(footer).filter((node) => node.tagName === 'PROGRESS');
  assert.deepEqual(meters.map((node) => node.value), [72, 0], 'observed zero is distinguishable from unknown');
  assert.match(footer.textContent, /账号共享额度，未按单模型拆分/);
  assert.match(footer.textContent, /额度来源：Codex 原生额度查询/);
  assert.match(footer.textContent, /与Codex 审核共用本机 Codex 登录/);
  assert.match(footer.textContent, /改名或新建会话不会切换账号/);
  assert.match(footer.textContent, /Codex 额度池 · 5小时窗口.*重置/);
  assert.match(footer.textContent, /7天窗口.*重置/);
  assert.doesNotMatch(footer.textContent, /987654321|old-session@example.com/);
  const body = footer.lastChild;
  await ui.poll(); assert.equal(ui.get('provider-account-codex'), footer); assert.equal(footer.lastChild, body); assert.equal(footer.open, true);
  assert.equal(ui.posts().length, 0, 'opening and polling quota uses the existing cached metadata only');
  await ui.click('open-partners');
  assert.match(ui.get('partners-list').textContent, /reader@example.com/);
  const ids = descendants(ui.document.root).map((node) => node.id).filter(Boolean);
  assert.equal(new Set(ids).size, ids.length, 'room and management account footers have distinct DOM identities');
  assert.equal(ui.get('events').textContent.includes('reader@example.com'), false, 'account data is not added to chat');
});

test('unavailable provider quota and malformed percentages stay unknown despite task usage', async () => {
  const saved = snapshot({ agents: catalog.slice(0, 2) });
  saved.members[0].usage = { input_tokens: 100000, output_tokens: 20000 };
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, account: agent.id === 'claude'
    ? accountFixture({ state: 'partial', email: 'claude-reader@example.com', plan: 'max', source: 'claude_auth_status', stale: true,
      quota: { state: 'unavailable', scope: 'provider_account', windows: [], reason: 'provider_usage_not_exposed', checked_at: 1000, stale: false } })
    : accountFixture({ email: null, quota: { state: 'available', scope: 'provider_account', checked_at: null, windows: [
      { name: '未确认窗口', remaining_percent: 140, used_percent: -5, window_minutes: null, resets_at: null },
    ] } }) }));
  const ui = await browser({ rooms: [saved.room], saved, partners });
  const claude = ui.get('provider-account-claude');
  assert.equal(ui.get('account-email-claude').textContent, '已登录 · 待刷新');
  assert.match(claude.textContent, /claude-reader@example.com/);
  assert.match(claude.textContent, /当前接入尚未取得订阅额度/);
  assert.equal(descendants(claude).some((node) => node.tagName === 'PROGRESS'), false);
  assert.doesNotMatch(claude.textContent, /剩余 0%|100000|20000/);
  const codex = ui.get('provider-account-codex');
  assert.doesNotMatch(ui.get('account-email-codex').textContent, /邮箱|@/);
  assert.match(codex.textContent, /邮箱未获取/);
  assert.match(codex.textContent, /比例未提供.*重置时间未提供/);
  assert.equal(descendants(codex).some((node) => node.tagName === 'PROGRESS'), false);
  assert.equal(ui.posts().length, 0);
});

test('account refresh is explicit and single flight while reading and drafts remain intact', async () => {
  const saved = readableDelivery();
  const waiting = deferred();
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, account: null }));
  const ui = await browser({ rooms: [saved.room], saved, partners, route: async (request) => {
    if (request.path === '/api/partners/codex/account/refresh') {
      await waiting.promise; partners[1].account = accountFixture();
      return { partners, supported_adapters: [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }] };
    }
  } });
  assert.match(ui.get('provider-account-codex').textContent, /还没有检查这个账号/);
  ui.type('保留这段草稿');
  deliveryButton(ui, '交付/说明.md').fire('click'); await settle();
  const heading = ui.get('reader-body').firstChild; ui.get('reader-body').scrollTop = 319;
  const refresh = accountRefreshButton(ui.get('provider-account-codex'));
  refresh.fire('click'); refresh.fire('click'); await settle();
  assert.equal(ui.posts().length, 1);
  assert.deepEqual(ui.posts()[0].body, {});
  assert.equal(accountRefreshButton(ui.get('provider-account-claude')).disabled, true);
  assert.equal(ui.get('message').disabled, false, 'account monitoring does not interrupt writing the next goal');
  assert.match(ui.get('provider-account-codex').textContent, /正在查询/);
  waiting.resolve(); await settle();
  assert.equal(ui.get('reader-body').firstChild, heading); assert.equal(ui.get('reader-body').scrollTop, 319);
  assert.equal(ui.get('message').value, '保留这段草稿');
  assert.match(ui.get('provider-account-codex').textContent, /reader@example.com/);
  await ui.poll(); assert.equal(ui.posts().length, 1);
  assert.equal(ui.posts()[0].path, '/api/partners/codex/account/refresh', 'no room or model dispatch happens');
});

test('expanded quota panels refresh immediately and each minute, pausing on opt-out, hidden pages and close', async () => {
  let clock = 1000000;
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, account: null }));
  const ui = await browser({ partners, now: () => clock, route(request) {
    if (request.path === '/api/partners/codex/account/refresh') {
      partners[1].account = accountFixture({ checked_at: clock / 1000 });
      return { partners, supported_adapters: [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }] };
    }
  } });
  const footer = ui.get('provider-account-codex'); footer.open = true; footer.fire('toggle'); await settle();
  assert.equal(ui.posts().length, 1, 'opening a visible panel requests a no-inference account check');
  clock += 30000; await ui.poll(); assert.equal(ui.posts().length, 1);
  clock += 31000; await ui.poll(); assert.equal(ui.posts().length, 2);
  let auto = descendants(footer).find((node) => node.tagName === 'INPUT' && node.dataset.focusKey === 'account-auto');
  auto.checked = false; auto.fire('change'); clock += 61000; await ui.poll(); assert.equal(ui.posts().length, 2);
  auto = descendants(footer).find((node) => node.tagName === 'INPUT' && node.dataset.focusKey === 'account-auto');
  auto.checked = true; auto.fire('change'); await settle(); assert.equal(ui.posts().length, 3);
  ui.document.hidden = true; ui.document.root.fire('visibilitychange'); clock += 61000; await ui.poll();
  assert.equal(ui.posts().length, 3, 'hidden page cancels active account timers');
  ui.document.hidden = false; ui.document.root.fire('visibilitychange'); await settle(); assert.equal(ui.posts().length, 4);
  footer.open = false; footer.fire('toggle'); clock += 61000; await ui.poll(); assert.equal(ui.posts().length, 4);
  assert.ok(ui.posts().every((request) => request.path === '/api/partners/codex/account/refresh'));
});

test('a failed account check labels retained email and quota as last observations, not the current account', async () => {
  const account = accountFixture({ state: 'error', stale: true, reason: 'account_check_timeout' });
  account.quota = { ...account.quota, state: 'error', stale: true, reason: 'provider_usage_unavailable' };
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, account: agent.id === 'codex' ? account : null }));
  const ui = await browser({ partners });
  assert.equal(ui.get('account-email-codex').textContent, '已登录 · 待刷新');
  const footer = ui.get('provider-account-codex');
  assert.match(footer.textContent, /上次确认登录.*reader@example.com/);
  assert.match(footer.textContent, /旧快照/);
  assert.match(footer.textContent, /这次检查超时/);
  assert.deepEqual(descendants(footer).filter((node) => node.tagName === 'PROGRESS').map((node) => node.value), [72, 0]);
  assert.equal(ui.posts().length, 0);
});

test('unsupported account connectors stay visibly unavailable and never start automatic quota requests', async () => {
  const partners = [{ ...catalog[1], adapter: 'codex', enabled: true, account: { state: 'unavailable', source: 'unsupported',
    reason: 'account_adapter_unsupported', email: null, quota: { state: 'unavailable', windows: [], reason: 'account_adapter_unsupported' } } }];
  const ui = await browser({ agents: [catalog[1]], partners });
  const footer = ui.get('provider-account-codex');
  assert.equal(accountRefreshButton(footer).disabled, true);
  assert.match(footer.textContent, /暂不提供账号与额度查询/);
  footer.open = true; footer.fire('toggle'); await settle(); await ui.poll();
  assert.equal(ui.posts().length, 0);
});

test('account deadlines survive async checks and manual phase changes without starving another visible profile', async () => {
  const startedAt = 1000000; let clock = startedAt;
  const pendingChecks = new Map(); const calls = [];
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, account: null }));
  const ui = await browser({ partners, now: () => clock, route: async (request) => {
    const match = request.path.match(/^\/api\/partners\/(codex|claude)\/account\/refresh$/);
    if (!match) return;
    const id = match[1]; const began = clock; const waiting = deferred();
    calls.push({ id, began }); pendingChecks.set(id, waiting);
    await waiting.promise;
    partners.find((entry) => entry.id === id).account = accountFixture({ checked_at: began / 1000 });
    return { partners, supported_adapters: [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }] };
  } });
  const codex = ui.get('provider-account-codex'); const claude = ui.get('provider-account-claude');
  codex.open = true; codex.fire('toggle'); claude.open = true; claude.fire('toggle'); await settle();
  assert.deepEqual(calls, [{ id: 'codex', began: startedAt }], 'only one native check runs at a time');
  clock = startedAt + 8000; pendingChecks.get('codex').resolve(); await settle();
  assert.deepEqual(calls[1], { id: 'claude', began: startedAt + 8000 }, 'a second opened profile gets checked after the first finishes');
  clock = startedAt + 12000; pendingChecks.get('claude').resolve(); await settle();
  clock = startedAt + 59999; await ui.runTimers(); assert.equal(calls.length, 2);
  clock = startedAt + 60000; await ui.runTimers();
  assert.deepEqual(calls[2], { id: 'codex', began: startedAt + 60000 }, '8-second check duration must not double the refresh interval');
  clock = startedAt + 68000; pendingChecks.get('codex').resolve(); await settle();
  assert.deepEqual(calls[3], { id: 'claude', began: startedAt + 68000 }, 'the second profile keeps its own one-minute deadline');
  clock = startedAt + 72000; pendingChecks.get('claude').resolve(); await settle();
  clock = startedAt + 90000; accountRefreshButton(codex).fire('click'); await settle();
  assert.deepEqual(calls[4], { id: 'codex', began: startedAt + 90000 });
  clock = startedAt + 98000; pendingChecks.get('codex').resolve(); await settle();
  clock = startedAt + 120000; await ui.runTimers(); assert.equal(calls.length, 5, 'manual refresh moves only that profile deadline');
  clock = startedAt + 128000; await ui.runTimers();
  assert.deepEqual(calls[5], { id: 'claude', began: startedAt + 128000 });
  clock = startedAt + 132000; pendingChecks.get('claude').resolve(); await settle();
  clock = startedAt + 150000; await ui.runTimers();
  assert.deepEqual(calls[6], { id: 'codex', began: startedAt + 150000 }, 'manual phase shift still gives a predictable one-minute refresh');
  clock = startedAt + 158000; pendingChecks.get('codex').resolve(); await settle();
  codex.open = false; codex.fire('toggle'); claude.open = false; claude.fire('toggle');
  clock = startedAt + 240000; await ui.runTimers(); assert.equal(calls.length, 7);
  assert.ok(ui.posts().every((request) => request.path.endsWith('/account/refresh')));
});

test('history context menu targets the clicked task without selecting it, with keyboard navigation and reversible actions', async () => {
  const saved = withCurrentRun(snapshot({ status: 'ready_for_user' }));
  saved.collaboration = teamCollaboration({ state: 'ready_for_user' });
  const otherId = '77777777-7777-4777-8777-777777777777';
  const other = { ...saved.room, id: otherId, goal: '另一个历史任务', lifecycle_can_change: true };
  const rooms = [saved.room, other];
  const ui = await browser({ rooms, saved, route(request) {
    if (request.path === `/api/rooms/${otherId}/lifecycle`) {
      other.lifecycle_state = request.body.action === 'archive' ? 'archived' : 'active';
      return { ...snapshot(), room: other };
    }
  } });
  ui.type('当前任务未发送的草稿');
  const card = ui.get('rooms').children.find((node) => node.title === other.goal);
  const event = { prevented: false, preventDefault() { this.prevented = true; } };
  card.fire('contextmenu', event); await settle();
  assert.equal(ui.get('entry-context-menu').hidden, false);
  assert.match(ui.get('entry-context-title').textContent, /另一个历史任务/);
  assert.equal(ui.get('room-title').title, saved.room.goal, 'right click does not switch the active conversation');
  assert.equal(ui.get('message').value, '当前任务未发送的草稿');
  assert.equal(ui.posts().length, 0);
  const archive = lifecycleButton(ui.get('entry-context-actions'), { kind: 'rooms', id: otherId, action: 'archive' });
  const remove = lifecycleButton(ui.get('entry-context-actions'), { kind: 'rooms', id: otherId, action: 'remove' });
  assert.equal(archive.getAttribute('role'), 'menuitem'); assert.equal(ui.document.activeElement, archive);
  ui.document.root.fire('keydown', { key: 'ArrowDown' }); assert.equal(ui.document.activeElement, remove);
  ui.document.root.fire('keydown', { key: 'Home' }); assert.equal(ui.document.activeElement, archive);
  ui.document.root.fire('keydown', { key: 'End' }); assert.equal(ui.document.activeElement, remove);
  ui.document.root.fire('keydown', { key: 'Escape' });
  assert.equal(ui.get('entry-context-menu').hidden, true); assert.equal(ui.document.activeElement, card);
  card.fire('keydown', { key: 'F10', shiftKey: true }); await settle();
  lifecycleButton(ui.get('entry-context-actions'), { kind: 'rooms', id: otherId, action: 'archive' }).fire('click'); await settle();
  assert.equal(ui.get('entry-context-menu').hidden, true);
  assert.deepEqual(ui.posts().map((request) => [request.path, request.body]), [[`/api/rooms/${otherId}/lifecycle`, { action: 'archive' }]]);
  assert.equal(ui.get('room-title').title, saved.room.goal);
  selectLifecycleView(ui, 'rooms', 'archived');
  ui.get('rooms').firstChild.fire('contextmenu'); await settle();
  assert.equal(lifecycleButton(ui.get('entry-context-actions'), { kind: 'rooms', id: otherId, action: 'restore' }).disabled, false);
  assert.equal(descendants(ui.get('entry-context-actions')).some((node) => node.dataset.lifecycleAction === 'archive'), false);
});

test('project and partner context menus keep removal confirmation and leave native copy menus alone', async () => {
  const project = registeredProject('88888888-8888-4888-8888-888888888888', '右键项目');
  const ui = await browser({ projects: [project] });
  await ui.click('open-projects');
  const card = ui.get('projects-list').firstChild; card.focus();
  card.fire('keydown', { key: 'F10', shiftKey: true }); await settle();
  lifecycleButton(ui.get('entry-context-actions'), { kind: 'projects', id: project.id, action: 'remove' }).fire('click'); await settle();
  assert.equal(ui.get('entry-context-menu').hidden, true);
  assert.equal(ui.get('lifecycle-confirm').hidden, false);
  assert.match(ui.get('lifecycle-confirm-description').textContent, /磁盘上的项目文件和已有聊天都会保留/);
  assert.equal(ui.posts().length, 0, 'choosing remove only opens the existing confirmation');
  await ui.click('lifecycle-confirm-cancel'); assert.equal(ui.document.activeElement, card);
  await ui.click('open-partners');
  const partner = ui.get('partners-list').children.find((node) => node.dataset.partner === 'codex');
  const input = descendants(partner).find((node) => node.tagName === 'INPUT');
  partner.fire('contextmenu', { target: input }); await settle();
  assert.equal(ui.get('entry-context-menu').hidden, true, 'inputs keep the system context menu');
  ui.selectText('需要复制的工具说明'); partner.fire('contextmenu'); await settle();
  assert.equal(ui.get('entry-context-menu').hidden, true, 'selected text keeps the system copy menu');
  ui.selectText(''); partner.fire('contextmenu'); await settle();
  assert.match(ui.get('entry-context-title').textContent, /Codex/);
  assert.equal(lifecycleButton(ui.get('entry-context-actions'), { kind: 'partners', id: 'codex', action: 'archive' }).disabled, false);
  ui.document.root.fire('pointerdown', { target: ui.get('partner-name') });
  assert.equal(ui.get('entry-context-menu').hidden, true);
  partner.fire('contextmenu'); ui.document.root.fire('scroll', { target: ui.get('partners-view') });
  assert.equal(ui.get('entry-context-menu').hidden, true);
  assert.equal(ui.posts().length, 0);
});

test('context menu keeps active work guarded and closes on an authoritative capability change', async () => {
  const saved = snapshot({ status: 'completed' });
  saved.room.lifecycle_can_change = false; saved.room.lifecycle_blocked_reason = 'team_cleanup_unconfirmed';
  const ui = await browser({ rooms: [saved.room], saved });
  const card = ui.get('rooms').firstChild; card.fire('contextmenu'); await settle();
  const archive = lifecycleButton(ui.get('entry-context-actions'), { kind: 'rooms', id: roomId, action: 'archive' });
  assert.equal(archive.disabled, true); assert.match(ui.get('entry-context-actions').textContent, /清理尚未确认/);
  archive.fire('click'); await settle(); assert.equal(ui.posts().length, 0);
  saved.room.lifecycle_can_change = true; saved.room.lifecycle_blocked_reason = null; await ui.poll();
  assert.equal(ui.get('entry-context-menu').hidden, true, 'stale capabilities are never left actionable in an open menu');
  card.fire('contextmenu'); await settle();
  assert.equal(lifecycleButton(ui.get('entry-context-actions'), { kind: 'rooms', id: roomId, action: 'archive' }).disabled, false);
  ui.document.root.fire('keydown', { key: 'Escape' });
  assert.equal(ui.posts().length, 0);
});

test('a disconnected browser gives a conditional desktop recovery path without searching ports or dispatching work', async () => {
  const ui = await browser({ route(request) { if (request.method === 'GET') throw new Error('fictional offline service'); } });
  assert.equal(ui.get('send').disabled, true);
  assert.match(ui.get('notice').textContent, /连接中断或等待超时.*如果刚重启过豆窝，旧浏览器地址可能失效.*从桌面版重新打开/);
  assert.match(ui.get('composer-note').textContent, /本地服务未连接.*请先刷新.*如果刚重启过豆窝.*从桌面版重新打开/);
  assert.equal(ui.posts().length, 0);
  assert.ok(ui.requests.every((request) => request.path.startsWith('/api/')), 'recovery advice never probes other ports or services');
});

function latestResultUsage(values, observedAt = 900) {
  return { source: 'codex', scope: 'provider_reported_latest_result', job_id: jobId, observed_at: observedAt, values };
}

test('compact partner controls stay outside the form and sidebar while latest room-only Token details remain in the popup', async () => {
  const agents = [...catalog, { id: 'fourth', name: '第四位伙伴', avatar: 'F' }];
  const saved = snapshot({ agents });
  saved.members[0].usage = latestResultUsage({ input_tokens: 0, output_tokens: 0 });
  saved.members[1].usage = latestResultUsage({ input_tokens: 1234, output_tokens: 56 });
  saved.members[2].usage = { input_tokens: 99999, scope: 'unconfirmed_history' };
  const partners = agents.map((agent) => ({ ...agent, adapter: agent.id, enabled: true, monitor: { latest_usage: latestResultUsage({ input_tokens: 888888 }) },
    account: agent.id === 'codex' ? accountFixture() : null }));
  const ui = await browser({ agents, rooms: [saved.room], saved, partners, route(request) {
    if (request.path.endsWith('/account/refresh')) return { partners };
  } });
  const bar = ui.get('composer-usage-bar'); const groups = ui.get('composer-usage-items').children;
  assert.equal(groups.length, 4);
  assert.equal(descendants(ui.get('composer')).includes(bar), false);
  assert.equal(descendants(ui.get('members-panel')).includes(bar), false, 'narrow layouts do not depend on the hidden sidebar');
  assert.ok(groups.every((node) => node.children.length === 2 && node.children.every((child) => child.type === 'button')));
  assert.match(groups[0].firstChild.getAttribute('aria-label'), /Claude Code的模型与思考强度/);
  assert.match(ui.get('composer-usage-codex').textContent, /剩余 0%/, 'reported zero remains a real observation');
  assert.doesNotMatch(bar.textContent, /Token|上次检查|888,888|99,999|上下文.*%/);
  assert.equal(ui.posts().length, 0, 'painting the compact footer starts neither account checks nor models');
  for (const [id, pattern] of [['claude', /输入 Token0.*输出 Token0/], ['codex', /输入 Token1234.*输出 Token56/], ['test_peer', /历史快照，统计方式未确认.*输入 Token99999/], ['fourth', /暂无可靠|尚未取得|未提供/]]) {
    await ui.click(`composer-usage-${id}`);
    assert.match(ui.get('composer-usage-token-fields').textContent, pattern);
    assert.doesNotMatch(ui.get('composer-usage-token-fields').textContent, /888888|888,888/);
    await ui.click('composer-usage-close');
  }
  assert.ok(ui.posts().every((request) => request.path.endsWith('/account/refresh')));
});

test('composer quota distinguishes multiple account pools and missing percentages without inventing a model quota', async () => {
  const account = accountFixture();
  account.quota.windows = [
    { name: 'poolA', window_minutes: 10080, remaining_percent: 20 },
    { name: 'poolB', window_minutes: 10080, remaining_percent: 60 },
  ];
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true,
    account: agent.id === 'codex' ? account : accountFixture({ source: 'claude_auth_status', quota: { state: 'unavailable', reason: 'provider_usage_not_exposed', windows: [] } }) }));
  const ui = await browser({ partners });
  assert.match(ui.get('composer-usage-codex').textContent, /2 个额度窗口/);
  assert.equal(descendants(ui.get('composer-usage-codex')).some((node) => node.tagName === 'PROGRESS'), false);
  assert.match(ui.get('composer-usage-claude').textContent, /额度未取得/);
  assert.doesNotMatch(ui.get('composer-usage-items').textContent, /20%|60%|剩余 0%/);
  assert.equal(ui.posts().length, 0);
});

test('composer quota popup queries explicitly, shares the sidebar watcher and preserves draft, reader and keyboard focus', async () => {
  let clock = 1000000; const waiting = deferred();
  const saved = readableDelivery(); saved.members[1].usage = latestResultUsage({ input_tokens: 0, output_tokens: 9, model: 'actually-reported-model' });
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, account: null }));
  const ui = await browser({ rooms: [saved.room], saved, partners, now: () => clock, route: async (request) => {
    if (request.path === '/api/partners/codex/account/refresh') {
      await waiting.promise; partners[1].account = accountFixture(); return { partners };
    }
  } });
  ui.type('保留这份草稿'); deliveryButton(ui, '交付/说明.md').fire('click'); await settle();
  const readerHeading = ui.get('reader-body').firstChild; ui.get('reader-body').scrollTop = 219;
  const chip = ui.get('composer-usage-codex'); chip.fire('click'); await settle();
  assert.equal(ui.get('composer-usage-popover').hidden, false);
  assert.equal(ui.get('composer-usage-popover').getAttribute('role'), 'dialog');
  assert.equal(ui.document.activeElement, ui.get('composer-usage-close'));
  assert.equal(ui.posts().length, 1); assert.deepEqual(ui.posts()[0].body, {});
  const sidebar = ui.get('provider-account-codex'); sidebar.open = true; sidebar.fire('toggle'); await settle();
  assert.equal(ui.posts().length, 1, 'two visible views of the same login never duplicate an in-flight native query');
  waiting.resolve(); await settle();
  assert.match(ui.get('composer-usage-popover').textContent, /reader@example.com.*Plus/);
  assert.match(ui.get('composer-usage-token-fields').textContent, /actually-reported-model/);
  assert.match(ui.get('composer-usage-token-fields').textContent, /供应商最新一次结果快照/);
  assert.equal(ui.get('reader-body').firstChild, readerHeading); assert.equal(ui.get('reader-body').scrollTop, 219);
  assert.equal(ui.get('message').value, '保留这份草稿');
  const popupAccount = ui.get('composer-provider-account-codex');
  ui.document.root.fire('keydown', { key: 'Tab' });
  assert.equal(ui.document.activeElement.parentNode.classList.contains('compact-account-more'), true, 'CSS-hidden account summary is not a keyboard stop');
  assert.notEqual(ui.document.activeElement, popupAccount.firstChild);
  ui.get('composer-usage-close').focus(); ui.document.root.fire('keydown', { key: 'Tab', shiftKey: true });
  assert.equal(ui.document.activeElement.parentNode, ui.get('composer-usage-tokens'), 'closed detail contents are excluded from the focus cycle');
  ui.document.root.fire('keydown', { key: 'Escape' });
  assert.equal(ui.get('composer-usage-popover').hidden, true); assert.equal(ui.document.activeElement, chip);
  sidebar.open = false; sidebar.fire('toggle'); clock += 61000; await ui.poll();
  assert.equal(ui.posts().length, 1, 'closing both views stops native queries');
  chip.fire('click'); await settle();
  ui.document.root.fire('pointerdown', { target: ui.get('message') });
  assert.equal(ui.get('composer-usage-popover').hidden, true);
  assert.ok(ui.posts().every((request) => request.path === '/api/partners/codex/account/refresh'), 'quota controls never submit a room or model request');
});

function activeActivitySnapshot() {
  const saved = withCurrentRun(snapshot({ agents: catalog.slice(0, 2), status: 'running', jobs: [
    { id: jobId, provider: 'claude', kind: 'team_work', status: 'working', text: 'private dispatch: secretly search passwords' },
    { id: 'review-call', provider: 'codex', kind: 'team_review', status: 'queued', text: 'private review instruction' },
  ] }), { status: 'in_progress', can_accept: false, can_continue: false, work_mode: 'build' });
  const runId = saved.current_run.id;
  saved.events = [
    { seq: 1, kind: 'job_created', run_id: runId, job_id: jobId, sender: 'service', recipient: 'claude', created_at: 100 },
    { seq: 2, kind: 'job_claimed', run_id: runId, job_id: jobId, sender: 'claude', created_at: 105 },
    { seq: 3, kind: 'job_created', run_id: runId, job_id: 'review-call', recipient: 'codex', created_at: 110 },
  ];
  saved.members.forEach((member) => { member.status = 'working'; member.current_task = 'old raw dispatch'; });
  saved.team = { run_id: runId, phase: 'working', tasks: [
    { id: 'a', title: '实现计分规则', owner: 'claude', status: 'working', write_paths: ['score.py'] },
    { id: 'b', title: '核对边界条件', owner: 'claude', reviewer: 'codex', status: 'review' },
  ] };
  saved.collaboration = teamCollaboration(); return saved;
}

test('main activity cards bind current-run work, queue, reviewer and helper roles without parsing dispatch text', async () => {
  const saved = activeActivitySnapshot(); const ui = await browser({ rooms: [saved.room], saved, now: () => 125000 });
  assert.equal(ui.get('partner-activity').hidden, false);
  const claude = ui.get('partner-activity-claude'); const codex = ui.get('partner-activity-codex');
  assert.match(claude.firstChild.textContent, /Claude Code.*正在处理/);
  assert.doesNotMatch(claude.firstChild.textContent, /实现计分规则/);
  assert.match(claude.textContent, /正在开发「实现计分规则」/);
  assert.match(claude.textContent, /本次认领于.*距今 20秒/);
  assert.match(codex.firstChild.textContent, /Codex.*等待开始/);
  assert.match(codex.textContent, /等待开始审核「核对边界条件」/);
  assert.doesNotMatch(codex.textContent, /本次认领于/);
  assert.doesNotMatch(ui.get('partner-activity').textContent, /private|passwords|old raw dispatch|即时.*%/);
  claude.open = true; await ui.poll(); assert.equal(ui.get('partner-activity-claude'), claude); assert.equal(claude.open, true);
  saved.jobs[1].status = 'working'; saved.events.push({ seq: 4, kind: 'job_claimed', run_id: saved.current_run.id, job_id: 'review-call', sender: 'codex', created_at: 120 });
  await ui.poll(); assert.match(codex.firstChild.textContent, /正在审核/); assert.match(codex.textContent, /核对边界条件/);
  saved.jobs[1].kind = 'team_help'; saved.team.tasks[1] = { id: 'b', title: '核对边界条件', owner: 'claude', help_to: 'codex', status: 'waiting_peer' };
  await ui.poll(); assert.match(codex.firstChild.textContent, /正在协助/); assert.match(codex.textContent, /核对边界条件/);
  saved.team.tasks.push({ id: 'c', title: '另一项协助', owner: 'claude', help_to: 'codex', status: 'waiting_peer' });
  await ui.poll(); assert.match(codex.textContent, /具体任务待核对/); assert.doesNotMatch(codex.firstChild.textContent, /另一项协助/);
  assert.equal(ui.posts().length, 0);
});

test('activity cards ignore prior-run jobs and self-reported tool claims, while inspection records remain limited evidence', async () => {
  const saved = activeActivitySnapshot();
  saved.events.forEach((event) => { event.run_id = 'previous-run'; });
  saved.events.push({ ...message(4, 'claude', '我正在搜索 secret.txt'), run_id: saved.current_run.id });
  saved.team.tasks = [];
  const ui = await browser({ rooms: [saved.room], saved });
  assert.match(ui.get('partner-activity-claude').textContent, /阶段待核对/);
  assert.doesNotMatch(ui.get('partner-activity').textContent, /正在搜索|secret.txt|本次认领于|正在开发|正在审核/);
  saved.events.push({ seq: 5, kind: 'project_inspection', run_id: saved.current_run.id, job_id: jobId, sender: 'claude', created_at: 200, phase: 'work' });
  await ui.poll();
  assert.match(ui.get('partner-activity-claude').textContent, /最近记录确认查看过项目资料.*没有提供具体文件或正在使用的工具/);
  assert.match(ui.get('partner-activity-claude').firstChild.textContent, /阶段待核对/, 'a current inspection cannot reassign an old run’s job');
  assert.doesNotMatch(ui.get('partner-activity-claude').textContent, /secret.txt/);
  saved.members[0].identity_uncertain = true; await ui.poll();
  assert.match(ui.get('partner-activity-claude').firstChild.textContent, /结果待核对/);
  assert.equal(ui.posts().length, 0);
});

test('stopped current runs override stale work and needs-user states, hide dead composer controls and separate inherited files', async () => {
  const saved = readableDelivery(); saved.current_run.status = 'stopped'; saved.current_run.can_continue = false;
  saved.current_run.can_retry = true; saved.current_run.can_accept = false;
  saved.room.status = 'needs_user'; saved.collaboration = teamCollaboration({ state: 'needs_user', reason: '本轮没有可核对的交付。' });
  saved.collaboration.outcome.artifacts = saved.project.files.map(({ path, sha256 }) => ({ path, sha256 }));
  saved.jobs = [{ id: jobId, provider: 'claude', kind: 'team_work', status: 'failed', error: 'team_result_missing' }];
  saved.events = [{ seq: 1, kind: 'job_created', run_id: saved.current_run.id, job_id: jobId, recipient: 'claude', created_at: 290 },
    { seq: 2, kind: 'job_finished', run_id: saved.current_run.id, job_id: jobId, sender: 'claude', created_at: 300 }];
  saved.members[0].status = 'needs_user'; saved.members[1].status = 'working';
  const ui = await browser({ rooms: [saved.room], saved });
  assert.match(ui.get('partner-activity-claude').textContent, /本次处理未完成.*本次提交缺少必需的文件或任务结果/);
  assert.match(ui.get('partner-activity-codex').textContent, /本轮已停止/);
  assert.doesNotMatch(ui.get('partner-activity').textContent, /正在处理|等你处理|等你验收/);
  assert.equal(ui.get('composer').hidden, true); assert.equal(ui.get('cancel-room').hidden, false, 'a stopped phase without confirmed cleanup keeps Stop available');
  assert.equal(ui.get('terminal-composer-note').hidden, false); assert.match(ui.get('terminal-composer-note').textContent, /新的尝试（新会话）/);
  assert.doesNotMatch(ui.get('work-mode-note').textContent, /正在按目标推进/);
  const history = descendants(ui.get('collaboration-outcome')).find((node) => node.classList.contains('saved-file-history'));
  assert.ok(history); assert.equal(history.tagName, 'DETAILS'); assert.equal(Boolean(history.open), false);
  assert.match(history.textContent, /已保存文件（未列入本轮交付）.*不代表本轮完成了修改或交付/);
  deliveryButton(ui, '交付/说明.md').fire('click'); await settle();
  await ui.click('reader-back'); assert.equal(ui.get('composer').hidden, true, 'reading inherited files cannot revive a stopped composer');
  saved.current_run.can_retry = false; await ui.poll();
  assert.match(ui.get('terminal-composer-note').textContent, /当前不能接续这轮/);
  assert.equal(ui.document.getElementById('retry-run'), null);
  assert.equal(ui.posts().length, 0, 'viewing a failed round does not retry it');
});

test('delivered and accepted activity states are distinct and do not retain old work badges', async () => {
  const saved = activeActivitySnapshot(); saved.current_run.status = 'ready_for_user'; saved.room.status = 'ready_for_user';
  saved.collaboration = teamCollaboration({ state: 'ready_for_user' });
  const ui = await browser({ rooms: [saved.room], saved });
  assert.match(ui.get('partner-activity-claude').textContent, /本轮已交付.*等你验收/);
  assert.doesNotMatch(ui.get('partner-activity').textContent, /本次认领于|正在处理|等待开始审核/);
  saved.current_run.accepted_at = 400; saved.collaboration.outcome.state = 'accepted'; await ui.poll();
  assert.match(ui.get('partner-activity-claude').textContent, /本轮已接受.*可补充下一项要求/);
  assert.doesNotMatch(ui.get('partner-activity').textContent, /等你验收|正在处理/);
  assert.equal(ui.posts().length, 0);
});

function runtimeOptionsFixture({ checked = true } = {}) {
  return { scope: 'coordinator_owned_only', permissions: [
    { id: 'read_only', name: '只读' }, { id: 'workspace_write', name: '允许修改隔离副本' },
  ], partners: {
    claude: { state: checked ? 'ready' : 'not_checked', supported: true, catalog_source: 'claude_cli_help_aliases',
      models: checked ? [{ id: 'opus', name: 'Opus', efforts: ['low', 'high'], default_effort: 'high' }, { id: 'sonnet', name: 'Sonnet', efforts: ['medium'], default_effort: 'medium' }] : [] },
    codex: { state: checked ? 'ready' : 'not_checked', supported: true,
      models: checked ? [{ id: 'observed-codex-model', name: 'Observed model', efforts: ['low', 'xhigh'], default_effort: 'low' }] : [] },
  } };
}
async function openRuntimeSettings(ui) {
  await ui.click('runtime-settings');
}
function selectRuntime(ui, id, value) { const select = ui.get(id); select.value = value; select.fire('change'); }

test('runtime controls read cached options on open and query each tool only explicitly, then send one shared permission and independent model settings', async () => {
  let options = runtimeOptionsFixture({ checked: false });
  const saved = snapshot({ agents: catalog.slice(0, 2), status: 'running' });
  const ui = await browser({ agents: catalog.slice(0, 2), route(request) {
    if (request.path === '/api/runtime-options') return options;
    if (request.path.endsWith('/runtime/refresh')) { options = runtimeOptionsFixture(); return options; }
    if (request.path === '/api/rooms' && request.method === 'POST') return saved;
  } });
  assert.equal(ui.requests.some((request) => request.path === '/api/runtime-options'), false, 'closed settings do not even query the cache');
  await openRuntimeSettings(ui);
  assert.equal(ui.posts().length, 0, 'GET of a cached catalog never starts a CLI or model');
  assert.equal(ui.get('runtime-model-claude').children.length, 1);
  assert.equal(ui.get('runtime-effort-claude').disabled, true);
  await ui.click('runtime-refresh-claude');
  assert.deepEqual(ui.posts().map((request) => [request.path, request.body]), [['/api/partners/claude/runtime/refresh', {}]]);
  selectRuntime(ui, 'runtime-permission', 'read_only');
  selectRuntime(ui, 'runtime-model-claude', 'opus'); selectRuntime(ui, 'runtime-effort-claude', 'high');
  selectRuntime(ui, 'runtime-model-codex', 'observed-codex-model'); selectRuntime(ui, 'runtime-effort-codex', 'xhigh');
  assert.match(ui.get('runtime-settings-body').textContent, /是否可用仍取决于当前登录和订阅/);
  await ui.click('runtime-settings-close'); ui.type('共同完成一个虚构小任务'); await ui.submit();
  const create = ui.posts().find((request) => request.path === '/api/rooms'); assert.ok(create);
  assert.deepEqual(create.body.runtime_settings, { permission: 'read_only', members: {
    claude: { model: 'opus', effort: 'high' }, codex: { model: 'observed-codex-model', effort: 'xhigh' },
  } });
  assert.equal(ui.posts().length, 2);
});

test('plan runtime keeps requested build permissions while explaining the host-enforced read-only phase', async () => {
  const saved = snapshot({ agents: catalog.slice(0, 2), status: 'running' });
  const ui = await browser({ agents: catalog.slice(0, 2), route(request) {
    if (request.path === '/api/runtime-options') return runtimeOptionsFixture();
    if (request.path === '/api/rooms' && request.method === 'POST') return saved;
  } });
  await openRuntimeSettings(ui); selectRuntime(ui, 'runtime-model-claude', 'opus'); selectRuntime(ui, 'runtime-effort-claude', 'high');
  selectRuntime(ui, 'runtime-model-claude', 'sonnet'); assert.equal(ui.get('runtime-effort-claude').value, '', 'switching model clears a strength that belongs to the prior model');
  await ui.click('runtime-settings-close'); await ui.click('work-mode-plan'); await openRuntimeSettings(ui);
  assert.equal(ui.get('runtime-permission').value, 'workspace_write'); assert.equal(ui.get('runtime-permission').disabled, false);
  assert.match(ui.get('runtime-settings-note').textContent, /方案阶段强制只读/);
  await ui.click('runtime-settings-close'); await ui.click('work-mode-build'); await openRuntimeSettings(ui); assert.equal(ui.get('runtime-permission').value, 'workspace_write');
  await ui.click('runtime-settings-close'); await ui.click('work-mode-plan'); ui.type('先讨论，不改文件'); await ui.submit();
  assert.deepEqual(ui.posts()[0].body.runtime_settings, { permission: 'workspace_write', members: {
    claude: { model: 'sonnet', effort: null }, codex: { model: null, effort: null },
  } });
  assert.equal(ui.posts()[0].body.work_mode, 'plan');
});

test('active runtime settings are frozen and a later round gets its own choices without rewriting the current run', async () => {
  const saved = activeActivitySnapshot();
  saved.current_run.runtime_settings = { permission: 'workspace_write', effective_permission: 'workspace_write', members: {
    claude: { model: 'opus', effort: 'high' }, codex: { model: null, effort: null },
  } };
  const original = structuredClone(saved.current_run.runtime_settings);
  const ui = await browser({ rooms: [saved.room], saved, route(request) {
    if (request.path === '/api/runtime-options') return runtimeOptionsFixture();
    if (request.path === `/api/rooms/${roomId}/runs`) return saved;
  } });
  await openRuntimeSettings(ui);
  assert.equal(ui.get('runtime-model-claude').disabled, true); assert.equal(ui.get('runtime-permission').disabled, true);
  assert.equal(ui.get('runtime-model-claude').value, 'opus'); assert.match(ui.get('runtime-settings-note').textContent, /本轮设置已经固定/);
  selectRuntime(ui, 'runtime-model-claude', 'sonnet'); assert.deepEqual(saved.current_run.runtime_settings, original);
  assert.equal(ui.posts().length, 0);
  saved.current_run.status = 'ready_for_user'; saved.current_run.can_continue = true; saved.room.status = 'ready_for_user';
  saved.jobs.forEach((job) => { job.status = 'completed'; }); saved.collaboration = teamCollaboration({ state: 'ready_for_user' });
  await ui.poll(); assert.equal(ui.get('runtime-model-claude').disabled, false);
  selectRuntime(ui, 'runtime-model-claude', 'sonnet'); selectRuntime(ui, 'runtime-effort-claude', 'medium');
  await ui.click('runtime-settings-close'); ui.type('接着完成下一段'); await ui.submit();
  const next = ui.posts().find((request) => request.path.endsWith('/runs')); assert.ok(next);
  assert.equal(next.body.runtime_settings.members.claude.model, 'sonnet'); assert.equal(next.body.runtime_settings.members.claude.effort, 'medium');
  assert.deepEqual(saved.current_run.runtime_settings, original, 'UI never mutates frozen server settings');
});

test('runtime choices survive new-goal drafts and unsupported tools keep defaults without implicit capability requests', async () => {
  const options = runtimeOptionsFixture(); options.partners.codex = { state: 'unavailable', supported: false, models: [] };
  const saved = snapshot({ agents: catalog.slice(0, 2) });
  const ui = await browser({ agents: catalog.slice(0, 2), rooms: [saved.room], saved, selectSaved: false, route(request) {
    if (request.path === '/api/runtime-options') return options;
  } });
  await openRuntimeSettings(ui);
  selectRuntime(ui, 'runtime-model-claude', 'opus'); selectRuntime(ui, 'runtime-effort-claude', 'high'); selectRuntime(ui, 'runtime-permission', 'read_only');
  await ui.click('runtime-settings-close'); ui.type('暂存的新目标');
  assert.equal(ui.get('runtime-refresh-codex').disabled, true); assert.equal(ui.get('runtime-model-codex').children.length, 1);
  ui.get('rooms').firstChild.fire('click'); await settle(); await ui.click('new-room');
  assert.equal(ui.get('message').value, '暂存的新目标'); assert.equal(ui.get('runtime-model-claude').value, 'opus');
  assert.equal(ui.get('runtime-effort-claude').value, 'high'); assert.equal(ui.get('runtime-permission').value, 'read_only');
  await ui.poll(); assert.equal(ui.posts().length, 0, 'polling, drafts and opening tasks do not refresh native model catalogs');
});

test('a reply-only room without saved settings shows the tool defaults, not a missing record', async () => {
  // Catches: every default project room reading “权限：未记录 · 未记录模型”.
  const saved = withCurrentRun(snapshot({ status: 'running' }), { status: 'in_progress', can_continue: false, can_retry: false });
  saved.room.mode = 'project_folder';
  const ui = await browser({ rooms: [saved.room], saved, route(request) { if (request.path === '/api/runtime-options') return runtimeOptionsFixture(); } });
  await openRuntimeSettings(ui);
  assert.match(ui.get('runtime-settings-summary').textContent, /权限：副本内修改/);
  assert.doesNotMatch(ui.get('runtime-settings-body').textContent, /没有保存权限/);
});

test('a reply-only room that could move to local development does not call its permission legacy', async () => {
  // Catches: a finished default room reading “历史设置：副本内修改”.
  const saved = withCurrentRun(snapshot({ status: 'running' }), { status: 'in_progress', can_continue: false, can_retry: false, can_native_handoff: true, runtime_settings: { permission: 'workspace_write', members: {} } });
  saved.room.mode = 'project_folder';
  const ui = await browser({ rooms: [saved.room], saved, nativeProjectSupport: true, route(request) { if (request.path === '/api/runtime-options') return runtimeOptionsFixture(); } });
  await openRuntimeSettings(ui);
  assert.match(ui.get('runtime-settings-summary').textContent, /权限：副本内修改/);
  assert.doesNotMatch(ui.get('runtime-settings-summary').textContent, /历史设置/);
});

test('old frozen runs without a runtime snapshot do not claim an observed permission or model', async () => {
  const saved = withCurrentRun(snapshot({ status: 'running' }), { status: 'in_progress', can_continue: false, can_retry: false });
  const ui = await browser({ rooms: [saved.room], saved, route(request) { if (request.path === '/api/runtime-options') return runtimeOptionsFixture(); } });
  await openRuntimeSettings(ui);
  assert.match(ui.get('runtime-settings-summary').textContent, /权限：未记录/);
  assert.match(ui.get('runtime-settings-body').textContent, /没有保存权限、模型或思考强度.*无法确认/);
  assert.equal(ui.document.getElementById('runtime-permission'), null);
  assert.equal(ui.posts().length, 0);
});

test('runtime settings use an external bounded dialog instead of expanding the composer layout, with safe dismissal and focus', async () => {
  const ui = await browser({ agents: catalog.slice(0, 2), route(request) { if (request.path === '/api/runtime-options') return runtimeOptionsFixture(); } });
  ui.type('保留的长目标。'.repeat(80)); const draft = ui.get('message').value; ui.get('timeline').scrollTop = 159;
  const trigger = ui.get('runtime-settings'); const dialog = ui.get('runtime-settings-popover'); const shell = ui.document.querySelector('.app-shell');
  assert.equal(trigger.tagName, 'BUTTON'); assert.equal(trigger.getAttribute('type'), 'button');
  assert.equal(descendants(trigger).includes(ui.get('runtime-settings-body')), false, 'the one-line trigger never contains expanded content');
  assert.equal(descendants(shell).includes(dialog), false, 'settings cannot consume flex space or push the permanent quota bar down');
  assert.equal(dialog.getAttribute('aria-modal'), 'true');
  await openRuntimeSettings(ui);
  assert.equal(dialog.hidden, false); assert.equal(shell.inert, true); assert.equal(trigger.getAttribute('aria-expanded'), 'true');
  assert.equal(ui.document.activeElement.id, 'runtime-permission');
  ui.get('runtime-settings-close').focus();
  ui.document.root.fire('keydown', { key: 'Tab' }); assert.equal(ui.document.activeElement.id, 'runtime-permission');
  selectRuntime(ui, 'runtime-permission', 'read_only'); selectRuntime(ui, 'runtime-model-claude', 'opus'); selectRuntime(ui, 'runtime-effort-claude', 'high');
  ui.get('runtime-settings-close').focus(); ui.document.root.fire('keydown', { key: 'Tab', shiftKey: true });
  assert.equal(ui.document.activeElement.id, 'runtime-permission', 'the task permission dialog does not tab into hidden partner cards');
  ui.document.root.fire('keydown', { key: 'Tab' }); assert.equal(ui.document.activeElement, ui.get('runtime-settings-close'));
  dialog.fire('pointerdown', { target: ui.get('runtime-settings-body') }); assert.equal(dialog.hidden, false, 'clicking inside does not dismiss');
  ui.document.root.fire('keydown', { key: 'Escape' });
  assert.equal(dialog.hidden, true); assert.equal(shell.inert, false); assert.equal(ui.document.activeElement, trigger);
  assert.equal(trigger.getAttribute('aria-expanded'), 'false');
  await openRuntimeSettings(ui); assert.equal(ui.get('runtime-model-claude').value, 'opus'); assert.equal(ui.get('runtime-effort-claude').value, 'high');
  assert.equal(ui.get('runtime-permission').value, 'read_only');
  dialog.fire('pointerdown'); assert.equal(dialog.hidden, true); assert.equal(shell.inert, false); assert.equal(ui.document.activeElement, trigger);
  assert.equal(ui.get('message').value, draft); assert.equal(ui.get('timeline').scrollTop, 159);
  assert.equal(ui.posts().length, 0, 'opening, choosing and dismissing settings neither validates the form nor dispatches native queries or tasks');
});

function availabilityFixture(overrides = {}) {
  return { state: 'unknown', selectable: true, reason: 'quota_unknown', source: 'provider_account', observed_at: 1000,
    resets_at: null, stale: false, scope: 'default_cli_login', identity_checked_at: 1000, ...overrides };
}
function availabilityPartners(overrides = {}) {
  return catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, availability: availabilityFixture(overrides[agent.id]) }));
}
function participantInput(ui, id) { return ui.document.querySelectorAll('[data-participant]').find((node) => node.dataset.participant === id); }

test('quota availability refreshes at startup and by visible deadlines without model calls, then stops hidden and refreshes on wake', async () => {
  let clock = 1000000; const partners = availabilityPartners(); let checks = 0;
  const ui = await browser({ agents: catalog.slice(0, 2), partners, now: () => clock, route(request) {
    if (request.path === '/api/availability/refresh') {
      checks += 1; partners.forEach((partner) => { partner.availability.observed_at = clock / 1000; });
      return { partners, supported_adapters: [{ id: 'claude' }, { id: 'codex' }] };
    }
  } });
  await settle(); assert.equal(checks, 1, 'a cached availability contract enables one bounded batch on startup');
  assert.ok(ui.posts().every((request) => request.path === '/api/availability/refresh'));
  assert.equal(ui.get('composer-usage-popover').hidden, true, 'closed account popups do not stop availability monitoring');
  clock += 59999; await ui.runTimers(); assert.equal(checks, 1);
  clock += 1; await ui.runTimers(); assert.equal(checks, 2);
  ui.document.hidden = true; ui.document.root.fire('visibilitychange'); clock += 90000; await ui.poll(); assert.equal(checks, 2);
  ui.document.hidden = false; ui.document.root.fire('visibilitychange'); await settle(); assert.equal(checks, 3);
  assert.match(ui.get('composer-usage-claude').title, /当前接入还没查到额度.*上次检查/);
  assert.equal(participantInput(ui, 'claude').disabled, false, 'missing quota information never becomes a false block');
});

test('a newly blocked selected partner remains selected and removable while sending is guarded instead of shrinking the team', async () => {
  const partners = availabilityPartners(); let blocked = false; const saved = snapshot({ agents: [catalog[0]] });
  const ui = await browser({ agents: catalog.slice(0, 2), partners, route(request) {
    if (request.path === '/api/availability/refresh') return { partners, supported_adapters: [{ id: 'claude' }, { id: 'codex' }] };
    if (request.path === '/api/rooms' && request.method === 'POST') return saved;
  } });
  await settle(); ui.type('双方一起完成这项工作'); assert.equal(ui.get('send').disabled, false);
  partners[1].availability = availabilityFixture({ state: 'blocked', selectable: false, reason: 'quota_exhausted', stale: true, resets_at: 3000 });
  await ui.poll();
  assert.equal(participantInput(ui, 'codex').checked, true); assert.equal(participantInput(ui, 'codex').disabled, false);
  assert.match(ui.get('participant-count').textContent, /2 已选/); assert.equal(ui.get('send').disabled, true);
  assert.equal(ui.get('message').disabled, false, 'users may keep drafting while they adjust the team');
  assert.match(ui.get('composer-note').textContent, /Codex.*额度已用完.*取消勾选/);
  assert.match(ui.get('composer-usage-codex').title, /额度已用完.*旧快照.*预计重置/);
  // One plain line on the card, in the ordinary waiting style; the full record is under 账号与额度.
  assert.match(ui.get('availability-codex').textContent, /^额度用完 · .+恢复$/);
  assert.equal(ui.get('availability-codex').classList.contains('waiting'), true);
  assert.equal(ui.get('availability-codex').classList.contains('blocked'), false, 'running out of quota is not an error');
  assert.match(ui.get('provider-account-codex').textContent, /供应商共享额度已用完/);
  const before = ui.posts().length; await ui.submit(); assert.equal(ui.posts().length, before, 'form submission cannot bypass the quota guard');
  await ui.invite('codex', false); assert.equal(participantInput(ui, 'codex').disabled, true);
  assert.match(ui.get('participant-count').textContent, /1 已选/); assert.equal(ui.get('send').disabled, false);
  await ui.invite('codex', true); assert.equal(participantInput(ui, 'codex').checked, false, 'blocked additions are rejected even by a synthetic stale change event');
  assert.match(ui.get('participant-count').textContent, /1 已选/);
  await ui.submit(); const create = ui.posts().find((request) => request.path === '/api/rooms');
  assert.deepEqual(create.body.participants, ['claude'], 'only the user action removes a member from a new team');
});

test('initially blocked quota is excluded from defaults but remains visible, unknown quota and login alone are not availability promises', async () => {
  const partners = availabilityPartners({ codex: { state: 'blocked', selectable: false, reason: 'subscription_limit_reached' } });
  partners[0].account = accountFixture({ source: 'claude_auth_status', quota: { state: 'unavailable', windows: [], reason: 'provider_usage_not_exposed' } });
  const ui = await browser({ agents: catalog.slice(0, 2), partners, route(request) {
    if (request.path === '/api/availability/refresh') return { partners, supported_adapters: [{ id: 'claude' }, { id: 'codex' }] };
  } });
  await settle();
  assert.equal(participantInput(ui, 'codex').checked, false); assert.equal(participantInput(ui, 'codex').disabled, true);
  assert.equal(participantInput(ui, 'claude').checked, true); assert.equal(participantInput(ui, 'claude').disabled, false);
  assert.match(ui.get('participant-picker').textContent, /额度已用完/);
  await ui.click('open-partners'); assert.match(ui.get('partners-list').textContent, /工具已报告订阅额度用完/);
  assert.match(ui.get('partners-list').textContent, /当前接入还没查到额度/);
  assert.doesNotMatch(ui.get('partners-list').textContent, /当前登录.*额度充足/);
  assert.ok(ui.posts().every((request) => request.path === '/api/availability/refresh'));
});

test('quota changes never erase frozen room members or block acceptance and reading, while retry is kept behind the availability guard', async () => {
  const saved = readableDelivery(); saved.current_run.can_retry = true;
  const partners = availabilityPartners({ codex: { state: 'blocked', selectable: false, reason: 'quota_exhausted' } });
  const ui = await browser({ agents: catalog.slice(0, 2), rooms: [saved.room], saved, partners, route(request) {
    if (request.path === '/api/availability/refresh') return { partners, supported_adapters: [{ id: 'claude' }, { id: 'codex' }] };
  } });
  await settle(); assert.deepEqual(ui.members(), ['member-claude', 'member-codex']);
  assert.equal(ui.get('accept-run').disabled, false); assert.equal(ui.get('retry-run').disabled, true);
  deliveryButton(ui, '交付/说明.md').fire('click'); await settle(); assert.equal(ui.get('delivery-reader').hidden, false);
  const heading = ui.get('reader-body').firstChild; ui.get('reader-body').scrollTop = 226;
  await ui.poll(); assert.equal(ui.get('reader-body').firstChild, heading); assert.equal(ui.get('reader-body').scrollTop, 226);
  assert.deepEqual(ui.members(), ['member-claude', 'member-codex']);
  assert.ok(ui.posts().every((request) => request.path === '/api/availability/refresh'));
});

test('a completed plan carries requested permissions into build without elevating an explicitly read-only choice', async () => {
  for (const requested of ['workspace_write', 'read_only']) {
    const planned = withCurrentRun(snapshot({ agents: catalog.slice(0, 2), status: 'ready_for_user' }), { work_mode: 'plan', runtime_settings: {
      permission: requested, effective_permission: 'read_only', members: { claude: { model: null, effort: null }, codex: { model: null, effort: null } },
    } });
    planned.room.mode = 'text_artifacts'; planned.collaboration = teamCollaboration({ state: 'ready_for_user' });
    const ui = await browser({ agents: catalog.slice(0, 2), taskModes: ['text', 'text_artifacts'], route(request) {
      if (request.path === '/api/runtime-options') return runtimeOptionsFixture();
      if (request.method === 'POST' && (request.path === '/api/rooms' || request.path.endsWith('/runs'))) return planned;
    } });
    await ui.click('work-mode-plan'); await openRuntimeSettings(ui);
    if (requested === 'read_only') selectRuntime(ui, 'runtime-permission', 'read_only');
    await ui.click('runtime-settings-close'); ui.type('先做方案，确认后继续开发'); await ui.submit();
    const create = ui.posts()[0]; assert.equal(create.body.work_mode, 'plan');
    assert.equal(create.body.runtime_settings.permission, requested);
    assert.equal(create.body.runtime_settings.effective_permission, undefined, 'the client does not control the effective host permission');
    assert.equal(planned.current_run.runtime_settings.effective_permission, 'read_only');
    await ui.click('build-from-plan');
    const build = ui.posts().find((request) => request.path.endsWith('/runs')); assert.ok(build);
    assert.equal(build.body.work_mode, 'build'); assert.equal(build.body.runtime_settings.permission, requested);
    assert.equal(build.body.runtime_settings.effective_permission, undefined);
    assert.equal(ui.posts().length, 2, 'one explicit plan submission and one explicit build action, without retries');
  }
});

function availabilityRecheckButton(root, id) { return descendants(root).find((node) => node.dataset.availabilityRecheck === id); }

test('quota recheck needs explicit confirmation, downgrades shared aliases to unknown and never enables disabled tools or starts tasks', async () => {
  const agents = [...catalog.slice(0, 2), { id: 'claude_alias', name: 'Claude 第二会话', adapter: 'claude', avatar: 'C' }];
  const blocked = availabilityFixture({ state: 'blocked', selectable: false, reason: 'subscription_limit_reached', can_request_recheck: true });
  const partners = agents.map((agent) => ({ ...agent, adapter: agent.adapter || agent.id, enabled: agent.id !== 'claude_alias',
    availability: agent.id === 'codex' ? availabilityFixture() : { ...blocked } }));
  const ui = await browser({ agents, partners, now: () => 1000000, route(request) {
    if (request.path === '/api/availability/refresh') return { partners, supported_adapters: [{ id: 'claude' }, { id: 'codex' }] };
    if (request.path === '/api/partners/claude/availability/recheck') {
      for (const partner of partners.filter((entry) => entry.adapter === 'claude')) partner.availability = availabilityFixture({ source: 'user_recheck_requested', can_request_recheck: false });
      return { partners, supported_adapters: [{ id: 'claude' }, { id: 'codex' }], availability_event: { kind: 'quota_recheck_requested', adapter: 'claude', observed_at: 1000 } };
    }
  } });
  await settle(); await ui.click('open-partners');
  let action = availabilityRecheckButton(ui.get('partners-list'), 'claude'); assert.ok(action); assert.equal(action.disabled, false);
  action.fire('click'); await settle();
  assert.equal(ui.get('availability-recheck-dialog').hidden, false); assert.equal(ui.document.querySelector('.app-shell').inert, true);
  assert.match(ui.get('availability-recheck-description').textContent, /共用本机 CLI 登录.*尚未确认.*不表示.*已经恢复/);
  assert.equal(ui.document.activeElement, ui.get('availability-recheck-cancel'));
  ui.document.root.fire('keydown', { key: 'Tab' }); assert.equal(ui.document.activeElement, ui.get('availability-recheck-submit'));
  ui.document.root.fire('keydown', { key: 'Escape' });
  assert.equal(ui.get('availability-recheck-dialog').hidden, true); assert.equal(ui.document.querySelector('.app-shell').inert, true, 'the underlying manager keeps the chat inert'); assert.equal(ui.get('workspace-manager').inert, false);
  assert.equal(ui.posts().filter((request) => request.path.endsWith('/availability/recheck')).length, 0, 'cancel is a pure UI action');
  action = availabilityRecheckButton(ui.get('partners-list'), 'claude'); action.fire('click'); await settle();
  await ui.click('availability-recheck-submit');
  assert.equal(ui.get('availability-recheck-dialog').hidden, true); assert.equal(ui.document.querySelector('.app-shell').inert, true, 'the underlying manager keeps the chat inert'); assert.equal(ui.get('workspace-manager').inert, false);
  assert.equal(partners[0].availability.state, 'unknown'); assert.equal(partners[2].availability.state, 'unknown'); assert.equal(partners[2].enabled, false);
  assert.equal(participantInput(ui, 'claude').disabled, false); assert.equal(participantInput(ui, 'claude_alias').disabled, true);
  assert.match(ui.get('manager-notice').textContent, /额度仍未确认.*仍可能遇到限额.*没有启动模型/);
  assert.deepEqual(ui.posts().filter((request) => request.path.endsWith('/availability/recheck')).map((request) => [request.path, request.body]), [['/api/partners/claude/availability/recheck', {}]]);
  assert.ok(ui.posts().every((request) => request.path === '/api/availability/refresh' || request.path.endsWith('/availability/recheck')));
});

test('quota recheck is unavailable for unknown, unsupported and expired identity checks even if an old cached capability remains', async () => {
  const partners = availabilityPartners({ claude: { state: 'blocked', selectable: false, reason: 'subscription_limit_reached', can_request_recheck: true },
    codex: { state: 'blocked', selectable: false, reason: 'quota_exhausted', can_request_recheck: false } });
  const ui = await browser({ agents: catalog.slice(0, 2), partners, now: () => 1091000, route(request) {
    if (request.path === '/api/availability/refresh') return { partners, supported_adapters: [{ id: 'claude' }, { id: 'codex' }] };
  } });
  await settle(); await ui.click('open-partners');
  const action = availabilityRecheckButton(ui.get('partners-list'), 'claude'); assert.ok(action); assert.equal(action.disabled, true);
  assert.match(action.title, /登录核查记录已过期/);
  assert.equal(availabilityRecheckButton(ui.get('partners-list'), 'codex'), undefined);
  assert.match(ui.get('partners-list').textContent, /旧快照/);
  action.fire('click'); assert.equal(ui.get('availability-recheck-dialog').hidden, true);
  partners[0].availability = availabilityFixture({ can_request_recheck: true }); await ui.poll();
  assert.equal(availabilityRecheckButton(ui.get('partners-list'), 'claude'), undefined, 'unknown status is never a request to clear a block');
});

test('availability batch deadlines use query start times and never double the minute after asynchronous checks', async () => {
  let clock = 1000000; let checks = 0; let pending = deferred(); const partners = availabilityPartners();
  const ui = await browser({ agents: catalog.slice(0, 2), partners, now: () => clock, route: async (request) => {
    if (request.path === '/api/availability/refresh') {
      checks += 1; const wait = pending; await wait.promise;
      partners.forEach((partner) => { partner.availability.observed_at = clock / 1000; });
      return { partners, supported_adapters: [{ id: 'claude' }, { id: 'codex' }] };
    }
  } });
  assert.equal(checks, 1); clock += 8000; pending.resolve(); await settle(); pending = deferred();
  clock = 1059999; await ui.runTimers(); assert.equal(checks, 1);
  clock = 1060000; await ui.runTimers(); assert.equal(checks, 2, 'an eight-second check still refreshes at the next minute start');
  clock += 8000; pending.resolve(); await settle();
  ui.document.hidden = true; ui.document.root.fire('visibilitychange'); clock += 120000; await ui.runTimers(); assert.equal(checks, 2);
});

test('an uncertain quota recheck recovers cached observations without replaying the action', async () => {
  const partners = availabilityPartners({ claude: { state: 'blocked', selectable: false, reason: 'subscription_limit_reached', can_request_recheck: true } });
  let rechecks = 0;
  const ui = await browser({ agents: catalog.slice(0, 2), partners, now: () => 1000000, route(request) {
    if (request.path === '/api/availability/refresh') return { partners, supported_adapters: [{ id: 'claude' }, { id: 'codex' }] };
    if (request.path === '/api/partners/claude/availability/recheck') {
      rechecks += 1; partners[0].availability = availabilityFixture({ source: 'user_recheck_requested', can_request_recheck: false });
      throw new Error('fictional lost response after accepting the metadata change');
    }
  } });
  await settle(); await ui.click('open-partners'); availabilityRecheckButton(ui.get('partners-list'), 'claude').fire('click'); await settle();
  await ui.click('availability-recheck-submit');
  assert.equal(rechecks, 1); assert.equal(ui.get('availability-recheck-dialog').hidden, false);
  assert.match(ui.get('availability-recheck-error').textContent, /结果尚未确认.*不会自动重复/);
  assert.equal(ui.get('availability-recheck-submit').disabled, true, 'the recovered unknown observation no longer permits a second clear');
  await ui.poll(); assert.equal(rechecks, 1);
  ui.document.root.fire('keydown', { key: 'Escape' }); assert.equal(ui.document.querySelector('.app-shell').inert, true, 'the underlying manager keeps the chat inert'); assert.equal(ui.get('workspace-manager').inert, false);
  assert.ok(ui.posts().every((request) => request.path === '/api/availability/refresh' || request.path.endsWith('/availability/recheck')));
});

test('an account identity block preserves a recorded limit without claiming the unknown current account is exhausted', async () => {
  const partners = availabilityPartners();
  const ui = await browser({ agents: catalog.slice(0, 2), partners, route(request) {
    if (request.path === '/api/availability/refresh') return { partners, supported_adapters: [{ id: 'claude' }, { id: 'codex' }] };
  } });
  await settle(); ui.type('先保留这个双人任务');
  partners[1].availability = availabilityFixture({ state: 'blocked', selectable: false, reason: 'account_identity_unknown' });
  await ui.poll();
  assert.equal(participantInput(ui, 'codex').checked, true); assert.equal(ui.get('send').disabled, true);
  assert.match(ui.get('composer-note').textContent, /Codex：当前登录待确认/);
  assert.match(ui.get('composer-usage-codex').title, /当前登录待确认.*之前的额度限制仍保留.*刷新账号/);
  assert.doesNotMatch(ui.get('composer-note').textContent, /额度已用完/);
  assert.doesNotMatch(ui.get('composer-usage-codex').textContent, /额度已用完/);
  assert.equal(availabilityRecheckButton(ui.get('provider-account-codex'), 'codex'), undefined);
  assert.ok(ui.posts().every((request) => request.path === '/api/availability/refresh'));
});

test('an old limit can be manually reallowed with a fresh identity check while missing, expired or future identity times stay blocked', async () => {
  const partners = availabilityPartners({ claude: { state: 'blocked', selectable: false, reason: 'subscription_limit_reached',
    can_request_recheck: true, observed_at: 900, identity_checked_at: 1099, stale: true } });
  const ui = await browser({ agents: catalog.slice(0, 2), partners, now: () => 1100000, route(request) {
    if (request.path === '/api/availability/refresh') return { partners, supported_adapters: [{ id: 'claude' }, { id: 'codex' }] };
  } });
  await settle(); await ui.click('open-partners');
  let action = availabilityRecheckButton(ui.get('partners-list'), 'claude'); assert.ok(action);
  assert.equal(action.disabled, false, 'old limit age does not permanently lock a newly confirmed login');
  assert.match(ui.get('partners-list').textContent, /旧快照.*登录核查/);
  assert.equal(partners[0].availability.observed_at, 900); assert.equal(partners[0].availability.stale, true);
  action.fire('click'); await settle(); assert.equal(ui.get('availability-recheck-submit').disabled, false);
  await ui.click('availability-recheck-cancel');
  for (const timestamp of [null, undefined, '1099', NaN, 1101, 1009.99]) {
    partners[0].availability.identity_checked_at = timestamp; await ui.poll();
    action = availabilityRecheckButton(ui.get('partners-list'), 'claude'); assert.ok(action); assert.equal(action.disabled, true, `invalid identity time ${String(timestamp)} is not a current login check`);
    action.fire('click'); assert.equal(ui.get('availability-recheck-dialog').hidden, true);
  }
  partners[0].availability.identity_checked_at = 1010; await ui.poll();
  assert.equal(availabilityRecheckButton(ui.get('partners-list'), 'claude').disabled, false, 'a confirmed identity exactly 90 seconds old remains within the backend window');
  assert.ok(ui.posts().every((request) => request.path === '/api/availability/refresh'), 'eligibility changes alone never clear the limit or start inference');
});

function inheritedDeliveryFixture(workMode) {
  const saved = withCurrentRun(snapshot({ agents: catalog.slice(0, 2), status: 'ready_for_user' }), { work_mode: workMode, number: 2, revision: 3 });
  saved.room.mode = 'text_artifacts';
  saved.project = { available: true, revision: 3, snapshot_sha256: 'f'.repeat(64), files: Array.from({ length: 4 }, (_, index) => ({
    path: `old-${index}.py`, owner: 'claude', content: `# previously saved ${index}`, sha256: String(index + 1).repeat(64),
  })) };
  const tasks = ['claude', 'codex'].map((owner, index) => ({ id: `next-${index}`, title: `本轮任务 ${index}`, owner, status: 'accepted', result: `# 本轮方案 ${index}\n这是当前轮的答复。`, result_files: [] }));
  if (workMode === 'build') {
    tasks.forEach((task, index) => {
      const file = { path: `new-${index}.py`, owner: task.owner, content: `# newly delivered ${index}`, sha256: String(index + 5).repeat(64) };
      saved.project.files.push(file); task.result_files = [{ path: file.path, sha256: file.sha256 }];
    });
  }
  saved.team = { run_id: saved.current_run.id, phase: 'ready_for_user', tasks };
  saved.collaboration = teamCollaboration({ state: 'ready_for_user', tasks: tasks.map(({ result_files, ...task }) => task) });
  saved.collaboration.outcome.artifacts = saved.project.files.map(({ path, sha256 }) => ({ path, sha256 }));
  return saved;
}
function deliverySections(ui) {
  const groups = ui.get('collaboration-outcome').children.filter((node) => node.classList.contains('delivery-content'));
  return { current: groups.find((node) => !node.classList.contains('saved-file-history')), saved: groups.find((node) => node.classList.contains('saved-file-history')) };
}

test('a ready plan lists its two current answers while four inherited files remain readable as saved inventory', async () => {
  const saved = inheritedDeliveryFixture('plan'); const ui = await browser({ rooms: [saved.room], saved });
  const groups = deliverySections(ui); assert.ok(groups.current); assert.ok(groups.saved);
  assert.match(groups.current.textContent, /方案与答复/);
  assert.equal(descendants(groups.current).filter((node) => node.classList.contains('delivery-file')).length, 2);
  assert.doesNotMatch(groups.current.textContent, /old-\d\.py/);
  assert.match(groups.saved.firstChild.textContent, /已保存文件（未列入本轮交付）/);
  assert.equal(descendants(groups.saved).filter((node) => node.classList.contains('delivery-file')).length, 4);
  assert.equal(Boolean(groups.saved.open), false);
  assert.match(ui.get('collaboration-outcome').textContent, /工作区文件清单（不代表本轮交付）/);
  deliveryButton(ui, 'old-0.py').fire('click'); await settle(); assert.match(ui.get('reader-body').textContent, /previously saved 0/);
  assert.equal(ui.posts().length, 0);
});

test('a ready build lists only its exact two current file bindings and keeps inherited or mismatched inventory separate', async () => {
  const saved = inheritedDeliveryFixture('build'); const ui = await browser({ rooms: [saved.room], saved });
  let groups = deliverySections(ui);
  let currentPaths = descendants(groups.current).filter((node) => node.classList.contains('delivery-file')).map((node) => node.dataset.documentPath);
  assert.deepEqual(currentPaths, ['new-0.py', 'new-1.py']);
  assert.equal(descendants(groups.saved).filter((node) => node.classList.contains('delivery-file')).length, 4);
  assert.doesNotMatch(groups.current.textContent, /old-\d\.py/);
  saved.team.tasks[1].result_files[0].sha256 = 'e'.repeat(64);
  await ui.poll(); groups = deliverySections(ui);
  currentPaths = descendants(groups.current).filter((node) => node.classList.contains('delivery-file')).map((node) => node.dataset.documentPath);
  assert.equal(currentPaths.includes('new-0.py'), true); assert.equal(currentPaths.includes('new-1.py'), false, 'a matching path alone cannot assert current delivery');
  assert.equal(descendants(groups.saved).filter((node) => node.classList.contains('delivery-file')).length, 5);
  assert.equal(ui.posts().length, 0);
});

test('quota percentages are shown to one decimal, never as floating-point noise', async () => {
  // Catches: "42.00000000000001%" when remaining was derived from a provider's used_percent.
  const account = claudeUsageAccount([
    { name: 'claude:five_hour', label: 'Claude · 5小时窗口', scope: 'provider_account', kind: 'session', window_minutes: 300, used_percent: 57.99999999999999, resets_at: 3000 },
    { name: 'claude:seven_day', label: 'Claude · 7天窗口', scope: 'provider_account', kind: 'weekly_all', window_minutes: 10080, used_percent: 99.98, remaining_percent: 0.02, resets_at: 9000 },
  ]);
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, account: agent.id === 'claude' ? account : null }));
  const ui = await browser({ partners });
  const footer = ui.get('provider-account-claude');
  assert.match(footer.textContent, /Claude · 5小时窗口.*已用 58%/);
  assert.match(footer.textContent, /Claude · 7天窗口.*剩余 0.1%/, 'a little left is not shown as none');
  assert.doesNotMatch(footer.textContent, /\d\.\d{2,}%/);
  assert.deepEqual(descendants(footer).filter((node) => node.tagName === 'PROGRESS').map((node) => node.value), [58, 0.1]);
});

function claudeUsageAccount(windows) {
  return accountFixture({ source: 'claude_auth_status', auth_method: 'subscription', plan: 'ultra',
    quota: { state: 'available', scope: 'provider_account_and_model', source: 'claude_sdk_get_usage', checked_at: 1000, stale: false, reason: null, windows } });
}

test('Claude quota renders native shared and model windows separately and uses only the shared week for the bottom summary', async () => {
  const account = claudeUsageAccount([
    { name: 'claude:five_hour', label: 'Claude · 5小时窗口', scope: 'provider_account', kind: 'session', window_minutes: 300, used_percent: 8, remaining_percent: 92, resets_at: 3000 },
    { name: 'claude:seven_day', label: 'Claude · 7天窗口', scope: 'provider_account', kind: 'weekly_all', window_minutes: 10080, used_percent: 13, remaining_percent: 87, resets_at: 9000 },
    { name: 'claude:model_scoped:0', label: 'Fable · 模型周额度', scope: 'model', model: 'Fable', kind: 'weekly_scoped', window_minutes: 10080, used_percent: 42.5, remaining_percent: 57.5, resets_at: 9000 },
  ]);
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, account: agent.id === 'claude' ? account : null }));
  const ui = await browser({ partners });
  const footer = ui.get('provider-account-claude');
  assert.match(footer.textContent, /账号共享与模型专属额度分别列示，不能相加/);
  assert.match(footer.textContent, /Claude · 5小时窗口.*剩余 92%/);
  assert.match(footer.textContent, /Claude · 7天窗口.*剩余 87%/);
  assert.match(footer.textContent, /Fable · 模型周额度.*剩余 57.5%.*模型专属 · Fable/);
  assert.match(footer.textContent, /额度来源：Claude Code 原生额度查询/);
  assert.doesNotMatch(footer.textContent, /claude:five_hour|claude:model_scoped/);
  assert.deepEqual(descendants(footer).filter((node) => node.tagName === 'PROGRESS').map((node) => node.value), [92, 87, 57.5]);
  const chip = ui.get('composer-usage-claude'); assert.match(chip.textContent, /剩余 87%/);
  assert.doesNotMatch(chip.textContent, /57.5%|Fable/);
  assert.equal(chip.getAttribute('aria-label').includes('剩余 87%'), true);
  assert.equal(ui.posts().length, 0, 'rendering cached native quota starts neither inference nor a native query');
});

test('model-scoped quota is never presented as the overall account balance', async () => {
  const account = claudeUsageAccount([{ name: 'claude:model_scoped:0', label: 'Fable · 模型周额度', scope: 'model', model: 'Fable',
    kind: 'weekly_scoped', window_minutes: 10080, used_percent: 100, remaining_percent: 0 }]);
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, account: agent.id === 'claude' ? account : null }));
  const ui = await browser({ partners }); const chip = ui.get('composer-usage-claude');
  assert.match(chip.textContent, /1 个模型额度窗口/); assert.doesNotMatch(chip.textContent, /剩余 0%/);
  assert.equal(descendants(chip).some((node) => node.tagName === 'PROGRESS'), false);
  assert.match(ui.get('provider-account-claude').textContent, /模型专属 · Fable/);
  assert.equal(participantInput(ui, 'claude').disabled, false, 'UI does not infer an account-wide block from a single model pool');
  assert.equal(ui.posts().length, 0);
});

test('quota fallback describes the current integration and promises continued use only for actually selectable partners', async () => {
  const saved = snapshot({ agents: catalog.slice(0, 2) });
  for (const status of ['ready', 'disabled', 'missing', 'archived']) {
    const partner = { ...catalog[0], adapter: 'claude', enabled: status !== 'disabled', selectable: status === 'ready',
      installation: { state: status === 'missing' ? 'missing' : 'found' }, lifecycle_state: status === 'archived' ? 'archived' : 'active',
      account: claudeUsageAccount([]) };
    partner.account.quota = { ...partner.account.quota, state: 'unavailable', reason: 'provider_usage_not_exposed' };
    const ui = await browser({ agents: catalog.slice(0, 2), partners: [partner, { ...catalog[1], adapter: 'codex', enabled: true }], rooms: [saved.room], saved });
    const chip = ui.get('composer-usage-claude'); assert.match(chip.textContent, /额度未取得/);
    assert.equal(chip.getAttribute('aria-label').includes('仍可使用'), status === 'ready', `${status} is not inferred to be usable from unknown quota alone`);
    assert.match(ui.get('provider-account-claude').textContent, /当前接入尚未取得订阅额度/);
    assert.doesNotMatch(ui.get('provider-account-claude').textContent, /这个工具没有提供|Claude.*不支持额度|无法提前预警/);
    assert.equal(ui.posts().length, 0);
  }
});

test('an account down to its reserve says others go first, on the room line and its account panel', async () => {
  // Catches: the reserve flag computed on the server but never shown, shown on the wrong partner, or English copy absent.
  const saved = snapshot({ agents: catalog.slice(0, 2) });
  saved.workload = { configured: false, accounts: [
    { scope: 'claude', members: ['claude'], share: 2, level: 'normal', points: 600, calls: 2, target_percent: 50, actual_percent: 60 },
    { scope: 'codex', members: ['codex'], share: 0, level: 'idle', points: 400, calls: 2, target_percent: 0, actual_percent: 40 },
  ] };
  const partners = [{ ...catalog[0], adapter: 'claude', enabled: true, selectable: true, workload_level: 'normal', account: claudeUsageAccount([]), quota_reserve: null },
    { ...catalog[1], adapter: 'codex', enabled: true, selectable: true, workload_level: 'normal', quota_reserve: 9 }];
  const ui = await browser({ agents: catalog.slice(0, 2), partners, rooms: [saved.room], saved });
  assert.equal(ui.get('member-workload-codex').textContent, '角色：全能 · 本轮工作量 40% · 额度只剩约 9%，先派给别人');
  assert.equal(ui.get('member-workload-claude').textContent, '角色：全能 · 本轮工作量 60%');
  assert.match(ui.get('provider-account-codex').textContent, /额度只剩约 9%，豆窝先把活派给其他伙伴，没人能接时才用它。/);
  assert.doesNotMatch(ui.get('provider-account-claude').textContent, /额度只剩约/);
  const english = await browser({ language: 'en', agents: catalog.slice(0, 2), partners, rooms: [saved.room], saved });
  assert.equal(english.get('member-workload-codex').textContent, 'Role: All-round · This round 40% of the work · About 9% quota left, others go first');
});

test('each account shows its part of the round and its workload level can be changed', async () => {
  // Catches: the percentages missing or attached to the wrong partner, a setting that never reaches the server, or English copy absent.
  const saved = snapshot({ agents: catalog.slice(0, 2) });
  saved.workload = { configured: true, accounts: [
    { scope: 'claude', members: ['claude'], share: 3, level: 'high', points: 600, calls: 2, target_percent: 75, actual_percent: 60 },
    { scope: 'codex', members: ['codex'], share: 1, level: 'low', points: 400, calls: 2, target_percent: 25, actual_percent: 40 },
  ] };
  const partners = [{ ...catalog[0], adapter: 'claude', enabled: true, selectable: true, workload_level: 'high', account: claudeUsageAccount([]) },
    { ...catalog[1], adapter: 'codex', enabled: true, selectable: true, workload_level: 'low' }];
  const ui = await browser({ agents: catalog.slice(0, 2), partners, rooms: [saved.room], saved,
    route(request) { if (request.path === '/api/partners/claude/workload') return { partners, supported_adapters: [] }; } });
  assert.equal(ui.get('member-workload-claude').textContent, '角色：全能 · 本轮工作量 60% · 设定 75%');
  assert.equal(ui.get('member-workload-codex').textContent, '角色：全能 · 本轮工作量 40% · 设定 25%');
  const select = descendants(ui.get('provider-account-claude')).find((node) => node.dataset?.focusKey === 'account-workload');
  assert.equal(select.value, 'high');
  select.value = 'low'; select.fire('change'); await settle();
  assert.deepEqual(ui.posts().filter((post) => post.path.endsWith('/workload')).map((post) => [post.path, post.body]),
    [['/api/partners/claude/workload', { level: 'low' }]]);

  saved.workload.configured = false;
  const plain = await browser({ agents: catalog.slice(0, 2), partners, rooms: [saved.room], saved });
  assert.equal(plain.get('member-workload-claude').textContent, '角色：全能 · 本轮工作量 60%');
  saved.workload = null;
  const none = await browser({ agents: catalog.slice(0, 2), partners, rooms: [saved.room], saved });
  assert.equal(none.get('member-workload-claude').textContent, '角色：全能', 'the role shows even before any work is counted');
  saved.workload = { configured: true, accounts: [{ scope: 'claude', members: ['claude'], share: 3, level: 'high', points: 600, calls: 2, target_percent: 75, actual_percent: 60 }] };
  const english = await browser({ language: 'en', agents: catalog.slice(0, 2), partners, rooms: [saved.room], saved });
  assert.equal(english.get('member-workload-claude').textContent, 'Role: All-round · This round 60% of the work · set to 75%');
});

test('each partner can be given a role and a workload for the new task, and a model change keeps them', async () => {
  // Catches: role or workload never reaching the server, a model change dropping the role, or the chip hiding the role.
  const saved = snapshot({ agents: catalog.slice(0, 2), status: 'running' });
  const ui = await browser({ agents: catalog.slice(0, 2), route(request) {
    if (request.path === '/api/runtime-options') return runtimeOptionsFixture();
    if (request.path === '/api/rooms' && request.method === 'POST') return saved;
  } });
  await openRuntimeSettings(ui);
  assert.deepEqual(ui.get('runtime-role-claude').children.map((node) => node.textContent), ['全能（默认）', '开发', '审查', '规划', '新建角色…']);
  assert.equal(ui.get('runtime-role-claude').value, 'all'); assert.equal(ui.get('runtime-workload-claude').value, '');
  selectRuntime(ui, 'runtime-role-claude', 'reviewer');
  assert.match(ui.get('runtime-card-claude').textContent, /只审别人交付的改动，不领实现任务，也不参加规划。/);
  selectRuntime(ui, 'runtime-workload-claude', 'high');
  selectRuntime(ui, 'runtime-model-claude', 'opus');
  assert.equal(ui.get('runtime-role-claude').value, 'reviewer', 'choosing a model keeps the role');
  selectRuntime(ui, 'runtime-role-codex', 'builder');
  assert.match(ui.get('runtime-shortcut-claude').textContent, /Opus.*审查/);
  assert.match(ui.get('runtime-shortcut-claude').title, /审查 · 工作量多/);
  await ui.click('runtime-settings-close'); ui.type('分工完成一个虚构小任务'); await ui.submit();
  const create = ui.posts().find((request) => request.path === '/api/rooms'); assert.ok(create);
  assert.deepEqual(create.body.runtime_settings.members, {
    claude: { model: 'opus', effort: null, role: { id: 'reviewer' }, workload: 'high' },
    codex: { model: null, effort: null, role: { id: 'builder' } },
  });
});

test('a role is sent even when model options could not be read', async () => {
  // Catches: settings dropped from the new task because only model choices were thought to need them.
  const saved = snapshot({ agents: catalog.slice(0, 2), status: 'running' });
  const ui = await browser({ agents: catalog.slice(0, 2), route(request) {
    if (request.path === '/api/runtime-options') return { scope: 'unavailable' };
    if (request.path === '/api/rooms' && request.method === 'POST') return saved;
  } });
  await openRuntimeSettings(ui);
  assert.equal(ui.get('runtime-model-claude').disabled, true);
  assert.equal(ui.get('runtime-role-claude').disabled, false);
  selectRuntime(ui, 'runtime-role-claude', 'planner');
  await ui.click('runtime-settings-close'); ui.type('先规划再动手'); await ui.submit();
  const create = ui.posts().find((request) => request.path === '/api/rooms'); assert.ok(create);
  assert.deepEqual(create.body.runtime_settings.members.claude, { model: null, effort: null, role: { id: 'planner' } });
});

test('a custom role is checked before saving, offered to every partner and sent whole', async () => {
  // Catches: an unnamed role or one with no step being saved, the duty text lost, or a deleted role still offered.
  const saved = snapshot({ agents: catalog.slice(0, 2), status: 'running' });
  const ui = await browser({ agents: catalog.slice(0, 2), route(request) {
    if (request.path === '/api/runtime-options') return runtimeOptionsFixture();
    if (request.path === '/api/rooms' && request.method === 'POST') return saved;
  } });
  await openRuntimeSettings(ui);
  selectRuntime(ui, 'runtime-role-claude', 'new');
  assert.equal(ui.get('role-editor-ability-work').checked, true);
  await ui.click('role-editor-save');
  assert.equal(ui.get('role-editor-error').textContent, '请填写角色名称。');
  ui.get('role-editor-name').value = '  文档 '; ui.get('role-editor-name').fire('input');
  ui.get('role-editor-duty').value = '只写说明文档'; ui.get('role-editor-duty').fire('input');
  ui.get('role-editor-ability-work').checked = false; ui.get('role-editor-ability-work').fire('change');
  await ui.click('role-editor-save');
  assert.equal(ui.get('role-editor-error').textContent, '至少选一个环节。');
  ui.get('role-editor-ability-review').checked = true; ui.get('role-editor-ability-review').fire('change');
  await ui.click('role-editor-save');
  assert.equal(ui.document.getElementById('role-editor-name'), null, 'saving closes the editor');
  const id = ui.get('runtime-role-claude').value;
  assert.match(id, /^custom:[a-z0-9]{12}$/);
  assert.match(ui.get('runtime-card-claude').textContent, /只写说明文档（负责：审查）/);
  assert.ok(ui.get('runtime-role-codex').children.some((node) => node.value === id && node.textContent === '文档'), 'the saved role is offered to the other partner');
  selectRuntime(ui, 'runtime-role-codex', id);
  await ui.click('role-delete-codex');
  assert.equal(ui.get('runtime-role-codex').value, 'all');
  assert.equal(ui.get('runtime-role-codex').children.some((node) => node.value === id), false, 'a deleted role is no longer offered');
  assert.equal(ui.get('runtime-role-claude').value, id, 'a partner already using it keeps it for this task');
  await ui.click('runtime-settings-close'); ui.type('按自定义角色分工'); await ui.submit();
  const create = ui.posts().find((request) => request.path === '/api/rooms');
  assert.deepEqual(create.body.runtime_settings.members.claude.role, { id, name: '文档', duty: '只写说明文档', abilities: ['review'] });
  assert.equal(create.body.runtime_settings.members.codex.role, undefined);
});

test('the room shows each partner role and what each step of this round cost', async () => {
  // Catches: the role invisible once the task runs, or the per-step cost missing, misordered or untranslated.
  const saved = snapshot({ agents: catalog.slice(0, 2) });
  saved.room.runtime_settings = { permission: 'workspace_write', effective_permission: 'workspace_write', members: {
    claude: { model: null, effort: null, role: { id: 'reviewer', abilities: ['review'] } }, codex: { model: null, effort: null } } };
  saved.workload = { configured: false, accounts: [
    { scope: 'claude', members: ['claude'], share: 2, level: 'normal', points: 400, calls: 2, target_percent: 50, actual_percent: 40 },
    { scope: 'codex', members: ['codex'], share: 2, level: 'normal', points: 600, calls: 3, target_percent: 50, actual_percent: 60 },
  ], phases: [{ phase: 'work', points: 600, calls: 3, percent: 60, members: { codex: 600 } },
    { phase: 'review', points: 400, calls: 2, percent: 40, members: { claude: 400 } }] };
  const ui = await browser({ agents: catalog.slice(0, 2), rooms: [saved.room], saved });
  assert.equal(ui.get('member-workload-claude').textContent, '角色：审查 · 本轮工作量 40%');
  assert.equal(ui.get('member-workload-codex').textContent, '角色：全能 · 本轮工作量 60%');
  assert.equal(ui.get('phase-load').hidden, false);
  assert.equal(ui.get('phase-load').textContent, '本轮消耗（按 Token 折算）：干活 60% · 审查 40%');
  const english = await browser({ language: 'en', agents: catalog.slice(0, 2), rooms: [saved.room], saved });
  assert.equal(english.get('member-workload-claude').textContent, 'Role: Review · This round 40% of the work');
  assert.equal(english.get('phase-load').textContent, "This round's usage by tokens: Building 60% · Review 40%");
  saved.workload = null;
  const none = await browser({ agents: catalog.slice(0, 2), rooms: [saved.room], saved });
  assert.equal(none.get('phase-load').hidden, true);
  assert.equal(none.get('member-workload-claude').textContent, '角色：审查');
});

test('composer keeps editable text and send controls outside the independently scrollable setup area', async () => {
  const ui = await browser(); const context = ui.get('composer-context');
  assert.equal(descendants(context).includes(ui.get('message')), false);
  assert.equal(descendants(context).includes(ui.get('send')), false);
  assert.equal(descendants(context).includes(ui.get('more-settings')), true);
  assert.equal(descendants(ui.get('task-options-popover')).includes(context), true);
  assert.equal(descendants(ui.get('composer')).includes(context), false);
  assert.equal(descendants(ui.get('composer')).includes(ui.get('composer-usage-bar')), false);
  ui.type('保留这份较长的目标。'.repeat(50)); context.scrollTop = 167; ui.get('more-settings').open = true;
  await ui.poll(); assert.equal(context.scrollTop, 167); assert.equal(ui.get('more-settings').open, true);
  assert.equal(ui.get('message').value, '保留这份较长的目标。'.repeat(50));
  assert.equal(ui.get('send').disabled, false);
  assert.equal(ui.posts().length, 0);
});

test('Claude headline flags do not hide real shared quota while model and surface pools stay separate', async () => {
  const account = claudeUsageAccount([
    { name: 'shared', label: 'Claude · 7天窗口', scope: 'provider_account', kind: 'weekly_all', window_minutes: 10080, used_percent: 13, remaining_percent: 87, active: false },
    { name: 'session', label: 'Claude · 5小时窗口', scope: 'provider_account', kind: 'session', window_minutes: 300, used_percent: 8, remaining_percent: 92, active: true },
    { name: 'model', label: 'Fable · 模型周额度', scope: 'model', model: 'Fable', kind: 'weekly_scoped', window_minutes: 10080, used_percent: 100, remaining_percent: 0, active: true },
    { name: 'surface', label: '特定场景周额度', scope: 'provider_account', surface: 'web', kind: 'weekly_all', window_minutes: 10080, used_percent: 100, remaining_percent: 0, active: true },
  ]);
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, account: agent.id === 'claude' ? account : null }));
  const ui = await browser({ partners }); const chip = ui.get('composer-usage-claude');
  assert.match(chip.textContent, /剩余 87%/); assert.doesNotMatch(chip.textContent, /剩余 0%/);
  assert.doesNotMatch(ui.get('provider-account-claude').textContent, /未激活|未启用|不适用/);
  assert.match(ui.get('provider-account-claude').textContent, /场景专属额度/);
  assert.equal(ui.posts().length, 0);
});

test('eight participants keep one quota row and bounded activity nodes without rebuilding drafts or starting work', async () => {
  const agents = Array.from({ length: 8 }, (_, index) => catalog[index] || { id: `peer_${index}`, name: `第 ${index + 1} 位伙伴`, avatar: 'P' });
  const saved = withCurrentRun(snapshot({ agents, status: 'running' }), { status: 'in_progress', can_continue: false, can_accept: false });
  saved.collaboration = teamCollaboration();
  const ui = await browser({ agents, rooms: [saved.room], saved });
  assert.equal(ui.get('composer-usage-items').children.length, 8); assert.equal(ui.get('partner-activity-cards').children.length, 8);
  const chips = [...ui.get('composer-usage-items').children]; const cards = [...ui.get('partner-activity-cards').children];
  assert.ok(chips.every((node) => node.classList.contains('partner-controls') && node.children.length === 2 && node.children.every((child) => child.type === 'button')));
  assert.ok(cards.every((node) => node.tagName === 'DETAILS'));
  ui.type('保留多伙伴任务中的补充草稿'); ui.get('composer-context').scrollTop = 71;
  await ui.poll(); assert.equal(ui.get('composer-context').scrollTop, 71); assert.equal(ui.get('message').value, '保留多伙伴任务中的补充草稿');
  assert.deepEqual(ui.get('composer-usage-items').children, chips); assert.deepEqual(ui.get('partner-activity-cards').children, cards);
  assert.equal(ui.posts().length, 0);
});

test('a new task never sends to a partner that was left out after being the default recipient', async () => {
  // Catches: the first partner became the recipient, was then left out (out
  // of quota, unticked), and the new task was refused as invalid_target.
  const ui = await browser({ agents: catalog.slice(0, 2), taskModes: ['text', 'python_artifacts'] });
  ui.get('task-mode').value = 'python_artifacts'; ui.get('task-mode').fire('change'); await settle();
  const claude = participantInput(ui, 'claude'); claude.checked = false; claude.fire('change'); await ui.poll();
  ui.type('只让剩下的伙伴来做。');
  await ui.submit();
  assert.equal(ui.posts().length, 1);
  assert.deepEqual(ui.posts()[0].body.participants, ['codex']);
  assert.equal(ui.posts()[0].body.target, 'codex');
});

test('compact task options remain outside chat layout and preserve selections, blank-message guards and keyboard focus', async () => {
  const ui = await browser({ agents: catalog.slice(0, 2) });
  const form = ui.get('composer'); const dialog = ui.get('task-options-popover');
  assert.equal(dialog.hidden, true); assert.equal(descendants(ui.document.querySelector('.app-shell')).includes(dialog), false);
  assert.equal(descendants(form).includes(ui.get('participant-picker')), false);
  assert.equal(descendants(form).includes(ui.get('scope-description')), false);
  assert.equal(ui.get('message').getAttribute('required'), null);
  assert.equal(ui.get('message').getAttribute('placeholder'), '说说这次想做什么');
  assert.equal(ui.get('send').disabled, true);
  for (const id of ['task-options-partners', 'task-options-project', 'task-options-more', 'runtime-settings']) assert.equal(ui.get(id).getAttribute('type'), 'button');
  const trigger = ui.get('task-options-partners'); await ui.click(trigger.id);
  assert.equal(dialog.hidden, false); assert.equal(ui.document.querySelector('.app-shell').inert, true);
  assert.equal(ui.document.activeElement.dataset.participant, 'claude');
  const chosen = participantInput(ui, 'codex'); chosen.checked = false; chosen.fire('change'); await ui.poll();
  assert.equal(chosen.checked, false); assert.match(trigger.textContent, /伙伴 · 1/);
  ui.get('task-options-close').focus(); ui.document.root.fire('keydown', { key: 'Tab', shiftKey: true });
  assert.equal(ui.document.activeElement.tagName, 'SUMMARY', 'closed sections expose their summaries, not hidden controls');
  ui.document.root.fire('keydown', { key: 'Escape' });
  assert.equal(dialog.hidden, true); assert.equal(ui.document.activeElement.id, trigger.id); assert.equal(ui.get('send').disabled, true);
  ui.type('保留这份草稿'); const draft = ui.get('message').value;
  await ui.click('task-options-more'); assert.equal(ui.get('more-settings').open, true);
  dialog.fire('pointerdown', { target: ui.get('target-picker') }); assert.equal(dialog.hidden, false);
  dialog.fire('pointerdown'); assert.equal(dialog.hidden, true); assert.equal(ui.document.activeElement.id, 'task-options-more');
  assert.equal(ui.get('message').value, draft); assert.equal(ui.posts().length, 0);
});

test('visible partner model shortcuts load the clicked catalog directly and keep per-model effort, draft and focus stable', async () => {
  let options = runtimeOptionsFixture({ checked: false }); const waiting = deferred();
  const ui = await browser({ agents: catalog.slice(0, 2), route: async (request) => {
    if (request.path === '/api/runtime-options') return options;
    if (request.path === '/api/partners/claude/runtime/refresh') {
      await waiting.promise; options = runtimeOptionsFixture();
      options.partners.claude.catalog_source = 'claude_sdk_initialize';
      options.partners.claude.models[0].name = 'Opus 5.5'; options.partners.claude.models[0].resolved_model = 'claude-opus-5-5';
      options.partners.claude.models.push({ id: 'haiku', name: 'Haiku 4.5', efforts: [] }, { id: 'default', name: 'Default (recommended)', resolved_model: 'claude-opus-5-5', efforts: ['low', 'high'] }); return options;
    }
  } });
  const trigger = ui.get('runtime-shortcut-claude'); const quota = ui.get('composer-usage-claude');
  assert.equal(trigger.type, 'button'); assert.equal(trigger.parentNode, quota.parentNode);
  assert.match(trigger.getAttribute('aria-label'), /Claude Code的模型与思考强度：跟随工具，默认强度/);
  ui.type('这份目标不被设置弹层改掉'); await ui.click(trigger.id);
  assert.equal(ui.get('runtime-settings-popover').hidden, false);
  assert.equal(ui.get('runtime-card-claude').hidden, false); assert.equal(ui.get('runtime-card-codex').hidden, true);
  assert.equal(ui.get('runtime-permission').parentNode.hidden, true);
  assert.match(ui.get('runtime-settings-title').textContent, /Claude Code.*模型、角色与工作量/);
  assert.match(ui.get('runtime-card-claude').textContent, /正在从本机工具读取模型选项/);
  assert.doesNotMatch(ui.get('runtime-card-claude').textContent, /尚未读取可选模型/);
  assert.deepEqual(ui.posts().map((request) => request.path), ['/api/partners/claude/runtime/refresh']);
  waiting.resolve(); await settle();
  assert.equal(ui.document.activeElement.id, 'runtime-model-claude');
  assert.ok(ui.get('runtime-model-claude').children.some((node) => node.textContent === 'Opus 5.5'));
  assert.equal(ui.get('runtime-model-claude').firstChild.textContent, '跟随工具设置');
  assert.ok(ui.get('runtime-model-claude').children.some((node) => node.value === 'default' && node.textContent === '默认推荐（Opus 5.5）'));
  assert.equal(ui.get('runtime-model-claude').value, '', 'opening a catalog never turns null into an explicit recommended model');
  selectRuntime(ui, 'runtime-model-claude', 'opus'); selectRuntime(ui, 'runtime-effort-claude', 'high');
  assert.match(trigger.textContent, /Opus 5.5.*高/);
  selectRuntime(ui, 'runtime-model-claude', 'haiku');
  assert.equal(ui.get('runtime-effort-claude').value, ''); assert.equal(ui.get('runtime-effort-claude').disabled, true);
  assert.match(trigger.textContent, /Haiku 4.5.*默认/);
  ui.document.root.fire('keydown', { key: 'Escape' }); assert.equal(ui.document.activeElement.id, trigger.id);
  await ui.poll(); assert.equal(ui.get(trigger.id), trigger); assert.equal(ui.get('message').value, '这份目标不被设置弹层改掉');
  assert.equal(ui.posts().length, 1, 'polling and closing never repeat a capability request or submit the goal');
});

test('working model shortcuts show frozen choices without native refresh while later choices are explicitly marked next round', async () => {
  const saved = activeActivitySnapshot(); saved.current_run.runtime_settings = { permission: 'workspace_write', effective_permission: 'workspace_write',
    members: { claude: { model: 'opus', effort: 'high' }, codex: { model: null, effort: null } } };
  const options = runtimeOptionsFixture();
  const ui = await browser({ rooms: [saved.room], saved, route(request) { if (request.path === '/api/runtime-options') return options; } });
  const trigger = ui.get('runtime-shortcut-claude'); await ui.click(trigger.id);
  assert.equal(ui.get('runtime-model-claude').disabled, true); assert.equal(ui.get('runtime-effort-claude').disabled, true);
  assert.equal(ui.get('runtime-refresh-claude').disabled, true);
  await ui.click('runtime-refresh-claude');
  assert.match(trigger.getAttribute('aria-label'), /本轮.*模型与思考强度.*本轮已固定/);
  assert.equal(ui.document.activeElement.id, 'runtime-card-claude'); assert.equal(ui.posts().length, 0);
  await ui.click('runtime-settings-close'); assert.equal(ui.document.activeElement.id, trigger.id);
  saved.current_run.status = 'ready_for_user'; saved.current_run.can_continue = true; saved.room.status = 'ready_for_user';
  saved.jobs.forEach((job) => { job.status = 'completed'; }); saved.collaboration = teamCollaboration({ state: 'ready_for_user' });
  await ui.poll();
  assert.match(trigger.getAttribute('aria-label'), /下一轮/); assert.equal(ui.get('runtime-shortcuts-caption').textContent, '下一轮');
  assert.equal(saved.current_run.runtime_settings.members.claude.model, 'opus'); assert.equal(ui.posts().length, 0);
});

test('a delayed partner catalog preserves a user focus change instead of jumping back to the model field', async () => {
  const options = runtimeOptionsFixture(); const waiting = deferred();
  const ui = await browser({ agents: catalog.slice(0, 2), route: async (request) => {
    if (request.path === '/api/runtime-options') return options;
    if (request.path === '/api/partners/claude/runtime/refresh') { await waiting.promise; return options; }
  } });
  await openRuntimeSettings(ui); selectRuntime(ui, 'runtime-model-claude', 'opus'); selectRuntime(ui, 'runtime-effort-claude', 'high');
  await ui.click('runtime-settings-close'); await ui.click('runtime-shortcut-claude');
  assert.equal(ui.get('runtime-effort-claude').disabled, false);
  ui.get('runtime-effort-claude').focus(); waiting.resolve(); await settle();
  assert.equal(ui.document.activeElement.id, 'runtime-effort-claude');
  assert.equal(ui.posts().length, 1); await ui.click('runtime-settings-close');
  assert.equal(ui.document.activeElement.id, 'runtime-shortcut-claude');
});

test('a delayed permission cache does not reclaim focus after the user moves to the close control', async () => {
  const options = runtimeOptionsFixture(); const waiting = deferred(); let queries = 0;
  const ui = await browser({ agents: catalog.slice(0, 2), route: async (request) => {
    if (request.path === '/api/runtime-options') { queries += 1; if (queries > 1) await waiting.promise; return options; }
  } });
  await openRuntimeSettings(ui); await ui.click('runtime-settings-close'); await openRuntimeSettings(ui);
  assert.equal(ui.document.activeElement.id, 'runtime-permission');
  ui.document.root.fire('keydown', { key: 'Tab', shiftKey: true }); assert.equal(ui.document.activeElement.id, 'runtime-settings-close');
  waiting.resolve(); await settle(); assert.equal(ui.document.activeElement.id, 'runtime-settings-close');
  assert.equal(ui.posts().length, 0);
});

test('the compact project shortcut describes the frozen room association instead of a different new-goal draft', async () => {
  const projectId = '44444444-4444-4444-8444-444444444444';
  const otherId = '55555555-5555-4555-8555-555555555555';
  const saved = snapshot({ agents: catalog.slice(0, 2), status: 'ready_for_user' }); saved.room.project_id = projectId;
  const projects = [{ id: projectId, name: '当前项目', path: '/fictional/current', files: [], selection_kind: 'folder' },
    { id: otherId, name: '另一个草稿项目', path: '/fictional/draft', files: [], selection_kind: 'folder' }];
  const ui = await browser({ agents: catalog.slice(0, 2), rooms: [saved.room], saved, selectSaved: false, route(request) {
    if (request.path === '/api/projects') return { projects };
  } });
  ui.get('project-picker').value = otherId; ui.get('project-picker').fire('change');
  assert.equal(ui.get('task-options-project').textContent, '另一个草稿项目');
  ui.get('rooms').firstChild.fire('click'); await settle();
  assert.equal(ui.get('task-options-project').textContent, '当前项目');
  await ui.click('task-options-project'); assert.match(ui.get('frozen-project').textContent, /当前项目.*项目固定/);
  assert.equal(ui.document.activeElement.id, 'frozen-project'); assert.equal(ui.get('new-room-setup').hidden, true);
  assert.equal(ui.posts().length, 0);
});

const objectiveId = '66666666-6666-4666-8666-666666666666';
function objectiveSnapshot({ status = 'awaiting_continue', scope = 'current_goal', decision = 'continue' } = {}) {
  const saved = withCurrentRun(snapshot({ agents: catalog.slice(0, 2), status: status === 'running' ? 'running' : 'ready_for_user' }), {
    status: status === 'running' ? 'in_progress' : 'ready_for_user', can_continue: false, can_accept: true, can_retry: false,
  });
  saved.current_run.objective_id = objectiveId;
  saved.current_run.execution_policy = { mode: 'autonomous', max_seconds: 7200, max_calls: 64, quota_failover: true };
  saved.objective = { id: objectiveId, mode: 'autonomous', status, original_goal: '完成这份虚构的小工具', created_at: 1000,
    deadline: 8200, budget: { used: 13, limit: 64, queued: 1, remaining: 50 }, stage: 2, pause_requested: false,
    next_step: status === 'running' ? null : { decision, summary: '接着补齐说明与检查', goal: '补齐说明与检查', scope_reference: '原目标中已有要求',
      question: null, scope, can_start: status === 'awaiting_continue' && scope === 'current_goal',
      binding: { run_id: saved.current_run.id, revision: saved.current_run.revision, digest: saved.current_run.digest }, review_kind: 'independent_review' } };
  saved.collaboration = teamCollaboration({ state: status === 'running' ? 'in_progress' : 'ready_for_user' });
  return saved;
}

test('stage delivery offers the exact frozen next step without acceptance, text synthesis or budget reset', async () => {
  const saved = objectiveSnapshot({ status: 'running' });
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, route(request) {
    if (request.path.endsWith('/continue')) return saved;
  } });
  ui.type('阶段之间保留的未发送补充');
  saved.room.status = 'ready_for_user'; saved.current_run.status = 'ready_for_user'; saved.objective.status = 'awaiting_continue';
  saved.objective.next_step = { decision: 'continue', summary: '接着补齐说明与检查', goal: '不应被客户端拼进请求', scope: 'current_goal', can_start: true,
    binding: { run_id: saved.current_run.id, revision: saved.current_run.revision, digest: saved.current_run.digest }, review_kind: 'independent_review' };
  saved.collaboration = teamCollaboration({ state: 'ready_for_user' }); await ui.poll();
  assert.match(ui.get('collaboration-outcome').textContent, /这一阶段已交付.*下一步：接着补齐说明与检查/);
  assert.equal(ui.document.getElementById('accept-run'), null); assert.ok(ui.get('continue-objective'));
  assert.equal(ui.posts().length, 0); await ui.click('continue-objective');
  const request = ui.posts()[0]; assert.equal(request.path, `/api/rooms/${roomId}/continue`);
  assert.deepEqual(Object.keys(request.body).sort(), ['digest', 'request_id', 'revision', 'run_id']);
  assert.equal(request.body.run_id, saved.current_run.id); assert.equal(ui.get('message').value, '阶段之间保留的未发送补充');
  assert.equal(saved.objective.deadline, 8200); assert.equal(saved.objective.budget.limit, 64);
  saved.objective.next_step.binding.digest = 'a'.repeat(64); await ui.poll(); assert.equal(ui.document.getElementById('continue-objective'), null);
  assert.match(ui.get('collaboration-outcome').textContent, /范围或版本尚未确认/); assert.equal(ui.posts().length, 1);
});

test('pausing waits for the current call boundary and resumes the same objective without losing an unsent draft', async () => {
  const saved = objectiveSnapshot({ status: 'running' });
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, route(request) {
    if (request.path.endsWith('/pause')) { saved.objective.status = 'paused'; saved.objective.pause_requested = true; return saved; }
    if (request.path.endsWith('/resume')) { saved.objective.status = 'running'; return saved; }
  } });
  ui.type('暂停时不丢这段补充'); await ui.click('pause-objective');
  assert.match(ui.get('pause-objective').textContent, /正在暂停/); assert.equal(ui.get('pause-objective').disabled, true);
  assert.match(ui.get('room-status').textContent, /等待当前处理结束/); assert.equal(ui.get('cancel-room').hidden, false);
  assert.deepEqual(Object.keys(ui.posts()[0].body).sort(), ['objective_id', 'request_id']); assert.equal(ui.posts()[0].body.objective_id, objectiveId);
  saved.objective.pause_requested = false; await ui.poll(); assert.match(ui.get('pause-objective').textContent, /继续推进/);
  await ui.click('pause-objective'); assert.equal(ui.posts()[1].path, `/api/rooms/${roomId}/resume`);
  assert.equal(ui.posts()[1].body.objective_id, objectiveId); assert.equal(ui.get('message').value, '暂停时不丢这段补充');
  assert.equal(saved.objective.deadline, 8200); assert.equal(saved.objective.budget.used, 13);
});

test('an accepted legacy stage uses the dedicated objective capability and preserves historical acceptance', async () => {
  const saved = withCurrentRun(snapshot({ agents: catalog.slice(0, 2), status: 'ready_for_user' }), {
    status: 'accepted', accepted_at: 1000, can_continue: false, can_start_objective: true,
  });
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, route(request) {
    if (request.path.endsWith('/objective')) return saved;
  } });
  assert.equal(ui.get('start-objective').disabled, false); assert.equal(ui.posts().length, 0);
  assert.equal(ui.document.getElementById('execution-policy'), null);
  await ui.click('start-objective'); const request = ui.posts()[0];
  assert.equal(request.path, `/api/rooms/${roomId}/objective`);
  assert.deepEqual(Object.keys(request.body).sort(), ['digest', 'execution_policy', 'request_id', 'revision', 'run_id']);
  assert.deepEqual(request.body.execution_policy, { mode: 'autonomous', max_seconds: null, max_calls: null, quota_failover: true }); assert.equal(saved.current_run.accepted_at, 1000);
});

test('a suggested new goal stays separate from continue and needs an explicit new-budget action', async () => {
  const saved = objectiveSnapshot({ status: 'needs_user', scope: 'new_goal' });
  saved.current_run.can_start_suggested_goal = true;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, route(request) { if (request.path.endsWith('/runs')) return saved; } });
  assert.equal(ui.document.getElementById('continue-objective'), null);
  assert.match(ui.get('collaboration-outcome').textContent, /超出当前目标.*不会自动开工/);
  assert.match(ui.get('runtime-shortcuts-caption').textContent, /新目标/);
  assert.match(ui.get('collaboration-outcome').textContent, /开始新的目标，原目标记录保留/); assert.equal(ui.posts().length, 0);
  await ui.click('start-suggested-goal'); const request = ui.posts()[0];
  assert.equal(request.path, `/api/rooms/${roomId}/runs`); assert.equal(request.body.text, saved.objective.next_step.goal);
  assert.deepEqual(request.body.execution_policy, { mode: 'autonomous', max_seconds: null, max_calls: null, quota_failover: true }); assert.equal(saved.objective.deadline, 8200);
});

test('single-partner self-check completion names its scope without a redundant automatic acceptance', async () => {
  const saved = objectiveSnapshot({ status: 'complete', decision: 'complete' });
  saved.team = { run_id: saved.current_run.id, tasks: [{ id: 'solo', title: '完成内容', owner: 'codex', status: 'self_checked', review_kind: 'self_check', result_files: [] }] };
  saved.collaboration.tasks = [{ id: 'solo', title: '完成内容', owner: 'codex', status: 'self_checked', review_kind: 'self_check' }];
  saved.objective.next_step.review_kind = 'self_check';
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.match(ui.get('collaboration-outcome').textContent, /本轮已交付.*1 项只做了自查，没有别的伙伴看过.*完成评估由单个伙伴/);
  assert.match(ui.get('collaboration-plan').textContent, /只做了自查/); assert.doesNotMatch(ui.get('collaboration-plan').textContent, /已完成，伙伴看过/);
  assert.equal(ui.document.getElementById('accept-run'), null); assert.equal(ui.document.getElementById('continue-objective'), null);
  assert.equal(ui.posts().length, 0);
});

test('uncertain objective continuation is stored without replay and retries only the same bound request explicitly', async () => {
  const saved = objectiveSnapshot(); const storage = new Map();
  const ui = await browser({ rooms: [saved.room], saved, storage, executionPolicySupport: true, route(request) {
    if (request.path.endsWith('/continue')) throw new Error('fictional lost result after a possible accepted continuation');
  } });
  await ui.click('continue-objective'); const original = structuredClone(ui.posts()[0]);
  assert.equal(ui.get('pending-request').hidden, false); assert.match(ui.get('pending-description').textContent, /已确认的下一步.*可能已经生效/);
  await ui.poll(); assert.equal(ui.posts().length, 1);
  const reloaded = await browser({ rooms: [saved.room], saved, storage, executionPolicySupport: true, route(request) {
    if (request.path.endsWith('/continue')) return saved;
  } });
  assert.equal(reloaded.posts().length, 0); assert.equal(reloaded.get('pending-request').hidden, false);
  await reloaded.click('retry-request'); assert.equal(reloaded.posts()[0].path, original.path); assert.deepEqual(reloaded.posts()[0].body, original.body);
  assert.equal(saved.objective.budget.limit, 64); assert.equal(saved.objective.deadline, 8200);
});

test('objective scope and capability changes fail closed and complete only exposes final confirmation', async () => {
  const saved = objectiveSnapshot();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.ok(ui.get('continue-objective')); saved.objective.next_step.scope = 'new_goal'; saved.objective.next_step.can_start = false;
  saved.current_run.can_start_suggested_goal = false; await ui.poll();
  assert.equal(ui.document.getElementById('continue-objective'), null); assert.equal(ui.document.getElementById('start-suggested-goal'), null);
  saved.current_run.objective_id = '77777777-7777-4777-8777-777777777777'; await ui.poll();
  assert.equal(ui.get('pause-objective').hidden, true); assert.doesNotMatch(ui.get('room-status').textContent, /正在推进目标/);
  assert.equal(ui.posts().length, 0);
});

function objectiveDeliveryFixture() {
  const saved = objectiveSnapshot({ status: 'checkpoint' });
  saved.room.mode = 'text_artifacts';
  const files = [
    { path: 'first.md', owner: 'claude', content: '# 第一阶段成果\n第一阶段的正文' },
    { path: 'second.md', owner: 'codex', content: '# 第二阶段成果\n第二阶段的正文' },
    { path: 'inventory.md', owner: 'source', content: '# 旧库存\n尚未列入交付' },
  ].map((file) => ({ ...file, sha256: createHash('sha256').update(file.content).digest('hex') }));
  saved.project = { available: true, revision: 3, snapshot_sha256: 'd'.repeat(64), files };
  saved.current_run.revision = 3; saved.current_run.digest = saved.project.snapshot_sha256;
  saved.team = { run_id: saved.current_run.id, phase: 'working', tasks: [] };
  saved.collaboration = teamCollaboration({ state: 'in_progress', tasks: [] });
  saved.objective.delivery = { revision: 3, digest: saved.project.snapshot_sha256, verified: true, total: 2,
    files: files.slice(0, 2).map((file, index) => ({ path: file.path, sha256: file.sha256, verified: true,
      run_id: saved.current_run.id, revision: 3, digest: saved.project.snapshot_sha256, review_kind: index ? 'self_check' : 'independent_review' })), missing_or_changed: [] };
  return saved;
}
function addObjectiveBaseline(saved) {
  const content = '# 第一阶段旧版\n这是上一轮的正文。';
  const binding = { run_id: '66666666-6666-4666-8666-666666666666', revision: 1, digest: 'a'.repeat(64),
    path: 'first.md', sha256: createHash('sha256').update(content).digest('hex') };
  saved.objective.baseline = { run_id: binding.run_id, revision: binding.revision, digest: binding.digest, verified: true,
    files: [{ run_id: binding.run_id, path: binding.path, sha256: binding.sha256, review_kind: 'independent_review' }] };
  return { binding, content };
}

test('objective delivery aggregates verified stages at a checkpoint without promoting inherited inventory', async () => {
  const saved = objectiveDeliveryFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  const current = ui.get('document-shortcuts');
  assert.match(current.textContent, /第一阶段成果.*伙伴看过.*第二阶段成果.*只做了自查/);
  assert.doesNotMatch(current.textContent, /旧库存|inventory.md/);
  assert.match(ui.get('document-more-items').textContent, /旧库存/);
  saved.objective.delivery.files[1].verified = false;
  saved.objective.delivery.missing_or_changed = [{ path: 'second.md', expected_sha256: saved.project.files[1].sha256, reason: 'changed' }];
  await ui.poll();
  const updated = ui.get('document-shortcuts');
  assert.match(updated.textContent, /第一阶段成果/); assert.doesNotMatch(updated.textContent, /第二阶段成果/);
  assert.match(ui.get('document-more-items').textContent, /第二阶段成果/, 'changed proof remains readable as saved inventory rather than current delivery');
  saved.objective.delivery.digest = 'e'.repeat(64); await ui.poll();
  assert.equal(ui.get('document-shortcuts').children.length > 0, false, 'a mismatched head binding never promotes files');
  assert.equal(ui.posts().length, 0);
});

test('baseline opens only the frozen historical body and remains readable across stages of the same objective', async () => {
  const saved = objectiveDeliveryFixture(); const baseline = addObjectiveBaseline(saved);
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, route(request) {
    if (request.path.startsWith(`/api/rooms/${roomId}/file?`)) {
      const query = Object.fromEntries(new URL(request.path, 'http://fictional.invalid').searchParams);
      assert.deepEqual(query, { ...baseline.binding, revision: String(baseline.binding.revision) });
      return { ...baseline.binding, content: baseline.content };
    }
  } });
  assert.equal(ui.get('task-materials').hidden, true);
  assert.ok(descendants(ui.get('room-documents')).some(node => node.dataset.documentKey?.includes(':baseline:')));
  const button = descendants(ui.get('room-documents')).find((node) => node.dataset.documentPath === 'first.md' && node.dataset.documentKey?.includes(':baseline:'));
  button.fire('click'); await waitUntil(() => ui.get('reader-body').textContent.includes('这是上一轮'), 'historical SHA validation finishes');
  assert.match(ui.get('reader-title').textContent, /第一阶段旧版/); assert.doesNotMatch(ui.get('reader-body').textContent, /第一阶段的正文/);
  ui.get('reader-body').scrollTop = 137;
  saved.current_run.id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'; saved.current_run.objective_id = objectiveId;
  saved.objective.stage += 1; await ui.poll();
  assert.equal(ui.get('delivery-reader').hidden, false); assert.equal(ui.get('reader-body').scrollTop, 137);
  assert.match(ui.get('reader-body').textContent, /这是上一轮/);
  saved.objective.id = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'; saved.current_run.objective_id = saved.objective.id; await ui.poll();
  assert.equal(ui.get('delivery-reader').hidden, true, 'a different objective cannot inherit a historical reader binding');
  assert.equal(ui.requests.filter((request) => request.path.includes('/file?')).length, 1); assert.equal(ui.posts().length, 0);
});

test('historical delivery rejects altered content or bindings without falling back to the current file', async () => {
  for (const mismatch of ['content', 'binding']) {
    const saved = objectiveDeliveryFixture(); const baseline = addObjectiveBaseline(saved);
    const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, route(request) {
      if (request.path.includes('/file?')) return { ...baseline.binding, content: mismatch === 'content' ? 'forged-body' : baseline.content,
        ...(mismatch === 'binding' ? { revision: 4 } : {}) };
    } });
    const button = descendants(ui.get('room-documents')).find((node) => node.dataset.documentPath === 'first.md' && node.dataset.documentKey?.includes(':baseline:'));
    button.fire('click'); await waitUntil(() => ui.get('reader-body').textContent.includes('版本无法确认'), 'a bad historical response is rejected');
    assert.doesNotMatch(ui.get('reader-body').textContent, /forged-body|这是上一轮|第一阶段的正文/); assert.equal(ui.posts().length, 0);
  }
});

test('an obsolete historical response cannot replace a later document or reopen a closed reader', async () => {
  const saved = objectiveDeliveryFixture(); const baseline = addObjectiveBaseline(saved); const pending = deferred(); const afterClose = deferred(); let reads = 0;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, route(request) {
    if (request.path.includes('/file?')) return ++reads === 1 ? pending.promise : afterClose.promise;
  } });
  const baselineButton = descendants(ui.document.querySelector('.running-delivery-history')).find((node) => node.dataset.documentPath === 'first.md' && node.dataset.documentKey?.includes(':baseline:'));
  baselineButton.fire('click'); await settle(); assert.match(ui.get('reader-body').textContent, /正在读取并核对/);
  const currentButton = ui.get('reader-files').children.find((node) => node.dataset.documentPath === 'second.md'); currentButton.fire('click'); await settle();
  assert.match(ui.get('reader-body').textContent, /第二阶段的正文/);
  pending.resolve({ ...baseline.binding, content: baseline.content }); await settle();
  assert.match(ui.get('reader-body').textContent, /第二阶段的正文/); assert.equal(ui.get('delivery-reader').hidden, false);
  await ui.click('reader-back'); baselineButton.fire('click'); await settle();
  await ui.click('reader-back'); await ui.click('new-room'); afterClose.resolve({ ...baseline.binding, content: baseline.content }); await settle();
  assert.equal(ui.get('delivery-reader').hidden, true); assert.equal(ui.posts().length, 0);
});

test('quota retirement and recorded task takeover stay visible without inventing independent review or changing members', async () => {
  const saved = objectiveSnapshot({ status: 'running' });
  saved.objective.retired_partners = { claude: { reason: 'subscription_limit_reached', at: 1100, cleanup_confirmed: true } };
  saved.team = { run_id: saved.current_run.id, tasks: [{ id: 'write', title: '补齐小工具说明', owner: 'codex', status: 'working', assignment_epoch: 2, review_kind: 'self_check' }] };
  saved.collaboration.tasks = saved.team.tasks;
  saved.jobs = [{ id: jobId, provider: 'codex', kind: 'team_work', status: 'working' }];
  saved.events = [
    { seq: 1, kind: 'job_created', run_id: saved.current_run.id, job_id: jobId, sender: 'service', recipient: 'codex', created_at: 1100 },
    { seq: 2, kind: 'job_claimed', run_id: saved.current_run.id, job_id: jobId, sender: 'codex', created_at: 1101 },
    { seq: 3, kind: 'task_taken_over', run_id: saved.current_run.id, task_id: 'write', previous_owner: 'claude', owner: 'codex', assignment_epoch: 2, created_at: 1102 },
    { seq: 4, kind: 'task_taken_over', run_id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee', task_id: 'unrelated', previous_owner: 'codex', owner: 'claude', assignment_epoch: 2, created_at: 900 },
  ];
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.equal(ui.document.querySelector('.objective-takeovers'), null, 'active chat keeps takeover in its actual event and partner cards');
  assert.match(ui.get('partner-activity-claude').textContent, /本轮暂不参与.*此前额度受限.*Codex 正在接着做/);
  assert.match(ui.get('partner-activity-codex').textContent, /正在处理.*补齐小工具说明/);
  assert.match(ui.get('events').textContent, /Codex 已接过 Claude Code 的子任务/);
  assert.match(ui.get('collaboration-plan').textContent, /只做自查/);
  assert.deepEqual(ui.members(), ['member-claude', 'member-codex']); assert.equal(ui.posts().length, 0);
  saved.objective.status = 'complete'; saved.objective.next_step = { decision: 'complete', review_kind: 'self_check' };
  saved.jobs.forEach(job => { job.status = 'completed'; }); saved.team.tasks[0].status = 'self_checked'; await ui.poll();
  assert.match(ui.document.querySelector('.objective-takeovers').textContent, /Codex 已接手.*Codex 接手 Claude Code 的「补齐小工具说明」/);
  assert.doesNotMatch(ui.document.querySelector('.objective-takeovers').textContent, /Claude Code 接手 Codex/);
  assert.match(ui.get('partner-activity-codex').textContent, /目标已交付/);
  assert.doesNotMatch(ui.get('partner-activity-codex').textContent, /正在处理/);
});

test('a definitive objective rejection clears the pending action without retrying or changing the budget', async () => {
  const saved = objectiveSnapshot();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, route(request) {
    if (request.path.endsWith('/continue')) return { error: 'objective_conflict' };
  } });
  await ui.click('continue-objective'); assert.equal(ui.get('pending-request').hidden, true);
  assert.match(ui.get('action-error').textContent, /目标记录已经变化/); await ui.poll();
  assert.equal(ui.posts().length, 1); assert.equal(saved.objective.budget.used, 13); assert.equal(saved.objective.deadline, 8200);
});

test('answering an objective decision uses the bound answer route and keeps its original budget and deadline', async () => {
  const saved = objectiveSnapshot({ status: 'needs_user', decision: 'needs_user', scope: 'current_goal' });
  saved.objective.can_answer = true; saved.objective.next_step.question = '先做说明还是先做检查？';
  saved.room.status = 'needs_user'; saved.current_run.status = 'stopped'; saved.current_run.can_continue = true;
  saved.collaboration.outcome.state = 'stopped';
  const originalPolicy = structuredClone(saved.current_run.execution_policy);
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, route(request) {
    if (request.path.endsWith('/answer')) { saved.objective.status = 'running'; saved.objective.can_answer = false; saved.objective.stage += 1; return saved; }
  } });
  assert.equal(ui.get('composer').hidden, false); assert.equal(ui.get('message').disabled, false);
  assert.match(ui.get('collaboration-outcome').textContent, /请在下方回复.*继续时沿用当前权限设置/);
  assert.equal(ui.get('send-label').textContent, '回答并继续'); ui.type('先补齐说明，再做检查。'); await ui.submit();
  const request = ui.posts()[0]; assert.equal(request.path, `/api/rooms/${roomId}/answer`);
  assert.deepEqual(Object.keys(request.body).sort(), ['digest', 'objective_id', 'request_id', 'revision', 'run_id', 'text']);
  assert.equal(request.body.objective_id, objectiveId); assert.equal(request.body.text, '先补齐说明，再做检查。');
  assert.equal(saved.objective.deadline, 8200); assert.equal(saved.objective.budget.used, 13); assert.equal(saved.objective.budget.limit, 64);
  assert.deepEqual(saved.current_run.execution_policy, originalPolicy); assert.equal(ui.get('message').value, '');
});

test('a needs-user answer cannot escape through legacy continuation and uncertain answers never replay on reload', async () => {
  const saved = objectiveSnapshot({ status: 'needs_user', decision: 'needs_user' }); saved.current_run.can_continue = true;
  saved.objective.can_answer = false;
  const storage = new Map(); const route = (request) => { if (request.path.endsWith('/answer')) throw new Error('fictional lost answer response'); };
  const ui = await browser({ rooms: [saved.room], saved, storage, executionPolicySupport: true, route });
  ui.type('不能变成新目标的回答'); await ui.submit(); assert.equal(ui.posts().length, 0); assert.equal(ui.get('send').disabled, true);
  saved.objective.can_answer = true; await ui.poll(); await ui.submit();
  const original = structuredClone(ui.posts()[0]); assert.equal(original.path.endsWith('/answer'), true);
  const reloaded = await browser({ rooms: [saved.room], saved, storage, executionPolicySupport: true, route(request) {
    if (request.path.endsWith('/answer')) return saved;
  } });
  assert.equal(reloaded.posts().length, 0); assert.match(reloaded.get('pending-description').textContent, /回答问题.*同一目标/);
  await reloaded.click('retry-request'); assert.deepEqual(structuredClone(reloaded.posts()[0]), original);
  assert.equal(saved.objective.deadline, 8200); assert.equal(saved.objective.budget.used, 13);
});

test('explicit quota failover allows only known exhaustion with another selected partner and keeps identity uncertainty blocked', async () => {
  const partners = availabilityPartners(); const saved = objectiveSnapshot({ status: 'running' });
  const ui = await browser({ agents: catalog.slice(0, 2), partners, now: () => 1000000, executionPolicySupport: true, route(request) {
    if (request.path === '/api/availability/refresh') return { partners };
    if (request.path === '/api/rooms' && request.method === 'POST') return saved;
  } });
  ui.type('由原本选中的伙伴完成这个目标');
  partners[0].availability = availabilityFixture({ state: 'blocked', selectable: false, reason: 'subscription_limit_reached' }); await ui.poll();
  assert.equal(ui.get('send').disabled, false); assert.match(ui.get('composer-note').textContent, /将由 Codex 继续.*Claude Code 额度不足/);
  assert.equal(participantInput(ui, 'claude').checked, true);
  partners[0].availability.reason = 'account_identity_unknown'; await ui.poll(); assert.equal(ui.get('send').disabled, true);
  partners[0].availability.reason = 'quota_exhausted'; partners[1].availability = availabilityFixture({ state: 'blocked', selectable: false, reason: 'quota_exhausted' }); await ui.poll();
  assert.equal(ui.get('send').disabled, true, 'no extra partner is added when every selected partner is blocked');
  partners[1].availability = availabilityFixture(); await ui.poll(); await ui.submit();
  const request = ui.posts().find((entry) => entry.path === '/api/rooms'); assert.ok(request);
  assert.deepEqual(request.body.participants, ['claude', 'codex']); assert.equal(request.body.execution_policy.quota_failover, true);
});

test('a pending policy create is quarantined rather than stripped or retried through an older service', async () => {
  const storage = new Map();
  const ui = await browser({ agents: catalog.slice(0, 2), storage, executionPolicySupport: true, route(request) {
    if (request.path === '/api/rooms' && request.method === 'POST') throw new Error('fictional lost create response');
  } });
  ui.type('不能在降级服务中重放的自主目标'); await ui.submit(); const original = structuredClone(ui.posts()[0]);
  const reloaded = await browser({ agents: catalog.slice(0, 2), storage });
  assert.equal(reloaded.get('pending-request').hidden, false); await reloaded.click('retry-request');
  assert.equal(reloaded.posts().length, 0); assert.equal(reloaded.get('pending-request').hidden, false);
  assert.match(reloaded.get('notice').textContent, /当前服务尚未提供.*原请求已保留.*没有重新发送/);
  const retained = savedOutbox(storage).request;
  assert.deepEqual(retained.body, original.body); assert.equal(retained.kind, 'create');
});

test('self-check and mixed validation scopes are explicit instead of implying independent review or unknown verification', async () => {
  const saved = objectiveSnapshot({ status: 'complete', decision: 'complete' });
  saved.objective.next_step.goal = null;
  saved.collaboration.outcome.validation.scope = 'self_check';
  saved.collaboration.outcome.validation.summary = '本次由作者自检，尚未进行独立审核。';
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  const checkLabel = () => ui.document.querySelector('.delivery-details').textContent;
  assert.match(checkLabel(), /检查范围：只做了自查，没有别的伙伴看过/);
  assert.match(ui.get('collaboration-outcome').textContent, /本轮已结束，无需回复或确认/);
  assert.equal(ui.get('message').disabled, true); assert.equal(ui.get('new-room').disabled, false);
  saved.collaboration.outcome.validation.scope = 'mixed_review';
  saved.collaboration.outcome.validation.summary = '部分成果由作者自检，其余独立审核范围见任务记录。'; await ui.poll();
  assert.match(checkLabel(), /检查范围：有的只做了自查，见详情/);
  assert.equal(ui.posts().length, 0);
});

test('history labels prefer authoritative objective state and keep lifecycle and legacy fallbacks', async () => {
  const saved = objectiveSnapshot(); saved.room.outcome_state = 'ready_for_user'; saved.room.objective_status = 'awaiting_continue';
  const ui = await browser({ rooms: [saved.room], saved, selectSaved: false, executionPolicySupport: true });
  const historyStatus = () => ui.get('rooms').firstChild.lastChild.firstChild.textContent;
  assert.equal(historyStatus(), '阶段已交付'); assert.doesNotMatch(historyStatus(), /待.*验收/);
  saved.room.objective_status = 'paused'; await ui.poll(); assert.equal(historyStatus(), '暂停', 'a list-only projection does not assert that in-flight work has ended');
  saved.room.objective_pause_requested = false; await ui.poll(); assert.equal(historyStatus(), '已暂停');
  delete saved.room.objective_status; saved.objective.status = 'complete'; ui.get('rooms').firstChild.fire('click'); await settle();
  assert.equal(historyStatus(), '目标已交付', 'reading a snapshot fills the list state from its bound objective');
  saved.room.lifecycle_state = 'archived'; await ui.poll(); selectLifecycleView(ui, 'rooms', 'archived'); assert.equal(historyStatus(), '已归档');
  const legacyRoom = { id: roomId, goal: '旧记录', status: 'ready_for_user', outcome_state: 'ready_for_user', objective_status: 'unrecognized', created_at: 1 };
  const legacy = await browser({ rooms: [legacyRoom], selectSaved: false, executionPolicySupport: true });
  assert.equal(legacy.get('rooms').firstChild.lastChild.firstChild.textContent, '已交付，待你验收');
  assert.equal(ui.posts().length + legacy.posts().length, 0);
});

test('objective titles stay on the original goal across short stage goals and preserve legacy titles', async () => {
  const saved = objectiveSnapshot(); saved.room.goal = '完善原目标说明及已有文件';
  saved.objective.original_goal = '制作加法和汇总工具，分两阶段交付';
  saved.room.objective_goal = saved.objective.original_goal;
  const other = { ...saved.room, id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', objective_goal: '另一个独立目标', updated_at: 0 };
  const ui = await browser({ rooms: [saved.room, other], saved, selectSaved: false, executionPolicySupport: true });
  ui.get('rooms').children[0].fire('click'); await settle();
  assert.equal(ui.get('rooms').children[0].firstChild.textContent, saved.objective.original_goal);
  assert.equal(ui.get('rooms').children[1].firstChild.textContent, '另一个独立目标');
  assert.equal(ui.get('room-title').textContent, saved.objective.original_goal); assert.equal(ui.get('room-title').title, saved.objective.original_goal);
  delete saved.room.objective_goal; saved.room.goal = '准备下一阶段'; await ui.poll();
  assert.equal(ui.get('rooms').children[0].title, saved.objective.original_goal, 'snapshot backfills the original goal into the summary');
  assert.equal(ui.get('room-title').textContent, saved.objective.original_goal); assert.equal(saved.room.goal, '准备下一阶段');
  const legacy = await browser({ rooms: [{ id: roomId, goal: '旧任务原来的标题', status: 'completed' }] });
  assert.equal(legacy.get('rooms').firstChild.firstChild.textContent, '旧任务原来的标题'); assert.equal(ui.posts().length + legacy.posts().length, 0);
});

test('old objective questions and summaries never appear after answering or changing the current result version', async () => {
  const saved = objectiveSnapshot({ status: 'needs_user', decision: 'needs_user' });
  saved.objective.next_step.summary = '先确认文档语言'; saved.objective.next_step.question = '说明文档用简体中文吗？';
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.match(ui.get('collaboration-outcome').textContent, /当前需要处理.*说明文档用简体中文吗/);
  saved.current_run.id = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'; saved.objective.status = 'paused'; await ui.poll();
  assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /先确认文档语言|说明文档用简体中文吗/);
  saved.objective.status = 'needs_user'; saved.objective.next_step.binding.run_id = saved.current_run.id; saved.current_run.revision = 2; await ui.poll();
  assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /先确认文档语言|说明文档用简体中文吗/);
  saved.objective.next_step.binding.revision = 2; saved.current_run.digest = 'e'.repeat(64); await ui.poll();
  assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /先确认文档语言|说明文档用简体中文吗/);
  saved.objective.next_step.binding.digest = saved.current_run.digest; await ui.poll();
  assert.match(ui.get('collaboration-outcome').textContent, /当前需要处理.*说明文档用简体中文吗/); assert.equal(ui.posts().length, 0);
});

function restartSnapshot({ legacy = false } = {}) {
  const saved = objectiveSnapshot({ status: 'stopped' });
  saved.current_run.status = 'stopped'; saved.current_run.can_accept = false; saved.current_run.can_restart_objective = true;
  saved.current_run.restart_objective_reason = null; saved.collaboration = teamCollaboration({ state: 'stopped' });
  saved.room.status = 'needs_user';
  if (legacy) { delete saved.objective; delete saved.current_run.objective_id; delete saved.current_run.execution_policy; }
  return saved;
}

test('restart keeps the original target while removing historical finite limits without a settings dialog', async () => {
  const saved = restartSnapshot(); saved.current_run.execution_policy.max_calls = 32;
  const original = structuredClone(saved); let sent;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, continuousObjectiveSupport: true, route(request) {
    if (request.path.endsWith('/restart-objective')) { sent = request; return saved; }
  } });
  assert.equal(ui.get('composer').hidden, false); assert.equal(ui.get('restart-objective').disabled, false);
  assert.equal(ui.document.getElementById('restart-policy'), null);
  assert.match(ui.get('collaboration-outcome').textContent, /继续/);
  assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /最多32次处理/);
  assert.equal(ui.posts().length, 0); await ui.click('restart-objective');
  assert.equal(sent.path, `/api/rooms/${roomId}/restart-objective`);
  assert.deepEqual(Object.keys(sent.body).sort(), ['digest', 'execution_policy', 'request_id', 'revision', 'run_id']);
  assert.equal(sent.body.run_id, original.current_run.id); assert.equal(sent.body.digest, original.current_run.digest);
  assert.equal(sent.body.revision, original.current_run.revision); assert.equal(sent.body.execution_policy.max_calls, null); assert.equal(sent.body.execution_policy.max_seconds, null);
  assert.equal(saved.objective.id, original.objective.id); assert.equal(saved.objective.original_goal, original.objective.original_goal);
  assert.deepEqual(saved.objective.budget, original.objective.budget, 'UI never resets or edits the stopped snapshot in place');
});

test('a legacy stopped task can explicitly choose continuous recovery but unknown cleanup and missing capability remain blocked', async () => {
  const saved = restartSnapshot({ legacy: true }); saved.current_run.can_retry = true;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, continuousObjectiveSupport: true, route(request) {
    if (request.path.endsWith('/restart-objective')) return saved;
  } });
  assert.equal(ui.document.getElementById('retry-run'), null, 'one clear recovery action replaces the redundant legacy retry');
  assert.equal(ui.get('composer').hidden, false); assert.equal(ui.get('terminal-composer-note').hidden, true);
  assert.equal(ui.document.getElementById('restart-policy'), null);
  saved.current_run.can_restart_objective = false; saved.current_run.can_retry = false;
  saved.current_run.restart_objective_reason = 'team_cleanup_unconfirmed'; await ui.poll();
  assert.equal(ui.get('restart-objective').disabled, true); assert.match(ui.get('collaboration-outcome').textContent, /执行或清理尚未确认/);
  await ui.click('restart-objective'); assert.equal(ui.posts().length, 0);
  saved.current_run.can_restart_objective = true; await ui.poll(); await ui.click('restart-objective');
  assert.deepEqual(ui.posts()[0].body.execution_policy, { mode: 'autonomous', max_seconds: null, max_calls: null, quota_failover: true });
  const old = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, continuousObjectiveSupport: false });
  assert.equal(old.document.getElementById('restart-objective'), null); assert.equal(old.posts().length, 0);
});

test('uncertain recovery is retained without replay, quarantined on old services, and explicitly retried with the same binding and policy', async () => {
  const saved = restartSnapshot({ legacy: true }); const storage = new Map();
  const ui = await browser({ rooms: [saved.room], saved, storage, executionPolicySupport: true, continuousObjectiveSupport: true, route(request) {
    if (request.path.endsWith('/restart-objective')) throw new Error('fictional lost recovery result');
  } });
  await ui.click('restart-objective'); const original = structuredClone(ui.posts()[0]); await ui.poll(); assert.equal(ui.posts().length, 1);
  const old = await browser({ rooms: [saved.room], saved, storage, executionPolicySupport: true, continuousObjectiveSupport: false });
  assert.equal(old.get('pending-request').hidden, false); await old.click('retry-request'); assert.equal(old.posts().length, 0);
  assert.match(old.get('notice').textContent, /不支持这份持续推进请求.*原请求已保留.*没有重新发送/);
  assert.deepEqual(savedOutbox(storage).request.body, original.body);
  const reloaded = await browser({ rooms: [saved.room], saved, storage, executionPolicySupport: true, continuousObjectiveSupport: true, route(request) {
    if (request.path.endsWith('/restart-objective')) return saved;
  } });
  assert.equal(reloaded.posts().length, 0); await reloaded.click('retry-request');
  assert.equal(reloaded.posts()[0].path, original.path); assert.deepEqual(reloaded.posts()[0].body, original.body);
});

test('a pending uncapped create cannot fall back to a finite policy on an older service', async () => {
  const storage = new Map();
  const ui = await browser({ agents: catalog.slice(0, 2), storage, executionPolicySupport: true, continuousObjectiveSupport: true, route(request) {
    if (request.path === '/api/rooms' && request.method === 'POST') throw new Error('fictional lost uncapped create');
  } });
  ui.type('持续推进的原请求'); await ui.submit(); const original = structuredClone(ui.posts()[0]);
  assert.equal(original.body.execution_policy.max_seconds, null); assert.equal(original.body.execution_policy.max_calls, null);
  const old = await browser({ agents: catalog.slice(0, 2), storage, executionPolicySupport: true, continuousObjectiveSupport: false }); await old.click('retry-request');
  assert.equal(old.posts().length, 0); assert.deepEqual(savedOutbox(storage).request.body, original.body);
});

function nativeProjectFixture() { return { id: '33333333-3333-4333-8333-333333333333', name: '虚构开发项目', path: '/fictional/native-project', files: [], selection_kind: 'folder' }; }
function nativeHandoffSnapshot(source) {
  const saved = objectiveSnapshot({ status: 'running' }); saved.room.id = '77777777-7777-4777-8777-777777777777'; saved.room.mode = 'native_project';
  saved.current_run.id = '88888888-8888-4888-8888-888888888888'; saved.current_run.runtime_settings = { permission: 'full_access', members: {} };
  saved.current_run.execution_policy = { mode: 'autonomous', max_seconds: null, max_calls: null, quota_failover: true };
  saved.native_handoff = { source_room_id: source.room.id, source_run_id: source.current_run.id, revision: source.current_run.revision, digest: source.current_run.digest };
  return saved;
}

test('native full access requires both explicit folder workflow and server capability and never becomes the default permission', async () => {
  const project = nativeProjectFixture(); const saved = objectiveSnapshot({ status: 'running' }); saved.room.mode = 'native_project';
  const ui = await browser({ agents: catalog.slice(0, 2), projects: [project], executionPolicySupport: true, continuousObjectiveSupport: true, nativeProjectSupport: true, route(request) {
    if (request.path === '/api/runtime-options') return runtimeOptionsFixture();
    if (request.path === '/api/rooms' && request.method === 'POST') return saved;
  } });
  selectRuntime(ui, 'project-picker', project.id); assert.equal(ui.get('project-workflow-control').hidden, false);
  await openRuntimeSettings(ui); assert.equal(ui.get('runtime-permission').children.some((node) => node.value === 'full_access'), false);
  await ui.click('runtime-settings-close'); selectRuntime(ui, 'project-workflow', 'native_project'); await openRuntimeSettings(ui);
  assert.equal(ui.get('runtime-permission').value, 'workspace_write'); assert.ok(ui.get('runtime-permission').children.some((node) => node.value === 'full_access'));
  assert.match(ui.get('runtime-settings-note').textContent, /Full Access.*按任务要求读写本机文件.*执行命令和联网/);
  selectRuntime(ui, 'runtime-permission', 'full_access'); await ui.click('runtime-settings-close');
  assert.match(ui.get('runtime-settings-summary').textContent, /Full Access/); assert.match(ui.get('scope-description').textContent, /Plan 使用原生规划模式.*Full Access 可按任务要求读写本机文件/);
  ui.type('在虚构项目中完成开发'); assert.equal(ui.get('composer-note').textContent, '按任务要求和所选原生权限执行'); await ui.submit();
  assert.equal(ui.posts()[0].body.mode, 'native_project'); assert.equal(ui.posts()[0].body.project_id, project.id);
  assert.equal(ui.posts()[0].body.runtime_settings.permission, 'full_access'); assert.equal(ui.posts()[0].body.execution_policy.max_calls, null);
  const old = await browser({ agents: catalog.slice(0, 2), projects: [project], taskModes: ['text', 'native_project'], executionPolicySupport: true, route(request) {
    if (request.path === '/api/runtime-options') return runtimeOptionsFixture();
  } });
  selectRuntime(old, 'project-picker', project.id); assert.equal(old.get('project-workflow-control').hidden, true);
  selectRuntime(old, 'project-workflow', 'native_project'); await openRuntimeSettings(old);
  assert.equal(old.get('runtime-permission').children.some((node) => node.value === 'full_access'), false); assert.equal(old.posts().length, 0);
});

test('native handoff is an explicit bound action with separately selected permissions and models, not an edited goal or old session replay', async () => {
  const source = restartSnapshot(); source.room.mode = 'project_folder'; source.room.project_id = nativeProjectFixture().id;
  source.current_run.can_native_handoff = true; const original = structuredClone(source); const next = nativeHandoffSnapshot(source);
  const ui = await browser({ rooms: [source.room], saved: source, projects: [nativeProjectFixture()], executionPolicySupport: true, continuousObjectiveSupport: true, nativeProjectSupport: true, route(request) {
    if (request.path === '/api/runtime-options' || request.path.endsWith('/runtime/refresh')) return runtimeOptionsFixture();
    if (request.path.endsWith('/native-handoff')) return next;
  } });
  assert.equal(ui.document.getElementById('restart-objective'), null); assert.equal(ui.get('native-handoff').textContent, '继续');
  await openRuntimeSettings(ui); assert.equal(ui.get('runtime-permission').value, 'workspace_write'); selectRuntime(ui, 'runtime-permission', 'full_access');
  await ui.click('runtime-settings-close'); assert.equal(ui.document.activeElement.id, 'runtime-settings');
  await ui.click('runtime-shortcut-codex'); selectRuntime(ui, 'runtime-model-codex', 'observed-codex-model'); selectRuntime(ui, 'runtime-effort-codex', 'xhigh'); await ui.click('runtime-settings-close');
  assert.equal(ui.posts().filter((entry) => !entry.path.endsWith('/runtime/refresh')).length, 0); await ui.click('native-handoff');
  const request = ui.posts().find((entry) => entry.path.endsWith('/native-handoff'));
  assert.deepEqual(Object.keys(request.body).sort(), ['digest', 'execution_policy', 'request_id', 'revision', 'run_id', 'runtime_settings']);
  assert.equal(request.body.digest, original.current_run.digest); assert.equal(request.body.run_id, original.current_run.id);
  assert.equal(request.body.runtime_settings.permission, 'full_access'); assert.deepEqual(request.body.runtime_settings.members.codex, { model: 'observed-codex-model', effort: 'xhigh' });
  assert.equal(ui.get('pending-request').hidden, true); assert.equal(ui.get('earlier-discussion').hidden, false);
  assert.equal(ui.document.getElementById('native-handoff-source'), null);
  assert.deepEqual(source, original, 'reading and handing off never changes the original record in the UI');
  const title = ui.get('room-title').textContent; ui.get('earlier-discussion').open = true; ui.get('earlier-discussion').fire('toggle'); await settle();
  assert.equal(ui.get('room-title').textContent, title, 'earlier discussion opens inside the current chat');
});

test('native handoff uses authority gates and retains uncertain raw requests across reload without capability downgrade or automatic replay', async () => {
  const saved = restartSnapshot({ legacy: true }); saved.room.mode = 'project_folder'; saved.current_run.can_native_handoff = false;
  saved.current_run.can_restart_objective = false; saved.current_run.native_handoff_reason = 'team_cleanup_unconfirmed'; const storage = new Map();
  const ui = await browser({ rooms: [saved.room], saved, storage, executionPolicySupport: true, continuousObjectiveSupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.endsWith('/native-handoff')) throw new Error('fictional lost native handoff');
  } });
  assert.equal(ui.get('native-handoff').disabled, true); await ui.click('native-handoff'); assert.equal(ui.posts().length, 0);
  saved.current_run.can_native_handoff = true; await ui.poll(); await ui.click('native-handoff');
  const original = structuredClone(ui.posts()[0]); assert.equal(ui.get('pending-request').hidden, false); await ui.poll(); assert.equal(ui.posts().length, 1);
  const old = await browser({ rooms: [saved.room], saved, storage, executionPolicySupport: true, continuousObjectiveSupport: true });
  await old.click('retry-request'); assert.equal(old.posts().length, 0); assert.match(old.get('notice').textContent, /不支持完整开发.*原请求已保留.*没有降低权限或重新发送/);
  assert.deepEqual(savedOutbox(storage).request.body, original.body);
  const reloaded = await browser({ rooms: [saved.room], saved, storage, executionPolicySupport: true, continuousObjectiveSupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.endsWith('/native-handoff')) return nativeHandoffSnapshot(saved);
  } });
  assert.equal(reloaded.posts().length, 0); await reloaded.click('retry-request'); assert.deepEqual(reloaded.posts()[0].body, original.body);
  assert.equal(reloaded.get('pending-request').hidden, true);
});

test('a working partner card says how recently it was active, and warns only after ten silent minutes', async () => {
  // Catches the card going back to a bare "running command", a heartbeat being reported as a fresh message, and a false stuck warning.
  const now = Date.now() / 1000;
  const saved = activeActivitySnapshot(); saved.room.mode = 'native_project';
  saved.jobs[0].claimed_at = now - 5800;
  saved.events.push({ seq: 100, kind: 'message', sender: 'claude', job_id: saved.jobs[0].id, run_id: saved.current_run.id, native_progress: true, text: '先读委托池的规则。', created_at: now - 4580 });
  saved.events.push({ seq: 101, kind: 'native_tool_activity', sender: 'claude', job_id: saved.jobs[0].id, run_id: saved.current_run.id, state: 'running', tool_kind: 'Bash', heartbeat: true, elapsed_time_seconds: 725, created_at: now - 20 });
  const ui = await browser({ rooms: [saved.room], saved, nativeProjectSupport: true });
  assert.equal(ui.get('partner-liveness-claude').textContent, '刚刚有活动 · 当前工具已运行 12 分钟 · 这一步已做 1 小时 36 分钟 · 上次发言 1 小时 16 分钟前');
  assert.doesNotMatch(ui.get('partner-liveness-claude').className, /warning/);
  saved.events.at(-1).created_at = now - 740; saved.events.at(-2).created_at = now - 4580; await ui.poll();
  assert.equal(ui.get('partner-liveness-claude').textContent, '已 12 分钟没有新记录，可能卡住了');
  assert.match(ui.get('partner-liveness-claude').className, /warning/);
  saved.jobs[0].status = 'completed'; await ui.poll();
  assert.throws(() => ui.get('partner-liveness-claude'), /missing/);
});

function progressCard({ age = 5, status = 'working' } = {}) {
  const now = Date.now() / 1000;
  const saved = activeActivitySnapshot(); saved.room.mode = 'native_project';
  saved.jobs[0].status = status; saved.jobs[0].claimed_at = now - 100;
  saved.events.push({ seq: 100, kind: 'native_progress_status', sender: 'claude', job_id: saved.jobs[0].id, run_id: saved.current_run.id, text: '96/182 done, 0 failed', created_at: now - age });
  return saved;
}

test('a working partner card shows the latest command progress under the liveness line, with its age', async () => {
  // Catches the line missing, placed above liveness, or carrying a wrong age for recent and minutes-old updates.
  const saved = progressCard();
  const ui = await browser({ rooms: [saved.room], saved, nativeProjectSupport: true });
  assert.equal(ui.get('partner-progress-status-claude').textContent, '进度：96/182 done, 0 failed · 刚刚');
  const lines = ui.get('public-progress-claude').children;
  assert.equal(lines.indexOf(ui.get('partner-progress-status-claude')), lines.indexOf(ui.get('partner-liveness-claude')) + 1);
  saved.events.at(-1).created_at = Date.now() / 1000 - 125; await ui.poll();
  assert.equal(ui.get('partner-progress-status-claude').textContent, '进度：96/182 done, 0 failed · 2 分钟前');
});

test('command progress older than ten minutes says it may be out of date, and not before', async () => {
  // Catches an old line reading as current, and the warning arriving early.
  const fresh = progressCard({ age: 595 });
  const ui = await browser({ rooms: [fresh.room], saved: fresh, nativeProjectSupport: true });
  assert.equal(ui.get('partner-progress-status-claude').textContent, '进度：96/182 done, 0 failed · 9 分钟前');
  fresh.events.at(-1).created_at = Date.now() / 1000 - 605; await ui.poll();
  assert.equal(ui.get('partner-progress-status-claude').textContent, '进度：96/182 done, 0 failed · 10 分钟前（可能已过期）');
});

test('command progress appears only while the job is working', async () => {
  // Catches a stale line on a queued job, or one left on the card after the job ended.
  const queued = progressCard({ status: 'queued' });
  const waiting = await browser({ rooms: [queued.room], saved: queued, nativeProjectSupport: true });
  assert.throws(() => waiting.get('partner-progress-status-claude'), /missing/);
  const saved = progressCard();
  const ui = await browser({ rooms: [saved.room], saved, nativeProjectSupport: true });
  assert.ok(ui.get('partner-progress-status-claude'));
  saved.jobs[0].status = 'completed'; await ui.poll();
  assert.throws(() => ui.get('partner-progress-status-claude'), /missing/);
});

test('command progress is untrusted display text: no markup, no chat line, no credit as activity', async () => {
  // Catches rendering file text as markup, putting it in the timeline, or a fresh line hiding a stuck warning.
  const saved = progressCard();
  saved.jobs[0].claimed_at = Date.now() / 1000 - 900;
  saved.events[saved.events.length - 1].text = '<img src=x onerror=alert(1)> 96/182';
  const ui = await browser({ rooms: [saved.room], saved, nativeProjectSupport: true });
  assert.equal(ui.get('partner-progress-status-claude').textContent, '进度：<img src=x onerror=alert(1)> 96/182 · 刚刚');
  assert.equal(ui.get('partner-progress-status-claude').children.length, 0);
  assert.doesNotMatch(ui.get('events').textContent, /96\/182/);
  assert.match(ui.get('partner-liveness-claude').textContent, /^已 \d+ 分钟没有新记录，可能卡住了$/);
  assert.match(ui.get('partner-liveness-claude').className, /warning/);
});

test('command progress reads in English, with the unit agreeing in number', async () => {
  // Catches Chinese copy in the English interface and "1 minutes ago".
  const saved = progressCard({ age: 65 });
  const ui = await browser({ language: 'en', rooms: [saved.room], saved, nativeProjectSupport: true });
  assert.equal(ui.get('partner-progress-status-claude').textContent, 'Progress: 96/182 done, 0 failed · 1 minute ago');
  saved.events.at(-1).created_at = Date.now() / 1000 - 700; await ui.poll();
  assert.equal(ui.get('partner-progress-status-claude').textContent, 'Progress: 96/182 done, 0 failed · 11 minutes ago (may be out of date)');
});

test('native tool activity is bound to the active provider job and never masquerades as tests passing or leaks raw command receipts into chat', async () => {
  const saved = activeActivitySnapshot(); saved.room.mode = 'native_project';
  saved.events.push({ seq: 100, kind: 'native_tool_activity', sender: 'claude', job_id: saved.jobs[0].id, run_id: saved.current_run.id, state: 'started', description: '正在使用工具',
    created_at: Math.max(...saved.events.map((event) => event.created_at || 0)) + 1 });
  saved.native_executions = [{ job_id: jobId, provider: 'claude', run_id: saved.current_run.id, phase: 'work', permission: 'full_access', tool_calls: 3, tool_results: 3,
    cleanup_confirmed: true, exit_code: 0, duration_seconds: 1.25, status: 'completed', report_trust: 'native_tool_observation', receipt_path: '/fictional/private-command-receipt' }];
  saved.native_workspaces = [{ provider: 'claude', state: 'blocked', conflicts: [{ path: 'main.py', reason: 'native_merge_conflict' }], blocked: [{ path: 'art.png', reason: 'native_binary_not_imported' }, { path: 'other.py', reason: 'native_path_not_owned' }, { path: 'old.py', reason: 'native_unresolved_conflict' }] }];
  const ui = await browser({ rooms: [saved.room], saved, nativeProjectSupport: true });
  assert.match(ui.get('partner-activity-claude').textContent, /正在使用本机工具.*实现计分规则/);
  assert.match(ui.get('collaboration-outcome').textContent, /有 4 项改动.*main.py.*新旧版本冲突.*art.png.*二进制.*other.py.*授权写入范围.*old.py.*旧版本冲突尚未明确解决/);
  assert.match(ui.get('native-execution-records').textContent, /工具调用 3 次.*工具返回 3 次.*退出码 0.*已确认清理/);
  assert.match(ui.get('native-execution-records').textContent, /不等于测试、游戏操作或目标已验收/);
  assert.doesNotMatch(ui.get('events').textContent, /正在使用工具|private-command-receipt|工具调用 3/);
  assert.doesNotMatch(ui.get('native-execution-records').textContent, /private-command-receipt|测试通过/);
  saved.events.at(-1).state = 'running'; saved.events.at(-1).description = '工具仍在运行'; saved.events.at(-1).tool_kind = 'Bash';
  saved.events.at(-1).heartbeat = true; saved.events.at(-1).elapsed_time_seconds = 65.9; await ui.poll();
  assert.match(ui.get('partner-activity-claude').textContent, /正在执行命令.*已运行 65 秒.*只表示工具仍在运行/);
  assert.match(ui.get('native-execution-records').textContent, /工具调用 3 次.*工具返回 3 次/);
  assert.doesNotMatch(ui.get('events').textContent, /工具仍在运行|已运行 65 秒/);
  saved.events.at(-1).state = 'completed'; saved.events.at(-1).description = '工具已返回'; await ui.poll();
  assert.match(ui.get('partner-activity-claude').textContent, /处理中 · 最近完成：执行命令/);
  saved.current_run.status = 'stopped'; await ui.poll(); assert.doesNotMatch(ui.get('partner-activity-claude').textContent, /正在使用本机工具|处理中 · 最近完成：执行命令/);
  assert.equal(ui.posts().length, 0);
});

test('a pending native full-access create is preserved on an old service instead of being sent with less power', async () => {
  const project = nativeProjectFixture(); const storage = new Map();
  const ui = await browser({ agents: catalog.slice(0, 2), projects: [project], storage, executionPolicySupport: true, continuousObjectiveSupport: true, nativeProjectSupport: true, route(request) {
    if (request.path === '/api/runtime-options') return runtimeOptionsFixture();
    if (request.path === '/api/rooms' && request.method === 'POST') throw new Error('fictional lost native create');
  } });
  selectRuntime(ui, 'project-picker', project.id); selectRuntime(ui, 'project-workflow', 'native_project');
  await openRuntimeSettings(ui); selectRuntime(ui, 'runtime-permission', 'full_access'); await ui.click('runtime-settings-close');
  ui.type('保留完整开发的原始请求'); await ui.submit(); const original = structuredClone(ui.posts()[0]);
  const old = await browser({ agents: catalog.slice(0, 2), projects: [project], storage, executionPolicySupport: true, continuousObjectiveSupport: true });
  assert.equal(old.get('pending-request').hidden, false); await old.click('retry-request'); assert.equal(old.posts().length, 0);
  assert.deepEqual(savedOutbox(storage).request.body, original.body);
  assert.equal(original.body.runtime_settings.permission, 'full_access'); assert.equal(original.body.mode, 'native_project');
});

test('a mismatched native handoff response remains uncertain and cannot replace the original task', async () => {
  const source = restartSnapshot({ legacy: true }); source.room.mode = 'project_folder'; source.current_run.can_native_handoff = true;
  const wrong = nativeHandoffSnapshot(source); wrong.native_handoff.digest = 'e'.repeat(64);
  const ui = await browser({ rooms: [source.room], saved: source, executionPolicySupport: true, continuousObjectiveSupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.endsWith('/native-handoff')) return wrong;
  } });
  await ui.click('native-handoff'); assert.equal(ui.get('pending-request').hidden, false);
  assert.match(ui.get('notice').textContent, /结果未确认.*原请求已保留/);
  assert.equal(ui.document.getElementById('native-handoff-source'), null); assert.ok(ui.get('native-handoff'));
  await ui.poll(); assert.equal(ui.posts().length, 1);
});

test('a native baseline reads only its exact source room and frozen file binding without switching the current task', async () => {
  const saved = objectiveDeliveryFixture(); saved.room.mode = 'native_project'; const baseline = addObjectiveBaseline(saved);
  const sourceRoomId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'; saved.objective.baseline.source_room_id = sourceRoomId;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.includes('/file?')) {
      const url = new URL(request.path, 'http://fictional.invalid'); assert.equal(url.pathname, `/api/rooms/${sourceRoomId}/file`);
      assert.deepEqual(Object.fromEntries(url.searchParams), { ...baseline.binding, revision: String(baseline.binding.revision) });
      return { ...baseline.binding, content: baseline.content };
    }
  } });
  const title = ui.get('room-title').textContent;
  const button = descendants(ui.get('room-documents')).find((node) => node.dataset.documentPath === 'first.md' && node.dataset.documentKey?.includes(':baseline:'));
  assert.ok(button.dataset.documentKey.includes(sourceRoomId)); button.fire('click');
  await waitUntil(() => ui.get('reader-body').textContent.includes('这是上一轮'), 'the old-room body passes its exact SHA binding');
  assert.equal(ui.get('room-title').textContent, title); assert.equal(ui.requests.filter((request) => request.path.includes('/file?')).length, 1);
  assert.doesNotMatch(ui.get('reader-body').textContent, /第一阶段的正文/); assert.equal(ui.posts().length, 0);
});

test('a changed or invalid baseline source room never accepts a late body or falls back to the current room', async () => {
  const saved = objectiveDeliveryFixture(); const baseline = addObjectiveBaseline(saved);
  const firstSource = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'; const secondSource = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
  saved.objective.baseline.source_room_id = firstSource; let finishOld;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.startsWith(`/api/rooms/${firstSource}/file?`)) return new Promise((resolve) => { finishOld = resolve; });
    if (request.path.startsWith(`/api/rooms/${secondSource}/file?`)) return { ...baseline.binding, content: baseline.content };
  } });
  const openBaseline = () => descendants(ui.get('room-documents')).find((node) => node.dataset.documentPath === 'first.md' && node.dataset.documentKey?.includes(':baseline:')).fire('click');
  openBaseline(); await settle(); assert.equal(typeof finishOld, 'function');
  saved.objective.baseline.source_room_id = secondSource; await ui.poll(); finishOld({ ...baseline.binding, content: baseline.content }); await settle();
  assert.doesNotMatch(ui.get('reader-body').textContent, /这是上一轮|第一阶段的正文/, 'an old source cannot finish a reader still open on the same objective');
  openBaseline(); await waitUntil(() => ui.get('reader-body').textContent.includes('这是上一轮'), 'reopening uses the newly frozen source');
  await ui.click('reader-back'); saved.objective.baseline.source_room_id = '../../not-a-room'; await ui.poll();
  assert.equal(descendants(ui.get('room-documents')).some(node => node.dataset.documentKey?.includes(':baseline:')), false, 'an invalid explicit source is not replaced with the current room');
  const reads = ui.requests.filter((request) => request.path.includes('/file?'));
  assert.equal(reads.length, 2); assert.ok(reads.every((request) => !request.path.startsWith(`/api/rooms/${roomId}/file?`)));
  assert.equal(ui.posts().length, 0);
});

test('native read-only permissions describe provider-specific read commands without changing static project limits', async () => {
  const project = nativeProjectFixture();
  const ui = await browser({ agents: catalog.slice(0, 2), projects: [project], nativeProjectSupport: true, route(request) {
    if (request.path === '/api/runtime-options') return runtimeOptionsFixture();
  } });
  selectRuntime(ui, 'project-picker', project.id); selectRuntime(ui, 'project-workflow', 'native_project');
  await openRuntimeSettings(ui); selectRuntime(ui, 'runtime-permission', 'read_only');
  assert.match(ui.get('runtime-settings-note').textContent, /历史只读设置.*原有只读语义.*对应工具决定/);
  assert.doesNotMatch(ui.get('runtime-settings-note').textContent, /不允许任何命令|禁止所有命令|OS层禁止/);
  await ui.click('runtime-settings-close'); selectRuntime(ui, 'project-workflow', 'project_folder'); await openRuntimeSettings(ui);
  assert.doesNotMatch(ui.get('runtime-settings-note').textContent, /历史只读设置/);
  assert.match(ui.get('scope-description').textContent, /暂不运行项目、安装依赖或自动合入/); assert.equal(ui.posts().length, 0);
});

test('native progress safeguards and unsupported participants display concrete reasons instead of raw error codes', async () => {
  const saved = restartSnapshot(); saved.room.mode = 'native_project'; saved.objective.stop_reason = 'native_no_progress';
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true });
  assert.match(ui.get('collaboration-outcome').textContent, /同一工具连续失败，任务文件没有变化，已暂停/);
  assert.match(ui.get('partner-activity-claude').textContent, /同一工具连续失败/);
  saved.objective.stop_reason = 'native_progress_unconfirmed'; await ui.poll();
  assert.match(ui.get('collaboration-outcome').textContent, /任务文件状态未能确认，已暂停，避免重复操作/);
  saved.room.mode = 'project_folder'; saved.current_run.can_native_handoff = false; saved.current_run.native_handoff_reason = 'native_adapter_unsupported'; saved.current_run.can_restart_objective = false; await ui.poll();
  assert.equal(ui.get('native-handoff').disabled, true); assert.match(ui.get('collaboration-outcome').textContent, /只支持 Claude Code 和 Codex.*没有自动删去成员/);
  assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /native_no_progress|native_progress_unconfirmed|native_adapter_unsupported/);
  assert.equal(ui.posts().length, 0);
});

test('a pause waiting on the current step keeps the room in progress, and a partner out of quota never claims to be pausing', async () => {
  // Catches a room still working being filed under stopped, a retired partner shown as pausing, and quota exhaustion worded as a result to verify.
  const saved = objectiveSnapshot({ status: 'paused' }); saved.objective.pause_requested = true; saved.members[1].status = 'idle';
  saved.room.objective_status = 'paused'; saved.room.objective_pause_requested = true;
  saved.room.work_activity = { state: 'working' };
  saved.objective.retired_partners = { codex: { reason: 'agent_quota_exhausted', at: 1100, cleanup_confirmed: true } };
  saved.jobs = [{ id: jobId, provider: 'claude', status: 'working', kind: 'team_work', run_id: saved.current_run.id }];
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.equal(ui.get('status-codex').textContent, '本轮暂不参与');
  assert.match(ui.get('collaboration-outcome').textContent, /额度已用完，本目标不再安排它/);
  assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /本轮结果需要核对/);
  assert.equal(ui.get('rooms').firstChild.dataset.group, 'go');
  saved.objective.pause_requested = false; saved.room.objective_pause_requested = false; saved.room.work_activity = null; saved.jobs = []; await ui.poll();
  assert.equal(ui.get('rooms').firstChild.dataset.group, 'stop');
  assert.equal(ui.posts().length, 0);
});

test('an old cache miss retires after an hour', async () => {
  // Catches a one-off miss staying on screen long after later calls read the cache normally.
  const now = Date.now() / 1000;
  const miss = (age) => ({ seq: 2, kind: 'native_cache_miss', sender: 'claude', provider: 'claude', job_id: jobId, run_id: null, phase: 'task', cache_creation_input_tokens: 704077, seconds_since_previous_call: 35, created_at: now - age });
  const fresh = snapshot({ events: [] }); fresh.latest_native_cache_miss = miss(3500);
  assert.equal((await browser({ rooms: [fresh.room], saved: fresh })).get('cache-miss-note').hidden, false);
  const old = snapshot({ events: [] }); old.latest_native_cache_miss = miss(3700);
  assert.equal((await browser({ rooms: [old.room], saved: old })).get('cache-miss-note').hidden, true);
});

test('the sidebar distinguishes a cleaned-up quota retirement from a decision or uncertain state while the objective continues', async () => {
  const saved = objectiveSnapshot({ status: 'running' }); saved.members[1].status = 'needs_user';
  saved.objective.retired_partners = { codex: { reason: 'agent_quota_exhausted', at: 1100, cleanup_confirmed: true } };
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.equal(ui.get('status-codex').textContent, '本轮暂不参与'); assert.match(ui.get('task-codex').textContent, /此前额度受限.*本轮安排保持/);
  saved.objective.retired_partners.codex.cleanup_confirmed = false; await ui.poll(); assert.equal(ui.get('status-codex').textContent, '等你处理');
  saved.objective.retired_partners.codex.cleanup_confirmed = true; saved.members[1].identity_uncertain = true; await ui.poll();
  assert.equal(ui.get('status-codex').textContent, '结果待核对');
  saved.members[1].identity_uncertain = false; saved.members[1].status = 'unknown'; await ui.poll(); assert.equal(ui.get('status-codex').textContent, '状态待确认');
  saved.members[1].status = 'needs_user'; saved.objective.status = 'needs_user'; saved.room.status = 'needs_user'; await ui.poll();
  assert.equal(ui.get('status-codex').textContent, '等你处理');
  saved.objective.status = 'running'; saved.room.status = 'running'; saved.current_run.status = 'stopped'; await ui.poll();
  assert.notEqual(ui.get('status-codex').textContent, '本轮暂不参与');
  saved.current_run.status = 'in_progress'; saved.objective.retired_partners = {}; await ui.poll(); assert.equal(ui.get('status-codex').textContent, '等你处理');
  assert.equal(ui.posts().length, 0);
});

function nativeRecoverySnapshot() {
  const saved = objectiveSnapshot({ status: 'interrupted' }); saved.room.mode = 'native_project'; saved.room.status = 'needs_user';
  saved.current_run.status = 'unknown'; saved.current_run.can_recover_native = false; saved.current_run.native_recovery_reason = 'team_cleanup_unconfirmed';
  saved.current_run.can_retry = true;
  saved.team = { run_id: saved.current_run.id, phase: 'unknown', tasks: [{ id: 'existing-task', title: '继续已认领的实现', owner: 'claude', status: 'unknown' }] };
  saved.collaboration = teamCollaboration({ state: 'unknown', tasks: saved.team.tasks });
  return saved;
}

test('native onsite recovery uses only the authoritative gate and exact file binding, preserving the board and settings in a new session', async () => {
  const saved = nativeRecoverySnapshot();
  const resumed = structuredClone(saved); resumed.room.status = 'running'; resumed.current_run.status = 'in_progress'; resumed.current_run.can_recover_native = false;
  resumed.current_run.id = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
  resumed.team.phase = 'working'; resumed.objective.status = 'running'; resumed.collaboration = teamCollaboration({ state: 'in_progress', tasks: resumed.team.tasks });
  resumed.members[0].session_id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.endsWith('/recover-native')) return resumed;
  } });
  assert.equal(ui.get('recover-native').disabled, true); assert.match(ui.get('collaboration-outcome').textContent, /执行或清理尚未确认/);
  assert.equal(ui.document.getElementById('retry-run'), null, 'an unknown native scene never offers the broad legacy retry');
  await ui.click('recover-native'); assert.equal(ui.posts().length, 0);
  saved.current_run.native_recovery_reason = 'native_recovery_diagnostic_unconfirmed'; await ui.poll();
  assert.match(ui.get('collaboration-outcome').textContent, /现场记录暂时对不上，已保留文件和记录；核对后再继续/);
  assert.equal(ui.get('recover-native').disabled, true); await ui.click('recover-native'); assert.equal(ui.posts().length, 0);
  saved.current_run.can_recover_native = true; saved.current_run.native_recovery_reason = null; await ui.poll();
  assert.equal(ui.get('recover-native').disabled, false); assert.match(ui.get('collaboration-outcome').textContent, /沿用原目标、分工和权限/);
  ui.type('尚未发送的补充仍保留'); await ui.click('recover-native');
  assert.deepEqual(ui.posts()[0].body, { request_id: ui.posts()[0].body.request_id, run_id: saved.current_run.id, revision: saved.current_run.revision, digest: saved.current_run.digest });
  assert.equal(ui.posts()[0].path, `/api/rooms/${roomId}/recover-native`); assert.equal(ui.get('pending-request').hidden, true);
  assert.match(ui.get('session-claude').textContent, /aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/);
  assert.equal(ui.get('message').value, '尚未发送的补充仍保留'); assert.deepEqual(resumed.team.tasks, saved.team.tasks);
  assert.deepEqual(resumed.objective.budget, saved.objective.budget); assert.deepEqual(resumed.current_run.execution_policy, saved.current_run.execution_policy);
});

test('a stopped unchanged-reason failure offers native recovery only when the host gate permits its exact bound request', async () => {
  const saved = nativeRecoverySnapshot(); const code = 'native_unchanged_reason_required';
  saved.current_run.status = 'stopped'; saved.objective.status = 'stopped'; saved.objective.stop_reason = code;
  saved.objective.next_step = null; saved.team.phase = 'stopped'; saved.team.stop_reason = code;
  saved.team.tasks[0].status = 'blocked'; saved.collaboration = teamCollaboration({ state: 'stopped', tasks: saved.team.tasks });
  saved.room.runtime_settings = { permission: 'full_access', members: { claude: { model: null, effort: null }, codex: { model: null, effort: null } } };
  saved.current_run.runtime_settings = structuredClone(saved.room.runtime_settings);
  const resumed = structuredClone(saved); resumed.room.status = 'running'; resumed.current_run.status = 'in_progress';
  resumed.current_run.id = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'; resumed.objective.status = 'running';
  resumed.team.phase = 'working'; resumed.collaboration = teamCollaboration({ state: 'in_progress', tasks: resumed.team.tasks });
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.endsWith('/recover-native')) return resumed;
  } });
  assert.equal(ui.document.getElementById('recover-native'), null); assert.equal(ui.posts().length, 0);
  saved.current_run.can_recover_native = true; saved.current_run.native_recovery_reason = null; await ui.poll();
  const previousButton = ui.get('recover-native'); assert.equal(previousButton.disabled, false);
  saved.current_run.can_recover_native = false; saved.current_run.native_recovery_reason = 'native_recovery_unpublished_changes'; await ui.poll();
  assert.equal(ui.document.getElementById('recover-native'), null);
  previousButton.fire('click'); await settle(); assert.equal(ui.posts().length, 0, 'a stale visible button cannot bypass the host gate');
  saved.current_run.can_recover_native = true; saved.current_run.native_recovery_reason = null; await ui.poll();
  ui.type('保留这段尚未发送的补充'); await ui.click('recover-native');
  assert.equal(ui.posts().length, 1); assert.equal(ui.posts()[0].path, `/api/rooms/${roomId}/recover-native`);
  assert.deepEqual(ui.posts()[0].body, { request_id: ui.posts()[0].body.request_id,
    run_id: saved.current_run.id, revision: saved.current_run.revision, digest: saved.current_run.digest });
  assert.equal(ui.get('message').value, '保留这段尚未发送的补充'); assert.equal(ui.get('pending-request').hidden, true);
  assert.deepEqual(resumed.current_run.execution_policy, saved.current_run.execution_policy);
  assert.deepEqual(resumed.room.runtime_settings, saved.room.runtime_settings);
});

test('partial-work native recovery explains read-only inspection without bypassing the host gate or presenting private changes as delivery', async () => {
  const saved = nativeRecoverySnapshot(); saved.current_run.native_recovery_kind = 'partial_work';
  saved.current_run.native_recovery_reason = 'agent_quota_exhausted';
  saved.objective.delivery = { revision: saved.current_run.revision, digest: saved.current_run.digest,
    total: 0, files: [], missing_or_changed: [], verified: true };
  const resumed = structuredClone(saved); resumed.room.status = 'running'; resumed.current_run.status = 'in_progress';
  resumed.current_run.id = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'; resumed.current_run.can_recover_native = false;
  resumed.objective.status = 'running'; resumed.team.phase = 'working'; resumed.team.tasks[0].status = 'assigned';
  resumed.collaboration = teamCollaboration({ state: 'in_progress', tasks: resumed.team.tasks, reason: '正在只读核对保留的改动。' });
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.endsWith('/recover-native')) return resumed;
  } });
  assert.equal(ui.get('recover-native').disabled, true); assert.match(ui.get('collaboration-outcome').textContent, /额度已用完/);
  await ui.click('recover-native'); assert.equal(ui.posts().length, 0, 'partial metadata cannot clear quota or override a disabled host gate');
  saved.current_run.can_recover_native = true; saved.current_run.native_recovery_reason = null;
  saved.current_run.native_recovery_kind = 'future_unknown_kind'; await ui.poll();
  assert.match(ui.get('collaboration-outcome').textContent, /沿用原目标、分工和权限/);
  assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /伙伴会先核对保留的改动/);
  saved.current_run.native_recovery_kind = 'partial_work'; await ui.poll();
  assert.equal(ui.get('recover-native').disabled, false);
  assert.match(ui.get('collaboration-outcome').textContent, /伙伴会先核对保留的改动，再接着做/);
  assert.match(ui.get('collaboration-outcome').textContent, /不重复执行上次调用/);
  assert.equal(deliverySections(ui).current, undefined);
  ui.type('这段尚未发送的补充保留'); await ui.click('recover-native');
  assert.equal(ui.posts().length, 1); assert.equal(ui.posts()[0].path, `/api/rooms/${roomId}/recover-native`);
  assert.deepEqual(ui.posts()[0].body, { request_id: ui.posts()[0].body.request_id,
    run_id: saved.current_run.id, revision: saved.current_run.revision, digest: saved.current_run.digest });
  assert.equal(ui.get('message').value, '这段尚未发送的补充保留'); assert.equal(ui.get('pending-request').hidden, true);
  assert.equal(deliverySections(ui).current, undefined, 'recovery inspection has not published a delivery');
  assert.deepEqual(resumed.objective.delivery.files, []);
  assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /独立审核通过|目标已完成|修复完成/);
});

test('uncertain native onsite recovery survives reload without automatic replay and retries only its original request on a capable service', async () => {
  const saved = nativeRecoverySnapshot(); saved.current_run.can_recover_native = true; const storage = new Map();
  const ui = await browser({ rooms: [saved.room], saved, storage, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.endsWith('/recover-native')) throw new Error('fictional lost recovery result');
  } });
  await ui.click('recover-native'); const original = structuredClone(ui.posts()[0]); await ui.poll(); assert.equal(ui.posts().length, 1);
  const old = await browser({ rooms: [saved.room], saved, storage, executionPolicySupport: true, continuousObjectiveSupport: false });
  assert.equal(old.get('pending-request').hidden, false); assert.equal(old.document.getElementById('recover-native'), null);
  await old.click('retry-request'); assert.equal(old.posts().length, 0); assert.deepEqual(savedOutbox(storage).request.body, original.body);
  const reloaded = await browser({ rooms: [saved.room], saved, storage, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.endsWith('/recover-native')) return { ...saved, current_run: { ...saved.current_run, id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' } };
  } });
  assert.equal(reloaded.posts().length, 0); await reloaded.click('retry-request');
  assert.equal(reloaded.posts()[0].path, original.path); assert.deepEqual(reloaded.posts()[0].body, original.body);
  assert.equal(reloaded.get('pending-request').hidden, true);
});

test('goal reassessment lives in task management and sends the exact original binding without replacing a draft or goal settings', async () => {
  const saved = objectiveSnapshot({ status: 'complete', decision: 'complete' }); saved.current_run.can_reassess_objective = true;
  saved.room.mode = 'native_project';
  saved.current_run.runtime_settings = { permission: 'workspace_write', members: { claude: { model: 'sonnet', effort: 'low' }, codex: { model: null, effort: null } } };
  const next = structuredClone(saved); next.current_run.id = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
  next.current_run.can_reassess_objective = false; next.objective.status = 'checkpoint'; next.room.status = 'running';
  let respond;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.endsWith('/reassess-objective')) return new Promise((resolve) => { respond = resolve; });
  } });
  const button = ui.get('reassess-objective'); assert.equal(button.type, 'button');
  assert.ok(descendants(ui.get('room-lifecycle-menu')).includes(button)); assert.equal(descendants(ui.get('collaboration-outcome')).includes(button), false);
  ui.type('之后还想补充的草稿'); await ui.click('reassess-objective');
  assert.equal(ui.posts().length, 1); assert.equal(ui.document.getElementById('reassess-objective'), null, 'a busy mutation removes the action');
  assert.deepEqual(ui.posts()[0].body, { request_id: ui.posts()[0].body.request_id, run_id: saved.current_run.id, revision: saved.current_run.revision, digest: saved.current_run.digest });
  assert.equal(ui.posts()[0].path, `/api/rooms/${roomId}/reassess-objective`); await ui.poll(); assert.equal(ui.posts().length, 1);
  respond(next); await waitUntil(() => ui.get('pending-request').hidden, 'the same-objective readonly assessment is confirmed');
  assert.equal(ui.get('message').value, '之后还想补充的草稿'); assert.equal(ui.document.getElementById('reassess-objective'), null);
  assert.deepEqual(next.objective.budget, saved.objective.budget); assert.equal(next.objective.original_goal, saved.objective.original_goal);
  assert.deepEqual(next.current_run.execution_policy, saved.current_run.execution_policy); assert.deepEqual(next.current_run.runtime_settings, saved.current_run.runtime_settings);
  assert.equal(saved.current_run.accepted_at, null);
  saved.current_run.can_reassess_objective = false;
  const unavailable = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.equal(unavailable.document.getElementById('reassess-objective'), null); assert.equal(unavailable.posts().length, 0);
  saved.current_run.can_reassess_objective = true; saved.jobs = [{ id: jobId, provider: 'claude', status: 'working' }]; await unavailable.poll();
  assert.equal(unavailable.document.getElementById('reassess-objective'), null, 'a working task cannot be reassessed even with a stale flag');
  saved.jobs = []; const old = await browser({ rooms: [saved.room], saved });
  assert.equal(old.document.getElementById('reassess-objective'), null, 'an old service cannot enable the action from snapshot fields alone');
});

test('an unchanged verified goal file is lazy-readable by its exact source version after an empty-board assessment and rejects a wrong version', async () => {
  const saved = objectiveDeliveryFixture(); saved.room.mode = 'native_project';
  const content = '# 保留的说明\n这份文件经过审核，但没有改写源文件。'; const sha256 = createHash('sha256').update(content).digest('hex');
  const binding = { run_id: '33333333-3333-4333-8333-333333333333', revision: 7, digest: 'e'.repeat(64), path: 'notes.md', sha256 };
  saved.objective.delivery.files.push({ ...binding, task_id: 'notes', review_kind: 'independent_review', verified: true }); saved.objective.delivery.total = 3;
  assert.equal(saved.team.tasks.length, 0); assert.equal(saved.project.files.some((file) => file.path === 'notes.md'), false);
  let wrongVersion = false;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.includes('/file?')) {
      const url = new URL(request.path, 'http://fictional.invalid'); assert.equal(url.pathname, `/api/rooms/${roomId}/file`);
      assert.deepEqual(Object.fromEntries(url.searchParams), { ...binding, revision: String(binding.revision) });
      return { ...binding, revision: wrongVersion ? 8 : binding.revision, content };
    }
  } });
  const current = ui.get('document-shortcuts');
  assert.match(current.textContent, /notes.md.*伙伴看过/); assert.doesNotMatch(current.textContent, /inventory.md/);
  assert.equal(ui.requests.filter((request) => request.path.includes('/file?')).length, 0, 'metadata is not fetched until the user opens it');
  const open = () => descendants(ui.get('room-documents')).find((node) => node.dataset.documentPath === 'notes.md').fire('click');
  open(); await waitUntil(() => ui.get('reader-body').textContent.includes('没有改写源文件'), 'exact notes body passes SHA verification');
  assert.equal(ui.get('reader-title').textContent, '保留的说明'); assert.match(ui.get('reader-meta').textContent, /本目标成果.*版本 7/);
  await ui.click('reader-back'); wrongVersion = true; open();
  await waitUntil(() => ui.get('reader-body').textContent.includes('版本无法确认'), 'a wrong source version is rejected');
  assert.doesNotMatch(ui.get('reader-body').textContent, /没有改写源文件/); assert.equal(ui.posts().length, 0);
  await ui.click('reader-back'); saved.objective.delivery.digest = 'f'.repeat(64); await ui.poll();
  assert.equal(descendants(ui.get('room-documents')).some((node) => node.dataset.documentPath === 'notes.md'), false,
    'unmatched head proof cannot promote metadata into a readable current delivery');
  assert.equal(ui.requests.filter((request) => request.path.includes('/file?')).length, 2);
});


test('saying continue resumes the same native task through the host gate without requiring acceptance or another chat', async () => {
  const saved = nativeRecoverySnapshot(); saved.current_run.can_recover_native = true;
  saved.current_run.native_recovery_kind = 'partial_work'; saved.current_run.native_recovery_provider = 'codex';
  saved.current_run.native_recovery_source_provider = 'claude';
  const resumed = structuredClone(saved); resumed.current_run.id = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
  resumed.current_run.can_recover_native = false; resumed.current_run.status = 'in_progress';
  resumed.objective.status = 'running'; resumed.room.status = 'running'; resumed.team.phase = 'working';
  resumed.collaboration = teamCollaboration({ state: 'in_progress', tasks: resumed.team.tasks });
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.endsWith('/recover-native')) return resumed;
  } });
  assert.equal(ui.get('recover-native').textContent, '继续');
  assert.equal(ui.get('composer').hidden, false); assert.equal(ui.get('message').disabled, false);
  ui.type('继续吧！'); await ui.submit();
  assert.equal(ui.posts().length, 1); assert.equal(ui.posts()[0].path, `/api/rooms/${roomId}/recover-native`);
  assert.deepEqual(Object.keys(ui.posts()[0].body).sort(), ['digest', 'request_id', 'revision', 'run_id']);
  assert.equal(ui.get('message').value, ''); assert.equal(resumed.room.id, saved.room.id);
});

test('continuation without an atomic supplement contract keeps extra instructions as a draft rather than replaying', async () => {
  const saved = restartSnapshot();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  ui.type('继续，同时删除旧记录'); await ui.submit();
  assert.equal(ui.posts().length, 0); assert.equal(ui.get('message').value, '继续，同时删除旧记录');
  saved.current_run.can_restart_objective = false; saved.current_run.restart_objective_reason = 'team_cleanup_unconfirmed'; await ui.poll();
  ui.type('继续'); await ui.submit(); assert.equal(ui.posts().length, 0);
  assert.equal(ui.get('message').value, '继续');
});


test('new tasks have no artificial limit controls and use continuous work without changing permissions', async () => {
  const saved = objectiveSnapshot({ status: 'running' });
  const ui = await browser({ agents: catalog.slice(0, 2), executionPolicySupport: true, route(request) {
    if (request.path === '/api/runtime-options') return runtimeOptionsFixture();
    if (request.path === '/api/rooms' && request.method === 'POST') return saved;
  } });
  for (const id of ['execution-policy', 'execution-policy-popover', 'execution-policy-mode', 'execution-policy-seconds', 'execution-policy-calls', 'execution-policy-night']) assert.equal(ui.document.getElementById(id), null);
  await openRuntimeSettings(ui); selectRuntime(ui, 'runtime-permission', 'read_only'); await ui.click('runtime-settings-close');
  ui.type('共同完成这个目标'); await ui.submit();
  assert.deepEqual(ui.posts()[0].body.execution_policy, { mode: 'autonomous', max_seconds: null, max_calls: null, quota_failover: true });
  assert.equal(ui.posts()[0].body.runtime_settings.permission, 'read_only');
});

test('historical finite budgets show actual activity without presenting their old caps or deadlines as current controls', async () => {
  const saved = objectiveSnapshot({ status: 'running' });
  saved.conversation_budget = { used: 90, run_count: 13, run_limit: 8, max_calls: 256 };
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.equal(ui.get('budget-used').textContent, '13 次');
  assert.equal(ui.document.getElementById('budget-deadline'), null);
  assert.equal(ui.get('conversation-budget').textContent, '历史累计：13 轮 · 90 次调用');
  assert.doesNotMatch(ui.get('objective-budget').textContent, /上限|剩余|截止/);
});


test('a stopped task shows one continuation action even when both old upgrade and restart capabilities are projected', async () => {
  const saved = restartSnapshot(); saved.room.mode = 'project_folder'; saved.current_run.can_native_handoff = false;
  saved.current_run.native_handoff_reason = 'native_handoff_unavailable';
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true });
  assert.equal(ui.document.getElementById('native-handoff'), null); assert.equal(ui.get('restart-objective').textContent, '继续');
  assert.equal(ui.document.getElementById('restart-policy'), null);
  saved.current_run.can_native_handoff = true; await ui.poll();
  assert.equal(ui.document.getElementById('restart-objective'), null); assert.equal(ui.get('native-handoff').textContent, '继续');
  saved.current_run.can_native_handoff = false; saved.current_run.can_restart_objective = false; await ui.poll();
  assert.equal(ui.document.getElementById('restart-objective'), null); assert.equal(ui.get('native-handoff').disabled, true);
  assert.equal(ui.posts().length, 0);
});


test('native continuation carries an arbitrary supplement atomically with the frozen recovery binding', async () => {
  const saved = nativeRecoverySnapshot(); saved.current_run.can_recover_native = true;
  const resumed = structuredClone(saved); resumed.current_run.id = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
  resumed.current_run.can_recover_native = false; resumed.current_run.status = 'in_progress';
  resumed.objective.status = 'running'; resumed.room.status = 'running'; resumed.team.phase = 'working';
  resumed.collaboration = teamCollaboration({ state: 'in_progress', tasks: resumed.team.tasks });
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.endsWith('/recover-native')) return resumed;
  } });
  ui.type('继续，先补齐期限字段，再给我看真实验证结果。'); await ui.submit();
  assert.equal(ui.posts().length, 1); assert.equal(ui.posts()[0].path, `/api/rooms/${roomId}/recover-native`);
  assert.deepEqual(ui.posts()[0].body, { request_id: ui.posts()[0].body.request_id, run_id: saved.current_run.id,
    revision: saved.current_run.revision, digest: saved.current_run.digest, user_message: '继续，先补齐期限字段，再给我看真实验证结果。' });
  assert.equal(ui.get('message').value, '');
});

test('an uncertain native supplement keeps the exact request and draft across reload without a second message race', async () => {
  const saved = nativeRecoverySnapshot(); saved.current_run.can_recover_native = true; const storage = new Map();
  const ui = await browser({ rooms: [saved.room], saved, storage, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.endsWith('/recover-native')) throw new Error('fictional lost recovery response');
  } });
  ui.type('先完成现有清单，保留尚未验证的结论。'); await ui.submit(); const original = structuredClone(ui.posts()[0]);
  assert.equal(ui.get('message').value, original.body.user_message);
  const reloaded = await browser({ rooms: [saved.room], saved, storage, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.endsWith('/recover-native')) return { ...saved, current_run: { ...saved.current_run, id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' } };
  } });
  assert.equal(reloaded.posts().length, 0); assert.equal(reloaded.get('message').value, original.body.user_message);
  await reloaded.click('retry-request'); assert.deepEqual(structuredClone(reloaded.posts()[0]), original); assert.equal(reloaded.posts().length, 1);
  assert.equal(reloaded.get('message').value, '');
});

test('native recovery takes precedence over obsolete objective and stage continuation projections', async () => {
  const saved = nativeRecoverySnapshot(); saved.current_run.can_start_objective = true;
  delete saved.objective; delete saved.current_run.objective_id;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true });
  assert.equal(ui.document.getElementById('start-objective'), null); assert.equal(ui.get('recover-native').disabled, true);
  saved.current_run.can_recover_native = true; await ui.poll();
  assert.equal(ui.document.getElementById('start-objective'), null); assert.equal(ui.get('recover-native').disabled, false);
  const objective = objectiveSnapshot(); saved.objective = objective.objective; saved.current_run.objective_id = objective.objective.id;
  saved.objective.next_step.binding = { run_id: saved.current_run.id, revision: saved.current_run.revision, digest: saved.current_run.digest };
  await ui.poll();
  assert.equal(ui.document.getElementById('continue-objective'), null); assert.equal(ui.get('recover-native').textContent, '继续');
  assert.equal(ui.posts().length, 0);
});


test('legacy no-progress protection explains the cause and continuation without exposing a raw error code', async () => {
  const saved = restartSnapshot(); saved.objective.stop_reason = 'no_progress';
  const expected = '重复处理同一结果，没有新的进展。补充要求后可以继续。';
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, route(request) {
    if (request.path.endsWith('/restart-objective')) return { error: 'no_progress' };
  } });
  assert.ok(ui.get('collaboration-outcome').textContent.includes(expected));
  assert.ok(ui.get('partner-activity-claude').textContent.includes(expected));
  assert.equal(ui.get('restart-objective').disabled, false); assert.equal(ui.posts().length, 0);
  await ui.click('restart-objective'); assert.equal(ui.posts().length, 1);
  assert.equal(ui.get('action-error').textContent, expected);
  assert.doesNotMatch(ui.get('action-error').textContent, /no_progress/);
});


test('the stopped native composer uses send wording so the primary continue action remains unique', async () => {
  const saved = nativeRecoverySnapshot(); saved.current_run.can_recover_native = true;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true });
  assert.equal(ui.get('recover-native').textContent, '继续');
  assert.equal(ui.get('send-label').textContent, '发送'); assert.equal(ui.get('send').disabled, true);
  ui.type('先补齐验证，再给我看结果。');
  assert.equal(ui.get('send-label').textContent, '发送并继续'); assert.equal(ui.get('send').disabled, false);
  ui.type(''); assert.equal(ui.get('send-label').textContent, '发送'); assert.equal(ui.get('send').disabled, true);
  assert.equal(ui.posts().length, 0);
});


test('workspace manager traps focus outside the inert chat and restores the trigger without losing its draft', async () => {
  const ui = await browser(); const trigger = ui.get('open-projects'); trigger.focus(); ui.type('保留这段聊天草稿');
  await ui.click('open-projects'); const manager = ui.get('workspace-manager'); const shell = ui.document.querySelector('.app-shell');
  assert.equal(shell.inert, true); assert.equal(descendants(shell).includes(manager), false);
  assert.equal(ui.document.activeElement, ui.get('close-manager'));
  ui.document.root.fire('keydown', { key: 'Tab', shiftKey: true });
  assert.ok(descendants(manager).includes(ui.document.activeElement)); assert.notEqual(ui.document.activeElement, ui.get('close-manager'));
  ui.document.root.fire('keydown', { key: 'Tab' }); assert.equal(ui.document.activeElement, ui.get('close-manager'));
  await ui.click('manager-partners-tab'); ui.document.root.fire('keydown', { key: 'Escape' });
  assert.equal(manager.hidden, true); assert.equal(shell.inert, false); assert.equal(ui.document.activeElement, trigger);
  assert.equal(ui.get('message').value, '保留这段聊天草稿'); assert.equal(ui.posts().length, 0);
});

test('generic returned-tool descriptions do not replace the actual task or duplicate status in activity cards', async () => {
  const saved = activeActivitySnapshot();
  saved.events.push({ seq: 4, kind: 'native_tool_activity', run_id: saved.current_run.id, job_id: jobId,
    sender: 'claude', state: 'completed', tool_kind: 'Bash', description: '工具已返回', created_at: 130 });
  const ui = await browser({ rooms: [saved.room], saved }); const card = ui.get('partner-activity-claude');
  assert.match(card.firstChild.textContent, /正在处理/); assert.doesNotMatch(card.firstChild.textContent, /工具已返回|实现计分规则|最近完成/);
  assert.match(card.children[1].textContent, /处理中 · 最近完成：执行命令.*实现计分规则/);
  assert.equal(card.children[1].children.filter((node) => /实现计分规则/.test(node.textContent)).length, 1);
  card.open = true; await ui.poll(); assert.equal(card.open, true);
  assert.equal(ui.get('message').placeholder, '补充要求，或告诉伙伴要调整什么');
  const css = fs.readFileSync(path.join(assetRoot, 'style.css'), 'utf8');
  assert.match(css, /partner-activity-cards:has\(> \.partner-activity-card\[open\]\).*max-height: min\(38dvh, 320px\)/);
  assert.match(css, /\.usage-quota-text, \.usage-query-action \{ font-size: 12px/);
  assert.equal(ui.posts().length, 0);
});

test('availability surfaces deduplicate the same observation and keep current metadata while management task titles stay short', async () => {
  const partners = availabilityPartners(); const saved = activeActivitySnapshot();
  partners.forEach((partner) => { partner.availability = availabilityFixture({ state: 'available', reason: null }); });
  const longGoal = '继续推进虚构项目现有任务，' + '需要核对已有实现和实际结果。'.repeat(40);
  partners[0].monitor = { status: 'working', current_tasks: [{ room_id: roomId, room_goal: longGoal, status: 'working', current_task: '核对已有实现' }] };
  const ui = await browser({ partners, rooms: [saved.room], saved, executionPolicySupport: true });
  assert.equal(ui.get('availability-claude').hidden, true, 'an unblocked partner card carries no status box');
  assert.equal((ui.get('provider-account-claude').textContent.match(/额度够用/g) || []).length, 1);
  assert.match(ui.get('provider-account-claude').textContent, /上次检查/);
  await ui.click('open-partners');
  const card = ui.get('partners-list').children.find((node) => node.dataset.partner === 'claude');
  const note = card.children.find((node) => node.classList.contains('availability-note'));
  assert.equal((note.textContent.match(/额度够用/g) || []).length, 1);
  const link = descendants(card).find((node) => node.classList.contains('task-room-link'));
  assert.ok(link.textContent.length < 100); assert.equal(link.title, longGoal);
  assert.ok(ui.posts().every((request) => request.path === '/api/availability/refresh' || request.path.endsWith('/account/refresh')), 'opening metadata may refresh accounts but never starts a task');
});

test('native-capable project registration distinguishes execution workflow from permissions without a blanket no-run promise', async () => {
  const project = nativeProjectFixture();
  const ui = await browser({ projects: [project], nativeProjectSupport: true });
  await ui.click('open-projects');
  assert.match(ui.get('projects-list').textContent, /可选本机开发或文本协作.*按所选权限执行/);
  assert.doesNotMatch(ui.get('projects-list').textContent, /不会改动原项目.*暂不运行/);
  assert.match(html, /开发并运行（本机工具）/); assert.match(html, /只改文件（不运行）/);
  assert.match(html, /在输入框下方的“权限”/); assert.equal(ui.posts().length, 0);
});


function withNaturalContinuation(saved, canSend = true, reason = null) {
  if (!saved.team) saved.team = { run_id: saved.current_run.id, phase: 'stopped', tasks: [] };
  saved.continuation = { can_send: canSend, reason, binding: { run_id: saved.current_run.id, revision: saved.current_run.revision, digest: saved.current_run.digest } };
  return saved;
}

test('completed and paused tasks accept a bound natural follow-up in the same chat without selecting a new task', async () => {
  for (const status of ['complete', 'paused', 'stopped']) {
    const saved = withNaturalContinuation(status === 'stopped' ? restartSnapshot() : objectiveSnapshot({ status, decision: 'complete' }));
    saved.team = { run_id: saved.current_run.id, phase: status === 'stopped' ? 'stopped' : 'ready_for_user', tasks: [] };
    const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, route(request) {
      if (request.path.endsWith('/messages')) return saved;
    } });
    assert.equal(ui.get('composer').hidden, false); assert.equal(ui.get('message').disabled, false);
    assert.equal(ui.get('send-label').textContent, '发送');
    ui.type('这里再改一下，把说明写清楚。'); await ui.submit();
    assert.equal(ui.posts().length, 1); const request = ui.posts()[0];
    assert.equal(request.path, `/api/rooms/${roomId}/messages`);
    assert.deepEqual(request.body, { text: '这里再改一下，把说明写清楚。', target: 'all', request_id: request.body.request_id,
      intent: 'continue', binding: saved.continuation.binding });
    assert.equal(ui.get('message').value, '');
    assert.equal(ui.posts().some((post) => /recover-native|restart-objective|accept|fork/.test(post.path)), false);
  }
});

test('natural continuation preserves blocked drafts and follows the host gate instead of inferring permission from old run flags', async () => {
  const saved = withNaturalContinuation(restartSnapshot(), false, 'agent_quota_exhausted');
  saved.current_run.can_recover_native = true; saved.room.mode = 'native_project';
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.endsWith('/messages')) return saved;
  } });
  assert.equal(ui.get('composer').hidden, false); assert.equal(ui.get('message').disabled, false);
  ui.type('额度恢复后接着做。'); assert.equal(ui.get('send').disabled, true); await ui.submit();
  assert.equal(ui.posts().length, 0); assert.equal(ui.document.getElementById('recover-native'), null);
  saved.continuation.can_send = true; saved.continuation.reason = null; await ui.poll();
  assert.equal(ui.get('send').disabled, false); assert.equal(ui.get('message').value, '额度恢复后接着做。');
  saved.continuation.binding.digest = 'a'.repeat(64); await ui.poll(); await ui.submit(); assert.equal(ui.posts().length, 0);
  saved.continuation.binding.digest = saved.current_run.digest; await ui.poll(); await ui.submit(); assert.equal(ui.posts().length, 1);
});

test('a failed recovery request reports that attempt without denying a fresh host continuation capability', async () => {
  const saved = withNaturalContinuation(restartSnapshot()); saved.room.mode = 'native_project';
  saved.current_run.can_recover_native = true;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.endsWith('/messages')) return { error: 'native_recovery_unavailable' };
  } });
  await ui.click('continue-current');
  assert.equal(ui.get('action-error').textContent, '这次恢复请求未完成，已有记录与成果保留。');
  assert.equal(ui.get('action-error').hidden, false); assert.equal(ui.posts().length, 1);
  await ui.poll();
  assert.equal(ui.get('continue-current').disabled, false);
  assert.equal(ui.get('action-error').hidden, false, 'the failed attempt is not silently called successful');
  assert.equal(ui.posts().length, 1, 'reading a new capability never retries work');
});
test('uncertain natural continuation retains the exact bound request on reload and cannot be replayed through an older capability', async () => {
  const saved = withNaturalContinuation(restartSnapshot()); const storage = new Map();
  const ui = await browser({ rooms: [saved.room], saved, storage, executionPolicySupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.endsWith('/messages')) throw new Error('fictional lost continuation response');
  } });
  ui.type('继续补齐现有工作，不重新执行已确认的部分。'); await ui.submit(); const original = structuredClone(ui.posts()[0]);
  assert.equal(ui.get('message').value, original.body.text);
  const old = await browser({ rooms: [saved.room], saved, storage, executionPolicySupport: true });
  assert.equal(old.get('message').value, original.body.text); await old.click('retry-request'); assert.equal(old.posts().length, 0);
  assert.equal(old.get('pending-request').hidden, false);
  const reloaded = await browser({ rooms: [saved.room], saved, storage, executionPolicySupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.endsWith('/messages')) return saved;
  } });
  assert.equal(reloaded.posts().length, 0); await reloaded.click('retry-request');
  assert.deepEqual(structuredClone(reloaded.posts()[0]), original); assert.equal(reloaded.get('message').value, '');
});


test('manager close restores the explicit mouse or accessibility trigger even when clicking did not focus it', async () => {
  const ui = await browser(); ui.get('message').focus(); await ui.click('open-projects');
  await ui.click('close-manager'); assert.equal(ui.document.activeElement, ui.get('open-projects'));
  ui.get('message').focus(); await ui.click('open-partners'); ui.document.root.fire('keydown', { key: 'Escape' });
  assert.equal(ui.document.activeElement, ui.get('open-partners')); assert.equal(ui.posts().length, 0);
});

test('activity keeps the actual task and one recent operation as a compact status without inferring tests or model thoughts', async () => {
  const saved = activeActivitySnapshot();
  for (let index = 0; index < 7; index++) saved.events.push({ seq: 100 + index, kind: 'native_tool_activity', sender: 'claude',
    job_id: jobId, run_id: saved.current_run.id, state: index === 6 ? 'completed' : 'started', operation: 'run_command',
    tool_kind: 'command_execution', description: '工具已返回', created_at: 200 + index });
  const ui = await browser({ rooms: [saved.room], saved, now: () => 310000 });
  const card = ui.get('partner-activity-claude');
  assert.match(card.firstChild.textContent, /正在处理/); assert.doesNotMatch(card.firstChild.textContent, /实现计分规则|最近完成|最近更新/);
  assert.match(card.children[1].textContent, /实现计分规则.*最近更新 1分钟前，尚无新操作记录/);
  assert.equal(descendants(card).some((node) => node.classList.contains('activity-recent-actions')), false, 'tool history is not a substitute for collaboration chat');
  assert.doesNotMatch(card.textContent, /测试通过|正在测试|模型思考中|等待模型响应/);
  assert.equal(ui.posts().length, 0);
});


function inlineHandoffFixture(sourceRoomId) {
  const saved = objectiveSnapshot({ status: 'running' }); saved.room.mode = 'native_project';
  saved.team = { run_id: saved.current_run.id, phase: 'working', tasks: [] };
  saved.native_handoff = { source_room_id: sourceRoomId, source_run_id: '66666666-6666-4666-8666-666666666666', revision: 2, digest: 'a'.repeat(64) };
  return saved;
}

test('earlier handoff discussion loads real messages across source runs inline without mixing sequence numbers or authors', async () => {
  const sourceRoomId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'; const saved = inlineHandoffFixture(sourceRoomId);
  saved.events = [{ seq: 1, kind: 'message', sender: 'codex', recipient: 'all', text: '当前聊天里的原消息', created_at: 1300, run_id: saved.current_run.id }];
  const source = { room: { id: sourceRoomId, lifecycle_state: 'archived' }, members: [{ id: 'claude', name: '当时的 Claude' }, { id: 'codex', name: '当时的 Codex' }], events: [
    { seq: 3, kind: 'message', sender: 'claude', recipient: 'codex', text: '你帮我核对第三项。', created_at: 1200, run_id: saved.native_handoff.source_run_id },
    { seq: 1, kind: 'message', sender: 'codex', recipient: 'claude', text: '我来完成第一和第二项。', created_at: 1000, run_id: '77777777-7777-4777-8777-777777777777' },
    { seq: 2, kind: 'message', sender: 'claude', recipient: 'codex', text: '好，我做剩下的部分。', created_at: 1100, run_id: '77777777-7777-4777-8777-777777777777' },
    { seq: 4, kind: 'warning', code: 'not-a-chat', created_at: 1250 },
  ] };
  const original = structuredClone(source);
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path === `/api/rooms/${sourceRoomId}`) return source;
  } });
  assert.equal(ui.requests.some((request) => request.path === `/api/rooms/${sourceRoomId}`), false);
  const title = ui.get('room-title').textContent; const details = ui.get('earlier-discussion'); details.open = true; details.fire('toggle'); await settle();
  assert.equal(ui.get('earlier-messages').children.length, 3);
  assert.deepEqual(ui.get('earlier-messages').children.map((node) => node.dataset.sourceSeq), ['1', '2', '3']);
  assert.match(ui.get('earlier-messages').textContent, /当时的 Codex.*我来完成第一和第二项.*当时的 Claude.*好，我做剩下/);
  assert.doesNotMatch(ui.get('events').textContent, /我来完成第一和第二项/); assert.match(ui.get('events').textContent, /当前聊天里的原消息/);
  assert.equal(ui.get('room-title').textContent, title); assert.equal(details.open, true);
  const firstBubble = ui.get('earlier-messages').firstChild; await ui.poll();
  assert.equal(ui.get('earlier-messages').firstChild, firstBubble, 'polling preserves the historical reading nodes');
  assert.equal(ui.requests.filter((request) => request.path === `/api/rooms/${sourceRoomId}`).length, 1);
  assert.deepEqual(source, original); assert.equal(ui.posts().length, 0);
});

test('late or mismatched earlier-discussion responses cannot reopen a closed history or leak into another source', async () => {
  const first = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'; const second = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
  const saved = inlineHandoffFixture(first); const old = deferred(); const fresh = deferred();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path === `/api/rooms/${first}`) return old.promise;
    if (request.path === `/api/rooms/${second}`) return fresh.promise;
  } });
  const open = () => { ui.get('earlier-discussion').open = true; ui.get('earlier-discussion').fire('toggle'); };
  open(); await settle(); saved.native_handoff.source_room_id = second; await ui.poll();
  old.resolve({ room: { id: first }, members: [], events: [{ seq: 1, kind: 'message', sender: 'claude', text: '不得串入的旧来源', created_at: 1 }] }); await settle();
  assert.doesNotMatch(ui.get('earlier-messages').textContent, /不得串入/); assert.equal(ui.get('earlier-discussion').open, false);
  open(); await settle(); ui.get('earlier-discussion').open = false;
  fresh.resolve({ room: { id: second }, members: [], events: [{ seq: 1, kind: 'message', sender: 'codex', text: '新来源的真实讨论', created_at: 2 }] }); await settle();
  assert.equal(ui.get('earlier-discussion').open, false); assert.match(ui.get('earlier-messages').textContent, /新来源的真实讨论/);
  assert.equal(ui.posts().length, 0);
});


test('an earlier-discussion source mismatch stays an explicit read error and never falls back to the current room', async () => {
  const sourceRoomId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'; const saved = inlineHandoffFixture(sourceRoomId);
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path === `/api/rooms/${sourceRoomId}`) return { room: { id: roomId }, members: [], events: [{ seq: 1, kind: 'message', sender: 'claude', text: '不得显示的错误来源', created_at: 1 }] };
  } });
  ui.get('earlier-discussion').open = true; ui.get('earlier-discussion').fire('toggle'); await settle();
  assert.match(ui.get('earlier-note').textContent, /没有读到此前讨论/); assert.equal(ui.get('earlier-load').hidden, false);
  assert.equal(ui.get('earlier-messages').children.length, 0); assert.doesNotMatch(ui.get('events').textContent, /错误来源/);
  const previousReads = ui.requests.filter((request) => request.path === `/api/rooms/${sourceRoomId}`).length;
  saved.native_handoff.source_room_id = '../../unknown'; await ui.poll();
  assert.equal(ui.get('earlier-discussion').hidden, true); assert.equal(ui.get('earlier-messages').children.length, 0);
  assert.equal(ui.requests.filter((request) => request.path === `/api/rooms/${sourceRoomId}`).length, previousReads); assert.equal(ui.posts().length, 0);
});

test('normal chat keeps continuation notices short while execution details preserve the original run context', async () => {
  const saved = objectiveSnapshot({ status: 'running' });
  saved.events = [
    { seq: 1, kind: 'run_started', number: 1, created_at: 1000 },
    { seq: 2, kind: 'run_started', number: 18, retry: true, created_at: 1001 },
    { seq: 3, kind: 'warning', code: 'native_progress_unconfirmed', created_at: 1002 },
  ];
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.match(ui.get('events').textContent, /开始工作.*已继续/);
  assert.doesNotMatch(ui.get('events').textContent, /第 18 轮|专用会话/);
  assert.match(ui.get('events').textContent, /任务文件状态未能确认/);
  assert.match(ui.get('activity-events').textContent, /第 18 轮协作已开始，使用新的专用会话/);
});

test('native public commentary displays original messages with truthful truncation metadata and no synthetic reply tag', async () => {
  const saved = objectiveSnapshot({ status: 'running' });
  const original = '我来核对第一项。\n{"plan":[1,2]} 🫘';
  saved.events = [
    { seq: 1, kind: 'message', sender: 'codex', recipient: 'all', job_id: 'public-job', native_progress: true, native_item_id: 'item-a', text: original, created_at: 1000 },
    { seq: 2, kind: 'message', sender: 'codex', recipient: 'all', job_id: 'public-job', native_progress: true, native_item_id: 'item-b', text: original, created_at: 1001 },
    { seq: 3, kind: 'message', sender: 'claude', recipient: 'codex', job_id: 'other-job', native_progress: true, truncated: true, original_characters: 100, text: '好的🫘', created_at: 1002 },
    { seq: 4, kind: 'message', sender: 'claude', job_id: 'other-job', native_progress: true, truncated: false, original_characters: 100, text: '完整原文', created_at: 1003 },
    { seq: 5, kind: 'message', sender: 'claude', job_id: 'other-job', native_progress: true, truncated: true, original_characters: 2, text: '不足以证明截断', created_at: 1004 },
  ];
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  const messages = ui.get('events').children;
  assert.equal(messages.length, 5, 'same text from distinct real items stays visible');
  assert.equal(descendants(messages[0]).find((node) => node.classList.contains('event-text')).textContent, original);
  assert.match(messages[0].textContent, /Codex/); assert.doesNotMatch(messages[0].textContent, /伙伴答复|任务目标与补充|草案答复/);
  assert.match(messages[2].textContent, /仅显示前 3 字，原文 100 字/);
  assert.doesNotMatch(messages[3].textContent + messages[4].textContent, /仅显示前/);
  await ui.poll(); assert.equal(ui.get('events').children.length, 5); assert.equal(ui.posts().length, 0);
});

function lifecycleEnvelope(action, status = 'pending', extra = {}) {
  return { room_id: roomId, deleted: false, lifecycle_operation: { id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', room_id: roomId, action, status, request_id: '00000000-0000-4000-8000-000000000001', error: null }, ...extra };
}

test('capable room management archives running work in one request and queries pending cleanup without replay', async () => {
  const saved = snapshot({ status: 'running' }); saved.room.lifecycle_can_change = false; saved.room.lifecycle_blocked_reason = 'team_cleanup_unconfirmed';
  let operation; let completed = false;
  const ui = await browser({ rooms: [saved.room], saved, roomLifecycleSupport: true, route(request) {
    if (request.path.endsWith('/lifecycle')) { operation = lifecycleEnvelope('archive'); operation.lifecycle_operation.request_id = request.body.request_id; saved.room.lifecycle_state = 'archived'; return operation; }
    if (request.path.startsWith('/api/room-lifecycle-operations/')) { if (completed) operation.lifecycle_operation.status = 'completed'; return operation; }
  } });
  ui.type('这条草稿保留');
  const archive = lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'archive' });
  assert.equal(archive.disabled, false); archive.fire('click'); await settle();
  assert.equal(ui.posts().length, 1); assert.deepEqual(Object.keys(ui.posts()[0].body).sort(), ['action', 'request_id']);
  assert.equal(ui.posts()[0].body.action, 'archive'); assert.match(ui.get('room-lifecycle-status').textContent, /正在停止任务并归档/);
  assert.equal(ui.get('message').value, '这条草稿保留');
  assert.equal(lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'restore' }).disabled, true);
  await ui.poll(); assert.equal(ui.posts().length, 1); completed = true; await ui.poll();
  assert.equal(ui.get('room-lifecycle-status').hidden, true); assert.equal(ui.posts().length, 1);
  assert.deepEqual(ui.get('rooms-filter').children.map((node) => node.getAttribute('value')), ['active', 'archived']);
});

test('permanent room removal requires accurate confirmation and only disappears after confirmed deletion', async () => {
  const saved = snapshot({ status: 'running' }); const rooms = [saved.room]; let operation;
  const ui = await browser({ rooms, saved, roomLifecycleSupport: true, route(request) {
    if (request.path.endsWith('/lifecycle')) { operation = lifecycleEnvelope('remove'); operation.lifecycle_operation.request_id = request.body.request_id; return operation; }
    if (request.path.startsWith('/api/room-lifecycle-operations/')) return operation;
  } });
  lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'remove' }).fire('click');
  assert.match(ui.get('lifecycle-confirm-title').textContent, /永久删除/); assert.match(ui.get('lifecycle-confirm-description').textContent, /无法恢复.*原项目文件、原终端.*不会删除/);
  assert.doesNotMatch(ui.get('lifecycle-confirm-note').textContent, /可以.*恢复/); assert.equal(ui.posts().length, 0);
  await ui.click('lifecycle-confirm-cancel'); assert.equal(ui.posts().length, 0);
  lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'remove' }).fire('click'); await ui.click('lifecycle-confirm-submit');
  assert.equal(ui.posts().length, 1); assert.equal(ui.get('rooms').children.length, 1, 'pending deletion does not pretend data is gone');
  operation.lifecycle_operation.status = 'failed'; operation.lifecycle_operation.error = 'purge_files_incomplete'; await ui.poll();
  assert.match(ui.get('room-lifecycle-status').textContent, /删除未完成：文件清理未完成.*记录仍保留/); assert.equal(ui.get('rooms').children.length, 1);
  lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'remove' }).fire('click'); await ui.click('lifecycle-confirm-submit');
  assert.equal(ui.posts().length, 2, 'a failed cleanup retries only after another explicit confirmation');
  operation.lifecycle_operation.status = 'completed'; operation.deleted = true; rooms.length = 0; await ui.poll();
  assert.equal(ui.get('rooms').children.length, 0); assert.equal(ui.get('room-lifecycle-menu').hidden, true, 'confirmed deletion clears the old selected chat');
  assert.equal(ui.posts().length, 2);
});

test('legacy removed rooms remain an explicit dynamic review without automatic deletion or a third normal filter', async () => {
  const saved = snapshot({ status: 'ready_for_user' }); saved.room.lifecycle_state = 'removed';
  const ui = await browser({ rooms: [saved.room], saved, roomLifecycleSupport: true });
  assert.deepEqual(ui.get('rooms-filter').children.map((node) => node.getAttribute('value')), ['active', 'archived']);
  assert.match(ui.get('legacy-room-summary').textContent, /（1）/); assert.equal(ui.get('legacy-room-review').hidden, false);
  assert.equal(ui.posts().length, 0);
  const remove = lifecycleButton(ui.get('legacy-room-list'), { kind: 'rooms', id: roomId, action: 'remove' }); assert.equal(remove.textContent, '永久删除');
  remove.fire('click'); assert.equal(ui.posts().length, 0); await ui.click('lifecycle-confirm-cancel'); assert.equal(ui.posts().length, 0);
});

test('uncertain lifecycle requests recover by read-only request id across reload and never replay their POST', async () => {
  const saved = snapshot({ status: 'running' }); const storage = new Map(); let body; let received = false;
  const route = (request) => {
    if (request.path.endsWith('/lifecycle')) { body = request.body; throw new Error('lost receipt'); }
    if (request.path.startsWith('/api/room-lifecycle-operations?request_id=')) {
      if (!received) throw new Error('not yet observed');
      const response = lifecycleEnvelope('archive'); response.lifecycle_operation.request_id = body.request_id; return response;
    }
    if (request.path.startsWith('/api/room-lifecycle-operations/')) { const response = lifecycleEnvelope('archive'); response.lifecycle_operation.request_id = body.request_id; return response; }
  };
  const ui = await browser({ rooms: [saved.room], saved, storage, roomLifecycleSupport: true, route });
  lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'archive' }).fire('click'); await settle();
  assert.equal(ui.posts().length, 1); assert.match(ui.get('room-lifecycle-status').textContent, /结果尚未确认/);
  const reloaded = await browser({ rooms: [saved.room], saved, storage, roomLifecycleSupport: true, route });
  assert.equal(reloaded.posts().length, 0); assert.ok(reloaded.requests.some((request) => request.path === `/api/room-lifecycle-operations?request_id=${body.request_id}`));
  received = true; await reloaded.poll(); assert.equal(reloaded.posts().length, 0); assert.match(reloaded.get('room-lifecycle-status').textContent, /正在停止任务并归档/);
});

test('native recovery keeps frozen task settings visible and pending continue does not claim an absent supplement', async () => {
  const saved = withNaturalContinuation(nativeRecoverySnapshot()); saved.current_run.can_recover_native = true;
  saved.current_run.native_recovery_kind = 'partial_work'; saved.current_run.native_recovery_reason = null;
  saved.current_run.runtime_settings = { permission: 'full_access', members: { codex: { model: 'chosen-model', effort: 'high' } } };
  const waiting = deferred();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.endsWith('/messages')) return waiting.promise;
  } });
  assert.equal(ui.get('runtime-settings-summary').textContent, '继续沿用本任务设置');
  assert.match(ui.get('composer-usage-bar').textContent, /chosen-model.*高/);
  const action = descendants(ui.get('collaboration-outcome')).find((node) => node.tagName === 'BUTTON' && node.textContent === '继续'); assert.ok(action);
  action.fire('click'); await settle();
  assert.match(ui.get('pending-description').textContent, /接着处理原任务/); assert.doesNotMatch(ui.get('pending-description').textContent, /发给.*补充说明/);
  assert.doesNotMatch(ui.get('composer-note').textContent, /原消息/);
  waiting.resolve(saved); await settle();
});

test('retired partner cards describe a real takeover plainly and omit an unrecorded update time', async () => {
  const saved = objectiveSnapshot({ status: 'running' });
  saved.objective.retired_partners = { claude: { cleanup_confirmed: true } };
  saved.team = { run_id: saved.current_run.id, phase: 'working', tasks: [{ id: 'takeover-task', owner: 'codex', status: 'working', title: '真实接手任务' }] };
  saved.events = [{ seq: 1, kind: 'task_taken_over', owner: 'codex', previous_owner: 'claude', task_id: 'takeover-task', created_at: 1000 }]; saved.jobs = [];
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.match(ui.get('partner-activity-claude').textContent, /本轮暂不参与.*此前额度受限.*Codex 正在接着做/);
  assert.doesNotMatch(ui.get('partner-activity-claude').textContent, /确认清理|更新时间未记录/);
  saved.events = []; const withoutTakeover = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.match(withoutTakeover.get('partner-activity-claude').textContent, /此前额度受限.*本轮安排保持/); assert.doesNotMatch(withoutTakeover.get('partner-activity-claude').textContent, /Codex 正在接着做/);
});

test('a partner retired because its model service never connected is not described as out of quota', async () => {
  // Catches: a partner that could not start being reported as having run out of quota.
  const saved = objectiveSnapshot({ status: 'running' });
  saved.objective.retired_partners = { claude: { reason: 'native_preflight_failed', cleanup_confirmed: true } };
  saved.team = { run_id: saved.current_run.id, phase: 'working', tasks: [{ id: 'takeover-task', owner: 'codex', status: 'working', title: '真实接手任务' }] };
  saved.events = [{ seq: 1, kind: 'task_taken_over', owner: 'codex', previous_owner: 'claude', task_id: 'takeover-task', created_at: 1000 }]; saved.jobs = [];
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.match(ui.get('partner-activity-claude').textContent, /本轮暂不参与.*模型服务连接失败.*Codex 正在接着做/);
  assert.doesNotMatch(ui.get('partner-activity-claude').textContent, /额度/);
});

test('a task the host moved before work began is named in the chat in both languages', async () => {
  // Catches the rebalance line missing from the timeline or left untranslated.
  const saved = objectiveSnapshot({ status: 'running' });
  saved.team = { run_id: saved.current_run.id, phase: 'working', tasks: [{ id: 'write', title: '整理导入脚本', owner: 'codex', status: 'assigned' }] };
  saved.jobs = [];
  saved.events = [{ seq: 1, kind: 'task_rebalanced', run_id: saved.current_run.id, task_id: 'write', title: '整理导入脚本', previous_owner: 'claude', owner: 'codex',
    assignment_epoch: 1, reason: 'over_share', created_at: 1100 }];
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.match(ui.get('events').textContent, /为均衡工作量，.*整理导入脚本.*已从 Claude Code 改由 Codex 负责/);
  const english = await browser({ language: 'en', rooms: [saved.room], saved, executionPolicySupport: true });
  assert.match(english.get('events').textContent, /To balance the work, .*moved from Claude Code to Codex/);
});
test('work that reached the repository from outside the room is shown in the chat in both languages', async () => {
  // Catches: the host detecting outside commits but the user seeing nothing,
  // or "1 commits" / untranslated Chinese in the English interface.
  const saved = objectiveSnapshot({ status: 'running' });
  saved.events = [
    { seq: 1, kind: 'source_advanced_externally', sender: 'system', repository: 'ranger-world-time', external_count: 27, complete: true, commits: [], created_at: 1000 },
    { seq: 2, kind: 'source_advanced_externally', sender: 'system', repository: 'ranger-world-time', external_count: 1, complete: true, commits: [], created_at: 1001 },
    { seq: 3, kind: 'source_advanced_externally', sender: 'system', repository: 'ranger-world-time', external_count: 0, complete: false, commits: [], created_at: 1002 }];
  saved.jobs = [];
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  const chat = ui.get('events').textContent;
  assert.match(chat, /房间外有 27 个新提交进入 ranger-world-time，伙伴会先按最新代码核对任务/);
  assert.match(chat, /ranger-world-time 的提交历史在房间外变了/);
  const english = await browser({ language: 'en', rooms: [saved.room], saved, executionPolicySupport: true });
  const text = english.get('events').textContent;
  assert.match(text, /27 new commits reached ranger-world-time from outside this room/);
  assert.match(text, /1 new commit reached ranger-world-time from outside this room/);
  assert.match(text, /The commit history of ranger-world-time changed outside this room/);
  assert.doesNotMatch(text, /[一-龥]{4,}/);
});

test('a mismatched lifecycle receipt never deletes another task or claims the request succeeded', async () => {
  const saved = snapshot({ status: 'running' });
  const ui = await browser({ rooms: [saved.room], saved, roomLifecycleSupport: true, route(request) {
    if (request.path.endsWith('/lifecycle') || request.path.startsWith('/api/room-lifecycle-operations')) {
      const response = lifecycleEnvelope('remove', 'completed', { deleted: true }); response.lifecycle_operation.request_id = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'; return response;
    }
  } });
  lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'remove' }).fire('click'); await ui.click('lifecycle-confirm-submit');
  assert.equal(ui.get('rooms').children.length, 1); assert.match(ui.get('room-lifecycle-status').textContent, /结果尚未确认/); assert.equal(ui.posts().length, 1);
});

test('a completed remove operation without the deletion receipt keeps the task and stays unconfirmed', async () => {
  const saved = snapshot({ status: 'running' }); let requestId;
  const ui = await browser({ rooms: [saved.room], saved, roomLifecycleSupport: true, route(request) {
    if (request.path.endsWith('/lifecycle') || request.path.startsWith('/api/room-lifecycle-operations')) {
      requestId ||= request.body?.request_id;
      const response = lifecycleEnvelope('remove', 'completed'); response.lifecycle_operation.request_id = requestId; return response;
    }
  } });
  lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'remove' }).fire('click'); await ui.click('lifecycle-confirm-submit');
  assert.equal(ui.get('rooms').children.length, 1); assert.match(ui.get('room-lifecycle-status').textContent, /结果尚未确认/); assert.doesNotMatch(ui.get('room-lifecycle-status').textContent, /已永久删除/);
  assert.equal(ui.posts().length, 1);
});

test('an authoritative missing lifecycle journal re-delivers only the same confirmed request without another prompt', async () => {
  const saved = snapshot({ status: 'running' }); let posts = 0; let original;
  const ui = await browser({ rooms: [saved.room], saved, roomLifecycleSupport: true, route(request) {
    if (request.path.endsWith('/lifecycle')) {
      posts += 1;
      if (posts === 1) { original = structuredClone(request.body); throw new Error('connection dropped before receipt'); }
      assert.deepEqual(request.body, original);
      const response = lifecycleEnvelope('archive'); response.lifecycle_operation.request_id = request.body.request_id; return response;
    }
    if (request.path.startsWith('/api/room-lifecycle-operations?request_id=')) return { error: 'lifecycle_operation_not_found' };
    if (request.path.startsWith('/api/room-lifecycle-operations/')) { const response = lifecycleEnvelope('archive'); response.lifecycle_operation.request_id = original.request_id; return response; }
  } });
  lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'archive' }).fire('click'); await settle();
  assert.equal(posts, 2); assert.deepEqual(ui.posts().map((request) => request.body), [original, original]);
  assert.equal(ui.get('lifecycle-confirm').hidden, true); assert.match(ui.get('room-lifecycle-status').textContent, /正在停止任务并归档/);
  await ui.poll(); assert.equal(posts, 2, 'a received operation is only queried');
});

test('a host-bound partial inspection job is shown as checking earlier work rather than developing', async () => {
  const saved = objectiveSnapshot({ status: 'running' });
  saved.team = { run_id: saved.current_run.id, phase: 'working', tasks: [{ id: 'q1', owner: 'codex', status: 'working', title: '开发 Q1' }] };
  saved.jobs = [{ id: jobId, provider: 'codex', status: 'working', kind: 'team_work' }];
  saved.events = [
    { seq: 1, kind: 'native_partial_inspection_queued', run_id: saved.current_run.id, job_id: jobId, created_at: 1000 },
    { seq: 2, kind: 'job_claimed', run_id: saved.current_run.id, job_id: jobId, sender: 'codex', created_at: 1001 },
    { seq: 3, kind: 'native_tool_activity', run_id: saved.current_run.id, job_id: jobId, sender: 'codex', state: 'completed', tool_kind: 'Read', created_at: 1002 },
  ];
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.match(ui.get('partner-activity-codex').textContent, /正在核对上次进度.*只读核对/);
  assert.doesNotMatch(ui.get('partner-activity-codex').textContent, /正在开发|正在「开发 Q1」/);
  saved.jobs = [{ ...saved.jobs[0], id: 'next-job' }]; saved.events.push({ seq: 4, kind: 'job_claimed', run_id: saved.current_run.id, job_id: 'next-job', sender: 'codex', created_at: 1003 }); await ui.poll();
  assert.doesNotMatch(ui.get('partner-activity-codex').textContent, /正在核对上次进度/);
});

test('a definitive lifecycle rejection does not enter an automatic re-delivery loop', async () => {
  const saved = snapshot({ status: 'running' });
  const ui = await browser({ rooms: [saved.room], saved, roomLifecycleSupport: true, route(request) {
    if (request.path.endsWith('/lifecycle')) return { error: 'request_conflict' };
  } });
  lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'archive' }).fire('click'); await settle(); await ui.poll();
  assert.equal(ui.posts().length, 1); assert.equal(ui.requests.some((request) => request.path.startsWith('/api/room-lifecycle-operations')), false);
  assert.match(ui.get('room-lifecycle-status').textContent, /整理未完成/); assert.equal(ui.get('rooms').children.length, 1);
});

test('a deleted handoff source clears earlier discussion without fetching or resurrecting its cached chat', async () => {
  const sourceRoomId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'; const saved = inlineHandoffFixture(sourceRoomId);
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path === `/api/rooms/${sourceRoomId}`) return { room: { id: sourceRoomId }, members: [], events: [{ seq: 1, kind: 'message', sender: 'claude', text: '删除前真实讨论', created_at: 1 }] };
  } });
  ui.get('earlier-discussion').open = true; ui.get('earlier-discussion').fire('toggle'); await settle();
  assert.match(ui.get('earlier-messages').textContent, /删除前真实讨论/); const reads = ui.requests.filter((request) => request.path === `/api/rooms/${sourceRoomId}`).length;
  saved.native_handoff_source_available = false; await ui.poll();
  assert.equal(ui.get('earlier-discussion').hidden, true); assert.equal(ui.get('earlier-messages').children.length, 0);
  ui.get('earlier-discussion').open = true; ui.get('earlier-discussion').fire('toggle'); await settle();
  assert.equal(ui.requests.filter((request) => request.path === `/api/rooms/${sourceRoomId}`).length, reads);
});

test('a known room can archive during an in-flight message without a late reply painting it active again', async () => {
  const saved = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); const late = structuredClone(saved); const response = deferred();
  late.events.push({ seq: 999, kind: 'message', sender: 'codex', text: '晚到的旧执行回复', created_at: 2000 });
  const ui = await browser({ rooms: [saved.room], saved, roomLifecycleSupport: true, executionPolicySupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.endsWith('/messages')) return response.promise;
    if (request.path.endsWith('/lifecycle')) {
      saved.room.lifecycle_state = 'archived'; const result = { ...structuredClone(saved), ...lifecycleEnvelope('archive', 'completed') };
      result.lifecycle_operation.request_id = request.body.request_id; return result;
    }
  } });
  ui.type('继续'); await ui.submit(); const original = structuredClone(ui.posts()[0]);
  const archive = lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'archive' }); assert.equal(archive.disabled, false);
  archive.fire('click'); await settle(); assert.equal(ui.posts().length, 2); assert.equal(ui.get('pending-request').hidden, true, 'superseded receipt no longer occupies the send slot');
  response.resolve(late); await settle();
  assert.match(ui.get('room-lifecycle-note').textContent, /已归档/); assert.doesNotMatch(ui.get('events').textContent, /晚到的旧执行回复/);
  assert.equal(ui.get('pending-request').hidden, true); assert.deepEqual(structuredClone(ui.posts()[0]), original); await ui.poll(); assert.equal(ui.posts().length, 2);
});

test('permanent removal remains available with an unknown message receipt and does not replay or revive that chat', async () => {
  const saved = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); const rooms = [saved.room];
  const ui = await browser({ rooms, saved, roomLifecycleSupport: true, executionPolicySupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.endsWith('/messages')) throw new Error('lost message receipt');
    if (request.path.endsWith('/lifecycle')) { const result = lifecycleEnvelope('remove', 'completed', { deleted: true }); result.lifecycle_operation.request_id = request.body.request_id; rooms.length = 0; return result; }
  } });
  ui.type('继续'); await ui.submit();
  const remove = lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'remove' }); assert.equal(remove.disabled, false);
  remove.fire('click'); await ui.click('lifecycle-confirm-submit'); assert.equal(ui.get('room-lifecycle-menu').hidden, true);
  assert.equal(ui.get('new-room').disabled, false, 'the removed chat does not hold the global send slot');
  await ui.click('retry-request'); assert.equal(ui.posts().length, 2, 'old message body is retained but cannot be re-delivered after explicit removal');
});

test('stop uses its existing idempotent route independently of an in-flight continuation', async () => {
  const saved = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); const response = deferred(); const stopped = structuredClone(saved); stopped.objective.status = 'stopped'; stopped.room.status = 'cancelled';
  const ui = await browser({ rooms: [saved.room], saved, roomLifecycleSupport: true, executionPolicySupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.endsWith('/messages')) return response.promise;
    if (request.path.endsWith('/cancel')) return stopped;
  } });
  ui.type('继续'); await ui.submit(); const original = structuredClone(ui.posts()[0]);
  assert.equal(ui.get('cancel-room').disabled, false); await ui.click('cancel-room');
  assert.deepEqual(ui.posts().map((request) => request.path), [`/api/rooms/${roomId}/messages`, `/api/rooms/${roomId}/cancel`]); assert.deepEqual(ui.posts()[1].body, {});
  response.resolve(saved); await settle(); assert.match(ui.get('room-status').textContent, /停止/); assert.deepEqual(structuredClone(ui.posts()[0]), original);
});

test('a late superseded response cannot release the send slot or replace the newer task request', async () => {
  const saved = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); const late = structuredClone(saved); const oldResponse = deferred(); const newResponse = deferred(); const storage = new Map();
  const ui = await browser({ rooms: [saved.room], saved, storage, roomLifecycleSupport: true, executionPolicySupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.endsWith('/messages')) return oldResponse.promise;
    if (request.path === '/api/rooms' && request.method === 'POST') return newResponse.promise;
    if (request.path.endsWith('/lifecycle')) { saved.room.lifecycle_state = 'archived'; const result = { ...structuredClone(saved), ...lifecycleEnvelope('archive', 'completed') }; result.lifecycle_operation.request_id = request.body.request_id; return result; }
  } });
  ui.type('继续'); await ui.submit(); const original = structuredClone(ui.posts()[0].body);
  lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'archive' }).fire('click'); await settle();
  const metadata = savedOutbox(storage); assert.deepEqual(metadata.sidelined_requests[0].request.body, original);
  await ui.click('new-room'); ui.type('另一个新的任务'); await ui.submit();
  assert.equal(ui.posts().length, 3); assert.match(ui.get('pending-description').textContent, /另一个新的任务/);
  oldResponse.resolve(late); await settle();
  assert.equal(ui.get('pending-request').hidden, false); assert.match(ui.get('pending-description').textContent, /另一个新的任务/); assert.equal(ui.get('send-label').textContent, '发送中');
  const next = structuredClone(saved); next.room.id = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'; next.room.goal = '另一个新的任务'; next.objective.goal = '另一个新的任务'; next.objective.original_goal = '另一个新的任务'; next.room.objective_goal = '另一个新的任务'; next.room.lifecycle_state = 'active'; newResponse.resolve(next); await settle();
  assert.equal(ui.get('pending-request').hidden, true); assert.match(ui.get('room-title').textContent, /另一个新的任务/);
  assert.equal(savedOutbox(storage).sidelined_requests.length, 0, 'a confirmed superseded receipt no longer retains its body in the outbox');
});

test('permanent deletion clears cached reader, chat, draft and receipt bodies, including a late original reply', async () => {
  const saved = withNaturalContinuation(readableDelivery()); const rooms = [saved.room]; const late = structuredClone(saved); const receipt = deferred(); const storage = new Map();
  saved.events.push({ seq: 999, kind: 'message', sender: 'claude', text: '仅属于被删聊天的正文', created_at: 1000 });
  const ui = await browser({ rooms, saved, storage, roomLifecycleSupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.endsWith('/messages')) return receipt.promise;
    if (request.path.endsWith('/lifecycle')) { rooms.length = 0; const result = lifecycleEnvelope('remove', 'completed', { deleted: true }); result.lifecycle_operation.request_id = request.body.request_id; return result; }
  } });
  deliveryButton(ui, '交付/说明.md').fire('click'); await settle(); assert.match(ui.get('reader-body').textContent, /交付说明/); await ui.click('reader-back');
  ui.type('必须从浏览器缓存删除的敏感补充'); await ui.submit(); const requestId = ui.posts()[0].body.request_id;
  lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'remove' }).fire('click'); await ui.click('lifecycle-confirm-submit');
  for (const id of ['reader-body', 'reader-files', 'events', 'earlier-messages', 'project-files']) assert.equal(ui.get(id).textContent, '', id);
  assert.doesNotMatch([...storage.values()].join(' '), /必须从浏览器缓存删除的敏感补充|仅属于被删聊天的正文|交付说明/);
  const tombstone = savedOutbox(storage); assert.ok(tombstone.deleted_rooms.includes(roomId));
  assert.ok(tombstone.sidelined_requests.every(entry => entry.request?.body?.request_id !== requestId || Object.keys(entry.request.body).every(key => key === 'request_id')));
  receipt.resolve(late); await settle(); assert.equal(ui.get('room-title').textContent, '新任务'); assert.equal(ui.get('events').textContent, '');
  assert.doesNotMatch([...storage.values()].join(' '), /必须从浏览器缓存删除的敏感补充|交付说明/);
  await ui.click('new-room'); assert.equal(ui.get('message').value, ''); assert.equal(ui.posts().length, 2);
});

test('polling an authoritative missing room clears this tabs stale chat, while a network error does not', async () => {
  for (const error of ['room_not_found', 'network']) {
    const saved = snapshot({ status: 'ready_for_user' }); saved.events.push({ seq: 999, kind: 'message', sender: 'claude', text: '别的窗口删除前的正文', created_at: 1000 });
    let missing = false;
    const ui = await browser({ rooms: [saved.room], saved, roomLifecycleSupport: true, route(request) {
      if (request.path === '/api/rooms' && missing) return { rooms: [] };
      if (request.path === `/api/rooms/${roomId}` && missing) { if (error === 'network') throw new Error('offline'); return { error }; }
    } });
    ui.type('旧任务的草稿'); missing = true; await ui.poll();
    if (error === 'room_not_found') {
      assert.equal(ui.get('room-title').textContent, '新任务'); assert.equal(ui.get('events').textContent, ''); assert.equal(ui.get('message').value, ''); assert.equal(ui.get('new-room').disabled, false);
    } else {
      assert.match(ui.get('events').textContent, /别的窗口删除前的正文/); assert.equal(ui.get('message').value, '旧任务的草稿');
    }
    assert.equal(ui.posts().length, 0);
  }
});

test('the room list distinguishes pausing from paused using actual call or owned-process state', async () => {
  const saved = objectiveSnapshot({ status: 'paused' }); saved.objective.pause_requested = true;
  saved.jobs = [{ id: jobId, provider: 'codex', status: 'working' }];
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.match(ui.get('rooms').firstChild.lastChild.textContent, /正在暂停/);
  saved.objective.pause_requested = false; await ui.poll(); assert.match(ui.get('rooms').firstChild.lastChild.textContent, /正在暂停/);
  saved.jobs[0].status = 'queued'; await ui.poll(); assert.match(ui.get('rooms').firstChild.lastChild.textContent, /正在暂停/);
  saved.jobs = []; saved.native_executions = [{ status: 'working', provider: 'codex' }]; await ui.poll(); assert.match(ui.get('rooms').firstChild.lastChild.textContent, /正在暂停/);
  saved.native_executions = []; await ui.poll(); assert.match(ui.get('rooms').firstChild.lastChild.textContent, /已暂停/); assert.doesNotMatch(ui.get('rooms').firstChild.lastChild.textContent, /正在暂停/);
  assert.equal(ui.posts().length, 0);
});

test('native-session cleanup confirmation describes only the capability actually supplied by the host', async () => {
  for (const supported of [false, true]) {
    const saved = snapshot({ status: 'running' }); let operation;
    const ui = await browser({ rooms: [saved.room], saved, roomLifecycleSupport: true, roomNativeSessionCleanupSupport: supported, route(request) {
      if (request.path.endsWith('/lifecycle')) { operation = lifecycleEnvelope('remove'); operation.lifecycle_operation.request_id = request.body.request_id; operation.lifecycle_operation.native_sessions_pending = 2; operation.lifecycle_operation.room_data_deleted = false; return operation; }
      if (request.path.startsWith('/api/room-lifecycle-operations/')) return operation;
    } });
    lifecycleButton(ui.get('room-lifecycle-actions'), { kind: 'rooms', id: roomId, action: 'remove' }).fire('click');
    const description = ui.get('lifecycle-confirm-description').textContent;
    if (supported) { assert.match(description, /专用本机会话.*其它聊天、原项目文件和账号不受影响.*云端记录不在清理范围/); assert.doesNotMatch(description, /供应商保存的会话不会删除/); }
    else { assert.doesNotMatch(description, /专用本机会话/); assert.match(description, /模型供应商保存的会话不会删除/); }
    assert.equal(ui.posts().length, 0); await ui.click('lifecycle-confirm-submit'); await ui.poll();
    assert.doesNotMatch(ui.get('room-lifecycle-status').textContent, /已永久删除/); assert.equal(ui.get('rooms').children.length, 1); assert.equal(ui.posts().length, 1);
  }
});

test('paused partner summaries match the real pause state and display task titles without frozen progress prefixes', async () => {
  const saved = objectiveSnapshot({ status: 'paused' }); saved.objective.pause_requested = false;
  saved.members.forEach((member) => { member.status = 'needs_user'; }); saved.jobs = [];
  saved.team = { run_id: saved.current_run.id, phase: 'working', tasks: [
    { id: 'dependency', title: '[0%] 实现恢复链', owner: 'claude', status: 'accepted' },
    { id: 'evidence', title: '[0%] 更新证据说明', owner: 'codex', status: 'assigned', dependencies: ['dependency'] },
  ] }; saved.collaboration.tasks = saved.team.tasks;
  const publicText = '[0%] 我说的原文保持这样。';
  saved.events = [{ seq: 1, kind: 'message', sender: 'codex', text: publicText, created_at: 1000 }];
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.equal(ui.get('status-codex').textContent, '已暂停'); assert.equal(ui.get('task-codex').textContent, '继续后接着做。'); assert.doesNotMatch(ui.get('status-codex').textContent, /等你处理/);
  assert.doesNotMatch(ui.get('collaboration-plan').textContent, /\[0%\]/); assert.match(ui.get('collaboration-plan').textContent, /实现恢复链.*已完成|实现恢复链.*审核/);
  assert.match(ui.get('events').textContent, /\[0%\] 我说的原文保持这样/);
  assert.equal(saved.team.tasks[0].title, '[0%] 实现恢复链'); assert.equal(saved.events[0].text, publicText);
  saved.objective.pause_requested = true; saved.jobs = [{ id: jobId, provider: 'codex', status: 'working', kind: 'team_work' }]; saved.team.tasks[1].status = 'working';
  saved.events.push({ seq: 2, kind: 'job_claimed', sender: 'codex', run_id: saved.current_run.id, job_id: jobId, created_at: 1001 }); await ui.poll();
  assert.equal(ui.get('status-codex').textContent, '正在暂停'); assert.match(ui.get('task-codex').textContent, /更新证据说明.*处理结束后暂停/); assert.doesNotMatch(ui.get('task-codex').textContent, /\[0%\]/);
  assert.match(ui.get('work-mode-note').textContent, /正在暂停/);
  saved.members[1].identity_uncertain = true; await ui.poll(); assert.equal(ui.get('status-codex').textContent, '结果待核对');
  saved.members[1].identity_uncertain = false; saved.members[1].status = 'failed'; await ui.poll(); assert.equal(ui.get('status-codex').textContent, '执行失败');
  assert.equal(ui.posts().length, 0);
});

test('natural completion and delivery copy points to the same chat instead of an absent next-round or new-task step', async () => {
  const complete = withNaturalContinuation(objectiveSnapshot({ status: 'complete' }));
  const ui = await browser({ rooms: [complete.room], saved: complete, executionPolicySupport: true, naturalContinuationSupport: true });
  assert.match(ui.get('work-mode-note').textContent, /直接在这里说/); assert.doesNotMatch(ui.get('work-mode-note').textContent, /开新任务/); assert.equal(ui.get('message').disabled, false);
  const delivered = withNaturalContinuation(readableDelivery()); delivered.team.phase = 'ready_for_user'; delivered.current_run.work_mode = 'legacy'; delivered.current_run.can_continue = true;
  const old = await browser({ rooms: [delivered.room], saved: delivered, executionPolicySupport: true, naturalContinuationSupport: true });
  assert.match(old.get('collaboration-outcome').textContent, /需要调整时，直接在下方补充/);
  assert.doesNotMatch(old.get('collaboration-outcome').textContent, /再点“开始下一轮”|请用当前项目开新任务/);
  assert.equal(old.get('send-label').textContent, '发送'); assert.equal(ui.posts().length + old.posts().length, 0);
});

test('a naturally paused chat has one continue action while running pause and old-host resume remain available', async () => {
  const saved = withNaturalContinuation(objectiveSnapshot({ status: 'paused' })); saved.objective.pause_requested = false;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.endsWith('/messages')) return saved;
  } });
  assert.equal(ui.get('pause-objective').hidden, true); assert.equal(ui.get('continue-current').textContent, '继续');
  await ui.click('continue-current'); assert.equal(ui.posts()[0].path, `/api/rooms/${roomId}/messages`); assert.equal(ui.posts()[0].body.intent, 'continue');
  saved.objective.status = 'running'; await ui.poll(); assert.equal(ui.get('pause-objective').hidden, false); assert.equal(ui.get('pause-objective').textContent, '暂停推进');
  saved.objective.status = 'paused'; saved.objective.pause_requested = true; await ui.poll();
  assert.equal(ui.get('pause-objective').hidden, false); assert.equal(ui.get('pause-objective').disabled, true); assert.match(ui.get('pause-objective').textContent, /正在暂停/);
  saved.objective.pause_requested = false;
  const old = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.equal(old.get('pause-objective').hidden, false); assert.equal(old.get('pause-objective').textContent, '继续推进');
});

test('collapsed activity stays one short identity line with real task and tool details available on expansion', async () => {
  const saved = activeActivitySnapshot(); const original = '我来做第一项，请你核对第二项。';
  saved.events.push({ seq: 10, kind: 'message', sender: 'claude', recipient: 'codex', job_id: jobId, native_progress: true, text: original, created_at: 199 });
  const ui = await browser({ rooms: [saved.room], saved }); const card = ui.get('partner-activity-claude');
  for (const [operation, state, expected] of [['read_file', 'started', '正在读文件'], ['edit_file', 'started', '正在改文件'], ['run_command', 'started', '正在执行'], ['run_command', 'completed', '正在处理']]) {
    saved.events.push({ seq: 100 + saved.events.length, kind: 'native_tool_activity', run_id: saved.current_run.id, job_id: jobId, sender: 'claude', operation, state, created_at: 200 + saved.events.length });
    await ui.poll();
    assert.match(card.firstChild.textContent, new RegExp(expected)); assert.match(card.firstChild.textContent, /Claude Code/);
    assert.equal(descendants(card.firstChild).some((node) => ['activity-task', 'activity-time'].includes(node.className)), false);
    assert.match(card.children[1].textContent, /实现计分规则/);
    assert.doesNotMatch(card.firstChild.textContent, /已完成|测试通过|最近完成/);
    assert.match(ui.get('events').textContent, /我来做第一项，请你核对第二项/); assert.equal(saved.events.find((event) => event.seq === 10).text, original);
  }
  card.open = true; await ui.poll(); assert.equal(card.open, true); assert.match(card.children[1].textContent, /最近完成：执行命令/);
  const css = fs.readFileSync(path.join(assetRoot, 'style.css'), 'utf8');
  assert.match(css, /\.activity-state \{ flex: 1 1 auto;[^}]*white-space: nowrap/); assert.match(css, /\.activity-identity strong \{ flex: 0 0 auto/);
  assert.match(css, /\.activity-identity strong, \.activity-state, \.activity-details-body \{ font-size: 12px/);
  assert.equal(ui.posts().length, 0);
});


test('message receipts confirm lost replies without redispatch and only authoritative misses resend the exact body', async () => {
  for (const receipt of ['received', 'missing', 'network', 'conflict']) {
    const saved = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); let sends = 0;
    const ui = await browser({ rooms: [saved.room], saved, naturalContinuationSupport: true, messageReceiptsSupport: true, route(request) {
      if (request.path.endsWith('/messages')) { if (++sends === 1) throw new Error('lost response'); return saved; }
      if (request.path.endsWith('/messages/receipt')) {
        if (receipt === 'missing') return { error: 'message_request_not_found' };
        if (receipt === 'network') throw new Error('still offline');
        if (receipt === 'conflict') return { error: 'request_conflict' };
        return { received: true, room_id: roomId, request_id: request.body.request_id };
      }
    } });
    ui.type('保留真实补充'); await ui.submit();
    const bodies = ui.posts().filter((request) => request.path.endsWith('/messages')).map((request) => request.body);
    assert.equal(bodies.length, receipt === 'missing' ? 2 : 1);
    const checks = ui.posts().filter((request) => request.path.endsWith('/messages/receipt')); assert.equal(checks.length, 1);
    assert.deepEqual(structuredClone(checks[0].body), structuredClone(bodies[0]));
    if (receipt === 'missing') assert.deepEqual(structuredClone(bodies[1]), structuredClone(bodies[0]));
    assert.equal(ui.get('pending-request').hidden, ['received', 'missing'].includes(receipt));
    if (['network', 'conflict'].includes(receipt)) assert.equal(ui.get('message').value, '保留真实补充');
  }
});

test('message receipt navigation preserves original outbox and late replies cannot replace the newer task', async () => {
  const saved = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); const response = deferred(); const newer = deferred(); const storage = new Map();
  const ui = await browser({ rooms: [saved.room], saved, storage, naturalContinuationSupport: true, messageReceiptsSupport: true, route(request) {
    if (request.path.endsWith('/messages')) return response.promise;
    if (request.path === '/api/rooms' && request.method === 'POST') return newer.promise;
  } });
  ui.type('原聊天补充'); await ui.submit(); const original = structuredClone(ui.posts()[0].body);
  assert.equal(ui.get('new-room').disabled, false); await ui.click('new-room');
  assert.equal(savedOutbox(storage).sidelined_requests[0].request.body.text, '原聊天补充');
  ui.type('新的任务正文'); await ui.submit();
  response.resolve(saved); await settle();
  assert.match(ui.get('pending-description').textContent, /新的任务正文/); assert.equal(ui.get('send-label').textContent, '发送中');
  assert.deepEqual(structuredClone(ui.posts()[0].body), original); assert.equal(ui.get('message').value, '新的任务正文');
});

test('detached message receipts do not resend after independent archive and deletion erases their original bodies', async () => {
  const saved = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); const response = deferred(); const rooms = [saved.room]; const storage = new Map();
  const ui = await browser({ rooms, saved, storage, roomLifecycleSupport: true, naturalContinuationSupport: true, messageReceiptsSupport: true, route(request) {
    if (request.path.endsWith('/messages')) return response.promise;
    if (request.path.endsWith('/lifecycle')) { const result = lifecycleEnvelope('remove', 'completed', { deleted: true }); result.lifecycle_operation.request_id = request.body.request_id; rooms.length = 0; return result; }
  } });
  ui.type('被删除的补充正文'); await ui.submit(); await ui.click('new-room');
  await ui.click('open-projects');
  // The sidebar context menu remains available even though the original send is still in flight.
  const card = ui.get('rooms').children[0]; card.fire('contextmenu', { preventDefault() {}, clientX: 1, clientY: 1 });
  lifecycleButton(ui.get('entry-context-actions'), { kind: 'rooms', id: roomId, action: 'remove' }).fire('click'); await ui.click('lifecycle-confirm-submit');
  response.resolve(saved); await settle(); await ui.poll();
  assert.doesNotMatch([...storage.values()].join(''), /被删除的补充正文/);
  assert.equal(ui.posts().filter((request) => request.path.endsWith('/messages')).length, 1);
});

test('project registration deletion is capability gated and retains old removed entries for explicit review', async () => {
  const project = registeredProject('33333333-3333-4333-8333-333333333333', '当前登记'); const old = registeredProject('44444444-4444-4444-8444-444444444444', '旧移除登记', 'removed'); const projects = [project, old];
  const ui = await browser({ projects, projectDeletionSupport: true, route(request) {
    if (request.path.endsWith('/lifecycle')) {
      assert.deepEqual(Object.keys(request.body).sort(), ['action', 'request_id']);
      projects.splice(projects.indexOf(project), 1);
      return { project_id: project.id, deleted: true, projects, project_deletion: { project_id: project.id, request_id: request.body.request_id, status: 'completed', error: null } };
    }
  } });
  await ui.click('open-projects'); assert.equal(ui.posts().length, 0);
  assert.deepEqual(ui.get('projects-filter').children.map((node) => node.value), ['active', 'archived']);
  assert.match(ui.get('projects-list').textContent, /整理旧移除登记（1）.*旧移除登记/);
  lifecycleButton(ui.get('projects-list'), { kind: 'projects', id: project.id, action: 'remove' }).fire('click');
  assert.match(ui.get('lifecycle-confirm-title').textContent, /永久删除项目登记/);
  assert.match(ui.get('lifecycle-confirm-description').textContent, /原项目文件和已有聊天中的项目副本都会保留/);
  await ui.click('lifecycle-confirm-submit'); assert.equal(ui.posts().length, 1);
  assert.equal(projects.includes(old), true); assert.match(ui.get('manager-notice').textContent, /已永久删除/);
});

test('unknown project deletion checks its journal and only a journal miss repeats the original intent', async () => {
  const project = registeredProject('33333333-3333-4333-8333-333333333333', '登记'); let sends = 0;
  const ui = await browser({ projects: [project], projectDeletionSupport: true, route(request) {
    if (request.path.endsWith('/lifecycle')) {
      if (++sends === 1) throw new Error('response lost');
      return { project_id: project.id, deleted: true, projects: [], project_deletion: { project_id: project.id, request_id: request.body.request_id, status: 'completed', error: null } };
    }
    if (request.path.startsWith('/api/project-deletions?')) return { error: 'project_deletion_not_found' };
  } });
  await ui.click('open-projects'); lifecycleButton(ui.get('projects-list'), { kind: 'projects', id: project.id, action: 'remove' }).fire('click'); await ui.click('lifecycle-confirm-submit');
  const posts = ui.posts().filter((request) => request.path.endsWith('/lifecycle')); assert.equal(posts.length, 2); assert.deepEqual(structuredClone(posts[0].body), structuredClone(posts[1].body));
  assert.equal(ui.requests.filter((request) => request.path.startsWith('/api/project-deletions?')).length, 1);
});

test('detached unknown message resumes receipt checking across reload without changing the request or replaying confirmed work', async () => {
  const saved = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); const response = deferred(); const storage = new Map();
  const ui = await browser({ rooms: [saved.room], saved, storage, naturalContinuationSupport: true, messageReceiptsSupport: true, route(request) {
    if (request.path.endsWith('/messages')) return response.promise;
  } });
  ui.type('跨窗口保留原消息'); await ui.submit(); const original = structuredClone(ui.posts()[0].body); await ui.click('new-room');
  const reloaded = await browser({ rooms: [saved.room], saved, storage, naturalContinuationSupport: true, messageReceiptsSupport: true, route(request) {
    if (request.path.endsWith('/messages/receipt')) return { received: true, room_id: roomId, request_id: request.body.request_id };
  } });
  const queries = reloaded.posts(); assert.equal(queries.length, 1); assert.match(queries[0].path, /messages\/receipt$/); assert.deepEqual(structuredClone(queries[0].body), original);
  assert.equal(savedOutbox(storage).sidelined_requests.length, 0);
});

test('message receipt deletion clears the original body and a conflict can never be manually replayed', async () => {
  for (const code of ['room_deleted', 'request_conflict']) {
    const saved = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); const storage = new Map();
    const ui = await browser({ rooms: [saved.room], saved, storage, naturalContinuationSupport: true, messageReceiptsSupport: true, route(request) {
      if (request.path.endsWith('/messages')) throw new Error('lost reply');
      if (request.path.endsWith('/messages/receipt')) return { error: code };
    } });
    ui.type('仅属于原请求的正文'); await ui.submit(); await ui.click('retry-request'); await ui.poll();
    assert.equal(ui.posts().filter((request) => request.path.endsWith('/messages')).length, 1);
    if (code === 'room_deleted') { assert.doesNotMatch([...storage.values()].join(''), /仅属于原请求的正文/); assert.equal(ui.get('message').value, ''); }
    else { assert.equal(ui.get('message').value, '仅属于原请求的正文'); assert.equal(ui.get('retry-request').disabled, true); }
  }
});

test('a late missing-message receipt cannot redispatch after the user independently stops the task', async () => {
  const saved = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); const query = deferred(); const stopped = structuredClone(saved); stopped.objective.status = 'stopped'; stopped.room.status = 'cancelled';
  const ui = await browser({ rooms: [saved.room], saved, roomLifecycleSupport: true, naturalContinuationSupport: true, messageReceiptsSupport: true, route(request) {
    if (request.path.endsWith('/messages')) throw new Error('lost reply');
    if (request.path.endsWith('/messages/receipt')) return query.promise;
    if (request.path.endsWith('/cancel')) return stopped;
  } });
  ui.type('待核对消息'); await ui.submit(); await ui.click('cancel-room');
  query.resolve({ error: 'message_request_not_found' }); await settle();
  assert.equal(ui.posts().filter((request) => request.path.endsWith('/messages')).length, 1); assert.match(ui.get('room-status').textContent, /停止/);
});

test('pending or malformed project deletion never reports success and a network error never means journal miss', async () => {
  for (const resultKind of ['pending', 'malformed', 'network']) {
    const project = registeredProject('33333333-3333-4333-8333-333333333333', '保留登记');
    const ui = await browser({ projects: [project], projectDeletionSupport: true, route(request) {
      if (request.path.endsWith('/lifecycle')) {
        if (resultKind === 'network') throw new Error('lost reply');
        return { project_id: project.id, deleted: false, project_deletion: { project_id: project.id, request_id: request.body.request_id, status: resultKind === 'pending' ? 'pending' : 'completed' } };
      }
      if (request.path.startsWith('/api/project-deletions?')) throw new Error('journal unavailable');
    } });
    await ui.click('open-projects'); lifecycleButton(ui.get('projects-list'), { kind: 'projects', id: project.id, action: 'remove' }).fire('click'); await ui.click('lifecycle-confirm-submit'); await ui.poll();
    assert.equal(ui.posts().filter((request) => request.path.endsWith('/lifecycle')).length, 1); assert.doesNotMatch(ui.get('manager-notice').textContent, /已永久删除/);
    assert.match(ui.get('projects-list').textContent, /保留登记/);
  }
});

test('returning to a chat with an unresolved detached message keeps that original pending rather than offering duplicate send', async () => {
  const saved = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); const response = deferred();
  const ui = await browser({ rooms: [saved.room], saved, naturalContinuationSupport: true, messageReceiptsSupport: true, route(request) {
    if (request.path.endsWith('/messages')) return response.promise;
  } });
  ui.type('仍在等待回执的补充'); await ui.submit(); const original = structuredClone(ui.posts()[0].body); await ui.click('new-room');
  ui.get('rooms').children[0].fire('click'); await settle();
  assert.equal(ui.get('pending-request').hidden, false); assert.equal(ui.get('send').disabled, true); assert.equal(ui.get('message').value, original.text);
  await ui.submit(); assert.equal(ui.posts().length, 1); assert.equal(ui.get('new-room').disabled, false);
  response.resolve(saved); await settle(); assert.equal(ui.get('pending-request').hidden, true);
});


test('initial connection has a loading state while send stays disabled, before success or a real failure', async () => {
  for (const failure of [false, true]) {
    const observations = [];
    const ui = await browser({ route(request, document) {
      if (request.path === '/api/status') observations.push({ connection: document.getElementById('connection').textContent,
        note: document.getElementById('composer-note').textContent, disabled: document.getElementById('send').disabled });
      if (failure) throw new Error('fictional connection failure');
    } });
    assert.equal(observations[0].connection, '正在连接本地服务…');
    assert.equal(observations[0].note, '正在连接本地服务…'); assert.equal(observations[0].disabled, true);
    assert.doesNotMatch(observations[0].note, /未连接|刷新|重新打开/);
    assert.equal(ui.get('connection').textContent, failure ? '本地服务未连接' : '本地服务已连接');
    if (failure) { assert.equal(ui.get('send').disabled, true); assert.match(ui.get('composer-note').textContent, /未连接/); }
  }
});

test('missing bootstrap authentication is a real connection failure and never pretends to be loading or connected', async () => {
  const ui = await browser({ token: '__LAC_TOKEN__' });
  assert.equal(ui.requests.length, 0); assert.equal(ui.get('connection').textContent, '本地服务未连接');
  assert.equal(ui.get('send').disabled, true); assert.match(ui.get('notice').textContent, /从本地协作服务打开/);
});

test('sidebar uses the same task waiting semantics as the compact activity card and native working takes precedence', async () => {
  const saved = activeActivitySnapshot(); saved.jobs = []; saved.members.forEach((member) => { member.status = 'idle'; });
  saved.team.tasks = [{ id: 'a', title: '实现规则', owner: 'claude', reviewer: 'codex', status: 'review' }];
  const ui = await browser({ rooms: [saved.room], saved });
  assert.equal(ui.get('status-claude').textContent, '等伙伴审核'); assert.match(ui.get('task-claude').textContent, /实现规则.*等待独立审核/);
  assert.match(ui.get('partner-activity-claude').firstChild.textContent, /等伙伴审核/);
  saved.team.tasks[0] = { id: 'a', title: '实现规则', owner: 'claude', help_to: 'codex', status: 'waiting_peer' }; await ui.poll();
  assert.equal(ui.get('status-claude').textContent, '等伙伴协助'); assert.match(ui.get('task-claude').textContent, /等待Codex协助/);
  saved.jobs.push({ id: 'live-native', provider: 'claude', kind: 'team_help', status: 'working' });
  saved.events.push({ seq: 100, kind: 'job_claimed', run_id: saved.current_run.id, job_id: 'live-native', sender: 'claude', created_at: 150 }); await ui.poll();
  assert.equal(ui.get('status-claude').textContent, '正在协助');
  saved.members[0].identity_uncertain = true; await ui.poll(); assert.equal(ui.get('status-claude').textContent, '结果待核对');
  saved.members[0].identity_uncertain = false; saved.members[0].status = 'failed'; await ui.poll(); assert.equal(ui.get('status-claude').textContent, '执行失败');
  assert.equal(ui.posts().length, 0);
});


test('routine execution lifecycle stays in details while original conversation, help and attention events remain in chat', async () => {
  const saved = snapshot({ status: 'running' });
  saved.events = [message(1, 'user', '一起完成这个任务'),
    { ...message(2, 'claude', '我做第一项，你来核对第二项。'), native_progress: true },
    { seq: 3, kind: 'job_claimed', sender: 'claude', job_id: jobId, created_at: 3 },
    { seq: 4, kind: 'execution_started', revision: 1, created_at: 4 },
    { seq: 5, kind: 'execution_finished', status: 'completed', revision: 1, cleanup_confirmed: true, exit_code: 0, created_at: 5 },
    { seq: 6, kind: 'team_help_requested', sender: 'claude', recipient: 'codex', created_at: 6 },
    { seq: 7, kind: 'execution_finished', status: 'failed', revision: 2, cleanup_confirmed: true, exit_code: 1, created_at: 7 },
    { seq: 8, kind: 'execution_finished', status: 'unknown', revision: 3, cleanup_confirmed: false, created_at: 8 },
    { seq: 9, kind: 'room_status', cancellation_confirmed: false, created_at: 9 },
    { seq: 10, kind: 'team_task_completed', sender: 'claude', title: '完成第一项', created_at: 10 },
  ];
  const before = structuredClone(saved.events); const ui = await browser({ rooms: [saved.room], saved });
  assert.deepEqual(ui.get('events').children.map((node) => Number(node.dataset.seq)), [1, 2, 6, 7, 8, 9, 10]);
  assert.equal(descendants(ui.get('events').children[1]).find((node) => node.className === 'event-text').textContent, saved.events[1].text);
  assert.deepEqual(ui.get('activity-events').children.map((node) => Number(node.dataset.seq)), [3, 4, 5, 6, 7, 8, 9, 10]);
  assert.deepEqual(saved.events, before); assert.equal(ui.posts().length, 0);
});

test('unknown or unconfirmed cleanup keeps independent Stop and only confirmed idle cancellation hides it', async () => {
  const saved = withNaturalContinuation(objectiveSnapshot({ status: 'stopped' })); saved.room.status = 'cancelled';
  saved.team.phase = 'unknown'; saved.collaboration.outcome.state = 'unknown'; saved.cancellation_confirmed = false;
  const reply = deferred(); const ui = await browser({ rooms: [saved.room], saved, naturalContinuationSupport: true, route(request) {
    if (request.path.endsWith('/cancel')) return reply.promise;
  } });
  assert.equal(ui.get('cancel-room').hidden, false); assert.equal(ui.get('cancel-room').disabled, false);
  await ui.click('cancel-room'); await ui.click('cancel-room'); assert.equal(ui.posts().length, 1, 'an in-flight stop cannot be duplicated');
  saved.team.phase = 'stopped'; saved.collaboration.outcome.state = 'stopped'; saved.cancellation_confirmed = true; saved.jobs = [];
  reply.resolve(saved); await settle(); assert.equal(ui.get('cancel-room').hidden, true);
  saved.jobs = [{ id: 'still-live', provider: 'claude', status: 'working' }]; await ui.poll();
  assert.equal(ui.get('cancel-room').hidden, false, 'a live job outweighs an older stop confirmation'); assert.equal(ui.get('cancel-room').disabled, false);
});


test('current stop control distinguishes historical uncertain receipts from current or unbound work without losing old live workers', async () => {
  const saved = withNaturalContinuation(objectiveSnapshot({ status: 'complete' })); saved.room.status = 'completed'; saved.team.phase = 'ready_for_user'; saved.collaboration.outcome.state = 'ready_for_user';
  const oldRun = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
  saved.jobs = [{ id: 'old-unknown', provider: 'claude', status: 'unknown' }];
  saved.events.push({ seq: 100, kind: 'job_created', run_id: oldRun, job_id: 'old-unknown', recipient: 'claude' });
  saved.executions = [{ id: 'old-execution', team_run_id: oldRun, status: 'unknown', result: { cleanup_confirmed: false } }];
  saved.native_executions = [{ run_id: oldRun, cleanup_confirmed: false }];
  const original = structuredClone({ jobs: saved.jobs, executions: saved.executions, native_executions: saved.native_executions });
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  assert.equal(ui.get('cancel-room').hidden, true);
  assert.deepEqual({ jobs: saved.jobs, executions: saved.executions, native_executions: saved.native_executions }, original, 'historical evidence is not rewritten');
  saved.jobs[0].status = 'working'; await ui.poll(); assert.equal(ui.get('cancel-room').hidden, false, 'an old worker still running remains stoppable');
  saved.jobs[0].status = 'unknown'; saved.executions[0].status = 'queued'; await ui.poll(); assert.equal(ui.get('cancel-room').hidden, false);
  saved.executions[0].status = 'unknown'; saved.native_executions[0].run_id = saved.current_run.id; await ui.poll(); assert.equal(ui.get('cancel-room').hidden, false);
  saved.native_executions[0].run_id = oldRun; delete saved.executions[0].team_run_id; await ui.poll(); assert.equal(ui.get('cancel-room').hidden, false, 'missing ownership is conservative');
  saved.executions[0].team_run_id = oldRun; saved.events = saved.events.filter((event) => event.job_id !== 'old-unknown'); await ui.poll(); assert.equal(ui.get('cancel-room').hidden, false, 'an unbound unknown job is conservative');
  saved.jobs = []; saved.current_run.status = 'stopped'; saved.team.phase = 'unknown'; saved.cancellation_confirmed = true; await ui.poll(); assert.equal(ui.get('cancel-room').hidden, false, 'an explicit current unknown phase cannot be masked by stopped run or older cancellation');
  assert.equal(ui.posts().length, 0);
});

test('terminal and needs-user sidebar task states agree with the activity card while real identity and failure warnings remain visible', async () => {
  const saved = withNaturalContinuation(objectiveSnapshot({ status: 'complete' })); saved.room.status = 'completed'; saved.jobs = [];
  saved.members.forEach((member) => { member.status = 'idle'; });
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  assert.equal(ui.get('status-claude').textContent, '目标已交付'); assert.match(ui.get('partner-activity-claude').firstChild.textContent, /目标已交付/);
  saved.current_run.accepted_at = 123; await ui.poll(); assert.equal(ui.get('status-claude').textContent, '目标已确认完成');
  saved.objective.status = 'needs_user'; saved.room.status = 'needs_user'; saved.objective.next_step = { question: '请确认验收范围' }; await ui.poll();
  assert.equal(ui.get('status-claude').textContent, '需要你回复'); assert.match(ui.get('partner-activity-claude').firstChild.textContent, /需要你回复/); assert.match(ui.get('task-claude').textContent, /请确认验收范围/);
  saved.objective.status = 'paused'; saved.objective.pause_requested = false; await ui.poll(); assert.equal(ui.get('status-claude').textContent, '已暂停');
  saved.objective.status = 'complete'; saved.members[0].identity_uncertain = true; await ui.poll(); assert.equal(ui.get('status-claude').textContent, '结果待核对');
  saved.members[0].identity_uncertain = false; saved.members[0].status = 'failed'; await ui.poll(); assert.equal(ui.get('status-claude').textContent, '执行失败');
  assert.equal(ui.posts().length, 0);
});


test('plan updates are neutral progress and concrete collaboration conflicts remain warnings without generic acceptance language', async () => {
  const codes = ['team_plan_changed', 'team_claim_conflict', 'team_write_path_conflict', 'team_plan_stalled'];
  const saved = snapshot({ status: 'running', events: codes.map((code, index) => ({ seq: index + 1, kind: 'warning', code, created_at: index + 1 })) });
  const ui = await browser({ rooms: [saved.room], saved });
  const rows = ui.get('events').children;
  assert.match(rows[0].textContent, /分工已更新/); assert.equal(rows[0].classList.contains('warning'), false);
  assert.match(rows[1].textContent, /该任务已有伙伴认领，等待协调/);
  assert.match(rows[2].textContent, /修改范围重叠，等待调整/);
  assert.match(rows[3].textContent, /尚未形成可执行分工/);
  for (const row of rows.slice(1)) assert.equal(row.classList.contains('warning'), true);
  assert.doesNotMatch(ui.get('events').textContent, /本轮结果需要核对|验收|失败|已解决/);
  assert.equal(ui.get('activity-events').children.length, 4, 'all original warning events remain in execution detail');
  saved.events.push({ seq: 5, kind: 'warning', code: 'team_plan_changed', cancellation_confirmed: false, created_at: 5 }); await ui.poll();
  assert.equal(ui.get('events').children[4].classList.contains('warning'), true, 'an explicit unconfirmed cancellation is still an actual warning');
  assert.equal(ui.posts().length, 0);
});


test('a definitive native continuation rejection stays beside the composer across background refresh without automatic retry', async () => {
  const saved = nativeRecoverySnapshot(); saved.current_run.can_recover_native = true; saved.current_run.native_recovery_reason = null;
  let accepted = false; const resumed = structuredClone(saved); resumed.current_run.id = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'; resumed.current_run.status = 'in_progress'; resumed.team.phase = 'working'; resumed.objective.status = 'running'; resumed.room.status = 'running';
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.endsWith('/recover-native')) return accepted ? resumed : { error: 'native_recovery_unpublished_changes' };
  } });
  ui.type('保留未发送的下一项要求'); await ui.click('recover-native');
  assert.equal(ui.get('pending-request').hidden, true); assert.equal(ui.get('action-error').hidden, false);
  assert.equal(ui.get('action-error').textContent, '上次留下的改动已保留，本次尚未启动续作。');
  assert.doesNotMatch(ui.get('action-error').textContent, /请先处理|副本|内部|审批/); assert.equal(ui.get('message').value, '保留未发送的下一项要求');
  await ui.poll(); await ui.poll();
  assert.equal(ui.get('action-error').hidden, false); assert.match(ui.get('action-error').textContent, /尚未启动续作/); assert.equal(ui.posts().length, 1);
  assert.equal(ui.get('recover-native').disabled, false);
  accepted = true; await ui.click('recover-native'); assert.equal(ui.get('action-error').hidden, true); assert.equal(ui.get('action-error').textContent, '');
  const css = fs.readFileSync(path.join(assetRoot, 'style.css'), 'utf8'); assert.match(css, /\.notice, \.pending-request\s*\{[^}]*font-size: 12px/s);
});

test('operation rejection belongs to its chat and cannot return after navigation or a late detached reply', async () => {
  const saved = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); let deferredReply = null;
  const ui = await browser({ rooms: [saved.room], saved, naturalContinuationSupport: true, messageReceiptsSupport: true, route(request) {
    if (request.path.endsWith('/messages')) return deferredReply ? deferredReply.promise : { error: 'objective_conflict' };
  } });
  ui.type('补充原任务'); await ui.submit(); assert.match(ui.get('action-error').textContent, /目标记录已经变化/);
  ui.get('rooms').children[0].fire('click'); await settle(); assert.equal(ui.get('action-error').hidden, false, 'rereading the same chat is not a new action');
  await ui.click('new-room'); assert.equal(ui.get('action-error').hidden, true); assert.equal(ui.get('action-error').textContent, '');
  ui.get('rooms').children[0].fire('click'); await settle(); assert.equal(ui.get('action-error').hidden, true, 'leaving cleared the old operation error');
  deferredReply = deferred(); ui.type('第二次补充'); await ui.submit(); await ui.click('new-room');
  ui.type('另一个聊天的草稿'); deferredReply.resolve({ error: 'native_recovery_unpublished_changes' }); await settle(); await ui.poll();
  assert.equal(ui.get('action-error').hidden, true); assert.equal(ui.get('message').value, '另一个聊天的草稿');
  assert.equal(ui.posts().filter((request) => request.path.endsWith('/messages')).length, 2);
});


test('an exact room hash restores the same chat with GET only and explicit new task clears the URL selection', async () => {
  const saved = snapshot({ events: [message(1, 'claude', '原聊天正文')] });
  const ui = await browser({ rooms: [saved.room], saved, selectSaved: false, locationHash: `#room=${roomId.toUpperCase()}` });
  assert.match(ui.get('events').textContent, /原聊天正文/); assert.equal(ui.location.hash, `#room=${roomId}`);
  assert.equal(ui.requests.filter((request) => request.path === `/api/rooms/${roomId}`).length, 1); assert.equal(ui.posts().length, 0);
  const reloaded = await browser({ rooms: [saved.room], saved, selectSaved: false, locationHash: ui.location.hash });
  assert.match(reloaded.get('events').textContent, /原聊天正文/); assert.equal(reloaded.posts().length, 0);
  await ui.click('new-room'); assert.equal(ui.location.hash, '');
  saved.room.status = 'running'; const explicitNew = await browser({ rooms: [saved.room], saved, selectSaved: false, locationHash: ui.location.hash });
  assert.equal(explicitNew.get('room-title').textContent, '新任务'); assert.equal(explicitNew.posts().length, 0);
});

test('an archived exact hash is readable without restore and invalid or absent UUID hashes never select another room', async () => {
  const saved = snapshot({ events: [message(1, 'codex', '归档正文可读')] }); saved.room.lifecycle_state = 'archived';
  const archived = await browser({ rooms: [saved.room], saved, selectSaved: false, locationHash: `#room=${roomId}` });
  assert.match(archived.get('events').textContent, /归档正文可读/); assert.equal(archived.get('room-status').textContent, '已归档'); assert.equal(archived.posts().length, 0); assert.equal(saved.room.lifecycle_state, 'archived');
  for (const hash of ['#room=../../private', '#room=not-a-uuid', `#room=${roomId}&token=fictional-secret`, '#room=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb']) {
    const ui = await browser({ rooms: [saved.room], saved, selectSaved: false, locationHash: hash });
    assert.equal(ui.location.hash, ''); assert.equal(ui.get('room-title').textContent, '新任务'); assert.equal(ui.requests.some((request) => request.path === `/api/rooms/${roomId}`), false); assert.equal(ui.posts().length, 0);
  }
});

test('a failed hash read retains its exact intent without polling the room repeatedly and manual refresh can retry the GET', async () => {
  const saved = snapshot({ events: [message(1, 'claude', '重连后原记录')] }); let offline = true;
  const ui = await browser({ rooms: [saved.room], saved, selectSaved: false, locationHash: `#room=${roomId}`, route(request) {
    if (request.path === `/api/rooms/${roomId}` && offline) throw new Error('fictional read failure');
  } });
  assert.equal(ui.location.hash, `#room=${roomId}`); assert.equal(ui.get('room-title').textContent, '新任务');
  await ui.poll(); await ui.poll(); assert.equal(ui.requests.filter((request) => request.path === `/api/rooms/${roomId}`).length, 1);
  offline = false; await ui.click('refresh'); assert.match(ui.get('events').textContent, /重连后原记录/);
  assert.equal(ui.requests.filter((request) => request.path === `/api/rooms/${roomId}`).length, 2); assert.equal(ui.posts().length, 0);
});

test('deleted or unauthorized hash targets clear selection safely while list transport failure preserves it', async () => {
  const saved = snapshot();
  for (const code of ['room_deleted', 'forbidden']) {
    const ui = await browser({ rooms: [saved.room], saved, selectSaved: false, locationHash: `#room=${roomId}`, route(request) {
      if (request.path === `/api/rooms/${roomId}`) return { error: code };
    } });
    assert.equal(ui.location.hash, ''); assert.equal(ui.get('room-title').textContent, '新任务'); await ui.poll();
    assert.equal(ui.requests.filter((request) => request.path === `/api/rooms/${roomId}`).length, 1); assert.equal(ui.posts().length, 0);
  }
  const unavailable = await browser({ rooms: [saved.room], saved, selectSaved: false, locationHash: `#room=${roomId}`, route(request) {
    if (request.path === '/api/rooms') throw new Error('fictional list failure');
  } });
  assert.equal(unavailable.location.hash, `#room=${roomId}`); assert.equal(unavailable.requests.some((request) => request.path === `/api/rooms/${roomId}`), false); assert.equal(unavailable.posts().length, 0);
});


test('protocol result uncertainty is not mislabeled as session identity failure and leaves the underlying gate intact', async () => {
  const saved = nativeRecoverySnapshot(); const runId = saved.current_run.id;
  saved.objective.stop_reason = 'protocol_turn_incomplete';
  saved.members.forEach((member) => { member.identity_uncertain = true; member.status = 'unknown'; });
  saved.jobs = saved.members.map((member) => ({ id: `protocol-${member.id}`, provider: member.id, status: 'unknown', error: 'protocol_turn_incomplete' }));
  saved.events = saved.jobs.map((job, index) => ({ seq: index + 1, kind: 'job_created', job_id: job.id, run_id: runId, recipient: job.provider }));
  saved.native_executions = saved.jobs.map((job) => ({ run_id: runId, job_id: job.id, provider: job.provider, cleanup_confirmed: true }));
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true });
  for (const provider of ['claude', 'codex']) {
    assert.equal(ui.get(`status-${provider}`).textContent, '结果待核对');
    assert.match(ui.get(`partner-activity-${provider}`).firstChild.textContent, /结果待核对/);
    assert.match(ui.get(`task-${provider}`).textContent, /本次回复未完整结束，已保存的改动和记录保留/);
    assert.doesNotMatch(ui.get(`task-${provider}`).textContent, /身份尚未确认|身份已确认/);
  }
  assert.equal(ui.get('recover-native').disabled, true); assert.equal(saved.members.every((member) => member.identity_uncertain), true); assert.equal(ui.posts().length, 0);
  saved.jobs[0].error = 'session_identity_changed'; await ui.poll();
  assert.equal(ui.get('status-claude').textContent, '身份待核查'); assert.match(ui.get('partner-activity-claude').firstChild.textContent, /身份待核查/);
  assert.match(ui.get('task-claude').textContent, /返回的会话身份发生变化/);
  saved.jobs[0].error = 'native_session_identity_mismatch'; await ui.poll(); assert.equal(ui.get('status-claude').textContent, '身份待核查'); assert.match(ui.get('task-claude').textContent, /专用会话身份与本次任务不匹配/);
});


function withRuntimeConfiguration(saved, { canEdit = true, pending = false } = {}) {
  const actual = { permission: 'workspace_write', members: { claude: { model: 'sonnet', effort: 'medium' }, codex: { model: 'gpt-5.4', effort: 'high' } } };
  saved.room.runtime_settings = structuredClone(actual); saved.room.work_mode = 'build';
  saved.current_run.runtime_settings = structuredClone(actual); saved.current_run.work_mode = 'build';
  saved.runtime_configuration = { can_edit: canEdit, reason: canEdit ? null : 'team_cleanup_unconfirmed', desired: { work_mode: 'build', runtime_settings: structuredClone(actual) }, pending, pending_reason: pending ? 'in_flight' : null };
  return saved;
}

function unrecordedRuntimeRoom(mode) {
  const saved = withNaturalContinuation(objectiveSnapshot({ status: 'complete', decision: 'complete' }));
  saved.room.mode = mode; saved.room.work_mode = 'build'; saved.room.runtime_settings = null;
  saved.current_run.runtime_settings = null; saved.current_run.work_mode = 'build';
  saved.runtime_configuration = { can_edit: true, reason: null, desired: { work_mode: 'build', runtime_settings: null }, pending: false, pending_reason: null };
  return saved;
}

test('a finished reply-only room started without choosing models can still pick them for the next message', async () => {
  // Catches: a room whose start recorded no model settings keeping every partner's model fixed after the round ends.
  const saved = unrecordedRuntimeRoom('text_artifacts'); const options = runtimeOptionsFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, naturalRuntimeSupport: true, route(request) {
    if (request.path === '/api/runtime-options') return options;
    if (request.path.endsWith('/messages')) return saved;
  } });
  await ui.click('runtime-shortcut-claude');
  assert.equal(ui.get('runtime-model-claude').disabled, false);
  assert.doesNotMatch(ui.get('runtime-settings-note').textContent, /固定/);
  selectRuntime(ui, 'runtime-model-claude', 'opus'); await ui.click('runtime-settings-close');
  ui.type('换个模型再想一个'); await ui.submit();
  const body = ui.posts().at(-1).body;
  assert.equal(body.runtime_settings.permission, 'workspace_write');
  assert.equal(body.runtime_settings.members.claude.model, 'opus');
  assert.equal(body.runtime_settings.members.codex.model, null);
});

test('a local-development room with no recorded settings stays view-only', async () => {
  // Catches: the reply-only default leaking into native rooms, where a missing record means an old, unknown permission.
  const saved = unrecordedRuntimeRoom('native_project'); const options = runtimeOptionsFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, naturalRuntimeSupport: true, nativeProjectSupport: true, route(request) {
    if (request.path === '/api/runtime-options') return options;
  } });
  await ui.click('runtime-shortcut-claude');
  assert.match(ui.get('runtime-shortcut-claude').getAttribute('aria-label'), /未记录模型.*本轮已固定/);
  assert.match(ui.get('runtime-settings-note').textContent, /已固定/);
});

function agentStartedRoom(workMode) {
  // The read shape of a room an agent started over MCP: no objective, no recorded models.
  const saved = withCurrentRun(snapshot({ agents: catalog.slice(0, 2), status: 'ready_for_user', events: [message(1, 'mcp', '完全虚构的文字目标')] }),
    { work_mode: workMode, runtime_settings: null });
  saved.collaboration = teamCollaboration({ state: 'ready_for_user' });
  saved.room.mode = 'text'; saved.room.work_mode = workMode; saved.room.runtime_settings = null;
  saved.runtime_configuration = { can_edit: true, reason: null, desired: { work_mode: workMode, runtime_settings: null }, pending: false, pending_reason: null };
  return saved;
}

test('a room an agent started without a work mode reads as a normal room whose models can change', async () => {
  // Catches: an agent-started room shown as an old history record with every model locked.
  const saved = agentStartedRoom('build'); const options = runtimeOptionsFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, naturalRuntimeSupport: true, route(request) {
    if (request.path === '/api/runtime-options') return options;
  } });
  assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /历史任务/);
  assert.equal(ui.get('work-mode-plan').disabled, false);
  await ui.click('runtime-shortcut-claude');
  assert.equal(ui.get('runtime-model-claude').disabled, false);
  assert.doesNotMatch(ui.get('runtime-shortcut-claude').getAttribute('aria-label'), /已固定/);
});

test('a real old record without a work mode still reads as history with its models fixed', async () => {
  // Catches: the agent-room change unlocking settings or dropping the history note on genuinely old records.
  const saved = agentStartedRoom('legacy'); const options = runtimeOptionsFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, naturalRuntimeSupport: true, route(request) {
    if (request.path === '/api/runtime-options') return options;
  } });
  assert.match(ui.get('collaboration-outcome').textContent, /这是历史任务/);
  assert.equal(ui.get('work-mode-plan').disabled, true);
  await ui.click('runtime-shortcut-claude');
  assert.equal(ui.get('runtime-model-claude').disabled, true);
});

test('natural runtime edits stay local until one atomic message and show desired pending settings while the actual run stays frozen', async () => {
  const saved = withRuntimeConfiguration(withNaturalContinuation(objectiveSnapshot({ status: 'running' })));
  const actual = structuredClone(saved.room.runtime_settings); const options = runtimeOptionsFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, naturalRuntimeSupport: true, route(request) {
    if (request.path === '/api/runtime-options') return options;
    if (request.path.endsWith('/messages')) { saved.runtime_configuration.desired = { work_mode: request.body.work_mode || 'build', runtime_settings: request.body.runtime_settings || saved.runtime_configuration.desired.runtime_settings }; saved.runtime_configuration.pending = true; return saved; }
  } });
  await openRuntimeSettings(ui); assert.equal(ui.get('runtime-permission').disabled, false); selectRuntime(ui, 'runtime-permission', 'read_only');
  await ui.click('runtime-settings-close'); await ui.click('work-mode-plan');
  await ui.click('runtime-settings'); selectRuntime(ui, 'runtime-model-claude', 'opus'); selectRuntime(ui, 'runtime-effort-claude', 'high'); await ui.click('runtime-settings-close');
  assert.equal(ui.posts().length, 0); assert.equal(ui.get('runtime-shortcuts-caption').textContent, '发消息后生效');
  // Catches a user changing permissions mid-run without being told a message is needed to apply them.
  await openRuntimeSettings(ui); assert.match(ui.get('runtime-settings-note').textContent, /^还没生效：发一条消息才会提交/); await ui.click('runtime-settings-close');
  ui.type('按新设置接着做'); await ui.submit(); const body = ui.posts()[0].body;
  assert.equal(body.work_mode, 'plan'); assert.equal(body.runtime_settings.permission, 'read_only'); assert.equal(body.runtime_settings.members.claude.model, 'opus'); assert.equal(body.runtime_settings.members.claude.effort, 'high');
  assert.deepEqual(saved.room.runtime_settings, actual); assert.deepEqual(saved.current_run.runtime_settings, actual);
  assert.equal(ui.get('runtime-shortcuts-caption').textContent, '后续工作生效'); await openRuntimeSettings(ui); assert.match(ui.get('runtime-settings-note').textContent, /已保存，后续工作开始生效.*正在运行的调用不变/);
  await ui.click('runtime-settings-close'); ui.type('再补充一句'); await ui.submit();
  assert.equal(Object.hasOwn(ui.posts()[1].body, 'runtime_settings'), false); assert.equal(Object.hasOwn(ui.posts()[1].body, 'work_mode'), false, 'unchanged values are omitted and cannot erase pending settings');
  saved.runtime_configuration.pending = false; saved.room.runtime_settings = structuredClone(saved.runtime_configuration.desired.runtime_settings); saved.room.work_mode = 'plan'; await ui.poll();
  assert.equal(ui.get('runtime-shortcuts-caption').textContent, '设置');
});

test('natural runtime settings preserve drafts on rejection and never unlock an old or host-disabled capability', async () => {
  const saved = withRuntimeConfiguration(withNaturalContinuation(objectiveSnapshot({ status: 'running' }))); const options = runtimeOptionsFixture();
  for (const enabled of [false, true]) {
    saved.runtime_configuration.can_edit = enabled;
    const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, naturalRuntimeSupport: enabled, route(request) {
      if (request.path === '/api/runtime-options') return options;
      if (request.path.endsWith('/messages')) return { error: 'runtime_model_unsupported' };
    } });
    await openRuntimeSettings(ui);
    if (!enabled) { assert.equal(ui.get('runtime-permission').disabled, true); assert.equal(ui.get('work-mode-plan').disabled, true); continue; }
    selectRuntime(ui, 'runtime-permission', 'read_only'); await ui.click('runtime-settings-close'); ui.type('保留正文和设置'); await ui.submit(); await ui.poll();
    assert.equal(ui.get('message').value, '保留正文和设置'); assert.equal(ui.get('runtime-shortcuts-caption').textContent, '发消息后生效'); await openRuntimeSettings(ui); assert.equal(ui.get('runtime-permission').value, 'read_only'); assert.match(ui.get('action-error').textContent, /这个模型不在工具返回的可选列表/);
  }
  saved.runtime_configuration.can_edit = false;
  const disabled = await browser({ rooms: [saved.room], saved, naturalRuntimeSupport: true, executionPolicySupport: true, naturalContinuationSupport: true, route(request) { if (request.path === '/api/runtime-options') return options; } });
  await openRuntimeSettings(disabled); assert.equal(disabled.get('runtime-permission').disabled, true); assert.equal(disabled.get('work-mode-plan').disabled, true); assert.equal(disabled.posts().length, 0);
});

test('unknown natural runtime messages retain the exact settings body for receipt and cannot downgrade into an older host', async () => {
  const saved = withRuntimeConfiguration(withNaturalContinuation(objectiveSnapshot({ status: 'running' }))); const storage = new Map(); const options = runtimeOptionsFixture();
  const ui = await browser({ rooms: [saved.room], saved, storage, naturalRuntimeSupport: true, naturalContinuationSupport: true, executionPolicySupport: true, messageReceiptsSupport: true, route(request) {
    if (request.path === '/api/runtime-options') return options;
    if (request.path.endsWith('/messages') || request.path.endsWith('/messages/receipt')) throw new Error('fictional lost receipt');
  } });
  await openRuntimeSettings(ui); selectRuntime(ui, 'runtime-permission', 'read_only'); await ui.click('runtime-settings-close'); ui.type('原设置原消息'); await ui.submit(); const body = structuredClone(ui.posts().find((request) => request.path.endsWith('/messages')).body);
  const query = ui.posts().find((request) => request.path.endsWith('/messages/receipt')); assert.deepEqual(structuredClone(query.body), body);
  const old = await browser({ rooms: [saved.room], saved, storage, naturalContinuationSupport: true, messageReceiptsSupport: true });
  await old.click('retry-request'); await old.poll(); assert.equal(old.posts().filter((request) => request.path.endsWith('/messages')).length, 0); assert.ok(old.posts().every((request) => request.path.endsWith('/messages/receipt'))); assert.equal(old.get('message').value, '原设置原消息'); assert.match(old.get('notice').textContent, /尚不支持.*运行设置/);
  const newer = await browser({ rooms: [saved.room], saved, storage, naturalRuntimeSupport: true, naturalContinuationSupport: true, messageReceiptsSupport: true, route(request) {
    if (request.path.endsWith('/messages/receipt')) { assert.deepEqual(structuredClone(request.body), body); return { received: true, room_id: roomId, request_id: request.body.request_id }; }
  } });
  assert.equal(newer.posts().filter((request) => request.path.endsWith('/messages')).length, 0); assert.equal(newer.get('pending-request').hidden, true);
});


test('natural runtime optional fields are independent and a same-run refresh preserves local drafts over stale recorded settings', async () => {
  for (const field of ['work_mode', 'runtime_settings']) {
    const saved = withRuntimeConfiguration(withNaturalContinuation(objectiveSnapshot({ status: 'running' }))); const options = runtimeOptionsFixture();
    const ui = await browser({ rooms: [saved.room], saved, naturalRuntimeSupport: true, naturalContinuationSupport: true, executionPolicySupport: true, route(request) {
      if (request.path === '/api/runtime-options') return options;
      if (request.path.endsWith('/messages')) return saved;
    } });
    if (field === 'work_mode') await ui.click('work-mode-plan');
    else { await openRuntimeSettings(ui); selectRuntime(ui, 'runtime-permission', 'read_only'); await ui.click('runtime-settings-close'); }
    await ui.poll(); ui.type('只提交我改的设置'); await ui.submit(); const body = ui.posts()[0].body;
    assert.equal(Object.hasOwn(body, field), true); assert.equal(Object.hasOwn(body, field === 'work_mode' ? 'runtime_settings' : 'work_mode'), false);
  }
});

test('a confirmed runtime receipt is read against its original identity even when catalog, availability or edit gate has changed', async () => {
  const saved = withRuntimeConfiguration(withNaturalContinuation(objectiveSnapshot({ status: 'running' }))); const options = runtimeOptionsFixture(); const storage = new Map();
  const ui = await browser({ rooms: [saved.room], saved, storage, naturalRuntimeSupport: true, naturalContinuationSupport: true, executionPolicySupport: true, messageReceiptsSupport: true, route(request) {
    if (request.path === '/api/runtime-options') return options;
    if (request.path.endsWith('/messages') || request.path.endsWith('/messages/receipt')) throw new Error('fictional receipt loss');
  } });
  await openRuntimeSettings(ui); selectRuntime(ui, 'runtime-model-claude', 'opus'); selectRuntime(ui, 'runtime-effort-claude', 'high'); await ui.click('runtime-settings-close'); ui.type('不要按新目录改写这条消息'); await ui.submit();
  const body = structuredClone(ui.posts().find((request) => request.path.endsWith('/messages')).body);
  saved.runtime_configuration.can_edit = false; saved.runtime_configuration.reason = 'agent_quota_exhausted';
  const reloaded = await browser({ rooms: [saved.room], saved, storage, naturalRuntimeSupport: false, naturalContinuationSupport: true, messageReceiptsSupport: true, route(request) {
    if (request.path.endsWith('/messages/receipt')) { assert.deepEqual(structuredClone(request.body), body); return { received: true, room_id: roomId, request_id: body.request_id }; }
    if (request.path === '/api/runtime-options') return { ...options, partners: { claude: { supported: false, models: [] }, codex: { supported: false, models: [] } } };
  } });
  assert.equal(reloaded.posts().filter((request) => request.path.endsWith('/messages')).length, 0); assert.equal(reloaded.get('pending-request').hidden, true);
});

test('missing runtime configuration cannot unlock controls and Stop remains independent of an in-flight settings message', async () => {
  const saved = withRuntimeConfiguration(withNaturalContinuation(objectiveSnapshot({ status: 'running' }))); const options = runtimeOptionsFixture();
  const missing = structuredClone(saved); delete missing.runtime_configuration;
  const absent = await browser({ rooms: [missing.room], saved: missing, naturalRuntimeSupport: true, naturalContinuationSupport: true, executionPolicySupport: true, route(request) { if (request.path === '/api/runtime-options') return options; } });
  await openRuntimeSettings(absent); assert.equal(absent.get('runtime-permission').disabled, true); assert.equal(absent.get('work-mode-plan').disabled, true);
  const response = deferred(); const stopped = structuredClone(saved); stopped.objective.status = 'stopped'; stopped.cancellation_confirmed = true;
  const ui = await browser({ rooms: [saved.room], saved, naturalRuntimeSupport: true, naturalContinuationSupport: true, executionPolicySupport: true, messageReceiptsSupport: true, route(request) {
    if (request.path === '/api/runtime-options') return options;
    if (request.path.endsWith('/messages')) return response.promise;
    if (request.path.endsWith('/cancel')) return stopped;
  } });
  await openRuntimeSettings(ui); selectRuntime(ui, 'runtime-permission', 'read_only'); await ui.click('runtime-settings-close'); ui.type('设置与消息一起发送'); await ui.submit(); const original = structuredClone(ui.posts()[0].body);
  assert.equal(ui.get('cancel-room').disabled, false); await ui.click('cancel-room'); assert.equal(ui.posts()[1].path, `/api/rooms/${roomId}/cancel`);
  response.resolve(saved); await settle(); assert.deepEqual(structuredClone(ui.posts()[0].body), original); assert.match(ui.get('room-status').textContent, /已停止/);
});


test('an authoritative unreceived settings message rejected on redelivery keeps editable drafts and does not loop', async () => {
  const saved = withRuntimeConfiguration(withNaturalContinuation(objectiveSnapshot({ status: 'running' }))); const options = runtimeOptionsFixture(); let sends = 0;
  const ui = await browser({ rooms: [saved.room], saved, naturalRuntimeSupport: true, naturalContinuationSupport: true, executionPolicySupport: true, messageReceiptsSupport: true, route(request) {
    if (request.path === '/api/runtime-options') return options;
    if (request.path.endsWith('/messages')) { if (++sends === 1) throw new Error('fictional lost receipt'); return { error: 'runtime_model_unsupported' }; }
    if (request.path.endsWith('/messages/receipt')) return { error: 'message_request_not_found' };
  } });
  await openRuntimeSettings(ui); selectRuntime(ui, 'runtime-permission', 'read_only'); await ui.click('runtime-settings-close'); ui.type('保留被明确拒绝的设置草稿'); await ui.submit();
  const bodies = ui.posts().filter((request) => request.path.endsWith('/messages')).map((request) => request.body); assert.equal(bodies.length, 2); assert.deepEqual(structuredClone(bodies[0]), structuredClone(bodies[1]));
  assert.equal(ui.get('pending-request').hidden, true); assert.equal(ui.get('message').value, '保留被明确拒绝的设置草稿'); assert.equal(ui.get('runtime-shortcuts-caption').textContent, '发消息后生效'); assert.match(ui.get('action-error').textContent, /这个模型不在工具返回的可选列表/);
  await ui.poll(); await ui.poll(); assert.equal(ui.posts().filter((request) => request.path.endsWith('/messages')).length, 2);
  await openRuntimeSettings(ui); assert.equal(ui.get('runtime-permission').disabled, false); assert.equal(ui.get('runtime-permission').value, 'read_only');
});

test('late detached 200 or 409 cannot clear a newer same-chat model, mode and message draft after receipt confirmation', async () => {
  for (const status of [200, 409]) for (const includedSettings of [false, true]) {
    const saved = withRuntimeConfiguration(withNaturalContinuation(objectiveSnapshot({ status: 'running' })));
    const options = runtimeOptionsFixture(); const originalResponse = deferred();
    const ui = await browser({ rooms: [saved.room], saved, naturalRuntimeSupport: true, naturalContinuationSupport: true, executionPolicySupport: true, messageReceiptsSupport: true, route(request) {
      if (request.path === '/api/runtime-options') return options;
      if (request.path.endsWith('/messages')) return originalResponse.promise;
      if (request.path.endsWith('/messages/receipt')) return { received: true, room_id: roomId, request_id: request.body.request_id };
    } });
    if (includedSettings) { await openRuntimeSettings(ui); selectRuntime(ui, 'runtime-permission', 'read_only'); await ui.click('runtime-settings-close'); await ui.click('work-mode-plan'); }
    ui.type('旧正文'); await ui.submit(); const original = structuredClone(ui.posts()[0].body);
    await ui.click('new-room'); ui.expireDetachedReceipts(); await ui.poll();
    ui.get('rooms').children[0].fire('click'); await settle();
    ui.type('新正文不能被旧回包清掉'); await openRuntimeSettings(ui); selectRuntime(ui, 'runtime-model-claude', 'opus'); await ui.click('runtime-settings-close'); await ui.click('work-mode-plan');
    originalResponse.resolve(status === 200 ? saved : { error: 'runtime_model_unsupported' }); await settle(); await ui.poll();
    assert.equal(ui.get('message').value, '新正文不能被旧回包清掉'); assert.equal(ui.get('work-mode-plan').getAttribute('aria-pressed'), 'true');
    await openRuntimeSettings(ui); assert.equal(ui.get('runtime-model-claude').value, 'opus'); assert.equal(ui.get('runtime-shortcuts-caption').textContent, '发消息后生效');
    assert.deepEqual(structuredClone(ui.posts().find((request) => request.path.endsWith('/messages')).body), original);
  }
});

test('natural runtime same-goal questions use settings wording and read-only discussion follows actual frozen work', async () => {
  const saved = withRuntimeConfiguration(withNaturalContinuation(objectiveSnapshot({ status: 'needs_user' }))); saved.current_run.can_start_suggested_goal = true;
  const options = runtimeOptionsFixture();
  const ui = await browser({ rooms: [saved.room], saved, naturalRuntimeSupport: true, naturalContinuationSupport: true, executionPolicySupport: true, route(request) { if (request.path === '/api/runtime-options') return options; } });
  assert.equal(ui.get('work-mode-label').textContent, '后续工作'); await openRuntimeSettings(ui); assert.equal(ui.get('runtime-settings-title').textContent, '后续工作权限'); assert.doesNotMatch(ui.get('runtime-settings-note').textContent, /新目标/); await ui.click('runtime-settings-close');
  saved.objective.status = 'running'; saved.room.work_mode = 'plan'; saved.room.runtime_settings.effective_permission = 'read_only';
  saved.jobs = [{ id: 'discussion-job', provider: 'claude', status: 'working', kind: 'team_user', run_id: saved.current_run.id }];
  saved.events.push({ seq: 40, kind: 'job_claimed', sender: 'claude', job_id: 'discussion-job', run_id: saved.current_run.id, work_mode: 'plan', runtime_settings: { effective_permission: 'read_only' }, created_at: 40 },
    { seq: 41, kind: 'native_readonly_discussion', sender: 'claude', job_id: 'discussion-job', run_id: saved.current_run.id, created_at: 41 });
  await ui.poll(); assert.match(ui.get('partner-activity-claude').textContent, /只读讨论/); assert.doesNotMatch(ui.get('partner-activity-claude').textContent, /核对上次进度/);
  saved.objective.status = 'complete'; saved.jobs[0].status = 'completed'; await ui.poll();
  assert.equal(ui.get('room-status').textContent, '讨论已结束'); assert.equal(ui.get('status-claude').textContent, '讨论已结束'); assert.match(ui.get('work-mode-note').textContent, /讨论已结束/);
  assert.equal(ui.posts().length, 0);
});

test('an actual idle read-only discussion keeps the running goal and earlier build reviews without implying delivery or active work', async () => {
  const saved = withRuntimeConfiguration(withNaturalContinuation(objectiveSnapshot({ status: 'running' })));
  saved.room.work_mode = 'plan'; saved.room.status = 'ready_for_user'; saved.room.runtime_settings.effective_permission = 'read_only';
  saved.current_run.work_mode = 'build'; saved.collaboration.outcome = { state: 'ready_for_user', reason: '子任务已交付并互审，等待你验收。' };
  saved.team.phase = 'ready_for_user'; saved.jobs = []; saved.members.forEach((member) => { member.status = 'idle'; });
  saved.runtime_configuration.desired.work_mode = 'plan';
  const ui = await browser({ rooms: [saved.room], saved, naturalRuntimeSupport: true, naturalContinuationSupport: true, executionPolicySupport: true });
  assert.match(ui.get('room-subtitle').textContent, /方案讨论/); assert.doesNotMatch(ui.get('room-subtitle').textContent, /直接动手/);
  assert.equal(ui.get('room-status').textContent, '方案讨论'); assert.match(ui.get('collaboration-outcome').textContent, /现在没在做事。可以接着讨论，或切到“动手”开工/);
  assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /等待你验收|正在推进目标/);
  for (const provider of ['claude', 'codex']) { assert.equal(ui.get(`status-${provider}`).textContent, '方案讨论'); assert.match(ui.get(`task-${provider}`).textContent, /现在没在做事/); }
  assert.equal(saved.objective.status, 'running');
  // B17: build mode under a still read-only permission is named by the permission, not as a plan discussion.
  saved.room.work_mode = 'build'; await ui.poll(); assert.equal(ui.get('room-status').textContent, '只读权限');
  saved.jobs = [{ id: 'held-build', provider: 'codex', status: 'working', kind: 'team_work' }];
  saved.events.push({ seq: 60, kind: 'job_claimed', job_id: 'held-build', sender: 'codex', run_id: saved.current_run.id, work_mode: 'build', runtime_settings: { effective_permission: 'workspace_write' }, created_at: 60 });
  await ui.poll(); assert.match(ui.get('room-subtitle').textContent, /直接动手/); assert.notEqual(ui.get('status-codex').textContent, '方案讨论');
  assert.equal(ui.posts().length, 0);
});

function publicProgressFixture() {
  const saved = withNaturalContinuation(objectiveSnapshot({ status: 'running' }));
  saved.team.phase = 'working'; saved.jobs = [{ id: 'active-claude', provider: 'claude', status: 'working', kind: 'team_work' }];
  saved.events.push({ seq: 70, kind: 'job_claimed', sender: 'claude', job_id: 'active-claude', run_id: saved.current_run.id, created_at: 1070 });
  return saved;
}

test('partner task preview shows only its actual current job public original and can focus the matching chat message', async () => {
  const saved = publicProgressFixture(); const run = saved.current_run.id;
  const text = '我来补生产代码，另一个伙伴帮我检查边界。\n这里保留 **公开原话**。';
  saved.events.push({ seq: 71, kind: 'message', sender: 'claude', job_id: 'active-claude', run_id: run, native_progress: true, text, created_at: 1071 },
    { seq: 72, kind: 'message', sender: 'codex', job_id: 'active-claude', run_id: run, native_progress: true, text: '错误作者不能串过来', created_at: 1072 },
    { seq: 73, kind: 'message', sender: 'claude', job_id: 'previous-job', run_id: run, native_progress: true, text: '旧调用迟到不能覆盖', created_at: 1073 },
    { seq: 74, kind: 'message', sender: 'claude', job_id: 'active-claude', run_id: 'old-run', native_progress: true, text: '旧阶段迟到不能覆盖', created_at: 1074 });
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  assert.match(ui.get('public-progress-claude').textContent, /本次进展原话/); assert.ok(ui.get('public-progress-claude').textContent.includes(text));
  assert.doesNotMatch(ui.get('public-progress-claude').textContent, /错误作者|旧调用|旧阶段/);
  await ui.click('public-progress-jump-claude'); assert.equal(ui.document.activeElement.dataset.seq, '71');
  assert.equal(saved.events.find((event) => event.seq === 71).text, text); assert.equal(ui.posts().length, 0);
});

test('new jobs cannot borrow old public progress and stopped work labels the last record as history', async () => {
  const saved = publicProgressFixture(); const run = saved.current_run.id;
  saved.events.push({ seq: 71, kind: 'message', sender: 'claude', job_id: 'active-claude', run_id: run, native_progress: true, text: '上次的原话', created_at: 1071 });
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  saved.jobs[0].status = 'completed'; saved.jobs.push({ id: 'next-claude', provider: 'claude', status: 'working', kind: 'team_work' });
  saved.events.push({ seq: 72, kind: 'job_claimed', sender: 'claude', job_id: 'next-claude', run_id: run, created_at: 1072 },
    { seq: 73, kind: 'native_tool_activity', sender: 'claude', job_id: 'next-claude', run_id: run, state: 'completed', operation: 'read_file', created_at: 1073 },
    { seq: 74, kind: 'message', sender: 'claude', job_id: 'active-claude', run_id: run, native_progress: true, text: '更晚收到的旧调用原话', created_at: 1074 });
  await ui.poll(); assert.match(ui.get('public-progress-claude').textContent, /最近返回：读取文件/); assert.doesNotMatch(ui.get('public-progress-claude').textContent, /上次|旧调用|测试通过|持续推进/);
  saved.events.push({ seq: 75, kind: 'message', sender: 'claude', job_id: 'next-claude', run_id: run, native_progress: true, text: '这次实际公开进展', created_at: 1075 });
  await ui.poll(); assert.match(ui.get('public-progress-claude').textContent, /本次进展原话.*这次实际公开进展/);
  saved.events.push({ seq: 76, kind: 'message', sender: 'claude', job_id: 'next-claude', run_id: run, text: '更晚的最终公开答复仍在主聊', created_at: 1076 });
  saved.jobs[1].status = 'completed'; saved.objective.status = 'paused'; await ui.poll();
  assert.match(ui.get('public-progress-claude').textContent, /此前进展原话（已结束）/); assert.equal(ui.get('public-progress-claude').firstChild.open, false); assert.doesNotMatch(ui.get('public-progress-claude').firstChild.firstChild.textContent, /这次实际公开进展/); assert.ok(ui.get('public-progress-claude').textContent.includes('这次实际公开进展')); assert.doesNotMatch(ui.get('public-progress-claude').textContent, /本次进展原话|更晚的最终公开答复/); assert.match(ui.get('events').textContent, /更晚的最终公开答复仍在主聊/); assert.equal(ui.posts().length, 0);
});

test('public preview clears across rooms and long original text stays inspectable with truthful truncation metadata', async () => {
  const saved = publicProgressFixture(); const second = publicProgressFixture(); second.room.id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'; second.room.goal = '另一个聊天';
  const long = '公开原文不做人工摘要。'.repeat(30);
  saved.events.push({ seq: 71, kind: 'message', sender: 'claude', job_id: 'active-claude', run_id: saved.current_run.id, native_progress: true, text: long, truncated: true, original_characters: long.length + 100, created_at: 1071 });
  const ui = await browser({ rooms: [saved.room, second.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, route(request) { if (request.path === `/api/rooms/${second.room.id}`) return second; } });
  assert.ok(ui.get('public-progress-claude').textContent.includes(long)); assert.match(ui.get('public-progress-claude').textContent, /仅显示前/);
  const oldJump = ui.get('public-progress-jump-claude'); ui.get('rooms').children.find((card) => card.title === second.room.goal).fire('click'); await settle();
  assert.match(ui.get('public-progress-claude').textContent, /还没收到这次工作的进展消息/); assert.doesNotMatch(ui.get('public-progress-claude').textContent, /公开原文不做/);
  const focus = ui.document.activeElement; oldJump.fire('click'); assert.equal(ui.document.activeElement, focus); assert.equal(ui.posts().length, 0);
});


test('baseline is omitted only when verified current objective delivery has the exact same source binding and review meaning', async () => {
  for (const variant of ['identical', 'other-room', 'other-run', 'other-revision', 'other-digest', 'other-sha', 'other-review', 'unverified', 'drift']) {
    const saved = objectiveDeliveryFixture(); const file = saved.objective.delivery.files[0];
    saved.objective.baseline = { verified: true, source_room_id: saved.room.id, run_id: file.run_id, revision: file.revision, digest: file.digest,
      files: [{ run_id: file.run_id, path: file.path, sha256: file.sha256, review_kind: file.review_kind }] };
    const baseline = saved.objective.baseline;
    if (variant === 'other-room') baseline.source_room_id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
    if (variant === 'other-run') { baseline.run_id = '66666666-6666-4666-8666-666666666666'; baseline.files[0].run_id = baseline.run_id; }
    if (variant === 'other-revision') baseline.revision += 1;
    if (variant === 'other-digest') baseline.digest = 'e'.repeat(64);
    if (variant === 'other-sha') baseline.files[0].sha256 = 'f'.repeat(64);
    if (variant === 'other-review') baseline.files[0].review_kind = 'self_check';
    if (variant === 'unverified') file.verified = false;
    if (variant === 'drift') saved.objective.delivery.digest = 'e'.repeat(64);
    const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
    const baselineButtons = descendants(ui.get('room-documents')).filter((button) => button.dataset.documentKey?.includes(':baseline:'));
    assert.equal(baselineButtons.length > 0, variant !== 'identical', variant);
    const buttons = descendants(ui.get('room-documents')).filter((button) => button.dataset.documentPath === file.path);
    assert.equal(buttons.length, variant === 'identical' ? 1 : 2, variant);
    if (variant === 'identical') assert.match(buttons[0].textContent, /伙伴看过/);
    if (variant === 'unverified' || variant === 'drift') assert.match(ui.get('document-more-items').textContent, /第一阶段成果/);
    assert.equal(ui.posts().length, 0);
  }
});

test('complete checkpoint scope never forces a new chat and reader navigation is compact report-first without resetting selection or scroll', async () => {
  const saved = withNaturalContinuation(objectiveDeliveryFixture()); saved.objective.status = 'complete';
  saved.objective.next_step = { decision: 'complete', scope: 'new_goal', summary: '本目标已完成', binding: { run_id: saved.current_run.id, revision: saved.current_run.revision, digest: saved.current_run.digest }, review_kind: 'independent_review' };
  const code = { path: 'main.py', content: 'VALUE = 2', sha256: 'f'.repeat(64), owner: 'claude' }; saved.project.files.unshift(code);
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /这个建议超出当前目标|请用“新任务”/);
  const codeButton = deliveryButton(ui, 'main.py'); assert.equal(codeButton.children.filter((node) => node.classList.contains('delivery-file-path')).length, 0);
  codeButton.fire('click'); await settle(); assert.equal(!!ui.get('reader-navigation').open, false);
  assert.match(ui.get('reader-navigation-summary').textContent, /切换成果.*份/); assert.equal(ui.get('reader-files').firstChild.dataset.documentPath, 'first.md');
  assert.equal(ui.get('reader-title').textContent, 'main.py'); ui.get('reader-body').scrollTop = 87;
  await ui.poll(); assert.equal(ui.get('reader-title').textContent, 'main.py'); assert.equal(ui.get('reader-body').scrollTop, 87);
  saved.objective.status = 'awaiting_continue'; saved.objective.next_step.decision = 'continue'; await ui.click('reader-back'); await ui.poll();
  assert.match(ui.get('collaboration-outcome').textContent, /你可以在这里明确后续要求/); assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /请用“新任务”/); assert.equal(ui.posts().length, 0);
});

function partnerOperationFixture(provider, action, requestId, { status = 'pending', error = null, targets = [] } = {}) {
  return { id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', request_id: requestId, provider, action, status, stage: status === 'completed' ? 'completed' : 'stopping', error, targets };
}
function managedPartnersFixture() {
  return catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, lifecycle_state: 'active', lifecycle_busy: true, lifecycle_can_change: false }));
}

test('partner lifecycle accepts one profile intent during pending chat and reports retention without claiming the work stopped', async () => {
  const partners = managedPartnersFixture(); const sibling = { ...partners[0], id: 'claude_other', name: '另一个Claude' }; partners.push(sibling);
  const saved = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); const messageResponse = deferred(); let operation;
  const envelope = () => ({ partners, supported_adapters: catalog.slice(0, 2), partner_lifecycle_operation: operation, partner_lifecycle_operations: operation ? [operation] : [], deleted: false });
  const ui = await browser({ agents: [...catalog.slice(0, 2), sibling], partners, rooms: [saved.room], saved, partnerLifecycleSupport: true, naturalContinuationSupport: true, executionPolicySupport: true, messageReceiptsSupport: true, route(request) {
    if (request.path === '/api/partners') return envelope();
    if (request.path.endsWith('/messages')) return messageResponse.promise;
    if (request.path === '/api/partners/claude/enabled') { operation = partnerOperationFixture('claude', 'disable', request.body.request_id, { error: 'partner_cleanup_unconfirmed' }); return envelope(); }
    if (request.path.startsWith('/api/partners/lifecycle-operations/')) return envelope();
  } });
  ui.type('仍在等回执'); await ui.submit(); await ui.click('open-partners');
  const button = descendants(ui.get('partners-list')).find((node) => node.dataset.focusKey === 'enabled' && node.textContent === '停用');
  assert.equal(button.disabled, false); button.fire('click'); await settle();
  const posted = ui.posts().find((request) => request.path === '/api/partners/claude/enabled'); assert.equal(posted.body.enabled, false); assert.match(posted.body.request_id, /^[0-9a-f-]{36}$/);
  assert.match(ui.get('partners-list').textContent, /停用尚未完成/); assert.doesNotMatch(ui.get('manager-notice').textContent, /已停用|已经停止/);
  assert.equal(partners[2].enabled, true); assert.equal(ui.posts().some((request) => request.path.includes('claude_other/')), false);
  operation = { ...operation, status: 'completed', stage: 'completed', error: null, targets: [{ room_id: roomId, state: 'waiting_receiver', reason: 'objective_paused' }] }; partners[0].enabled = false;
  await ui.poll(); assert.match(ui.get('partners-list').textContent, /伙伴已停用.*已有工作的改动已保留/);
  messageResponse.resolve(saved); await settle();
});

test('partner remove confirms configuration-only scope and does not wait for a receiver or erase historical room members', async () => {
  let partners = managedPartnersFixture(); let operation; const saved = withNaturalContinuation(objectiveSnapshot({ status: 'paused' }));
  const envelope = () => ({ partners, supported_adapters: catalog.slice(0, 2), partner_lifecycle_operation: operation, partner_lifecycle_operations: operation ? [operation] : [], deleted: operation?.status === 'completed' });
  const ui = await browser({ partners, rooms: [saved.room], saved, partnerLifecycleSupport: true, naturalContinuationSupport: true, executionPolicySupport: true, route(request) {
    if (request.path === '/api/partners') return envelope();
    if (request.path === '/api/partners/claude/lifecycle') { partners = partners.filter((partner) => partner.id !== 'claude'); operation = partnerOperationFixture('claude', 'remove', request.body.request_id, { status: 'completed', targets: [{ room_id: roomId, state: 'waiting_receiver', reason: 'no_available_partner' }] }); return envelope(); }
    if (request.path.startsWith('/api/partners/lifecycle-operations/')) return envelope();
  } });
  await ui.click('open-partners'); const remove = lifecycleButton(ui.get('partners-list'), { kind: 'partners', id: 'claude', action: 'remove' }); assert.equal(remove.disabled, false); remove.fire('click'); await settle();
  assert.match(ui.get('lifecycle-confirm-description').textContent, /伙伴配置.*已有聊天和成果保留.*保住改动/); assert.doesNotMatch(ui.get('lifecycle-confirm-description').textContent, /删除.*专用|删除.*聊天|卸载/);
  await ui.click('lifecycle-confirm-submit'); assert.equal(ui.posts().filter((request) => request.path.endsWith('/lifecycle')).length, 1);
  assert.match(ui.get('manager-notice').textContent, /伙伴配置已移除.*改动已保留/); assert.ok(ui.get('member-claude')); assert.equal(ui.get('partners-filter').children.some((option) => option.value === 'removed'), false);
});

test('partner unknown receipts use the original profile request and only authoritative journal misses permit same-key redelivery', async () => {
  for (const outcome of ['received', 'missing', 'network', 'conflict']) {
    const partners = managedPartnersFixture(); let operation; let posts = 0;
    const envelope = () => ({ partners, supported_adapters: catalog.slice(0, 2), partner_lifecycle_operation: operation, partner_lifecycle_operations: operation ? [operation] : [], deleted: false });
    const ui = await browser({ partners, partnerLifecycleSupport: true, route(request) {
      if (request.path === '/api/partners') return envelope();
      if (request.path === '/api/partners/claude/lifecycle') { operation = partnerOperationFixture('claude', 'archive', request.body.request_id, { status: 'completed' }); if (++posts === 1) throw new Error('lost reply'); partners[0].lifecycle_state = 'archived'; return envelope(); }
      if (request.path.startsWith('/api/partners/lifecycle-operations/')) {
        if (outcome === 'missing') return { error: 'partner_lifecycle_operation_not_found' };
        if (outcome === 'network') throw new Error('journal offline');
        if (outcome === 'conflict') return { error: 'request_conflict' };
        partners[0].lifecycle_state = 'archived'; return envelope();
      }
    } });
    await ui.click('open-partners'); lifecycleButton(ui.get('partners-list'), { kind: 'partners', id: 'claude', action: 'archive' }).fire('click'); await settle();
    const sent = ui.posts().filter((request) => request.path === '/api/partners/claude/lifecycle');
    assert.equal(sent.length, outcome === 'missing' ? 2 : 1, outcome); if (sent.length === 2) assert.deepEqual(structuredClone(sent[0].body), structuredClone(sent[1].body));
    const lookup = ui.requests.find((request) => request.path.startsWith('/api/partners/lifecycle-operations/'));
    assert.equal(lookup.method, 'GET'); assert.ok(lookup.path.endsWith(sent[0].body.request_id));
    if (['network', 'conflict'].includes(outcome)) assert.doesNotMatch(ui.get('manager-notice').textContent, /伙伴已归档/);
    if (outcome === 'conflict') { await ui.poll(); assert.equal(ui.posts().filter((request) => request.path.endsWith('/lifecycle')).length, 1); }
  }
});

test('partner journal survives reload without reinterpreting the original intent and legacy removed profiles need explicit cleanup', async () => {
  const storage = new Map(); const partners = managedPartnersFixture(); partners.push({ ...partners[1], id: 'old_removed', name: '旧移除配置', lifecycle_state: 'removed' }); let requestId;
  const first = await browser({ partners, storage, partnerLifecycleSupport: true, route(request) {
    if (request.path === '/api/partners/claude/enabled') { requestId = request.body.request_id; throw new Error('lost request'); }
    if (request.path.startsWith('/api/partners/lifecycle-operations/')) throw new Error('still offline');
  } });
  await first.click('open-partners'); descendants(first.get('partners-list')).find((node) => node.dataset.focusKey === 'enabled').fire('click'); await settle();
  assert.match(first.get('partners-list').textContent, /整理旧移除配置（1）/); assert.equal(first.get('partners-filter').children.some((option) => option.value === 'removed'), false);
  const second = await browser({ partners, storage, partnerLifecycleSupport: true, route(request) {
    if (request.path.startsWith('/api/partners/lifecycle-operations/')) return { partners, supported_adapters: catalog.slice(0, 2), deleted: false, partner_lifecycle_operation: partnerOperationFixture('claude', 'disable', requestId, { status: 'completed' }) };
  } });
  await settle();
  assert.equal(second.posts().length, 0); assert.ok(second.requests.some((request) => request.path.endsWith(`/lifecycle-operations/${requestId}`)));
  const cached = JSON.parse(storage.get('lac-room-lifecycle-v1')).partner_operations.find((operation) => operation.request_id === requestId);
  assert.deepEqual(cached.body, { enabled: false, request_id: requestId }); assert.equal(cached.status, 'completed');
});

test('held old partner journal cannot overwrite a newer intent, revive removed configuration or regress a completed receipt at the same clock time', async () => {
  for (const nextAction of ['enable', 'remove']) {
    let partners = managedPartnersFixture(); let oldOperation; let newOperation; const oldReply = deferred(); let held = false;
    const list = () => ({ partners, supported_adapters: catalog.slice(0, 2), partner_lifecycle_operations: [oldOperation, newOperation].filter(Boolean) });
    const ui = await browser({ partners, partnerLifecycleSupport: true, now: () => 1000000, route(request) {
      if (request.path === '/api/partners') return list();
      if (request.path === '/api/partners/claude/enabled') {
        partners[0].enabled = request.body.enabled;
        if (!oldOperation) { oldOperation = partnerOperationFixture('claude', 'disable', request.body.request_id, { status: 'completed', targets: [{ room_id: roomId, state: 'waiting_receiver', reason: 'objective_paused' }] }); return { ...list(), partner_lifecycle_operation: oldOperation, deleted: false }; }
        newOperation = partnerOperationFixture('claude', 'enable', request.body.request_id, { status: 'completed' }); return { ...list(), partner_lifecycle_operation: newOperation, deleted: false };
      }
      if (request.path === '/api/partners/claude/lifecycle') {
        partners = partners.filter((partner) => partner.id !== 'claude'); newOperation = partnerOperationFixture('claude', 'remove', request.body.request_id, { status: 'completed' }); return { ...list(), partner_lifecycle_operation: newOperation, deleted: true };
      }
      if (request.path.includes(`/lifecycle-operations/${oldOperation?.request_id}`)) { held = true; return oldReply.promise; }
    } });
    await ui.click('open-partners'); descendants(ui.get('partners-list')).find((node) => node.dataset.focusKey === 'enabled').fire('click'); await settle();
    const stale = { ...structuredClone(list()), partner_lifecycle_operation: { ...structuredClone(oldOperation), status: 'pending', stage: 'stopping' }, deleted: false };
    await ui.poll(); assert.equal(held, true);
    if (nextAction === 'enable') { descendants(ui.get('partners-list')).find((node) => node.dataset.focusKey === 'enabled').fire('click'); await settle(); }
    else { lifecycleButton(ui.get('partners-list'), { kind: 'partners', id: 'claude', action: 'remove' }).fire('click'); await settle(); await ui.click('lifecycle-confirm-submit'); }
    oldReply.resolve(stale); await settle();
    const cached = JSON.parse(ui.storage.get('lac-room-lifecycle-v1')).partner_operations;
    assert.equal(cached.find((operation) => operation.request_id === oldOperation.request_id).status, 'completed');
    if (nextAction === 'enable') {
      const current = descendants(ui.get('partners-list')).find((node) => node.dataset.focusKey === 'enabled'); assert.equal(current.textContent, '停用'); assert.equal(current.disabled, false);
      assert.match(ui.get('manager-notice').textContent, /伙伴已启用/); assert.doesNotMatch(ui.get('partners-list').textContent, /停用尚未完成/);
    } else {
      assert.equal(ui.get('partners-list').children.some((node) => node.dataset.partner === 'claude'), false);
      assert.match(ui.get('manager-notice').textContent, /配置已移除/);
    }
    assert.ok(ui.get('partners-list').children.some((node) => node.dataset.partner === 'codex'));
  }
});


test('explicit management filters and tabs return their view to the top while ordinary refresh preserves scroll', async () => {
  const partners = managedPartnersFixture(); partners[1].lifecycle_state = 'archived';
  const ui = await browser({ partners, partnerLifecycleSupport: true });
  await ui.click('open-partners'); ui.get('partners-view').scrollTop = 580;
  ui.get('partners-filter').value = 'archived'; ui.get('partners-filter').fire('change'); await settle();
  assert.equal(ui.get('partners-view').scrollTop, 0); assert.match(ui.get('partners-list').textContent, /Codex/);
  ui.get('partners-view').scrollTop = 230; await ui.poll(); assert.equal(ui.get('partners-view').scrollTop, 230);
  ui.get('projects-view').scrollTop = 490; await ui.click('manager-projects-tab'); assert.equal(ui.get('projects-view').scrollTop, 0);
  await ui.click('manager-partners-tab'); assert.equal(ui.get('partners-view').scrollTop, 0); assert.equal(ui.posts().length, 0);
});

test('exact configuration handoff projection separates historical source uncertainty from current typed read-only work without changing facts', async () => {
  const saved = publicProgressFixture(); const run = saved.current_run.id;
  const source = saved.members.find((member) => member.id === 'claude'); const receiver = saved.members.find((member) => member.id === 'codex');
  Object.assign(source, { identity_uncertain: true, status: 'unknown', configuration_dispatchable: false, configuration_dispatch_reason: 'partner_removed', configuration_handoff_state: 'done', configuration_handoff_completed: true });
  Object.assign(receiver, { status: 'working', configuration_dispatchable: true, configuration_dispatch_reason: null, configuration_handoff_state: null, configuration_handoff_completed: false });
  saved.jobs[0].status = 'failed'; saved.jobs[0].error = 'protocol_turn_incomplete';
  saved.jobs.push({ id: 'handoff-read', provider: 'codex', status: 'working', kind: 'team_work', partner_handoff_inspection: true });
  saved.events.push({ seq: 71, kind: 'job_claimed', sender: 'codex', job_id: 'handoff-read', run_id: run, work_mode: 'build', runtime_settings: { permission: 'full_access' }, created_at: 1071 },
    { seq: 72, kind: 'message', sender: 'codex', job_id: 'handoff-read', run_id: run, native_progress: true, text: '我先只读核对上次保留的改动。', created_at: 1072 });
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, partnerLifecycleSupport: true });
  assert.equal(ui.get('status-claude').textContent, '已移除配置'); assert.match(ui.get('task-claude').textContent, /工作已交接/);
  assert.equal(ui.get('configuration-history-claude').hidden, false); assert.match(ui.get('configuration-history-claude').textContent, /此前存在未确认/);
  assert.equal(source.identity_uncertain, true); assert.equal(saved.jobs[0].error, 'protocol_turn_incomplete');
  assert.equal(ui.get('status-codex').textContent, '只读核对接手进度'); assert.doesNotMatch(ui.get('task-codex').textContent, /开发/); assert.match(ui.get('public-progress-codex').textContent, /我先只读核对/);
  source.configuration_handoff_completed = false; source.configuration_handoff_state = 'pending'; source.configuration_dispatch_reason = 'partner_lifecycle_pending'; await ui.poll();
  assert.equal(ui.get('status-claude').textContent, '结果待核对'); assert.doesNotMatch(ui.get('task-claude').textContent, /已交接|已清理|已停止/); assert.equal(ui.get('configuration-history-claude').hidden, true);
  receiver.identity_uncertain = true; saved.jobs[1].error = 'session_identity_changed'; await ui.poll(); assert.equal(ui.get('status-codex').textContent, '身份待核查');
  receiver.identity_uncertain = false; saved.objective.status = 'complete'; await ui.poll(); assert.equal(ui.get('status-codex').textContent, '只读核对接手进度');
  assert.equal(ui.posts().length, 0);
});

test('configuration completion never hides an actual in-flight job and pending configuration does not claim clean stop', async () => {
  const saved = publicProgressFixture(); const member = saved.members[0];
  Object.assign(member, { configuration_dispatchable: false, configuration_dispatch_reason: 'partner_disabled', configuration_handoff_state: 'done', configuration_handoff_completed: true });
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, partnerLifecycleSupport: true });
  assert.equal(ui.get('status-claude').textContent, '正在处理'); assert.doesNotMatch(ui.get('task-claude').textContent, /工作已交接/);
  saved.jobs[0].status = 'completed'; member.configuration_handoff_completed = false; member.configuration_dispatch_reason = 'partner_lifecycle_pending'; member.configuration_handoff_state = 'retained'; await ui.poll();
  assert.equal(ui.get('status-claude').textContent, '配置处理中'); assert.match(ui.get('task-claude').textContent, /停止与接手尚未确认/); assert.doesNotMatch(ui.get('task-claude').textContent, /已交接|已清理/);
  assert.equal(ui.posts().length, 0);
});

test('configuration-disabled source runtime controls are historical-only while receiver settings and plan still submit atomically', async () => {
  const saved = withRuntimeConfiguration(withNaturalContinuation(objectiveSnapshot({ status: 'running' }))); saved.team.phase = 'working';
  Object.assign(saved.members[0], { configuration_dispatchable: false, configuration_dispatch_reason: 'partner_removed', configuration_handoff_completed: true, configuration_handoff_state: 'done' });
  Object.assign(saved.members[1], { configuration_dispatchable: true, configuration_dispatch_reason: null });
  const retained = { model: 'old-removed-model', effort: 'old-effort' };
  saved.room.runtime_settings.members.claude = structuredClone(retained); saved.current_run.runtime_settings.members.claude = structuredClone(retained); saved.runtime_configuration.desired.runtime_settings.members.claude = structuredClone(retained);
  const options = runtimeOptionsFixture();
  const ui = await browser({ rooms: [saved.room], saved, partnerLifecycleSupport: true, naturalRuntimeSupport: true, naturalContinuationSupport: true, executionPolicySupport: true, route(request) {
    if (request.path === '/api/runtime-options') return options;
    if (request.path.endsWith('/runtime/refresh')) return options;
    if (request.path.endsWith('/messages')) { saved.runtime_configuration.desired = { work_mode: request.body.work_mode, runtime_settings: request.body.runtime_settings }; return saved; }
  } });
  assert.match(ui.get('runtime-shortcut-claude').textContent, /old-removed-model.*仅查看/);
  await ui.click('runtime-shortcut-claude'); assert.match(ui.get('runtime-settings-note').textContent, /仅供查看/);
  assert.equal(ui.get('runtime-model-claude').disabled, true); assert.equal(ui.get('runtime-effort-claude').disabled, true); assert.equal(ui.get('runtime-refresh-claude').disabled, true);
  assert.equal(ui.get('runtime-model-claude').value, retained.model); selectRuntime(ui, 'runtime-model-claude', 'opus');
  assert.equal(ui.posts().some((request) => request.path === '/api/partners/claude/runtime/refresh'), false);
  await ui.click('runtime-settings-close'); await ui.click('runtime-shortcut-codex'); assert.equal(ui.get('runtime-model-codex').disabled, false);
  selectRuntime(ui, 'runtime-model-codex', 'observed-codex-model'); selectRuntime(ui, 'runtime-effort-codex', 'xhigh'); await ui.click('runtime-settings-close'); await ui.click('work-mode-plan');
  assert.equal(ui.get('composer-usage-claude').disabled, false); ui.type('先按新设置讨论'); await ui.submit();
  const body = ui.posts().find((request) => request.path.endsWith('/messages')).body;
  assert.equal(body.work_mode, 'plan'); assert.deepEqual(structuredClone(body.runtime_settings.members.claude), retained); assert.equal(body.runtime_settings.members.codex.effort, 'xhigh');
});


test('archived partner view keeps only direct archive cards while returning current restores discovery and adding controls', async () => {
  const partners = managedPartnersFixture(); partners.push({ ...partners[0], id: 'claude_a', name: '归档独立会话', lifecycle_state: 'archived' });
  const directory = { partners, supported_adapters: catalog.slice(0, 2), discovery: { state: 'completed', scope: 'supported_cli_locations', tools: [{ adapter: 'claude', name: 'Claude Code', installed: true }] } };
  const ui = await browser({ partners, partnerLifecycleSupport: true, route(request) { if (request.path === '/api/partners') return directory; } });
  await ui.click('open-partners'); assert.equal(ui.get('partner-discovery').hidden, false); assert.equal(ui.get('partners-intro').hidden, false); assert.equal(ui.get('partner-add-details').hidden, false);
  ui.get('partners-filter').value = 'archived'; ui.get('partners-filter').fire('change'); await settle();
  assert.equal(ui.get('partner-discovery').hidden, true); assert.equal(ui.get('partners-intro').hidden, true); assert.equal(ui.get('partner-add-details').hidden, true); assert.equal(ui.get('advanced-sessions').hidden, true);
  assert.match(ui.get('partners-list').textContent, /归档独立会话/); assert.equal(ui.get('additional-partners-list').children.length, 0);
  assert.doesNotMatch(ui.get('partners-list').textContent, /本机未检测|连接检查|同一工具可以/);
  assert.equal(lifecycleButton(ui.get('partners-list'), { kind: 'partners', id: 'claude_a', action: 'restore' }).disabled, false);
  assert.equal(lifecycleButton(ui.get('partners-list'), { kind: 'partners', id: 'claude_a', action: 'remove' }).disabled, false);
  await ui.poll(); assert.equal(ui.get('partner-discovery').hidden, true);
  ui.get('partners-filter').value = 'active'; ui.get('partners-filter').fire('change'); await settle();
  assert.equal(ui.get('partner-discovery').hidden, false); assert.equal(ui.get('partners-intro').hidden, false); assert.equal(ui.get('partner-add-details').hidden, false); assert.equal(ui.get('partners-view').scrollTop, 0); assert.equal(ui.posts().length, 0);
});


test('outbox commit failure preserves the same editable request and never dispatches before durable completion', async () => {
  const failure = { commit: true }; const storage = new Map();
  const ui = await browser({ storage, idbFailure: failure, route(request) {
    if (request.path === '/api/rooms' && request.method === 'POST') throw new Error('lost response');
  } });
  ui.type('不能丢掉的正文'); await ui.submit();
  assert.equal(ui.posts().length, 0);
  assert.equal(ui.get('message').value, '不能丢掉的正文');
  assert.match(ui.get('action-error').textContent, /尚未发送.*无法可靠保存/);
  failure.commit = false; await ui.click('retry-request');
  assert.equal(ui.posts().length, 1);
  assert.deepEqual(savedOutbox(storage).request.body, ui.posts()[0].body);
  assert.equal(Object.hasOwn(JSON.parse(storage.get('lac-rooms-pending-v1')), 'request'), false, 'sessionStorage holds only a small request selection hint');
  const reloaded = await browser({ storage });
  assert.equal(reloaded.get('message').value, '不能丢掉的正文'); assert.equal(reloaded.posts().length, 0);
});

test('legacy pending migration retains the exact raw record until the IndexedDB transaction commits', async () => {
  const storage = new Map(); const first = await browser({ storage, route(request) {
    if (request.path === '/api/rooms' && request.method === 'POST') throw new Error('lost response');
  } });
  first.type('旧版未确认内容'); await first.submit();
  const old = structuredClone(savedOutbox(storage));
  outboxDatabases.get(storage).clear();
  const raw = JSON.stringify({ scope: old.scope, request: old.request }); storage.set('lac-rooms-pending-v1', raw);
  const failed = await browser({ storage, idbFailure: { commit: true } });
  assert.equal(storage.get('lac-rooms-pending-v1'), raw); assert.equal(failed.posts().length, 0);
  assert.equal(failed.get('message').value, '旧版未确认内容');
  const migrated = await browser({ storage });
  assert.equal(Object.hasOwn(JSON.parse(storage.get('lac-rooms-pending-v1')), 'request'), false);
  assert.deepEqual(savedOutbox(storage).request.body, old.request.body);
  assert.equal(migrated.posts().length, 0);
});

test('two windows retain independent outbox bodies and acknowledging one never clears the other key', async () => {
  const storage = new Map();
  const one = await browser({ storage, route(request) { if (request.path === '/api/rooms' && request.method === 'POST') throw new Error('lost one'); } });
  one.type('窗口一未决正文'); await one.submit();
  const first = structuredClone(one.posts()[0].body);
  // Separate sessionStorage maps share one IndexedDB origin.
  const fresh = new Map(storage); outboxDatabases.set(fresh, outboxDatabases.get(storage));
  const otherRequest = { kind: 'create', body: { ...first, request_id: '00000000-0000-4000-8000-999999999999', goal: '窗口二已确认正文' } };
  // A fresh tab is modeled with a different session hint but the same IDB origin.
  fresh.set('lac-rooms-pending-v1', JSON.stringify({ scope: savedOutbox(storage).scope, request: otherRequest }));
  const active = await browser({ storage: fresh, route(request) { if (request.path === '/api/rooms' && request.method === 'POST') return snapshot(); } });
  await active.click('retry-request');
  const rows = [...outboxDatabases.get(storage).values()].filter((row) => row.request);
  assert.ok(rows.some((row) => row.request.body.request_id === first.request_id));
  assert.ok(!rows.some((row) => row.request.body.request_id === otherRequest.body.request_id));
  assert.equal(rows.find((row) => row.request.body.request_id === first.request_id).request.body.goal, '窗口一未决正文');
});

test('an empty durable scope index never hides a different legacy raw pending request', async () => {
  const storage = new Map(); const ui = await browser({ storage, route(request) { if (request.path === '/api/rooms' && request.method === 'POST') throw new Error('lost reply'); } });
  ui.type('旧窗口尚未提交确认的完整原文'); await ui.submit();
  const old = savedOutbox(storage); const original = structuredClone(old.request.body);
  outboxDatabases.get(storage).clear(); outboxDatabases.get(storage).set(old.scope, { scope: old.scope, request_ids: [], deleted_rooms: [] });
  storage.set('lac-rooms-pending-v1', JSON.stringify({ scope: old.scope, request: old.request }));
  const restored = await browser({ storage });
  assert.equal(restored.get('message').value, original.goal);
  assert.deepEqual(savedOutbox(storage).request.body, original);
  assert.equal(restored.posts().length, 0);
});

function pagedFixture(saved, events, liveJobs = []) {
  const value = structuredClone(saved);
  value.projection = 'room_state_v1'; value.through_seq = events.at(-1)?.seq || 0; value.event_count = events.length; value.events = [];
  value.room.goal_preview = value.room.goal; delete value.room.goal;
  value.live = { jobs: liveJobs }; return value;
}
function eventPageRoute(request, events) {
  if (!request.path.includes('/events?')) return undefined;
  const query = new URL(`http://fixture${request.path}`).searchParams;
  const through = Number(query.get('through_seq')); const before = query.has('before_seq') ? Number(query.get('before_seq')) : Infinity;
  const after = query.has('after_seq') ? Number(query.get('after_seq')) : -1;
  const available = events.filter((event) => event.seq <= through);
  const candidates = available.filter((event) => event.seq < before && event.seq > after);
  const page = query.has('after_seq') ? candidates.slice(0, 100) : candidates.slice(-100);
  return { room_id: roomId, through_seq: through, events: page, first_seq: page[0]?.seq ?? null, last_seq: page.at(-1)?.seq ?? null,
    has_before: page.length > 0 && available.some((event) => event.seq < page[0].seq), has_after: page.length > 0 && available.some((event) => event.seq > page.at(-1).seq), oversize: false };
}

test('paged rooms load a tail and prepend older original messages without moving draft, selection or current claims', async () => {
  const source = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); source.team.phase = 'working';
  const events = Array.from({ length: 130 }, (_, index) => ({ seq: index + 1, kind: 'message', sender: 'claude', recipient: 'all', text: `原始讨论${index + 1}`, created_at: 1000 + index, run_id: source.current_run.id }));
  const active = { id: jobId, provider: 'codex', status: 'working', kind: 'team_work', run_id: source.current_run.id, work_mode: 'build', runtime_settings: { effective_permission: 'workspace_write' }, claimed_at: 1000, latest_native_activity: { kind: 'native_tool_activity', sender: 'codex', job_id: jobId, state: 'running', operation: 'read_file', created_at: 1100 } };
  const saved = pagedFixture(source, events, [active]);
  const ui = await browser({ rooms: [source.room], saved, pagingSupport: true, naturalContinuationSupport: true, executionPolicySupport: true, selection: '保留所选文字', route(request) {
    if (request.path.endsWith('/state')) return saved;
    return eventPageRoute(request, events);
  } });
  assert.equal(ui.requests.some((request) => request.path === `/api/rooms/${roomId}`), false);
  assert.equal(ui.get('events').children.filter((node) => node.dataset.seq).length, 100);
  assert.match(ui.get('partner-activity-codex').textContent, /正在读文件/);
  const originalNode = ui.get('events').children.find((node) => node.dataset.seq === '31');
  ui.type('保留未发送草稿'); ui.get('timeline').scrollTop = 25;
  Object.defineProperty(ui.get('timeline'), 'scrollHeight', { get: () => ui.get('events').children.length * 50, configurable: true });
  ui.get('timeline').fire('scroll'); await settle();
  assert.equal(ui.get('events').children.length, 130); assert.equal(ui.get('events').children.find((node) => node.dataset.seq === '31'), originalNode);
  assert.equal(ui.get('timeline').scrollTop, 1525); assert.equal(ui.get('message').value, '保留未发送草稿');
  assert.equal(ui.posts().length, 0); assert.equal(ui.get('history-status').hidden, true);
  events.push({ ...events.at(-1), seq: 131, text: '新的真实回复' }); saved.through_seq = 131;
  await ui.poll(); assert.equal(ui.get('events').children.length, 131);
  assert.match(ui.get('events').textContent, /原始讨论1.*新的真实回复/s); assert.equal(ui.get('message').value, '保留未发送草稿');
  assert.ok(ui.requests.some((request) => request.path.includes('after_seq=130')));
});

test('a paged room shows command progress from the projected job when its event is not in the page', async () => {
  // Catches the card reading only loaded events, which would lose the line once it scrolls out of the tail.
  const source = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); source.team.phase = 'working';
  const now = Date.now() / 1000;
  const active = { id: jobId, provider: 'codex', status: 'working', kind: 'team_work', run_id: source.current_run.id, work_mode: 'build', runtime_settings: { effective_permission: 'workspace_write' }, claimed_at: now - 30,
    latest_progress_status: { kind: 'native_progress_status', sender: 'codex', job_id: jobId, text: '96/182 done, 0 failed', created_at: now - 5 } };
  const saved = pagedFixture(source, [], [active]);
  const ui = await browser({ rooms: [source.room], saved, pagingSupport: true, naturalContinuationSupport: true, executionPolicySupport: true, route(request) {
    if (request.path.endsWith('/state')) return saved;
    return eventPageRoute(request, []);
  } });
  assert.equal(ui.get('partner-progress-status-codex').textContent, '进度：96/182 done, 0 failed · 刚刚');
});

test('long UTF8 messages commit their entire escaped body before a small ACK and survive a failed state reload without replay', async () => {
  const source = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); source.team.phase = 'ready_for_user';
  const saved = pagedFixture(source, []); let sent = false;
  const storage = new Map(); const ui = await browser({ storage, rooms: [source.room], saved, pagingSupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.endsWith('/state')) { if (sent) throw new Error('state temporarily offline'); return saved; }
    if (request.path.includes('/events?')) return eventPageRoute(request, []);
    if (request.path.endsWith('/messages?response=ack')) { sent = true; return { acknowledged: true, room_id: roomId, request_id: request.body.request_id, state_url: `/api/rooms/${roomId}/state` }; }
  } });
  const text = `保留原文\n${'\u0001'.repeat(1048500)}`;
  ui.type(text); await ui.submit();
  const post = ui.posts().find((request) => request.path.endsWith('/messages?response=ack'));
  assert.ok(post); assert.equal(post.body.text, text);
  assert.ok(Buffer.byteLength(JSON.stringify(post.body)) > 6 * 1024 * 1024 - 1000);
  assert.match(ui.get('action-error').textContent, /已发送.*无需再次发送/);
  assert.equal(savedOutbox(storage).request, null); assert.equal(ui.get('pending-request').hidden, true);
  await ui.poll(); assert.equal(ui.posts().filter((request) => request.path.includes('/messages')).length, 1);
  assert.equal(ui.get('character-count').hidden, true); assert.doesNotMatch(html, /maxlength="3000"|0 \/ 3000/);
});

test('long messages use raw UTF8 capacity and never truncate an oversize draft or silently downgrade through an old host', async () => {
  const ui = await browser({ pagingSupport: true });
  const text = '汉'.repeat(349526); ui.type(text); await ui.submit();
  assert.equal(ui.posts().length, 0); assert.equal(ui.get('message').value, text); assert.match(ui.get('notice').textContent, /传输容量.*未发送/);
  const old = await browser(); const original = 'a'.repeat(4000); old.type(original); await old.submit();
  assert.equal(old.posts().length, 0); assert.equal(old.get('message').value, original); assert.match(old.get('notice').textContent, /尚未支持长消息.*原文已保留/);
});

test('durable outbox works without sessionStorage and a changed connection only reads the original key', async () => {
  class RestrictedStorage extends Map { get() { throw new Error('SS denied'); } set() { throw new Error('SS quota'); } delete() { throw new Error('SS denied'); } }
  const storage = new RestrictedStorage();
  const ui = await browser({ storage, route(request) { if (request.path === '/api/rooms' && request.method === 'POST') throw new Error('lost'); } });
  ui.type('IDB保留完整未决正文'); await ui.submit();
  assert.equal(ui.posts().length, 1);
  const reloaded = await browser({ storage });
  assert.equal(reloaded.get('message').value, 'IDB保留完整未决正文'); assert.equal(reloaded.posts().length, 0);
  const usable = new Map(); const original = await browser({ storage: usable, route(request) { if (request.path === '/api/rooms' && request.method === 'POST') throw new Error('lost'); } });
  original.type('旧连接不能自动重发'); await original.submit();
  const key = original.posts()[0].body.request_id;
  const changed = await browser({ storage: usable, token: 'different-connection' });
  assert.equal(changed.get('message').value, '旧连接不能自动重发'); await changed.click('retry-request');
  assert.equal(changed.posts().length, 0); assert.equal(savedOutbox(usable).request.body.request_id, key);
});

test('paged live progress and actual read-only claim stay truthful when their original events are outside the tail', async () => {
  const source = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); source.team.phase = 'working';
  const full = '这是伙伴真实说的话。'.repeat(1000); const sha = createHash('sha256').update(full).digest('hex');
  const ref = { kind: 'event_text', room_id: roomId, seq: 1, sha256: sha };
  const events = Array.from({ length: 101 }, (_, index) => ({ seq: index + 1, kind: 'message', native_progress: index === 0, sender: 'claude', recipient: 'all', job_id: jobId, text: index === 0 ? full : `原始记录${index}`, run_id: source.current_run.id, created_at: 1000 + index }));
  const active = { id: jobId, provider: 'claude', kind: 'team_user', status: 'working', run_id: source.current_run.id, work_mode: 'plan', runtime_settings: { members: { claude: { model: 'actual-model', effort: 'high' } } }, permission: 'read_only', claimed_at: 1000, native_readonly_discussion: true,
    latest_public_progress: { seq: 1, sender: 'claude', created_at: 1000, text_preview: full.slice(0, 100), text_text_ref: ref, text_truncated: true, text_characters: full.length, producer_truncated: false } };
  const saved = pagedFixture(source, events, [active]);
  const ui = await browser({ rooms: [source.room], saved, pagingSupport: true, naturalRuntimeSupport: true, naturalContinuationSupport: true, executionPolicySupport: true, route(request) {
    if (request.path.endsWith('/state')) return saved;
    if (request.path.includes('/text?')) return { ...ref, text: full, characters: full.length, utf8_bytes: Buffer.byteLength(full), truncated: false };
    return eventPageRoute(request, events);
  } });
  assert.match(ui.get('partner-activity-claude').textContent, /只读讨论.*本次配置：方案 · 只读 · actual-model · high/s);
  assert.match(ui.get('public-progress-claude').textContent, /本次进展原话/); assert.doesNotMatch(ui.get('public-progress-claude').textContent, /还没收到/);
  const read = descendants(ui.get('public-progress-claude')).find((node) => node.tagName === 'BUTTON' && node.textContent === '读取完整原话');
  read.fire('click');
  await waitUntil(() => ui.get('public-progress-claude').textContent.includes(full), 'full public progress must pass its asynchronous SHA verification');
  assert.ok(ui.get('public-progress-claude').textContent.includes(full));
  await ui.click('public-progress-jump-claude'); assert.ok(ui.get('events').textContent.includes(full)); assert.equal(ui.get('events').children.length, 101);
  assert.equal(ui.posts().length, 0);
});

test('paged complete state ignores a known older unknown job and exact goal refs cannot seed a fork with preview text', async () => {
  const source = objectiveSnapshot({ status: 'complete', decision: 'complete' }); source.room.status = 'completed'; source.cancellation_confirmed = true;
  const full = '完整目标原文'.repeat(1000); const sha = createHash('sha256').update(full).digest('hex');
  const ref = { kind: 'run_goal', room_id: roomId, run_id: source.current_run.id, sha256: sha };
  source.current_run.goal_preview = full.slice(0, 20); delete source.current_run.goal; source.current_run.goal_text_ref = ref; source.current_run.goal_truncated = true; source.current_run.can_fork = true; source.current_run.revision = 0;
  source.jobs = [{ id: jobId, provider: 'claude', status: 'unknown', run_id: '99999999-9999-4999-8999-999999999999' }];
  const saved = pagedFixture(source, []);
  const ui = await browser({ rooms: [source.room], saved, pagingSupport: true, executionPolicySupport: true, route(request) {
    if (request.path.endsWith('/state')) return saved;
    if (request.path.includes('/text?')) return { ...ref, text: full, truncated: false };
    return eventPageRoute(request, []);
  } });
  assert.equal(ui.get('cancel-room').hidden, true);
  await ui.click('fork-room'); assert.equal(ui.get('message').value, full);
  assert.equal(ui.posts().length, 0);
});

test('a conflicting legacy body keeps both original records quarantined under the same key', async () => {
  const storage = new Map(); const ui = await browser({ storage, route(request) { if (request.path === '/api/rooms' && request.method === 'POST') throw new Error('lost'); } });
  ui.type('已经登记的原始意图'); await ui.submit(); const original = structuredClone(savedOutbox(storage));
  const changed = structuredClone(original); changed.request.body.goal = '不能覆盖同一key的新内容';
  const raw = JSON.stringify({ scope: changed.scope, request: changed.request }); storage.set('lac-rooms-pending-v1', raw);
  const reloaded = await browser({ storage }); await reloaded.click('retry-request');
  assert.equal(reloaded.posts().length, 0); assert.equal(storage.get('lac-rooms-pending-v1'), raw);
  const row = outboxDatabases.get(storage).get(`${original.scope}:${original.request.body.request_id}`);
  assert.equal(row.request.body.goal, '已经登记的原始意图');
});

test('confirmed room deletion retries failed local outbox cleanup without retaining a resurrectable request', async () => {
  const source = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); const storage = new Map(); const failure = { commit: false }; let deleted = false;
  const ui = await browser({ storage, idbFailure: failure, rooms: [source.room], saved: source, naturalContinuationSupport: true, messageReceiptsSupport: true, route(request) {
    if (request.path.endsWith('/messages')) throw new Error('lost');
    if (request.path.endsWith('/messages/receipt')) { if (deleted) return { error: 'room_deleted' }; throw new Error('temporarily offline'); }
  } });
  ui.type('应该从本机outbox删除的正文'); await ui.submit(); failure.commit = true; deleted = true;
  await ui.poll(); assert.equal(ui.get('message').value, ''); assert.match(ui.get('action-error').textContent, /本机待发送缓存尚未确认清理/);
  failure.commit = false; await ui.poll(); await settle();
  assert.ok(![...outboxDatabases.get(storage).values()].some((row) => row.request?.body?.text === '应该从本机outbox删除的正文'));
  assert.equal(ui.posts().filter((request) => request.path.endsWith('/messages')).length, 1);
});


test('an acknowledged native recovery with an anomalous returned binding stays sent and never revives its request', async () => {
  const source = nativeRecoverySnapshot(); source.current_run.can_recover_native = true; source.current_run.native_recovery_reason = null;
  const saved = pagedFixture(source, []); const storage = new Map();
  const ui = await browser({ storage, rooms: [source.room], saved, pagingSupport: true, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.endsWith('/state')) return saved; // Deliberately returns the old run after a confirmed ACK.
    if (request.path.includes('/events?')) return eventPageRoute(request, []);
    if (request.path.endsWith('/recover-native?response=ack')) return { acknowledged: true, room_id: roomId, request_id: request.body.request_id };
  } });
  await ui.click('recover-native');
  assert.equal(ui.posts().length, 1); assert.match(ui.get('action-error').textContent, /^已发送，记录待核对。无需再次发送。$/);
  assert.doesNotMatch(ui.get('notice').textContent, /原请求已保留|结果未确认/);
  assert.equal(ui.get('pending-request').hidden, true); assert.equal(savedOutbox(storage).request, null);
  await ui.poll(); assert.equal(ui.posts().length, 1); assert.equal(ui.get('pending-request').hidden, true);
  const reloaded = await browser({ storage, rooms: [source.room], saved, pagingSupport: true, executionPolicySupport: true, nativeProjectSupport: true, route(request) {
    if (request.path.endsWith('/state')) return saved;
    return eventPageRoute(request, []);
  } });
  assert.equal(reloaded.posts().length, 0); assert.equal(reloaded.get('pending-request').hidden, true);
});


test('unavailable browser storage never blocks pause or independent Stop while resume still requires durable intent', async () => {
  const saved = objectiveSnapshot({ status: 'running' }); saved.objective.can_pause = true; saved.objective.can_resume = true;
  saved.jobs = [{ id: jobId, provider: 'codex', kind: 'team_work', status: 'working' }];
  const failure = { open: true };
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, idbFailure: failure, route(request) {
    if (request.path.endsWith('/pause')) { saved.objective.status = 'paused'; saved.objective.pause_requested = true; return saved; }
    if (request.path.endsWith('/cancel')) { saved.cancellation_confirmed = false; return saved; }
  } });
  await ui.click('pause-objective');
  assert.equal(ui.posts().length, 1); assert.match(ui.posts()[0].path, /\/pause$/);
  assert.equal(ui.get('pending-request').hidden, true);
  assert.match(ui.get('room-status').textContent, /正在暂停/); assert.equal(saved.jobs[0].status, 'working', 'pause acceptance does not invent worker cleanup');
  await ui.click('cancel-room'); assert.equal(ui.posts().length, 2); assert.match(ui.posts()[1].path, /\/cancel$/);
  assert.equal(ui.get('cancel-room').hidden, false, 'unconfirmed cleanup remains stoppable');
  saved.objective.pause_requested = false; saved.jobs = []; await ui.poll();
  await ui.click('pause-objective'); assert.equal(ui.posts().length, 2, 'resume cannot use the stop-only persistence exception');
  assert.match(ui.get('action-error').textContent, /尚未发送.*无法可靠保存/);
});

test('idle paged history fallback never claims that a current checkpoint with no progress erased earlier public speech', async () => {
  const source = objectiveSnapshot({ status: 'complete', decision: 'complete' }); source.jobs = [];
  const saved = pagedFixture(source, []);
  const ui = await browser({ rooms: [source.room], saved, pagingSupport: true, executionPolicySupport: true, route(request) {
    if (request.path.endsWith('/state')) return saved;
    return eventPageRoute(request, []);
  } });
  assert.equal(ui.get('public-progress-claude').textContent, '现在没在做事，之前的进展在聊天里。');
  assert.doesNotMatch(ui.get('public-progress-claude').textContent, /没有.*公开进展/); assert.equal(ui.posts().length, 0);
});

const stableOutboxScope = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
function hostOutboxPeer({ scope = stableOutboxScope, beforeStage, onStage, onDispatch } = {}) {
  const rows = new Map(); let sequence = 0;
  const summary = (row, body = false) => ({ ...structuredClone(row), ...(body ? {} : { body: undefined }) });
  const peer = { rows, scope, controls: new Map(), stageCalls: 0, dispatchCalls: 0, retireCalls: 0,
    async route(request) {
      const url = new URL(`http://fixture${request.path}`);
      if (url.pathname === '/api/outbox' && request.method === 'POST') {
        peer.stageCalls++;
        const data = request.body; if (beforeStage) await beforeStage(request);
        if (data.scope !== scope) return { error: 'outbox_scope_conflict' };
        const old = rows.get(data.request_id);
        if (old && (old.kind !== data.kind || old.room_id !== data.room_id || !sameJson(old.body, data.body))) return { error: 'request_conflict' };
        const row = old || { seq: ++sequence, scope, request_id: data.request_id, kind: data.kind, room_id: data.room_id,
          source_seq: data.source_seq ?? null, fingerprint: createHash('sha256').update(JSON.stringify(data.body)).digest('hex'), body: structuredClone(data.body), status: 'pending', created_at: 1000, updated_at: 1000, source_instance_id: 'host-instance-one', attempt_count: 0,
          delivery: { state: data.legacy_unknown || data.room_id && !Number.isSafeInteger(data.source_seq) ? 'unconfirmed' : 'staged', can_send: !data.legacy_unknown && (!data.room_id || Number.isSafeInteger(data.source_seq)), can_retire: !data.legacy_unknown, reason: null, result_room_id: null } };
        if (data.room_id && (peer.controls.get(data.room_id) ?? -1) > (row.source_seq ?? -1)) row.delivery = { ...row.delivery, state: 'conflict', can_send: false, can_retire: true, reason: 'outbox_intervening_control' };
        rows.set(data.request_id, row);
        if (onStage) await onStage(request, row);
        return { outbox: summary(row) };
      }
      if (url.pathname === '/api/outbox' && request.method === 'GET') {
        const through = Number(url.searchParams.get('through_seq') || sequence); const after = Number(url.searchParams.get('after_seq') || 0);
        const all = [...rows.values()].filter((row) => row.status === 'pending'); const eligible = all.filter((row) => row.seq > after && row.seq <= through);
        const entries = eligible.slice(0, 100);
        return { scope, entries: entries.map((row) => summary(row)), through_seq: through, last_seq: entries.at(-1)?.seq ?? null, has_more: eligible.length > entries.length, pending_count: all.length };
      }
      const match = /^\/api\/outbox\/([^/]+)(\/retire)?$/.exec(url.pathname);
      if (match) {
        const row = rows.get(decodeURIComponent(match[1])); if (!row) return { error: 'outbox_not_found' };
        if (match[2]) {
          peer.retireCalls++;
          if (request.body.scope !== scope || request.body.fingerprint !== row.fingerprint) return { error: 'request_conflict' };
          if (!row.delivery.can_retire) return { error: 'outbox_retire_unavailable' };
          row.status = 'retired'; row.body = null; row.delivery = { ...row.delivery, state: 'retired', can_send: false, can_retire: false };
          return { outbox: summary(row) };
        }
        return { outbox: summary(row, true) };
      }
      if (request.method === 'POST' && url.searchParams.has('outbox_scope')) {
        peer.dispatchCalls++; const row = rows.get(request.body.request_id);
        if (!row || row.status !== 'pending' || !sameJson(row.body, request.body)) return { error: 'request_conflict' };
        row.attempt_count++; row.delivery = { ...row.delivery, state: 'in_flight', can_send: false, can_retire: false };
        if (onDispatch) return onDispatch(request, row);
        row.delivery = { state: 'received', can_send: false, can_retire: true, reason: null, result_room_id: row.room_id || roomId };
        return { acknowledged: true, request_id: row.request_id, room_id: row.delivery.result_room_id };
      }
      return undefined;
    },
  };
  return peer;
}
function sameJson(left, right) { return JSON.stringify(left) === JSON.stringify(right); }

async function hostRoomBrowser(peer, options = {}) {
  const source = options.source || withNaturalContinuation(objectiveSnapshot({ status: 'running' })); if (source.team) source.team.phase = 'ready_for_user';
  const saved = pagedFixture(source, []);
  return browser({ rooms: [source.room], saved, pagingSupport: true, hostOutboxScope: peer.scope, naturalContinuationSupport: true, messageReceiptsSupport: true, ...options,
    route: async (request, document) => {
      const special = options.route ? await options.route(request, document) : undefined; if (special !== undefined) return special;
      const result = await peer.route(request); if (result !== undefined) return result;
      if (request.path.endsWith('/state')) return saved;
      return eventPageRoute(request, []);
    } });
}

test('host outbox commits the original body before dispatch despite unavailable browser storage and retires only confirmed receipt', async () => {
  const peer = hostOutboxPeer(); const ui = await hostRoomBrowser(peer, { idbFailure: { open: true } });
  ui.type('通过宿主保存再发送'); await ui.submit(); await ui.poll();
  const posts = ui.posts(); const stage = posts.find((request) => request.path === '/api/outbox');
  const dispatch = posts.find((request) => request.path.includes('/messages?response=ack&outbox_scope='));
  assert.ok(stage && dispatch); assert.deepEqual(stage.body.body, dispatch.body); assert.equal(stage.body.request_id, dispatch.body.request_id);
  assert.equal(peer.dispatchCalls, 1); assert.equal(peer.rows.get(dispatch.body.request_id).status, 'retired');
  assert.equal(peer.rows.get(dispatch.body.request_id).body, null); assert.equal(ui.get('pending-request').hidden, true);
  assert.ok(posts.indexOf(stage) < posts.indexOf(dispatch));
});

test('a fresh origin transparently resumes only an exact staged host intent without replacing the newer local draft or selecting its chat', async () => {
  const peer = hostOutboxPeer(); const key = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
  const body = { text: '原房原始发送意图', target: 'all', request_id: key };
  await peer.route({ path: '/api/outbox', method: 'POST', body: { scope: peer.scope, request_id: key, kind: 'message', room_id: roomId, source_seq: 0, body } });
  const ui = await hostRoomBrowser(peer, { storage: new Map(), token: 'new-instance-new-token', selectSaved: false });
  await settle();
  assert.equal(peer.dispatchCalls, 1); const dispatch = ui.posts().find((request) => request.path.includes('outbox_scope='));
  assert.deepEqual(dispatch.body, body); assert.equal(ui.location.hash, '', 'background resume never selects the old chat');
  ui.type('当前新任务草稿'); await ui.poll();
  assert.equal(ui.get('message').value, '当前新任务草稿'); assert.equal(peer.dispatchCalls, 1);
  assert.equal(peer.rows.get(key).status, 'retired');
});

test('attempted in-flight and legacy-unknown host intents only reconcile reads across a fresh browser and never redispatch', async () => {
  for (const state of ['in_flight', 'unconfirmed']) {
    const peer = hostOutboxPeer(); const key = state === 'in_flight' ? 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee' : 'ffffffff-ffff-4fff-8fff-ffffffffffff';
    await peer.route({ path: '/api/outbox', method: 'POST', body: { scope: peer.scope, request_id: key, kind: 'message', room_id: roomId, source_seq: 0, body: { text: '原始未确认正文', target: 'all', request_id: key }, legacy_unknown: state === 'unconfirmed' } });
    const row = peer.rows.get(key); row.delivery.state = state; row.delivery.can_send = false; row.delivery.can_retire = false; row.attempt_count = state === 'in_flight' ? 1 : 0;
    const ui = await hostRoomBrowser(peer, { storage: new Map(), token: 'reopened-token' });
    await ui.poll(); await ui.poll();
    assert.equal(peer.dispatchCalls, 0); assert.equal(row.status, 'pending'); assert.equal(row.body.text, '原始未确认正文');
    assert.equal(ui.posts().some((request) => request.path.includes('outbox_scope=')), false);
  }
});

test('a late committed host stage cannot dispatch after archive or release a newer request send slot', async () => {
  const heldStage = deferred(); const heldNewReply = deferred(); const source = withNaturalContinuation(objectiveSnapshot({ status: 'running' }));
  const newRoomId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
  const peer = hostOutboxPeer({ onStage(request) { if (request.body.body.text === '等待保存的旧消息') return heldStage.promise; },
    onDispatch(request, row) { assert.equal(row.kind, 'create', 'intervened old message must never reach dispatch'); return heldNewReply.promise; } });
  const newSource = structuredClone(source); newSource.room.id = newRoomId; newSource.room.goal = '新的任务草稿'; newSource.objective.original_goal = '新的任务草稿'; newSource.objective.goal = '新的任务草稿';
  const newSaved = pagedFixture(newSource, []);
  const ui = await hostRoomBrowser(peer, { source, roomLifecycleSupport: true, executionPolicySupport: true, route(request) {
    if (request.path.endsWith('/lifecycle')) {
      source.room.lifecycle_state = 'archived';
      for (const row of peer.rows.values()) if (row.room_id === roomId) row.delivery = { ...row.delivery, state: 'conflict', can_send: false, can_retire: true, reason: 'outbox_intervening_control' };
      const result = { ...structuredClone(source), ...lifecycleEnvelope('archive', 'completed') }; result.lifecycle_operation.request_id = request.body.request_id; return result;
    }
    if (request.path === `/api/rooms/${newRoomId}/state`) return newSaved;
    if (request.path.startsWith(`/api/rooms/${newRoomId}/events?`)) return { room_id: newRoomId, through_seq: 0, events: [], has_before: false, has_after: false, first_seq: null, last_seq: null };
  } });
  ui.type('等待保存的旧消息'); await ui.submit(); assert.equal(peer.dispatchCalls, 0);
  await ui.click('new-room'); ui.type('新的任务草稿'); await ui.submit(); assert.equal(peer.dispatchCalls, 1);
  ui.get('rooms').firstChild.fire('contextmenu'); await settle();
  lifecycleButton(ui.get('entry-context-actions'), { kind: 'rooms', id: roomId, action: 'archive' }).fire('click'); await settle();
  heldStage.resolve(); await settle();
  assert.equal(peer.dispatchCalls, 1); assert.equal(ui.get('send-label').textContent, '发送中');
  assert.equal(ui.get('pending-request').hidden, false); assert.equal(savedOutbox(ui.storage).request.body.goal, '新的任务草稿'); assert.equal(ui.get('message').value, '新的任务草稿');
  const newRow = [...peer.rows.values()].find((row) => row.kind === 'create'); newRow.delivery = { state: 'received', can_send: false, can_retire: true, reason: null, result_room_id: newRoomId };
  heldNewReply.resolve({ acknowledged: true, request_id: newRow.request_id, room_id: newRoomId }); await settle();
  assert.equal(ui.get('pending-request').hidden, true); assert.match(ui.get('room-title').textContent, /新的任务草稿/);
  assert.equal(peer.dispatchCalls, 1);
});

test('host outbox stage failure never dispatches and explicit pause bypasses both host and browser storage', async () => {
  const source = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); source.objective.can_pause = true;
  const peer = hostOutboxPeer(); const ui = await hostRoomBrowser(peer, { source, executionPolicySupport: true, idbFailure: { open: true }, route(request) {
    if (request.path === '/api/outbox') throw new Error('outbox unavailable');
    if (request.path.endsWith('/pause?response=ack')) return { acknowledged: true, room_id: roomId, request_id: request.body.request_id };
  } });
  await ui.click('pause-objective'); assert.equal(peer.stageCalls, 0); assert.equal(peer.dispatchCalls, 0);
  assert.ok(ui.posts().some((request) => request.path.endsWith('/pause?response=ack')));
  ui.type('不能绕过宿主保存的正文'); await ui.submit();
  assert.equal(peer.dispatchCalls, 0); assert.match(ui.get('action-error').textContent, /发送记录尚未确认.*未派发/);
  assert.equal(ui.get('message').value, '不能绕过宿主保存的正文');
});

test('legacy browser unknown requests migrate conservatively and never become never-attempted sends', async () => {
  const storage = new Map(); const source = withNaturalContinuation(objectiveSnapshot({ status: 'running' }));
  const old = await browser({ storage, rooms: [source.room], saved: source, naturalContinuationSupport: true, route(request) { if (request.path.endsWith('/messages')) throw new Error('old response lost'); } });
  old.type('旧版可能已经发送的正文'); await old.submit(); const original = structuredClone(old.posts()[0].body);
  const peer = hostOutboxPeer(); const migrated = await hostRoomBrowser(peer, { source, storage });
  const stage = migrated.posts().find((request) => request.path === '/api/outbox');
  assert.equal(stage.body.legacy_unknown, true); assert.deepEqual(stage.body.body, original);
  await migrated.poll(); await migrated.poll(); assert.equal(peer.dispatchCalls, 0);
  assert.equal(peer.rows.get(original.request_id).delivery.state, 'unconfirmed'); assert.equal(migrated.get('message').value, original.text);
});

test('a different durable workspace scope cannot replay a cached host-bound body', async () => {
  const storage = new Map(); const firstPeer = hostOutboxPeer({ onDispatch() { throw new Error('lost after attempt'); } });
  const first = await hostRoomBrowser(firstPeer, { storage }); first.type('只能属于原工作区的发送'); await first.submit();
  const original = first.posts().find((request) => request.path.includes('outbox_scope=')).body;
  const otherPeer = hostOutboxPeer({ scope: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' });
  const other = await hostRoomBrowser(otherPeer, { storage }); await other.poll(); await other.click('retry-request');
  assert.equal(otherPeer.stageCalls, 0); assert.equal(otherPeer.dispatchCalls, 0); assert.equal(other.get('message').value, original.text);
  assert.ok(firstPeer.rows.get(original.request_id).body);
});

test('a restored received host record cannot clear a newer identical local text draft', async () => {
  const source = withNaturalContinuation(objectiveSnapshot({ status: 'running' }));
  const peer = hostOutboxPeer(); const ui = await hostRoomBrowser(peer, { source });
  ui.type('同样的话也可能是新的要求');
  const key = 'abababab-abab-4bab-8bab-abababababab';
  await peer.route({ path: '/api/outbox', method: 'POST', body: { scope: peer.scope, request_id: key, kind: 'message', room_id: roomId, source_seq: 0, body: { text: '同样的话也可能是新的要求', target: 'all', request_id: key } } });
  const row = peer.rows.get(key); row.delivery = { state: 'received', can_send: false, can_retire: true, reason: null, result_room_id: roomId };
  await ui.poll();
  assert.equal(ui.get('message').value, '同样的话也可能是新的要求'); assert.equal(peer.dispatchCalls, 0); assert.equal(row.status, 'retired');
});

test('a stage arriving after an explicit pause retains its original source sequence and never auto-reverses that pause on reopen', async () => {
  const network = deferred(); let hold = true;
  const peer = hostOutboxPeer({ beforeStage(request) { if (hold && request.body.kind === 'resume') return network.promise; } });
  const source = objectiveSnapshot({ status: 'paused' }); source.objective.pause_requested = false; source.objective.can_resume = true;
  const ui = await hostRoomBrowser(peer, { source, naturalContinuationSupport: false, executionPolicySupport: true });
  await ui.click('pause-objective');
  const staged = ui.posts().find((request) => request.path === '/api/outbox'); assert.equal(staged.body.kind, 'resume'); assert.equal(staged.body.source_seq, 0);
  assert.equal(peer.rows.size, 0, 'the stage has not reached the host before the control');
  peer.controls.set(roomId, 5); // Another authenticated window has now paused this room.
  hold = false; network.resolve(); await settle();
  const row = peer.rows.get(staged.body.request_id); assert.equal(row.source_seq, 0); assert.equal(row.delivery.can_send, false);
  assert.equal(peer.dispatchCalls, 0);
  if (row.status === 'retired') assert.equal(row.body, null);
  else assert.deepEqual(row.body, staged.body.body);
  const reopened = await hostRoomBrowser(peer, { source, storage: new Map(), token: 'next-instance-token', naturalContinuationSupport: false, executionPolicySupport: true });
  await reopened.poll(); assert.equal(peer.dispatchCalls, 0);
  assert.equal(row.delivery.state === 'conflict' || row.status === 'retired', true);
});

test('a host-staged create can continue in the background while normal navigation and new drafts stay independent', async () => {
  const reply = deferred(); const peer = hostOutboxPeer({ onDispatch() { return reply.promise; } });
  const source = withNaturalContinuation(objectiveSnapshot({ status: 'running' }));
  const ui = await hostRoomBrowser(peer, { source, selectSaved: false });
  ui.type('后台创建的原始目标'); await ui.submit();
  const row = [...peer.rows.values()][0]; assert.equal(row.kind, 'create'); assert.equal(peer.dispatchCalls, 1);
  assert.equal(ui.get('new-room').disabled, false, 'confirmed host staging allows normal new-chat navigation');
  await ui.click('new-room'); assert.equal(ui.get('message').value, '');
  ui.type('后来写的新任务草稿');
  ui.get('rooms').firstChild.fire('click'); await settle(); assert.equal(ui.location.hash, `#room=${roomId}`);
  await ui.click('new-room'); assert.equal(ui.get('message').value, '后来写的新任务草稿');
  row.delivery = { state: 'received', can_send: false, can_retire: true, reason: null, result_room_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' };
  reply.resolve({ acknowledged: true, request_id: row.request_id, room_id: row.delivery.result_room_id }); await settle();
  assert.equal(ui.location.hash, ''); assert.equal(ui.get('message').value, '后来写的新任务草稿');
  assert.equal(ui.get('pending-request').hidden, true); assert.equal(peer.dispatchCalls, 1);
});

 test('a restored staged creation opens its real result only on the untouched welcome page', async () => {
  const peer = hostOutboxPeer(); const key = 'abababab-abab-4bab-8bab-abababababab';
  await peer.route({ path: '/api/outbox', method: 'POST', body: { scope: peer.scope, request_id: key, kind: 'create', room_id: null,
    body: { request_id: key, goal: '恢复原来的创建意图', participants: ['claude', 'codex'], mode: 'text' } } });
  const ui = await hostRoomBrowser(peer, { selectSaved: false, storage: new Map(), token: 'fresh-host-token' });
  await ui.poll();
  assert.equal(ui.location.hash, `#room=${roomId}`);
  assert.equal(peer.dispatchCalls, 1); assert.equal(peer.rows.get(key).status, 'retired');
  assert.equal(ui.get('message').value, '');
});

function completedSuggestionFixture(goal = '复用已交付的诊断记录，核对真实点击失败时的状态。') {
  const saved = withNaturalContinuation(objectiveDeliveryFixture());
  saved.objective.status = 'complete'; saved.team.phase = 'ready_for_user';
  saved.collaboration = teamCollaboration({ state: 'ready_for_user', tasks: [] });
  saved.objective.next_step = { decision: 'complete', scope: 'new_goal', goal, summary: '完整完成报告。'.repeat(100), can_start: false,
    binding: { run_id: saved.current_run.id, revision: saved.current_run.revision, digest: saved.current_run.digest }, review_kind: 'independent_review' };
  return saved;
}
test('completed work folds its full report and files while showing the exact bound optional next suggestion', async () => {
  const saved = completedSuggestionFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.endsWith('/messages')) return saved;
  } });
  const report = ui.document.querySelector('.completion-report'); const files = ui.document.querySelector('.delivery-content');
  assert.equal(report.tagName, 'DETAILS'); assert.equal(report.open, false); assert.match(report.textContent, /完整完成报告/);
  assert.equal(files.tagName, 'DETAILS'); assert.equal(files.open, false);
  assert.equal(ui.document.querySelector('.completion-suggestion').textContent, saved.objective.next_step.goal);
  assert.equal(ui.get('add-completion-suggestion').textContent, '加入输入框'); assert.equal(ui.posts().length, 0);
  assert.equal(ui.document.querySelector('.completion-summary-preview').textContent, saved.objective.next_step.summary);
  assert.match(ui.document.querySelector('.completion-guidance').textContent, /无需回复或确认/);
  assert.equal(ui.document.getElementById('continue-current'), null); assert.equal(ui.document.getElementById('accept-run'), null);
  assert.match(files.textContent, /伙伴看过|只做了自查/);
  report.open = true; files.open = true; saved.collaboration.outcome.validation.summary = '新增检查说明'; await ui.poll();
  assert.equal(ui.document.querySelector('.completion-report').open, true); assert.equal(ui.document.querySelector('.delivery-content').open, true);
  await ui.click('add-completion-suggestion');
  assert.equal(ui.get('message').value, saved.objective.next_step.goal); assert.equal(ui.posts().length, 0);
});
test('applying a completion suggestion preserves and extends a newer draft without dispatching it', async () => {
  const saved = completedSuggestionFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  ui.choose('codex'); ui.type('我还想保留的另一条要求'); assert.equal(ui.get('add-completion-suggestion').textContent, '加入输入框');
  await ui.click('add-completion-suggestion'); await ui.click('add-completion-suggestion');
  assert.equal(ui.get('recipient-button').textContent.includes('Codex'), true);
  assert.equal(ui.get('message').value, `我还想保留的另一条要求\n\n${saved.objective.next_step.goal}`);
  assert.equal(ui.document.activeElement.id, 'message'); assert.equal(ui.posts().length, 0);
});
test('missing or stale completion suggestions never promote the full completion report into a proposed action', async () => {
  for (const variant of ['missing', 'stale', 'blocked']) {
    const saved = completedSuggestionFixture(variant === 'missing' ? null : '真实但需核对绑定的建议');
    if (variant === 'stale') saved.objective.next_step.binding.digest = 'a'.repeat(64);
    if (variant === 'blocked') saved.continuation.can_send = false;
    const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
    assert.equal(ui.document.querySelector('.completion-suggestion') === null, variant !== 'blocked');
    if (variant !== 'blocked') assert.match(ui.document.querySelector('.completion-next').textContent, variant === 'stale' ? /版本尚未确认/ : /模型本轮未提出新的执行目标/);
    assert.equal(ui.document.getElementById('continue-current'), null);
    assert.equal(ui.document.getElementById('add-completion-suggestion') === null, variant !== 'blocked'); assert.equal(ui.posts().length, 0);
    assert.equal(ui.document.querySelector('.completion-report').textContent.includes('完整完成报告'), variant !== 'stale', 'unbound previous report is not presented as this completion');
  }
});

test('double activation of a completion suggestion only fills once and never dispatches work', async () => {
  const saved = completedSuggestionFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  await ui.click('add-completion-suggestion'); await ui.click('add-completion-suggestion');
  assert.equal(ui.get('message').value, saved.objective.next_step.goal);
  assert.equal(ui.posts().length, 0); assert.equal(ui.get('pending-request').hidden, true);
  assert.equal(ui.location.hash, `#room=${roomId}`);
});
test('an old completion suggestion button cannot alter the draft after its run binding changes', async () => {
  let saved = completedSuggestionFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, route(request) {
    if (request.method === 'GET' && request.path === `/api/rooms/${roomId}`) return saved;
  } });
  const oldButton = ui.get('add-completion-suggestion');
  saved = structuredClone(saved); saved.current_run.revision++; saved.continuation.binding.revision++;
  saved.objective.next_step.binding.revision++; await ui.poll(); ui.type('当前草稿');
  oldButton.fire('click'); await settle();
  assert.equal(ui.get('message').value, '当前草稿'); assert.equal(ui.posts().length, 0);
});
test('completed review scope keeps original task titles in closed details after the guidance and summary', async () => {
  const saved = completedSuggestionFixture(); saved.objective.next_step.review_kind = 'self_check';
  saved.team.tasks = ['[0%] 原始任务甲', '[0%] 原始任务乙', '[0%] 原始任务丙'].map((title, index) => ({ id: `S${index}`, title, owner: 'codex', status: 'self_checked', review_kind: 'self_check' }));
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  const outcome = ui.get('collaboration-outcome'); const details = ui.document.querySelector('.review-scope-details');
  assert.equal(details.open, false); assert.match(details.textContent, /\[0%\] 原始任务甲/);
  assert.equal(ui.document.querySelector('.review-scope-note').textContent, '3 项只做了自查，没有别的伙伴看过。');
  assert.equal(outcome.children.filter((node) => node.classList.contains('review-scope-note')).length, 1);
  const visibleParagraphs = outcome.children.filter((node) => node.tagName === 'P');
  assert.doesNotMatch(visibleParagraphs.map((node) => node.textContent).join(''), /\[0%\]|原始任务/);
  assert.ok(outcome.children.indexOf(ui.document.querySelector('.completion-guidance')) < outcome.children.indexOf(details));
  assert.ok(outcome.children.indexOf(ui.document.querySelector('.completion-summary-preview')) < outcome.children.indexOf(ui.document.querySelector('.review-scope-note')));
  details.open = true; await ui.poll(); assert.equal(ui.document.querySelector('.review-scope-details').open, true);
  assert.equal(ui.posts().length, 0);
});
test('checkpoint self-check never relabels independently reviewed work as unreviewed', async () => {
  const saved = completedSuggestionFixture(); saved.objective.next_step.review_kind = 'self_check';
  saved.team.tasks = [{ id: 'A', owner: 'claude', title: '已审核任务', status: 'accepted', review_kind: 'independent_review' }];
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  assert.match(ui.get('collaboration-outcome').textContent, /完成评估由单个伙伴核对/);
  assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /项只做了自查|尚有内容未经独立互审/);
});

test('the compact work indicator animates only actual live work and names the real checkpoint or inspection phase', async () => {
  const source = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); source.team.phase = 'working';
  const job = { id: jobId, provider: 'claude', kind: 'team_user', status: 'working', phase: 'checkpoint', run_id: source.current_run.id };
  const saved = pagedFixture(source, [], [job]);
  const ui = await browser({ rooms: [source.room], saved, pagingSupport: true, executionPolicySupport: true, route(request) {
    if (request.path.endsWith('/state')) return saved; return eventPageRoute(request, []);
  } });
  assert.equal(ui.get('work-indicator').hidden, false); assert.equal(ui.get('work-indicator').classList.contains('is-working'), true);
  assert.match(ui.get('work-indicator-text').textContent, /Claude Code 正在核对下一步/);
  const textNode = ui.get('work-indicator-text'); const descriptor = Object.getOwnPropertyDescriptor(FakeElement.prototype, 'textContent'); let updates = 0;
  Object.defineProperty(textNode, 'textContent', { get() { return descriptor.get.call(this); }, set(value) { updates++; descriptor.set.call(this, value); }, configurable: true });
  await ui.poll(); assert.equal(updates, 0, 'unchanged polling does not reannounce the live status');
  saved.live.jobs[0].native_partial_inspection = true; await ui.poll();
  assert.match(textNode.textContent, /正在核对上次进度/);
  saved.live.jobs[0].status = 'queued'; await ui.poll();
  assert.match(textNode.textContent, /等待开始/); assert.equal(ui.get('work-indicator').classList.contains('is-working'), false);
  saved.live.jobs = []; saved.team.tasks = [{ id: 'peer', owner: 'claude', status: 'waiting_peer', title: '等另一位伙伴' }]; await ui.poll();
  assert.equal(ui.get('work-indicator').hidden, true, 'objective running or waiting for a peer cannot manufacture active work');
});

test('work motion stops for pause, user decision, completion and a disconnected snapshot without erasing public speech', async () => {
  const source = withNaturalContinuation(objectiveSnapshot({ status: 'running' })); source.team.phase = 'working';
  const event = { seq: 1, kind: 'message', sender: 'claude', recipient: 'all', text: '这是实际公开进展，不是生成的状态摘要。', native_progress: true, job_id: jobId, run_id: source.current_run.id, created_at: 1000 };
  const job = { id: jobId, provider: 'claude', kind: 'team_user', status: 'working', run_id: source.current_run.id };
  const saved = pagedFixture(source, [event], [job]); let disconnected = false;
  const ui = await browser({ rooms: [source.room], saved, pagingSupport: true, executionPolicySupport: true, route(request) {
    if (disconnected) throw new Error('fixture offline');
    if (request.path.endsWith('/state')) return saved; return eventPageRoute(request, [event]);
  } });
  assert.equal(ui.get('work-indicator').classList.contains('is-working'), true);
  saved.objective.status = 'paused'; saved.objective.pause_requested = true; await ui.poll();
  assert.equal(ui.get('work-indicator').classList.contains('is-working'), true); assert.match(ui.get('work-indicator-text').textContent, /正在结束当前处理/);
  saved.live.jobs = []; await ui.poll(); assert.equal(ui.get('work-indicator').hidden, true);
  saved.live.jobs = [job]; saved.objective.pause_requested = false;
  for (const status of ['needs_user', 'complete']) {
    saved.objective.status = status; await ui.poll();
    assert.equal(ui.get('work-indicator').hidden, true); assert.equal(ui.get('work-indicator').classList.contains('is-working'), false);
    assert.match(ui.get('events').textContent, /实际公开进展/);
  }
  saved.objective.status = 'running'; await ui.poll(); assert.equal(ui.get('work-indicator').classList.contains('is-working'), true);
  const objective = saved.objective; saved.objective = null; saved.room.status = 'completed'; await ui.poll();
  assert.equal(ui.get('work-indicator').hidden, true, 'legacy completed records cannot animate stale working metadata');
  saved.objective = objective; saved.room.status = 'running'; await ui.poll();
  disconnected = true; await ui.poll(); assert.equal(ui.get('work-indicator').hidden, true);
  assert.equal(ui.get('work-indicator').classList.contains('is-working'), false);
  assert.match(fs.readFileSync(path.join(assetRoot, 'style.css'), 'utf8'), /@media \(prefers-reduced-motion: reduce\)[\s\S]*work-indicator[\s\S]*animation: none/);
});

test('continuing and checkpoint work keep previous readable results in a compact closed history instead of a large outcome card', async () => {
  const saved = completedSuggestionFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  ui.document.querySelector('.delivery-content').open = true;
  for (const status of ['running', 'checkpoint']) {
    saved.objective.status = status; saved.collaboration.outcome.state = 'in_progress'; saved.team.phase = 'working';
    await ui.poll();
    const outcome = ui.get('collaboration-outcome'); const history = ui.document.querySelector('.running-delivery-history');
    assert.equal(outcome.hidden, true); assert.equal(outcome.children.length, 0);
    assert.equal(history.parentNode, ui.get('materials-outcomes'));
    assert.equal(history.open, false); assert.match(history.firstChild.textContent, /此前成果.*份/);
    assert.doesNotMatch(outcome.textContent, /完整完成报告|正在安排下一步|检查范围|导出 ZIP/);
    assert.equal(ui.document.getElementById('download-delivery'), null);
    assert.equal(descendants(history).some((node) => node.classList.contains('delivery-file')), true);
    await ui.poll(); assert.equal(history.open, false, 'ordinary poll never opens the historical results');
  }
  const history = ui.document.querySelector('.running-delivery-history'); history.open = true;
  saved.collaboration.summary = '真实分工稍有更新'; await ui.poll();
  assert.equal(ui.document.querySelector('.running-delivery-history').open, true, 'explicitly opened history remains usable while work continues');
  await ui.click('task-materials-open');
  descendants(ui.get('materials-outcomes')).find(node => node.dataset.documentPath === 'first.md').fire('click');
  await settle(); assert.match(ui.get('reader-body').textContent, /第一阶段的正文/);
  assert.equal(ui.posts().length, 0);
});

const concreteStepsRequest = '请先说明我具体要做哪一步、在哪里操作或找什么；如果你们能在现有授权范围自行完成，请先自行处理；需要我提供材料时给出具体获取方法，拿不到时说明还能推进什么。';
function needsReplyFixture() {
  const saved = completedSuggestionFixture(); saved.objective.status = 'needs_user'; saved.objective.can_answer = true;
  saved.room.status = 'needs_user'; saved.team.phase = 'needs_user';
  const question = '请提供完整的实际运行记录。\n如果暂时找不到，请说明你在哪一步遇到了问题。';
  saved.objective.next_step.question_source = { seq: 21, sender: 'system', job_id: null, created_at: 1021 };
  saved.events.push({ seq: 21, kind: 'objective_checkpoint_finished', sender: 'system', created_at: 1021, objective_id: saved.objective.id, run_id: saved.current_run.id, decision: 'needs_user' });
  saved.objective.next_step.question = question; saved.objective.next_step.summary = '技术说明与已有检查。'.repeat(40);
  saved.collaboration = teamCollaboration({ state: 'needs_user', reason: question, nextAction: question });
  return saved;
}
test('needs-user questions appear once at their real checkpoint position in normal chat with only compact old results', async () => {
  const saved = needsReplyFixture(); const question = saved.objective.next_step.question;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.endsWith('/messages')) return saved;
  } });
  const events = ui.get('events');
  assert.equal(events.textContent.split(question).length - 1, 1);
  const checkpoint = events.children.find(node => node.dataset.seq === '21');
  assert.ok(checkpoint.textContent.includes(question)); assert.match(checkpoint.textContent, /豆窝协作/);
  assert.equal(ui.get('collaboration-outcome').textContent.includes(question), false);
  const history = ui.document.querySelector('.waiting-delivery-history'); assert.equal(history.open, false);
  assert.equal(history.parentNode, ui.get('materials-outcomes')); assert.equal(ui.get('collaboration-outcome').hidden, true);
  assert.match(history.textContent, /技术说明与已有检查/);
  await ui.click('task-materials-open');
  assert.equal(descendants(ui.get('task-materials')).includes(checkpoint), false);
  assert.equal(ui.get('task-materials').hidden, false);
  await ui.click('task-materials-close');
  assert.equal(ui.get('timeline').hidden, false);
  assert.equal(ui.document.getElementById('write-reply'), null); assert.equal(ui.document.getElementById('clarify-next-step'), null);
  assert.equal(ui.posts().length, 0);
  ui.type('我能提供的材料是这些。'); await ui.submit();
  const post = ui.posts()[0]; assert.equal(post.path, `/api/rooms/${roomId}/messages`);
  assert.equal(post.body.text, '我能提供的材料是这些。'); assert.equal(post.body.intent, 'continue');
  assert.deepEqual(post.body.binding, saved.continuation.binding);
});
test('normal reply drafts remain untouched by question and result refreshes without a helper or approval layer', async () => {
  const saved = needsReplyFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  ui.type('先保留这句尚未发出的要求。'); ui.get('message').selectionStart = 2; ui.get('message').selectionEnd = 5;
  await ui.poll();
  assert.equal(ui.get('message').value, '先保留这句尚未发出的要求。');
  assert.equal(ui.get('message').selectionStart, 2); assert.equal(ui.get('message').selectionEnd, 5);
  assert.equal(ui.posts().length, 0); assert.equal(ui.document.getElementById('write-reply'), null);
});
test('plain composer replies retain the bound legacy answer route and the natural host send gate', async () => {
  const saved = needsReplyFixture();
  const legacy = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, route(request) {
    if (request.path.endsWith('/answer')) return saved;
  } });
  legacy.type('真实用户回复'); await legacy.submit(); const post = legacy.posts()[0];
  assert.equal(post.path, `/api/rooms/${roomId}/answer`); assert.equal(post.body.text, '真实用户回复');
  assert.equal(post.body.objective_id, saved.objective.id); assert.equal(post.body.run_id, saved.current_run.id);
  saved.continuation.can_send = false; saved.continuation.reason = 'agent_quota_exhausted';
  const blocked = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  blocked.type('保留未发出的回答'); assert.equal(blocked.get('send').disabled, true); assert.equal(blocked.posts().length, 0);
});
test('switching from expanded completed results to a question closes old results without hiding the original question', async () => {
  const saved = completedSuggestionFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  ui.document.querySelector('.delivery-content').open = true; ui.document.querySelector('.delivery-details').open = true;
  const next = needsReplyFixture(); Object.assign(saved, next); await ui.poll();
  assert.equal(ui.document.querySelector('.waiting-delivery-history').open, false);
  assert.ok(ui.get('events').textContent.includes(next.objective.next_step.question));
  await ui.poll(); assert.equal(ui.document.querySelector('.waiting-delivery-history').open, false);
  assert.equal(ui.posts().length, 0);
});
test('checkpoint questions require real system source sequence and exact current binding without guessing an agent author', async () => {
  for (const invalid of ['source', 'binding', 'event']) {
    const saved = needsReplyFixture(); const question = saved.objective.next_step.question;
    if (invalid === 'source') delete saved.objective.next_step.question_source;
    if (invalid === 'binding') saved.objective.next_step.binding.digest = 'f'.repeat(64);
    if (invalid === 'event') saved.events.find(event => event.seq === 21).sender = 'claude';
    const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
    assert.equal(ui.get('events').textContent.includes(question), false); assert.equal(ui.posts().length, 0);
  }
});

test('persisted checkpoint questions stay once in ordinary chat after answering and refreshing without current-step fallback', async () => {
  const saved = needsReplyFixture(); const question = saved.objective.next_step.question;
  const checkpoint = saved.events.find(event => event.seq === 21);
  checkpoint.question = question; checkpoint.question_binding = structuredClone(saved.objective.next_step.binding);
  checkpoint.proposal_digest = 'e'.repeat(64);
  saved.events.push({ seq: 22, kind: 'objective_decision', sender: 'user', text: '这是用户真实回复。', created_at: 1022 });
  saved.objective.status = 'running'; saved.objective.next_step = null; saved.collaboration.outcome.state = 'in_progress';
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  assert.equal(ui.get('events').textContent.split(question).length - 1, 1);
  assert.deepEqual(ui.get('events').children.filter(node => ['21', '22'].includes(node.dataset.seq)).map(node => node.dataset.seq), ['21', '22']);
  await ui.poll(); assert.equal(ui.get('events').textContent.split(question).length - 1, 1);
  const reopened = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  assert.equal(reopened.get('events').textContent.split(question).length - 1, 1);
  assert.equal(reopened.posts().length, 0);
});
test('paged persisted checkpoint questions preserve literal text and their original sequence without a new agent message', async () => {
  const source = needsReplyFixture(); const question = source.objective.next_step.question + '<img src=x onerror=alert(1)>';
  const event = { ...source.events.find(item => item.seq === 21), question,
    question_binding: structuredClone(source.objective.next_step.binding), proposal_digest: 'e'.repeat(64) };
  source.objective.status = 'complete'; source.objective.next_step = null;
  const saved = pagedFixture(source, [event]);
  const ui = await browser({ rooms: [source.room], saved, pagingSupport: true, executionPolicySupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.endsWith('/state')) return saved;
    return eventPageRoute(request, [event]);
  } });
  const node = ui.get('events').children.find(item => item.dataset.seq === '21');
  assert.ok(node.textContent.includes(question)); assert.match(node.textContent, /豆窝协作/);
  assert.equal(descendants(node).some(item => item.tagName === 'IMG'), false); assert.equal(ui.posts().length, 0);
});

test('real user objective decisions remain visible in sequence as literal original replies while system decision payloads stay internal', async () => {
  const user = { seq: 2, kind: 'objective_decision', sender: 'user', text: `${concreteStepsRequest}\n<img src=x onerror=alert(1)>`, created_at: 1002 };
  const internal = { seq: 3, kind: 'objective_decision', sender: 'system', text: '不应冒成用户发言的内部数据', created_at: 1003 };
  const saved = needsReplyFixture(); saved.events = [message(1, 'claude', '请补充具体材料。'), user, internal, message(4, 'codex', '收到，先说明具体操作。')];
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  const nodes = ui.get('events').children;
  assert.deepEqual(nodes.map((node) => node.dataset.seq), ['1', '2', '4']);
  assert.match(nodes[1].textContent, /你.*用户回复/s); assert.ok(nodes[1].textContent.includes(user.text));
  assert.equal(descendants(nodes[1]).some((node) => node.tagName === 'IMG'), false);
  assert.doesNotMatch(ui.get('events').textContent, /内部数据/); assert.equal(ui.posts().length, 0);
});

test('paged user objective decision previews read their exact full original lazily and never resend the answer', async () => {
  const source = needsReplyFixture(); const full = concreteStepsRequest.repeat(100); const sha = createHash('sha256').update(full).digest('hex');
  const ref = { kind: 'event_text', room_id: roomId, seq: 1, sha256: sha };
  const event = { seq: 1, kind: 'objective_decision', sender: 'user', text_preview: full.slice(0, 50), text_truncated: true, text_text_ref: ref, created_at: 1001 };
  const saved = pagedFixture(source, [event]);
  const ui = await browser({ rooms: [source.room], saved, pagingSupport: true, executionPolicySupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.endsWith('/state')) return saved;
    if (request.path.includes('/text?')) return { ...ref, text: full, characters: full.length, utf8_bytes: Buffer.byteLength(full), truncated: false };
    return eventPageRoute(request, [event]);
  } });
  assert.match(ui.get('events').textContent, /用户回复/); assert.equal(ui.requests.filter((request) => request.path.includes('/text?')).length, 0);
  const details = descendants(ui.get('events')).find((node) => node.classList.contains('bound-text'));
  details.open = true; details.fire('toggle');
  await waitUntil(() => details.textContent.includes(full), 'the full original user reply must pass its asynchronous SHA verification');
  assert.ok(details.textContent.includes(full)); await ui.poll();
  assert.equal(ui.get('events').children.filter((node) => node.dataset.seq === '1').length, 1);
  assert.equal(ui.posts().length, 0);
});

test('disconnected ordinary replies retain their draft and do not create a replacement approval action', async () => {
  const saved = needsReplyFixture(); let offline = false;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, route() {
    if (offline) throw new Error('fixture offline');
  } });
  ui.type('断开时仍保留的草稿'); offline = true; await ui.poll();
  assert.equal(ui.get('message').disabled, true); assert.equal(ui.get('message').value, '断开时仍保留的草稿');
  assert.equal(ui.document.getElementById('write-reply'), null); assert.equal(ui.posts().length, 0);
});

test('compact runtime cards retain full partner identity and independent quota buttons without changing default model values', async () => {
  const options = runtimeOptionsFixture(); options.partners.claude.models[0].name = 'Opus 5.5'; options.partners.claude.models[0].resolved_model = 'claude-opus-5-5';
  options.partners.claude.models.push({ id: 'default', name: 'Default (recommended)', resolved_model: 'claude-opus-5-5', efforts: ['low', 'high'] });
  const agents = catalog.slice(0, 2).map((agent) => ({ ...agent, name: agent.id === 'claude' ? 'Claude Code 工作账号' : 'Codex 本地工作账号' }));
  const ui = await browser({ agents, route(request) {
    if (request.path === '/api/runtime-options') return options;
    if (request.path === '/api/rooms' && request.method === 'POST') return snapshot({ agents });
  } });
  await openRuntimeSettings(ui); selectRuntime(ui, 'runtime-model-claude', 'default');
  const shortcut = ui.get('runtime-shortcut-claude'); const quota = ui.get('composer-usage-claude');
  assert.equal(descendants(shortcut).find((node) => node.classList.contains('shortcut-name')).textContent, 'Claude Code 工作账号');
  assert.equal(descendants(shortcut).find((node) => node.classList.contains('shortcut-model')).textContent, 'Opus 5.5（默认）');
  assert.equal(descendants(shortcut).find((node) => node.classList.contains('shortcut-effort')).textContent, '默认 · 全能');
  assert.match(shortcut.getAttribute('aria-label'), /Claude Code 工作账号的模型与思考强度：默认推荐（Opus 5.5），默认强度/);
  assert.equal(shortcut.parentNode, quota.parentNode); assert.notEqual(shortcut.id, quota.id); assert.equal(quota.type, 'button');
  assert.match(quota.getAttribute('aria-label'), /账号额度/);
  selectRuntime(ui, 'runtime-effort-claude', 'high'); await ui.click('runtime-settings-close');
  ui.type('使用刚才显式选择的默认模型'); await ui.submit();
  assert.equal(ui.posts().length, 1); assert.equal(ui.posts()[0].body.runtime_settings.members.claude.model, 'default');
  assert.equal(ui.posts()[0].body.runtime_settings.members.claude.effort, 'high');
});

test('sidebar work motion uses the room list authority for unselected tasks and keeps every card and status node stable', async () => {
  const statuses = ['working', 'queued', 'idle', 'idle', 'idle', 'idle'];
  const rooms = statuses.map((status, index) => ({ id: `00000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`, goal: `任务${index}`, status: 'running', lifecycle_state: 'active', updated_at: 1000,
    objective_status: index === 3 ? 'paused' : index === 4 ? 'needs_user' : index === 5 ? 'complete' : 'running',
    work_activity: { state: status, working_jobs: status === 'working' ? 1 : 0, queued_jobs: status === 'queued' ? 1 : 0, draining: false } }));
  const ui = await browser({ rooms, selectSaved: false, executionPolicySupport: true });
  const cards = [...ui.get('rooms').children]; const statusNodes = cards.map((card) => card.lastChild.firstChild);
  assert.deepEqual(cards.map((card) => card.lastChild.firstChild.firstChild.hidden), [false, true, true, true, true, true]);
  assert.equal(statusNodes[1].textContent, '等待开始');
  assert.match(statusNodes[0].getAttribute('aria-label'), /正在工作/);
  cards[0].focus(); await ui.poll();
  assert.equal(ui.document.activeElement, cards[0]); assert.equal(ui.get('rooms').firstChild, cards[0]); assert.equal(cards[0].lastChild.firstChild, statusNodes[0]);
  rooms[0].objective_status = 'paused'; rooms[0].work_activity.draining = true; await ui.poll();
  assert.equal(statusNodes[0].textContent, '正在暂停'); assert.equal(statusNodes[0].firstChild.hidden, false);
  assert.match(statusNodes[0].getAttribute('aria-label'), /正在结束当前处理/);
  rooms[0].work_activity = { state: 'idle', working_jobs: 0, queued_jobs: 0, draining: false }; await ui.poll();
  assert.equal(statusNodes[0].textContent, '已暂停'); assert.equal(statusNodes[0].firstChild.hidden, true);
  assert.equal(ui.requests.some((request) => /\/api\/rooms\/[^/?]+(?:\/state)?$/.test(request.path)), false, 'sidebar motion never polls every room snapshot');
  assert.equal(ui.posts().length, 0);
});
test('sidebar work motion stops immediately on a failed connection and never infers busy work on an old host', async () => {
  const working = { id: roomId, goal: '真实工作任务', status: 'running', objective_status: 'running', lifecycle_state: 'active', work_activity: { state: 'working', working_jobs: 1, queued_jobs: 0, draining: false } };
  const old = { ...working, id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', goal: '仅目标状态没有工作摘要' }; delete old.work_activity;
  let offline = false;
  const ui = await browser({ rooms: [working, old], selectSaved: false, executionPolicySupport: true, route() { if (offline) throw new Error('fixture offline'); } });
  const indicator = ui.get('rooms').children[0].lastChild.firstChild.firstChild;
  assert.equal(indicator.hidden, false); assert.equal(indicator.classList.contains('is-working'), true);
  assert.equal(ui.get('rooms').children[1].lastChild.firstChild.firstChild.hidden, true);
  offline = true; await ui.poll(); assert.equal(indicator.hidden, true); assert.equal(indicator.classList.contains('is-working'), false);
  assert.equal(indicator.getAttribute('aria-hidden'), 'true');
  assert.match(fs.readFileSync(path.join(assetRoot, 'style.css'), 'utf8'), /prefers-reduced-motion: reduce[\s\S]*room-work-spinner[\s\S]*animation: none/);
  assert.equal(ui.posts().length, 0);
});

test('model setting notes distinguish submitted pending settings from a new unsent choice without adding a reply approval step', async () => {
  const saved = withRuntimeConfiguration(withNaturalContinuation(objectiveSnapshot({ status: 'running' })), { pending: true });
  const actual = structuredClone(saved.current_run.runtime_settings); const options = runtimeOptionsFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, naturalRuntimeSupport: true, route(request) {
    if (request.path === '/api/runtime-options' || request.path.endsWith('/runtime/refresh')) return options;
  } });
  await ui.click('runtime-shortcut-claude'); assert.match(ui.get('runtime-settings-note').textContent, /设置已提交.*后续调用.*当前调用不变/);
  selectRuntime(ui, 'runtime-model-claude', 'opus'); selectRuntime(ui, 'runtime-effort-claude', 'high');
  assert.match(ui.get('runtime-settings-note').textContent, /发送消息或点击“继续”后.*后续调用.*当前调用不变/);
  assert.deepEqual(saved.current_run.runtime_settings, actual);
  assert.equal(ui.posts().filter((request) => !request.path.endsWith('/runtime/refresh')).length, 0);
});
function hostResultInterruptionFixture() {
  const saved = needsReplyFixture(); saved.objective.status = 'interrupted'; saved.objective.interruption_kind = 'host_result_validation';
  saved.objective.stop_reason = 'invalid_text'; saved.collaboration.outcome.state = 'interrupted';
  return saved;
}
test('a typed host-result interruption shows one existing continuation and folds the preserved old user question without requesting an invalid-text answer', async () => {
  const saved = hostResultInterruptionFixture(); const question = saved.objective.next_step.question;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.endsWith('/messages')) return saved;
  } });
  const result = ui.get('collaboration-outcome'); const history = ui.document.querySelector('.technical-interruption-history');
  assert.equal(result.firstChild.textContent, '处理遇到技术问题'); assert.match(result.textContent, /结果提交未通过格式校验.*继续后由伙伴重新提交并核对/);
  assert.equal(history.open, false); assert.ok(history.textContent.includes(question));
  assert.equal(result.textContent.split(question).length - 1, 1);
  assert.equal(result.children.some((node) => node.classList.contains('outcome-question')), false);
  assert.equal(ui.document.getElementById('write-reply'), null); assert.equal(ui.document.getElementById('clarify-next-step'), null);
  assert.equal(ui.get('continue-current').textContent, '继续'); assert.equal(ui.get('continue-current').disabled, false);
  assert.equal(descendants(result).filter((node) => node.tagName === 'BUTTON' && node.textContent === '继续').length, 1);
  assert.equal(ui.document.querySelector('.delivery-content').open, false); assert.equal(ui.posts().length, 0);
  await ui.click('continue-current'); const post = ui.posts()[0];
  assert.equal(post.path, `/api/rooms/${roomId}/messages`); assert.equal(post.body.text, '继续'); assert.equal(post.body.intent, 'continue');
  assert.deepEqual(post.body.binding, saved.continuation.binding);
});
test('invalid-text strings or an interruption marker without interrupted status never reclassify a real user question', async () => {
  for (const marker of [null, 'host_result_validation']) {
    const saved = needsReplyFixture(); saved.objective.stop_reason = 'invalid_text'; if (marker) saved.objective.interruption_kind = marker;
    const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
    assert.ok(ui.get('events').textContent.includes(saved.objective.next_step.question));
    assert.equal(ui.document.querySelector('.technical-interruption-history'), null);
    assert.equal(ui.document.getElementById('write-reply'), null); assert.equal(ui.posts().length, 0);
  }
});

function commonNativeRuntimeOptions() {
  const options = runtimeOptionsFixture(); options.permissions = [
    { id: 'plan', name: 'Plan', legacy: false }, { id: 'full_access', name: 'Full Access', legacy: false },
    { id: 'read_only', name: '只读', legacy: true }, { id: 'workspace_write', name: '副本内修改', legacy: true },
  ]; return options;
}
test('new native work exposes the common Plan and Full Access modes and defaults to Plan without changing model choices', async () => {
  const project = nativeProjectFixture(); const options = commonNativeRuntimeOptions();
  const saved = objectiveSnapshot({ status: 'running' }); saved.room.mode = 'native_project';
  const ui = await browser({ agents: catalog.slice(0, 2), projects: [project], nativeProjectSupport: true, route(request) {
    if (request.path === '/api/runtime-options') return options;
    if (request.path === '/api/rooms' && request.method === 'POST') return saved;
  } });
  selectRuntime(ui, 'project-picker', project.id); selectRuntime(ui, 'project-workflow', 'native_project'); await settle(); await openRuntimeSettings(ui);
  assert.deepEqual(ui.get('runtime-permission').children.map((node) => node.value), ['plan', 'full_access']);
  assert.equal(ui.get('runtime-permission').value, 'plan');
  selectRuntime(ui, 'runtime-model-claude', 'opus'); selectRuntime(ui, 'runtime-effort-claude', 'high');
  await ui.click('runtime-settings-close'); await ui.poll(); await openRuntimeSettings(ui);
  assert.equal(ui.get('runtime-permission').value, 'plan'); assert.equal(ui.get('runtime-model-claude').value, 'opus');
  assert.doesNotMatch(ui.get('runtime-settings-note').textContent, /不会自动合回|不自动合回|不能.*原项目/);
  await ui.click('runtime-settings-close'); ui.type('按默认规划权限处理虚构任务'); await ui.submit();
  assert.equal(ui.posts()[0].body.runtime_settings.permission, 'plan');
  assert.equal(ui.posts()[0].body.runtime_settings.members.claude.model, 'opus'); assert.equal(ui.posts()[0].body.runtime_settings.members.claude.effort, 'high');
});
test('native legacy permissions remain exact historical selections until the user explicitly chooses a common mode', async () => {
  for (const permission of ['read_only', 'workspace_write', 'full_access']) {
    const saved = withRuntimeConfiguration(withNaturalContinuation(objectiveSnapshot({ status: 'running' }))); saved.room.mode = 'native_project';
    saved.room.runtime_settings.permission = permission; saved.current_run.runtime_settings.permission = permission; saved.runtime_configuration.desired.runtime_settings.permission = permission;
    const original = structuredClone(saved.current_run.runtime_settings);
    const ui = await browser({ rooms: [saved.room], saved, nativeProjectSupport: true, executionPolicySupport: true, naturalContinuationSupport: true, naturalRuntimeSupport: true, route(request) {
      if (request.path === '/api/runtime-options') return commonNativeRuntimeOptions();
      if (request.path.endsWith('/messages')) return saved;
    } });
    await openRuntimeSettings(ui); assert.equal(ui.get('runtime-permission').value, permission);
    const values = ui.get('runtime-permission').children.map((node) => node.value);
    assert.deepEqual(values, permission === 'full_access' ? ['plan', 'full_access'] : ['plan', 'full_access', permission]);
    if (permission !== 'full_access') assert.match(ui.get('runtime-permission').children.at(-1).textContent, /历史设置/);
    await ui.poll(); assert.equal(ui.get('runtime-permission').value, permission); assert.deepEqual(saved.current_run.runtime_settings, original);
    selectRuntime(ui, 'runtime-permission', 'plan'); await ui.click('runtime-settings-close'); ui.type('使用我明确选择的原生Plan'); await ui.submit();
    assert.equal(ui.posts()[0].body.runtime_settings.permission, 'plan'); assert.deepEqual(saved.current_run.runtime_settings, original);
  }
});
test('non-native editing retains its original permission choices even when the server supports common native modes', async () => {
  const ui = await browser({ route(request) { if (request.path === '/api/runtime-options') return commonNativeRuntimeOptions(); } });
  await openRuntimeSettings(ui);
  assert.deepEqual(ui.get('runtime-permission').children.map((node) => node.value), ['read_only', 'workspace_write']);
  assert.equal(ui.get('runtime-permission').value, 'workspace_write'); assert.equal(ui.posts().length, 0);
});


test('mention recipient filters current members, removes only the selected token and submits the real target once', async () => {
  const saved = withNaturalContinuation(restartSnapshot());
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true,
    route(request) { if (request.path.endsWith('/messages')) return saved; } });
  assert.match(ui.get('recipient-button').textContent, /全体/);
  ui.type('保留正文 @Codex');
  assert.equal(ui.get('recipient-menu').hidden, false);
  assert.deepEqual(ui.get('recipient-menu').children.map((node) => node.textContent), ['Codex']);
  ui.get('message').fire('keydown', { key: 'Enter' });
  assert.equal(ui.get('message').value, '保留正文 ');
  assert.match(ui.get('recipient-button').textContent, /Codex/);
  assert.equal(ui.posts().length, 0, 'Enter selects a recipient and does not send');
  await ui.submit();
  const sent = ui.posts().filter((request) => request.path.includes('/messages'));
  assert.equal(sent.length, 1); assert.equal(sent[0].body.target, 'codex'); assert.equal(sent[0].body.text, '保留正文');
});

test('mention picker supports keyboard, Chinese names and IME without parsing emails or unknown mentions', async () => {
  const saved = withNaturalContinuation(restartSnapshot()); saved.members[1].name = '代码伙伴';
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  ui.type('mail@example.com'); assert.equal(ui.get('recipient-menu').hidden, true);
  ui.type('普通 @陌生人'); assert.equal(ui.get('recipient-menu').hidden, false);
  ui.get('message').fire('keydown', { key: 'Enter' }); assert.equal(ui.get('message').value, '普通 @陌生人');
  assert.equal(ui.posts().length, 0);
  ui.get('message').fire('keydown', { key: 'Escape' }); assert.equal(ui.get('recipient-menu').hidden, true);
  ui.type('正文 @代码'); assert.match(ui.get('recipient-menu').textContent, /代码伙伴/);
  ui.get('message').fire('compositionstart');
  ui.get('message').fire('keydown', { key: 'Enter', isComposing: true }); await ui.submit();
  assert.equal(ui.posts().length, 0); assert.equal(ui.get('message').value, '正文 @代码');
  ui.get('message').fire('compositionend'); ui.get('message').fire('keydown', { key: 'Enter' });
  assert.match(ui.get('recipient-button').textContent, /代码伙伴/);
  await ui.click('recipient-clear'); assert.match(ui.get('recipient-button').textContent, /全体/);
  await ui.click('recipient-button'); ui.get('message').fire('keydown', { key: 'ArrowDown' });
  ui.get('message').fire('keydown', { key: 'Enter' }); assert.match(ui.get('recipient-button').textContent, /Claude/);
  assert.equal(ui.get('message').value, '正文 '); assert.equal(ui.posts().length, 0);
});

test('recipient and body draft survive room navigation and same-origin reload without broadcasting a removed member', async () => {
  const saved = withNaturalContinuation(restartSnapshot()); const storage = new Map();
  const options = { rooms: [saved.room], saved, storage, executionPolicySupport: true, naturalContinuationSupport: true };
  const ui = await browser(options); ui.type('这条要给 Codex'); await ui.choose('codex');
  await ui.click('new-room'); ui.get('rooms').firstChild.fire('click'); await settle();
  assert.equal(ui.get('message').value, '这条要给 Codex'); assert.match(ui.get('recipient-button').textContent, /Codex/);
  const reopened = await browser(options);
  assert.equal(reopened.get('message').value, '这条要给 Codex'); assert.match(reopened.get('recipient-button').textContent, /Codex/);
  saved.members = saved.members.filter((member) => member.id !== 'codex'); await reopened.poll();
  assert.equal(reopened.get('send').disabled, true); assert.match(reopened.get('recipient-note').textContent, /不会改发全体/);
  await reopened.submit(); assert.equal(reopened.posts().length, 0);
  await reopened.click('recipient-clear'); assert.equal(reopened.get('send').disabled, false);
  assert.equal(reopened.get('message').value, '这条要给 Codex');
});

test('recipient stays immutable in an uncertain pending message across reload', async () => {
  const saved = withNaturalContinuation(restartSnapshot()); const storage = new Map();
  const options = { rooms: [saved.room], saved, storage, executionPolicySupport: true, naturalContinuationSupport: true,
    route(request) { if (request.path.endsWith('/messages')) throw new Error('fictional disconnect after delivery'); } };
  const ui = await browser(options); ui.type('定向补充'); await ui.choose('codex'); await ui.submit();
  const sent = ui.posts().find((request) => request.path.endsWith('/messages'));
  assert.equal(sent.body.target, 'codex');
  const reopened = await browser(options);
  assert.match(reopened.get('recipient-button').textContent, /Codex/); assert.equal(reopened.get('message').value, '定向补充');
  assert.equal(reopened.posts().length, 0); assert.equal(savedOutbox(storage).request.body.request_id, sent.body.request_id);
  assert.equal(savedOutbox(storage).request.body.target, 'codex');
});


test('mention selection preserves surrounding text and escaped picker leaves email and ordinary at text literal', async () => {
  const saved = withNaturalContinuation(restartSnapshot());
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true,
    route(request) { if (request.path.endsWith('/messages')) return saved; } });
  ui.type('前文 @Codex 后文 mail@example.com');
  ui.get('message').setSelectionRange(9, 9); ui.get('message').fire('click');
  assert.equal(ui.get('recipient-menu').hidden, false);
  ui.get('message').fire('keydown', { key: 'Enter' });
  assert.equal(ui.get('message').value, '前文  后文 mail@example.com');
  await ui.poll(); assert.match(ui.get('recipient-button').textContent, /Codex/);
  await ui.click('recipient-clear'); ui.type('正文 @不是收件人 mail@example.com');
  ui.get('message').fire('keydown', { key: 'Escape' }); await ui.submit();
  const sent = ui.posts().find((request) => request.path.endsWith('/messages'));
  assert.equal(sent.body.target, 'all'); assert.equal(sent.body.text, '正文 @不是收件人 mail@example.com');
});

function mediaFixture() {
  const saved = completedSuggestionFixture();
  saved.team.tasks = [{ id: 'M1', result_version: 1, status: 'accepted', result: '交付附件' }];
  saved.events.push({ seq: 50, kind: 'team_command', command: 'complete', task_id: 'M1', run_id: saved.current_run.id, sender: 'claude' });
  const assets = [
    { id: '1'.repeat(64), name: '演示 录像.mp4', path: '交付/演示 录像.mp4', kind: 'video', mime: 'video/mp4' },
    { id: '2'.repeat(64), name: '截图.png', path: '交付/截图.png', kind: 'image', mime: 'image/png' },
    { id: '3'.repeat(64), name: '解说.mp3', path: '交付/解说.mp3', kind: 'audio', mime: 'audio/mpeg' },
    { id: '4'.repeat(64), name: '附件.bin', path: '交付/附件.bin', kind: 'file', mime: 'application/octet-stream' },
  ].map((asset) => ({ ...asset, bytes: 8192, sha256: asset.id, available: true, reference_aliases: [`/owned/project/${asset.path}`, `file:///owned/project/${encodeURI(asset.path)}`], source: { run_id: saved.current_run.id, task_id: 'M1', result_version: 1 }, verified: false, review_kind: null }));
  const route = (request) => {
    if (request.path === `/api/rooms/${roomId}/media`) return { assets };
    const found = request.path.match(/\/media\/([0-9a-f]{64})\/access$/);
    if (found) { const asset = assets.find((item) => item.id === found[1]); return { asset_id: asset.id, sha256: asset.sha256, url: `/room-media/opaque-${asset.id}`, expires_at: Date.now() / 1000 + 1200, mime: asset.mime, bytes: asset.bytes, name: asset.name }; }
    if (request.path.startsWith(`/api/rooms/${roomId}/media/resolve?`)) return { error: 'media_not_found' };
  };
  return { saved, assets, route };
}
function mediaViewButton(ui, id) {
  const card = ui.get('media-items').children.find((node) => node.dataset.mediaId === id);
  assert.ok(card); return card.children.find((node) => node.classList.contains('media-open'));
}
test('host-bound media is visible outside code lists and native video survives polls without autoplay or duplicate access', async () => {
  const { saved, assets, route } = mediaFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, mediaSupport: true, route });
  assert.ok(html.indexOf('id="room-media"') > html.indexOf('id="collaboration-outcome"'), 'default bottom delivery view reaches the media after the outcome');
  assert.equal(ui.get('room-media').hidden, false); assert.equal(ui.get('room-media').getAttribute('aria-label'), '图片与视频'); assert.match(ui.get('room-media').textContent, /演示 录像/);
  assert.equal(ui.document.querySelector('.media-thumbnail').src, `/room-media/opaque-${assets[1].id}`);
  await ui.choose('codex'); ui.type('保留这段草稿'); mediaViewButton(ui, assets[0].id).fire('click'); await settle();
  const video = descendants(ui.get('reader-body')).find((node) => node.tagName === 'VIDEO');
  assert.ok(video); assert.equal(video.controls, true); assert.equal(video.autoplay, false); assert.equal(video.preload, 'metadata');
  assert.match(video.src, /^\/room-media\//); assert.equal(video.src.includes('fictional-test-connection'), false);
  const access = ui.posts().filter((request) => request.path.endsWith('/access')); assert.equal(access.length, 2);
  await ui.poll(); assert.equal(descendants(ui.get('reader-body')).find((node) => node.tagName === 'VIDEO'), video);
  assert.equal(ui.posts().filter((request) => request.path.endsWith('/access')).length, 2);
  assert.equal(ui.posts().some((request) => request.path.endsWith('/messages')), false);
  let paused = 0; let unloaded = 0; video.pause = () => paused++; video.load = () => unloaded++;
  ui.get('reader-body').fire('keydown', { key: 'Escape' }); await settle();
  assert.equal(paused, 1); assert.equal(unloaded, 1); assert.equal(ui.get('message').value, '保留这段草稿');
  assert.match(ui.get('recipient-button').textContent, /Codex/);
});
test('media downloads use only scoped resource URLs and audio or unknown files never execute as documents', async () => {
  const { saved, assets, route } = mediaFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, mediaSupport: true, route });
  mediaViewButton(ui, assets[2].id).fire('click'); await settle();
  assert.equal(descendants(ui.get('reader-body')).some((node) => node.tagName === 'AUDIO' && node.controls), true);
  await ui.click('media-download'); assert.equal(ui.document.downloads[0].filename, '解说.mp3');
  assert.equal(ui.get('media-other').open, false);
  assert.match(ui.get('media-other-label').textContent, /其他附件 · 1/);
  descendants(ui.get('media-file-items')).find((node) => node.tagName === 'BUTTON').fire('click'); await settle();
  assert.equal(ui.document.downloads.at(-1).filename, '附件.bin');
  assert.equal(descendants(ui.get('reader-body')).some((node) => ['IFRAME', 'OBJECT', 'EMBED'].includes(node.tagName)), false);
  assert.equal(ui.posts().some((request) => request.path.endsWith('/messages')), false);
});
test('Markdown and chat local links including angle-bracket spaces resolve only through the host attachment binding', async () => {
  const { saved, assets, route } = mediaFixture();
  saved.events.push({ seq: 55, kind: 'message', sender: 'claude', recipient: 'all', text: `[看录像](</owned/project/${assets[0].path}>) 与 [外部](https://example.com)`, created_at: 1000 });
  saved.project.files[0].content = `# 报告\n[看录像](</owned/project/${assets[0].path}>)\n![外网图](https://example.com/private.png)\n[未知文件](../unknown.mp4)\n[危险](javascript:alert)`;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, mediaSupport: true, route });
  const chat = descendants(ui.get('events')).find((node) => node.classList.contains('media-reference'));
  assert.ok(chat); assert.equal(chat.textContent, '看录像'); chat.fire('click'); await settle();
  assert.equal(descendants(ui.get('reader-body')).some((node) => node.tagName === 'VIDEO'), true);
  await ui.click('reader-back'); deliveryButton(ui, 'first.md').fire('click'); await settle();
  const references = descendants(ui.get('reader-body')).filter((node) => node.classList.contains('media-reference'));
  assert.equal(references.length, 2); assert.equal(references[0].textContent, '看录像');
  assert.equal(descendants(ui.get('reader-body')).some((node) => node.tagName === 'IMG' && /^https:/.test(node.src || '')), false);
  references[1].fire('click'); await settle(); assert.match(ui.get('notice').textContent, /暂未关联/);
  assert.equal(ui.posts().some((request) => request.path.endsWith('/messages')), false);
});
test('an untrusted media access URL is never assigned to a player and late access cannot reopen another chat', async () => {
  const { saved, assets, route } = mediaFixture(); let unsafe = true; const late = deferred();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, mediaSupport: true, route(request) {
    if (request.path.endsWith(`/media/${assets[0].id}/access`)) return unsafe ? { asset_id: assets[0].id, sha256: assets[0].sha256, url: 'https://evil.example/media', expires_at: Date.now() / 1000 + 1200 } : late.promise;
    return route(request);
  } });
  mediaViewButton(ui, assets[0].id).fire('click'); await settle();
  assert.match(ui.get('reader-body').textContent, /地址尚未确认/); assert.equal(descendants(ui.get('reader-body')).some((node) => node.tagName === 'VIDEO'), false);
  await ui.click('reader-back'); unsafe = false; mediaViewButton(ui, assets[0].id).fire('click'); await settle();
  await ui.click('new-room'); late.resolve(route({ path: `/api/rooms/${roomId}/media/${assets[0].id}/access` })); await settle();
  assert.equal(ui.get('delivery-reader').hidden, true); assert.equal(ui.get('room-media').hidden, true);
  assert.equal(ui.posts().some((request) => request.path.endsWith('/messages')), false);
});

test('media stays before ordinary attachments when the list exceeds the first page and explicit retry renews a failed resource', async () => {
  const { saved, assets, route } = mediaFixture(); let videoTickets = 0;
  const files = Array.from({ length: 10 }, (_, index) => ({ ...assets[3], id: String(index + 5).padStart(64, '0'), name: `source-${index}.txt` }));
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, mediaSupport: true, route(request) {
    if (request.path === `/api/rooms/${roomId}/media`) return { assets: [...files, ...assets] };
    if (request.path.endsWith(`/media/${assets[0].id}/access`)) return { ...route(request), url: `/room-media/video-${++videoTickets}` };
    return route(request);
  } });
  const cards = ui.get('media-items').children;
  assert.equal(cards.length, 3); assert.deepEqual(cards.slice(0, 3).map((node) => node.dataset.mediaId), assets.slice(0, 3).map((asset) => asset.id));
  assert.equal(ui.get('media-more').hidden, true);
  assert.equal(ui.get('media-other').open, false); assert.match(ui.get('media-other-label').textContent, /其他附件 · 11/);
  ui.get('media-other').open = true; await ui.poll(); assert.equal(ui.get('media-other').open, true);
  assert.equal(ui.get('media-items').children.length, 3);
  mediaViewButton(ui, assets[0].id).fire('click'); await settle();
  const first = descendants(ui.get('reader-body')).find((node) => node.tagName === 'VIDEO');
  first.fire('error');
  descendants(ui.get('reader-body')).find((node) => node.tagName === 'BUTTON' && node.textContent === '重新读取附件').fire('click'); await settle();
  const renewed = descendants(ui.get('reader-body')).find((node) => node.tagName === 'VIDEO');
  assert.notEqual(renewed, first); assert.equal(renewed.src, '/room-media/video-2'); assert.equal(renewed.autoplay, false);
  assert.equal(videoTickets, 2); assert.equal(ui.posts().some((request) => request.path.endsWith('/messages')), false);
});

test('inline local image references use scoped resources and a host without media capability keeps its original reader behavior', async () => {
  const { saved, assets, route } = mediaFixture();
  saved.project.files[0].content = `![本地截图](<${assets[1].reference_aliases[1]}>)`;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, mediaSupport: true, route });
  deliveryButton(ui, 'first.md').fire('click'); await settle();
  const image = descendants(ui.get('reader-body')).find((node) => node.tagName === 'IMG');
  assert.ok(image); assert.equal(image.src, `/room-media/opaque-${assets[1].id}`);
  const old = await browser({ rooms: [saved.room], saved, route });
  deliveryButton(old, 'first.md').fire('click'); await settle();
  assert.equal(old.get('room-media').hidden, true);
  assert.equal(old.requests.some((request) => request.path.includes('/media')), false);
  assert.equal(descendants(old.get('reader-body')).some((node) => node.tagName === 'IMG'), false);
});


test('late media follows only the still-bottom timeline and never pulls an active history reader down', async () => {
  for (const readingHistory of [false, true]) {
    const { saved, assets, route } = mediaFixture(); const listing = deferred();
    const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, mediaSupport: true, route(request) {
      if (request.path === `/api/rooms/${roomId}/media`) return listing.promise;
      return route(request);
    } });
    const timeline = ui.get('timeline');
    Object.defineProperty(timeline, 'scrollHeight', { get: () => 1000 + ui.get('media-items').children.length * 200, configurable: true });
    timeline.scrollTop = readingHistory ? 100 : 400;
    listing.resolve({ assets }); await settle();
    assert.equal(timeline.scrollTop, readingHistory ? 100 : 1600);
    assert.equal(ui.get('room-media').hidden, false);
    assert.equal(ui.posts().some((request) => request.path.endsWith('/messages')), false);
  }
});

test('current media stays visible while carried or unproven references fold into a persistent previous attachments row', async () => {
  const { saved, assets, route } = mediaFixture();
  assets[0].source = { ...assets[0].source, run_id: '66666666-6666-4666-8666-666666666666' };
  assets[2].source = { run_id: saved.current_run.id, document_path: 'old-report.md' };
  const options = { rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, mediaSupport: true, route };
  const ui = await browser(options);
  assert.deepEqual(ui.get('media-items').children.map((node) => node.dataset.mediaId), [assets[1].id]);
  assert.equal(ui.get('media-history').open, false); assert.equal(ui.get('media-history-label').textContent, '此前附件 · 2');
  assert.deepEqual(ui.get('media-history-items').children.map((node) => node.dataset.mediaId), [assets[0].id, assets[2].id]);
  ui.get('media-history').open = true; ui.get('media-history').fire('toggle'); await ui.poll(); assert.equal(ui.get('media-history').open, true);
  const reopened = await browser({ ...options, storage: ui.storage }); assert.equal(reopened.get('media-history').open, true);
  await ui.click('new-room'); ui.get('rooms').firstChild.fire('click'); await settle(); assert.equal(ui.get('media-history').open, true);
  assert.equal(ui.posts().some((request) => request.path.endsWith('/messages')), false);
});

test('a later user supplement folds the displayed delivery once and new host completed work becomes visible without reopening old attachments', async () => {
  const { saved, assets, route } = mediaFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, mediaSupport: true, route });
  assert.equal(ui.get('media-items').children.length, 3);
  saved.events.push({ seq: 60, kind: 'message', sender: 'user', recipient: 'codex', text: '接着整理下一项', created_at: 60 });
  await ui.poll(); assert.equal(ui.get('media-items').children.length, 0); assert.equal(ui.get('media-heading').hidden, true);
  assert.equal(ui.get('media-history').open, false); assert.equal(ui.get('media-history-label').textContent, '此前附件 · 4');
  ui.get('media-history').open = true; ui.get('media-history').fire('toggle'); await ui.poll(); assert.equal(ui.get('media-history').open, true);
  assets[1].source = { run_id: saved.current_run.id, task_id: 'M2', result_version: 1 };
  saved.team.tasks.push({ id: 'M2', result_version: 1, result: '新截图', status: 'accepted' });
  saved.events.push({ seq: 70, kind: 'team_command', sender: 'claude', command: 'complete', task_id: 'M2', run_id: saved.current_run.id });
  await ui.poll(); assert.deepEqual(ui.get('media-items').children.map((node) => node.dataset.mediaId), [assets[1].id]);
  assert.equal(ui.get('media-history').open, true);
});

test('an accepted carry with only a relabeled run has no new completion proof and stays folded after refresh', async () => {
  const { saved, assets, route } = mediaFixture(); saved.events = saved.events.filter((event) => event.kind !== 'team_command');
  const options = { rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, mediaSupport: true, route };
  const ui = await browser(options);
  assert.equal(ui.get('media-items').children.length, 0); assert.equal(ui.get('media-history').open, false);
  assert.equal(ui.get('media-history-label').textContent, '此前附件 · 4');
  const reopened = await browser({ ...options, storage: ui.storage });
  assert.equal(reopened.get('media-history').open, false); assert.equal(reopened.get('media-items').children.length, 0);
});

test('a carried video becomes current at final delivery after technical continuation and folds only after a later supplement', async () => {
  const { saved, assets, route } = mediaFixture();
  const originalRun = saved.current_run.id; const carryRun = 'abababab-abab-4bab-8bab-abababababab';
  const video = assets[0]; video.name = 'capture-7f90.mp4'; video.path = 'proof/capture-7f90.mp4';
  saved.current_run.id = carryRun; saved.team.run_id = carryRun; saved.objective.next_step.binding.run_id = carryRun;
  saved.objective.status = 'running'; saved.team.phase = 'working';
  saved.events.push({ seq: 60, kind: 'objective_decision', sender: 'user', text: '继续处理刚才的提交问题', run_id: carryRun });
  const options = { rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, mediaSupport: true, route };
  const ui = await browser(options);
  assert.equal(video.source.run_id, originalRun); assert.equal(ui.get('media-items').children.length, 0);
  assert.equal(ui.get('media-history').open, false);
  saved.events.push({ seq: 70, kind: 'team_command', command: 'review', task_id: 'M1', sender: 'codex', run_id: carryRun });
  await ui.poll(); assert.equal(ui.get('media-items').children.length, 0, 'a review event alone is not a fabricated publication');
  saved.objective.status = 'complete'; saved.team.phase = 'ready_for_user';
  saved.events.push({ seq: 80, kind: 'objective_checkpoint_agreed', sender: 'system', run_id: carryRun });
  video.source.delivery = { run_id: carryRun, seq: 80 };
  await ui.poll();
  assert.deepEqual(ui.get('media-items').children.map((node) => node.dataset.mediaId), [video.id]);
  assert.equal(ui.get('media-history').open, false); assert.equal(video.source.run_id, originalRun);
  mediaViewButton(ui, video.id).fire('click'); await settle();
  assert.equal(descendants(ui.get('reader-body')).some((node) => node.tagName === 'VIDEO'), true);
  await ui.click('reader-back');
  const delivered = await browser({ ...options, storage: ui.storage });
  assert.deepEqual(delivered.get('media-items').children.map((node) => node.dataset.mediaId), [video.id]);
  saved.events.push({ seq: 90, kind: 'message', sender: 'user', text: '现在再整理另一项', recipient: 'all', run_id: carryRun });
  await ui.poll(); assert.equal(ui.get('media-items').children.length, 0); assert.equal(ui.get('media-history').open, false);
  ui.get('media-history').open = true; ui.get('media-history').fire('toggle'); await ui.poll();
  const reopened = await browser({ ...options, storage: ui.storage }); assert.equal(reopened.get('media-history').open, true);
  assert.equal(ui.posts().some((request) => request.path.endsWith('/messages')), false);
});

test('an explicit publication in another run or before the latest supplement never makes old media current', async () => {
  const { saved, assets, route } = mediaFixture();
  saved.events.push({ seq: 90, kind: 'message', sender: 'user', text: '新补充', recipient: 'all' });
  assets[0].source.delivery = { run_id: 'abababab-abab-4bab-8bab-abababababab', seq: 100 };
  assets[1].source.delivery = { run_id: saved.current_run.id, seq: 80 };
  assets[2].source.delivery = { run_id: saved.current_run.id, seq: '100' };
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, mediaSupport: true, route });
  assert.equal(ui.get('media-items').children.length, 0); assert.equal(ui.get('media-history-label').textContent, '此前附件 · 4');
  assert.equal(ui.posts().some((request) => request.path.endsWith('/messages')), false);
});


test('a paged refresh with no user in the loaded tail retains the host supplement watermark and keeps prior delivery folded', async () => {
  const { saved: source, assets, route } = mediaFixture();
  const events = [{ seq: 90, kind: 'message', sender: 'user', text: '交付之后的补充' },
    ...Array.from({ length: 150 }, (_, index) => ({ seq: 100 + index, kind: 'job_finished', sender: 'claude', status: 'completed' }))];
  assets[0].source.delivery = { run_id: source.current_run.id, seq: 80 };
  const options = { rooms: [source.room], saved: pagedFixture(source, events), pagingSupport: true,
    executionPolicySupport: true, naturalContinuationSupport: true, mediaSupport: true,
    route(request) {
      if (request.path === `/api/rooms/${roomId}/state`) return pagedFixture(source, events);
      const page = eventPageRoute(request, events); if (page) return page;
      if (request.path === `/api/rooms/${roomId}/media`) return { assets, latest_user_seq: 90 };
      return route(request);
    } };
  const ui = await browser(options);
  assert.equal(ui.get('media-items').children.length, 0); assert.equal(ui.get('media-history').open, false);
  assert.equal(ui.requests.filter((request) => request.path.includes('/events?')).length, 1, 'only the normal tail page is loaded');
  assert.equal(ui.requests.some((request) => request.path.includes('before_seq=')), false, 'media does not fetch all old chat to guess a watermark');
  assert.doesNotMatch(ui.get('events').textContent, /交付之后的补充/);
  const refreshed = await browser({ ...options, storage: new Map() });
  assert.equal(refreshed.get('media-items').children.length, 0); assert.equal(refreshed.get('media-history').open, false);
  ui.get('media-history').open = true; ui.get('media-history').fire('toggle'); await ui.poll();
  const preserved = await browser({ ...options, storage: ui.storage }); assert.equal(preserved.get('media-history').open, true);
  assets[0].source.delivery = { run_id: source.current_run.id, seq: 250 };
  events.push({ seq: 260, kind: 'message', sender: 'user', text: '后来另一条补充' });
  await ui.poll(); assert.equal(ui.get('media-items').children.length, 0, 'loaded newer user seq wins over an older host response');
  assert.equal(ui.posts().some((request) => request.path.endsWith('/messages')), false);
});

function projectCodeFixture() {
  return { workspaces: [{ id: 'project', kind: 'project', label: '关联项目', path: '/projects/示例 项目' },
    { id: 'native:codex', kind: 'native', label: 'Codex 工作副本', path: '/private/workspaces/codex', provider: 'codex' }],
    workspace_id: 'project', observed_at: 1700000000, state: 'ready', reason: null, repository_root: '/projects/示例 项目',
    project_path: '.', branch: 'feature/demo', detached: false, unborn: false,
    uncommitted: { total: 23, staged: 2, unstaged: 18, untracked: 2, conflicted: 1 },
    files: Array.from({ length: 20 }, (_, index) => ({ path: `src/file-${index}.js`, index: ' ', worktree: 'M' })), files_truncated: true,
    last_commit: { hash: 'a'.repeat(40), short_hash: 'aaaaaaa', subject: '完成首版界面布局', committed_at: '2026-11-02T12:00:00Z' },
    upstream: { state: 'none', name: null, ahead: null, behind: null, scope: 'repository_local_refs' } };
}
test('project code is a compact first sidebar card with distinct workspace status and no Git queries on ordinary polling', async () => {
  const saved = completedSuggestionFixture(); const data = projectCodeFixture(); let reads = 0;
  data.last_commit.subject = '保留这段完整的长提交说明。'.repeat(40);
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, codeStatusSupport: true,
    route(request) { if (request.path.includes('/code-status')) { reads++; return data; } } });
  assert.ok(html.indexOf('id="code-status-card"') < html.indexOf('id="member-cards"'));
  assert.equal(ui.get('code-local-summary').textContent, '有 23 个文件未提交'); assert.equal(ui.get('code-status-details').open, false);
  assert.match(ui.get('code-status-branch').textContent, /feature\/demo/);
  assert.equal(ui.get('code-commit-subject').textContent, data.last_commit.subject); assert.equal(ui.get('code-commit-hash').textContent, 'aaaaaaa');
  assert.equal(ui.get('code-commit-full').textContent, `最近提交：${data.last_commit.subject}`);
  assert.equal(ui.get('code-upstream').textContent, '未设置跟踪分支'); assert.doesNotMatch(ui.get('code-upstream').textContent, /未推送 0/);
  assert.equal(ui.get('code-file-list').children.length, 20); assert.match(ui.get('code-files-more').textContent, /另有 3 个文件/);
  assert.match(ui.get('code-directory').textContent, /\/projects\/示例 项目/);
  await ui.choose('codex'); ui.type('正文和收件人都保留'); ui.get('code-status-details').open = true;
  await ui.poll(); await ui.poll(); assert.equal(reads, 1); assert.equal(ui.get('code-status-details').open, true);
  data.uncommitted = { total: 0, staged: 0, unstaged: 0, untracked: 0, conflicted: 0 }; data.files = []; data.files_truncated = false;
  data.upstream = { state: 'ready', name: 'origin/main', ahead: 0, behind: 3, scope: 'repository_local_refs' };
  await ui.click('code-status-refresh'); assert.equal(reads, 2); assert.equal(ui.get('code-local-summary').textContent, '工作区干净');
  assert.match(ui.get('code-upstream').textContent, /领先 0.*落后 3.*基于本地记录/);
  assert.equal(ui.get('message').value, '正文和收件人都保留'); assert.match(ui.get('recipient-button').textContent, /Codex/);
  assert.equal(ui.posts().length, 0);
});
test('a late code workspace result cannot replace a newer unavailable scope or leave a false clean indicator', async () => {
  const saved = completedSuggestionFixture(); const data = projectCodeFixture(); const late = deferred(); let projectReads = 0;
  data.uncommitted.total = 0;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, codeStatusSupport: true,
    route(request) {
      if (!request.path.includes('/code-status')) return;
      const workspace = new URL(`http://fixture${request.path}`).searchParams.get('workspace');
      if (workspace === 'native:codex') return late.promise;
      return ++projectReads === 1 ? data : { ...data, state: 'unavailable', reason: 'directory_unavailable', uncommitted: null, files: [] };
    } });
  assert.equal(ui.get('code-local-summary').classList.contains('is-clean'), true);
  ui.get('code-workspace').value = 'native:codex'; ui.get('code-workspace').fire('change'); await settle();
  assert.match(ui.get('code-local-summary').textContent, /正在读取/);
  ui.get('code-workspace').value = 'project'; ui.get('code-workspace').fire('change'); await settle();
  assert.equal(ui.get('code-local-summary').textContent, '项目目录暂不可用');
  late.resolve({ ...data, workspace_id: 'native:codex', branch: 'wrong-late-branch' }); await settle();
  assert.equal(ui.get('code-workspace').value, 'project'); assert.equal(ui.get('code-local-summary').classList.contains('is-clean'), false);
  assert.doesNotMatch(ui.get('code-status-branch').textContent, /wrong-late/);
  await ui.click('new-room'); assert.equal(ui.get('code-status-card').hidden, true);
});
test('active work labels old code observations and its terminal transition refreshes Git exactly once', async () => {
  const saved = completedSuggestionFixture(); saved.current_run.status = 'working'; saved.objective.status = 'running';
  // Checked moments ago: results old enough to be called out are covered by their own test.
  const data = projectCodeFixture(); data.uncommitted.total = 0; data.observed_at = Math.floor(Date.now() / 1000) - 30; let reads = 0;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, codeStatusSupport: true,
    route(request) { if (request.path.includes('/code-status')) { reads++; return data; } } });
  assert.equal(ui.get('code-local-summary').textContent, '上次检查：工作区干净'); assert.equal(ui.get('code-local-summary').classList.contains('is-clean'), false);
  assert.match(ui.get('code-status-freshness').textContent, /任务进行中/);
  await ui.poll(); assert.equal(reads, 1);
  saved.current_run.status = 'stopped'; saved.room.work_activity = { state: 'working', draining: true };
  await ui.poll(); assert.equal(reads, 1); assert.equal(ui.get('code-local-summary').classList.contains('is-clean'), false);
  saved.room.work_activity.state = 'idle'; saved.current_run.status = 'complete'; saved.objective.status = 'complete'; await ui.poll(); assert.equal(reads, 2);
  await ui.poll(); assert.equal(reads, 2); assert.equal(ui.get('code-local-summary').textContent, '工作区干净');
  assert.equal(ui.requests.filter((request) => request.path.includes('/code-status')).every((request) => request.path.includes('refresh_remote=1')), true);
});
test('unbound and non Git workspaces never report zero changes and old hosts receive no code status requests', async () => {
  for (const [state, reason, expected] of [['unbound', 'no_project_workspace', '未关联本机项目'], ['not_git', 'not_git_repository', '此目录未使用 Git'], ['unavailable', 'git_filter_required', '仓库使用自定义文件转换，暂未读取状态']]) {
    const saved = completedSuggestionFixture(); const data = { ...projectCodeFixture(), state, reason, branch: null, uncommitted: null, last_commit: null, upstream: null, files: [] };
    const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true, route(request) { if (request.path.includes('/code-status')) return data; } });
    assert.equal(ui.get('code-local-summary').textContent, expected); assert.doesNotMatch(ui.get('code-status-card').textContent, /工作区干净|未推送 0/);
  }
  const saved = completedSuggestionFixture(); const old = await browser({ rooms: [saved.room], saved });
  assert.equal(old.get('code-status-card').hidden, true); assert.equal(old.requests.some((request) => request.path.includes('/code-status')), false);
});


test('disconnected code status keeps its observed data without a green clean claim or an enabled refresh', async () => {
  const saved = completedSuggestionFixture(); const data = projectCodeFixture(); data.uncommitted.total = 0; let offline = false;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, codeStatusSupport: true,
    route(request) { if (offline) throw new Error('fixture offline'); if (request.path.includes('/code-status')) return data; } });
  assert.equal(ui.get('code-local-summary').textContent, '工作区干净'); assert.equal(ui.get('code-local-summary').classList.contains('is-clean'), true);
  offline = true; await ui.poll();
  assert.equal(ui.get('code-local-summary').textContent, '上次检查：工作区干净'); assert.equal(ui.get('code-local-summary').classList.contains('is-clean'), false);
  assert.equal(ui.get('code-status-refresh').disabled, true); assert.match(ui.get('code-status-freshness').textContent, /本地连接已断开/);
  assert.equal(ui.get('code-commit-subject').textContent, data.last_commit.subject);
  assert.match(ui.get('code-directory').textContent, /\/projects\/示例 项目/);
});

test('code status refresh adopts the host delivery repository default until a workspace was explicitly selected', async () => {
  const saved = completedSuggestionFixture(); const data = projectCodeFixture();
  const delivery = { id: `delivery:${'a'.repeat(64)}`, kind: 'delivery', label: '交付所在仓库 · sample-app', path: '/worktrees/sample-app', source: { asset_id: 'b'.repeat(64), run_id: saved.current_run.id } };
  data.workspaces[0].label = '登记目录 · code'; let found = false; const queries = [];
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, codeStatusSupport: true,
    route(request) {
      if (!request.path.includes('/code-status')) return;
      queries.push(request.path); return found ? { ...data, workspaces: [...data.workspaces, delivery], workspace_id: delivery.id, branch: 'main', repository_root: delivery.path }
        : { ...data, state: 'not_git', reason: 'not_git_repository', uncommitted: null, last_commit: null, files: [], upstream: null };
    } });
  assert.equal(ui.get('code-local-summary').textContent, '此目录未使用 Git'); assert.match(ui.get('code-status-branch').textContent, /登记目录 · code/);
  found = true; await ui.click('code-status-refresh');
  assert.equal(queries.length, 2); assert.equal(queries.some((path) => path.includes('workspace=')), false);
  assert.equal(ui.get('code-workspace').value, delivery.id); assert.match(ui.get('code-status-branch').textContent, /交付所在仓库 · sample-app.*main/);
  assert.match(ui.get('code-local-summary').textContent, /23 个文件未提交/);
  assert.match(ui.get('code-directory').textContent, /\/worktrees\/sample-app/);
  assert.equal(ui.posts().length, 0);
});
test('an explicit code workspace remains selected across refreshes even when the host would default to a delivery repository', async () => {
  const saved = completedSuggestionFixture(); const data = projectCodeFixture();
  const delivery = { id: `delivery:${'c'.repeat(64)}`, kind: 'delivery', label: '交付所在仓库 · independent-tool', path: '/worktrees/independent-tool' };
  const queries = []; data.workspaces[0].label = '登记目录 · source';
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, codeStatusSupport: true,
    route(request) {
      if (!request.path.includes('/code-status')) return;
      queries.push(request.path); const selected = new URL(`http://fixture${request.path}`).searchParams.get('workspace') || delivery.id;
      return { ...data, workspaces: [...data.workspaces, delivery], workspace_id: selected };
    } });
  assert.equal(ui.get('code-workspace').value, delivery.id);
  ui.get('code-workspace').value = 'project'; ui.get('code-workspace').fire('change'); await settle();
  await ui.click('code-status-refresh');
  assert.equal(ui.get('code-workspace').value, 'project'); assert.match(ui.get('code-status-branch').textContent, /登记目录 · source/);
  assert.equal(queries.filter((path) => path.includes('workspace=project')).length, 2);
  await ui.poll(); assert.equal(queries.length, 3); assert.equal(ui.posts().length, 0);
});


test('a continuation binding conflict describes the task action without claiming that its delivery is unreadable', async () => {
  const saved = withNaturalContinuation(objectiveSnapshot({ status: 'stopped' })); saved.current_run.can_restart_objective = true;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true,
    route(request) { if (request.path.endsWith('/messages')) return { error: 'objective_binding_changed' }; } });
  ui.type('继续处理剩余任务'); await ui.submit();
  assert.match(ui.get('action-error').textContent, /任务状态或版本已变化.*这次操作未完成.*刷新后重试/);
  assert.doesNotMatch(ui.get('action-error').textContent, /交付的版本|最新正文|无法读取/);
  assert.equal(ui.get('message').value, '继续处理剩余任务'); assert.equal(ui.get('composer').hidden, false);
  assert.equal(ui.posts().filter((request) => request.path.endsWith('/messages')).length, 1);
  assert.equal(ui.requests.some((request) => request.path.includes('/file?')), false);
});
test('a real bound reader response conflict retains the file version protection wording', async () => {
  const saved = objectiveDeliveryFixture(); saved.objective.status = 'stopped'; addObjectiveBaseline(saved);
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true,
    route(request) { if (request.path.includes('/file?')) return { error: 'objective_binding_changed' }; } });
  descendants(ui.document.querySelector('.baseline-file-history')).find((node) => node.dataset.documentPath === 'first.md').fire('click');
  await waitUntil(() => ui.get('reader-body').textContent.includes('交付的版本无法确认'), 'the actual file API keeps its specific protection label');
  assert.match(ui.get('reader-body').textContent, /没有替换成最新正文/); assert.equal(ui.get('composer').hidden, true);
  assert.equal(ui.get('action-error').hidden, true); assert.equal(ui.posts().length, 0);
});


test('host-declared Kimi Code discovery is dynamic, selected by default like the other tools, and shows unknown quota', async () => {
  const agents = [...catalog.slice(0, 2), { id: 'kimi', adapter: 'kimi', name: 'Kimi Code', avatar: 'K' }];
  const partners = agents.map((agent) => ({ ...agent, adapter: agent.id, enabled: true, installation: { state: 'found' },
    health: { state: 'unknown' }, account: { state: 'unavailable', source: 'unsupported', reason: 'account_adapter_unsupported',
      quota: { state: 'unavailable', windows: [] } } }));
  const ui = await browser({ agents, partners, route(request) {
    if (request.path === '/api/partners') return { partners, supported_adapters: agents,
      discovery: { state: 'completed', scope: 'supported_cli_locations', tools: agents.map((agent) => ({ adapter: agent.id, name: agent.name, installed: true })) } };
  } });
  assert.match(ui.get('discovery-hint-text').textContent, /Claude Code、Codex、Kimi Code/);
  assert.equal(participantInput(ui, 'kimi').checked, true, 'every supported tool that can work takes part unless someone unticks it');
  assert.deepEqual(ui.targets(), ['all', 'claude', 'codex', 'kimi']);
  assert.deepEqual(ui.members(), ['member-claude', 'member-codex', 'member-kimi']);
  await ui.click('open-partners');
  const card = ui.get('partners-list').children.find((node) => node.textContent.includes('Kimi Code'));
  assert.ok(card); assert.match(card.textContent, /接入方式暂不提供账号与额度查询|额度无法获取/);
  assert.doesNotMatch(card.textContent, /剩余\s*0%|额度充足|已登录/);
  assert.equal(ui.posts().length, 0);
});

test('three-member mention sends the Kimi profile target once without changing frozen room membership', async () => {
  const agents = [...catalog.slice(0, 2), { id: 'kimi', adapter: 'kimi', name: 'Kimi Code', avatar: 'K' }];
  const saved = withNaturalContinuation(restartSnapshot());
  saved.members.push({ ...agents[2], provider: 'kimi', status: 'idle', session_id: null, current_task: null, usage: null });
  const ui = await browser({ agents, rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true,
    route(request) { if (request.path.endsWith('/messages')) return saved; } });
  ui.type('只核对这段说明 @Kimi');
  assert.equal(ui.get('recipient-menu').hidden, false);
  assert.deepEqual(ui.get('recipient-menu').children.map((node) => node.textContent), ['Kimi Code']);
  ui.get('message').fire('keydown', { key: 'Enter' });
  assert.equal(ui.posts().length, 0); assert.match(ui.get('recipient-button').textContent, /Kimi Code/);
  await ui.submit();
  const sent = ui.posts().filter((request) => request.path.endsWith('/messages'));
  assert.equal(sent.length, 1); assert.equal(sent[0].body.target, 'kimi'); assert.equal(sent[0].body.text, '只核对这段说明');
  assert.deepEqual(ui.members(), ['member-claude', 'member-codex', 'member-kimi']);
});

function nativeSetupFixture(state = 'login_required', operation = null) {
  return { id: 'kimi', name: 'Kimi Code', setup: { state,
    install_url: 'https://www.kimi.com/code/docs/getting-started', login_supported: true, login_operation: operation } };
}
function nativeLoginButton(ui, key) {
  return descendants(ui.get('native-tool-setup')).find((node) => node.dataset[key] === 'kimi');
}
test('native setup starts only explicit official login, coalesces clicks and completes without dispatch or draft changes', async () => {
  let now = 1000; const pending = deferred(); let setup = nativeSetupFixture();
  const waiting = { id: 'login-one', adapter: 'kimi', state: 'waiting', verification_uri: 'https://kimi.com/code/authorize_device?user_code=ABCD-1234', user_code: 'ABCD-1234', reason: null };
  const ui = await browser({ now: () => now, route(request) {
    if (request.path === '/api/partners') return { partners: catalog.slice(0, 2), supported_adapters: [setup] };
    if (request.path === '/api/native-adapters/kimi/login' && request.method === 'POST') return pending.promise;
    if (request.path === '/api/native-adapters/kimi/login') { setup = nativeSetupFixture('ready'); return { operation: { ...waiting, state: 'completed' } }; }
  } });
  ui.type('保留未发的内容'); await ui.choose('codex'); await ui.click('open-partners');
  assert.equal(ui.requests.some((request) => request.path.includes('/login')), false);
  nativeLoginButton(ui, 'nativeLoginStart').fire('click'); nativeLoginButton(ui, 'nativeLoginStart').fire('click'); await settle();
  assert.equal(ui.posts().filter((request) => request.path.includes('/login')).length, 1);
  pending.resolve({ operation: waiting }); await settle();
  assert.match(ui.get('native-tool-setup').textContent, /ABCD-1234/);
  const link = descendants(ui.get('native-tool-setup')).find((node) => node.textContent === '打开官方登录页');
  assert.equal(link.href, waiting.verification_uri); assert.equal(link.rel, 'noopener noreferrer');
  now += 1600; await ui.runTimers();
  assert.equal(ui.get('native-tool-setup').hidden, true, 'successful login leaves no redundant ready onboarding card');
  assert.equal(nativeLoginButton(ui, 'nativeLoginStart'), undefined);
  assert.equal(ui.get('message').value, '保留未发的内容');
  assert.deepEqual(ui.posts().map((request) => request.path), ['/api/native-adapters/kimi/login']);
});

test('uncertain login start is checked only by GET and cancellation uses the exact operation without touching tasks', async () => {
  let now = 1000; const operation = { id: 'login-two', adapter: 'kimi', state: 'waiting',
    verification_uri: 'https://kimi.com/code/authorize_device', user_code: 'WXYZ-2345' };
  const ui = await browser({ now: () => now, route(request) {
    if (request.path === '/api/partners') return { partners: catalog.slice(0, 2), supported_adapters: [nativeSetupFixture()] };
    if (request.path === '/api/native-adapters/kimi/login') {
      if (request.method === 'POST') throw new Error('lost response');
      if (request.method === 'DELETE') { assert.deepEqual(request.body, { operation_id: operation.id }); return { operation: { ...operation, state: 'cancelled' } }; }
      return { operation };
    }
  } });
  await ui.click('open-partners'); nativeLoginButton(ui, 'nativeLoginStart').fire('click'); await settle();
  assert.match(ui.get('native-tool-setup').textContent, /不会重复启动/);
  now += 1600; await ui.runTimers(); assert.match(ui.get('native-tool-setup').textContent, /WXYZ-2345/);
  const reads = ui.requests.filter((request) => request.path.includes('/login') && request.method === 'GET').length;
  await ui.click('close-manager'); now += 2000; await ui.runTimers();
  assert.equal(ui.requests.filter((request) => request.path.includes('/login') && request.method === 'GET').length, reads);
  await ui.click('open-partners'); nativeLoginButton(ui, 'nativeLoginCancel').fire('click'); await settle();
  assert.match(ui.get('native-tool-setup').textContent, /登录已取消.*可以重新登录/);
  assert.equal(ui.posts().filter((request) => request.path.includes('/login')).length, 1);
  assert.equal(ui.posts().some((request) => request.path.includes('/rooms')), false);
});

test('native setup shows install and unknown states honestly and never renders unsafe authorization links', async () => {
  const operation = { id: 'bad-url', adapter: 'kimi', state: 'waiting', verification_uri: 'javascript:alert(1)', user_code: 'SAFE-CODE' };
  let setup = nativeSetupFixture('not_installed');
  const ui = await browser({ route(request) {
    if (request.path === '/api/partners') return { partners: [], supported_adapters: [setup] };
  } });
  await ui.click('open-partners');
  assert.match(ui.get('native-tool-setup').textContent, /还没安装.*安装/);
  assert.equal(nativeLoginButton(ui, 'nativeLoginStart'), undefined);
  setup = nativeSetupFixture('unknown', operation); await ui.poll();
  assert.match(ui.get('native-tool-setup').textContent, /等待登录/);
  assert.equal(descendants(ui.get('native-tool-setup')).some((node) => node.tagName === 'A'), false);
  assert.doesNotMatch(ui.get('native-tool-setup').textContent, /已就绪|已登录|剩余0%/);
  assert.equal(ui.posts().length, 0);
});

function firstRunAdapters(states) {
  return Object.entries(states).map(([id, setup]) => ({ id, name: id, setup: { login_supported: true, install_url: 'https://example.test/setup', login_operation: null, install_operation: null, ...setup } }));
}
function setupRoute(body) {
  return (request) => {
    if (!['/api/partners', '/api/partners/discover'].includes(request.path.split('?')[0])) return undefined;
    const value = body();
    return { ...value, connection_protocol: { id: 'beanest-json-v1' }, discovery: { state: 'completed', scope: 'supported_cli_locations', observed_at: 1800000000,
      tools: value.supported_adapters.map((adapter) => ({ adapter: adapter.id, name: adapter.id, installed: adapter.setup.state !== 'not_installed' })) } };
  };
}

test('a new user is walked through install and sign-in on the first screen, and nobody else sees it', async () => {
  // Catches: the setup steps missing, the wrong step offered, a start button before any tool can work, or regular users being nagged.
  let adapters = firstRunAdapters({ claude: { state: 'not_installed' }, codex: { state: 'not_installed' }, kimi: { state: 'not_installed' } });
  const partnerRows = () => catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, selectable: true, installation: { state: 'found' } }));
  const listing = setupRoute(() => ({ partners: partnerRows(), supported_adapters: adapters }));
  const ui = await browser({ agents: catalog.slice(0, 2), route(request) {
    if (request.path === '/api/native-adapters/claude/install') return { operation: { id: 'one', adapter: 'claude', state: 'starting', reason: null } };
    return listing(request);
  } });
  await ui.poll();
  assert.equal(ui.get('first-run-setup').hidden, false);
  assert.match(ui.get('first-run-cards').textContent, /还没安装.*安装/);
  assert.equal(ui.get('first-run-done').hidden, true, 'nothing can work yet');
  const install = descendants(ui.get('first-run-cards')).find((node) => node.tagName === 'BUTTON' && node.textContent === '安装');
  install.fire('click'); await settle();
  assert.deepEqual(ui.posts().filter((post) => post.path.includes('/install')).map((post) => post.path), ['/api/native-adapters/claude/install']);
  assert.match(ui.get('first-run-cards').textContent, /正在安装/);

  adapters = firstRunAdapters({ claude: { state: 'login_required' }, codex: { state: 'not_installed' }, kimi: { state: 'not_installed' } });
  await ui.poll();
  assert.match(ui.get('first-run-cards').textContent, /还差登录.*登录/);
  assert.equal(ui.get('first-run-done').hidden, true, 'an installed tool that is not signed in cannot work yet');

  adapters = firstRunAdapters({ claude: { state: 'ready' }, codex: { state: 'not_installed' }, kimi: { state: 'not_installed' } });
  await ui.poll();
  assert.match(ui.get('first-run-cards').textContent, /可以用了/);
  assert.equal(ui.get('first-run-done').hidden, false);
  await ui.click('first-run-done');
  assert.equal(ui.get('first-run-setup').hidden, true);

  for (const states of [{ claude: { state: 'ready' } }, { claude: { state: 'unknown' }, kimi: { state: 'not_installed' } }]) {
    const regular = await browser({ agents: catalog.slice(0, 2), route: setupRoute(() => ({ partners: partnerRows(), supported_adapters: firstRunAdapters(states) })) });
    await regular.poll();
    assert.equal(regular.get('first-run-setup').hidden, true, JSON.stringify(states));
  }
});

test('a tool that is installed but not signed in is neither selectable nor described as signed in', async () => {
  // Catches a partner being picked for work it cannot do, and the card claiming a login that does not exist.
  const adapters = firstRunAdapters({ claude: { state: 'login_required' }, codex: { state: 'ready' } });
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, selectable: true, installation: { state: 'found' }, account: { state: 'available', stale: false } }));
  const ui = await browser({ agents: catalog.slice(0, 2), partners, route: setupRoute(() => ({ partners, supported_adapters: adapters })) });
  await ui.poll();
  const checked = descendants(ui.get('participant-picker')).filter((node) => node.tagName === 'INPUT' && node.checked).map((node) => node.dataset.participant);
  assert.deepEqual(checked, ['codex']);
});

test('a tool found after the page opened joins the default choice, but not after someone picked by hand', async () => {
  // Catches: a third tool set up in the app staying unticked, or a discovery overriding a deliberate choice.
  const kimi = { id: 'kimi', adapter: 'kimi', name: 'Kimi Code', avatar: 'K' };
  for (const picked of [false, true]) {
    const agents = catalog.slice(0, 2).map((agent) => ({ ...agent }));
    const ui = await browser({ agents });
    if (picked) await ui.invite('codex', false);
    agents.push(kimi);
    await ui.poll();
    if (participantInput(ui, 'kimi')) assert.equal(participantInput(ui, 'kimi').checked, !picked, `picked=${picked}`);
    else assert.fail('the new tool should be listed');
  }
});

test('official setup web bridge runs only on the explicit link click and leaves a visible address on failure', async () => {
  const opened = []; const setup = nativeSetupFixture('not_installed');
  setup.setup.install_operation = { id: 'install-one', adapter: 'kimi', state: 'failed', reason: 'install_failed' };
  const ui = await browser({ desktopBridge: { openWebPage(url) { opened.push(url); return Promise.resolve(false); } }, route(request) {
    if (request.path === '/api/partners') return { partners: [], supported_adapters: [setup] };
  } });
  await ui.click('open-partners'); assert.equal(opened.length, 0);
  const link = descendants(ui.get('native-tool-setup')).find((node) => node.tagName === 'A' && node.textContent === '官方说明');
  link.fire('click'); await settle();
  assert.deepEqual(opened, [setup.setup.install_url]); assert.match(ui.get('manager-notice').textContent, /网页未打开.*复制/);
  assert.ok(ui.get('native-tool-setup').textContent.includes(setup.setup.install_url)); assert.equal(ui.posts().length, 0);
});


test('setup omits duplicate unknown tool cards with no action while keeping the active device login', async () => {
  const operation = { id: 'compact-login', adapter: 'kimi', state: 'waiting', user_code: 'TEST-1234', verification_uri: 'https://kimi.com/code/authorize_device' };
  const supported = [...catalog.slice(0, 2).map((agent) => ({ ...agent, setup: { state: 'unknown', login_supported: false, install_url: null, login_operation: null } })), nativeSetupFixture('login_required', operation)];
  const ui = await browser({ route(request) {
    if (request.path === '/api/partners') return { partners: catalog.slice(0, 2), supported_adapters: supported };
  } });
  await ui.click('open-partners');
  assert.deepEqual(ui.get('native-tool-setup').children.map((node) => node.dataset.adapterSetup), ['kimi']);
  assert.match(ui.get('native-tool-setup').textContent, /TEST-1234/);
  assert.match(ui.get('partners-list').textContent, /Claude Code.*Codex/);
  assert.equal(ui.posts().length, 0);
});

test('new task drafts restore three members and a Kimi-only selection across actual document reloads', async () => {
  const agents = [...catalog.slice(0, 2), { id: 'kimi', adapter: 'kimi', name: 'Kimi Code', avatar: 'K' }];
  for (const onlyKimi of [false, true]) {
    const storage = new Map(); let ui = await browser({ agents, storage });
    await ui.invite('kimi');
    if (onlyKimi) { await ui.invite('claude', false); await ui.invite('codex', false); }
    ui.type('尚未发送的三伙伴任务'); await settle();
    ui = await browser({ agents, storage });
    assert.equal(ui.get('message').value, '尚未发送的三伙伴任务');
    assert.deepEqual(ui.targets(), onlyKimi ? ['all', 'kimi'] : ['all', 'claude', 'codex', 'kimi']);
    assert.equal(participantInput(ui, 'kimi').checked, true);
    assert.equal(ui.posts().length, 0);
  }
});

test('restored new task members filter a disabled Kimi without silently inviting replacement partners', async () => {
  const agents = [...catalog.slice(0, 2), { id: 'kimi', adapter: 'kimi', name: 'Kimi Code', avatar: 'K' }];
  const storage = new Map(); const first = await browser({ agents, storage });
  await first.invite('kimi'); await first.invite('claude', false); await first.invite('codex', false);
  first.type('保留我的任务草稿'); await settle();
  const partners = agents.map((agent) => ({ ...agent, enabled: agent.id !== 'kimi', lifecycle_state: 'active' }));
  const second = await browser({ agents, partners, storage });
  assert.equal(second.get('message').value, '保留我的任务草稿');
  assert.deepEqual(second.targets(), ['all']);
  assert.equal(participantInput(second, 'kimi').checked, false);
  assert.equal(participantInput(second, 'claude').checked, false);
  assert.equal(participantInput(second, 'codex').checked, false);
  assert.equal(second.posts().length, 0);
});

test('explicit partner choices survive an empty new-task draft, including an intentional empty selection', async () => {
  const agents = [...catalog.slice(0, 2), { id: 'kimi', adapter: 'kimi', name: 'Kimi Code', avatar: 'K' }];
  for (const empty of [false, true]) {
    const storage = new Map(); const first = await browser({ agents, storage });
    await first.invite('claude', false); await first.invite('codex', false);
    await first.invite('kimi', !empty);
    await settle();
    const restored = await browser({ agents, storage });
    assert.equal(restored.get('message').value, '');
    assert.deepEqual(restored.targets(), empty ? ['all'] : ['all', 'kimi']);
    assert.equal(restored.posts().length, 0);
  }
});

function memberInvitationFixture() {
  const source = withNaturalContinuation(objectiveSnapshot({ status: 'complete', decision: 'complete' }));
  source.room.status = 'completed'; source.members = source.members.filter((member) => ['claude', 'codex'].includes(member.id));
  source.member_invitation = { supported: true, can_invite: true, reason: null,
    candidates: [{ id: 'kimi', adapter: 'kimi', name: 'Kimi Code', avatar: 'K', bean_style: 'sprout' }] };
  return source;
}
function acceptFixtureInvite(source, row) {
  source.members.push({ ...source.member_invitation.candidates[0], provider: 'kimi', status: 'idle', session_id: null,
    current_task: null, usage: null, joined_pending: true }); source.member_invitation.candidates = [];
  source.member_invitation.can_invite = false; source.member_invitation.reason = 'agent_unavailable';
  row.delivery = { state: 'received', can_send: false, can_retire: true, reason: null, result_room_id: source.room.id };
  return { acknowledged: true, room_id: source.room.id, request_id: row.request_id };
}
async function invitationBrowser(source, peer, options = {}) {
  return browser({ agents: [...catalog.slice(0, 2), { id: 'kimi', adapter: 'kimi', name: 'Kimi Code', avatar: 'K' }],
    rooms: [source.room], saved: pagedFixture(source, []), pagingSupport: true, hostOutboxScope: peer.scope,
    memberInvitationSupport: true, naturalContinuationSupport: true, executionPolicySupport: true, ...options,
    route: async (request) => {
      if (options.route) { const result = await options.route(request); if (result !== undefined) return result; }
      const result = await peer.route(request); if (result !== undefined) return result;
      if (request.path.endsWith('/state')) return pagedFixture(source, []);
      return eventPageRoute(request, []);
    } });
}
function inviteKimiButton(ui) { return descendants(ui.get('member-invitation-options')).find((node) => node.dataset.inviteMember === 'kimi'); }

test('an explicit invitation joins the delivered chat once without changing the draft recipient or old delivery credit', async () => {
  const source = memberInvitationFixture(); const oldRun = source.current_run.id, oldGoal = source.objective.original_goal;
  const held = deferred(); let staged;
  const peer = hostOutboxPeer({ onDispatch(request, row) { staged = { request, row }; return held.promise; } });
  const ui = await invitationBrowser(source, peer);
  ui.type('这段还没发送'); await ui.choose('codex'); await ui.click('task-options-partners');
  assert.match(ui.get('member-invitation-hint').textContent, /不会重做已交付/);
  const invite = inviteKimiButton(ui); invite.fire('click'); invite.fire('click'); await settle();
  assert.equal(peer.dispatchCalls, 1); assert.equal(ui.get('message').value, '这段还没发送');
  assert.match(staged.request.path, /\/members\?response=ack&outbox_scope=/);
  assert.deepEqual(Object.keys(staged.request.body).sort(), ['participants', 'request_id']);
  assert.deepEqual(staged.request.body.participants, ['kimi']);
  held.resolve(acceptFixtureInvite(source, staged.row)); await settle();
  await waitUntil(() => ui.members().includes('member-kimi'), 'canonical members render after receipt');
  assert.match(ui.get('partner-activity-kimi').textContent, /已加入，等待安排/);
  assert.match(ui.get('member-invitation-status').textContent, /当前没有其他可邀请的伙伴/);
  assert.doesNotMatch(ui.get('member-invitation-status').textContent, /无法邀请|未完成/);
  assert.doesNotMatch(ui.get('partner-activity-kimi').textContent, /目标已交付|已确认完成|经独立审核/);
  assert.match(ui.get('partner-activity-claude').textContent, /目标已交付/);
  assert.equal(ui.get('message').value, '这段还没发送'); assert.match(ui.get('recipient-button').textContent, /Codex/);
  assert.equal(source.current_run.id, oldRun); assert.equal(source.objective.original_goal, oldGoal);
  assert.deepEqual(ui.targets(), ['all', 'claude', 'codex', 'kimi']);
  assert.equal(ui.posts().some((request) => /\/messages|\/runs|\/continue/.test(request.path)), false);
});

test('a lost invitation reply survives reload as a read-only receipt check and never replays the member POST', async () => {
  const source = memberInvitationFixture(); const storage = new Map();
  const peer = hostOutboxPeer({ onDispatch() { throw new Error('lost member response'); } });
  const first = await invitationBrowser(source, peer, { storage }); first.type('原草稿和定向保留'); await first.choose('codex');
  await first.click('task-options-partners'); inviteKimiButton(first).fire('click'); await settle();
  assert.equal(peer.dispatchCalls, 1); const row = [...peer.rows.values()][0]; const originalBody = structuredClone(row.body);
  const reloaded = await invitationBrowser(source, peer, { storage });
  assert.equal(peer.dispatchCalls, 1); assert.equal(reloaded.get('message').value, '原草稿和定向保留');
  assert.match(reloaded.get('recipient-button').textContent, /Codex/);
  await reloaded.click('task-options-partners'); await reloaded.click('member-invitation-check');
  assert.equal(peer.dispatchCalls, 1); assert.deepEqual(row.body, originalBody);
  acceptFixtureInvite(source, row); await reloaded.poll();
  assert.ok(reloaded.members().includes('member-kimi')); assert.equal(peer.dispatchCalls, 1);
  assert.equal(reloaded.get('message').value, '原草稿和定向保留');
});

test('late invitation completion cannot switch away from a newly composed task or replace its text', async () => {
  const source = memberInvitationFixture(); const held = deferred(); let row;
  const peer = hostOutboxPeer({ onDispatch(_request, value) { row = value; return held.promise; } });
  const ui = await invitationBrowser(source, peer); await ui.click('task-options-partners'); inviteKimiButton(ui).fire('click'); await settle();
  await ui.click('task-options-close'); await ui.click('new-room'); ui.type('这是新的未发草稿');
  held.resolve(acceptFixtureInvite(source, row)); await settle();
  assert.equal(ui.get('room-title').textContent, '新任务'); assert.equal(ui.get('message').value, '这是新的未发草稿');
  assert.equal(ui.get('room-member-invitation').hidden, true); assert.equal(peer.dispatchCalls, 1);
});

test('without invitation capability an existing chat only shows its original members and never offers a fake action', async () => {
  const source = memberInvitationFixture(); const ui = await browser({ rooms: [source.room], saved: source });
  await ui.click('task-options-partners'); assert.equal(ui.get('room-member-invitation').hidden, true);
  assert.equal(ui.posts().length, 0); assert.deepEqual(ui.members(), ['member-claude', 'member-codex']);
});


test('inviting during work keeps the actual worker active and the newcomer waiting without a global composer lock', async () => {
  const source = memberInvitationFixture(); source.objective.status = 'running'; source.current_run.status = 'working'; source.room.status = 'running';
  source.jobs = [{ id: 'working-original', provider: 'codex', status: 'working', phase: 'work', text: '已有工作' }];
  const held = deferred(); let row;
  const peer = hostOutboxPeer({ onDispatch(_request, entry) { row = entry; return held.promise; } });
  const ui = await invitationBrowser(source, peer); await ui.click('task-options-partners');
  assert.match(ui.get('member-invitation-hint').textContent, /原伙伴继续/);
  inviteKimiButton(ui).fire('click'); await settle();
  assert.equal(ui.get('message').disabled, false); assert.match(ui.get('partner-activity-codex').textContent, /正在|处理/);
  held.resolve(acceptFixtureInvite(source, row)); await settle();
  await waitUntil(() => ui.members().includes('member-kimi'), 'running chat receives the invited member');
  assert.match(ui.get('partner-activity-kimi').textContent, /已加入，等待安排/);
  assert.equal(source.jobs.length, 1); assert.equal(source.jobs[0].id, 'working-original');
  assert.equal(peer.dispatchCalls, 1); assert.equal(source.objective.status, 'running');
});

function mainlineCodeFixture() {
  return { ...projectCodeFixture(), mainline: { state: 'ready', name: 'production', relation: 'unmerged', ahead: 2, behind: 1,
    scope: 'repository_local_refs' }, merge_request: { available: true, reason: null, target_branch: 'production',
    source_branch: 'feature/demo', repository_root: '/projects/示例 项目', head: 'a'.repeat(40), mainline_head: 'b'.repeat(40), scope: 'repository_local_refs' } };
}
test('documents expose bound current delivery titles directly while previous and inventory files stay folded', async () => {
  const saved = objectiveDeliveryFixture(); const previous = addObjectiveBaseline(saved);
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true,
    route(request) { if (request.path.includes('/file?')) return { ...previous.binding, content: previous.content }; } });
  assert.equal(ui.get('room-documents').hidden, false);
  assert.match(ui.get('document-shortcuts').textContent, /第一阶段成果/);
  assert.match(ui.get('document-shortcuts').textContent, /伙伴看过/);
  assert.doesNotMatch(ui.get('document-shortcuts').textContent, /旧库存|沿用/);
  assert.equal(ui.get('document-more').open, false); assert.match(ui.get('document-more-items').textContent, /旧库存/);
  descendants(ui.get('document-shortcuts')).find((node) => node.dataset.documentPath === 'first.md').fire('click'); await settle();
  assert.match(ui.get('reader-body').textContent, /第一阶段的正文/); assert.equal(ui.posts().length, 0);
  await ui.click('reader-back');
  const old = descendants(ui.get('document-more-items')).find((node) => node.dataset.documentPath === 'first.md');
  old.fire('click'); await waitUntil(() => ui.get('reader-body').textContent.includes('这是上一轮的正文'), 'historical shortcut exact reader');
  const read = ui.requests.find((request) => request.path.includes('/file?'));
  for (const [key, value] of Object.entries(previous.binding)) assert.equal(new URL(`http://fixture${read.path}`).searchParams.get(key), String(value));
});

test('merge request is an explicit single durable user message preserving the independent draft and recipient', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture()); const data = mainlineCodeFixture(); const held = deferred(); let dispatched;
  const peer = hostOutboxPeer({ onDispatch(request, row) { dispatched = { request, row }; return held.promise; } });
  const ui = await browser({ rooms: [saved.room], saved: pagedFixture(saved, []), executionPolicySupport: true,
    naturalContinuationSupport: true, codeStatusSupport: true, pagingSupport: true, hostOutboxScope: peer.scope,
    route: async (request) => {
      if (request.path.includes('/code-status')) return data;
      const result = await peer.route(request); if (result !== undefined) return result;
      if (request.path.endsWith('/state')) return pagedFixture(saved, []);
      return eventPageRoute(request, []);
    } });
  ui.type('不要丢失这份未发草稿'); await ui.choose('codex');
  assert.match(ui.get('code-local-summary').textContent, /23.*未提交/); assert.match(ui.get('code-mainline').textContent, /尚未合入 production.*2/);
  assert.equal(ui.get('code-merge-panel').open, false);
  ui.get('code-merge-panel').open = true; ui.get('code-merge-panel').fire('toggle'); await settle();
  assert.equal(peer.dispatchCalls, 0); assert.match(ui.get('code-merge-description').textContent, /本任务关联仓库.*示例 项目/s);
  ui.get('code-merge-panel').open = false; assert.equal(ui.get('message').value, '不要丢失这份未发草稿');
  ui.get('code-merge-panel').open = true; ui.get('code-merge-target').value = 'claude';
  ui.get('code-merge-send').fire('click'); ui.get('code-merge-send').fire('click'); await settle();
  assert.equal(peer.dispatchCalls, 1); assert.equal(dispatched.request.body.target, 'claude');
  assert.match(dispatched.request.body.text, /关联仓库：\/projects\/示例 项目/);
  assert.match(dispatched.request.body.text, /核对实际默认主分支并合入/);
  assert.match(dispatched.request.path, /\/messages\?response=ack&outbox_scope=/);
  dispatched.row.delivery = { state: 'received', can_send: false, can_retire: true, reason: null, result_room_id: saved.room.id };
  held.resolve({ acknowledged: true, room_id: saved.room.id, request_id: dispatched.row.request_id }); await settle();
  assert.equal(ui.get('message').value, '不要丢失这份未发草稿'); assert.match(ui.get('recipient-button').textContent, /Codex/);
  assert.equal(ui.posts().some((request) => /\/runs|\/continue|\/merge/.test(request.path)), false);
});

test('unknown mainline and already merged branches still allow a partner to inspect and submit without guessing a target', async () => {
  for (const relation of ['unknown', 'merged', 'same_branch']) {
    const saved = completedSuggestionFixture(); const data = mainlineCodeFixture();
    data.mainline = { state: relation === 'unknown' ? 'unknown' : 'ready', name: relation === 'unknown' ? null : 'production', relation };
    data.merge_request.available = false;
    const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true,
      codeStatusSupport: true, route(request) { if (request.path.includes('/code-status')) return data; } });
    assert.equal(ui.get('code-merge-panel').hidden, false); assert.equal(ui.posts().length, 0);
    assert.match(ui.get('code-mainline').textContent, relation === 'unknown' ? /尚未确认/ : relation === 'merged' ? /已合入/ : /当前就在主分支/);
  }
});

test('Kimi quota windows show supplied remaining values and an explicit official usage page without inventing account facts', async () => {
  const agents = [...catalog.slice(0, 2), { id: 'kimi', name: 'Kimi Code', adapter: 'kimi', avatar: 'K' }];
  const account = accountFixture({ email: null, plan: null, source: 'kimi_code_web_oauth_usage', quota: {
    state: 'available', source: 'kimi_code_web_oauth_usage', usage_url: 'https://www.kimi.com/code/console', scope: 'provider_account', checked_at: 1000,
    windows: [{ name: 'kimi:limit5h', remaining_percent: 99, used_percent: 1, window_minutes: 300, resets_at: 2000 }] } });
  const partners = agents.map((agent) => ({ ...agent, enabled: true, adapter: agent.id, account: agent.id === 'kimi' ? account : null,
    ...(agent.id === 'kimi' ? { availability: availabilityFixture({ reason: 'adapter_unsupported' }) } : {}) }));
  const saved = snapshot({ agents }); const ui = await browser({ agents, partners, rooms: [saved.room], saved });
  const footer = ui.get('provider-account-kimi'); footer.open = true; footer.fire('toggle'); await settle();
  assert.match(footer.textContent, /Kimi Code · 5小时额度/); assert.match(footer.textContent, /剩余 99%/);
  assert.match(footer.textContent, /Kimi Code 本机会员登录额度查询/);
  assert.match(footer.textContent, /月度总额度：本次查询未提供/);
  assert.match(ui.get('composer-usage-items').textContent, /5小时剩余 99%/); assert.match(footer.textContent, /订阅/); assert.doesNotMatch(footer.textContent, /订阅未提供/);
  const link = descendants(footer).find((node) => node.dataset.usageLink === 'kimi');
  assert.equal(link.href, 'https://www.kimi.com/code/console'); assert.equal(link.target, '_blank');
  assert.doesNotMatch(footer.textContent, /Plus|剩余 0%/);
  assert.equal(ui.get('availability-kimi').hidden, true);
  assert.match(footer.textContent, /5小时剩余 99%/);
  assert.doesNotMatch(ui.get('composer-usage-kimi').title, /未取得|未支持/);
  partners[2].availability = availabilityFixture({ state: 'blocked', selectable: false, reason: 'quota_exhausted' });
  const limited = await browser({ agents, partners, rooms: [saved.room], saved });
  assert.match(limited.get('availability-kimi').textContent, /^额度用完 · 供应商没给恢复时间，每小时自动试一次$/);
  assert.equal(limited.get('composer-usage-kimi').classList.contains('quota-blocked'), true);
});

test('an uncertain merge message reloads the original receipt without sending again or replacing the unrelated composer draft', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture()); const data = mainlineCodeFixture(); const storage = new Map();
  const peer = hostOutboxPeer({ onDispatch() { throw new Error('lost response after dispatch'); } });
  const options = { rooms: [saved.room], saved: pagedFixture(saved, []), storage, executionPolicySupport: true,
    naturalContinuationSupport: true, codeStatusSupport: true, pagingSupport: true, hostOutboxScope: peer.scope,
    route: async (request) => {
      if (request.path.includes('/code-status')) return data;
      const result = await peer.route(request); if (result !== undefined) return result;
      if (request.path.endsWith('/state')) return pagedFixture(saved, []);
      return eventPageRoute(request, []);
    } };
  const ui = await browser(options); ui.type('另一个尚未发送的想法'); await ui.choose('codex');
  ui.get('code-merge-panel').open = true; await ui.click('code-merge-send');
  assert.equal(peer.dispatchCalls, 1); const original = structuredClone([...peer.rows.values()][0].body);
  const next = await browser(options); await next.poll();
  assert.equal(peer.dispatchCalls, 1); assert.equal(next.get('message').value, '另一个尚未发送的想法');
  assert.match(next.get('recipient-button').textContent, /Codex/); assert.deepEqual([...peer.rows.values()][0].body, original);
});

test('a lazy current document shortcut reads its exact delivery binding and never substitutes a newer file', async () => {
  const saved = objectiveDeliveryFixture(); const content = saved.project.files[0].content;
  saved.objective.delivery.files[0].run_id = '66666666-6666-4666-8666-666666666666';
  const file = structuredClone(saved.objective.delivery.files[0]); saved.project.files = [];
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true,
    route(request) {
      if (request.path.includes('/file?')) return { run_id: file.run_id, revision: file.revision, digest: file.digest,
        path: file.path, sha256: file.sha256, content };
    } });
  const shortcut = descendants(ui.get('document-shortcuts')).find((node) => node.dataset.documentPath === file.path);
  assert.ok(shortcut); shortcut.fire('click');
  await waitUntil(() => ui.get('reader-body').textContent.includes('第一阶段的正文'), 'current lazy document exact read');
  const read = ui.requests.find((request) => request.path.includes('/file?'));
  const query = new URL(`http://fixture${read.path}`).searchParams;
  for (const key of ['run_id', 'revision', 'digest', 'path', 'sha256']) assert.equal(query.get(key), String(file[key]));
  assert.equal(ui.posts().length, 0);
});


test('the primary code summary separates tracked changes from untracked files without classifying their contents', async () => {
  const saved = completedSuggestionFixture(); const data = mainlineCodeFixture();
  data.uncommitted = { total: 20, tracked: 0, untracked: 20, staged: 0, unstaged: 0, conflicted: 0 };
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true,
    codeStatusSupport: true, route(request) { if (request.path.includes('/code-status')) return data; } });
  assert.equal(ui.get('code-local-summary').textContent, '已提交内容尚未合入 production');
  assert.match(ui.get('code-mainline').textContent, /尚未合入 production/);
  assert.equal(ui.get('code-untracked-status').textContent, '另有 20 项未纳入 Git（文件或目录）');
  assert.equal(ui.get('code-tracked-status').textContent, '已跟踪文件：无待提交改动');
  data.uncommitted = { total: 6, tracked: 2, untracked: 4, staged: 1, unstaged: 1, conflicted: 0 };
  await ui.click('code-status-refresh');
  assert.equal(ui.get('code-local-summary').textContent, '有 2 个已跟踪文件待提交');
  delete data.uncommitted.tracked; await ui.click('code-status-refresh');
  assert.equal(ui.get('code-local-summary').textContent, '有 6 个文件未提交'); assert.equal(ui.posts().length, 0);
});

test('composer quota labels include only observed windows consistently across providers', async () => {
  const agents = [...catalog.slice(0, 2), { id: 'kimi', name: 'Kimi Code', adapter: 'kimi' }];
  const partners = agents.map((agent, index) => ({ ...agent, enabled: true, adapter: agent.id,
    account: accountFixture({ quota: { state: 'available', scope: 'provider_account', stale: index === 1,
      windows: [{ remaining_percent: 73, window_minutes: index === 0 ? 300 : index === 1 ? 10080 : null }] } }) }));
  const saved = snapshot({ agents }); const ui = await browser({ agents, partners, rooms: [saved.room], saved });
  assert.equal(ui.get('composer-usage-claude').textContent, '5小时剩余 73%');
  assert.equal(ui.get('composer-usage-codex').textContent, '上次：7天剩余 73%');
  assert.equal(ui.get('composer-usage-kimi').textContent, '剩余 73%', 'an unknown duration never acquires a provider default');
  assert.equal(ui.posts().length, 0);
});

test('desktop reopening reads the latest terminal room state without waiting for a polling tick', async () => {
  let notifyOpened; const saved = activeActivitySnapshot();
  const ui = await browser({ rooms: [saved.room], saved,
    desktopBridge: { onOpened(callback) { notifyOpened = callback; return () => {}; } } });
  ui.type('保留尚未发送的补充'); await ui.choose('codex');
  saved.current_run.status = 'stopped'; saved.room.status = 'stopped';
  saved.collaboration = teamCollaboration({ state: 'stopped' });
  saved.jobs = []; saved.members.forEach((member) => { member.status = 'idle'; });
  const reads = ui.requests.filter((request) => request.path === `/api/rooms/${saved.room.id}`).length;
  notifyOpened(); await settle();
  assert.ok(ui.requests.filter((request) => request.path === `/api/rooms/${saved.room.id}`).length > reads);
  assert.doesNotMatch(ui.get('partner-activity').textContent, /正在处理|正在分工/);
  assert.equal(ui.get('message').value, '保留尚未发送的补充');
  assert.match(ui.get('recipient-button').textContent, /Codex/);
  assert.equal(ui.posts().some((request) => !request.path.endsWith('/discover')), false);
});

test('missing effort metadata is described as unavailable in the integration rather than unsupported by the model', async () => {
  const options = runtimeOptionsFixture(); options.partners.claude.models[0].efforts = [];
  const ui = await browser({ agents: catalog.slice(0, 2), route(request) {
    if (request.path === '/api/runtime-options' || request.path.endsWith('/runtime/refresh')) return options;
  } });
  ui.type('保持草稿'); await ui.click('runtime-shortcut-claude');
  selectRuntime(ui, 'runtime-model-claude', 'opus');
  assert.equal(ui.get('runtime-effort-claude').disabled, true);
  assert.match(ui.get('runtime-card-claude').textContent, /当前接入未提供这个模型的可选思考强度/);
  assert.doesNotMatch(ui.get('runtime-card-claude').textContent, /模型不支持/);
  assert.equal(ui.get('runtime-effort-claude').value, '');
  assert.equal(ui.get('message').value, '保持草稿');
  assert.equal(ui.posts().every((request) => request.path.endsWith('/runtime/refresh')), true);
});

test('a slow outbox check cannot hide an already returned terminal room snapshot', async () => {
  const saved = activeActivitySnapshot(); const peer = hostOutboxPeer(); const held = deferred(); let hold = false; let waiting = false; let checks = 0; let notifyOpened;
  const ui = await browser({ rooms: [saved.room], saved, hostOutboxScope: peer.scope,
    desktopBridge: { onOpened(callback) { notifyOpened = callback; return () => {}; } }, route: async (request) => {
    if (hold && request.path.startsWith('/api/outbox?')) { waiting = true; checks += 1; await held.promise; }
    return peer.route(request);
  } });
  saved.current_run.status = 'stopped'; saved.room.status = 'stopped'; saved.jobs = [];
  saved.collaboration = teamCollaboration({ state: 'stopped' }); saved.members.forEach((member) => { member.status = 'idle'; });
  hold = true;
  try {
    await ui.poll(); assert.equal(waiting, true);
    assert.doesNotMatch(ui.get('partner-activity').textContent, /正在处理|正在分工/);
    assert.equal(ui.get('refresh').disabled, false);
    saved.current_run.status = 'ready_for_user'; saved.room.status = 'ready_for_user';
    saved.collaboration = teamCollaboration({ state: 'ready_for_user' });
    notifyOpened(); await settle();
    assert.match(ui.get('partner-activity-claude').textContent, /本轮已交付/);
    assert.equal(checks, 1, 'an activation updates the room without duplicating the held receipt check');
    assert.equal(ui.posts().some((request) => !request.path.endsWith('/discover')), false);
  } finally { held.resolve(); await settle(); }
});

test('choosing an effort explicitly pins the uniquely reported default model while merely opening preserves null', async () => {
  const options = runtimeOptionsFixture(); options.partners.claude.models[0].is_default = true;
  const saved = snapshot({ agents: catalog.slice(0, 2) });
  const ui = await browser({ agents: catalog.slice(0, 2), route(request) {
    if (request.path === '/api/runtime-options' || request.path.endsWith('/runtime/refresh')) return options;
    if (request.path === '/api/rooms' && request.method === 'POST') return saved;
  } });
  ui.type('原始任务正文'); await ui.click('runtime-shortcut-claude');
  assert.equal(ui.get('runtime-model-claude').value, '');
  assert.equal(ui.get('runtime-effort-claude').disabled, false);
  assert.match(ui.get('runtime-card-claude').textContent, /当前工具默认：Opus.*选择思考强度时会同时选定此模型/);
  await ui.poll(); assert.equal(ui.get('runtime-model-claude').value, '');
  assert.equal(ui.get('runtime-effort-claude').value, '');
  selectRuntime(ui, 'runtime-effort-claude', 'high');
  assert.equal(ui.get('runtime-model-claude').value, 'opus');
  assert.equal(ui.get('message').value, '原始任务正文');
  await ui.click('runtime-settings-close'); await ui.submit();
  assert.deepEqual(ui.posts().find((request) => request.path === '/api/rooms').body.runtime_settings.members.claude, { model: 'opus', effort: 'high' });
  const ambiguous = runtimeOptionsFixture(); ambiguous.partners.claude.models.forEach((model) => { model.is_default = true; });
  const other = await browser({ route(request) { if (request.path === '/api/runtime-options' || request.path.endsWith('/runtime/refresh')) return ambiguous; } });
  await other.click('runtime-shortcut-claude'); assert.equal(other.get('runtime-effort-claude').disabled, true);
  assert.equal(other.get('runtime-model-claude').value, '', 'multiple defaults never guess a model');
});

test('unsent default-model effort and work mode survive completed-room reload with the original draft and recipient', async () => {
  const saved = withRuntimeConfiguration(withNaturalContinuation(completedSuggestionFixture()));
  saved.runtime_configuration.desired.runtime_settings.members.claude = { model: null, effort: null };
  const options = runtimeOptionsFixture(); options.partners.claude.models[0].is_default = true; const storage = new Map();
  const config = { rooms: [saved.room], saved, storage, executionPolicySupport: true, naturalContinuationSupport: true, naturalRuntimeSupport: true,
    route(request) { if (request.path === '/api/runtime-options' || request.path.endsWith('/runtime/refresh')) return options; } };
  const ui = await browser(config); await ui.click('runtime-shortcut-claude'); selectRuntime(ui, 'runtime-effort-claude', 'high');
  await ui.click('runtime-settings-close'); await ui.click('work-mode-plan'); ui.type('下一步的原始补充'); await ui.choose('codex');
  const next = await browser(config); await next.click('runtime-shortcut-claude');
  assert.equal(next.get('runtime-model-claude').value, 'opus'); assert.equal(next.get('runtime-effort-claude').value, 'high');
  assert.equal(next.get('message').value, '下一步的原始补充'); assert.match(next.get('recipient-button').textContent, /Codex/);
  assert.equal(next.get('work-mode-plan').getAttribute('aria-pressed'), 'true');
  assert.match(next.get('runtime-settings-note').textContent, /发送消息或点击“继续”后/);
  assert.equal(next.posts().every((request) => request.path.endsWith('/runtime/refresh')), true, 'restoration never sends the draft or starts work');
});

test('settings-only drafts persist without text and malformed stored settings cannot replace host configuration', async () => {
  const saved = withRuntimeConfiguration(withNaturalContinuation(completedSuggestionFixture())); const storage = new Map(); const options = runtimeOptionsFixture();
  const config = { rooms: [saved.room], saved, storage, executionPolicySupport: true, naturalContinuationSupport: true, naturalRuntimeSupport: true,
    route(request) { if (request.path === '/api/runtime-options') return options; } };
  const ui = await browser(config); await openRuntimeSettings(ui); selectRuntime(ui, 'runtime-model-claude', 'opus');
  selectRuntime(ui, 'runtime-effort-claude', 'high'); selectRuntime(ui, 'runtime-permission', 'read_only');
  await ui.click('runtime-settings-close'); await ui.click('work-mode-plan');
  const next = await browser(config); await openRuntimeSettings(next);
  assert.equal(next.get('message').value, ''); assert.equal(next.get('runtime-model-claude').value, 'opus');
  assert.equal(next.get('runtime-effort-claude').value, 'high'); assert.equal(next.get('runtime-permission').value, 'read_only');
  assert.equal(next.get('work-mode-plan').getAttribute('aria-pressed'), 'true');
  const record = [...outboxDatabases.get(storage).values()].find((entry) => entry.scope?.startsWith('composer:') && entry.scope.endsWith(roomId));
  assert.ok(record); record.text = '坏设置仍不丢正文'; record.runtimeSettings = { permission: 'invented_full', members: { claude: { model: ['not-a-model'], effort: 7 } } };
  record.runtimeDraftDirty = true; record.workMode = 'execute-anything'; record.workModeDraftDirty = true;
  const invalid = await browser(config); await openRuntimeSettings(invalid);
  assert.equal(invalid.get('message').value, '坏设置仍不丢正文');
  assert.equal(invalid.get('runtime-model-claude').value, 'sonnet'); assert.equal(invalid.get('runtime-effort-claude').value, 'medium');
  assert.equal(invalid.get('runtime-permission').value, 'workspace_write');
  assert.equal(invalid.get('work-mode-build').getAttribute('aria-pressed'), 'true');
  assert.equal(invalid.posts().length, 0);
});

test('new-task runtime and work-mode drafts restore across documents without sending or upgrading their permission', async () => {
  const storage = new Map(); const options = runtimeOptionsFixture();
  const config = { storage, agents: catalog.slice(0, 2), executionPolicySupport: true,
    route(request) { if (request.path === '/api/runtime-options') return options; } };
  const ui = await browser(config); await openRuntimeSettings(ui); selectRuntime(ui, 'runtime-model-claude', 'opus');
  selectRuntime(ui, 'runtime-effort-claude', 'high'); selectRuntime(ui, 'runtime-permission', 'read_only');
  await ui.click('runtime-settings-close'); await ui.click('work-mode-plan');
  const next = await browser(config); await openRuntimeSettings(next);
  assert.equal(next.get('runtime-model-claude').value, 'opus'); assert.equal(next.get('runtime-effort-claude').value, 'high');
  assert.equal(next.get('runtime-permission').value, 'read_only'); assert.equal(next.get('message').value, '');
  assert.equal(next.get('work-mode-plan').getAttribute('aria-pressed'), 'true'); assert.equal(next.posts().length, 0);
});

test('recipient continuation follows the host projection while preserving blocked peers and global eligibility', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture(), false, 'session_identity_unknown');
  saved.continuation.recipients = { claude: { can_send: true, reason: null }, codex: { can_send: false, reason: 'session_identity_unknown' } };
  saved.members[1].identity_uncertain = true;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true,
    route(request) { if (request.path.endsWith('/messages')) return saved; } });
  ui.type('只给健康伙伴的新要求'); assert.equal(ui.get('send').disabled, true);
  await ui.choose('claude'); assert.equal(ui.get('send').disabled, false);
  await ui.choose('codex'); assert.equal(ui.get('send').disabled, true);
  await ui.choose('all'); assert.equal(ui.get('send').disabled, true);
  await ui.choose('claude'); await ui.submit();
  const sent = ui.posts().filter((request) => request.path.endsWith('/messages'));
  assert.equal(sent.length, 1); assert.equal(sent[0].body.target, 'claude'); assert.equal(sent[0].body.text, '只给健康伙伴的新要求');
  assert.deepEqual(sent[0].body.binding, saved.continuation.binding);
  assert.equal(saved.members[1].identity_uncertain, true);
});

test('recipient projections never bypass a stale run binding and old hosts keep their global decision', async () => {
  for (const stale of [false, true]) {
    const saved = withNaturalContinuation(completedSuggestionFixture(), false, 'session_identity_unknown');
    if (stale) {
      saved.continuation.recipients = { claude: { can_send: true, reason: null } };
      saved.continuation.binding.revision += 1;
    }
    const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
    await ui.choose('claude'); ui.type('不能由前端猜测放行'); await ui.submit();
    assert.equal(ui.get('send').disabled, true); assert.equal(ui.posts().length, 0);
    assert.equal(ui.get('message').value, '不能由前端猜测放行');
  }
});

test('merge requests use their own recipient projection without borrowing or replacing the composer recipient', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture(), false, 'session_identity_unknown');
  saved.continuation.recipients = { claude: { can_send: true, reason: null }, codex: { can_send: false, reason: 'session_identity_unknown' } };
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, codeStatusSupport: true,
    route(request) { if (request.path.includes('/code-status')) return mainlineCodeFixture(); if (request.path.endsWith('/messages')) return saved; } });
  ui.type('原草稿不参与合并请求'); await ui.choose('codex');
  const target = ui.get('code-merge-target'); assert.equal(ui.get('code-merge-send').disabled, true);
  target.value = 'claude'; target.fire('change'); assert.equal(ui.get('code-merge-send').disabled, false);
  target.value = 'codex'; target.fire('change'); assert.equal(ui.get('code-merge-send').disabled, true);
  target.value = 'claude'; target.fire('change'); await ui.click('code-merge-send');
  const sent = ui.posts().filter((request) => request.path.endsWith('/messages'));
  assert.equal(sent.length, 1); assert.equal(sent[0].body.target, 'claude');
  assert.equal(ui.get('message').value, '原草稿不参与合并请求'); assert.match(ui.get('recipient-button').textContent, /Codex/);
});


test('code card distinguishes merged commits from untracked entries without claiming a remote live check', async () => {
  const saved = completedSuggestionFixture(); const data = mainlineCodeFixture();
  data.mainline = { state: 'ready', name: 'main', relation: 'merged', ahead: 0, behind: 0 };
  data.merge_request.available = false;
  data.uncommitted = { total: 20, tracked: 0, untracked: 20, staged: 0, unstaged: 0, conflicted: 0 };
  data.upstream = { state: 'ready', name: 'origin/feature', ahead: 0, behind: 0 };
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, codeStatusSupport: true,
    route(request) { if (request.path.includes('/code-status')) return data; } });
  assert.equal(ui.get('code-local-summary').textContent, '已提交内容已合入 main');
  assert.equal(ui.get('code-local-summary').classList.contains('is-clean'), true);
  assert.equal(ui.get('code-tracked-status').textContent, '已跟踪文件：无待提交改动');
  assert.match(ui.get('code-untracked-status').textContent, /20 项未纳入 Git/);
  assert.equal(ui.get('code-untracked-note').hidden, false);
  assert.match(html, /这些文件或目录不在已有提交里/);
  assert.match(ui.get('code-upstream').textContent, /本地记录/);
  assert.doesNotMatch(ui.get('code-status-card').textContent, /远程已同步|GitHub：已核对推送|全代码已合/);
  assert.equal(ui.get('code-merge-panel').hidden, false);
  data.uncommitted = { total: 21, tracked: 1, untracked: 20, staged: 0, unstaged: 1, conflicted: 0 };
  await ui.click('code-status-refresh');
  assert.equal(ui.get('code-local-summary').textContent, '有 1 个已跟踪文件待提交');
  assert.equal(ui.get('code-local-summary').classList.contains('is-clean'), false);
  assert.match(ui.get('code-mainline').textContent, /已提交内容已合入主分支 main/);
  data.uncommitted.conflicted = 1; await ui.click('code-status-refresh');
  assert.match(ui.get('code-local-summary').textContent, /冲突待处理/);
  assert.equal(ui.posts().length, 0);
});

test('code card does not invent merge or push completion when local references are unknown', async () => {
  const saved = completedSuggestionFixture(); const data = mainlineCodeFixture();
  data.mainline = { state: 'unknown', relation: 'unknown' }; data.merge_request.available = false;
  data.upstream = { state: 'unknown' };
  data.uncommitted = { total: 2, tracked: 0, untracked: 2, staged: 0, unstaged: 0, conflicted: 0 };
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, codeStatusSupport: true,
    route(request) { if (request.path.includes('/code-status')) return data; } });
  assert.equal(ui.get('code-local-summary').textContent, '已跟踪文件无待提交改动');
  assert.match(ui.get('code-mainline').textContent, /主分支尚未确认/);
  assert.equal(ui.get('code-upstream').textContent, '跟踪分支状态不可用');
  assert.equal(ui.get('code-local-summary').classList.contains('is-clean'), false);
  data.mainline = { state: 'ready', name: 'main', relation: 'same_branch' };
  data.upstream = { state: 'ready', name: 'origin/main', ahead: 2, behind: 0 };
  await ui.click('code-status-refresh');
  assert.equal(ui.get('code-local-summary').textContent, '当前在主分支 main');
  assert.match(ui.get('code-upstream').textContent, /领先 2/);
  assert.equal(ui.posts().length, 0);
});

test('code file details show all returned entries and explicitly disclose an incomplete response', async () => {
  const saved = completedSuggestionFixture(); const data = projectCodeFixture();
  data.files = Array.from({ length: 23 }, (_, index) => ({ path: `new/entry-${index}`, index: '?', worktree: '?' }));
  data.uncommitted = { total: 23, tracked: 0, untracked: 23, staged: 0, unstaged: 0, conflicted: 0 };
  data.files_truncated = false;
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true,
    route(request) { if (request.path.includes('/code-status')) return data; } });
  assert.equal(ui.get('code-file-list').children.length, 23);
  assert.match(ui.get('code-file-list').textContent, /未纳入 Git · new\/entry-22/);
  assert.equal(ui.get('code-files-more').hidden, true);
  data.files = data.files.slice(0, 21); await ui.click('code-status-refresh');
  assert.equal(ui.get('code-file-list').children.length, 21);
  assert.equal(ui.get('code-files-more').hidden, false);
  assert.match(ui.get('code-files-more').textContent, /另有 2 个文件/);
  assert.equal(ui.posts().length, 0);
});

function taskCodeDeliveryFixture(state = 'confirmed') {
  const data = mainlineCodeFixture();
  data.mainline = { state: 'ready', name: 'main', relation: 'merged' }; data.merge_request.available = false;
  data.uncommitted = { total: 20, tracked: 0, untracked: 20, staged: 0, unstaged: 0, conflicted: 0 };
  data.delivery_summary = { scope: 'task_linked_workspaces', state,
    main: { state: 'confirmed', reason: null }, remote: { state, reason: null, checked_at: 1700000000 },
    workspace: { tracked: 0, untracked: 20 }, coverage: { repositories: 1, private_workspaces: 3 },
    request: { available: true, repository_roots: ['/projects/示例 项目'], targets: [{ repository_root: '/projects/示例 项目', target_branch: 'master' }] } };
  return data;
}

test('task code delivery is compact and separates confirmed commits from files still needing a partner decision', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture()); const data = taskCodeDeliveryFixture();
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true, naturalContinuationSupport: true,
    route(request) { if (request.path.includes('/code-status')) return data; } });
  assert.equal(ui.get('code-status-summary').textContent, '已提交代码已合入并同步 GitHub');
  assert.equal(ui.get('code-delivery-main').textContent, '主分支：已提交代码已合入');
  assert.equal(ui.get('code-delivery-remote').textContent, 'GitHub：当前主分支已同步');
  assert.equal(ui.get('code-status-details').open, false);
  assert.equal(ui.get('code-merge-panel').hidden, false); assert.equal(ui.get('code-merge-send').disabled, false);
  assert.ok(html.indexOf('<details id="code-status-details"') < html.indexOf('id="code-workspace-control"'));
  assert.equal(ui.posts().length, 0);
  data.delivery_summary.state = 'needs_attention';
  await ui.click('code-status-refresh');
  assert.equal(ui.get('code-status-summary').textContent, '已提交代码已合入并同步 GitHub');
  data.delivery_summary.state = 'pending'; data.delivery_summary.main.state = 'pending';
  await ui.click('code-status-refresh');
  assert.equal(ui.get('code-status-summary').textContent, '还有任务成果未合入主分支');
  assert.equal(ui.get('code-delivery-main').textContent, '主分支：还有成果待处理，来源未提供具体原因');
  data.delivery_summary.state = 'unknown'; data.delivery_summary.remote.state = 'unknown';
  await ui.click('code-status-refresh');
  assert.equal(ui.get('code-status-summary').textContent, '还有任务成果未合入主分支');
  assert.equal(ui.get('code-delivery-remote').textContent, 'GitHub：来源未提供可确认的核对结果');
  assert.equal(ui.get('code-status-summary').classList.contains('is-clean'), false);
  data.delivery_summary.main.state = 'unknown'; data.delivery_summary.workspace.untracked = 0;
  await ui.click('code-status-refresh');
  assert.match(ui.get('code-delivery-main').textContent, /来源未提供可确认的核对结果/);
  assert.doesNotMatch(ui.get('code-delivery-note').textContent, /点击刷新核对 GitHub/);
});

// Shaped like the real code-status payload for a work-branch room (see test_code_delivery_work_branch_http.py).
function workBranchDeliveryFixture({ remote = { state: 'confirmed', reason: null }, upstream = { state: 'ready', name: 'origin/codex/ranger-world-time', ahead: 0, behind: 0 }, tracked = 0 } = {}) {
  const data = projectCodeFixture();
  const repository = { id: 'delivery:ranger', kind: 'delivery', label: '交付所在仓库 · ranger-world-time', path: '/worktrees/ranger-world-time' };
  const checkOf = (part) => ({ ...part, workspace_id: repository.id });
  const workspace = tracked ? { state: 'pending', reason: 'uncommitted_tracked_changes' } : { state: 'confirmed', reason: null };
  const main = { state: 'pending', reason: 'branch_not_on_mainline' };
  Object.assign(data, { workspaces: [repository, data.workspaces[1]], workspace_id: repository.id, branch: 'codex/ranger-world-time',
    observed_at: Math.floor(Date.now() / 1000) - 30, repository_root: repository.path,
    mainline: { state: 'ready', name: 'main', relation: 'unmerged', ahead: 9, behind: 0, scope: 'repository_local_refs' }, upstream,
    uncommitted: { total: tracked, tracked, staged: 0, unstaged: tracked, untracked: 0, conflicted: 0 }, files: [], files_truncated: false,
    delivery_repository: { state: 'bound', reason: null, workspace_id: repository.id, source: 'user', candidates: [{ workspace_id: repository.id, label: repository.label }] },
    delivery_summary: { scope: 'task_linked_workspaces', state: 'pending', reason: main.reason,
      main: { ...main, checks: [checkOf(main)] }, remote: { ...remote, checks: [checkOf(remote)], checked_at: null, cached: false },
      published: { state: 'confirmed', reason: null, files: { total: 0, matched: 0, missing: 0, different: 0, mode_unknown: 0 } },
      private: { state: 'confirmed', reason: null },
      issues: [{ scope: 'main', ...checkOf(main) }, ...(remote.state === 'confirmed' ? [] : [{ scope: 'remote', ...checkOf(remote) }]), ...(tracked ? [{ scope: 'workspace', ...checkOf(workspace), tracked, untracked: 0 }] : [])],
      workspace: { ...workspace, checks: [{ ...checkOf(workspace), tracked, untracked: 0 }], tracked, untracked: 0 },
      coverage: { repositories: 1, private_workspaces: 0, retained_workspaces: 0 },
      branch: { name: 'codex/ranger-world-time', detached: false, mainline: 'main', relation: 'unmerged', ahead: 9, behind: 0, work_branch: true, upstream },
      request: { available: true, repository_roots: [repository.path], targets: [{ repository_root: repository.path, target_branch: 'main' }] } } });
  return data;
}
function deliveryCell(ui, label) { return ui.get('delivery-steps').children.find((cell) => cell.firstChild.textContent.includes(label)); }
function deliveryCardBody(ui, label) { return deliveryCell(ui, label).children[1].textContent; }
async function workBranchBrowser(data, mutate = (saved) => saved) {
  const saved = withNaturalContinuation(completedSuggestionFixture()); mutate(saved);
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true, naturalContinuationSupport: true,
    route(request) { if (request.path.includes('/code-status')) return data; } });
  await ui.poll();
  return { ui, saved };
}
const unpushedThree = { remote: { state: 'pending', reason: 'work_branch_not_pushed' }, upstream: { state: 'ready', name: 'origin/codex/ranger-world-time', ahead: 3, behind: 0 } };

test('a pushed work branch reads as pushed, and being unmerged is a plain fact rather than a warning', async () => {
  // Catches the 2026-11-08 screen: orange "未合入" card, orange title and a badge for normal branch work.
  const { ui } = await workBranchBrowser(workBranchDeliveryFixture());
  assert.equal(ui.get('code-status-summary').textContent, '工作分支 codex/ranger-world-time 已推送');
  assert.equal(ui.get('code-status-summary').classList.contains('is-dirty'), false);
  assert.equal(deliveryCardBody(ui, '主分支'), '在工作分支 codex/ranger-world-time 上，比 main 多 9 次提交');
  assert.equal(deliveryCell(ui, '主分支').classList.contains('pending'), false);
  assert.equal(deliveryCell(ui, '主分支').classList.contains('normal'), true);
  assert.equal(deliveryCardBody(ui, '远端'), '工作分支已推送');
  assert.equal(deliveryCell(ui, '远端').classList.contains('confirmed'), true);
  assert.equal(ui.get('code-delivery-remote').textContent, '远端：工作分支已推送');
  assert.equal(ui.get('tab-delivery-count').hidden, true);
  assert.match(ui.get('code-status-freshness').textContent, /推送状态来自本机跟踪记录，未联网核对/);
  assert.doesNotMatch(ui.get('code-status-freshness').textContent, /GitHub 检查于/);
  assert.equal(ui.get('code-repository-label').textContent, '交付仓库：交付所在仓库 · ranger-world-time · codex/ranger-world-time');
});

test('a finished task with unpushed commits on a work branch must remind, with the real count', async () => {
  // Catches the green "GitHub synced" tick that hid unpushed commits on the work branch.
  const { ui } = await workBranchBrowser(workBranchDeliveryFixture(unpushedThree));
  assert.equal(ui.get('code-status-summary').textContent, '工作分支 codex/ranger-world-time 还有 3 次提交没推送');
  assert.equal(ui.get('code-status-summary').classList.contains('is-dirty'), true);
  assert.equal(deliveryCardBody(ui, '远端'), '工作分支还有 3 次提交没推送');
  assert.equal(deliveryCell(ui, '远端').classList.contains('pending'), true);
  assert.equal(ui.get('tab-delivery-count').hidden, false);
  assert.equal(ui.get('tab-delivery-count').textContent, '1');
});

test('a work branch without a remote reminds and an unreadable upstream is reported as unknown', async () => {
  // Catches "no upstream" and "unknown" both collapsing into a reassuring or a guessed state.
  const none = await workBranchBrowser(workBranchDeliveryFixture({ remote: { state: 'pending', reason: 'work_branch_no_upstream' }, upstream: { state: 'none', name: null, ahead: null, behind: null } }));
  assert.equal(deliveryCardBody(none.ui, '远端'), '工作分支未设远端');
  assert.equal(deliveryCell(none.ui, '远端').classList.contains('pending'), true);
  assert.equal(none.ui.get('code-status-summary').textContent, '工作分支 codex/ranger-world-time 未设远端');
  assert.equal(none.ui.get('tab-delivery-count').textContent, '1');
  const unknown = await workBranchBrowser(workBranchDeliveryFixture({ remote: { state: 'unknown', reason: 'work_branch_upstream_unknown' }, upstream: { state: 'unknown', name: 'origin/x', ahead: null, behind: null } }));
  assert.equal(deliveryCardBody(unknown.ui, '远端'), '工作分支的推送状态未知');
  assert.equal(deliveryCell(unknown.ui, '远端').classList.contains('unknown'), true);
  assert.equal(unknown.ui.get('code-status-summary').textContent, '工作分支 codex/ranger-world-time 的推送状态未知');
  assert.equal(unknown.ui.get('code-status-summary').classList.contains('is-dirty'), false);
  assert.equal(unknown.ui.get('tab-delivery-count').hidden, true);
});

test('during a run uncommitted and unpushed work is shown as in progress, and reminds once the run ends', async () => {
  // Catches process state turning the badge orange mid-run, and a reminder that never comes back afterwards.
  const data = workBranchDeliveryFixture({ ...unpushedThree, tracked: 2 });
  const { ui, saved } = await workBranchBrowser(data, (value) => { value.room.work_activity = { state: 'working', working_jobs: 1, queued_jobs: 0, draining: false }; });
  for (const label of ['工作区', '远端']) {
    assert.equal(deliveryCell(ui, label).classList.contains('running'), true, label);
    assert.equal(deliveryCell(ui, label).classList.contains('pending'), false, label);
    assert.match(deliveryCell(ui, label).firstChild.textContent, /进行中/, label);
  }
  assert.equal(ui.get('tab-delivery-count').hidden, true);
  assert.equal(ui.get('code-status-summary').textContent, '上次检查：任务进行中，工作分支 codex/ranger-world-time 还在更新');
  assert.equal(ui.get('code-status-summary').classList.contains('is-dirty'), false);
  assert.equal(ui.get('code-local-summary').classList.contains('is-dirty'), false);
  saved.room.work_activity = { state: 'idle', working_jobs: 0, queued_jobs: 0, draining: false }; await ui.poll(); await ui.poll();
  assert.equal(deliveryCell(ui, '工作区').classList.contains('pending'), true);
  assert.equal(deliveryCell(ui, '远端').classList.contains('pending'), true);
  assert.equal(deliveryCell(ui, '主分支').classList.contains('pending'), false);
  assert.equal(ui.get('tab-delivery-count').textContent, '2');
  assert.equal(ui.get('code-status-summary').textContent, '还有工作区成果待提交');
  assert.equal(ui.get('code-status-summary').classList.contains('is-dirty'), true);
});

test('uncommitted changes on mainline during a run are in progress but an unpushed mainline keeps its original verdict', async () => {
  // Catches the running rule either missing mainline work or softening GitHub main's own check.
  const data = taskCodeDeliveryFixture();
  data.delivery_summary.workspace = { state: 'pending', reason: 'uncommitted_tracked_changes', checks: [{ state: 'pending', reason: 'uncommitted_tracked_changes', workspace_id: 'project' }], tracked: 2, untracked: 0 };
  data.delivery_summary.remote = { state: 'pending', reason: 'local_mainline_not_on_remote', checked_at: 1700000000 };
  data.delivery_summary.branch = { name: 'master', detached: false, mainline: 'master', relation: 'same_branch', ahead: 0, behind: 0, work_branch: false, upstream: { state: 'ready', name: 'origin/master', ahead: 1, behind: 0 } };
  data.observed_at = Math.floor(Date.now() / 1000) - 30;
  const { ui } = await workBranchBrowser(data, (value) => { value.room.work_activity = { state: 'working', working_jobs: 1, queued_jobs: 0, draining: false }; });
  assert.equal(deliveryCell(ui, '工作区').classList.contains('running'), true);
  assert.equal(deliveryCell(ui, 'GitHub').classList.contains('pending'), true);
  assert.equal(deliveryCardBody(ui, 'GitHub'), '当前主分支还有提交未同步');
  assert.equal(ui.get('tab-delivery-count').textContent, '1');
});

test('on mainline a commit that is not on the mainline keeps the original warning', async () => {
  // Catches the neutral treatment reaching anything but a real work branch.
  const data = taskCodeDeliveryFixture();
  data.delivery_summary.state = 'pending';
  data.delivery_summary.main = { state: 'pending', reason: 'branch_not_on_mainline', checks: [{ state: 'pending', reason: 'branch_not_on_mainline', workspace_id: 'project' }] };
  data.delivery_summary.issues = [{ scope: 'main', state: 'pending', reason: 'branch_not_on_mainline', workspace_id: 'project' }];
  data.delivery_summary.branch = { name: 'master', detached: false, mainline: 'master', relation: 'merged', ahead: 0, behind: 2, work_branch: false, upstream: { state: 'none', name: null, ahead: null, behind: null } };
  data.delivery_repository = { state: 'bound', reason: null, workspace_id: 'project', source: 'user', candidates: [{ workspace_id: 'project', label: '关联项目' }] };
  const { ui } = await workBranchBrowser(data);
  assert.equal(ui.get('code-status-summary').textContent, '还有任务成果未合入主分支');
  assert.equal(ui.get('code-status-summary').classList.contains('is-dirty'), true);
  assert.equal(deliveryCell(ui, '主分支').classList.contains('pending'), true);
  assert.equal(deliveryCardBody(ui, '主分支'), '还有分支上的提交未合入');
  assert.equal(deliveryCardBody(ui, 'GitHub'), '当前主分支已同步');
  assert.equal(ui.get('tab-delivery-count').textContent, '1');
});

test('the delivery repository name is repeated in the cards only when there is more than one', async () => {
  // Catches "（交付所在仓库 · 目录名）" on every card when the line above already names it.
  const repeated = (count) => {
    const data = workBranchDeliveryFixture(); delete data.delivery_summary.branch;
    data.delivery_summary.coverage.repositories = count; return data;
  };
  const single = await workBranchBrowser(repeated(1));
  assert.equal(deliveryCardBody(single.ui, '主分支'), '还有分支上的提交未合入');
  assert.doesNotMatch(single.ui.get('code-delivery-main').textContent, /交付所在仓库/);
  const multiple = await workBranchBrowser(repeated(2));
  assert.equal(multiple.ui.get('code-delivery-main').textContent, '主分支：还有分支上的提交未合入（交付所在仓库 · ranger-world-time）');
});

test('results older than ten minutes during a run say how old they are, without any extra Git read', async () => {
  // Catches a 37-minute-old answer looking like the current state, and a fix that adds polling.
  const working = (value) => { value.room.work_activity = { state: 'working', working_jobs: 1, queued_jobs: 0, draining: false }; };
  const now = Math.floor(Date.now() / 1000);
  const old = workBranchDeliveryFixture(); old.observed_at = now - 37 * 60 - 5;
  const opened = await workBranchBrowser(old, working);
  const reads = () => opened.ui.requests.filter((request) => request.path.includes('/code-status')).length;
  assert.equal(opened.ui.get('code-status-summary').textContent, '37 分钟前的结果：任务进行中，工作分支 codex/ranger-world-time 还在更新');
  assert.equal(opened.ui.get('code-delivery-main').textContent, '37 分钟前的结果：主分支：在工作分支 codex/ranger-world-time 上，比 main 多 9 次提交');
  assert.match(opened.ui.get('code-status-freshness').textContent, /这是 37 分钟前的旧结果，点刷新查看最新状态/);
  const before = reads(); await opened.ui.poll(); await opened.ui.poll(); assert.equal(reads(), before);
  const hours = workBranchDeliveryFixture(); hours.observed_at = now - 125 * 60;
  assert.match((await workBranchBrowser(hours, working)).ui.get('code-status-summary').textContent, /^2 小时前的结果：/);
  const recent = workBranchDeliveryFixture(); recent.observed_at = now - 9 * 60;
  const fresh = await workBranchBrowser(recent, working);
  assert.match(fresh.ui.get('code-status-summary').textContent, /^上次检查：/);
  assert.doesNotMatch(fresh.ui.get('code-status-freshness').textContent, /旧结果/);
  const idle = workBranchDeliveryFixture(); idle.observed_at = now - 37 * 60;
  const finished = await workBranchBrowser(idle);
  assert.equal(finished.ui.get('code-status-summary').textContent, '工作分支 codex/ranger-world-time 已推送');
});

test('the merge request says it merges into main and pushes, and English cards drop the repeated scope prefix', async () => {
  // Catches a vague "sync" button in a room that must not touch main, and "Remote: Remote: ..." card text.
  assert.match(html, /请伙伴提交、合入主分支并推送/); assert.doesNotMatch(html, /请伙伴提交并同步/);
  const saved = withNaturalContinuation(completedSuggestionFixture());
  const ui = await browser({ language: 'en', rooms: [saved.room], saved, codeStatusSupport: true, naturalContinuationSupport: true,
    route(request) { if (request.path.includes('/code-status')) return workBranchDeliveryFixture(unpushedThree); } });
  await ui.poll();
  assert.equal(deliveryCardBody(ui, 'Remote'), 'Work branch has 3 unpushed commits');
  assert.equal(deliveryCardBody(ui, 'Main branch'), 'On work branch codex/ranger-world-time, 9 commits ahead of main');
  assert.equal(ui.get('code-status-summary').textContent, 'Work branch codex/ranger-world-time has 3 unpushed commits');
  assert.match(ui.get('code-repository-label').textContent, /codex\/ranger-world-time$/);
});

test('initial code display checks remote once while polling and workspace detail reads stay local', async () => {
  const saved = completedSuggestionFixture(); const data = taskCodeDeliveryFixture('unknown');
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true,
    route(request) { if (request.path.includes('/code-status')) return { ...data, workspace_id: new URL(`http://fixture${request.path}`).searchParams.get('workspace') || data.workspace_id }; } });
  assert.equal(ui.requests.filter((request) => request.path.includes('/code-status')).length, 1);
  assert.match(ui.requests.filter((request) => request.path.includes('/code-status'))[0].path, /refresh_remote=1/);
  await ui.poll(); await ui.poll();
  assert.equal(ui.requests.filter((request) => request.path.includes('/code-status')).length, 1);
  await ui.click('code-status-refresh');
  assert.match(ui.requests.filter((request) => request.path.includes('/code-status')).at(-1).path, /refresh_remote=1/);
  ui.get('code-workspace').value = 'native:codex'; ui.get('code-workspace').fire('change'); await settle();
  const last = ui.requests.filter((request) => request.path.includes('/code-status')).at(-1);
  assert.match(last.path, /workspace=native%3Acodex/); assert.doesNotMatch(last.path, /refresh_remote/);
  assert.equal(ui.posts().length, 0);
});

test('remote freshness uses the real check time and local-only reads preserve that timestamp', async () => {
  const saved = completedSuggestionFixture(); const data = taskCodeDeliveryFixture();
  data.observed_at = 1700086400; data.delivery_summary.remote.checked_at = 1700000000;
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true,
    route(request) { if (request.path.includes('/code-status')) return { ...data, workspace_id: new URL(`http://fixture${request.path}`).searchParams.get('workspace') || data.workspace_id }; } });
  const initial = ui.get('code-status-freshness').textContent;
  assert.match(initial, /GitHub 检查于 /);
  const remoteTime = initial.split('GitHub 检查于 ')[1]; assert.ok(remoteTime);
  data.observed_at += 86400; data.delivery_summary.remote.cached = true;
  ui.get('code-workspace').value = 'native:codex'; ui.get('code-workspace').fire('change'); await settle();
  assert.notEqual(ui.get('code-status-freshness').textContent, initial);
  assert.equal(ui.get('code-status-freshness').textContent.split('GitHub 检查于 ')[1], remoteTime);
  assert.equal(ui.get('code-delivery-remote').textContent, 'GitHub：当前主分支已同步');
  delete data.delivery_summary.remote.checked_at; await ui.click('code-status-refresh');
  assert.doesNotMatch(ui.get('code-status-freshness').textContent, /GitHub 检查于 /);
  assert.equal(ui.posts().length, 0);
});

test('a local read with an invalidated bound remote performs one guarded check without retrying other failures', async () => {
  for (const result of ['confirmed', 'not_checked', 'git_timeout', 'binding_changed', 'network_error']) {
    const saved = withNaturalContinuation(completedSuggestionFixture()); const { data, bind } = deliveryRepositoryFixture(); bind('delivery:app');
    let mode = 'initial'; let reads = 0;
    const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true, naturalContinuationSupport: true, route(request) {
      if (!request.path.includes('/code-status')) return;
      reads++; const url = new URL(`http://fixture${request.path}`); const reply = structuredClone(data);
      if (mode !== 'initial' && result === 'network_error' && url.searchParams.has('refresh_remote')) throw new Error('fictional remote read failure');
      reply.workspace_id = url.searchParams.get('workspace') || data.workspace_id;
      if (mode !== 'initial') {
        const remoteReason = url.searchParams.has('refresh_remote') ? result : (result === 'git_timeout' ? 'git_timeout' : 'not_checked');
        reply.delivery_summary.remote = { state: remoteReason === 'confirmed' ? 'confirmed' : 'unknown', reason: remoteReason === 'confirmed' ? null : remoteReason };
        if (remoteReason === 'binding_changed') reply.delivery_repository.workspace_id = 'delivery:control';
      }
      return reply;
    } });
    mode = 'local'; ui.type('保留远端核查期间的草稿'); await ui.choose('codex');
    ui.get('code-workspace').value = 'delivery:control'; ui.get('code-workspace').fire('change'); await settle();
    assert.equal(reads, result === 'git_timeout' ? 2 : 3);
    const last = ui.requests.filter((request) => request.path.includes('/code-status')).at(-1);
    assert.match(last.path, /workspace=delivery%3Acontrol/);
    if (result === 'git_timeout') assert.doesNotMatch(last.path, /refresh_remote/);
    else assert.match(last.path, /refresh_remote=1/);
    if (result === 'binding_changed') assert.match(ui.get('code-local-summary').textContent, /交付仓库已变化/);
    if (result === 'network_error') {
      assert.match(ui.get('code-local-summary').textContent, /本次代码查询未完成/);
      assert.equal(ui.get('code-status-summary').classList.contains('is-clean'), false);
    }
    if (result === 'confirmed') assert.equal(ui.get('code-delivery-remote').textContent, 'GitHub：当前主分支已同步');
    await ui.poll(); await ui.poll(); assert.equal(reads, result === 'git_timeout' ? 2 : 3);
    assert.equal(ui.get('message').value, '保留远端核查期间的草稿'); assert.match(ui.get('recipient-button').textContent, /Codex/);
    assert.equal(ui.posts().length, 0);
  }
});

test('submission and sync sends one ordinary directed request even after a merge and preserves composer drafts', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture()); const data = taskCodeDeliveryFixture();
  const held = deferred(); let dispatched; const peer = hostOutboxPeer({ onDispatch(request, row) { dispatched = { request, row }; return held.promise; } });
  const ui = await browser({ rooms: [saved.room], saved: pagedFixture(saved, []), codeStatusSupport: true,
    naturalContinuationSupport: true, pagingSupport: true, hostOutboxScope: peer.scope,
    route: async (request) => {
      if (request.path.includes('/code-status')) return data;
      const result = await peer.route(request); if (result !== undefined) return result;
      if (request.path.endsWith('/state')) return pagedFixture(saved, []);
      return eventPageRoute(request, []);
    } });
  ui.type('未发送的新要求'); await ui.choose('codex');
  ui.get('code-merge-target').value = 'claude'; ui.get('code-merge-target').fire('change');
  ui.get('code-merge-send').fire('click'); ui.get('code-merge-send').fire('click'); await settle();
  assert.equal(peer.dispatchCalls, 1); assert.equal(dispatched.request.body.target, 'claude');
  assert.match(dispatched.request.body.text, /提交尚未提交的有效改动.*实际默认主分支.*GitHub.*实际远端提交/);
  assert.match(dispatched.request.body.text, /只提交本任务应纳入版本管理的内容/);
  assert.doesNotMatch(dispatched.request.body.text, /主分支 main|git push --force/);
  assert.match(dispatched.request.body.text, /示例 项目 → master/);
  dispatched.row.delivery = { state: 'received', can_send: false, can_retire: true, reason: null, result_room_id: saved.room.id };
  held.resolve({ acknowledged: true, room_id: saved.room.id, request_id: dispatched.row.request_id }); await settle();
  assert.equal(ui.get('message').value, '未发送的新要求'); assert.match(ui.get('recipient-button').textContent, /Codex/);
  assert.equal(ui.posts().some((request) => /\/merge|\/git/.test(request.path)), false);
});

test('a non Git registered folder cannot hide a valid task delivery summary from linked repositories', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture()); const data = taskCodeDeliveryFixture();
  data.state = 'not_git'; data.reason = 'not_git_repository'; data.repository_root = null;
  data.uncommitted = null; data.last_commit = null; data.files = []; data.upstream = null; data.mainline = null;
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true, naturalContinuationSupport: true,
    route(request) { if (request.path.includes('/code-status')) return data; } });
  assert.equal(ui.get('code-local-summary').textContent, '此目录未使用 Git');
  assert.equal(ui.get('code-status-summary').textContent, '已提交代码已合入并同步 GitHub');
  assert.equal(ui.get('code-delivery-states').hidden, false);
  assert.equal(ui.get('code-delivery-main').textContent, '主分支：已提交代码已合入');
  assert.equal(ui.get('code-delivery-remote').textContent, 'GitHub：当前主分支已同步');
  assert.equal(ui.get('code-merge-panel').hidden, false);
  assert.equal(ui.get('code-merge-send').disabled, false); assert.equal(ui.posts().length, 0);
});

test('unavailable selected native details and failed refresh retain a same-workspace delivery request with an explicit stale notice', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture()); const data = taskCodeDeliveryFixture('pending');
  const held = deferred(); let dispatched; let failed = false;
  const peer = hostOutboxPeer({ onDispatch(request, row) { dispatched = { request, row }; return held.promise; } });
  const ui = await browser({ rooms: [saved.room], saved: pagedFixture(saved, []), codeStatusSupport: true,
    naturalContinuationSupport: true, pagingSupport: true, hostOutboxScope: peer.scope,
    route: async (request) => {
      if (request.path.includes('/code-status')) {
        if (failed) throw new Error('local connection unavailable');
        const selected = new URL(`http://fixture${request.path}`).searchParams.get('workspace');
        return selected ? { ...data, workspace_id: selected, state: 'unavailable', reason: 'directory_unavailable',
          uncommitted: null, files: [], last_commit: null } : data;
      }
      const result = await peer.route(request); if (result !== undefined) return result;
      if (request.path.endsWith('/state')) return pagedFixture(saved, []);
      return eventPageRoute(request, []);
    } });
  ui.type('原草稿'); await ui.choose('codex');
  ui.get('code-workspace').value = 'native:codex'; ui.get('code-workspace').fire('change'); await settle();
  assert.equal(ui.get('code-local-summary').textContent, '项目目录暂不可用');
  assert.equal(ui.get('code-status-summary').textContent, '主分支已合入，GitHub 还有内容待同步');
  assert.equal(ui.get('code-delivery-states').hidden, false); assert.equal(ui.get('code-merge-panel').hidden, false);
  ui.get('code-merge-target').value = 'claude'; ui.get('code-merge-target').fire('change');
  assert.equal(ui.get('code-merge-send').disabled, false);
  await ui.click('code-merge-send'); assert.equal(peer.dispatchCalls, 1);
  assert.equal(dispatched.request.body.target, 'claude'); assert.match(dispatched.request.body.text, /关联仓库：\/projects\/示例 项目/);
  dispatched.row.delivery = { state: 'received', can_send: false, can_retire: true, reason: null, result_room_id: saved.room.id };
  held.resolve({ acknowledged: true, room_id: saved.room.id, request_id: dispatched.row.request_id }); await settle();
  assert.equal(ui.get('message').value, '原草稿'); assert.match(ui.get('recipient-button').textContent, /Codex/);
  failed = true; await ui.click('code-status-refresh');
  assert.equal(ui.get('code-merge-panel').hidden, false);
  assert.equal(ui.get('code-status-summary').classList.contains('is-clean'), false);
  assert.match(ui.get('code-status-summary').textContent, /上次|未成功|超时|失败/);
  assert.equal(ui.get('code-merge-send').disabled, false);
});

test('task code clarity explains missing task provenance while GitHub and untracked assets keep separate scopes', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture()); const data = taskCodeDeliveryFixture('unknown');
  data.delivery_summary.main = { state: 'unknown', reason: 'published_work_baseline_unknown' };
  data.delivery_summary.remote = { state: 'confirmed', reason: null };
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true, naturalContinuationSupport: true,
    route(request) { if (request.path.includes('/code-status')) return data; } });
  assert.match(ui.get('code-delivery-main').textContent, /缺少本任务起点记录/);
  assert.equal(ui.get('code-delivery-remote').textContent, 'GitHub：当前主分支已同步');
  assert.match(ui.get('code-status-summary').textContent, /任务成果尚未核齐/);
  assert.match(ui.get('code-delivery-note').textContent, /不能据此判断全部交付完成/);
  assert.match(ui.get('code-untracked-status').textContent, /20/);
  assert.doesNotMatch(ui.get('code-status-summary').textContent, /全部.*完成|交付状态待核实/);
  assert.equal(ui.get('code-status-details').open, false); assert.equal(ui.posts().length, 0);
});

test('task code clarity keeps a known mainline blocker visible when GitHub is unknown', async () => {
  const saved = completedSuggestionFixture(); const data = taskCodeDeliveryFixture('pending');
  data.delivery_summary.main = { state: 'pending', reason: 'private_work_not_published' };
  data.delivery_summary.remote = { state: 'unknown', reason: 'not_checked' };
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true,
    route(request) { if (request.path.includes('/code-status')) return data; } });
  assert.equal(ui.get('code-status-summary').textContent, '还有任务成果未合入主分支');
  assert.match(ui.get('code-delivery-main').textContent, /伙伴工作区还有成果未提交/);
  assert.match(ui.get('code-delivery-remote').textContent, /尚未检查远端.*刷新/);
  assert.match(ui.get('code-untracked-status').textContent, /20/);
  data.delivery_summary.main = { state: 'confirmed', reason: null };
  data.delivery_summary.remote = { state: 'unknown', reason: 'git_timeout' };
  await ui.click('code-status-refresh');
  assert.equal(ui.get('code-status-summary').textContent, '主分支已合入，GitHub 尚未核对');
  assert.match(ui.get('code-delivery-remote').textContent, /读取状态超时/);
  data.delivery_summary.remote = { state: 'pending', reason: 'local_mainline_not_on_remote' };
  await ui.click('code-status-refresh');
  assert.match(ui.get('code-delivery-remote').textContent, /当前主分支还有提交未同步/);
  assert.equal(ui.posts().length, 0);
});

test('task code clarity separates confirmed committed code from unreadable private work without claiming delivery complete', async () => {
  const saved = completedSuggestionFixture(); const data = taskCodeDeliveryFixture('unknown');
  data.delivery_summary.main = { state: 'confirmed', reason: null };
  data.delivery_summary.remote = { state: 'confirmed', reason: null };
  data.delivery_summary.private = { state: 'unknown', reason: 'private_workspace_unavailable' };
  data.delivery_summary.issues = [{ scope: 'private', state: 'unknown', reason: 'private_workspace_unavailable', workspace_id: 'native:codex' }];
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true,
    route(request) { if (request.path.includes('/code-status')) return data; } });
  assert.equal(ui.get('code-delivery-main').textContent, '主分支：已提交代码已合入');
  assert.equal(ui.get('code-delivery-remote').textContent, 'GitHub：当前主分支已同步');
  assert.equal(ui.get('code-delivery-private').hidden, false);
  assert.match(ui.get('code-delivery-private').textContent, /暂时无法读取伙伴工作区/);
  assert.match(ui.get('code-status-summary').textContent, /已提交代码已同步.*工作区成果尚未核齐/);
  assert.match(ui.get('code-delivery-private').textContent, /Codex/);
  assert.doesNotMatch(ui.get('code-delivery-private').textContent, /native:|\/private/);
  assert.equal(ui.get('code-status-summary').classList.contains('is-clean'), false);
  assert.match(ui.get('code-untracked-status').textContent, /20/);
  assert.equal(ui.posts().length, 0);
});

test('task code clarity keeps every supplied blocker and separates private pending work from GitHub pending commits', async () => {
  const saved = completedSuggestionFixture(); const data = taskCodeDeliveryFixture('pending');
  data.delivery_summary.main = { state: 'confirmed', reason: null };
  data.delivery_summary.remote = { state: 'pending', reason: 'local_mainline_not_on_remote' };
  data.delivery_summary.private = { state: 'pending', reason: 'private_work_not_published' };
  data.delivery_summary.issues = [
    { scope: 'private', state: 'pending', reason: 'private_work_not_published' },
    { scope: 'private', state: 'unknown', reason: 'retained_work_changed' },
    { scope: 'private', state: 'unknown', reason: 'retained_work_changed' },
    { scope: 'remote', state: 'pending', reason: 'local_mainline_not_on_remote' },
  ];
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true,
    route(request) { if (request.path.includes('/code-status')) return data; } });
  assert.equal(ui.get('code-status-summary').textContent, '还有工作区成果待提交');
  assert.match(ui.get('code-delivery-main').textContent, /已提交代码已合入/);
  assert.match(ui.get('code-delivery-private').textContent, /还有成果未提交.*保留成果已变化/);
  assert.equal(ui.get('code-delivery-private').textContent.match(/保留成果已变化/g).length, 1);
  assert.match(ui.get('code-delivery-remote').textContent, /当前主分支还有提交未同步/);
  assert.equal(ui.posts().length, 0);
});

test('task code clarity shows partial delivery file mapping without undoing confirmed mainline and GitHub facts', async () => {
  const saved = completedSuggestionFixture(); const data = taskCodeDeliveryFixture('unknown');
  data.delivery_summary.main = { state: 'confirmed', reason: null };
  data.delivery_summary.remote = { state: 'confirmed', reason: null };
  data.delivery_summary.private = { state: 'confirmed', reason: null };
  data.delivery_summary.published = { state: 'unknown', reason: 'published_work_mapping_unknown',
    files: { total: 26, matched: 23, missing: 3, different: 0 } };
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true,
    route(request) { if (request.path.includes('/code-status')) return data; } });
  assert.equal(ui.get('code-status-summary').textContent, '已提交代码已合入并同步 GitHub');
  assert.equal(ui.get('code-delivery-main').textContent, '主分支：已提交代码已合入');
  assert.equal(ui.get('code-delivery-remote').textContent, 'GitHub：当前主分支已同步');
  assert.match(ui.get('code-published-note').textContent, /23\/26 项与主分支一致.*主分支未找到相同路径：3 项/);
  assert.ok(html.indexOf('<details id="code-status-details"') < html.indexOf('id="code-published-note"'));
  assert.doesNotMatch(ui.get('code-delivery-note').textContent, /23\/26|未找到相同路径/);
  assert.match(ui.get('code-untracked-status').textContent, /20/);
  assert.equal(ui.get('code-delivery-private').textContent, '伙伴工作区：无额外未交付改动');
  assert.equal(ui.get('code-status-summary').classList.contains('is-clean'), true);
  assert.equal(ui.get('code-status-details').open, false); assert.equal(ui.posts().length, 0);
  data.delivery_summary.published = { state: 'unknown', reason: 'published_work_unavailable', files: { total: null, matched: null, missing: null, different: null, mode_unknown: null } };
  await ui.click('code-status-refresh');
  assert.match(ui.get('code-published-note').textContent, /暂时无法读取/);
  assert.doesNotMatch(ui.get('code-published-note').textContent, /0\/0/);
});

test('Git delivery headline and color exclude cumulative snapshot mismatches but preserve real tracked changes', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture()); const data = taskCodeDeliveryFixture();
  const delivery = data.delivery_summary;
  delivery.private = { state: 'confirmed', reason: null };
  delivery.workspace = { state: 'confirmed', reason: null, tracked: 0, untracked: 21 };
  delivery.published = { state: 'pending', reason: 'published_work_not_on_mainline',
    files: { total: 49, matched: 30, missing: 17, different: 2 } };
  delivery.state = 'pending';
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true, naturalContinuationSupport: true,
    route(request) { if (request.path.includes('/code-status')) return data; } });
  ui.type('保持草稿'); await ui.choose('codex');
  for (const state of ['pending', 'unknown']) {
    delivery.state = state; delivery.published.state = state; await ui.click('code-status-refresh');
    assert.equal(ui.get('code-status-summary').textContent, '已提交代码已合入并同步 GitHub');
    assert.equal(ui.get('code-status-summary').classList.contains('is-clean'), true);
    assert.equal(ui.get('code-status-summary').classList.contains('is-dirty'), false);
    assert.match(ui.get('code-published-note').textContent, /30\/49.*未找到相同路径：17 项.*内容或文件属性不同：2 项/);
    assert.match(ui.get('code-published-note').textContent, /不代表待提交、待合并或必须发布/);
    assert.equal(ui.get('code-status-details').open, false);
    assert.equal(ui.get('code-merge-send').disabled, false);
  }
  delivery.workspace = { state: 'pending', reason: 'uncommitted_tracked_changes', tracked: 2, untracked: 21 };
  data.uncommitted.tracked = 2; await ui.click('code-status-refresh');
  assert.equal(ui.get('code-status-summary').textContent, '还有工作区成果待提交');
  assert.equal(ui.get('code-status-summary').classList.contains('is-dirty'), true);
  assert.equal(ui.get('code-status-summary').classList.contains('is-clean'), false);
  assert.match(ui.get('code-delivery-workspace').textContent, /已跟踪的改动未提交/);
  assert.equal(ui.get('code-merge-send').disabled, false);
  delivery.main.state = 'pending'; await ui.click('code-status-refresh');
  assert.equal(ui.get('code-status-summary').textContent, '还有任务成果未合入主分支');
  assert.equal(ui.get('code-status-summary').classList.contains('is-clean'), false);
  assert.equal(ui.get('message').value, '保持草稿'); assert.match(ui.get('recipient-button').textContent, /Codex/);
  assert.equal(ui.posts().length, 0);
});

test('completion clarity preserves every report paragraph and marks stage plans as historical readable sources', async () => {
  const saved = completedSuggestionFixture(null);
  saved.objective.next_step.summary = `${'已完成并核对本次约定内容。'.repeat(30)}\n\n原始交付说明。\n\n尚未覆盖：另一入口尚未验证；不能据此声称所有情况已修复。`;
  const stage = { id: 'PLAN_A', title: '任意项目的阶段安排', owner: 'codex', status: 'accepted', result: '阶段建议：先做后续核对。这个步骤后来已经完成。', result_version: 1, result_files: [] };
  saved.team.tasks = [stage]; saved.collaboration.tasks = [stage];
  saved.objective.checkpoint_proposal = { proposal_digest: 'c'.repeat(64) };
  saved.events.push({ kind: 'team_command', command: 'checkpoint', sender: 'claude', run_id: saved.current_run.id,
    objective_id: saved.objective.id, binding: saved.objective.next_step.binding, proposal_digest: 'c'.repeat(64), created_at: 1700000000, seq: 700 });
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  assert.equal(ui.document.querySelector('.completion-summary-preview').textContent, saved.objective.next_step.summary);
  assert.match(ui.document.querySelector('.completion-summary-preview').textContent, /尚未覆盖.*不能据此声称所有情况已修复/s);
  assert.match(ui.document.querySelector('.completion-report-source').textContent, /Claude Code/);
  assert.match(ui.document.querySelector('.completion-next').textContent, /模型本轮未提出新的执行目标/);
  assert.match(ui.document.querySelector('.completion-next').textContent, /阶段记录.*可能已在本轮完成/);
  const link = descendants(ui.document.querySelector('.completion-reply-links')).find((node) => node.tagName === 'BUTTON');
  assert.equal(link.textContent, stage.title); link.fire('click'); await settle();
  assert.match(ui.get('reader-body').textContent, /这个步骤后来已经完成/);
  assert.equal(ui.posts().length, 0);
});

test('completion advice is a bound draft question preserving recipient and draft rather than replaying a stage plan', async () => {
  let saved = completedSuggestionFixture(null);
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, route(request) {
    if (request.method === 'GET' && request.path === `/api/rooms/${roomId}`) return saved;
  } });
  await ui.choose('codex'); ui.type('先保留我的补充。');
  const question = '请基于本轮已完成内容和未覆盖项，建议现在最值得做的一步并说明理由；不要重复已完成工作。';
  await ui.click('ask-completion-advice'); await ui.click('ask-completion-advice');
  assert.equal(ui.get('message').value, `先保留我的补充。\n\n${question}`);
  assert.match(ui.get('recipient-button').textContent, /Codex/); assert.equal(ui.posts().length, 0);
  const old = ui.get('ask-completion-advice');
  saved = structuredClone(saved); saved.current_run.revision++; saved.continuation.binding.revision++;
  saved.objective.next_step.binding.revision++; await ui.poll(); ui.type('当前新草稿');
  old.fire('click'); await settle();
  assert.equal(ui.get('message').value, '当前新草稿'); assert.equal(ui.posts().length, 0);
});

test('quota clarity distinguishes temporary query failure from unsupported login and preserves no invented balance', async () => {
  const agent = { id: 'kimi', name: 'Kimi Code', avatar: 'K' };
  const account = accountFixture({ source: 'kimi_managed_oauth', email: null, plan: null, reason: 'provider_account_details_not_exposed' });
  account.quota = { state: 'unavailable', scope: 'provider_account', source: 'kimi_code_web_oauth_usage', checked_at: 1700000000, stale: false,
    windows: [], reason: 'provider_usage_temporarily_unavailable' };
  const partners = [{ ...agent, adapter: 'kimi', enabled: true, account }];
  const ui = await browser({ agents: [agent], partners });
  const footer = ui.get('provider-account-kimi');
  assert.match(footer.textContent, /额度查询服务暂时不可用/);
  assert.equal(footer.textContent.match(/额度查询服务暂时不可用/g).length, 1);
  assert.doesNotMatch(footer.textContent, /当前接入方式未支持|暂不提供账号与额度查询|额度已用完|剩余 0%/);
  assert.match(footer.textContent, /邮箱未获取|邮箱未提供/); assert.match(footer.textContent, /Kimi Code 本机会员登录/);
  assert.equal(descendants(footer).filter((node) => node.tagName === 'PROGRESS').length, 0);
  assert.equal(accountRefreshButton(footer).disabled, false); assert.equal(ui.posts().length, 0);
});


test('completion provenance never guesses an author from an unbound or rejected same-run checkpoint', async () => {
  for (const variant of ['missing-binding', 'different-version', 'different-proposal']) {
    const saved = completedSuggestionFixture(null);
    saved.objective.checkpoint_proposal = { proposal_digest: 'c'.repeat(64) };
    const event = { kind: 'team_command', command: 'checkpoint', sender: 'claude', run_id: saved.current_run.id,
      objective_id: saved.objective.id, binding: { ...saved.objective.next_step.binding }, proposal_digest: 'c'.repeat(64), created_at: 1700000000 };
    if (variant === 'missing-binding') delete event.binding;
    if (variant === 'different-version') event.binding.revision++;
    if (variant === 'different-proposal') event.proposal_digest = 'a'.repeat(64);
    saved.events.push(event);
    const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
    assert.equal(ui.document.querySelector('.completion-report-source').textContent, '本轮完成评估原文');
    assert.equal(ui.posts().length, 0);
  }
});


test('existing active partners can preview cancel and save ten outfits without sending work', async () => {
  const agents = catalog.slice(0, 2).map((agent) => ({ ...agent, bean_style: 'sprout', enabled: true }));
  const saved = snapshot({ agents, events: [message(1, 'claude', '原消息保持不变')], jobs: [{ id: jobId, provider: 'claude', status: 'working' }] });
  const immutable = structuredClone(saved);
  const response = () => ({ partners: agents, supported_adapters: [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }] });
  const ui = await browser({ agents, partners: agents, rooms: [saved.room], saved, route(request) {
    if (request.path === '/api/partners/claude/appearance') { agents[0].bean_style = request.body.bean_style; return response(); }
    if (request.path === '/api/partners' || request.path === '/api/partners?view=compact') return response();
  } });
  ui.type('保留我的草稿'); await ui.choose('codex');
  const open = () => descendants(ui.get('member-claude')).find((node) => node.dataset.appearance === 'claude').fire('click');
  open();
  assert.equal(ui.get('appearance-dialog').hidden, false);
  assert.equal(ui.get('appearance-picker').children.length, 10);
  const rose = ui.get('appearance-picker').children.at(-1); rose.fire('click');
  assert.equal(ui.get('appearance-preview').firstChild.classList.contains('bean-style-rose'), true);
  assert.equal(ui.get('member-claude').firstChild.firstChild.classList.contains('bean-style-sprout'), true, 'preview is not committed');
  await ui.click('appearance-cancel'); assert.equal(ui.posts().length, 0);
  open();
  const picker = ui.get('appearance-picker'); picker.firstChild.fire('keydown', { key: 'End' });
  assert.equal(picker.children.at(-1).getAttribute('aria-checked'), 'true');
  await ui.click('appearance-save');
  assert.deepEqual(ui.posts().map(({ path, body }) => ({ path, body })), [{ path: '/api/partners/claude/appearance', body: { bean_style: 'rose' } }]);
  assert.equal(ui.get('appearance-dialog').hidden, true);
  assert.equal(ui.get('member-claude').firstChild.firstChild.classList.contains('bean-style-rose'), true);
  assert.equal(ui.get('events').firstChild.firstChild.classList.contains('bean-style-rose'), true);
  assert.equal(ui.get('message').value, '保留我的草稿');
  assert.match(ui.get('recipient-button').textContent, /Codex/);
  assert.deepEqual(saved, immutable, 'cosmetic update never mutates the room response');
  await ui.poll();
  assert.equal(ui.get('member-claude').firstChild.firstChild.classList.contains('bean-style-rose'), true);
  open(); assert.equal(picker.children.at(-1).getAttribute('aria-checked'), 'true');
});

test('appearance saving failure preserves selection and can close without changing a profile', async () => {
  const ui = await browser({ route(request) { if (request.path.endsWith('/appearance')) throw new Error('offline'); } });
  descendants(ui.get('member-claude')).find((node) => node.dataset.appearance === 'claude').fire('click');
  ui.get('appearance-picker').children.at(-1).fire('click'); await ui.click('appearance-save');
  assert.equal(ui.get('appearance-error').hidden, false);
  assert.equal(ui.get('appearance-picker').children.at(-1).getAttribute('aria-checked'), 'true');
  assert.equal(ui.get('appearance-save').disabled, false);
  await ui.click('appearance-cancel'); assert.equal(ui.get('appearance-dialog').hidden, true);
});


test('ready native setup hides settled success and cancellation but keeps active login and failure', async () => {
  let setup = nativeSetupFixture('ready');
  const ui = await browser({ route(request) {
    if (request.path === '/api/partners') return { partners: catalog.slice(0, 2), supported_adapters: [setup] };
  } });
  ui.type('保留草稿'); await ui.choose('codex'); await ui.click('open-partners');
  assert.equal(ui.get('native-tool-setup').hidden, true);
  for (const state of ['completed', 'cancelled']) {
    setup = nativeSetupFixture('ready', { id: 'old-login', adapter: 'kimi', state }); await ui.poll();
    assert.equal(ui.get('native-tool-setup').hidden, true);
  }
  for (const state of ['starting', 'waiting', 'failed']) {
    setup = nativeSetupFixture('ready', { id: 'current-login', adapter: 'kimi', state }); await ui.poll();
    assert.equal(ui.get('native-tool-setup').hidden, false, state);
  }
  assert.match(ui.get('native-tool-setup').textContent, /登录没有完成/);
  assert.ok(nativeLoginButton(ui, 'nativeLoginStart'), 'a real failure keeps explicit retry available');
  assert.equal(ui.get('message').value, '保留草稿'); assert.match(ui.get('recipient-button').textContent, /Codex/);
  assert.equal(ui.posts().length, 0);
});

test('reviewing task lists name the exact reviewer separately from its owner without inventing later reviews', async () => {
  const saved = snapshot({ status: 'running', agents: catalog.slice(0, 2) });
  saved.team = { tasks: [{ id: 'T1', title: '[0%] 核对结果', owner: 'claude', reviewer: 'codex', status: 'review' }] };
  saved.collaboration = teamCollaboration({ tasks: [{ id: 'T1', title: '[0%] 核对结果', owner: 'claude', status: 'review', result: '', waiting_for: '等待独立审核' }] });
  const ui = await browser({ rooms: [saved.room], saved });
  const plan = ui.get('collaboration-plan');
  assert.match(plan.textContent, /核对结果等伙伴检查负责人：Claude Code本次审核：Codex/);
  assert.doesNotMatch(plan.textContent, /\[0%\]/);
  delete saved.team.tasks[0].reviewer; await ui.poll(); assert.doesNotMatch(plan.textContent, /本次审核：/);
  saved.team.tasks[0].reviewer = 'codex'; saved.team.tasks[0].status = 'accepted'; await ui.poll();
  assert.doesNotMatch(plan.textContent, /本次审核：/, 'a stale review projection never names an accepted task as currently reviewing');
  assert.equal(ui.posts().length, 0);
});


for (const phase of ['refresh', 'dispatch']) test(`a rendering exception cannot strand the ${phase} lock or discard a request`, async () => {
  const saved = snapshot({ agents: catalog.slice(0, 2) });
  const ui = await browser({ rooms: [saved.room], saved });
  ui.type('保留待确认的正文'); await ui.choose('codex');
  const node = ui.get('work-indicator-text'); const descriptor = Object.getOwnPropertyDescriptor(FakeElement.prototype, 'textContent');
  let fail = true;
  Object.defineProperty(node, 'textContent', { get() { if (fail) { fail = false; throw new Error('fictional transient render failure'); } return descriptor.get.call(this); }, set(value) { descriptor.set.call(this, value); }, configurable: true });
  if (phase === 'refresh') await ui.refresh();
  else {
    const request = { kind: 'message', roomId, body: { text: '保留待确认的正文', target: 'codex', request_id: '00000000-0000-4000-8000-000000009999' } };
    await assert.rejects(ui.dispatchPending(request), /fictional transient render failure/);
    assert.equal(ui.get('pending-request').hidden, false, 'the exact unresolved request remains available to check');
    assert.match(ui.get('pending-description').textContent, /Codex/);
  }
  assert.equal(ui.get('refresh').disabled, false, 'finally releases a lock even when rendering fails before the first HTTP request');
  const before = ui.requests.length; await ui.poll(); assert.ok(ui.requests.length > before, 'ordinary polling resumes');
  assert.equal(ui.get('message').value, '保留待确认的正文'); assert.match(ui.get('recipient-button').textContent, /Codex/);
  assert.equal(ui.posts().length, 0, 'recovery reads never replay an unsent message');
});

test('window focus reads current state without replaying messages or bypassing an in-flight refresh', async () => {
  let held = null;
  const ui = await browser({ route(request) { if (held && request.path === '/api/status') return held.promise; } });
  ui.type('焦点切换保留草稿'); await ui.choose('codex');
  ui.document.hidden = true; const hidden = ui.requests.length; await ui.windowEvent('focus'); assert.equal(ui.requests.length, hidden);
  ui.document.hidden = false; held = deferred();
  await ui.windowEvent('focus'); const inFlight = ui.requests.length; assert.ok(inFlight > hidden);
  await ui.windowEvent('focus'); assert.equal(ui.requests.length, inFlight, 'the existing refresh guard still serializes reads');
  held.resolve({ agents: catalog }); held = null; await settle();
  assert.equal(ui.get('refresh').disabled, false); assert.equal(ui.get('message').value, '焦点切换保留草稿');
  assert.match(ui.get('recipient-button').textContent, /Codex/); assert.equal(ui.posts().length, 0);
});


test('a failed code refresh keeps the prior scoped delivery visible as historical without claiming a disconnected room', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture()); const data = taskCodeDeliveryFixture(); let fail = false;
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true, naturalContinuationSupport: true,
    route(request) { if (request.path.includes('/code-status')) { if (fail) throw new Error('fictional transport failure'); return data; } } });
  assert.equal(ui.get('code-status-summary').classList.contains('is-clean'), true);
  ui.type('代码查询不应弄丢草稿'); await ui.choose('codex'); fail = true;
  await ui.click('code-status-refresh');
  assert.match(ui.get('code-status-summary').textContent, /^上次检查：已提交代码已合入并同步/);
  assert.equal(ui.get('code-status-summary').classList.contains('is-clean'), false);
  assert.match(ui.get('code-delivery-main').textContent, /^上次检查：主分支/);
  assert.match(ui.get('code-status-freshness').textContent, /检查于.*本次代码查询未完成/);
  assert.doesNotMatch(ui.get('code-status-card').textContent, /旧浏览器地址|重新打开|连接中断/);
  assert.equal(ui.get('code-status-details').hidden, false); assert.equal(ui.get('code-merge-panel').hidden, false);
  assert.equal(ui.get('code-merge-send').disabled, false); assert.equal(ui.get('code-status-refresh').disabled, false);
  assert.match(ui.get('connection').textContent, /已连接/); assert.equal(ui.get('message').value, '代码查询不应弄丢草稿');
  assert.match(ui.get('recipient-button').textContent, /Codex/); assert.equal(ui.posts().length, 0);
});

test('code query loading and timeout retain old facts without altering other request deadlines or room connection', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture()); const data = taskCodeDeliveryFixture(); let wait = false; let clock = 1000;
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true, naturalContinuationSupport: true, now: () => clock,
    route(request, _document, options) { if (request.path.includes('/code-status')) {
      if (!wait) return data;
      return new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new Error('fictional aborted read'))));
    } } });
  wait = true; await ui.click('code-status-refresh');
  assert.match(ui.get('code-status-summary').textContent, /^上次检查：/);
  assert.match(ui.get('code-status-freshness').textContent, /正在重新核对/);
  assert.equal(ui.get('code-status-details').hidden, false);
  clock += 100000; await ui.runTimers();
  assert.match(ui.get('code-status-freshness').textContent, /本次代码查询等待超时/);
  assert.match(ui.get('code-status-summary').textContent, /^上次检查：/);
  assert.equal(ui.get('code-status-refresh').disabled, false); assert.match(ui.get('connection').textContent, /已连接/);
  assert.equal(ui.posts().length, 0);
});

test('a first code query failure reports only that query and does not invalidate a healthy chat connection', async () => {
  const saved = completedSuggestionFixture();
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true,
    route(request) { if (request.path.includes('/code-status')) throw new Error('fictional unavailable code query'); } });
  assert.match(ui.get('code-status-summary').textContent, /本次代码查询未完成/);
  assert.doesNotMatch(ui.get('code-status-card').textContent, /旧浏览器地址|重新打开|连接中断/);
  assert.equal(ui.get('code-status-details').hidden, true); assert.match(ui.get('connection').textContent, /已连接/);
  assert.equal(ui.posts().length, 0);
});

test('workspace uncertainty remains explicit beside known main and GitHub facts with friendly failing sources', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture()); const data = taskCodeDeliveryFixture();
  data.workspaces.push({ id: 'control-copy', label: '核对副本', kind: 'delivery' });
  const delivery = data.delivery_summary;
  delivery.state = 'unknown'; delivery.private = { state: 'confirmed' };
  delivery.workspace = { ...delivery.workspace, state: 'unknown', reason: 'git_timeout',
    checks: [{ workspace_id: data.workspace_id, state: 'confirmed', reason: null }, { workspace_id: 'control-copy', state: 'unknown', reason: 'git_timeout' }] };
  delivery.main.checks = [{ workspace_id: data.workspace_id, state: 'confirmed', reason: null }];
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true, naturalContinuationSupport: true,
    route(request) { if (request.path.includes('/code-status')) return data; } });
  assert.match(ui.get('code-status-summary').textContent, /已提交代码已同步，工作区成果尚未核齐/);
  assert.equal(ui.get('code-delivery-main').textContent, '主分支：已提交代码已合入');
  assert.equal(ui.get('code-delivery-remote').textContent, 'GitHub：当前主分支已同步');
  assert.match(ui.get('code-delivery-workspace').textContent, /工作区：读取状态超时.*核对副本/);
  assert.equal(ui.get('code-status-summary').classList.contains('is-clean'), false);
  delivery.state = 'pending'; delivery.workspace.state = 'pending'; delivery.workspace.reason = 'uncommitted_tracked_changes';
  delivery.workspace.checks[1] = { workspace_id: 'control-copy', state: 'pending', reason: 'uncommitted_tracked_changes' };
  await ui.click('code-status-refresh'); assert.equal(ui.get('code-status-summary').textContent, '还有工作区成果待提交');
  assert.match(ui.get('code-delivery-workspace').textContent, /已跟踪的改动未提交.*核对副本/);
  delivery.main = { state: 'pending', reason: 'branch_not_on_mainline', checks: [{ workspace_id: 'control-copy', state: 'pending', reason: 'branch_not_on_mainline' }] };
  await ui.click('code-status-refresh'); assert.match(ui.get('code-delivery-main').textContent, /分支上的提交未合入.*核对副本/);
  assert.equal(ui.posts().length, 0);
});

test('a retained code source after query failure still dispatches one real directed submission request and preserves the draft', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture()); const data = taskCodeDeliveryFixture(); let fail = false;
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true, naturalContinuationSupport: true,
    route(request) {
      if (request.path.includes('/code-status')) { if (fail) throw new Error('fictional code query failure'); return data; }
      if (request.path.endsWith('/messages') && request.method === 'POST') return { acknowledged: true, room_id: saved.room.id, request_id: request.body.request_id };
    } });
  ui.type('保留独立草稿'); await ui.choose('codex'); fail = true; await ui.click('code-status-refresh');
  ui.get('code-merge-target').value = 'claude'; ui.get('code-merge-target').fire('change');
  assert.equal(ui.get('code-merge-send').disabled, false);
  await ui.click('code-merge-send');
  const sent = ui.posts().filter((request) => request.path.endsWith('/messages'));
  assert.equal(sent.length, 1, 'enabled action must actually reach the ordinary user-message endpoint');
  assert.equal(sent[0].body.target, 'claude'); assert.match(sent[0].body.text, /执行时核对实际状态/);
  assert.match(sent[0].body.text, /示例 项目 → master/);
  assert.equal(ui.get('message').value, '保留独立草稿'); assert.match(ui.get('recipient-button').textContent, /Codex/);
  ui.get('code-workspace').value = 'native:codex'; ui.get('code-workspace').fire('change'); await settle();
  await ui.click('code-merge-send');
  assert.equal(ui.posts().filter((request) => request.path.endsWith('/messages')).length, 1, 'changing scope never reuses the old repository request');
  assert.equal(ui.posts().some((request) => /\/merge|\/git/.test(request.path)), false);
});

function deliveryRepositoryFixture() {
  const data = taskCodeDeliveryFixture();
  data.workspaces = [{ id: 'delivery:app', label: '应用仓库', path: '/projects/app/repository' }, { id: 'delivery:control', label: '核对副本', path: '/projects/control/repository' }];
  data.workspace_id = 'delivery:app';
  data.delivery_repository = { state: 'unbound', reason: 'delivery_repository_unbound', workspace_id: null, source: null,
    candidates: data.workspaces.map(({ id, label }) => ({ workspace_id: id, label })) };
  data.delivery_summary = { ...data.delivery_summary, state: 'unknown',
    main: { state: 'unknown', reason: 'delivery_repository_unbound' }, remote: { state: 'unknown', reason: 'delivery_repository_unbound' },
    published: { state: 'unknown', reason: 'published_work_mapping_unknown', files: { total: 26, matched: 23, missing: 3 } } };
  const bind = (id) => {
    data.delivery_repository = { ...data.delivery_repository, state: 'bound', reason: null, workspace_id: id, source: 'user' };
    data.workspace_id = id; data.delivery_summary.main = { state: 'confirmed' }; data.delivery_summary.remote = { state: 'confirmed' };
    data.delivery_summary.published.state = 'pending'; data.delivery_summary.published.reason = 'published_work_not_on_mainline';
    const root = data.workspaces.find((workspace) => workspace.id === id).path;
    data.delivery_summary.request = { available: true, repository_roots: [root], targets: [{ repository_root: root, target_branch: 'main' }] };
  };
  return { data, bind };
}

test('delivery repository selection is compact, explicit, persists across reload and does not send a task', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture()); const { data, bind } = deliveryRepositoryFixture();
  const route = (request) => {
    if (request.path.includes('/code-status')) return data;
    if (request.path.endsWith('/delivery-repository')) { assert.deepEqual(request.body, { workspace_id: 'delivery:app' }); bind(request.body.workspace_id); return { delivery_repository: data.delivery_repository }; }
  };
  const options = { rooms: [saved.room], saved, codeStatusSupport: true, naturalContinuationSupport: true, route };
  const ui = await browser(options); ui.type('保留原草稿'); await ui.choose('codex');
  assert.equal(ui.get('code-repository-label').textContent, '交付仓库'); assert.equal(ui.get('code-repository-picker').hidden, true);
  assert.equal(ui.get('code-status-summary').textContent, '尚未指定交付仓库'); assert.equal(ui.get('code-delivery-states').hidden, true);
  await ui.click('code-repository-edit'); ui.get('code-repository-choice').value = 'delivery:control'; ui.get('code-repository-choice').fire('change');
  await ui.click('code-repository-cancel'); assert.equal(ui.posts().length, 0);
  await ui.click('code-repository-edit'); ui.get('code-repository-choice').value = 'delivery:app'; ui.get('code-repository-choice').fire('change');
  await ui.click('code-repository-save');
  assert.equal(ui.posts().length, 1); assert.equal(ui.get('code-repository-label').textContent, '交付仓库：应用仓库');
  assert.match(ui.requests.filter((request) => request.path.includes('/code-status')).at(-1).path, /refresh_remote=1/);
  assert.equal(ui.get('code-repository-picker').hidden, true); assert.equal(ui.get('code-delivery-states').hidden, false);
  assert.equal(ui.get('code-status-summary').textContent, '已提交代码已合入并同步 GitHub'); assert.match(ui.get('code-published-note').textContent, /23\/26/);
  assert.equal(ui.get('message').value, '保留原草稿'); assert.match(ui.get('recipient-button').textContent, /Codex/);
  const reloaded = await browser(options); assert.equal(reloaded.get('code-repository-label').textContent, '交付仓库：应用仓库');
  assert.equal(reloaded.posts().length, 0); assert.equal(ui.posts().some((request) => request.path.endsWith('/messages')), false);
  data.delivery_summary.private = { state: 'pending', reason: 'private_work_not_published' }; await ui.click('code-status-refresh');
  assert.equal(ui.get('code-status-summary').textContent, '还有工作区成果待提交', 'private changes take priority over file mapping');
  data.delivery_summary.private.state = 'confirmed'; data.delivery_summary.workspace.state = 'pending'; await ui.click('code-status-refresh');
  assert.equal(ui.get('code-status-summary').textContent, '还有工作区成果待提交', 'tracked pending changes take priority too');
  data.delivery_summary.workspace.state = 'confirmed'; data.delivery_summary.main.state = 'unknown'; await ui.click('code-status-refresh');
  assert.equal(ui.get('code-status-summary').textContent, 'GitHub 当前主分支已同步，任务成果尚未核齐', 'an unknown main branch is not turned into a pending merge by snapshot differences');
});

test('a lost delivery repository save is read back once without repeating POST and unconfirmed choices remain editable', async () => {
  for (const applied of [false, true]) {
    const saved = completedSuggestionFixture(); const { data, bind } = deliveryRepositoryFixture(); let reads = 0;
    const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true, route(request) {
      if (request.path.includes('/code-status')) { reads++; return data; }
      if (request.path.endsWith('/delivery-repository')) { if (applied) bind(request.body.workspace_id); throw new Error('fictional lost save response'); }
    } });
    await ui.click('code-repository-edit'); ui.get('code-repository-choice').value = 'delivery:app'; ui.get('code-repository-choice').fire('change');
    const before = reads; await ui.click('code-repository-save');
    assert.equal(ui.posts().length, 1); assert.equal(reads, before + 1, 'only a read verifies the unknown mutation result');
    assert.equal(ui.get('code-repository-picker').hidden, applied);
    assert.equal(ui.get('code-repository-error').hidden, applied);
    if (!applied) assert.equal(ui.get('code-repository-choice').value, 'delivery:app');
  }
});

test('a completed binding save cannot replace a newly opened composer and no new-room task is dispatched', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture()); const { data, bind } = deliveryRepositoryFixture(); const held = deferred(); bind('delivery:control');
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.includes('/code-status')) return data;
    if (request.path.endsWith('/delivery-repository')) return held.promise;
  } });
  await ui.click('code-repository-edit'); ui.get('code-repository-choice').value = 'delivery:app'; ui.get('code-repository-choice').fire('change');
  await ui.click('code-repository-save');
  assert.equal(ui.get('code-merge-send').disabled, true); await ui.click('code-merge-send');
  assert.equal(ui.posts().some((request) => request.path.endsWith('/messages')), false, 'an unresolved replacement cannot send the old target');
  await ui.click('new-room'); ui.type('新任务草稿');
  bind('delivery:app'); held.resolve({ delivery_repository: data.delivery_repository }); await settle();
  assert.equal(ui.get('code-status-card').hidden, true); assert.equal(ui.get('message').value, '新任务草稿');
  assert.equal(ui.posts().length, 1); assert.equal(ui.posts().some((request) => request.path === '/api/rooms'), false);
});


test('only the bound delivery repository enters the explicit submit request and duplicate choices reveal their paths', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture()); const { data, bind } = deliveryRepositoryFixture();
  data.delivery_repository.candidates.forEach((candidate) => { candidate.label = 'repository'; });
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.includes('/code-status')) return data;
    if (request.path.endsWith('/delivery-repository')) { bind(request.body.workspace_id); return { delivery_repository: data.delivery_repository }; }
    if (request.path.endsWith('/messages')) return { acknowledged: true, room_id: saved.room.id, request_id: request.body.request_id };
  } });
  await ui.click('code-merge-send'); assert.equal(ui.posts().length, 0, 'unbound metadata cannot dispatch hidden old sources');
  ui.type('另一条未发草稿'); await ui.choose('codex'); await ui.click('code-repository-edit');
  assert.match(ui.get('code-repository-choice').children[1].textContent, /repository.*\/projects\/app\/repository/);
  assert.match(ui.get('code-repository-choice').children[2].textContent, /repository.*\/projects\/control\/repository/);
  ui.get('code-repository-choice').value = 'delivery:app'; ui.get('code-repository-choice').fire('change'); await ui.click('code-repository-save');
  ui.get('code-merge-target').value = 'claude'; ui.get('code-merge-target').fire('change'); await ui.click('code-merge-send');
  const sent = ui.posts().filter((request) => request.path.endsWith('/messages')); assert.equal(sent.length, 1);
  assert.match(sent[0].body.text, /\/projects\/app\/repository → main/);
  assert.doesNotMatch(sent[0].body.text, /\/projects\/control|baseline|\/private\//);
  assert.equal(ui.get('message').value, '另一条未发草稿'); assert.match(ui.get('recipient-button').textContent, /Codex/);
});

test('a transient repository picker render failure releases saving and allows an explicit retry without losing a draft', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture()); const { data, bind } = deliveryRepositoryFixture();
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.includes('/code-status')) return data;
    if (request.path.endsWith('/delivery-repository')) { bind(request.body.workspace_id); return { delivery_repository: data.delivery_repository }; }
  } });
  ui.type('保存失败也保留这条草稿'); await ui.choose('codex'); await ui.click('code-repository-edit');
  ui.get('code-repository-choice').value = 'delivery:app'; ui.get('code-repository-choice').fire('change');
  const node = ui.get('code-repository-label'), descriptor = Object.getOwnPropertyDescriptor(FakeElement.prototype, 'textContent'); let fail = true;
  Object.defineProperty(node, 'textContent', { get() { return descriptor.get.call(this); }, set(value) { if (fail) { fail = false; throw new Error('fictional picker render failure'); } descriptor.set.call(this, value); }, configurable: true });
  await ui.click('code-repository-save');
  assert.equal(ui.posts().length, 0, 'render recovery never automatically dispatches the mutation');
  assert.equal(ui.get('code-repository-save').disabled, false); assert.equal(ui.get('code-repository-cancel').disabled, false);
  assert.equal(ui.get('code-repository-choice').value, 'delivery:app');
  assert.equal(ui.get('message').value, '保存失败也保留这条草稿'); assert.match(ui.get('recipient-button').textContent, /Codex/);
  await ui.click('code-repository-save'); assert.equal(ui.posts().length, 1);
  assert.equal(ui.get('code-repository-label').textContent, '交付仓库：应用仓库');
});


test('an uncertain binding with failed readback cannot submit old repository sources until a new read confirms the target', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture()); const { data, bind } = deliveryRepositoryFixture(); bind('delivery:control');
  let failReads = false;
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true, naturalContinuationSupport: true, route(request) {
    if (request.path.includes('/code-status')) { if (failReads) throw new Error('fictional readback failure'); return data; }
    if (request.path.endsWith('/delivery-repository')) { bind(request.body.workspace_id); failReads = true; throw new Error('fictional lost mutation response'); }
    if (request.path.endsWith('/messages')) return { acknowledged: true, room_id: saved.room.id, request_id: request.body.request_id };
  } });
  ui.type('未知配置也保留草稿'); await ui.choose('codex'); await ui.click('code-repository-edit');
  ui.get('code-repository-choice').value = 'delivery:app'; ui.get('code-repository-choice').fire('change'); await ui.click('code-repository-save');
  assert.match(ui.get('code-status-summary').textContent, /交付仓库保存结果待确认/);
  assert.equal(ui.get('code-merge-panel').hidden, true); await ui.click('code-merge-send');
  assert.equal(ui.posts().filter((request) => request.path.endsWith('/messages')).length, 0);
  assert.equal(ui.get('code-repository-choice').value, 'delivery:app');
  failReads = false; await ui.click('code-status-refresh');
  assert.equal(ui.get('code-repository-label').textContent, '交付仓库：应用仓库'); assert.equal(ui.get('code-merge-panel').hidden, false);
  assert.equal(ui.get('code-repository-error').hidden, true);
  ui.get('code-merge-target').value = 'claude'; ui.get('code-merge-target').fire('change'); await ui.click('code-merge-send');
  const sent = ui.posts().filter((request) => request.path.endsWith('/messages')); assert.equal(sent.length, 1);
  assert.match(sent[0].body.text, /\/projects\/app\/repository → main/); assert.doesNotMatch(sent[0].body.text, /\/projects\/control/);
  assert.equal(ui.posts().filter((request) => request.path.endsWith('/delivery-repository')).length, 1);
  assert.equal(ui.get('message').value, '未知配置也保留草稿'); assert.match(ui.get('recipient-button').textContent, /Codex/);
});


test('task materials move saved documents and historical results out of chat and return without changing the draft', async () => {
  const saved = completedSuggestionFixture();
  saved.objective.status = 'running'; saved.collaboration.outcome.state = 'in_progress'; saved.team.phase = 'working';
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  assert.equal(ui.get('task-materials').hidden, true);
  assert.equal(descendants(ui.get('timeline')).includes(ui.get('room-documents')), false);
  assert.equal(descendants(ui.get('timeline')).includes(ui.get('media-history')), false);
  assert.equal(ui.get('collaboration-outcome').hidden, true);
  assert.match(ui.get('materials-outcomes').textContent, /此前成果/);
  await ui.choose('codex'); ui.type('保留资料阅读前的草稿');
  ui.get('timeline').scrollTop = 137;
  await ui.click('task-materials-open');
  assert.equal(ui.get('task-materials').hidden, false); assert.equal(ui.get('timeline').hidden, true);
  assert.equal(ui.get('task-materials-open').getAttribute('aria-expanded'), 'true');
  ui.get('task-materials-body').scrollTop = 89;
  const button = descendants(ui.get('document-shortcuts')).find(node => node.dataset.documentPath === 'first.md')
    || descendants(ui.get('document-more-items')).find(node => node.dataset.documentPath === 'first.md');
  button.focus(); button.fire('click'); await settle();
  assert.equal(ui.get('task-materials').hidden, true); assert.match(ui.get('reader-back').textContent, /返回任务资料/);
  assert.match(ui.get('reader-body').textContent, /第一阶段的正文/);
  await ui.click('reader-back');
  assert.equal(ui.get('task-materials').hidden, false); assert.equal(ui.document.activeElement, button);
  assert.equal(ui.get('task-materials-body').scrollTop, 89);
  saved.collaboration.summary = '分工更新'; await ui.poll();
  assert.equal(ui.get('task-materials').hidden, false); assert.equal(ui.get('timeline').scrollTop, 137);
  ui.get('task-materials').fire('keydown', { key: 'Escape' }); await settle();
  assert.equal(ui.get('task-materials').hidden, true); assert.equal(ui.document.activeElement, ui.get('task-materials-open'));
  assert.equal(ui.get('message').value, '保留资料阅读前的草稿'); assert.match(ui.get('recipient-button').textContent, /Codex/);
  assert.equal(ui.posts().length, 0);
  await ui.click('task-materials-open'); await ui.click('new-room');
  assert.equal(ui.get('task-materials').hidden, true); assert.equal(ui.get('task-materials-open').hidden, true);
});

test('task materials keep current delivery in chat while old attachments remain accessible without following new records', async () => {
  const { saved, assets, route } = mediaFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, mediaSupport: true, route });
  assert.equal(ui.get('collaboration-outcome').hidden, false); assert.equal(ui.get('room-media').hidden, false);
  assert.equal(descendants(ui.get('timeline')).includes(ui.get('room-media')), true);
  const current = assets[0]; current.source = null;
  saved.events.push(message(99, 'user', '新的要求')); await ui.poll();
  await ui.click('task-materials-open');
  assert.match(ui.get('media-history-label').textContent, /此前附件/);
  const old = ui.get('media-history-items').children.find(node => node.dataset.mediaId === current.id);
  assert.ok(old);
  ui.get('timeline').scrollTop = 71; ui.get('task-materials-body').scrollTop = 42;
  const extra = { ...assets[3], id: '5'.repeat(64), sha256: '5'.repeat(64), name: '新到的资料.bin', source: null };
  assets.push(extra); await ui.click('task-materials-refresh');
  assert.match(ui.get('media-history-files').textContent, /新到的资料/);
  assert.equal(ui.get('task-materials').hidden, false); assert.equal(ui.get('timeline').scrollTop, 71);
  assert.equal(ui.get('task-materials-body').scrollTop, 42);
  const trigger = old.children.find(node => node.classList.contains('media-open'));
  trigger.focus(); trigger.fire('click'); await settle();
  assert.match(ui.get('reader-back').textContent, /返回任务资料/);
  await ui.click('reader-back'); assert.equal(ui.document.activeElement, trigger);
  assert.equal(ui.get('task-materials').hidden, false);
  assert.equal(ui.posts().some(request => !request.path.endsWith('/access')), false);
});


test('Kimi account details display the real membership and distinguish absent provider email from a failed query', async () => {
  for (const reason of ['provider_not_provided', 'profile_query_unavailable']) {
    const agent = { id: 'kimi', name: 'Kimi Code', avatar: 'K' };
    const account = accountFixture({ source: 'kimi_managed_oauth', email: null, plan: reason === 'provider_not_provided' ? 'Plus' : null,
      field_reasons: { email: reason, plan: reason === 'provider_not_provided' ? null : reason } });
    const ui = await browser({ agents: [agent], partners: [{ ...agent, adapter: 'kimi', enabled: true, account }] });
    const footer = ui.get('provider-account-kimi');
    assert.match(footer.textContent, reason === 'provider_not_provided' ? /供应商未提供邮箱/ : /本次未取得邮箱/);
    assert.match(footer.textContent, reason === 'provider_not_provided' ? /Plus/ : /本次未取得订阅/);
    assert.doesNotMatch(footer.textContent, /邮箱未获取|订阅未取得/);
    assert.equal(ui.posts().length, 0);
  }
});


function currentTaskBlockFixture() {
  const saved = needsReplyFixture();
  saved.objective.next_step.origin = 'task_block'; saved.objective.next_step.decision = 'needs_user';
  saved.objective.next_step.question = '请确认这项文件修改的负责人。\n' + '这里保留伙伴提出的完整问题和已有证据。'.repeat(30);
  saved.objective.next_step.question_source = null;
  saved.events = saved.events.filter(event => event.kind !== 'objective_checkpoint_finished');
  saved.events.push({ seq: 25, kind: 'objective_decision_required', sender: 'system', run_id: saved.current_run.id,
    objective_id: saved.objective.id, task_ids: ['blocked-file'], created_at: 1025 });
  saved.team.tasks = [{ id: 'blocked-file', title: '核对文件修改', owner: 'codex', status: 'blocked', block_cause: 'permission_required' }];
  saved.collaboration.tasks = saved.team.tasks;
  return saved;
}
test('current bound task block shows one actionable literal question with its structured cause and preserves the independent draft', async () => {
  const saved = currentTaskBlockFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  const outcome = ui.get('collaboration-outcome');
  assert.equal(outcome.hidden, false); assert.match(outcome.textContent, /当前需要处理/);
  assert.match(outcome.textContent, /核对文件修改.*需要权限确认/);
  const details = ui.document.querySelector('.current-task-question');
  assert.equal(details.open, false); assert.ok(details.textContent.includes(saved.objective.next_step.question));
  assert.equal(ui.get('events').textContent.includes(saved.objective.next_step.question), false);
  await ui.choose('codex'); ui.type('保留原来的未发送补充');
  details.open = true; saved.collaboration.summary = '一项真实分工更新'; await ui.poll();
  assert.equal(ui.document.querySelector('.current-task-question').open, true);
  assert.equal(ui.get('message').value, '保留原来的未发送补充'); assert.match(ui.get('recipient-button').textContent, /Codex/);
  saved.objective.next_step.question = '请确认当前负责这项修改的伙伴。'; await ui.poll();
  assert.equal(ui.document.querySelector('.current-task-question'), null);
  assert.equal(ui.get('collaboration-outcome').textContent.split(saved.objective.next_step.question).length - 1, 1);
  assert.match(ui.get('collaboration-outcome').textContent, /继续时沿用当前权限设置/);
  assert.equal(ui.posts().length, 0);
});
test('reply-mistake blocks read as automatic replanning, while only a permission block offers a choice that fills the draft', async () => {
  // Catches internal correction codes being offered to the user as decisions, and choices that send without review.
  const saved = currentTaskBlockFixture();
  saved.objective.next_step.question = '[0%] 实跑遗漏入口：team_task_permission；全量测试：team_write_path_conflict';
  saved.team.tasks = [
    { id: 'reply-mistake', title: '[0%] 实跑遗漏入口', owner: 'codex', status: 'blocked', blocked_reason: 'team_task_permission', block_cause: null },
    { id: 'coordination', title: '[0%] 执行全量注册测试', owner: 'claude', status: 'blocked', blocked_reason: '需要内部重规划补一个修复任务。', block_cause: 'coordination_required' }];
  saved.collaboration.tasks = saved.team.tasks;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
  const now = ui.get('now-card');
  assert.equal(now.hidden, false);
  // The host reopens these blocks itself (stop_policy), so nothing waits on a click.
  assert.match(ui.get('now-label').textContent, /马上自动继续/);
  assert.doesNotMatch(now.textContent, /等你决定|team_task_permission|按原分工重试|先跳过|停止任务/);
  assert.equal(descendants(now).some((node) => node.tagName === 'BUTTON' && node.classList.contains('pri')), false, 'no primary click is needed');
  const outcomeLines = descendants(ui.get('collaboration-outcome')).filter((node) => node.classList.contains('detail-caption')).map((node) => node.textContent).join('\n');
  assert.doesNotMatch(outcomeLines, /实跑遗漏入口：/, 'replanned tasks are not listed as decisions');
  assert.match(outcomeLines, /2 项需要重新安排，点“继续推进”后会自动处理/);
  const pane = ui.get('progress-pane');
  assert.equal(ui.get('conversation').dataset.tab, 'progress');
  assert.match(pane.textContent, /2 项待重新安排/);
  assert.match(pane.textContent, /豆窝会接着处理，不用你选/);
  assert.equal(descendants(pane).filter((node) => node.tagName === 'BUTTON').length, 0, 'no per-task choice for automatic replanning');
  assert.ok(descendants(pane).some((node) => node.classList.contains('tech-details') && node.textContent.includes('team_task_permission')));
  // Once the host already continued this board since the user last spoke, the repeat is the user's.
  saved.events.push({ seq: 40, kind: 'objective_auto_continued', sender: 'system', code: 'task_block', run_id: saved.current_run.id, created_at: 1040 });
  await ui.poll();
  assert.match(ui.get('now-title').textContent, /有几项需要重新安排/);
  assert.doesNotMatch(ui.get('now-card').textContent, /马上自动继续|等你决定/);

  const permission = currentTaskBlockFixture();
  const second = await browser({ rooms: [permission.room], saved: permission, executionPolicySupport: true, naturalContinuationSupport: true });
  assert.match(second.get('now-title').textContent, /有一步要你同意权限/);
  const approve = descendants(second.get('now-card')).find((node) => node.tagName === 'BUTTON' && node.textContent === '同意，按原计划做');
  assert.ok(approve, 'a permission block offers an explicit choice');
  approve.fire('click'); await settle();
  assert.equal(second.get('message').value, '同意「核对文件修改」需要的权限，按原计划继续。');
  assert.equal(second.posts().length, 0, 'a quick choice never sends by itself');
  const timelineHidden = second.get('timeline').hidden;
  await second.click('tab-discussion');
  assert.equal(second.get('conversation').dataset.tab, 'discussion');
  assert.equal(second.get('timeline').hidden, timelineHidden, 'views switch by layout only and leave reader state alone');
});
test('stops the host continues by itself never read as a decision, and only user stops leave the click to the user', async () => {
  // Catches automatic stops (stop_policy) shown as waiting on the user, and user or keep-scene stops falsely promised to resume.
  const cases = [
    ['all_partners_quota_exhausted', /等额度恢复/, /伙伴额度都用完了，恢复后自动继续/, /有伙伴额度恢复后豆窝会自己接着做，不用你操作/],
    ['service_restarted', /马上自动继续/, /豆窝服务重启过，马上自动接着做/, /几秒后自动继续，不用你操作/],
    ['cli_nonzero_exit', /马上自动继续/, /推进停了一下，马上自动接着做/, /几秒后自动继续/],
    ['objective_calls_exhausted', /已停止/, /推进已停止/, /不会自动重跑/],
    ['session_identity_unknown', /已停止/, /推进已停止/, /不会自动重跑/],
  ];
  for (const [reason, label, title, text] of cases) {
    const saved = restartSnapshot(); saved.objective.stop_reason = reason;
    saved.objective.execution_policy = { ...saved.current_run.execution_policy };
    const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, continuousObjectiveSupport: true });
    const now = ui.get('now-card');
    assert.match(ui.get('now-label').textContent, label, reason);
    assert.match(ui.get('now-title').textContent, title, reason);
    assert.match(ui.get('now-text').textContent, text, reason);
    assert.doesNotMatch(now.textContent, /等你决定|同意，按原计划做|写回复/, reason);
    const automatic = !['objective_calls_exhausted', 'session_identity_unknown'].includes(reason);
    const primary = descendants(now).find((node) => node.tagName === 'BUTTON' && node.dataset.mirror === 'restart-objective');
    assert.ok(primary, `${reason} still lets the user continue now`);
    assert.equal(primary.classList.contains('pri'), !automatic, `${reason}: a click is only the main action when nothing resumes by itself`);
    assert.equal(ui.posts().length, 0);
  }
  // A step-by-step objective and a run without quota failover are never promised an automatic resume.
  const manual = restartSnapshot(); manual.objective.stop_reason = 'service_restarted';
  manual.objective.execution_policy = { ...manual.current_run.execution_policy, mode: 'step' };
  const manualUi = await browser({ rooms: [manual.room], saved: manual, executionPolicySupport: true, continuousObjectiveSupport: true });
  assert.doesNotMatch(manualUi.get('now-card').textContent, /自动继续|自动接着做/);
  const noFailover = restartSnapshot(); noFailover.objective.stop_reason = 'all_partners_quota_exhausted';
  noFailover.objective.execution_policy = { ...noFailover.current_run.execution_policy, quota_failover: false };
  const noFailoverUi = await browser({ rooms: [noFailover.room], saved: noFailover, executionPolicySupport: true, continuousObjectiveSupport: true });
  assert.doesNotMatch(noFailoverUi.get('now-title').textContent, /恢复后自动继续/);
  // With natural continuation the service's own action decides: a continuation it cannot send never resumes by itself.
  for (const canSend of [true, false]) {
    const natural = restartSnapshot(); natural.objective.stop_reason = 'service_shutdown';
    natural.objective.execution_policy = { ...natural.current_run.execution_policy };
    natural.continuation = { action: 'restart-objective', can_send: canSend, reason: canSend ? null : 'team_cleanup_unconfirmed',
      binding: { run_id: natural.current_run.id, revision: natural.current_run.revision, digest: natural.current_run.digest } };
    const naturalUi = await browser({ rooms: [natural.room], saved: natural, executionPolicySupport: true, continuousObjectiveSupport: true, naturalContinuationSupport: true });
    assert.equal(/马上自动继续/.test(naturalUi.get('now-label').textContent), canSend, `natural continuation can_send=${canSend}`);
    assert.doesNotMatch(naturalUi.get('now-card').textContent, /等你决定/);
  }
  // The same automatic stop coming back after one automatic continue waits for the user.
  const repeated = restartSnapshot(); repeated.objective.stop_reason = 'service_restarted';
  repeated.objective.execution_policy = { ...repeated.current_run.execution_policy };
  repeated.events.push({ seq: 90, kind: 'objective_auto_continued', sender: 'system', code: 'service_restarted', run_id: repeated.current_run.id, created_at: 1090 });
  const repeatedUi = await browser({ rooms: [repeated.room], saved: repeated, executionPolicySupport: true, continuousObjectiveSupport: true });
  assert.match(repeatedUi.get('now-title').textContent, /自动继续过一次，又停在同一处/);
  assert.ok(descendants(repeatedUi.get('now-card')).find((node) => node.dataset.mirror === 'restart-objective').classList.contains('pri'));
});
test('the service stop_kind keeps the card, the room list and the task board telling the same story', async () => {
  // Catches a list saying 已停止, unfinished tasks saying 已取消, or a note asking for a click while the card promises to resume.
  const fixture = (reason, kind) => {
    const saved = restartSnapshot(); saved.objective.stop_reason = reason;
    saved.stop_kind = kind; saved.room.objective_status = 'stopped'; saved.room.stop_kind = kind;
    saved.team = { ...(saved.team || {}), phase: 'stopped', stop_reason: reason };
    saved.team.tasks = [{ id: 'done', title: '已完成的一项', owner: 'codex', status: 'accepted', review_kind: 'independent_review' },
      { id: 'left', title: '没做完的一项', owner: 'claude', status: 'cancelled' }];
    saved.collaboration.tasks = saved.team.tasks;
    saved.members[0].status = 'interrupted';
    return saved;
  };
  const open = (saved) => browser({ rooms: [saved.room], saved, executionPolicySupport: true, continuousObjectiveSupport: true, naturalContinuationSupport: true });
  const partner = (saved) => `status-${saved.members[0].id}`;
  for (const [reason, kind, label] of [['service_shutdown', 'auto', '马上自动继续'], ['all_partners_quota_exhausted', 'quota_wait', '等额度恢复']]) {
    const ui = await open(fixture(reason, kind));
    assert.match(ui.get('now-label').textContent, new RegExp(label), kind);
    const card = ui.get('rooms').children[0];
    assert.match(card.textContent, new RegExp(label), `${kind}: the list says what the card says`);
    assert.doesNotMatch(card.textContent, /已停止/, kind); assert.equal(card.dataset.group, 'go', kind);
    const pane = ui.get('progress-pane');
    assert.match(pane.textContent, /1 项待继续/, kind);
    assert.match(pane.textContent, /自动继续后接着做/, kind);
    assert.doesNotMatch(pane.textContent, /已取消|点“继续推进”/, kind);
    assert.equal(ui.get(partner(fixture(reason, kind))).textContent, '待继续', `${kind}: the partner card agrees`);
    // Catches: a stop the host resumes by itself offered no pause, only stop.
    assert.equal(ui.get('pause-objective').hidden, false, `${kind}: the wait can be paused`);
    assert.match(ui.get('now-card').textContent, /暂停推进/, kind);
    assert.equal(ui.posts().length, 0);
  }
  const holdSaved = fixture('all_partners_quota_exhausted', 'quota_wait'); const hold = await open(holdSaved);
  hold.get('pause-objective').fire('click'); await settle();
  assert.equal(hold.posts().at(-1)?.path, `/api/rooms/${roomId}/pause`);
  // The service's null wins over a code that looks automatic: the next step is the user's.
  const manualSaved = fixture('service_shutdown', null); const manual = await open(manualSaved);
  assert.notEqual(manual.get(partner(manualSaved)).textContent, '待继续');
  assert.doesNotMatch(manual.get('now-card').textContent, /自动继续|自动接着做/);
  assert.equal(manual.get('pause-objective').hidden, true, 'a stop that waits for the user has nothing to pause');
  assert.match(manual.get('rooms').children[0].textContent, /目标已停止/);
  assert.match(manual.get('progress-pane').textContent, /点“继续推进”后会接着做/);
  // Only the user's own stop really cancels unfinished tasks.
  const cancelled = await open(fixture('cancel_requested', null));
  assert.match(cancelled.get('progress-pane').textContent, /1 项已取消/);
  assert.doesNotMatch(cancelled.get('progress-pane').textContent, /待继续/);
});
test('English accepts typed continue as the bare command, joins names with English punctuation and names Claude windows', async () => {
  // Catches the English hint telling people to type continue while only 继续 worked, 、 leaking into English lists, and Chinese Claude window names.
  for (const text of ['continue', 'Keep going.', 'please continue']) {
    const saved = restartSnapshot();
    const ui = await browser({ language: 'en', rooms: [saved.room], saved, executionPolicySupport: true, continuousObjectiveSupport: true,
      route(request) { if (request.path.endsWith('/restart-objective')) return saved; } });
    ui.type(text); await ui.submit();
    assert.equal(ui.posts().at(-1)?.path, `/api/rooms/${roomId}/restart-objective`, text);
  }
  const extra = restartSnapshot();
  const notCommand = await browser({ language: 'en', rooms: [extra.room], saved: extra, executionPolicySupport: true, continuousObjectiveSupport: true });
  notCommand.type('continue with the README too'); await notCommand.submit();
  assert.equal(notCommand.posts().length, 0, 'a real addition is kept, not taken as the bare command');

  const account = accountFixture({ quota: { state: 'available', stale: false, checked_at: 1000, scope: 'provider_account_and_model', windows: [
    { name: 'claude:five_hour', label: 'Claude · 5小时窗口', remaining_percent: 60, used_percent: 40, window_minutes: 300, resets_at: 2000 },
    { name: 'claude:seven_day', label: 'Claude · 7天窗口', remaining_percent: 30, used_percent: 70, window_minutes: 10080, resets_at: 3000 },
    { name: 'claude:model_scoped:2', label: 'Opus · 模型周额度', remaining_percent: 80, used_percent: 20, window_minutes: 10080, resets_at: 3000 }] } });
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, account: agent.id === 'claude' ? account : null }));
  const saved = restartSnapshot();
  const ui = await browser({ language: 'en', rooms: [saved.room], saved, partners });
  const footer = ui.get('provider-account-claude').textContent;
  assert.match(footer, /Claude · 5-hour window/); assert.match(footer, /Claude · 7-day window/); assert.match(footer, /Opus · weekly model quota/);
  assert.doesNotMatch(footer, /小时|天窗口|模型周额度/);
  assert.match(ui.get('frozen-participants').textContent, /Claude Code, Codex|Codex, Claude Code/);
  assert.doesNotMatch(ui.get('frozen-participants').textContent, /、/);
});
test('waiting for quota reads the same on the board and every partner card, without rearranging or repeating the stop', async () => {
  // Shape of a real room after #19: tasks held by all_partners_quota_exhausted, partners retired for quota.
  const saved = restartSnapshot(); saved.objective.stop_reason = 'all_partners_quota_exhausted';
  saved.stop_kind = 'quota_wait'; saved.room.objective_status = 'stopped'; saved.room.stop_kind = 'quota_wait';
  saved.team = { ...(saved.team || {}), phase: 'stopped', stop_reason: 'all_partners_quota_exhausted' };
  const [first, second] = saved.members;
  saved.team.tasks = [{ id: 'done', title: '已完成的一项', owner: first.id, status: 'self_checked' },
    { id: 'held', title: '被额度挡住的一项', owner: first.id, status: 'blocked', block_cause: null, blocked_reason: 'all_partners_quota_exhausted' }];
  saved.collaboration.tasks = saved.team.tasks;
  saved.collaboration.outcome.reason = '协作因异常停止，未完成项仍保留。';
  for (const member of saved.members) member.status = 'needs_user';
  saved.objective.retired_partners = { [first.id]: { reason: 'agent_quota_exhausted', cleanup_confirmed: true }, [second.id]: { reason: 'subscription_limit_reached', cleanup_confirmed: true } };
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, continuousObjectiveSupport: true, naturalContinuationSupport: true });
  const pane = ui.get('progress-pane');
  assert.match(pane.textContent, /1 项待继续/);
  assert.doesNotMatch(pane.textContent, /待重新安排|all_partners_quota_exhausted|技术细节/, 'a quota hold is not a replan and shows no code');
  for (const member of [first, second]) assert.equal(ui.get(`status-${member.id}`).textContent, '等额度恢复', member.id);
  assert.equal(ui.get(`task-${first.id}`).textContent, '「被额度挡住的一项」待继续');
  const cards = [first, second].map((member) => ui.get(`task-${member.id}`).textContent).join('\n');
  assert.doesNotMatch(cards, /伙伴的额度都用完了/, 'the stop reason is said once, on the card at the top');
  const outcome = ui.get('collaboration-outcome').textContent;
  assert.match(outcome, /等额度恢复/, 'the discussion outcome is titled like the card');
  assert.doesNotMatch(outcome, /异常停止|目标推进已停止/);
  assert.equal(ui.posts().length, 0);
});
test('current task question refuses stale bindings and never infers a permission category from free text or ordinary checkpoints', async () => {
  for (const variant of ['binding', 'origin', 'checkpoint', 'cause', 'coordination']) {
    const saved = currentTaskBlockFixture();
    if (variant === 'binding') saved.objective.next_step.binding.digest = 'f'.repeat(64);
    if (variant === 'origin') delete saved.objective.next_step.origin;
    if (variant === 'checkpoint') saved.objective.next_step.origin = 'checkpoint';
    if (variant === 'cause') saved.team.tasks[0].block_cause = null;
    if (variant === 'coordination') {
      saved.objective.status = 'running'; saved.collaboration.outcome.state = 'in_progress';
      saved.team.tasks[0].block_cause = 'coordination_required';
    }
    const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true });
    if (['cause', 'origin', 'checkpoint'].includes(variant)) {
      assert.match(ui.get('collaboration-outcome').textContent, /当前需要处理/);
      assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /需要权限确认/);
    } else assert.equal(ui.get('collaboration-outcome').hidden, true);
    assert.equal(ui.posts().length, 0);
  }
});


test('official Kimi display identity prefers actual email then masked phone then labeled nickname without exposing a raw phone', async () => {
  for (const [email, display, expected, stale = false] of [
    ['member@example.test', { masked_phone: '+86 **** 0567', nickname: 'Member Test' }, /member@example.test/],
    [null, { masked_phone: '+86 **** 0567', nickname: 'Member Test' }, /手机号：\+86 \*\*\*\* 0567/],
    [null, { nickname: 'Member Test' }, /昵称：Member Test/],
    [null, { masked_phone: '+86 **** 0567', nickname: 'Member Test' }, /上次确认：手机号.*待刷新/, true],
  ]) {
    const agent = { id: 'kimi', name: 'Kimi Code', avatar: 'K' };
    const account = accountFixture({ source: 'kimi_managed_oauth', email, plan: 'Plus', display_identity: display, stale,
      field_reasons: { email: email ? null : 'provider_not_provided', plan: null } });
    const ui = await browser({ agents: [agent], partners: [{ ...agent, adapter: 'kimi', enabled: true, account }] });
    assert.equal(ui.get('account-email-kimi').textContent, stale ? '已登录 · 待刷新' : '已登录', 'no phone, nickname or email fragment on the card');
    const footer = ui.get('provider-account-kimi');
    if (!stale) assert.match(footer.textContent, expected);
    else assert.match(footer.textContent, /上次确认登录/);
    assert.match(footer.textContent, /Plus/);
    if (email) assert.doesNotMatch(ui.get('account-email-kimi').textContent, /手机号/);
    assert.doesNotMatch(footer.textContent, /15512340567/); assert.equal(ui.posts().length, 0);
  }
});

test('typed quota recovery names the exhausted partner and keeps the audited continuation gate and draft intact', async () => {
  const saved = nativeRecoverySnapshot();
  saved.current_run.native_quota_failure = { source_job_id: jobId, provider: 'codex', reason: 'provider_quota_exhausted', http_status: 429 };
  saved.current_run.native_recovery_kind = 'partial_work'; saved.current_run.native_recovery_provider = 'claude';
  const partners = availabilityPartners({ codex: { state: 'blocked', selectable: false, reason: 'subscription_limit_reached', resets_at: 2000000000 } });
  const resumed = structuredClone(saved); resumed.room.status = 'running'; resumed.objective.status = 'running';
  resumed.current_run.status = 'in_progress'; resumed.current_run.id = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
  resumed.current_run.can_recover_native = false; resumed.team.phase = 'working';
  resumed.collaboration = teamCollaboration({ state: 'in_progress', tasks: resumed.team.tasks });
  const ui = await browser({ agents: catalog.slice(0, 2), partners, rooms: [saved.room], saved,
    executionPolicySupport: true, nativeProjectSupport: true, route(request) {
      if (request.path.endsWith('/recover-native')) return resumed;
    } });
  const initialPosts = ui.posts().length;
  ui.type('保留补充，不要代发 @Codex'); ui.get('message').fire('keydown', { key: 'Enter' });
  assert.match(ui.get('collaboration-outcome').textContent, /Codex.*订阅额度已用完/);
  assert.match(ui.get('collaboration-outcome').textContent, /预计重置/);
  assert.match(ui.get('collaboration-outcome').textContent, /执行或清理尚未确认/);
  await ui.click('recover-native'); assert.equal(ui.posts().length, initialPosts);
  saved.current_run.can_recover_native = true; saved.current_run.native_recovery_reason = null; await ui.poll();
  assert.match(ui.get('collaboration-outcome').textContent, /由 Claude Code 接着做/);
  assert.match(ui.get('collaboration-outcome').textContent, /伙伴会先核对保留的改动/);
  await ui.click('recover-native');
  const sent = ui.posts().slice(initialPosts); assert.equal(sent.length, 1); assert.equal(sent[0].path, `/api/rooms/${roomId}/recover-native`);
  assert.deepEqual(sent[0].body, { request_id: sent[0].body.request_id, run_id: saved.current_run.id,
    revision: saved.current_run.revision, digest: saved.current_run.digest });
  assert.equal(ui.get('message').value, '保留补充，不要代发 @Codex');
  assert.deepEqual(resumed.current_run.execution_policy, saved.current_run.execution_policy);
});

test('only exact typed quota diagnostics explain exhaustion, without turning arbitrary 429 failures into quota or login advice', async () => {
  const saved = nativeRecoverySnapshot(); saved.current_run.native_quota_failure = { source_job_id: jobId, provider: 'codex', reason: 'provider_error', http_status: 429 };
  saved.native_executions = [{ job_id: jobId, provider: 'codex', run_id: saved.current_run.id, status: 'unknown',
    permission: 'full_access', tool_calls: 21, tool_results: 21, cleanup_confirmed: true, exit_code: 0,
    failure_diagnostic: { phase: 'prompt', reason: 'provider_error', http_status: 429 } }];
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, nativeProjectSupport: true });
  assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /订阅额度已用完/);
  assert.doesNotMatch(ui.get('native-execution-records').textContent, /订阅额度已用完/);
  saved.current_run.native_quota_failure.reason = 'provider_quota_exhausted';
  saved.native_executions[0].failure_diagnostic.reason = 'provider_quota_exhausted'; await ui.poll();
  assert.match(ui.get('collaboration-outcome').textContent, /Codex.*订阅额度已用完.*恢复时间未提供/);
  assert.match(ui.get('native-execution-records').textContent, /供应商已明确报告订阅额度用完/);
  assert.doesNotMatch(ui.get('native-execution-records').textContent, /重新登录|升级|购买/);
  assert.equal(ui.get('recover-native').disabled, true); assert.equal(ui.posts().length, 0);
});

test('automatic quota handoff stays visible without a confirmation button and accepts a bound ordinary chat message', async () => {
  const saved = withNaturalContinuation(nativeRecoverySnapshot());
  saved.current_run.native_quota_failure = { source_job_id: jobId, provider: 'codex', reason: 'provider_quota_exhausted', http_status: 429 };
  saved.current_run.native_quota_handoff = { source_job_id: jobId, state: 'pending', reason: null };
  saved.continuation.action = 'handoff-message';
  const partners = availabilityPartners({ codex: { state: 'blocked', selectable: false, reason: 'subscription_limit_reached' } });
  const ui = await browser({ agents: catalog.slice(0, 2), partners, rooms: [saved.room], saved,
    executionPolicySupport: true, nativeProjectSupport: true, naturalContinuationSupport: true, route(request) {
      if (request.path.endsWith('/messages')) return saved;
    } });
  const initialPosts = ui.posts().length;
  assert.match(ui.get('collaboration-outcome').textContent, /Codex.*额度已用完.*正在自动核对.*无需点击继续/);
  assert.equal(ui.document.getElementById('continue-current'), null); assert.equal(ui.document.getElementById('recover-native'), null);
  ui.type('保留当前范围，请先完成未完成项'); assert.equal(ui.get('send').disabled, false);
  await ui.submit(); const sent = ui.posts().slice(initialPosts); assert.equal(sent.length, 1);
  assert.equal(sent[0].path, `/api/rooms/${roomId}/messages`);
  assert.equal(sent[0].body.text, '保留当前范围，请先完成未完成项'); assert.deepEqual(sent[0].body.binding, saved.continuation.binding);
  saved.current_run.native_quota_handoff.state = 'in_progress'; await ui.poll();
  assert.match(ui.get('collaboration-outcome').textContent, /正在自动核对/);
  saved.continuation.can_send = false; saved.continuation.reason = 'team_cleanup_unconfirmed'; await ui.poll();
  assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /可以在下方补充要求/);
  saved.current_run.native_quota_handoff.state = 'blocked'; saved.current_run.native_quota_handoff.reason = 'team_cleanup_unconfirmed';
  saved.continuation.can_send = true; saved.continuation.reason = null; await ui.poll();
  assert.match(ui.get('collaboration-outcome').textContent, /自动接手暂未完成.*可以补充要求.*不会因此重试受阻/);
  ui.type('受阻时只保留这条补充'); assert.equal(ui.get('send').disabled, false); await ui.submit();
  assert.equal(ui.posts().length, initialPosts + 2); assert.equal(ui.posts().at(-1).path, `/api/rooms/${roomId}/messages`);
  assert.equal(ui.document.getElementById('continue-current'), null);
  saved.continuation.can_send = false; saved.continuation.reason = 'team_cleanup_unconfirmed'; await ui.poll();
  ui.type('这条先留作草稿'); assert.match(ui.get('collaboration-outcome').textContent, /自动接手暂未完成.*执行或清理尚未确认/);
  assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /点击“继续”/);
  assert.equal(ui.get('send').disabled, true); await ui.submit(); assert.equal(ui.posts().length, initialPosts + 2);
  assert.equal(ui.get('message').value, '这条先留作草稿');
  saved.current_run.native_quota_handoff.source_job_id = 'old-unrelated-job'; await ui.poll();
  assert.doesNotMatch(ui.get('collaboration-outcome').textContent, /自动接手暂未完成|正在自动核对/);
});

test('a provider quota at the summary checkpoint shows automatic takeover while accepted tasks and ordinary chat stay intact', async () => {
  const saved = withNaturalContinuation(nativeRecoverySnapshot());
  saved.room.status = 'running'; saved.current_run.status = 'in_progress'; saved.objective.status = 'checkpoint';
  saved.team.phase = 'checkpoint'; saved.team.tasks = ['T1', 'T2', 'T3'].map(id => ({ id, title: id, owner: 'codex', status: 'accepted' }));
  saved.collaboration = teamCollaboration({ state: 'in_progress', tasks: saved.team.tasks });
  saved.current_run.native_quota_failure = { source_job_id: jobId, provider: 'claude', reason: 'provider_quota_exhausted' };
  saved.current_run.native_quota_handoff = { source_job_id: jobId, state: 'in_progress', reason: null };
  saved.continuation.action = 'handoff-message';
  saved.native_executions = [{ job_id: jobId, provider: 'claude', run_id: saved.current_run.id, phase: 'checkpoint',
    status: 'failed', permission: 'full_access', tool_calls: 0, tool_results: 0, cleanup_confirmed: true, exit_code: 1,
    failure_diagnostic: { reason: 'provider_quota_exhausted' } }];
  const partners = availabilityPartners({ claude: { state: 'blocked', selectable: false, reason: 'subscription_limit_reached', resets_at: 2000000000 } });
  const ui = await browser({ agents: catalog.slice(0, 2), partners, rooms: [saved.room], saved,
    executionPolicySupport: true, nativeProjectSupport: true, naturalContinuationSupport: true, route(request) {
      if (request.path.endsWith('/messages')) return saved;
    } });
  const initialPosts = ui.posts().length; const tasks = structuredClone(saved.team.tasks);
  assert.equal(ui.get('collaboration-outcome').hidden, false);
  assert.match(ui.get('collaboration-outcome').textContent, /Claude Code.*订阅额度已用完.*预计重置.*自动核对.*无需点击继续/);
  assert.match(ui.get('native-execution-records').textContent, /供应商已明确报告订阅额度用完/);
  assert.equal(ui.document.getElementById('continue-current'), null); assert.equal(ui.document.getElementById('recover-native'), null);
  ui.type('总结请保留未覆盖范围'); assert.equal(ui.get('send').disabled, false); await ui.submit();
  const sent = ui.posts().slice(initialPosts); assert.equal(sent.length, 1); assert.equal(sent[0].path, `/api/rooms/${roomId}/messages`);
  assert.equal(sent[0].body.text, '总结请保留未覆盖范围'); assert.deepEqual(saved.team.tasks, tasks);
  assert.equal(saved.objective.status, 'checkpoint', 'quota and successful message delivery do not imply completion');
});

test('a historical quota retirement does not contradict a fresh full quota or silently restore this round participation', async () => {
  const saved = objectiveSnapshot({ status: 'running' }); saved.members[1].status = 'needs_user';
  saved.objective.retired_partners = { codex: { reason: 'subscription_limit_reached', at: 1100, cleanup_confirmed: true } };
  const account = accountFixture(); account.quota.windows = [{ name: 'primary', remaining_percent: 100, used_percent: 0, window_minutes: 300 }];
  const partners = availabilityPartners(); partners[1].account = account;
  const before = structuredClone(saved.objective.retired_partners);
  const ui = await browser({ agents: catalog.slice(0, 2), partners, rooms: [saved.room], saved, executionPolicySupport: true });
  assert.equal(ui.get('status-codex').textContent, '本轮暂不参与');
  assert.match(ui.get('task-codex').textContent, /此前额度受限.*当前额度见下方/);
  assert.doesNotMatch(ui.get('task-codex').textContent, /等待额度恢复|额度暂时用完/);
  assert.match(ui.get('provider-account-codex').textContent, /剩余 100%/);
  account.quota.windows[0].remaining_percent = 0; account.quota.windows[0].used_percent = 100; await ui.poll();
  assert.equal(ui.get('status-codex').textContent, '本轮暂不参与');
  assert.deepEqual(saved.objective.retired_partners, before);
  assert.equal(ui.posts().filter(request => !request.path.includes('/availability/')).length, 0);
});

test('interface language switches and persists without translating authored content or dispatching requests', async () => {
  const saved = withNaturalContinuation(restartSnapshot());
  saved.room.goal = '新任务'; saved.objective.original_goal = '新任务'; saved.events = [message(1, 'claude', '当前需要处理：新任务，方案与文档都保持原文。')];
  const languageStorage = new Map();
  const ui = await browser({ rooms: [saved.room], saved, languageStorage, executionPolicySupport: true, naturalContinuationSupport: true });
  ui.type('这是原样保存的草稿 @Codex'); ui.get('message').fire('keydown', { key: 'Enter' });
  const draft = ui.get('message').value;
  assert.equal(ui.get('interface-language').tagName, 'BUTTON');
  assert.equal(ui.get('interface-language').textContent, 'English');
  assert.equal(ui.get('interface-language').getAttribute('lang'), 'en');
  const calls = ui.requests.length; const posts = ui.posts().length;
  await ui.click('interface-language');
  assert.equal(ui.get('interface-language').textContent, '简体中文'); assert.equal(languageStorage.get('beanest:locale'), 'en');
  assert.equal(ui.document.documentElement.lang, 'en'); assert.equal(ui.document.title, 'Beanest');
  assert.match(ui.get('new-room').textContent, /New task/i);
  assert.doesNotMatch(ui.get('send-label').textContent, /[一-龥]/);
  assert.doesNotMatch(ui.get('message').placeholder, /[一-龥]/);
  assert.equal(ui.get('room-title').textContent, '新任务');
  assert.match(ui.get('events').textContent, /当前需要处理：新任务，方案与文档都保持原文。/);
  assert.equal(ui.get('message').value, draft); assert.match(ui.get('recipient-button').textContent, /Codex/);
  assert.equal(ui.requests.length, calls); assert.equal(ui.posts().length, posts);
  await ui.poll(); assert.doesNotMatch(ui.get('send-label').textContent, /[一-龥]/); assert.equal(ui.get('message').value, draft);
  const reloaded = await browser({ rooms: [saved.room], saved, languageStorage, language: 'zh-CN', executionPolicySupport: true, naturalContinuationSupport: true });
  assert.equal(reloaded.get('interface-language').textContent, '简体中文');
  await reloaded.click('interface-language');
  assert.equal(reloaded.get('send-label').textContent, '发送'); assert.equal(reloaded.document.documentElement.lang, 'zh-CN');
  assert.equal(reloaded.get('interface-language').textContent,'English');
  assert.equal(reloaded.get('interface-language').getAttribute('lang'),'en');
  assert.equal(languageStorage.get('beanest:locale'),'zh-CN');
});

test('English is a browser default only without an explicit preference and desktop preferences synchronize without task calls', async () => {
  const calls = [];
  const first = await browser({ language: 'en-US', desktopBridge: { currentLocale: 'zh-CN', setLocale: async locale => { calls.push(locale); return { ok: true, locale }; } } });
  assert.equal(first.get('interface-language').textContent, 'English'); assert.deepEqual(calls, []);
  const storage = new Map([['beanest:locale', 'en']]);
  const second = await browser({ language: 'zh-CN', languageStorage: storage, desktopBridge: { currentLocale: 'zh-CN', setLocale: async locale => { calls.push(locale); return { ok: true, locale }; } } });
  assert.equal(second.get('interface-language').textContent, '简体中文'); assert.deepEqual(calls, ['en']); assert.equal(second.posts().length, 0);
  const requests = second.requests.length;
  await second.click('interface-language');
  assert.equal(second.get('interface-language').textContent, 'English');
  assert.equal(second.document.documentElement.lang, 'zh-CN');
  assert.deepEqual(calls, ['en','zh-CN']); assert.equal(storage.get('beanest:locale'),'zh-CN');
  assert.equal(second.requests.length,requests); assert.equal(second.posts().length,0);
  const web = await browser({ language: 'en-CA' }); assert.equal(web.get('interface-language').textContent, '简体中文');
});

test('language changes preserve materials reader content and scroll, runtime selections and open task options', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture());
  saved.current_run.runtime_settings = { permission: 'workspace_write', members: { claude: { model: null, effort: null }, codex: { model: null, effort: null } } };
  saved.objective.status = 'running'; saved.collaboration.outcome.state = 'in_progress'; saved.team.phase = 'working';
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalContinuationSupport: true, route(request) { if (request.path === '/api/runtime-options') return runtimeOptionsFixture(); } });
  ui.type('阅读时的原样草稿'); await ui.click('task-materials-open');
  ui.get('task-materials-body').scrollTop = 89;
  const file = [...descendants(ui.get('document-shortcuts')), ...descendants(ui.get('document-more-items'))].find(node=>node.dataset.documentPath==='first.md');
  file.fire('click'); await settle(); const body = ui.get('reader-body').textContent; ui.get('reader-body').scrollTop = 73;
  const calls = ui.requests.length;
  await ui.click('interface-language');
  assert.equal(ui.get('delivery-reader').hidden,false); assert.equal(ui.get('reader-body').textContent,body);
  assert.equal(ui.get('reader-body').scrollTop,73); assert.equal(ui.get('message').value,'阅读时的原样草稿');
  assert.match(ui.get('reader-back').textContent,/materials/i); assert.equal(ui.requests.length,calls);
  await ui.click('reader-back'); assert.equal(ui.get('task-materials').hidden,false); assert.equal(ui.get('task-materials-body').scrollTop,89);
  await ui.click('task-materials-close'); await openRuntimeSettings(ui);
  const permission = ui.get('runtime-permission').value;
  const numeric = ui.document.createElement('input'); numeric.id = 'test-nontext-selection'; numeric.type = 'number'; numeric.value = '42';
  numeric.selectionStart = 0; numeric.selectionEnd = 0; numeric.setSelectionRange = () => { throw Error('number inputs cannot select text'); };
  ui.get('composer').append(numeric);
  await ui.click('interface-language');
  assert.equal(ui.get('runtime-settings-popover').hidden,false); assert.equal(ui.get('runtime-permission').value,permission);
  assert.equal(numeric.value,'42');
  assert.equal(ui.get('message').value,'阅读时的原样草稿'); assert.equal(ui.posts().length,0);
});

test('language selection refreshes empty-home partner cards and connection copy in both directions without requests', async () => {
  const ui = await browser({ language: 'en-US' }); const calls = ui.requests.length;
  assert.equal(ui.get('interface-language').textContent, '简体中文'); assert.match(ui.get('status-claude').textContent,/Not started/i);
  ui.type('欢迎页独立草稿');
  await ui.click('interface-language');
  assert.equal(ui.get('status-claude').textContent,'未启动'); assert.equal(ui.get('connection').textContent,'本地服务已连接');
  assert.equal(ui.get('interface-language').getAttribute('aria-label'),'English');
  assert.doesNotMatch(ui.get('member-claude').textContent,/Not started|Last confirmed|Current task|Task session/);
  assert.equal(ui.get('message').value,'欢迎页独立草稿'); assert.equal(ui.requests.length,calls);
  await ui.click('interface-language');
  assert.match(ui.get('status-claude').textContent,/Not started/i); assert.match(ui.get('connection').textContent,/connected/i);
  assert.equal(ui.get('interface-language').getAttribute('aria-label'),'简体中文');
  assert.equal(ui.get('message').value,'欢迎页独立草稿'); assert.equal(ui.requests.length,calls);
});

test('localized stage continuation and bound questions remain actionable without presenting an unfinished objective as complete', async () => {
  for (const language of ['zh-CN','en-US']) {
    const saved = objectiveSnapshot();
    const ui = await browser({ language, rooms:[saved.room], saved, executionPolicySupport:true,
      route(request) { if (request.path.endsWith('/continue')) return saved; } });
    assert.equal(ui.document.querySelector('.completion-next'),null);
    assert.ok(ui.get('continue-objective')); assert.equal(ui.get('continue-objective').disabled,false);
    ui.type('阶段之间保留的草稿'); await ui.click('continue-objective');
    assert.equal(ui.posts().length,1); assert.equal(ui.posts()[0].path,`/api/rooms/${roomId}/continue`);
    assert.equal(ui.posts()[0].body.run_id,saved.current_run.id); assert.equal(ui.get('message').value,'阶段之间保留的草稿');
    const question = objectiveSnapshot({status:'needs_user',decision:'needs_user'});
    question.objective.can_answer=true; question.objective.next_step.question='请核对这一项规则？';
    const pending = await browser({language,rooms:[question.room],saved:question,executionPolicySupport:true});
    assert.equal(pending.get('collaboration-outcome').hidden,false);
    assert.equal(pending.get('collaboration-outcome').textContent.split('请核对这一项规则？').length-1,1);
    pending.type('尚未发送的回答'); question.objective.next_step.binding.digest='f'.repeat(64); await pending.poll();
    assert.doesNotMatch(pending.get('collaboration-outcome').textContent,/请核对这一项规则/);
    assert.equal(pending.get('message').value,'尚未发送的回答'); assert.equal(pending.posts().length,0);
  }
});

test('English Markdown interface notes translate while authored image labels and full source stay verbatim', async () => {
  const saved = readableDelivery(); const file = saved.project.files[0];
  file.content = '# 用户的中文标题\n\n![原样图片说明](https://example.test/image.png)\n\n![](https://example.test/untitled.png)\n\n' + '正文与代码不翻译。'.repeat(35000);
  const ui = await browser({language:'en-US',rooms:[saved.room],saved}); const before = ui.requests.length;
  deliveryButton(ui,file.path).fire('click'); await settle();
  assert.match(ui.get('reader-body').textContent,/Image: 原样图片说明 \(not loaded automatically\)/);
  assert.match(ui.get('reader-body').textContent,/Image: Untitled \(not loaded automatically\)/);
  assert.match(ui.get('reader-body').textContent,/Switch to View source/);
  assert.match(ui.get('reader-body').textContent,/用户的中文标题/);
  assert.equal(descendants(ui.get('reader-body')).some(node=>node.tagName==='IMG'),false);
  await ui.click('reader-source'); assert.equal(ui.get('reader-body').textContent,file.content);
  assert.equal(ui.requests.length,before); assert.equal(ui.posts().length,0);
});

test('English platform descriptors translate mixed fragments without reinterpreting model questions titles or legacy text', async () => {
  const saved = objectiveSnapshot({ status: 'needs_user', decision: 'needs_user' });
  const question = '伙伴正在按共享清单推进。';
  saved.objective.next_step.question = question;
  saved.collaboration = { mode: 'team', phase: 'working', summary: '共享任务 0/1 已交付并检查',
    summary_i18n: { parts: [{ key: '共享任务 {p0}/{p1} 已交付并检查', params: { p0: '0', p1: '1' } }] },
    tasks: [{ id: 'T1', owner: 'claude', status: 'assigned', title: '等待独立审核', result: '等待独立审核', waiting_for: '等待独立审核',
      waiting_for_i18n: { parts: [{ key: '等待独立审核' }] } }],
    outcome: { state: 'needs_user', reason: question, reason_i18n: { parts: [{ key: '等待独立审核' }] }, next_action: '模型原文：等待独立审核',
      remaining: ['等待独立审核：尚未交付并通过互审'],
      remaining_i18n: [{ parts: [{ text: '等待独立审核' }, { key: '：尚未交付并通过互审' }] }],
      validation: { scope: 'not_run', summary: '没有当前版本的实际运行通过记录。',
        summary_i18n: { parts: [{ key: '没有当前版本的实际运行通过记录。' }] } }, artifacts: [] } };
  const ui = await browser({ language: 'en', rooms: [saved.room], saved });
  assert.match(ui.get('collaboration-plan').textContent, /Shared tasks|shared tasks/);
  assert.match(ui.get('collaboration-plan').textContent, /等待独立审核/);
  assert.match(ui.get('collaboration-plan').textContent, /independent review/i);
  assert.ok(ui.get('collaboration-outcome').textContent.includes(question));
  assert.equal(ui.posts().length, 0);
  saved.objective.status = 'complete'; saved.objective.next_step.decision = 'complete'; saved.collaboration.outcome.state = 'ready_for_user';
  await ui.poll();
  assert.match(ui.get('collaboration-outcome').textContent, /等待独立审核.*not yet|等待独立审核.*not delivered/i);
  assert.equal(ui.posts().length, 0);
});

test('English quota windows and automatic handoff sentences retain readable system boundaries', async () => {
  const saved = withNaturalContinuation(nativeRecoverySnapshot());
  saved.current_run.native_quota_failure = { source_job_id: jobId, provider: 'codex', reason: 'provider_quota_exhausted' };
  saved.current_run.native_quota_handoff = { source_job_id: jobId, state: 'pending', reason: null };
  saved.continuation.action = 'handoff-message';
  const partners = availabilityPartners({ codex: { state: 'blocked', selectable: false, reason: 'subscription_limit_reached' } });
  partners[0].account = accountFixture({ quota: { state: 'available', scope: 'provider_account', windows: [{ remaining_percent: 27, window_minutes: 10080 }] } });
  const ui = await browser({ language: 'en', agents: catalog.slice(0,2), partners, rooms: [saved.room], saved,
    executionPolicySupport: true, nativeProjectSupport: true, naturalContinuationSupport: true });
  assert.match(ui.get('composer-usage-claude').getAttribute('aria-label'), /7-day.* 27% remaining/);
  let notice = ui.get('collaboration-outcome').textContent;
  assert.match(notice, /provided\. Existing results/);
  assert.match(notice, /Continue\. You can/);
  saved.current_run.native_quota_handoff.state = 'blocked'; saved.current_run.native_quota_handoff.reason = 'team_cleanup_unconfirmed'; await ui.poll();
  notice = ui.get('collaboration-outcome').textContent;
  assert.doesNotMatch(notice, /[.!]You can/);
  assert.equal(ui.posts().filter(request => /\/(messages|recover-native|continue|runs)(?:\?|$)/.test(request.path)).length,0);
});

test('English working status separates system fragments from raw task names and localizes accessible punctuation', async () => {
  const saved = activeActivitySnapshot();
  saved.team.tasks = [{id:'T1',title:'用户原文标题',owner:'claude',status:'working',write_paths:['main.py']}];
  saved.jobs = [{id:jobId,provider:'claude',kind:'team_work',status:'working',run_id:saved.current_run.id,task_id:'T1'}];
  saved.events.push({kind:'job_claimed',job_id:jobId,sender:'claude',run_id:saved.current_run.id});
  saved.room.work_activity = {state:'working',draining:false};
  saved.room.objective_status = 'running';
  const room = saved.room;
  const ui = await browser({language:'en',rooms:[room],saved});
  // English quotes a task name with “ ” instead of leaking 「」 (hard-coded punctuation audit).
  assert.match(ui.get('partner-activity-claude').textContent,/Building “用户原文标题”/);
  assert.doesNotMatch(ui.get('partner-activity-claude').textContent,/[「」、：；（）]/);
  const labels = descendants(ui.get('rooms')).map(node=>node.getAttribute('aria-label')).filter(Boolean);
  assert.ok(labels.some(label=>label.includes(', Working')), JSON.stringify(labels));
  assert.ok(labels.every(label=>!label.includes('，')));
  assert.equal(ui.posts().length,0);
});

test('English repository labels use source-bound descriptors while repository names and unknown labels remain opaque', async () => {
  const saved = withNaturalContinuation(completedSuggestionFixture()); const {data,bind} = deliveryRepositoryFixture();
  bind('delivery:app');
  const name = '用户原文仓库';
  const label = '交付所在仓库 · ' + name;
  const descriptor = {parts:[{key:'交付所在仓库 · {p0}',params:{p0:name}}]};
  Object.assign(data.workspaces[0],{label,label_i18n:descriptor});
  Object.assign(data.delivery_repository.candidates[0],{label,label_i18n:descriptor});
  const ui = await browser({language:'en',rooms:[saved.room],saved,codeStatusSupport:true,naturalContinuationSupport:true,
    route(request){if(request.path.includes('/code-status'))return data;}});
  assert.match(ui.get('code-repository-label').textContent,/Delivery repository.*用户原文仓库/i);
  assert.doesNotMatch(ui.get('code-repository-label').textContent,/交付所在仓库/);
  await ui.click('code-repository-edit');
  assert.ok(ui.get('code-repository-choice').children.some(node=>node.textContent==='核对副本'));
  assert.equal(ui.posts().length,0);
});

test('progress record labels switch both directions while preserving expansion count scroll and authored event text', async () => {
  const saved = activeActivitySnapshot();
  const ui = await browser({rooms:[saved.room],saved});
  const records = ui.get('activity-records'); records.open = true; records.scrollTop = 37; ui.get('activity-events').scrollTop = 22;
  const count = ui.get('activity-count').textContent; const events = ui.get('activity-events').textContent;
  const calls = ui.requests.length;
  await ui.click('interface-language');
  assert.equal(ui.get('activity-records-label').textContent,'Progress records ');
  assert.equal(ui.get('activity-count').textContent,count); assert.equal(records.open,true); assert.equal(records.scrollTop,37);
  await ui.click('interface-language');
  assert.equal(ui.get('activity-records-label').textContent,'进展记录 ');
  assert.equal(ui.get('activity-events').textContent,events); assert.equal(records.open,true); assert.equal(records.scrollTop,37);
  assert.equal(ui.get('activity-events').scrollTop,22);
  assert.equal(ui.get('activity-events').children.length,Number(count));
  assert.equal(ui.requests.length,calls); assert.equal(ui.posts().length,0);
});

test('host review-only interruption uses bound platform explanation without calling it a format failure or changing recovery gates', async () => {
  const reason = '审核被旧的负责人校验误拒，原任务和已提交结果仍保留。';
  const next = '继续后只补审核，不会重复执行已完成的工作。';
  for (const language of ['zh-CN','en']) {
    const saved = nativeRecoverySnapshot(); saved.objective.interruption_kind='host_result_validation';
    Object.assign(saved.collaboration.outcome,{reason,next_action:next,
      reason_i18n:{parts:[{key:reason,params:{}}]},next_action_i18n:{parts:[{key:next,params:{}}]}});
    const ui = await browser({language,rooms:[saved.room],saved,executionPolicySupport:true,nativeProjectSupport:true});
    const expected = language==='en' ? /old ownership check incorrectly rejected.*retry the review only/s : /审核被旧的负责人校验误拒.*继续后只补审核/s;
    const notice = descendants(ui.get('collaboration-outcome')).find(node=>node.classList.contains('outcome-reason'));
    assert.match(notice.textContent,expected);
    assert.doesNotMatch(notice.textContent,/格式校验|format validation/i);
    assert.match(ui.get('task-claude').textContent,expected);
    ui.type('保留我的补充 @Codex');
    const recipient = ui.get('recipient-button').textContent;
    assert.equal(ui.get('recover-native').disabled,true); await ui.click('recover-native'); assert.equal(ui.posts().length,0);
    saved.collaboration.outcome.reason_i18n.parts[0].key='stale metadata'; await ui.poll();
    const fallback = descendants(ui.get('collaboration-outcome')).find(node=>node.classList.contains('outcome-reason'));
    assert.match(fallback.textContent,language==='en' ? /format validation/i : /格式校验/);
    assert.equal(ui.get('message').value,'保留我的补充 @Codex'); assert.equal(ui.get('recipient-button').textContent,recipient);
    delete saved.collaboration.outcome.reason_i18n; await ui.poll();
    assert.equal(ui.posts().length,0); assert.equal(ui.get('recover-native').disabled,true);
  }
});

test('browser settlement waits for real SHA work but leaves held network reads and intervening drafts controllable', async () => {
  for (const outcome of ['valid', 'bad-sha', 'new-draft']) {
    const source = objectiveSnapshot({status:'complete',decision:'complete'}); source.room.status='completed';
    const full = '精确的完整目标'.repeat(1000);
    const ref = {kind:'run_goal',room_id:roomId,run_id:source.current_run.id,sha256:createHash('sha256').update(full).digest('hex')};
    Object.assign(source.current_run,{goal_preview:full.slice(0,20),goal_text_ref:ref,goal_truncated:true,can_fork:true,revision:0});
    delete source.current_run.goal;
    const saved = pagedFixture(source,[]); const network=deferred();
    const ui = await browser({rooms:[source.room],saved,pagingSupport:true,executionPolicySupport:true,route(request){
      if(request.path.endsWith('/state'))return saved;
      if(request.path.includes('/text?'))return network.promise;
      return eventPageRoute(request,[]);
    }});
    await ui.click('fork-room');
    assert.equal(ui.requests.filter(request=>request.path.includes('/text?')).length,1);
    assert.equal(ui.get('message').value,'','settlement must return while the network response is deliberately held');
    if(outcome==='new-draft')ui.type('网络等待期间的新草稿');
    network.resolve({...ref,text:outcome==='bad-sha'?'不匹配的正文':full,truncated:false});
    await settle();
    assert.equal(pendingCryptoDigests.size,0);
    if(outcome==='valid')assert.equal(ui.get('message').value,full);
    else if(outcome==='bad-sha'){
      assert.equal(ui.get('message').value,'');assert.match(ui.get('notice').textContent,/原文内容与记录版本不一致/);
    }else assert.equal(ui.get('message').value,'网络等待期间的新草稿');
    assert.equal(ui.posts().length,0);
  }
  await assert.rejects(trackedDigest('not-a-real-hash',new Uint8Array()),{name:'NotSupportedError'});
  await settle();assert.equal(pendingCryptoDigests.size,0,'rejected native computations must be released without swallowing the rejection');
});

test('current stage delivery starts collapsed and preserves deliberate expansion and collapse through reading and polling', async () => {
  const saved = withNaturalContinuation(objectiveDeliveryFixture());
  saved.objective.status='awaiting_continue'; saved.team.phase='ready_for_user';
  const ui = await browser({rooms:[saved.room],saved,executionPolicySupport:true,naturalContinuationSupport:true});
  let files=ui.document.querySelector('.delivery-content');
  assert.equal(files.tagName,'DETAILS'); assert.equal(files.firstChild.tagName,'SUMMARY');
  assert.equal(files.dataset.outcomeDetail,'current'); assert.equal(files.open,false);
  ui.type('保留未发送草稿');
  files.open=true; saved.collaboration.outcome.validation.summary='刷新检查范围'; await ui.poll();
  files=ui.document.querySelector('.delivery-content'); assert.equal(files.open,true);
  const read=descendants(files).find(node=>node.tagName==='BUTTON' && node.dataset.documentPath);
  assert.ok(read); read.fire('click'); await settle(); assert.equal(ui.get('delivery-reader').hidden,false);
  await ui.click('reader-back'); assert.equal(ui.document.querySelector('.delivery-content').open,true);
  ui.document.querySelector('.delivery-content').open=false;
  saved.collaboration.outcome.validation.summary='再次刷新检查范围'; await ui.poll();
  assert.equal(ui.document.querySelector('.delivery-content').open,false);
  assert.equal(ui.get('message').value,'保留未发送草稿'); assert.equal(ui.posts().length,0);
});

test('policy field captions span the full field instead of wrapping into the 80px label column', () => {
  // Catches the composer "processing mode" note collapsing to two or three words per line.
  const html = fs.readFileSync(path.join(assetRoot, 'index.html'), 'utf8');
  assert.match(html, /<label id="project-workflow-control" class="policy-field"[^>]*>[\s\S]*?<span class="detail-caption">/);
  const css = fs.readFileSync(path.join(assetRoot, 'style.css'), 'utf8');
  assert.match(css, /\.policy-field \{[^}]*grid-template-columns: 80px minmax\(0, 1fr\)/);
  assert.match(css, /\.policy-field > \.detail-caption \{[^}]*grid-column: 1 \/ -1/);
});

test('account popover names the login source, shows a copyable login command for an unsigned profile home, and switches back', async () => {
  // Catches: a profile silently sharing the local login, a missing or altered login command,
  // Beanest posting anything that would sign in, and a switch allowed while the partner works.
  const saved = snapshot({ agents: catalog.slice(0, 2) });
  const command = "CLAUDE_CONFIG_DIR='/runtime/partners/homes/claude' '/opt/homebrew/bin/claude' auth login";
  const team = accountFixture({ email: 'team@example.com', plan: 'max', auth_method: 'claude.ai', source: 'claude_auth_status', shared_profile_ids: ['claude'] });
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true,
    account_source: { kind: 'local', home: null, variable: null, login_command: null },
    account: agent.id === 'claude' ? team : accountFixture() }));
  const directory = () => ({ partners, supported_adapters: [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }] });
  const ui = await browser({ rooms: [saved.room], saved, partners, route(request) {
    if (request.path.startsWith('/api/partners/claude/account-source')) {
      partners[0].account_source = request.body.source === 'profile_home'
        ? { kind: 'profile_home', home: '/runtime/partners/homes/claude', variable: 'CLAUDE_CONFIG_DIR', login_command: command }
        : { kind: 'local', home: null, variable: null, login_command: null };
      partners[0].account = request.body.source === 'profile_home'
        ? accountFixture({ state: 'not_checked', email: null, plan: null, auth_method: null, source: 'claude_auth_status', scope: 'profile_cli_home',
          checked_at: null, stale: true, shared_profile_ids: ['claude'], quota: { state: 'not_checked', windows: [], stale: true } }) : team;
      return directory();
    }
    if (request.path.startsWith('/api/partners/claude/account/refresh')) {
      partners[0].account = accountFixture({ email: 'personal@example.com', plan: 'pro', auth_method: 'claude.ai', source: 'claude_auth_status',
        scope: 'profile_cli_home', shared_profile_ids: ['claude'] });
      return directory();
    }
  } });
  const footer = () => ui.get('provider-account-claude');
  const switchButton = () => descendants(footer()).find((node) => node.tagName === 'BUTTON' && node.dataset.focusKey === 'account-source');
  assert.match(footer().textContent, /登录方式本机登录/);
  assert.match(footer().textContent, /使用本机 Claude Code 的当前登录/);
  assert.equal(switchButton().textContent, '改用独立登录');
  assert.equal(descendants(footer()).some((node) => node.tagName === 'CODE'), false, 'the local login shows no login command');

  switchButton().fire('click'); await settle();
  assert.deepEqual(ui.posts().map((request) => [request.path.split('?')[0], request.body]), [['/api/partners/claude/account-source', { source: 'profile_home' }]]);
  assert.match(footer().textContent, /登录方式豆窝独立登录/);
  assert.match(footer().textContent, /使用豆窝为它单独保存的 Claude Code 登录，不影响本机登录和其他伙伴/);
  assert.match(footer().textContent, /豆窝不会替你登录/);
  const code = descendants(footer()).find((node) => node.tagName === 'CODE');
  assert.equal(code.textContent, command, 'the command is shown exactly as the service quoted it');
  const copy = descendants(footer()).find((node) => node.tagName === 'BUTTON' && node.dataset.focusKey === 'account-login-copy');
  copy.fire('click'); await settle();
  assert.match(ui.get('manager-notice').textContent, /手动选中命令复制/, 'without clipboard access the user is told to copy by hand');
  assert.match(ui.get('provider-account-codex').textContent, /使用本机 Codex 的当前登录/, 'other partners keep the local login');

  accountRefreshButton(footer()).fire('click'); await settle();
  assert.match(footer().textContent, /personal@example.com/);
  assert.equal(descendants(footer()).some((node) => node.tagName === 'CODE'), false, 'a signed-in profile home hides the login command');
  assert.equal(switchButton().textContent, '改回本机登录');

  partners[0].monitor = { status: 'working', current_tasks: [{ room_id: roomId, job_id: jobId, status: 'working' }] };
  await ui.click('open-partners');
  assert.equal(switchButton().disabled, true);
  assert.match(footer().textContent, /Claude Code正在执行任务，结束后才能切换登录方式/);

  partners[0].monitor = { status: 'idle', current_tasks: [] };
  await ui.click('open-partners');
  assert.equal(switchButton().disabled, false);
  switchButton().fire('click'); await settle();
  assert.deepEqual(ui.posts().at(-1).body, { source: 'local' });
  assert.match(footer().textContent, /登录方式本机登录/);
  assert.equal(switchButton().textContent, '改用独立登录');
  assert.ok(ui.posts().every((request) => /\/api\/partners\/claude\/(account-source|account\/refresh)/.test(request.path)),
    'no login, room or model request is ever sent');
});

// ---- 0.3.0 sweep: interface and copy fixes (A3–A23, B6–B17) ----

test('a confirmed stop no longer says the stop is still being checked', async () => {
  // Catches B7: after the service confirmed the user's stop (and after a reload) the card kept "已请求停止，正在核对本服务登记的执行".
  const saved = restartSnapshot(); saved.objective.stop_reason = 'cancel_requested'; saved.stop_kind = null;
  saved.cancellation_confirmed = true;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, continuousObjectiveSupport: true });
  assert.equal(ui.get('now-title').textContent, '推进已停止');
  assert.match(ui.get('now-text').textContent, /^已按你的要求停下。/);
  assert.doesNotMatch(ui.get('now-card').textContent, /正在核对/);
  const pending = restartSnapshot(); pending.objective.stop_reason = 'cancel_requested'; pending.stop_kind = null;
  pending.cancellation_confirmed = false;
  const waiting = await browser({ rooms: [pending.room], saved: pending, executionPolicySupport: true, continuousObjectiveSupport: true });
  assert.match(waiting.get('now-text').textContent, /^已请求停止，正在核对本服务登记的执行。/, 'an unconfirmed stop still says it is being checked');
});

test('after a restart that resumes by itself the first screen says it continues, not that nothing reruns', async () => {
  // Catches B6: "…不会自动重跑" with a resume button while the service was about to continue on its own.
  const saved = restartSnapshot(); saved.objective.stop_reason = 'service_restarted'; saved.objective.status = 'interrupted';
  saved.stop_kind = 'auto'; saved.room.objective_status = 'interrupted'; saved.room.stop_kind = 'auto';
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, continuousObjectiveSupport: true, naturalContinuationSupport: true });
  assert.match(ui.get('now-label').textContent, /^马上自动继续/);
  assert.equal(ui.get('now-title').textContent, '豆窝服务重启过，马上自动接着做');
  assert.doesNotMatch(ui.get('now-card').textContent, /不会自动重跑/);
  assert.match(ui.get('rooms').children[0].textContent, /马上自动继续/);
});

test('the delete confirmation and lifecycle labels use the sidebar name, not the latest round goal', async () => {
  // Catches B9: the sidebar showed the first goal while the confirm box named the last round's goal.
  const saved = restartSnapshot();
  saved.room.goal = '第三轮：把颜色改成蓝色'; saved.room.objective_goal = '完成这份虚构的小工具';
  const listed = { ...saved.room };
  delete saved.room.objective_goal;  // the open room's own record carries only the latest round
  const ui = await browser({ rooms: [listed], saved, executionPolicySupport: true, roomLifecycleSupport: true });
  ui.get('rooms').children[0].fire('click'); await settle();
  assert.equal(ui.get('rooms').children[0].firstChild.textContent, '完成这份虚构的小工具');
  const remove = descendants(ui.get('room-lifecycle-actions')).find((node) => node.dataset.lifecycleAction === 'remove');
  assert.equal(remove.getAttribute('aria-label'), '永久删除任务：完成这份虚构的小工具');
  remove.fire('click'); await settle();
  assert.equal(ui.get('lifecycle-confirm-title').textContent, '永久删除任务“完成这份虚构的小工具”？');
  const english = await browser({ language: 'en', rooms: [listed], saved, executionPolicySupport: true, roomLifecycleSupport: true });
  english.get('rooms').children[0].fire('click'); await settle();
  const archive = descendants(english.get('room-lifecycle-actions')).find((node) => node.dataset.lifecycleAction === 'archive');
  assert.equal(archive.getAttribute('aria-label'), 'Archive task: 完成这份虚构的小工具', 'A19: the English label has its space');
});

test('the files panel opens a delivered file in the reader instead of saying its content is unavailable', async () => {
  // Catches B8: the state sends content_available, not content, so every file read "内容不可用".
  const sha = 'a'.repeat(64);
  const saved = objectiveSnapshot({ status: 'complete' });
  saved.room.mode = 'project_files';
  saved.project = { available: true, revision: 2, snapshot_sha256: 'b'.repeat(64), files: [
    { path: 'notes.md', sha256: sha, owner: 'codex', content_available: true },
    { path: 'other.txt', sha256: 'c'.repeat(64), owner: 'claude', content_available: true }] };
  saved.objective.delivery = { revision: 2, digest: 'b'.repeat(64), files: [{ path: 'notes.md', sha256: sha, verified: true,
    run_id: saved.current_run.id, revision: 2, digest: 'b'.repeat(64) }] };
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, route(request) {
    if (request.path.startsWith(`/api/rooms/${roomId}/file?`)) return { run_id: saved.current_run.id, revision: 2, digest: 'b'.repeat(64), path: 'notes.md', sha256: sha, content: '# 笔记' };
  } });
  const files = ui.get('project-files');
  assert.doesNotMatch(files.textContent, /内容不可用/);
  const open = descendants(files).find((node) => node.dataset.openProjectFile === 'notes.md');
  assert.ok(open, 'a delivered file has an open button');
  assert.equal(open.textContent, '打开阅读');
  assert.match(files.textContent, /other\.txt[\s\S]*这里不直接显示正文，点上面的“下载成果”可以拿到全部文件。/);
  open.fire('click'); await settle();
  assert.equal(ui.get('delivery-reader').hidden, false);
});

test('names, labels and pickers stay in the chosen language', async () => {
  // Catches A19: an English sidebar tooltip "Claude Code：剩余 47%", a Chinese checkbox label and an untranslated "ultra" effort.
  const languageStorage = new Map();
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, account: accountFixture() }));
  const ui = await browser({ partners, languageStorage });
  assert.equal(participantInput(ui, 'claude').getAttribute('aria-label'), '邀请 Claude Code');
  const meter = ui.get('nav-quota').children[0];
  assert.equal(meter.title, 'Claude Code：剩余 0%');
  ui.get('interface-language').fire('click'); await settle();
  assert.equal(participantInput(ui, 'claude').getAttribute('aria-label'), 'Invite Claude Code');
  assert.equal(ui.get('nav-quota').children[0].title, 'Claude Code: 0% left');
});

test('Codex ultra effort is named in both languages and not repeated with another level', async () => {
  // Catches A19: the ultra option showing the raw id "ultra" in the Chinese interface.
  const saved = withRuntimeConfiguration(objectiveSnapshot({ status: 'complete' }));
  saved.runtime_configuration.desired.runtime_settings.members.codex = { model: 'gpt-x', effort: 'ultra' };
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalRuntimeSupport: true });
  assert.match(ui.get('member-model-codex').textContent, /· 极高 ⌄$/);
  const english = await browser({ language: 'en', rooms: [saved.room], saved, executionPolicySupport: true, naturalRuntimeSupport: true });
  assert.match(english.get('member-model-codex').textContent, /· Ultra ⌄$/);
});

test('a saved model choice keeps its friendly name after a reload without the model list', async () => {
  // Catches B11: after a refresh the labels showed raw ids such as "default" and "kimi-code/kimi-for-coding".
  const saved = withRuntimeConfiguration(objectiveSnapshot({ status: 'complete' }));
  saved.runtime_configuration.desired.runtime_settings.members.claude = { model: 'default', effort: 'low' };
  saved.runtime_configuration.desired.runtime_settings.members.codex = { model: 'kimi-code/kimi-for-coding', effort: null };
  const fresh = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalRuntimeSupport: true });
  assert.equal(fresh.get('member-model-claude').textContent, '默认推荐 · 低 ⌄', 'without any saved names, default reads as the tool default');
  assert.equal(fresh.get('member-model-codex').textContent, 'kimi-for-coding ⌄', 'a path id keeps its last part, and a default effort is left off the card');
  const languageStorage = new Map([['beanest:model-names-v1', JSON.stringify({ claude: {
    default: { name: '', resolved_model: 'claude-opus-5-5' }, opus: { name: 'Opus 5.5', resolved_model: 'claude-opus-5-5' } } })]]);
  const reloaded = await browser({ languageStorage, rooms: [saved.room], saved, executionPolicySupport: true, naturalRuntimeSupport: true });
  assert.equal(reloaded.get('member-model-claude').textContent, 'Opus 5.5（默认） · 低 ⌄');
});

test('the progress pane still lists what earlier rounds finished after the objective moves to a new run', async () => {
  // Catches B10: after a continuation only the new run's task was shown ("0/1 完成") and the finished work vanished.
  const saved = objectiveSnapshot({ status: 'running' });
  const first = { ...saved.current_run, id: '77777777-7777-4777-8777-777777777777', number: 1, finished_at: 1100 };
  saved.current_run = { ...saved.current_run, number: 2 };
  saved.runs = [first, { ...saved.current_run }];
  saved.team = { phase: 'working', tasks: [{ id: 'review', title: 'Kimi 复核 colors.md', owner: 'codex', status: 'open' }] };
  saved.collaboration.tasks = saved.team.tasks;
  const reads = [];
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, route(request) {
    if (request.path.startsWith(`/api/rooms/${roomId}/runs?run_id=`)) {
      reads.push(request.path);
      return { run: first, team: { tasks: [{ id: 'make', title: '创建 colors.md', owner: 'claude', status: 'accepted' },
        { id: 'skip', title: '没做完的一项', owner: 'codex', status: 'cancelled' }] } };
    }
  } });
  await settle();
  const pane = ui.get('progress-pane');
  assert.deepEqual(reads, [`/api/rooms/${roomId}/runs?run_id=${first.id}`]);
  assert.match(pane.textContent, /前几轮已完成 1 项/);
  assert.match(pane.textContent, /✓ 创建 colors\.md/);
  assert.doesNotMatch(pane.textContent, /没做完的一项/);
  await ui.poll();
  assert.equal(reads.length, 1, 'earlier runs are read once, not on every poll');
  assert.equal(ui.posts().length, 0);
});

test('a room without a project shows where its results are instead of repository controls', async () => {
  // Catches A7: a Q&A task's delivery tab showed 代码交付 / 选择仓库 / GitHub 同步.
  // Shaped like the real code-status answer for a room with no project (no_project_workspace, nothing to choose).
  const saved = objectiveSnapshot({ status: 'complete' }); saved.room.mode = 'text_artifacts'; saved.room.project_id = null;
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, codeStatusSupport: true, route(request) {
    if (request.path.startsWith(`/api/rooms/${roomId}/code-status`)) return { state: 'unbound', reason: 'no_project_workspace', workspace_id: null, workspaces: [],
      files: [], uncommitted: { total: null }, delivery_repository: { state: 'unbound', reason: 'delivery_repository_unbound', workspace_id: null, source: null, candidates: [] } };
  } });
  assert.equal(ui.get('code-status-card').hidden, true);
  assert.equal(ui.get('delivery-empty').hidden, false);
  assert.equal(ui.get('delivery-empty').textContent, '这个任务没有关联项目，没有代码要交付。伙伴的回复在“讨论”里，整理好的成果和文件在“资料”里。');
});

test('a deleted project is shown by the name the room recorded and said to be deleted', async () => {
  // Catches A10: after a permanent delete the room said only "关联项目" and the options "项目记录未找到".
  const saved = objectiveSnapshot({ status: 'complete' }); saved.room.mode = 'project_files';
  saved.room.project_id = '88888888-8888-4888-8888-888888888888';
  saved.project = { available: true, revision: 1, files: [], source: { id: saved.room.project_id, name: '记账小工具', path: '/fictional/ledger' } };
  const ui = await browser({ rooms: [saved.room], saved, projects: [], executionPolicySupport: true });
  assert.equal(ui.get('task-options-project').textContent, '记账小工具（已删除）');
  assert.equal(ui.get('room-project-chip').textContent, '记账小工具（已删除）');
  assert.equal(ui.get('frozen-project').textContent, '关联的项目“记账小工具”已从豆窝删除。原项目文件不受影响，这个任务里的副本和成果还在。');
  const kept = await browser({ rooms: [saved.room], saved, projects: [{ id: saved.room.project_id, name: '记账小工具', path: '/fictional/ledger', files: [] }], executionPolicySupport: true });
  assert.equal(kept.get('frozen-project').textContent, '关联项目：记账小工具 · 本任务项目固定。');
});

test('explicit file lists get a message about their actual problem', async () => {
  // Catches A9: every rejected list said "不要包含重复路径或上级目录", even for a binary file or too many files.
  const ui = await browser();
  await ui.click('open-projects');
  const submit = async (files) => {
    ui.get('project-name').value = '虚构项目'; ui.get('project-path').value = '/fictional/project';
    ui.get('project-explicit-files').checked = true; ui.get('project-files-input').value = files.join('\n');
    ui.get('project-form').fire('submit'); await settle();
    return ui.get('manager-notice').textContent;
  };
  assert.equal(await submit(Array.from({ length: 13 }, (_, index) => `n${index}.md`)), '最多 12 个文件，现在填了 13 个。');
  assert.equal(await submit(['/Users/me/a.md']), '请写相对项目文件夹的路径，例如 src/main.py，不要写完整路径：/Users/me/a.md');
  assert.equal(await submit(['src/../a.md']), '路径里不能有 ..、. 或空的一段，文件要在项目文件夹里面：src/../a.md');
  assert.equal(await submit(['a.md', 'A.md']), '这个文件填了两次：A.md');
  assert.equal(await submit(['logo.png']), 'logo.png 不是能处理的文本文件。只能选源码、文档、配置这类文本文件。');
  assert.equal(ui.posts().length, 0, 'nothing is sent for a list the service would reject');
  const missing = await browser({ route(request) { if (request.path === '/api/projects' && request.method === 'POST') return { error: 'project_file_unavailable' }; } });
  await missing.click('open-projects');
  missing.get('project-name').value = '虚构项目'; missing.get('project-path').value = '/fictional/missing';
  missing.get('project-form').fire('submit'); await settle();
  assert.equal(missing.get('manager-notice').textContent, '找不到这个文件夹，或者没有权限读取。请核对路径。');
});

test('registering a folder twice names the existing project, and a home folder needs a second click', async () => {
  // Catches A11: the same path registered again silently, and the whole home folder registered without a word.
  const projects = [registeredProject('99999999-9999-4999-8999-999999999999', 'ledger')];
  const ui = await browser({ projects, route(request) { if (request.path === '/api/projects' && request.method === 'POST') return { projects }; } });
  await ui.click('open-projects');
  ui.get('project-name').value = '再加一次'; ui.get('project-path').value = '/fictional/ledger/';
  ui.get('project-form').fire('submit'); await settle();
  assert.equal(ui.get('manager-notice').textContent, '这个文件夹已登记为「ledger」，直接选它就行。');
  assert.equal(ui.posts().length, 0);
  ui.get('project-name').value = '整个家目录'; ui.get('project-path').value = '/Users/someone';
  ui.get('project-form').fire('submit'); await settle();
  assert.equal(ui.get('manager-notice').textContent, '这是整个用户文件夹，里面有很多和项目无关的私人文件。建议只选项目所在的文件夹。确定要登记，就再点一次“添加项目”。');
  assert.equal(ui.posts().length, 0, 'the first click only warns');
  ui.get('project-form').fire('submit'); await settle();
  assert.equal(ui.posts().length, 1, 'the second click registers it');
  ui.get('project-path').value = '/'; ui.get('project-form').fire('submit'); await settle();
  assert.equal(ui.get('manager-notice').textContent, '不能登记整个磁盘的根目录，请选项目所在的文件夹。');
});

test('sign-in wording agrees everywhere and a failed quota check never reads as signed out', async () => {
  // Catches A4: the top line said a tool was ready while its card said 还差登录, a disabled checkbox sat next to
  // "仍可使用", and an old or failed account check made one view say 登录待刷新 while another said 已登录.
  const adapters = firstRunAdapters({ claude: { state: 'login_required' }, codex: { state: 'ready' } });
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, selectable: true, installation: { state: 'found' },
    account: agent.id === 'codex' ? accountFixture({ state: 'error', stale: true, reason: 'account_check_timeout' }) : null }));
  const listing = setupRoute(() => ({ partners, supported_adapters: adapters }));
  const ui = await browser({ agents: catalog.slice(0, 2), partners, route: listing });
  await ui.poll();
  assert.equal(ui.get('discovery-hint-text').textContent, '已找到 claude、codex。其中 claude 还差登录，其他的选好就可以开始。');
  assert.equal(ui.get('account-email-codex').textContent, '已登录 · 待刷新', 'an old account check is not a sign-out');
  const claudeOption = participantInput(ui, 'claude').parentNode;
  assert.equal(participantInput(ui, 'claude').disabled, true);
  const note = claudeOption.children.find((node) => node.classList.contains('participant-availability'));
  assert.equal(note.textContent, '还差登录');
  assert.doesNotMatch(claudeOption.textContent, /仍可使用/);
  await ui.click('open-partners');
  assert.match(ui.get('partners-list').textContent, /还差登录，登录后才能选入新任务/);
  assert.doesNotMatch(ui.get('partners-list').textContent, /claude · 可选入新任务/i);
});

test('a setup status that could not be read is not offered as a sign-in step', async () => {
  // Catches A4: a passing failure of the status check showed "正在确认" with a big 登录 button.
  const adapters = firstRunAdapters({ codex: { state: 'unknown' } });
  const partners = catalog.slice(1, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, selectable: true, installation: { state: 'found' }, account: null }));
  const ui = await browser({ agents: catalog.slice(1, 2), partners, route: setupRoute(() => ({ partners, supported_adapters: adapters })) });
  await ui.poll(); await ui.click('open-partners');
  const card = ui.get('native-tool-setup');
  assert.match(card.textContent, /登录状态没查到/);
  assert.match(card.textContent, /已经登录的不用重新登录/);
  const button = descendants(card).find((node) => node.dataset.nativeLoginStart === 'codex');
  assert.equal(button.textContent, '重新登录');
  assert.equal(button.classList.contains('setup-primary'), false, 'sign-in is a quiet choice here, not the main step');
  partners[0].account = accountFixture({ state: 'available' });
  await ui.poll(); await ui.click('open-partners');
  assert.equal(ui.get('native-tool-setup').hidden, true, 'a just-confirmed account hides the unread setup card');
});

test('a disabled partner is named with where to turn it back on, and a finished enable message goes away', async () => {
  // Catches A5: a disabled partner vanished from the home page and task options with only "停用设置保持不变",
  // and "伙伴已启用。已有聊天和成果保留。" stayed on its card forever.
  const partners = managedPartnersFixture(); partners[1].enabled = false; partners.forEach((partner) => { partner.lifecycle_busy = false; partner.lifecycle_can_change = true; });
  let operation = partnerOperationFixture('codex', 'enable', 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', { status: 'completed' });
  const envelope = () => ({ partners, supported_adapters: catalog.slice(0, 2), partner_lifecycle_operations: [operation] });
  const ui = await browser({ agents: catalog.slice(0, 1), partners, partnerLifecycleSupport: true, route(request) { if (request.path === '/api/partners') return envelope(); } });
  operation = partnerOperationFixture('codex', 'disable', 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', { status: 'completed' });
  await ui.poll();
  const expected = '已停用：Codex。要用的话，到“伙伴与额度”里点“启用”。';
  assert.equal(ui.get('home-disabled-note').textContent, expected); assert.equal(ui.get('home-disabled-note').hidden, false);
  assert.equal(ui.get('participant-disabled-note').textContent, expected);
  await ui.click('open-partners');
  assert.doesNotMatch(ui.get('partners-list').textContent, /伙伴已停用。已有聊天和成果保留。/, 'a settled operation is not repeated on the card');
  operation = partnerOperationFixture('codex', 'disable', 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', { status: 'completed', targets: [{ room_id: roomId, state: 'waiting_receiver' }] });
  await ui.poll(); await ui.click('open-partners');
  assert.match(ui.get('partners-list').textContent, /伙伴已停用。已有工作的改动已保留/, 'work still being handed on stays visible');
  partners[1].enabled = true;
  operation = partnerOperationFixture('codex', 'enable', 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee', { status: 'completed' });
  await ui.poll();
  assert.equal(ui.get('home-disabled-note').hidden, true);
});

test('partner replies render Markdown safely while the user text stays as typed', async () => {
  // Catches A6: tables and code blocks shown as raw Markdown in chat; and guards against HTML or unsafe links getting through.
  const reply = ['结论如下：', '', '| 名字 | 说明 |', '| --- | --- |', '| Tilly | 短 |', '', '```python', 'print("<b>hi</b>")', '```',
    '', '<img src=x onerror=alert(1)> [坏链接](javascript:alert(1)) [好链接](https://example.com/a) ![远程](https://example.com/x.png)'].join('\n');
  const saved = snapshot({ events: [message(1, 'user', '**我写的星号**'), message(2, 'codex', reply)] });
  const ui = await browser({ rooms: [saved.room], saved });
  const items = ui.get('events').children;
  const userText = descendants(items[0]).find((node) => node.classList.contains('event-text'));
  assert.equal(userText.textContent, '**我写的星号**');
  assert.equal(userText.children.length, 0);
  const text = descendants(items[1]).find((node) => node.classList.contains('event-text'));
  assert.ok(text.classList.contains('chat-markdown'));
  const nodes = descendants(text);
  assert.deepEqual(nodes.filter((node) => node.tagName === 'TH').map((node) => node.textContent), ['名字', '说明']);
  assert.equal(nodes.find((node) => node.tagName === 'PRE').textContent, 'print("<b>hi</b>")');
  assert.equal(nodes.some((node) => ['IMG', 'SCRIPT', 'B'].includes(node.tagName)), false, 'no raw HTML becomes an element');
  const links = nodes.filter((node) => node.tagName === 'A');
  assert.deepEqual(links.map((node) => node.href), ['https://example.com/a']);
  assert.equal(links[0].rel, 'noopener noreferrer');
  assert.match(text.textContent, /\[坏链接\]\(javascript:alert\(1\)\)/, 'an unsafe link stays visible as plain text');
  assert.match(text.textContent, /<img src=x onerror=alert\(1\)>/, 'HTML stays visible as text');
  assert.match(text.textContent, /图片：远程（不自动加载）/);
});

test('the read-only room hides the commit and push request, and a missing GitHub remote is final', async () => {
  // Catches B12: a plan room offered "请伙伴提交、合入主分支并推送", which would spend a round doing nothing;
  // and B13: "未找到可核对的 GitHub 远端" beside "主分支已合入，GitHub 尚未核对", as if a check were still coming.
  const data = taskCodeDeliveryFixture('unknown');
  data.delivery_summary.remote = { state: 'unknown', reason: 'github_remote_unavailable', checked_at: null };
  const open = async (workMode) => {
    const saved = withNaturalContinuation(completedSuggestionFixture()); saved.room.work_mode = workMode;
    return browser({ rooms: [saved.room], saved, codeStatusSupport: true, naturalContinuationSupport: true, naturalRuntimeSupport: true,
      route(request) { if (request.path.includes('/code-status')) return data; } });
  };
  const build = await open('build');
  assert.equal(build.get('code-merge-panel').hidden, false, 'a build room keeps the request');
  assert.equal(build.get('code-status-summary').textContent, '主分支已合入。这个仓库没有 GitHub 远端，不会推送，也不用核对。');
  assert.equal(build.get('code-delivery-remote').textContent, 'GitHub：没有 GitHub 远端，不会推送');
  const plan = await open('plan');
  assert.equal(plan.get('code-merge-panel').hidden, true);
  assert.equal(plan.posts().length, 0);
});

test('each partner card and each partner message name its role, the default one too', async () => {
  // Catches the role being hidden until someone changes it (or cut off in the narrow model button), so nobody sees who does what.
  const saved = snapshot({ agents: catalog.slice(0, 2), events: [message(1, 'claude', '我来写计划。')] });
  saved.room.runtime_settings = { permission: 'full_access', effective_permission: 'full_access',
    members: { claude: { model: null, effort: null, role: { id: 'planner' } }, codex: { model: null, effort: null } } };
  const ui = await browser({ agents: catalog.slice(0, 2), rooms: [saved.room], saved });
  assert.match(ui.get('member-workload-claude').textContent, /^角色：规划/);
  assert.match(ui.get('member-workload-codex').textContent, /^角色：全能/);
  assert.doesNotMatch(ui.get('member-model-claude').textContent, /规划/, 'the narrow card button keeps to the model');
  const roles = descendants(ui.get('events')).filter((node) => node.classList.contains('event-role'));
  assert.deepEqual(roles.map((node) => node.textContent), ['规划']);
});

test('results written into the project but not committed are not called merged', async () => {
  // Catches a Full Access delivery headline saying "主分支已合入……也不用核对" while the partners' new files sit untracked in the project.
  const data = taskCodeDeliveryFixture('unknown');
  data.delivery_summary.remote = { state: 'unknown', reason: 'github_remote_unavailable', checked_at: null };
  data.delivery_summary.state = 'pending'; data.delivery_summary.reason = 'published_work_not_on_mainline';
  data.delivery_summary.published = { state: 'pending', reason: 'published_work_not_on_mainline', files: { total: 6, matched: 0, missing: 6, different: 0 } };
  data.delivery_summary.workspace = { state: 'confirmed', reason: null, checks: [{ state: 'confirmed', reason: null, workspace_id: 'project', tracked: 0, untracked: 6 }], tracked: 0, untracked: 6 };
  const saved = withNaturalContinuation(completedSuggestionFixture()); saved.room.work_mode = 'build';
  const ui = await browser({ rooms: [saved.room], saved, codeStatusSupport: true, naturalContinuationSupport: true, naturalRuntimeSupport: true,
    route(request) { if (request.path.includes('/code-status')) return data; } });
  assert.equal(ui.get('code-status-summary').textContent, '这次的成果还没提交到主分支');
  assert.match(ui.get('code-delivery-workspace').textContent, /另有 6 个新文件还没纳入 Git/);
  assert.equal(ui.get('code-merge-panel').hidden, false, 'the request to commit and merge stays offered');
});

test('changing the workload shows a short saved note that clears itself', async () => {
  // Catches A23: switching 少/中/多 gave no sign it was saved.
  let clock = 1_000_000;
  const saved = snapshot({ agents: catalog.slice(0, 2) });
  const partners = [{ ...catalog[0], adapter: 'claude', enabled: true, selectable: true, workload_level: 'normal', account: claudeUsageAccount([]) },
    { ...catalog[1], adapter: 'codex', enabled: true, selectable: true, workload_level: 'normal' }];
  const ui = await browser({ now: () => clock, agents: catalog.slice(0, 2), partners, rooms: [saved.room], saved,
    route(request) { if (request.path === '/api/partners/claude/workload') { partners[0].workload_level = request.body.level; return { partners, supported_adapters: [] }; } } });
  const footer = () => ui.get('provider-account-claude');
  const status = () => descendants(footer()).find((node) => node.classList.contains('workload-status'));
  assert.equal(status().hidden, true);
  const select = descendants(footer()).find((node) => node.dataset?.focusKey === 'account-workload');
  select.value = 'high'; select.fire('change'); await settle();
  assert.equal(status().textContent, '已保存'); assert.equal(status().hidden, false);
  clock += 3500; await ui.runTimers();
  assert.equal(status().hidden, true, 'the note goes away by itself');
});

test('peer review wording is plain and says when nothing was run', async () => {
  // Catches A14/B14: "1 项里 0 项互审通过，1 项仅自检" and "检查：未执行验证" next to "2 项都已互审通过".
  const saved = objectiveSnapshot({ status: 'complete' });
  saved.team = { phase: 'ready_for_user', tasks: [{ id: 'a', title: '一', owner: 'claude', reviewer: 'codex', status: 'accepted', review_kind: 'independent_review' },
    { id: 'b', title: '二', owner: 'codex', reviewer: 'claude', status: 'accepted', review_kind: 'independent_review' }] };
  saved.collaboration = teamCollaboration({ state: 'ready_for_user', tasks: saved.team.tasks });
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.equal(ui.get('now-text').textContent, '2 项都有另一位伙伴看过并认可。检查是读内容，没有运行测试或程序。');
  assert.match(ui.get('collaboration-outcome').textContent, /检查范围：没有运行测试或程序/);
  saved.team.tasks = [{ id: 'c', title: '三', owner: 'claude', status: 'self_checked' }];
  saved.collaboration.tasks = saved.team.tasks;
  const single = await browser({ rooms: [saved.room], saved, executionPolicySupport: true });
  assert.equal(single.get('now-text').textContent, '1 项做完了，都只做了自查，没有别的伙伴看过。');
  assert.doesNotMatch(single.get('now-card').textContent, /互审|仅自检/);
});

test('account and usage facts use names people know', async () => {
  // Catches A14: "会员档位 max" and "来源 claude_cli" in the account and token panels.
  const saved = snapshot({ agents: catalog.slice(0, 2) });
  saved.members[0].usage = { source: 'claude_cli', scope: 'provider_reported_latest_result', values: { input_tokens: 12 } };
  const partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true, account: accountFixture({ plan: 'max' }) }));
  const ui = await browser({ rooms: [saved.room], saved, partners });
  assert.match(ui.get('provider-account-claude').textContent, /订阅Max/);
  assert.doesNotMatch(ui.get('provider-account-claude').textContent, /会员档位/);
  assert.match(ui.get('usage-claude').textContent, /数据来自Claude Code 回传的用量/);
  assert.doesNotMatch(ui.get('usage-claude').textContent, /claude_cli/);
});

test('a partner checking its own work is marked as a self-check, not addressed to itself', async () => {
  // Catches B15: "Kimi Code → Kimi Code" on a self-check message.
  const saved = snapshot({ events: [message(1, 'codex', '我自己核对了一遍。', 'codex'), message(2, 'codex', '给大家的说明。', 'all')] });
  const ui = await browser({ rooms: [saved.room], saved });
  const recipients = descendants(ui.get('events')).filter((node) => node.classList.contains('event-recipient')).map((node) => node.textContent);
  assert.deepEqual(recipients, ['自查', '→ 全体']);
  const css = fs.readFileSync(path.join(assetRoot, 'style.css'), 'utf8');
  assert.match(css, /\.public-progress-source \{ display: inline;/, 'the summary title stays on the line of its disclosure triangle');
});

test('after a plan the hint names the action that really exists', async () => {
  // Catches B16: an autonomous room told people to click "按方案开始", a button that only exists without an objective.
  const saved = withRuntimeConfiguration(objectiveSnapshot({ status: 'complete' }));
  saved.runtime_configuration.desired.work_mode = 'plan'; saved.room.work_mode = 'plan';
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalRuntimeSupport: true });
  ui.get('work-mode-plan').fire('click'); await settle();
  assert.match(ui.get('scope-description').textContent, /聊清楚后，切到“动手”再发一句话就会开工。$/);
  assert.doesNotMatch(ui.get('scope-description').textContent, /按方案开始/);
  saved.room.mode = 'native_project';
  saved.runtime_configuration.desired.runtime_settings.permission = 'plan'; saved.room.runtime_settings.permission = 'plan';
  const native = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalRuntimeSupport: true, nativeProjectSupport: true });
  native.get('work-mode-plan').fire('click'); await settle();
  assert.match(native.get('scope-description').textContent, /把权限从 Plan 换成 Full Access/);
  const fresh = await browser();
  fresh.get('work-mode-plan').fire('click'); await settle();
  assert.match(fresh.get('work-mode-note').textContent, /可以点“按方案开始”/, 'a host without objectives still has the button');
});

test('build mode under a read-only permission is not called a plan discussion', async () => {
  // Catches B17: after switching to 动手 the header and partner cards still said 方案讨论.
  const saved = withRuntimeConfiguration(objectiveSnapshot({ status: 'running' }));
  saved.room.mode = 'native_project'; saved.room.work_mode = 'build';
  saved.room.runtime_settings = { ...saved.room.runtime_settings, permission: 'plan', effective_permission: 'plan' };
  saved.collaboration = teamCollaboration({ state: 'in_progress' });
  for (const member of saved.members) member.session_id = '55555555-5555-4555-8555-555555555555';
  saved.team = { phase: 'working', tasks: [] };
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalRuntimeSupport: true, nativeProjectSupport: true });
  assert.equal(ui.get('room-status').textContent, '只读权限');
  assert.equal(ui.get('status-claude').textContent, '只读权限');
  assert.equal(ui.get('task-claude').textContent, '现在是只读权限，伙伴只能看不能改。要改文件，在“设置”里把权限换成 Full Access。');
  saved.room.work_mode = 'plan';
  const plan = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalRuntimeSupport: true, nativeProjectSupport: true });
  assert.equal(plan.get('room-status').textContent, '方案讨论');
});

test('management naming is one phrase, and room sub-records are not called task records', async () => {
  // Catches A8: 伙伴与额度 / 工具与状态 / 工具与会话 for one page, and 任务记录 used for both rooms and a room's processing log.
  const ui = await browser();
  await ui.click('open-partners');
  assert.equal(ui.get('manager-title').textContent, '伙伴与额度');
  assert.doesNotMatch(html, /工具与状态|工具与会话/);
  assert.doesNotMatch(script, /工具与状态|工具与会话/);
  assert.match(html, /<summary><span data-i18n="处理明细">处理明细<\/span> <span id="job-count">/);
});

test('layout fixes stay in the stylesheet', async () => {
  // Catches A15 (task options backdrop painted opaque), A16 (18px "添加项目"), A17 (quota label wrapping),
  // A20 (10px dropdowns and captions bigger than titles), A21 (light plates in dark mode) and A22 (empty note at the sidebar foot).
  const css = fs.readFileSync(path.join(assetRoot, 'style.css'), 'utf8');
  const rule = (selector) => css.split('\n').filter((line) => line.startsWith(`${selector} {`) || line.includes(`, ${selector} {`) || line.startsWith(`${selector},`));
  assert.doesNotMatch(css, /\.task-options-popover \{ background/);
  assert.doesNotMatch(css, /\.entry-context-menu, \.task-options-popover \{/);
  assert.doesNotMatch(css, /\.send-button > span:last-child/);
  assert.match(css, /\.send-button > span\[aria-hidden="true"\] \{ font-size: 18px;/);
  assert.match(css, /\.quota-row \{ display: grid; grid-template-columns: 4\.2em /);
  assert.match(css, /\.quota-row > span:first-child \{ white-space: nowrap;/);
  assert.match(css, /\.runtime-settings-popover select \{[^}]*font-size: 12px;/);
  assert.match(css, /\.quota-window-heading \{[^}]*font-size: 13px;/);
  assert.match(css, /\.quota-window \.detail-caption \{[^}]*font-size: 11\.5px;/);
  assert.match(css, /\.jobs-details, \.project-details, \.activity-details \{[^}]*font-size: 12\.5px;/);
  assert.match(css, /\.empty-avatars::before \{[^}]*background: var\(--art-plate\);/);
  assert.match(css, /\.bean-style-card \{[^}]*background: var\(--pick-bg\);/);
  assert.equal((css.match(/--art-plate: #1f251c;/g) || []).length, 2, 'both dark theme blocks set the plate');
  assert.match(css, /\.sidebar:has\(#room-list-empty:not\(\[hidden\]\)\) \.room-list \{ flex: 0 0 auto; \}/);
  assert.ok(rule('.participant-availability').some((line) => /color: var\(--muted\)/.test(line)), 'an ordinary quota line is not orange');
});

test('a finished plan discussion has one name on the card, in the list and on partner cards', async () => {
  // Catches A3: after a read-only plan was delivered the card said 目标已完成, the list 目标已交付 and the partner card 讨论已结束.
  const saved = withRuntimeConfiguration(objectiveSnapshot({ status: 'complete' }));
  saved.room.work_mode = 'plan'; saved.runtime_configuration.desired.work_mode = 'plan'; saved.room.runtime_settings.effective_permission = 'read_only';
  saved.room.objective_status = 'complete'; saved.team = { phase: 'ready_for_user', tasks: [] };
  for (const member of saved.members) member.session_id = '55555555-5555-4555-8555-555555555555';
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, naturalRuntimeSupport: true });
  assert.equal(ui.get('now-title').textContent, '讨论已结束');
  assert.match(ui.get('now-text').textContent, /要开工，切到“动手”再发一句话。$/);
  assert.match(ui.get('rooms').children[0].textContent, /讨论已结束/);
  assert.equal(ui.get('status-claude').textContent, '讨论已结束');
  assert.equal(ui.document.getElementById('pause-objective')?.hidden ?? true, true, 'nothing to pause after the discussion ended');
  await ui.click('new-room');
  assert.match(ui.get('rooms').children[0].textContent, /讨论已结束/, 'the list keeps the name when the room is not open');
});

// Project rename: the name is edited in place on the project card.
function projectCard(ui, id) {
  const card = ui.get('projects-list').children.find((node) => node.dataset.projectId === id);
  assert.ok(card, `missing project card ${id}`); return card;
}
function renameButton(root, id) {
  const button = descendants(root).find((node) => node.tagName === 'BUTTON' && node.dataset.projectRename === id);
  assert.ok(button, `missing rename button ${id}`); return button;
}
function renameInput(ui, id) { return descendants(ui.get('projects-list')).find((node) => node.dataset.projectRenameInput === id) || null; }
function renameAction(ui, action) {
  const button = descendants(ui.get('projects-list')).find((node) => node.dataset.projectRenameAction === action);
  assert.ok(button, `missing rename ${action}`); return button;
}
function typeRename(ui, id, text) { const input = renameInput(ui, id); input.value = text; input.fire('input'); }

test('rename is offered on the card and in the context menu and edits the name in place', async () => {
  // Catches a missing rename entry, or one that opens a native prompt instead of editing on the card.
  // The test window has no prompt/confirm, so calling one would throw here.
  const project = registeredProject('a1a1a1a1-a1a1-4a1a-8a1a-a1a1a1a1a1a1', '原来的名字');
  const archived = registeredProject('a2a2a2a2-a2a2-4a2a-8a2a-a2a2a2a2a2a2', '归档的项目', 'archived');
  const ui = await browser({ projects: [project, archived] });
  await ui.click('open-projects');
  const card = projectCard(ui, project.id);
  assert.equal(renameButton(card, project.id).textContent, '改名');
  assert.equal(renameInput(ui, project.id), null);
  renameButton(card, project.id).fire('click'); await settle();
  const input = renameInput(ui, project.id);
  assert.ok(input, 'the card now holds an input');
  assert.equal(input.value, '原来的名字');
  assert.equal(ui.document.activeElement, input);
  assert.deepEqual([input.selectionStart, input.selectionEnd], [0, 5], 'the old name is selected for typing over');
  assert.equal(descendants(projectCard(ui, project.id)).some((node) => node.tagName === 'H3'), false);
  assert.equal(renameButton(projectCard(ui, project.id), project.id).disabled, true, 'no second editor for the same card');
  assert.equal(renameAction(ui, 'save').textContent, '保存');
  assert.equal(renameAction(ui, 'cancel').textContent, '取消');
  renameAction(ui, 'cancel').fire('click'); await settle();
  // The context menu offers the same entry.
  projectCard(ui, project.id).fire('contextmenu'); await settle();
  renameButton(ui.get('entry-context-actions'), project.id).fire('click'); await settle();
  assert.equal(ui.get('entry-context-menu').hidden, true);
  assert.equal(renameInput(ui, project.id).value, '原来的名字');
  assert.equal(ui.posts().length, 0, 'opening the editor sends nothing');
  // Archived projects can be renamed from the archived view too.
  renameAction(ui, 'cancel').fire('click'); await settle();
  selectLifecycleView(ui, 'projects', 'archived');
  renameButton(projectCard(ui, archived.id), archived.id).fire('click'); await settle();
  assert.equal(renameInput(ui, archived.id).value, '归档的项目');
});

test('saving a rename posts only the trimmed name and every place shows the new name', async () => {
  // Catches a wrong request body, and a list, project picker, task options or room header
  // that keeps the old name after a successful rename.
  const id = 'b1b1b1b1-b1b1-4b1b-8b1b-b1b1b1b1b1b1';
  const saved = objectiveSnapshot({ status: 'complete' }); saved.room.mode = 'project_files'; saved.room.project_id = id;
  saved.project = { available: true, revision: 1, files: [], source: { id, name: '旧账本', path: '/fictional/旧账本' } };
  const projects = [registeredProject(id, '旧账本')];
  const ui = await browser({ rooms: [saved.room], saved, projects, executionPolicySupport: true, route(request) {
    if (request.path === `/api/projects/${id}/name`) {
      projects[0] = { ...projects[0], name: request.body.name };
      return { project: projects[0], projects };
    }
    if (request.path === '/api/projects' && request.method === 'POST') return { projects };
  } });
  assert.equal(ui.get('room-project-chip').textContent, '旧账本');
  await ui.click('open-projects');
  renameButton(projectCard(ui, id), id).fire('click'); await settle();
  typeRename(ui, id, '  家庭账本  ');
  let prevented = false;
  renameInput(ui, id).fire('keydown', { key: 'Enter', preventDefault() { prevented = true; } }); await settle();
  assert.equal(prevented, true);
  assert.deepEqual(ui.posts().map((request) => [request.path, request.body]), [[`/api/projects/${id}/name`, { name: '家庭账本' }]]);
  assert.equal(renameInput(ui, id), null, 'the editor closes after saving');
  assert.equal(descendants(projectCard(ui, id)).find((node) => node.tagName === 'H3').textContent, '家庭账本');
  assert.equal(ui.get('manager-notice').textContent, '已改名为「家庭账本」。');
  assert.deepEqual(ui.get('project-picker').children.map((node) => node.textContent), ['不关联项目 · 自由协作', '家庭账本']);
  assert.equal(ui.get('room-project-chip').textContent, '家庭账本');
  assert.equal(ui.get('task-options-project').textContent, '家庭账本');
  assert.equal(ui.get('frozen-project').textContent, '关联项目：家庭账本 · 本任务项目固定。');
  // Registering the same folder again names the project by its new name.
  ui.get('project-name').value = '再加一次'; ui.get('project-path').value = '/fictional/旧账本/';
  ui.get('project-form').fire('submit'); await settle();
  assert.equal(ui.get('manager-notice').textContent, '这个文件夹已登记为「家庭账本」，直接选它就行。');
  // A second rename works the same way.
  renameButton(projectCard(ui, id), id).fire('click'); await settle();
  typeRename(ui, id, '账本'); renameAction(ui, 'save').fire('click'); await settle();
  assert.deepEqual(ui.posts().at(-1).body, { name: '账本' });
  assert.equal(ui.get('room-project-chip').textContent, '账本');
  assert.equal(ui.posts().length, 2);
});

test('an empty or too long name is stopped on the card and a rejected name keeps the editor open', async () => {
  // Catches sending a blank or over-long name, and losing the typed text when the service refuses it.
  const id = 'c1c1c1c1-c1c1-4c1c-8c1c-c1c1c1c1c1c1';
  const projects = [registeredProject(id, '小项目')];
  const ui = await browser({ projects, route(request) {
    if (request.path === `/api/projects/${id}/name`) return { error: 'invalid_project_request' };
  } });
  await ui.click('open-projects');
  renameButton(projectCard(ui, id), id).fire('click'); await settle();
  const error = () => descendants(ui.get('projects-list')).find((node) => node.className === 'project-rename-error')?.textContent || '';
  typeRename(ui, id, '   '); renameAction(ui, 'save').fire('click'); await settle();
  assert.equal(error(), '名字不能为空。');
  assert.equal(renameInput(ui, id).getAttribute('aria-invalid'), 'true');
  assert.equal(ui.document.activeElement, renameInput(ui, id));
  typeRename(ui, id, '长'.repeat(81)); assert.equal(error(), '', 'typing clears the hint');
  renameInput(ui, id).fire('keydown', { key: 'Enter' }); await settle();
  assert.equal(error(), '名字太长了，最多 80 个字。');
  assert.equal(ui.posts().length, 0, 'nothing is sent for a name the service would reject');
  typeRename(ui, id, '含密钥的名字'); renameAction(ui, 'save').fire('click'); await settle();
  assert.equal(ui.posts().length, 1);
  assert.equal(error(), '这个名字不能用。名字里不能有换行这类控制字符，也不能像密钥或令牌。');
  assert.equal(renameInput(ui, id).value, '含密钥的名字', 'the typed text is kept');
  assert.equal(descendants(ui.get('projects-list')).some((node) => node.tagName === 'H3' && node.textContent === '含密钥的名字'), false);
});

test('Esc or cancel leaves the name unchanged and sends nothing', async () => {
  // Catches Esc saving the edit, closing the whole manager, or leaving the typed name on the card.
  const id = 'd1d1d1d1-d1d1-4d1d-8d1d-d1d1d1d1d1d1';
  const ui = await browser({ projects: [registeredProject(id, '不变的名字')] });
  await ui.click('open-projects');
  renameButton(projectCard(ui, id), id).fire('click'); await settle();
  typeRename(ui, id, '打了一半');
  let prevented = false;
  renameInput(ui, id).fire('keydown', { key: 'Escape', preventDefault() { prevented = true; } }); await settle();
  assert.equal(prevented, true, 'Esc is consumed so the manager stays open');
  assert.equal(renameInput(ui, id), null);
  assert.equal(descendants(projectCard(ui, id)).find((node) => node.tagName === 'H3').textContent, '不变的名字');
  assert.equal(ui.document.activeElement, projectCard(ui, id));
  // Reopening starts from the saved name, not the abandoned text.
  renameButton(projectCard(ui, id), id).fire('click'); await settle();
  assert.equal(renameInput(ui, id).value, '不变的名字');
  renameAction(ui, 'save').fire('click'); await settle();
  assert.equal(renameInput(ui, id), null, 'saving the same name just closes the editor');
  assert.equal(ui.posts().length, 0);
});

test('the English interface labels the rename controls in English', async () => {
  // Catches rename copy missing from the English catalogue.
  const id = 'e1e1e1e1-e1e1-4e1e-8e1e-e1e1e1e1e1e1';
  const ui = await browser({ language: 'en-US', projects: [registeredProject(id, 'Ledger')] });
  await ui.click('open-projects');
  const button = renameButton(projectCard(ui, id), id);
  assert.equal(button.textContent, 'Rename');
  assert.equal(button.getAttribute('aria-label'), 'Rename project: Ledger');
  button.fire('click'); await settle();
  assert.equal(renameInput(ui, id).getAttribute('aria-label'), 'New name for Ledger');
  typeRename(ui, id, ''); renameAction(ui, 'save').fire('click'); await settle();
  assert.match(ui.get('projects-list').textContent, /The name can't be empty\./);
});

function withdrawalFixture() {
  const saved = objectiveSnapshot({ status: 'running' });
  saved.room.status = 'running';
  saved.team = { phase: 'working', plan_author: 'claude', revision: 1, run_id: saved.current_run.id, stop_reason: null, members: [], tasks: [
    { id: 'T1', title: '写入口', owner: 'claude', status: 'working', call_running: true, dependencies: [] },
    { id: 'T2', title: '写说明', owner: 'codex', status: 'assigned', dependencies: [] },
    { id: 'T3', title: '补测试', owner: 'codex', status: 'assigned', dependencies: ['T2'] },
    { id: 'T4', title: '已做完的一项', owner: 'claude', status: 'accepted', review_kind: 'independent_review', dependencies: [] }] };
  saved.collaboration.tasks = saved.team.tasks.map(({ id, title, owner, status }) => ({ id, title, owner, status, waiting_for: '', result: '' }));
  return saved;
}
const withdrawButtons = (ui) => descendants(ui.get('progress-pane')).filter((node) => node.tagName === 'BUTTON' && node.dataset.withdrawTask);

test('withdrawing a task asks inside the page, lists what depends on it from the service, and sends only after the user agrees', async () => {
  // Catches a browser confirm() (the fake page has none, so calling it throws), a click that sends at once,
  // dependents withdrawn without being shown, a running task offered for withdrawal, and finished work offered at all.
  const saved = withdrawalFixture();
  const after = structuredClone(saved);
  for (const task of after.team.tasks) if (['T2', 'T3'].includes(task.id)) task.status = 'withdrawn';
  after.collaboration.tasks = after.team.tasks.map(({ id, title, owner, status }) => ({ id, title, owner, status, waiting_for: '', result: '' }));
  after.events = [...after.events, { seq: 91, kind: 'task_withdrawn', sender: 'user', run_id: after.current_run.id, task_id: 'T2', title: '写说明',
    tasks: [{ id: 'T2', title: '写说明' }, { id: 'T3', title: '补测试' }], created_at: 1091 }];
  const sent = [];
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, taskWithdrawalSupport: true, route(request) {
    if (!request.path.endsWith('/withdraw-task')) return undefined;
    sent.push(request.body);
    if (!request.body.include_dependents) return { error: 'task_withdraw_dependents', task_id: 'T2', dependents: [{ id: 'T3', title: '补测试' }] };
    Object.assign(saved, structuredClone(after)); return after;
  } });
  await ui.click('tab-progress');
  const buttons = withdrawButtons(ui);
  assert.deepEqual(buttons.map((node) => node.dataset.withdrawTask), ['T1', 'T2', 'T3'], 'finished work has no withdraw action');
  const running = buttons[0];
  assert.equal(running.disabled, true);
  assert.match(ui.get('progress-pane').textContent, /伙伴正在处理，这一步结束后才能撤/);
  running.fire('click'); await settle();
  assert.equal(ui.get('task-withdraw-confirm').hidden, true, 'a running task cannot even open the dialog');

  buttons[1].fire('click'); await settle();
  assert.equal(ui.get('task-withdraw-confirm').hidden, false);
  assert.match(ui.get('task-withdraw-title').textContent, /撤掉「写说明」？/);
  assert.equal(ui.posts().length, 0, 'opening the dialog sends nothing');
  await ui.click('task-withdraw-cancel');
  assert.equal(ui.get('task-withdraw-confirm').hidden, true);
  assert.equal(ui.posts().length, 0, 'keeping the task sends nothing');

  withdrawButtons(ui)[1].fire('click'); await settle();
  await ui.click('task-withdraw-submit');
  assert.equal(sent.length, 1);
  assert.deepEqual(Object.keys(sent[0]).sort(), ['request_id', 'run_id', 'task_id']);
  assert.equal(sent[0].run_id, saved.current_run.id); assert.equal(sent[0].task_id, 'T2');
  assert.equal(ui.get('task-withdraw-confirm').hidden, false, 'the dependents are shown before anything more is withdrawn');
  assert.equal(ui.get('task-withdraw-dependents').hidden, false);
  assert.equal(ui.get('task-withdraw-dependents').textContent, 'T3「补测试」');
  assert.equal(ui.get('task-withdraw-submit').textContent, '一起撤掉这 2 项');
  assert.equal(ui.get('task-withdraw-error').hidden, true);

  await ui.click('task-withdraw-submit');
  assert.equal(sent.length, 2);
  assert.equal(sent[1].include_dependents, true); assert.equal(sent[1].task_id, 'T2');
  assert.notEqual(sent[1].request_id, sent[0].request_id);
  assert.equal(ui.get('task-withdraw-confirm').hidden, true);
  const pane = ui.get('progress-pane').textContent;
  assert.match(pane, /1 \/ 2 完成/, 'withdrawn tasks leave the total');
  assert.match(pane, /2 项已撤掉/);
  assert.deepEqual(withdrawButtons(ui).map((node) => node.dataset.withdrawTask), ['T1'], 'withdrawn tasks offer nothing more');
  assert.match(ui.get('notice').textContent, /已撤掉「写说明」和依赖它的 1 项。/);
  await ui.click('tab-discussion');
  assert.match(ui.get('events').textContent, /你撤掉了 T2「写说明」，以及依赖它的 T3「补测试」/);
});

test('a refused withdrawal keeps the dialog open with the service reason, and an older service shows no action', async () => {
  // Catches a busy/finished refusal closing the dialog silently, and buttons offered by a service that cannot withdraw.
  const saved = withdrawalFixture();
  const ui = await browser({ rooms: [saved.room], saved, executionPolicySupport: true, taskWithdrawalSupport: true, route(request) {
    if (request.path.endsWith('/withdraw-task')) return { error: 'task_withdraw_busy' };
  } });
  await ui.click('tab-progress');
  withdrawButtons(ui)[2].fire('click'); await settle();
  await ui.click('task-withdraw-submit');
  assert.equal(ui.get('task-withdraw-confirm').hidden, false);
  assert.equal(ui.get('task-withdraw-error').hidden, false);
  assert.equal(ui.get('task-withdraw-error').textContent, '伙伴正在处理，这一步结束后才能撤。');
  assert.equal(ui.get('task-withdraw-submit').disabled, false, 'the user can try again once the step ends');
  ui.document.root.fire('keydown', { key: 'Escape', preventDefault() {} }); await settle();
  assert.equal(ui.get('task-withdraw-confirm').hidden, true, 'Esc closes the dialog');

  const old = await browser({ rooms: [saved.room], saved: withdrawalFixture(), executionPolicySupport: true });
  await old.click('tab-progress');
  assert.deepEqual(withdrawButtons(old), []);
});

test('the English interface words withdrawal in English, in the board, the dialog and the timeline', async () => {
  // Catches withdrawal copy missing from the English catalogue.
  const saved = withdrawalFixture();
  saved.team.tasks[3] = { ...saved.team.tasks[3], status: 'withdrawn' };
  saved.events = [...saved.events, { seq: 91, kind: 'task_withdrawn', sender: 'user', run_id: saved.current_run.id, task_id: 'T4', title: 'Old notes',
    tasks: [{ id: 'T4', title: 'Old notes' }], created_at: 1091 }];
  const ui = await browser({ language: 'en-US', rooms: [saved.room], saved, executionPolicySupport: true, taskWithdrawalSupport: true });
  await ui.click('tab-progress');
  const pane = ui.get('progress-pane').textContent;
  assert.match(pane, /1 withdrawn/); assert.match(pane, /Withdrawn/);
  assert.match(pane, /A partner is working on it; you can withdraw it once this step ends/);
  const button = withdrawButtons(ui)[1];
  assert.equal(button.textContent, 'Withdraw');
  button.fire('click'); await settle();
  assert.equal(ui.get('task-withdraw-title').textContent, 'Withdraw “写说明”?');
  assert.doesNotMatch(ui.get('task-withdraw-description').textContent, /[一-龥]/);
  await ui.click('tab-discussion');
  assert.match(ui.get('events').textContent, /You withdrew T4 “Old notes”/);
});

function apiPartnerFixture(overrides = {}) {
  const prices = { input: 0.66, output: 1.98, cache_read: 0.022, cache_write: 0.66 };
  return { id: 'ds', name: 'DeepSeek', avatar: 'D', adapter: 'claude', provider: 'api_endpoint', connection_state: 'api_model',
    enabled: true, builtin: false, lifecycle_state: 'active', selectable: true, installation: { state: 'found' },
    account: { state: 'ready', source: 'api_endpoint', auth_method: 'api_key', endpoint: 'api.deepseek.com', checked_at: 1000, stale: false },
    availability: { state: 'available', selectable: true, reason: null, source: 'api_spend_ledger', observed_at: 1000, stale: false, scope: 'api_endpoint' },
    api_model: { host: 'claude', host_name: 'Claude Code', base_url: 'https://api.deepseek.com/anthropic', currency: 'USD', timezone: 'UTC',
      key: { kind: 'env_file', path: '/Users/fictional/keys.env', variable: 'DEEPSEEK_API_KEY' }, default_model: 'deepseek-v4-pro',
      peak_windows: [{ start: '01:00', end: '04:00', days: [0, 1, 2, 3, 4] }],
      models: [{ id: 'deepseek-v4-pro', ...prices, peak: { input: 1.32, output: 3.96, cache_read: 0.044, cache_write: 1.32 } }],
      caps: { daily: 1, monthly: null } },
    spend: { currency: 'USD', day: '2027-01-16', day_spent: 0.66, month: '2027-01', month_spent: 0.66, month_calls: 1, month_unpriced_calls: 0,
      caps: { daily: 1, monthly: null }, capped: [], left_percent: 34, state: 'available' }, quota_reserve: null, ...overrides };
}
const apiPresetsFixture = [
  { id: 'deepseek', name: 'DeepSeek', host: 'claude', base_url: 'https://api.deepseek.com/anthropic', currency: 'USD', timezone: 'UTC',
    key: { kind: 'env_file', path: '', variable: 'DEEPSEEK_API_KEY' }, default_model: 'deepseek-v4-pro',
    peak_windows: [{ start: '01:00', end: '04:00', days: [0, 1, 2, 3, 4] }, { start: '06:00', end: '10:00', days: [0, 1, 2, 3, 4] }],
    models: [{ id: 'deepseek-v4-pro', input: 0.66, output: 1.98, cache_read: 0.022, cache_write: 0.66, peak: { input: 1.32, output: 3.96, cache_read: 0.044, cache_write: 1.32 } },
      { id: 'deepseek-flash', input: 0.15, output: 0.6, cache_read: 0.003, cache_write: 0.15 }],
    caps: { daily: 5, monthly: 50 } },
  { id: 'custom_anthropic', name: '自定义 Anthropic 兼容接口', host: 'claude', base_url: '', currency: 'USD', timezone: 'UTC',
    key: { kind: 'env_file', path: '', variable: '' }, default_model: '', peak_windows: [], models: [], caps: { daily: null, monthly: null } },
];
const apiCatalog = [catalog[0], { id: 'ds', name: 'DeepSeek', avatar: 'D', adapter: 'claude', provider: 'api_endpoint' }];

test('an API partner card shows spend against its cap, needs no Claude login, and is not hidden as an extra session', async () => {
  // Catches the card showing an empty quota, an API partner blocked by a missing Claude sign-in, or it being filed under 更多会话.
  const partners = [{ ...catalog[0], adapter: 'claude', enabled: true }, apiPartnerFixture()];
  const saved = snapshot({ agents: apiCatalog });
  const ui = await browser({ agents: apiCatalog, rooms: [saved.room], saved, partners, route(request) {
    if (request.path === '/api/partners' && request.method === 'GET') return { partners, api_model_presets: apiPresetsFixture,
      supported_adapters: [{ id: 'claude', name: 'Claude Code', setup: { state: 'login_required' } }, { id: 'codex', name: 'Codex' }] };
  } });
  await settle();
  assert.match(ui.get('member-quota-ds').textContent, /今天.*US\$0\.66 \/ US\$1\.00/);
  assert.deepEqual(descendants(ui.get('member-quota-ds')).filter((node) => node.tagName === 'PROGRESS').map((node) => node.value), [34]);
  await ui.click('open-partners');
  const card = ui.get('partners-list').children.find((node) => node.dataset.partner === 'ds');
  assert.ok(card, 'listed with the main partners, not under the extra sessions');
  assert.match(card.textContent, /API 接口：api\.deepseek\.com/);
  assert.doesNotMatch(card.textContent, /还差登录/);
  const footer = ui.get('manager-provider-account-ds'); footer.open = true;
  assert.match(footer.textContent, /今天已花US\$0\.66 \/ US\$1\.00/);
  assert.match(footer.textContent, /\/Users\/fictional\/keys\.env 里的 DEEPSEEK_API_KEY/);
  assert.match(footer.textContent, /高峰 输入 US\$1\.32/);
  assert.equal(descendants(footer).some((node) => node.dataset.focusKey === 'account-refresh'), false, 'no subscription account refresh');
});

test('a capped API partner waits like a used-up quota instead of asking the user to act', async () => {
  // Catches a reached cap reading as a status in doubt, which outlines the card and asks for a refresh that cannot help.
  const capped = apiPartnerFixture({ availability: { state: 'blocked', selectable: false, reason: 'spend_cap_reached', source: 'api_spend_ledger', observed_at: 1000, stale: false, scope: 'api_endpoint' },
    spend: { ...apiPartnerFixture().spend, day_spent: 1.32, capped: ['daily'], left_percent: 0, state: 'capped' } });
  const partners = [{ ...catalog[0], adapter: 'claude', enabled: true }, capped];
  const saved = snapshot({ agents: apiCatalog });
  const ui = await browser({ agents: apiCatalog, rooms: [saved.room], saved, partners });
  await settle();
  assert.match(ui.get('availability-ds').textContent, /已到花费上限/);
  assert.equal(ui.get('availability-ds').classList.contains('waiting'), true);
  assert.equal(ui.get('member-quota-ds').parentNode.classList.contains('quota-blocked'), false);
});

test('adding a DeepSeek preset sends the host tool, the editable prices and only the key location', async () => {
  // Catches the preset sending its own name as the adapter, dropping the price table, or saving without a key location.
  let partners = catalog.slice(0, 2).map((agent) => ({ ...agent, adapter: agent.id, enabled: true }));
  const response = () => ({ partners, api_model_presets: apiPresetsFixture, supported_adapters: [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }] });
  const ui = await browser({ agents: catalog.slice(0, 2), route(request) {
    if (request.path === '/api/partners' && request.method === 'GET') return response();
    if (request.path === '/api/partners' && request.method === 'POST') { partners = [...partners, { ...request.body, connection_state: 'api_model' }]; return response(); }
  } });
  await ui.click('open-partners');
  ui.get('partner-adapter').value = 'api:deepseek'; ui.get('partner-adapter').fire('change');
  assert.equal(ui.get('partner-api').hidden, false);
  assert.equal(ui.get('api-base-url').value, 'https://api.deepseek.com/anthropic');
  assert.equal(ui.get('api-peak').value, '01:00-04:00 1-5\n06:00-10:00 1-5');
  ui.get('partner-form').fire('submit'); await settle();
  assert.match(ui.get('manager-notice').textContent, /完整路径和变量名/, 'no key location, nothing sent');
  assert.equal(ui.posts().filter((request) => request.path === '/api/partners').length, 0);
  ui.get('api-key-path').value = '/Users/fictional/keys.env'; ui.get('api-cap-daily').value = '2.5'; ui.get('api-cap-monthly').value = '';
  ui.get('partner-form').fire('submit'); await settle();
  const sent = ui.posts().filter((request) => request.path === '/api/partners');
  assert.equal(sent.length, 1, 'the filled form is sent once');
  const body = sent[0].body;
  assert.equal(body.adapter, 'claude');
  assert.match(body.id, /^deepseek-[a-z0-9]+$/);
  assert.deepEqual(body.api_model.key, { kind: 'env_file', path: '/Users/fictional/keys.env', variable: 'DEEPSEEK_API_KEY' });
  assert.deepEqual(body.api_model.caps, { daily: 2.5, monthly: null });
  assert.deepEqual(body.api_model.models.map((model) => [model.id, model.input, !!model.peak]), [['deepseek-v4-pro', 0.66, true], ['deepseek-flash', 0.15, false]]);
  assert.deepEqual(body.api_model.peak_windows[1], { start: '06:00', end: '10:00', days: [0, 1, 2, 3, 4] });
  assert.equal(body.api_model.default_model, 'deepseek-v4-pro');
});
