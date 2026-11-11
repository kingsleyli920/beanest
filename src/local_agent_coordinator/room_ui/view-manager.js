'use strict';

// The projects and partners management page.

function renderProjectPicker() {
  const picker = $('project-picker');
  const projects = state.projects.filter(activeEntry);
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), projects.map((project) => [project.id, project.name])]);
  if (picker.dataset.fingerprint === fingerprint) { picker.value = state.selectedProject; return; }
  picker.dataset.fingerprint = fingerprint;
  const empty = element('option', t("不关联项目 · 自由协作")); empty.value = '';
  picker.replaceChildren(empty, ...projects.map((project) => {
    const option = element('option', project.name); option.value = project.id; return option;
  }));
  picker.value = state.selectedProject;
}

function projectModeFor(project) {
  return project?.selection_kind === 'folder' ? 'project_folder' : 'project_files';
}

function acceptProjects(result) {
  if (!Array.isArray(result.projects) || result.projects.some((project) => !uuidPattern.test(project.id)
      || typeof project.name !== 'string' || typeof project.path !== 'string' || !Array.isArray(project.files))) {
    throw new RequestError(t("项目记录格式尚未确认，请刷新后核对。"));
  }
  state.projects = result.projects; state.projectsAccepted = true;
  if (state.selectedProject && !state.projects.some((project) => project.id === state.selectedProject && activeEntry(project))) {
    state.selectedProject = ''; state.mode = 'text'; chooseDefaultArtifactMode();
    const draft = state.drafts.get('new');
    if (draft) { draft.project = ''; draft.mode = state.mode; }
  }
  renderProjectPicker(); renderProjects();
}

function renderProjects() {
  renderLifecycleFilter('projects', state.projects);
  const projects = visibleEntries('projects', state.projects);
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), [projects, [...state.projectDeletions.values()], state.projectDeletionSupported, state.nativeProjectSupported, state.lifecycleViews.projects, state.managementBusy, state.busy, !!state.pending]]);
  if (state.projectListFingerprint === fingerprint) return;
  state.projectListFingerprint = fingerprint;
  if (state.contextMenu?.kind === 'projects') closeContextMenu({ restoreFocus: true });
  $('projects-list').replaceChildren();
  if (!projects.length) $('projects-list').append(element('p', state.lifecycleViews.projects === 'active' ? t("还没有项目。选择项目文件夹，就能和伙伴一起处理任务。") : t("这里没有{p0}的项目。", { p0: lifecycleLabels[state.lifecycleViews.projects] }), 'manager-empty'));
  if (state.projectDeletionSupported) {
    const removed = state.projects.filter((project) => lifecycleState(project) === 'removed');
    if (removed.length) {
      const review = element('details', '', 'legacy-project-review');
      review.append(element('summary', t("整理旧移除登记（{p0}）", { p0: removed.length })));
      for (const project of removed) {
        const row = element('article', '', 'manager-card'); row.append(element('h3', project.name), lifecycleActions('projects', project)); review.append(row);
      }
      $('projects-list').append(review);
    }
  }
  for (const project of projects) {
    const card = element('article', '', 'manager-card');
    bindContextTarget(card, 'projects', project.id);
    card.append(element('h3', project.name), element('p', project.path, 'project-directory'));
    const files = element('ul', '', 'project-file-list');
    if (project.selection_kind === 'folder') files.append(element('li', t("完整项目 · 伙伴按需查找文件")));
    else for (const file of project.files) files.append(element('li', file));
    const choose = element('button', t("用这个项目开新任务"), 'quiet-button');
    choose.type = 'button'; choose.disabled = state.busy || !!state.pending || state.managementBusy || !activeEntry(project);
    choose.addEventListener('click', () => {
      if (state.busy || state.pending || !activeEntry(project)) return;
      if (state.selected) startNewRoom();
      state.selectedProject = project.id; state.mode = projectModeFor(project);
      if (state.target === 'all') state.target = state.participantIds[0] || 'all';
      renderProjectPicker(); renderControls(); closeManager();
      $('message').focus({ preventScroll: true });
    });
    card.append(files, element('p', project.selection_kind === 'folder' && state.nativeProjectSupported
      ? t("可选本机开发或文本协作 · 按所选权限执行") : t("文本协作 · 改稿保存在豆窝，暂不运行项目"), 'detail-caption'));
    if (activeEntry(project)) card.append(choose);
    card.append(lifecycleActions('projects', project));
    $('projects-list').append(card);
  }
}

