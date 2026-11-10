'use strict';

// First-run guidance, native tool setup, and partner appearance.

function nativeLoginPath(id) { return `/api/native-adapters/${encodeURIComponent(id)}/login`; }
function activeNativeLogin(operation) { return ['starting', 'waiting'].includes(operation?.state); }
function checkedNativeLogin(operation, id) {
  if (operation === null) return null;
  if (!operation || operation.adapter !== id || typeof operation.id !== 'string' || !operation.id || operation.id.length > 128
      || !['starting', 'waiting', 'completed', 'failed', 'cancelled'].includes(operation.state)) throw new RequestError(t("登录状态暂时无法确认。"));
  return operation;
}
function acceptNativeLogin(id, operation) {
  const previous = state.nativeLogins.get(id);
  if (previous?.id === operation?.id && !activeNativeLogin(previous) && activeNativeLogin(operation)) return;
  state.nativeLogins.set(id, operation);
}
function nativeSetupLink(text, url, reportFailure = (message) => managerNotice(message, true)) {
  // Official destinations are verified by the host's adapter; reject unsafe URL schemes here.
  if (typeof url !== 'string' || !/^https:\/\/[a-z0-9.-]+(?:\/[^\s<>]*)?$/i.test(url)) return null;
  const link = element('a', text, 'quiet-button'); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
  link.addEventListener('click', (event) => {
    if (typeof window.beanestDesktop?.openWebPage !== 'function') return;
    event.preventDefault();
    void Promise.resolve().then(() => window.beanestDesktop.openWebPage(url)).then((opened) => {
      if (opened !== true) reportFailure(t("网页未打开，可以复制卡片中的官方地址到浏览器。"));
    }).catch(() => reportFailure(t("网页未打开，可以复制卡片中的官方地址到浏览器。")));
  });
  return link;
}

