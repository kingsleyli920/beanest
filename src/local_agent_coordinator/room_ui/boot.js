'use strict';

// Page wiring and startup: runs last, after every view's declarations exist.

$('new-room').addEventListener('click', startNewRoom);
$('toggle-history').addEventListener('click', () => setHistoryExpanded($('toggle-history').getAttribute('aria-expanded') !== 'true'));

$('project-picker').addEventListener('change', () => {
  if (state.selected || state.busy || state.pending) return;
  const project = state.projects.find((entry) => entry.id === $('project-picker').value && activeEntry(entry));
  state.selectedProject = project?.id || '';
  state.mode = project ? projectModeFor(project) : 'text';
  if (project && state.target === 'all') state.target = state.participantIds[0] || 'all';
  renderControls();
});
$('project-workflow').addEventListener('change', () => {
  if (state.selected || state.busy || state.pending || !state.nativeProjectSupported) return;
  const project = state.projects.find((entry) => entry.id === state.selectedProject && activeEntry(entry));
  if (project?.selection_kind !== 'folder') return;
  state.mode = $('project-workflow').value === 'native_project' ? 'native_project' : 'project_folder'; state.modeExplicit = true;
  // Leaving native development explicitly drops the broader permission.
  if (state.mode !== 'native_project' && ['full_access', 'plan'].includes(state.runtimeSettings?.permission)) state.runtimeSettings = { ...state.runtimeSettings, permission: 'workspace_write' };
  applyNativePermissionDefault(); saveDraft(); renderControls();
  if (state.mode === 'native_project') void loadRuntimeOptions();
});