function acceptPartners(result) {
  const context = state.partnerResponseContexts.get(result);
  if (state.partnerLifecycleSupported && context && (context.intentEpoch !== state.partnerIntentEpoch || context.generation < state.partnerAppliedGeneration)) return;
  if (context) state.partnerAppliedGeneration = context.generation;
  if (!Array.isArray(result.partners) || !Array.isArray(result.supported_adapters)) throw new RequestError(t("伙伴管理记录暂不可用。"));
  const firstDirectory = !state.partnerDirectorySeen; state.partnerDirectorySeen = true;
  if (state.partnerLifecycleSupported) for (const operation of result.partner_lifecycle_operations || []) acceptPartnerOperation(operation, null, context);
  state.partners = result.partners.filter((partner) => partner && agentPattern.test(partner.id)).filter((partner) => {
    const operation = state.partnerLifecycleSupported && latestPartnerOperation(partner.id);
    return !(operation?.status === 'completed' && operation.action === 'remove');
  });
  if (state.partnerLifecycleSupported) for (const partner of state.partners) {
    const operation = latestPartnerOperation(partner.id);
    if (operation?.status !== 'completed') continue;
    if (['enable', 'disable'].includes(operation.action)) partner.enabled = operation.action === 'enable';
    if (['archive', 'restore'].includes(operation.action)) partner.lifecycle_state = operation.action === 'archive' ? 'archived' : 'active';
  }
  for (const partner of state.partners) if (Array.isArray(partner.monitor?.current_tasks)) {
    partner.monitor.current_tasks = partner.monitor.current_tasks.filter((task) => !state.deletedRooms.has(task.room_id));
  }
  if (firstDirectory && !state.pending && !state.selected && !Array.isArray(state.drafts.get('new')?.participants)) state.selectedParticipants = null;
  const supportedAdapterIds = new Set(result.supported_adapters.filter((adapter) => adapter && agentPattern.test(adapter.id)).map((adapter) => adapter.id));
  const discovery = result.discovery;
  state.discovery = discovery && ['completed', 'unavailable'].includes(discovery.state)
    && discovery.scope === 'supported_cli_locations' && Array.isArray(discovery.tools)
    && discovery.tools.every((tool) => tool && supportedAdapterIds.has(tool.adapter)
      && typeof tool.name === 'string' && tool.name.trim() && tool.name.length <= 80
      && (tool.installed === null || typeof tool.installed === 'boolean'))
    && new Set(discovery.tools.map((tool) => tool.adapter)).size === discovery.tools.length ? discovery : null;
  state.connectionTemplates = Array.isArray(result.connection_templates) ? result.connection_templates.filter((template) =>
    template && agentPattern.test(template.adapter) && typeof template.name === 'string' && template.protocol === 'beanest-json-v1') : [];
  state.connectionProtocol = result.connection_protocol?.id === 'beanest-json-v1' ? result.connection_protocol : null;
  renderDiscovery();
  const combined = [...result.supported_adapters, ...state.connectionTemplates.map((template) => ({ id: template.adapter, name: t(template.name) }))];
  const adapters = combined.filter((adapter, index) => adapter && agentPattern.test(adapter.id) && combined.findIndex((item) => item.id === adapter.id) === index);
  acceptBeanStyles(result.bean_styles);
  rememberAgents(state.partners);
  if (JSON.stringify(adapters) !== JSON.stringify(state.adapters)) {
    state.adapters = adapters;
    const previous = state.pendingPartner?.adapter || $('partner-adapter').value;
    $('partner-adapter').replaceChildren(...state.adapters.map((adapter) => {
      const option = element('option', adapter.name || adapter.id); option.value = adapter.id; return option;
    }));
    $('partner-adapter').value = state.adapters.some((adapter) => adapter.id === previous) ? previous : state.adapters[0]?.id || '';
  }
  setCatalog(state.catalog);
  renderMembers(state.snapshot);
  refreshAppearancePortraits();
  renderGlobalPartners(); renderNativeSetup();
  syncAvailabilityWatchTimer();
}

function monitorTaskText(task) {
  if (task.work_mode === 'plan' && task.kind === 'team_work') return t("整理已认领任务的方案");
  if (Object.hasOwn(teamStageLabels, task.kind)) return teamStageLabels[task.kind];
  if (typeof task.kind === 'string' && task.kind.startsWith('team_')) return t("团队任务阶段待核对");
  return typeof task.current_task === 'string' ? task.current_task : t("任务摘要不可用");
}