async function updateNativeLogin(id, method = 'GET') {
  if (!state.connected || state.nativeLoginBusy.has(id)) return;
  const existing = state.nativeLogins.get(id) || state.adapters.find((adapter) => adapter.id === id)?.setup?.login_operation;
  state.nativeLoginBusy.add(id); state.nativeLoginErrors.delete(id); renderNativeSetup();
  try {
    const body = method === 'DELETE' ? { operation_id: existing?.id } : method === 'POST' ? {} : undefined;
    const response = await api(nativeLoginPath(id), method, body);
    const operation = checkedNativeLogin(response.operation, id);
    acceptNativeLogin(id, operation);
    if (operation?.state === 'completed') { state.nativeLoginBusy.delete(id); await refreshAfterSetup(); }
  } catch (error) {
    state.nativeLoginErrors.set(id, method === 'POST' ? t("登录请求尚未确认，正在核对已有登录状态；不会重复启动。") : error.message || t("登录状态暂时无法确认。"));
    // A lost start response is resolved by a read, never by replaying login.
    if (method === 'POST') state.nativeLogins.set(id, { id: '', adapter: id, state: 'starting' });
  } finally {
    state.nativeLoginBusy.delete(id); renderNativeSetup();
  }
}
function syncNativeLoginTimer() {
  if (state.nativeLoginTimer) clearTimeout(state.nativeLoginTimer);
  state.nativeLoginTimer = null;
  if (!state.connected || (state.manager !== 'partners' && !firstRunVisible())) return;
  const ready = state.adapters.filter((adapter) => adapter.setup && !state.nativeLoginBusy.has(adapter.id));
  const logins = ready.filter((adapter) => adapter.setup.login_supported === true && activeNativeLogin(nativeLoginOf(adapter)));
  const installs = ready.filter((adapter) => activeNativeInstall(nativeInstallOf(adapter)));
  if (!logins.length && !installs.length) return;
  state.nativeLoginTimer = setTimeout(() => {
    state.nativeLoginTimer = null;
    for (const adapter of logins) void updateNativeLogin(adapter.id);
    for (const adapter of installs) void updateNativeInstall(adapter.id);
  }, 1500);
}
function activeNativeInstall(operation) { return operation?.state === 'starting'; }
function nativeInstallOf(adapter) { return state.nativeInstalls.has(adapter.id) ? state.nativeInstalls.get(adapter.id) : adapter.setup.install_operation; }
function nativeLoginOf(adapter) { return state.nativeLogins.has(adapter.id) ? state.nativeLogins.get(adapter.id) : adapter.setup.login_operation; }
async function refreshAfterSetup() {
  await refreshManager();
  await discoverOnOpen({ reopen: true, manual: true });
  renderNativeSetup();
}
async function updateNativeInstall(id, method = 'GET') {
  if (!state.connected || state.nativeLoginBusy.has(id)) return;
  const adapter = state.adapters.find((item) => item.id === id);
  const existing = adapter && nativeInstallOf(adapter);
  state.nativeLoginBusy.add(id); state.nativeLoginErrors.delete(id); renderNativeSetup();
  try {
    const body = method === 'DELETE' ? { operation_id: existing?.id } : method === 'POST' ? {} : undefined;
    const response = await api(`/api/native-adapters/${encodeURIComponent(id)}/install`, method, body);
    const operation = response.operation;
    state.nativeInstalls.set(id, operation);
    if (operation?.state === 'completed') { state.nativeLoginBusy.delete(id); await refreshAfterSetup(); }
  } catch (error) {
    state.nativeLoginErrors.set(id, method === 'POST' ? t("安装请求没有确认，正在核对；不会重复安装。") : error.message || t("安装状态暂时无法确认。"));
    if (method === 'POST') state.nativeInstalls.set(id, { id: '', adapter: id, state: 'starting' });
  } finally {
    state.nativeLoginBusy.delete(id); renderNativeSetup();
  }
}
function nativeInstallFailure(reason) {
  return ({ download_failed: t("下载失败，请检查网络后重试。"), install_timeout: t("安装超时了，请检查网络后重试。"),
    install_not_found: t("安装结束了，但没有找到这个工具。可以重试，或看官方说明。") })[reason] || t("安装没有成功，可以重试，或看官方说明。");
}
function setupButton(label, primary, onClick, disabled, dataset = {}) {
  const button = element('button', label, primary ? 'setup-primary' : 'quiet-button'); button.type = 'button';
  Object.assign(button.dataset, dataset);
  button.disabled = disabled; button.addEventListener('click', onClick);
  return button;
}
// One card per tool: install, then sign in, then ready. Only the current step is on screen.
function nativeSetupCard(adapter) {
  const setup = adapter.setup; const operation = nativeLoginOf(adapter); const install = nativeInstallOf(adapter);
  const busy = !state.connected || state.nativeLoginBusy.has(adapter.id);
  const card = element('section', '', 'manager-card native-setup-card'); card.dataset.adapterSetup = adapter.id;
  const stage = setup.state === 'not_installed' && activeNativeInstall(install) ? t("正在安装") : activeNativeLogin(operation) ? t("等待登录")
    : ({ not_installed: t("还没安装"), login_required: t("还差登录"), ready: t("可以用了"), unknown: t("正在确认") })[setup.state];
  card.append(element('h3', adapter.name || adapter.id), element('p', stage, `native-setup-stage ${setup.state}`));
  const actions = element('div', '', 'manager-card-actions');
  if (setup.state === 'not_installed') {
    if (activeNativeInstall(install)) {
      card.append(element('p', t("通常一两分钟，可以先做别的。"), 'detail-caption'));
      if (install.id) actions.append(setupButton(t("取消"), false, () => void updateNativeInstall(adapter.id, 'DELETE'), busy));
    } else {
      if (install?.state === 'failed') card.append(element('p', nativeInstallFailure(install.reason), 'detail-caption'));
      actions.append(setupButton(install?.state === 'failed' ? t("重试") : t("安装"), true, () => void updateNativeInstall(adapter.id, 'POST'), busy));
      if (install?.state === 'failed') { const link = nativeSetupLink(t("官方说明"), setup.install_url); if (link) { actions.append(link); card.append(element('p', setup.install_url, 'native-login-url')); } }
    }
  } else if (setup.login_supported === true) {
    if (activeNativeLogin(operation)) {
      const waiting = adapter.id !== 'kimi' || operation.state === 'waiting';
      card.append(element('p', waiting ? t("请在浏览器里完成登录，完成后这里会自动更新。") : t("正在准备官方登录…"), 'detail-caption'));
      const link = nativeSetupLink(adapter.id === 'kimi' ? t("打开官方登录页") : t("没打开浏览器？点这里"), operation.verification_uri); if (link) actions.append(link);
      if (typeof operation.user_code === 'string' && operation.user_code.length <= 80) card.append(element('p', t("授权码：{p0}", { p0: operation.user_code }), 'native-login-code'));
      if (operation.id) actions.append(setupButton(t("取消登录"), false, () => void updateNativeLogin(adapter.id, 'DELETE'), busy, { nativeLoginCancel: adapter.id }));
    } else {
      if (['failed', 'cancelled'].includes(operation?.state)) card.append(element('p', operation.state === 'cancelled' ? t("本次登录已取消，可以重新登录。") : t("登录没有完成，可以重新登录。"), 'detail-caption'));
      if (setup.state !== 'ready' || operation?.state === 'failed') actions.append(setupButton(t("登录"), true, () => void updateNativeLogin(adapter.id, 'POST'), busy, { nativeLoginStart: adapter.id }));
    }
  }
  if (state.nativeLoginErrors.has(adapter.id)) card.append(element('p', state.nativeLoginErrors.get(adapter.id), 'notice error'));
  card.append(actions);
  return card;
}
function firstRunVisible() {
  if (state.selected || state.firstRunDismissed || !state.adapters.length || state.discovery?.state !== 'completed' || state.discoveryInFlight) return !state.selected && !state.firstRunDismissed && !!state.firstRunOpen;
  // A status that could not be read must not nag someone whose tools already work.
  if (!state.adapters.some((adapter) => ['ready', 'unknown'].includes(adapter.setup?.state))) state.firstRunOpen = true;
  return !!state.firstRunOpen;
}
function renderFirstRun() {
  const visible = firstRunVisible();
  $('first-run-setup').hidden = !visible;
  $('empty-room').classList.toggle('needs-setup', visible);
  $('first-run-done').hidden = !visible || !state.adapters.some((adapter) => adapter.setup?.state === 'ready');
  if (!visible) return;
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), state.adapters.map((item) => item.setup), [...state.nativeLogins], [...state.nativeInstalls], [...state.nativeLoginBusy], [...state.nativeLoginErrors], state.connected]);
  if (fingerprint === state.firstRunFingerprint) return;
  state.firstRunFingerprint = fingerprint;
  $('first-run-cards').replaceChildren(...state.adapters.filter((adapter) => adapter.setup).map(nativeSetupCard));
}
$('first-run-done').addEventListener('click', () => {
  state.firstRunDismissed = true; state.firstRunOpen = false;
  state.selectedParticipants = state.catalog.filter((agent) => selectableAgent(agent) && !isAdditionalSession(agent)).slice(0, 3).map((agent) => agent.id);
  renderParticipantPicker(); renderFirstRun(); syncParticipants(null); renderMembers(null); renderControls();
  $('message').focus();
});
function renderNativeSetup() {
  const adapters = state.adapters.filter((adapter) => adapter.setup
    && ['not_installed', 'login_required', 'ready', 'unknown'].includes(adapter.setup.state)
    && (adapter.setup.login_supported === true || adapter.setup.state === 'not_installed' && adapter.setup.install_url)
    && (adapter.setup.state !== 'ready' || state.nativeLoginBusy.has(adapter.id) || state.nativeLoginErrors.has(adapter.id)
      || activeNativeLogin(nativeLoginOf(adapter)) || nativeLoginOf(adapter)?.state === 'failed'));
  const container = $('native-tool-setup');
  container.hidden = !adapters.length || state.lifecycleViews.partners !== 'active';
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), [adapters, [...state.nativeLogins], [...state.nativeInstalls], [...state.nativeLoginBusy], [...state.nativeLoginErrors], state.connected, state.lifecycleViews.partners]]);
  if (fingerprint !== state.nativeSetupFingerprint) {
    state.nativeSetupFingerprint = fingerprint;
    container.replaceChildren(...adapters.map(nativeSetupCard));
  }
  renderFirstRun();
  syncNativeLoginTimer();
}