$('message').addEventListener('input', () => { saveDraft(); updateRecipientQuery(); renderControls(); });
$('message').addEventListener('compositionstart', () => { composingMessage = true; closeRecipientMenu(); });
$('message').addEventListener('compositionend', () => { composingMessage = false; updateRecipientQuery(); });
$('message').addEventListener('click', updateRecipientQuery);
$('message').addEventListener('keydown', (event) => {
  if (composingMessage || event.isComposing || event.keyCode === 229) return;
  if (!recipientMenu) return;
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    const length = recipientMenu.options.length;
    if (length) recipientMenu.index = (recipientMenu.index + (event.key === 'ArrowDown' ? 1 : -1) + length) % length;
    renderRecipientMenu();
  } else if (event.key === 'Enter') {
    const id = recipientMenu.options[recipientMenu.index];
    if (id) { event.preventDefault(); chooseRecipient(id); }
    else closeRecipientMenu();
  } else if (event.key === 'Escape') { event.preventDefault(); closeRecipientMenu(); }
  else if (['Tab', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) closeRecipientMenu();
});
$('recipient-button').addEventListener('click', () => {
  if (recipientMenu) { closeRecipientMenu(); return; }
  recipientMenu = { query: '', index: Math.max(0, ['all', ...state.participantIds].indexOf(state.target)) };
  renderRecipientMenu(); $('message').focus({ preventScroll: true });
});
$('recipient-clear').addEventListener('click', () => chooseRecipient('all'));
document.addEventListener('pointerdown', (event) => {
  if (recipientMenu && !nodeContains($('recipient-menu'), event.target) && !nodeContains($('recipient-control'), event.target) && event.target !== $('message')) closeRecipientMenu();
});
for (const value of ['plan', 'build']) $(`work-mode-${value}`).addEventListener('click', () => {
  if (state.busy || state.pending || (state.selected && (naturalRuntimeConfiguration() ? !naturalRuntimeConfiguration().can_edit : state.snapshot?.current_run?.can_continue !== true && state.snapshot?.current_run?.can_start_suggested_goal !== true))) return;
  state.workMode = value; state.workModeDraftDirty = true; state.workModeDraftToken = crypto.randomUUID(); saveDraft(); renderControls();
});
$('task-mode').addEventListener('change', () => {
  if (state.selected || state.busy || state.pending) return;
  state.mode = state.taskModes.includes($('task-mode').value) ? $('task-mode').value : 'text';
  state.modeExplicit = true;
  if (state.mode !== 'text' && state.target === 'all') state.target = state.participantIds[0] || 'all';
  renderControls();
});
$('composer').addEventListener('submit', (event) => {
  event.preventDefault();
  if (state.busy || state.pending || composingMessage || event.isComposing) return;
  if (recipientProblem()) { showActionError(recipientProblem()); return; }
  if (!checkUserText($('message').value)) return;
  if (naturalContinuation()) {
    if (!$('send').disabled) void submitNaturalContinuation($('message').value);
    return;
  }
  if (continuationAction() && !$('send').disabled) {
    const text = $('message').value.trim();
    if (continuationAction() !== 'native_recovery' && !isContinueCommand(text)) {
      showNotice(t("这段补充已保留。先点“继续”接续原任务，恢复后可以发送补充。"));
      return;
    }
    void continueCurrentWork(state.snapshot, text);
    return;
  }
  if (policySubmissionError() || runtimeSubmissionError()) { showNotice(policySubmissionError() || runtimeSubmissionError(), 'error'); return; }
  if (submissionAvailabilityBlocked()) { showNotice(submissionAvailabilityMessage(), 'error'); return; }
  if ($('send').disabled) return;
  const text = $('message').value.trim();
  if (!text) { showNotice(t("请填写目标或补充说明。"), 'error'); return; }
  if (!checkUserText($('message').value)) return;
  if (currentObjective()?.can_answer === true) {
    void submitObjectiveAction('answer', state.snapshot, text);
    return;
  }
  if (state.forkSource?.roomId === state.selected) {
    void submitFork(text);
    return;
  }
  if (!currentObjective() && state.snapshot?.current_run?.can_continue === true) {
    void submitRunAction('run', { text });
    return;
  }
  state.pending = capturePendingIntent({
    kind: state.selected ? 'message' : 'create', roomId: state.selected,
    body: { [state.selected ? 'text' : 'goal']: text, target: state.target, request_id: crypto.randomUUID() },
  });
  if (state.selected) Object.assign(state.pending.body, pendingRuntimeChanges());
  if (!state.selected && state.mode !== 'text') state.pending.body.mode = state.mode;
  if (!state.selected) {
    state.pending.body.work_mode = state.workMode;
    const runtime = runtimePayload(); if (runtime) state.pending.body.runtime_settings = runtime;
    const policy = executionPolicyPayload(); if (policy) state.pending.body.execution_policy = policy;
    if (state.participantIds.length >= 2) state.pending.body.collaboration = true;
    state.pending.body.participants = [...state.participantIds];
    if (state.selectedProject) state.pending.body.project_id = state.selectedProject;
  }
  void submitPending();
});