function renderGlobalPartners() {
  renderPartnerViewVisibility();
  renderLifecycleFilter('partners', state.partners);
  // A tool that is not installed yet appears only as its setup card above.
  const partners = visibleEntries('partners', state.partners).filter((partner) => partner.installation?.state !== 'missing'
    || !state.adapters.some((adapter) => adapter.id === partner.adapter && adapter.setup));
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), [partners, state.lifecycleViews.partners, state.busy, !!state.pending, state.managementBusy, state.accountRefreshId, !!state.pendingPartner, state.partnerLifecycleSupported, [...state.partnerOperations.values()], state.catalog.map((agent) => agent.id), partners.map((partner) => availabilityInfo(partner.id).detail), state.availabilityError, state.availabilityRecheck?.id]]);
  if (fingerprint === state.partnerListFingerprint) return;
  state.partnerListFingerprint = fingerprint;
  if (state.contextMenu?.kind === 'partners') closeContextMenu({ restoreFocus: true });
  const previousCards = [...$('partners-list').children, ...$('additional-partners-list').children];
  const expanded = new Map(previousCards.map((card) => [card.dataset.partner,
    new Map([...card.children].filter((node) => node.tagName === 'DETAILS').map((node) => [node.dataset.detailKind || 'usage', Boolean(node.open)]))]));
  const focused = document.activeElement;
  let focusedCard = focused;
  while (focusedCard && !focusedCard.dataset?.partner) focusedCard = focusedCard.parentNode;
  const focus = focusedCard && focused?.dataset?.focusKey
    ? { partner: focusedCard.dataset.partner, key: focused.dataset.focusKey } : null;
  $('partners-list').replaceChildren();
  $('additional-partners-list').replaceChildren();
  const additional = partners.filter(isAdditionalSession);
  const active = additional.filter((partner) => ['working', 'queued', 'waiting_peer'].includes(partner.monitor?.status)).length;
  const waiting = additional.filter((partner) => partner.monitor?.status === 'needs_user').length;
  const uncertain = additional.filter((partner) => partner.monitor?.status === 'unknown').length;
  $('advanced-sessions-label').textContent = t("更多会话（高级）{p0}{p1}{p2}", { p0: active ? t(" · {p0} 个有任务", { p0: active }) : '', p1: waiting ? t(" · {p0} 个等你回复", { p0: waiting }) : '', p2: uncertain ? t(" · {p0} 个需核对", { p0: uncertain }) : '' });
  $('advanced-sessions').hidden = state.lifecycleViews.partners !== 'active' || !additional.length;
  if (!partners.length) $('partners-list').append(element('p', state.lifecycleViews.partners === 'active' ? t("还没有工具。可以安装已支持的工具，或在下方先保存本机连接器模板。") : t("这里没有{p0}的伙伴。", { p0: lifecycleLabels[state.lifecycleViews.partners] }), 'manager-empty'));
  for (const partner of partners) {
    const card = element('article', '', 'manager-card partner-monitor');
    card.dataset.partner = partner.id;
    bindContextTarget(card, 'partners', partner.id);
    const heading = element('div', '', 'member-heading');
    const identity = element('div');
    if (state.lifecycleViews.partners !== 'active') {
      identity.append(element('h3', partner.name), element('p', lifecycleLabels[lifecycleState(partner)], 'detail-caption'));
      heading.append(avatar(partner.id, partner), identity); card.append(heading, lifecycleActions('partners', partner));
      const operation = latestPartnerOperation(partner.id);
      if (state.partnerLifecycleSupported && operation && !partnerOperationSettled(operation)) card.append(element('p', partnerOperationText(operation), 'detail-caption'));
      $('partners-list').append(card); continue;
    }
    const installation = ['found', 'missing', 'unknown'].includes(partner.installation?.state) ? partner.installation.state : 'unknown';
    const availability = availabilityInfo(partner.id);
    const invitation = !activeEntry(partner) ? lifecycleLabels[lifecycleState(partner)] : partner.enabled === false ? state.partnerLifecycleSupported ? t("已停用，不再派发新工作") : t("新任务中已停用")
      : notSetUp(partner) ? partnerSetupState(partner.id) === 'not_installed' ? t("还没安装，装好后才能选入新任务") : t("还差登录，登录后才能选入新任务")
      : availability.blocked ? availability.summary : (state.catalog.some((agent) => agent.id === partner.id) ? t("可选入新任务") : t("暂不能加入新任务"));
    const toolName = ({ claude: 'Claude Code', codex: 'Codex' })[partner.adapter]
      || state.adapters.find((adapter) => adapter.id === partner.adapter)?.name || t("工具待确认");
    identity.append(element('h3', partner.name), element('p', notSetUp(partner) ? memberLoginCaption(partner.account, partner.id) : accountLoginCaption(partner.account), 'account-email'), element('p', `${toolName}${isAdditionalSession(partner) ? t(" 的独立会话") : ''} · ${invitation}`, 'detail-caption'));
    heading.append(avatar(partner.id, partner), identity, appearanceButton(partner.id));
    const health = partner.health || {};
    const healthLabels = { ready: t("接入检查就绪"), unavailable: t("接入不可用"),
      unknown: Number.isFinite(health.checked_at) ? t("接入尚未确认") : t("尚未检查接入") };
    card.append(heading, element('p', { found: t("本机已找到"), missing: t("本机未找到"), unknown: t("本机未检测") }[installation], 'installation-state'),
      element('p', t("连接检查：{p0}", { p0: healthLabels[health.state] || healthLabels.unknown }), `health-state ${health.state === 'ready' ? 'ready' : health.state === 'unavailable' ? 'unavailable' : 'unknown'}`));
    if (availability.value) card.append(element('p', availabilityText(availability), `availability-note${availability.blocked ? ' blocked' : ''}`));
    const skippedReasons = {
      config_unreadable: t("读不了本机配置文件，通常是较旧的 CLI 遇到新版配置；升级或移除这个安装即可。"),
      cli_outdated: t("版本太旧，不支持豆窝需要的参数；升级或移除这个安装即可。"),
      cli_unusable: t("这个安装无法正常运行，例如缺少它依赖的 Node。"),
      login_unconfirmed: t("没有确认已登录的订阅账号。"), login_check_failed: t("登录检查没有完成。"),
    };
    for (const item of Array.isArray(partner.installation?.skipped) ? partner.installation.skipped : []) {
      if (typeof item?.path !== 'string' || !Object.hasOwn(skippedReasons, item.reason)) continue;
      card.append(element('p', t("已跳过 {p0}（{p1}）：{p2}", { p0: item.path,
        p1: typeof item.version === 'string' ? item.version : t("版本未知"), p2: skippedReasons[item.reason] }), 'availability-note blocked'));
    }
    const recheck = availabilityRecheckControl(partner.id); if (recheck) card.append(recheck);
    const healthReasons = {
      cli_missing: t("本机未找到对应 CLI。"), cli_check_failed: t("CLI 检查未成功，接入状态尚未确认。"),
      auth_unconfirmed: t("当前认证方式尚未确认为已有订阅。"), check_timeout: t("接入检查超时，状态尚未确认。"),
      check_failed: t("接入检查未取得可确认的结果。"),
      connection_not_configured: t("已保存工具模板，还没有配置本机连接器，不能参加任务。"),
      protocol_invalid: t("本机连接器未按豆窝 JSON 协议返回，尚未接通。"),
      connector_not_ready: t("连接器尚未就绪，不能参加任务。"),
    };
    const connection = element('details', '', 'member-details');
    connection.dataset.detailKind = 'connection'; connection.open = expanded.get(partner.id)?.get('connection') || false;
    const connectionSummary = element('summary', t("接入详情")); connectionSummary.dataset.focusKey = 'connection';
    connection.append(connectionSummary);
    if (Object.hasOwn(healthReasons, health.reason)) connection.append(element('p', healthReasons[health.reason], 'detail-caption'));
    const facts = element('dl', '', 'monitor-facts');
    const fact = (label, value) => facts.append(element('dt', label), element('dd', value));
    fact(t("CLI 版本"), typeof health.version === 'string' ? health.version : t("不可用"));
    if (typeof partner.installation?.executable === 'string') fact(t("使用中的 CLI"), partner.installation.executable);
    fact(t("认证方式"), typeof health.auth?.method === 'string' ? health.auth.method : t("未确认"));
    fact(t("最近检查"), Number.isFinite(health.checked_at) ? timeLabel(health.checked_at, true) : t("尚未检查"));
    const monitor = partner.monitor || {};
    fact(t("最近活动"), Number.isFinite(monitor.latest_activity) ? timeLabel(monitor.latest_activity, true) : t("尚无登记活动"));
    fact(t("会话数"), Number.isSafeInteger(monitor.session_count) ? String(monitor.session_count) : t("不可用"));
    if (monitor.last_error) fact(t("最近异常"), executionMessage(monitor.last_error));
    connection.append(facts);
    const currentTasks = Array.isArray(monitor.current_tasks) ? monitor.current_tasks : [];
    const recordOnly = !currentTasks.length && ['needs_user', 'unknown'].includes(monitor.status);
    const overview = recordOnly
      ? (monitor.status === 'needs_user' ? t("有历史任务待查看") : t("有任务记录状态待核对"))
      : statusLabels[statusClass(monitor.status)];
    card.append(element('p', t("豆窝内：{p0}", { p0: overview }), 'monitor-overview'));
    const tasks = element('ul', '', 'monitor-tasks');
    for (const task of currentTasks) {
      const line = element('li');
      if (uuidPattern.test(task.room_id)) {
        const goal = typeof task.room_goal === 'string' ? task.room_goal : '';
        const button = element('button', goal ? roomTitle({ goal }) : t("查看目标"), 'task-room-link');
        button.title = goal || t("查看目标");
        button.type = 'button';
        button.dataset.focusKey = `task:${task.room_id}`;
        button.addEventListener('click', () => { if (!state.busy) { closeManager(); void selectRoom(task.room_id); } });
        line.append(button);
      }
      line.append(element('span', `${statusLabels[statusClass(task.status)]} · ${monitorTaskText(task)}`));
      tasks.append(line);
    }
    if (!tasks.children.length) tasks.append(element('li', recordOnly
      ? (monitor.status === 'needs_user' ? t("现在没有在做的任务；等你处理的事在任务记录里。")
        : t("现在没有在做的任务；以往情况可以在任务记录里看。"))
      : t("现在没有在处理的目标")));
    const usage = element('details', '', 'member-details');
    usage.dataset.detailKind = 'usage'; usage.open = expanded.get(partner.id)?.get('usage') || false;
    const fields = element('dl', '', 'usage-fields'); renderUsage(fields, monitor.latest_usage);
    const summary = element('summary', t("任务 Token 快照")); summary.dataset.focusKey = 'usage';
    usage.append(summary, fields);
    const actions = element('div', '', 'manager-card-actions');
    const check = element('button', t("检查连接"), 'quiet-button'); check.type = 'button'; check.disabled = state.managementBusy || !activeEntry(partner);
    check.dataset.focusKey = 'check';
    check.addEventListener('click', () => void manageMutation(workspaceApi.partner(partner.id, 'check'), {}, t("接入检查已更新，没有启动模型。")));
    const toggle = element('button', state.partnerLifecycleSupported ? partner.enabled === false ? t("启用") : t("停用") : partner.enabled === false ? t("启用到新任务") : t("停用于新任务"), 'quiet-button'); toggle.type = 'button'; toggle.disabled = state.managementBusy || !activeEntry(partner) || state.partnerLifecycleSupported && !!pendingPartnerOperation(partner.id);
    toggle.dataset.focusKey = 'enabled';
    toggle.addEventListener('click', () => {
      if (toggle.disabled) return;
      if (state.partnerLifecycleSupported) void mutatePartnerOperation(partner.id, partner.enabled === false ? 'enable' : 'disable');
      else void manageMutation(workspaceApi.partner(partner.id, 'enabled'), { enabled: partner.enabled === false }, t("已更新新任务中的可选伙伴；已有房间成员不变。"));
    });
    if (activeEntry(partner)) actions.append(check, toggle);
    actions.append(lifecycleActions('partners', partner));
    const operation = latestPartnerOperation(partner.id);
    if (state.partnerLifecycleSupported && operation && !partnerOperationSettled(operation)) card.append(element('p', partnerOperationText(operation), 'detail-caption'));
    if (activeEntry(partner) && partner.connection_state === 'not_configured') {
      const configure = element('button', t("配置本机连接器"), 'quiet-button'); configure.type = 'button'; configure.dataset.focusKey = 'configure';
      configure.disabled = state.managementBusy || !!state.pendingPartner;
      configure.addEventListener('click', () => beginConnection(partner));
      actions.append(configure);
      card.append(element('p', t("工具模板已保存 · 待配置，暂不能参加任务"), 'connection-caption'));
    } else if (partner.connection_state === 'configured') {
      card.append(element('p', partner.selectable ? t("本机连接器已检查，可按已登记能力参加任务") : t("连接配置已保存，需安装并通过检查后才能参加任务"), 'connection-caption'));
    }
    const accountDetails = accountFooter(partner.id, { scope: 'manager' }); accountDetails.open = expanded.get(partner.id)?.get('account') || false;
    card.append(tasks, connection, usage, actions, accountDetails);
    (isAdditionalSession(partner) ? $('additional-partners-list') : $('partners-list')).append(card);
    if (focus?.partner === partner.id) {
      const queue = [...card.children];
      while (queue.length) {
        const node = queue.shift();
        if (node.dataset.focusKey === focus.key) { node.focus({ preventScroll: true }); break; }
        queue.push(...node.children);
      }
    }
  }
  if (state.partnerLifecycleSupported) {
    const removed = state.partners.filter((partner) => lifecycleState(partner) === 'removed');
    if (removed.length && state.lifecycleViews.partners === 'active') {
      const review = element('details', '', 'legacy-partner-review');
      review.append(element('summary', t("整理旧移除配置（{p0}）", { p0: removed.length })));
      for (const partner of removed) {
        const row = element('article', '', 'manager-card'); row.append(element('h3', partner.name), lifecycleActions('partners', partner)); review.append(row);
      }
      $('partners-list').append(review);
    }
    for (const operation of state.partnerOperations.values()) if (!state.partners.some((partner) => partner.id === operation.provider)
        && (operation.status !== 'completed' || operation.error || operation.targets?.some((target) => !['done', 'superseded'].includes(target.state)))) {
      $('partners-list').append(element('p', `${displayName(operation.provider)}${interfaceColon()}${partnerOperationText(operation)}`, 'detail-caption'));
    }
  }
}