function renderPartnerViewVisibility() {
  const current = state.lifecycleViews.partners === 'active';
  $('partner-discovery').hidden = !current || (!state.discovery && !state.connectionProtocol);
  $('partners-intro').hidden = !current; $('partner-add-details').hidden = !current;
}
function renderDiscovery() {
  const discovery = state.discovery;
  $('discovery-hint').hidden = !discovery;
  renderPartnerViewVisibility();
  renderDiscoveryControls();
  if (!discovery) {
    $('partner-discovery-text').textContent = t("只检查本机已支持工具的安装位置，不读取聊天或调用模型。");
    return;
  }
  const found = discovery.tools.filter((tool) => tool.installed === true).map((tool) => tool.name);
  let message;
  if (discovery.state === 'unavailable') message = t("这次启动还没确认本机工具，现有设置保持不变。");
  else if (found.length) message = t("已找到 {p0}。选好工具就可以开始。", { p0: interfaceList(found) });
  else if (discovery.tools.length && discovery.tools.every((tool) => tool.installed === false)) message = t("还没在本机找到已支持的工具，可以到工具与状态中查看。");
  else message = t("还没确认本机有哪些支持的工具，可以到工具与状态中查看。");
  const hasDisabled = state.partners.some((partner) => partner.enabled === false
    && discovery.tools.some((tool) => tool.adapter === partner.adapter && tool.installed === true));
  if (discovery.state === 'completed' && found.length && hasDisabled) message += t(" 停用设置保持不变。");
  const hasHidden = state.partners.some((partner) => !activeEntry(partner));
  if (hasHidden) message += t(" 已归档或移除的伙伴不会自动恢复。");
  for (const id of ['discovery-hint-text', 'partner-discovery-text']) {
    if ($(id).textContent !== message) $(id).textContent = message;
  }
  $('discovery-manage').hidden = discovery.state === 'completed' && found.length > 0 && !((hasDisabled || hasHidden) && !state.catalog.filter(selectableAgent).length);
}