$('cancel-room').addEventListener('click', () => {
  if (!state.selected || !activeEntry(state.snapshot?.room)) return;
  void stopRoomIndependently(state.selected);
});
$('pause-objective').addEventListener('click', () => void submitObjectiveAction(currentObjective()?.status === 'paused' ? 'resume' : 'pause'));
$('refresh').addEventListener('click', () => void refresh({ restoreSelection: true }));
$('rediscover-partners').addEventListener('click', async () => {
  await discoverOnOpen({ reopen: true, manual: true });
  renderControls();
});
$('download-artifacts').addEventListener('click', () => void downloadProject());
$('open-projects').addEventListener('click', (event) => openManager('projects', event.currentTarget || $('open-projects')));
$('composer-projects').addEventListener('click', (event) => { openManager('projects', event.currentTarget || $('composer-projects')); $('project-add-details').open = true; });
$('open-partners').addEventListener('click', (event) => openManager('partners', event.currentTarget || $('open-partners')));
$('discovery-manage').addEventListener('click', (event) => openManager('partners', event.currentTarget || $('discovery-manage')));
$('manager-projects-tab').addEventListener('click', (event) => openManager('projects', event.currentTarget || $('manager-projects-tab')));
$('manager-partners-tab').addEventListener('click', (event) => openManager('partners', event.currentTarget || $('manager-partners-tab')));
$('close-manager').addEventListener('click', closeManager);
$('choose-project-folder').addEventListener('click', async () => {
  if (state.projectChoosing || state.managementBusy || state.busy || state.pending
      || typeof window.beanestDesktop?.chooseProjectFolder !== 'function') return;
  const previousNote = { text: $('project-picker-note').textContent, hidden: $('project-picker-note').hidden };
  state.projectChoosing = true; renderManagementControls();
  $('project-picker-note').textContent = t("正在选择项目文件夹…"); $('project-picker-note').hidden = false;
  try {
    const selection = await window.beanestDesktop.chooseProjectFolder();
    if (selection?.cancelled === true) {
      $('project-picker-note').textContent = previousNote.text; $('project-picker-note').hidden = previousNote.hidden; return;
    }
    if (selection?.cancelled !== false || typeof selection.path !== 'string' || !selection.path.startsWith('/')) throw new Error('invalid_project_selection');
    $('project-path').value = selection.path;
    $('project-explicit-files').checked = false;
    if (!$('project-name').value.trim()) $('project-name').value = typeof selection.name === 'string' && selection.name.trim() ? selection.name.trim().slice(0, 80) : t("我的项目");
    $('project-picker-note').textContent = t("已选完整项目文件夹。伙伴会按需查找文件；原项目保持不变，点“添加项目”后保存位置。");
  } catch {
    $('project-picker-note').textContent = t("文件夹选择未完成，请重试或手动填写项目目录。原表单仍保留。");
  } finally { state.projectChoosing = false; renderManagementControls(); }
});
$('choose-project-files').addEventListener('click', async () => {
  if (state.projectChoosing || state.managementBusy || state.busy || state.pending
      || typeof window.beanestDesktop?.chooseProjectFiles !== 'function') return;
  const previousNote = { text: $('project-picker-note').textContent, hidden: $('project-picker-note').hidden };
  state.projectChoosing = true; renderManagementControls();
  $('project-picker-note').textContent = t("正在选择项目文件…"); $('project-picker-note').hidden = false;
  try {
    const selection = await window.beanestDesktop.chooseProjectFiles();
    if (selection?.cancelled === true) {
      $('project-picker-note').textContent = previousNote.text; $('project-picker-note').hidden = previousNote.hidden;
      return;
    }
    if (selection?.cancelled !== false || typeof selection.directory !== 'string' || !selection.directory.startsWith('/')
        || !Array.isArray(selection.files) || !selection.files.length || selection.files.length > 12
        || selection.files.some((file) => typeof file !== 'string' || file.startsWith('/') || file.split('/').some((part) => !part || part === '.' || part === '..'))
        || new Set(selection.files).size !== selection.files.length) throw new Error('invalid_project_selection');
    $('project-explicit-files').checked = true;
    $('project-path').value = selection.directory;
    $('project-files-input').value = selection.files.join('\n');
    if (!$('project-name').value.trim()) $('project-name').value = typeof selection.suggestedName === 'string'
      && selection.suggestedName.trim() ? selection.suggestedName.trim().slice(0, 80) : t("我的项目");
    $('project-picker-note').textContent = t("已选 {p0} 个文件：{p1}。不会改动原项目，点“添加项目”后才保存。", { p0: selection.files.length, p1: interfaceList(selection.files) });
  } catch (error) {
    const labels = {
      project_selection_unavailable: t("暂时无法打开文件选择，请重试或展开手动填写。"),
      project_selection_outside_directory: t("请只选择同一项目目录中的文件。原表单未改变。"),
      invalid_project_selection: t("文件选择不符合当前范围，请选择最多 12 个项目内文本文件。"),
    };
    $('project-picker-note').textContent = labels[error?.message] || t("文件选择未完成，原表单仍保留。");
  } finally { state.projectChoosing = false; renderManagementControls(); }
});
$('project-form').addEventListener('submit', (event) => {
  event.preventDefault();
  if (state.projectChoosing) return;
  const files = $('project-files-input').value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const explicit = $('project-explicit-files').checked;
  const body = { name: $('project-name').value.trim(), path: $('project-path').value.trim(), ...(explicit ? { files } : { selection_kind: 'folder' }) };
  if (!body.name || !body.path.startsWith('/') || (explicit && (!files.length || files.length > 12 || new Set(files).size !== files.length
      || files.some((file) => file.startsWith('/') || file.split('/').some((part) => !part || part === '.' || part === '..'))))) {
    managerNotice(explicit ? t("请填写名称、完整目录路径和 1–12 个不重复的相对文件路径。") : t("请填写项目名称和完整目录路径。"), true); return;
  }
  void manageMutation(workspaceApi.projects, body, t("项目已登记。选择项目和本次伙伴后再开始任务，没有启动模型。"));
});
$('partner-form').addEventListener('submit', (event) => {
  event.preventDefault();
  if (state.pendingPartner || state.managementBusy || state.busy || state.pending) return;
  const adapter = $('partner-adapter').value;
  const body = {
    id: $('partner-id').value.trim() || generatedPartnerId(adapter),
    name: $('partner-name').value.trim() || defaultPartnerName(adapter),
    avatar: $('partner-avatar').value.trim() || defaultPartnerAvatar(adapter),
    adapter, bean_style: state.selectedBeanStyle,
  };
  const connector = state.connectionTemplates.some((template) => template.adapter === adapter);
  try {
    if (connector) body.connection = readConnection();
    if (state.configuringPartnerId && !body.connection) throw new RequestError(t("请填写本机连接器路径与调用方式后保存配置。"));
  } catch (error) { managerNotice(error.message, true); return; }
  if (!agentPattern.test(body.id) || !body.name || !body.avatar || [...body.avatar].length > 2 || !state.adapters.some((adapter) => adapter.id === body.adapter)
      || !beanStyleIds.has(body.bean_style)) {
    managerNotice(t("请填写有效标识、名称、1–2 个头像字符，并选择已支持的接入工具。"), true); return;
  }
  $('partner-id').value = body.id; $('partner-name').value = body.name; $('partner-avatar').value = body.avatar;
  state.pendingPartner = body; state.partnerSaveUncertain = false; rememberPendingPartner();
  const path = state.configuringPartnerId ? workspaceApi.partner(state.configuringPartnerId, 'connection') : workspaceApi.partners;
  const payload = state.configuringPartnerId ? { connection: body.connection } : body;
  const success = connector ? t("工具配置已保存。安装并通过连接检查后才能参加任务，没有启动模型。") : t("独立会话已创建，需要时可在“更多会话”中选择。");
  void manageMutation(path, payload, success).then((saved) => {
    if (saved) clearPartnerForm();
    else if (!state.partnerSaveUncertain) { state.pendingPartner = null; rememberPendingPartner(); renderManagementControls(); }
  });
});
$('partner-avatar').addEventListener('input', renderBeanStylePicker);
$('partner-adapter').addEventListener('change', () => {
  if (!state.beanStyleChosen && !state.configuringPartnerId) state.selectedBeanStyle = defaultBeanStyle($('partner-adapter').value);
  renderManagementControls();
});
$('cancel-connection').addEventListener('click', clearPartnerForm);
$('verify-partner-save').addEventListener('click', () => void verifyPendingPartner());
$('project-explicit-files').addEventListener('change', renderManagementControls);
for (const id of ['project-name', 'project-path', 'project-files-input']) $(id).addEventListener('input', renderManagementControls);
document.addEventListener('keydown', (event) => {
  if (!state.manager || event.defaultPrevented || state.lifecycleConfirm || state.availabilityRecheck || state.appearance) return;
  if (event.key === 'Escape') { event.preventDefault(); closeManager(); }
  else if (event.key === 'Tab') {
    const controls = dialogControls($('workspace-manager'));
    if (!controls.length) return;
    event.preventDefault();
    const current = controls.indexOf(document.activeElement);
    controls[(current + (event.shiftKey ? -1 : 1) + controls.length) % controls.length].focus({ preventScroll: true });
  }
});
$('workspace-manager').addEventListener('pointerdown', (event) => { if (event.target === $('workspace-manager')) closeManager(); });
$('verify-request').addEventListener('click', () => void refresh());
$('retry-request').addEventListener('click', () => void submitPending());
$('new-events').addEventListener('click', scrollToLatest);
$('toggle-members').addEventListener('click', () => {
  const expanded = document.querySelector('.app-shell').classList.toggle('members-visible');
  $('toggle-members').setAttribute('aria-expanded', String(expanded));
  syncAccountWatchTimer(); void refreshWatchingAccounts();
});
$('close-members').addEventListener('click', () => {
  document.querySelector('.app-shell').classList.remove('members-visible');
  $('toggle-members').setAttribute('aria-expanded', 'false');
  $('toggle-members').focus();
  syncAccountWatchTimer();
});
$('timeline').addEventListener('scroll', () => { void loadOlderEvents(); if (nearBottom()) { state.unseen = 0; $('new-events').hidden = true; } });
window.addEventListener('focus', () => { if (!document.hidden) void refresh(); });
document.addEventListener('visibilitychange', () => {
  syncAccountWatchTimer(); syncAvailabilityWatchTimer();
  if (!document.hidden) { void refresh(); void refreshWatchingAccounts(); void refreshAvailability({ immediate: true }); }
});
window.addEventListener('beforeunload', (event) => { if (state.pending) { event.preventDefault(); event.returnValue = ''; } });
if (typeof window.beanestDesktop?.onOpened === 'function') {
  const unsubscribe = window.beanestDesktop.onOpened(() => {
    void refresh();
    void discoverOnOpen({ reopen: true }).then((error) => {
      if (error) showNotice(error, 'error');
      renderControls();
    });
  });
  if (typeof unsubscribe === 'function') window.addEventListener('pagehide', unsubscribe);
}