async function refreshManager() {
  const results = await Promise.allSettled([api(workspaceApi.projects), api(workspaceApi.partners)]);
  if (results[0].status === 'fulfilled') acceptProjects(results[0].value);
  if (results[1].status === 'fulfilled') acceptPartners(results[1].value);
  const failed = results.find((result) => result.status === 'rejected');
  if (failed) managerNotice(failed.reason.message, true);
}

async function discoverOnOpen({ reopen = false, manual = false } = {}) {
  if (!state.connectionProtocol || state.discoveryInFlight || (!reopen && state.discoveryOpened)) return null;
  if (!manual && reopen && Number.isFinite(state.discoveryLastOpened) && Date.now() - state.discoveryLastOpened < 1000) return null;
  state.discoveryOpened = true;
  state.discoveryInFlight = true;
  renderDiscoveryControls();
  if (manual) {
    $('discovery-feedback').hidden = false;
    $('discovery-feedback').textContent = t("正在检查已支持工具的安装位置，不会启动模型…");
  }
  try {
    acceptPartners(await api(`${workspaceApi.partners}/discover`, 'POST', {}));
    const status = await api('/api/status');
    setCatalog(status.agents);
    renderMembers(state.snapshot);
    if (manual) $('discovery-feedback').textContent = state.discovery?.state === 'completed'
      ? t("安装观察已更新{p0}；没有启动模型。", { p0: Number.isFinite(state.discovery.observed_at) ? interfaceParen(timeLabel(state.discovery.observed_at, true)) : '' })
      : t("已刷新工具记录；安装观察尚未确认，没有启动模型。");
    return null;
  } catch (_error) {
    if (manual) $('discovery-feedback').textContent = t("这次检测未完成，已保存设置保持不变。可以稍后重新检测。");
    return t("这次打开未能更新工具发现；已保存设置保持不变，可在伙伴与额度中查看。");
  } finally {
    state.discoveryInFlight = false; state.discoveryLastOpened = Date.now();
    renderDiscoveryControls();
  }
}