function renderDiscoveryControls() {
  $('rediscover-partners').hidden = !state.connectionProtocol;
  $('rediscover-partners').disabled = !state.connectionProtocol || state.discoveryInFlight;
  $('rediscover-partners').textContent = state.discoveryInFlight ? t("正在检测…") : t("重新检测");
}

function acceptBeanStyles(options) {
  if (!Array.isArray(options)) return;
  const known = options.filter((style) => style && beanStyleIds.has(style.id)
    && typeof style.name === 'string' && style.name.length <= 80
    && typeof style.description === 'string' && style.description.length <= 120);
  if (!known.length || new Set(known.map((style) => style.id)).size !== known.length) return;
  state.beanStyleOptions = localizedLabels(known);
  if (!known.some((style) => style.id === state.selectedBeanStyle)) state.selectedBeanStyle = known[0].id;
  renderBeanStylePicker();
}

function renderBeanStylePicker() {
  const picker = $('bean-style-picker');
  const existing = new Map([...picker.children].map((node) => [node.dataset.beanChoice, node]));
  const typedInitial = $('partner-avatar').value.trim();
  const previewInitial = typedInitial && [...typedInitial].length <= 2 ? typedInitial : (({ codex: 'X', kimi: 'K' })[$('partner-adapter').value] || 'C');
  const blocked = state.managementBusy || state.busy || !!state.pending || !!state.pendingPartner || !!state.configuringPartnerId;
  const nodes = state.beanStyleOptions.map((style) => {
    let button = existing.get(style.id);
    if (!button) {
      button = element('button', '', 'bean-style-card');
      button.type = 'button'; button.dataset.beanChoice = style.id;
      button.setAttribute('role', 'radio');
      button.append(avatar('bean-preview', { bean_style: style.id, avatar: previewInitial }),
        element('span', '', 'bean-style-name'), element('span', '', 'bean-style-description'));
      button.addEventListener('click', () => {
        if (state.managementBusy || state.busy || state.pending) return;
        state.selectedBeanStyle = style.id; state.beanStyleChosen = true; renderBeanStylePicker();
      });
      button.addEventListener('keydown', (event) => {
        if (state.managementBusy || state.busy || state.pending) return;
        const index = state.beanStyleOptions.findIndex((entry) => entry.id === style.id);
        let next;
        if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % state.beanStyleOptions.length;
        else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index + state.beanStyleOptions.length - 1) % state.beanStyleOptions.length;
        else if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = state.beanStyleOptions.length - 1;
        else return;
        event.preventDefault();
        state.selectedBeanStyle = state.beanStyleOptions[next].id; state.beanStyleChosen = true; renderBeanStylePicker();
        [...picker.children].find((node) => node.dataset.beanChoice === state.selectedBeanStyle)?.focus({ preventScroll: true });
      });
    }
    button.children[0].lastChild.textContent = previewInitial;
    button.children[1].textContent = style.name;
    button.children[2].textContent = style.description;
    button.setAttribute('aria-label', `${style.name}${interfaceComma()}${style.description}`);
    button.setAttribute('aria-checked', String(style.id === state.selectedBeanStyle));
    button.setAttribute('tabindex', style.id === state.selectedBeanStyle ? '0' : '-1');
    button.disabled = blocked;
    return button;
  });
  if (nodes.length !== picker.children.length || nodes.some((node, index) => picker.children[index] !== node)) picker.replaceChildren(...nodes);
  const selected = state.beanStyleOptions.find((style) => style.id === state.selectedBeanStyle);
  $('bean-style-selection').textContent = selected ? t("已选 {p0} · {p1}", { p0: selected.name, p1: selected.description }) : '';
}