function renderLanguageControl() {
  const targetIsEnglish = window.BeanestI18n.getLocale() !== 'en';
  const button = $('interface-language');
  button.textContent = targetIsEnglish ? 'English' : '简体中文';
  button.setAttribute('aria-label', button.textContent);
  button.setAttribute('lang', targetIsEnglish ? 'en' : 'zh-CN');
}

// Language changes re-render interface state only. They never apply a server
// snapshot, save an editor draft, or dispatch a task/request.
function renderInterfaceLanguage() {
  const fields = ['input', 'textarea', 'select'].flatMap(tag => [...document.querySelectorAll(tag)])
    .filter(node => node.id && node.id !== 'interface-language')
    .map(node => ({ id: node.id, value: node.value, checked: node.checked,
      start: node.selectionStart, end: node.selectionEnd }));
  const focus = document.activeElement;
  const positions = ['timeline', 'task-materials-body', 'reader-body', ...($('activity-events') ? ['activity-events'] : [])].map(id => [id, $(id).scrollTop]);
  const openDetails = [...document.querySelectorAll('details')].filter(node => node.id).map(node => [node.id, node.open]);
  const unseen = state.unseen;
  renderRooms(); renderParticipantPicker(); renderMembers(state.snapshot); renderConnection();
  if (state.snapshot) {
    $('events').replaceChildren(); state.eventNodes.clear();
    renderEvents(state.snapshot, { preservePosition: true });
  }
  renderProjectPicker(); renderProjects(); renderGlobalPartners(); renderDiscovery(); renderNativeSetup();
  renderManagementControls(); renderCodeStatus(); renderMediaShelf(); renderMaterials();
  renderControls(); renderAvailabilityRecheck(); renderAppearance(); renderUsagePopover();
  if (state.reader) { state.reader.renderedKey = null; renderReader(); }
  for (const field of fields) {
    const node = $(field.id); if (!node) continue;
    if (node.tagName !== 'SELECT' || [...node.children].some(option => option.value === field.value)) node.value = field.value;
    if (field.checked !== undefined) node.checked = field.checked;
    const selectable = node.tagName === 'TEXTAREA' || node.tagName === 'INPUT'
      && ['text', 'search', 'tel', 'url', 'password'].includes(node.type || node.getAttribute('type') || 'text');
    if (selectable && Number.isInteger(field.start) && typeof node.setSelectionRange === 'function') node.setSelectionRange(field.start, field.end);
  }
  for (const [id, open] of openDetails) if ($(id)) $(id).open = open;
  for (const [id, top] of positions) $(id).scrollTop = top;
  state.unseen = unseen;
  renderLanguageControl(); applyTheme();
  if (focus?.id && $(focus.id)) $(focus.id).focus({ preventScroll: true });
}
renderLanguageControl();
applyTheme();
$('interface-theme').addEventListener('click', () => {
  const next = ({ system: 'light', light: 'dark', dark: 'system' })[themePreference()];
  try { localStorage.setItem(themePreferenceKey, next); } catch (_) { /* Session-only preference still applies. */ }
  applyTheme(next);
  if (document.documentElement?.dataset && next === 'system') delete document.documentElement.dataset.theme;
});
for (const name of ['progress', 'discussion', 'delivery', 'materials']) $(`tab-${name}`).addEventListener('click', () => selectRoomTab(name));
$('room-tabs').addEventListener('keydown', (event) => {
  const order = ['progress', 'discussion', 'delivery', 'materials']; const current = order.indexOf(currentRoomTab());
  if (!['ArrowLeft', 'ArrowRight'].includes(event.key) || current < 0) return;
  event.preventDefault(); selectRoomTab(order[(current + (event.key === 'ArrowRight' ? 1 : order.length - 1)) % order.length]);
});
$('interface-language').addEventListener('click', () => {
  window.BeanestI18n.setLocale(window.BeanestI18n.getLocale() === 'en' ? 'zh-CN' : 'en');
});
window.BeanestI18n.subscribe(renderInterfaceLanguage);
if (typeof window.beanestDesktop?.setLocale === 'function'
    && window.beanestDesktop.currentLocale !== window.BeanestI18n.getLocale()) {
  Promise.resolve(window.beanestDesktop.setLocale(window.BeanestI18n.getLocale())).catch(() => {});
}