function openManager(view, trigger = document.activeElement) {
  if (!state.manager) state.managerReturnFocus = state.taskOptions?.trigger || trigger;
  closeTaskOptions({ restoreFocus: false });
  closeRuntimePopover({ restoreFocus: false });
  closeUsagePopover();
  closeContextMenu();
  state.manager = view;
  $('workspace-manager').hidden = false; syncModalInert();
  $('projects-view').hidden = view !== 'projects'; $('partners-view').hidden = view !== 'partners';
  $(`${view}-view`).scrollTop = 0;
  $('manager-title').textContent = view === 'projects' ? t("带项目进小窝") : t("伙伴与额度");
  $('manager-projects-tab').setAttribute('aria-pressed', String(view === 'projects'));
  $('manager-partners-tab').setAttribute('aria-pressed', String(view === 'partners'));
  managerNotice(); renderNativeSetup();
  void refreshManager().catch((error) => managerNotice(error.message, true));
  $('close-manager').focus();
  syncAccountWatchTimer(); void refreshWatchingAccounts();
}

function closeManager() {
  if (!state.manager) return;
  const trigger = state.managerReturnFocus; const fallback = $(state.manager === 'partners' ? 'open-partners' : 'open-projects');
  closeContextMenu(); state.manager = null; state.managerReturnFocus = null;
  $('workspace-manager').hidden = true; syncModalInert(); syncAccountWatchTimer(); syncNativeLoginTimer();
  const visible = trigger?.isConnected !== false && !trigger?.hidden && !trigger?.disabled
    && (typeof trigger?.getClientRects !== 'function' || trigger.getClientRects().length > 0);
  (visible ? trigger : fallback)?.focus({ preventScroll: true });
}