function appearanceButton(id) {
  const button = element('button', t("换皮肤"), 'quiet-button appearance-button');
  button.type = 'button'; button.dataset.focusKey = 'appearance'; button.dataset.appearance = id;
  button.setAttribute('aria-label', t("给{p0}换皮肤", { p0: displayName(id) }));
  button.addEventListener('click', () => openAppearance(id, button));
  return button;
}
function refreshAppearancePortraits() {
  // Replace only portraits, keeping messages, readers, scroll and drafts intact.
  for (const node of document.querySelectorAll('.bean-avatar')) {
    if (!state.partners.some((partner) => partner.id === node.dataset.agent)) continue;
    if (!node.classList.contains(`bean-style-${beanStyle(node.dataset.agent)}`)) {
      const replacement = avatar(node.dataset.agent);
      replacement.lastChild.textContent = node.lastChild.textContent;
      node.replaceWith(replacement);
    }
  }
}
function openAppearance(id, trigger) {
  const partner = state.partners.find((item) => item.id === id);
  if (!partner || state.appearance?.saving) return;
  state.appearance = { id, selected: beanStyle(id, partner), trigger, saving: false };
  $('appearance-title').textContent = t("给{p0}换皮肤", { p0: partner.name });
  $('appearance-error').hidden = true;
  $('appearance-dialog').hidden = false; renderAppearance(); syncModalInert();
  [...$('appearance-picker').children].find((node) => node.getAttribute('aria-checked') === 'true')?.focus();
}
function closeAppearance() {
  if (!state.appearance || state.appearance.saving) return;
  const trigger = state.appearance.trigger;
  state.appearance = null; $('appearance-dialog').hidden = true; syncModalInert();
  if (trigger?.isConnected) trigger.focus({ preventScroll: true });
  else [...document.querySelectorAll('button')].find((node) => node.dataset.appearance === trigger?.dataset.appearance)?.focus({ preventScroll: true });
}
function renderAppearance() {
  const current = state.appearance; if (!current) return;
  const partner = state.partners.find((item) => item.id === current.id);
  const picker = $('appearance-picker');
  if (!picker.children.length) for (const style of state.beanStyleOptions) {
    const button = element('button', '', 'bean-style-card'); button.type = 'button';
    button.dataset.beanChoice = style.id; button.setAttribute('role', 'radio');
    button.append(avatar('bean-preview', { bean_style: style.id }), element('span', style.name, 'bean-style-name'), element('span', style.description, 'bean-style-description'));
    button.setAttribute('aria-label', `${style.name}${interfaceComma()}${style.description}`);
    button.addEventListener('click', () => {
      if (!state.appearance || state.appearance.saving) return;
      state.appearance.selected = style.id; renderAppearance();
    });
    button.addEventListener('keydown', (event) => {
      if (!state.appearance || state.appearance.saving) return;
      const index = state.beanStyleOptions.findIndex((item) => item.id === style.id);
      const count = state.beanStyleOptions.length;
      const next = ['ArrowRight', 'ArrowDown'].includes(event.key) ? (index + 1) % count
        : ['ArrowLeft', 'ArrowUp'].includes(event.key) ? (index + count - 1) % count
        : event.key === 'Home' ? 0 : event.key === 'End' ? count - 1 : null;
      if (next === null) return;
      event.preventDefault(); state.appearance.selected = state.beanStyleOptions[next].id; renderAppearance(); picker.children[next].focus();
    });
    picker.append(button);
  }
  for (const button of picker.children) {
    const selected = button.dataset.beanChoice === current.selected;
    button.setAttribute('aria-checked', String(selected)); button.setAttribute('tabindex', selected ? '0' : '-1'); button.disabled = current.saving;
    button.firstChild.lastChild.textContent = partner?.avatar || initials[current.id] || '?';
  }
  const style = state.beanStyleOptions.find((item) => item.id === current.selected);
  $('appearance-preview').replaceChildren(avatar('bean-preview', { bean_style: current.selected, avatar: partner?.avatar || initials[current.id] }));
  $('appearance-selection').textContent = style ? t("已选 {p0} · {p1}", { p0: style.name, p1: style.description }) : '';
  $('appearance-save').disabled = current.saving; $('appearance-cancel').disabled = current.saving;
  $('appearance-save').textContent = current.saving ? t("正在保存…") : t("保存皮肤");
}
$('appearance-cancel').addEventListener('click', closeAppearance);
$('appearance-save').addEventListener('click', async () => {
  const current = state.appearance; if (!current || current.saving) return;
  current.saving = true; renderAppearance(); $('appearance-error').hidden = true;
  try {
    acceptPartners(await api(`/api/partners/${encodeURIComponent(current.id)}/appearance`, 'POST', { bean_style: current.selected }));
    current.saving = false; closeAppearance();
  } catch (error) {
    current.saving = false; renderAppearance();
    $('appearance-error').textContent = t("皮肤未确认保存，请重试。当前选择已保留。"); $('appearance-error').hidden = false;
  }
});
$('appearance-dialog').addEventListener('pointerdown', (event) => { if (event.target === $('appearance-dialog')) closeAppearance(); });
document.addEventListener('keydown', (event) => {
  if (!state.appearance) return;
  if (event.key === 'Escape') { event.preventDefault(); closeAppearance(); }
  else if (event.key === 'Tab') {
    const controls = dialogControls($('appearance-dialog')).filter((node) => node.getAttribute('tabindex') !== '-1');
    if (!controls.length) return;
    event.preventDefault(); const index = controls.indexOf(document.activeElement);
    controls[(index + (event.shiftKey ? -1 : 1) + controls.length) % controls.length].focus();
  }
});