(async () => {
  if (!token || token === '__LAC_TOKEN__') {
    setConnection(false);
    showNotice(t("请从本地协作服务打开这个窗口。"), 'error');
    renderControls();
    return;
  }
  if (window.location?.hash && !roomFromLocation()) rememberRoomLocation();
  await restorePending();
  restoreLifecycleOperations();
  outboxRestored = true;
  try {
    await rememberPending();
    // A legacy body is removed only after its complete replacement committed.
    rememberLifecycleOperations();
  } catch (_error) { /* Legacy raw requests remain available if migration fails. */ }
  delete state.restoredOutbox;
  restorePendingPartner();
  renderControls();
  if (!state.selected && !state.pending && !$('message').value) {
    const epoch = state.epoch; await loadComposerDraft('new');
    if (epoch === state.epoch && !state.selected && !state.pending && !$('message').value) {
      const draft = state.drafts.get('new');
      if (draft) {
        $('message').value = draft.text; state.target = draft.target; state.targetExplicit = true;
        if (Array.isArray(draft.participants)) { state.selectedParticipants = [...draft.participants]; state.participantsChosen = true; }
        if (validRuntimeSettings(draft.runtimeSettings)) {
          state.runtimeSettings = copyRuntimeSettings(draft.runtimeSettings);
          state.runtimeDraftDirty = draft.runtimeDraftDirty; state.runtimeDraftToken = draft.runtimeDraftToken;
          state.runtimePermissionExplicit = draft.runtimePermissionExplicit;
        }
        if (['plan', 'build'].includes(draft.workMode)) {
          state.workMode = draft.workMode; state.workModeDraftDirty = draft.workModeDraftDirty;
          state.workModeDraftToken = draft.workModeDraftToken;
        }
        renderControls();
      }
    }
  }
  await refresh({ initial: true });
  void refreshAvailability({ immediate: true });
  setInterval(() => { if (!document.hidden) { if (state.busy) { void refreshLifecycleOperations(); void refreshPartnerOperations(); } else void refresh(); } }, 3000);
})();