async function manageMutation(path, body, success) {
  if (state.managementBusy || state.busy || state.pending) return false;
  state.managementBusy = true; renderManagementControls();
  managerNotice(t("正在保存或检查…"));
  try {
    const result = await api(path, 'POST', body);
    if (Array.isArray(result.projects)) acceptProjects(result);
    if (Array.isArray(result.partners)) acceptPartners(result);
    const expectedPartner = path === workspaceApi.partners ? body : path.endsWith('/connection') ? state.pendingPartner : null;
    if (expectedPartner && !state.partners.some((partner) => samePartner(partner, expectedPartner))) {
      throw new RequestError(t("伙伴保存记录尚未确认，请先核对。"), { uncertain: true });
    }
    await refresh();
    managerNotice(success);
    return true;
  } catch (error) {
    if ((path === workspaceApi.partners || path.endsWith('/connection')) && error.uncertain) state.partnerSaveUncertain = true;
    managerNotice(error.uncertain ? t("结果尚未确认。请刷新管理记录核对，不要重复添加；系统不会自动重试。") : error.message, true);
    await refreshManager().catch(() => {});
    return false;
  } finally { state.managementBusy = false; renderManagementControls(); }
}

function renderManagementControls() {
  const blocked = state.managementBusy || state.busy || !!state.pending;
  for (const id of ['save-project', 'project-name', 'project-path', 'project-files-input', 'project-explicit-files']) $(id).disabled = blocked || state.projectChoosing;
  $('save-project').disabled = blocked || state.projectChoosing || !['project-name', 'project-path'].every((id) => $(id).value.trim()) || ($('project-explicit-files').checked && !$('project-files-input').value.trim());
  for (const id of ['save-partner', 'partner-id', 'partner-name', 'partner-avatar', 'partner-adapter']) $(id).disabled = blocked || !!state.pendingPartner;
  if (state.configuringPartnerId) for (const id of ['partner-id', 'partner-name', 'partner-avatar', 'partner-adapter']) $(id).disabled = true;
  for (const id of ['connection-executable', 'connection-args', 'connection-billing']) $(id).disabled = blocked || !!state.pendingPartner;
  $('partner-save-pending').hidden = !state.pendingPartner;
  $('verify-partner-save').disabled = blocked || state.partnerVerifying;
  const adapter = $('partner-adapter').value || 'claude';
  const connector = state.connectionTemplates.some((template) => template.adapter === adapter);
  $('partner-connection').hidden = !connector;
  $('cancel-connection').hidden = !state.configuringPartnerId;
  $('cancel-connection').disabled = blocked || !!state.pendingPartner;
  $('save-partner').textContent = state.configuringPartnerId ? t("保存连接配置") : connector ? t("保存工具") : t("创建会话");
  $('partner-save-help').textContent = connector ? t("可以先保存待配置工具；没有连接器或未通过检查时，不会加入可选伙伴，也不会启动模型。")
    : t("新会话使用所选工具现有的登录。创建后不会开始任务，也不会自动参加其它任务。");
  $('partner-name').setAttribute('placeholder', t("{p0}（可留空）", { p0: defaultPartnerName(adapter) }));
  $('partner-avatar').setAttribute('placeholder', t("自动使用 {p0}", { p0: defaultPartnerAvatar(adapter) }));
  const nativePicker = typeof window.beanestDesktop?.chooseProjectFiles === 'function';
  $('choose-project-files').hidden = !nativePicker;
  $('choose-project-files').disabled = blocked || state.projectChoosing;
  const folderPicker = typeof window.beanestDesktop?.chooseProjectFolder === 'function';
  $('choose-project-folder').hidden = !folderPicker;
  $('choose-project-folder').disabled = blocked || state.projectChoosing;
  $('project-picker-help').textContent = folderPicker ? t("选一个项目文件夹，再点添加项目。无需逐个选择文件。") : t("桌面版可直接选择文件夹；这里可展开手动填写项目目录。");
  renderGlobalPartners();
  renderBeanStylePicker();
}

function defaultPartnerName(adapter) {
  const base = adapter === 'codex' ? t("Codex 会话") : adapter === 'claude' ? t("Claude 会话")
    : t("{p0} 工具", { p0: state.adapters.find((item) => item.id === adapter)?.name || t("本机工具") });
  const taken = new Set(state.partners.map((partner) => partner.name));
  if (!taken.has(base)) return base;
  let suffix = 2;
  while (taken.has(`${base}${suffix}`)) suffix += 1;
  return `${base}${suffix}`;
}

function defaultPartnerAvatar(adapter) {
  return adapter === 'codex' ? 'X' : adapter === 'claude' ? 'C' : [...(state.adapters.find((item) => item.id === adapter)?.name || t("本"))][0];
}

function generatedPartnerId(adapter) {
  const prefix = adapter.slice(0, 16);
  const suffixLength = Math.min(24, 31 - prefix.length);
  return `${prefix}-${crypto.randomUUID().replaceAll('-', '').slice(0, suffixLength)}`;
}

function readConnection() {
  const executable = $('connection-executable').value.trim();
  const args = $('connection-args').value.split(/\r?\n/).filter((value) => value.length > 0);
  const billing = $('connection-billing').value;
  if (!executable && !args.length) return null;
  if (!executable.startsWith('/') || executable.length > 4096 || /[\u0000-\u001f]/.test(executable)
      || args.length > 16 || args.some((arg) => arg.length > 512 || /[\u0000-\u001f]/.test(arg))
      || !['existing_subscription', 'local'].includes(billing)) throw new RequestError(errorLabels.invalid_partner_connection);
  return { protocol: 'beanest-json-v1', executable, args, billing };
}

function fillConnection(connection) {
  $('connection-executable').value = connection?.executable || '';
  $('connection-args').value = Array.isArray(connection?.args) ? connection.args.join('\n') : '';
  $('connection-billing').value = connection?.billing || '';
}

function samePartner(partner, expected) {
  if (partner.id !== expected.id || !['name', 'avatar', 'adapter', 'bean_style'].every((key) => partner[key] === expected[key])) return false;
  if (expected.connection === undefined) return true;
  const actual = partner.connection;
  return expected.connection === null ? actual === null : !!actual
    && ['protocol', 'executable', 'billing'].every((key) => actual[key] === expected.connection[key])
    && JSON.stringify(actual.args) === JSON.stringify(expected.connection.args);
}

function beginConnection(partner) {
  if (state.pendingPartner || state.managementBusy || state.busy || state.pending) return;
  state.configuringPartnerId = partner.id;
  $('partner-id').value = partner.id; $('partner-name').value = partner.name;
  $('partner-avatar').value = partner.avatar; $('partner-adapter').value = partner.adapter;
  state.selectedBeanStyle = beanStyle(partner.id, partner); state.beanStyleChosen = true;
  fillConnection(partner.connection);
  $('partner-add-details').open = true; $('partner-connection').open = true;
  renderManagementControls(); $('connection-executable').focus();
}

function rememberPendingPartner() {
  try {
    if (state.pendingPartnerScopeChanged) return;
    if (state.pendingPartner && state.authScope) sessionStorage.setItem(pendingPartnerStorageKey,
      JSON.stringify({ scope: state.authScope, body: state.pendingPartner, configuring: state.configuringPartnerId }));
    else sessionStorage.removeItem(pendingPartnerStorageKey);
  } catch (_error) { /* The current window still retains the same pending identity. */ }
}

function clearPartnerForm() {
  for (const id of ['partner-id', 'partner-name', 'partner-avatar']) $(id).value = '';
  fillConnection(null); state.configuringPartnerId = null;
  // The look follows the chosen tool until the user picks one themselves.
  state.selectedBeanStyle = defaultBeanStyle($('partner-adapter').value); state.beanStyleChosen = false;
  state.pendingPartner = null; state.partnerSaveUncertain = false;
  state.pendingPartnerScopeChanged = false;
  rememberPendingPartner(); renderManagementControls();
}

function restorePendingPartner() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(pendingPartnerStorageKey) || 'null');
    if (!saved) return;
    const body = saved.body;
    if (!body || !agentPattern.test(body.id) || typeof body.name !== 'string' || !body.name.trim() || body.name.length > 80
        || typeof body.avatar !== 'string' || !body.avatar.trim() || [...body.avatar].length > 2
        || !agentPattern.test(body.adapter) || !beanStyleIds.has(body.bean_style)) throw new Error();
    state.pendingPartner = body; state.partnerSaveUncertain = true;
    state.pendingPartnerScopeChanged = saved.scope !== state.authScope;
    $('partner-id').value = body.id; $('partner-name').value = body.name; $('partner-avatar').value = body.avatar;
    $('partner-adapter').value = body.adapter; state.selectedBeanStyle = body.bean_style; state.beanStyleChosen = true;
    state.configuringPartnerId = saved.configuring === body.id ? body.id : null;
    fillConnection(body.connection);
  } catch (_error) {
    try { sessionStorage.removeItem(pendingPartnerStorageKey); } catch (_ignored) { /* Storage can be unavailable. */ }
  }
}

async function verifyPendingPartner() {
  if (!state.pendingPartner || state.partnerVerifying || state.managementBusy) return;
  state.partnerVerifying = true; renderManagementControls();
  try {
    const result = await api(workspaceApi.partners); acceptPartners(result);
    const body = state.pendingPartner;
    const found = state.partners.find((partner) => partner.id === body.id);
    const matches = found && samePartner(found, body);
    if (matches && !state.pendingPartnerScopeChanged) {
      clearPartnerForm(); await refresh(); managerNotice(t("已从保存记录确认会话创建成功。"));
    } else managerNotice(state.pendingPartnerScopeChanged
      ? t("服务连接已经改变，不能用新连接确认旧添加。原信息仍保留，没有重新发送。")
      : t("暂未找到完全一致的保存记录，结果仍待确认。原信息已保留，没有重新发送。"), true);
  } catch (error) { managerNotice(error.message, true); }
  finally { state.partnerVerifying = false; renderManagementControls(); }
}
