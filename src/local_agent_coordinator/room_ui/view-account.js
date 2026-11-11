'use strict';

// Quota labels on partner cards and the provider account panels.

// Partner cards show the account-wide quota windows as bars; model-specific
// windows stay in the detailed account panel.
function quotaShortLabel(window) {
  const named = ({ 'kimi:limit5h': t("5 小时"), 'kimi:limit7d': t("7 天"), 'kimi:monthTotal': t("本月"), 'kimi:monthCode': t("本月代码") })[window.name];
  if (named) return named;
  const minutes = window.window_minutes;
  if (!Number.isFinite(minutes) || minutes <= 0) return t("额度");
  return minutes % 1440 === 0 ? t("{p0} 天", { p0: minutes / 1440 }) : minutes % 60 === 0 ? t("{p0} 小时", { p0: minutes / 60 }) : t("{p0} 分钟", { p0: minutes });
}
function memberQuotaWindows(id) {
  const quota = currentAccount(id).account?.quota;
  const windows = (quota?.state === 'available' || quota?.stale === true) && Array.isArray(quota.windows) ? quota.windows : [];
  return windows.filter((window) => window && typeof window === 'object' && !window.model && !window.surface)
    .map((window) => {
      const remaining = percentValue(window.remaining_percent); const used = percentValue(window.used_percent);
      return { window, remaining: remaining !== null ? remaining : used !== null ? 100 - used : null, stale: quota.stale === true };
    }).filter((entry) => entry.remaining !== null).slice(0, 3);
}
function renderMemberWorkload(id, workload) {
  const holder = document.getElementById(`member-workload-${id}`);
  if (!holder) return;
  const account = workload?.accounts?.find((row) => row.members.includes(id));
  const share = !Number.isInteger(account?.actual_percent) ? ''
    : workload.configured ? t("本轮工作量 {p0}% · 设定 {p1}%", { p0: account.actual_percent, p1: account.target_percent })
      : t("本轮工作量 {p0}%", { p0: account.actual_percent });
  const role = copyRole(state.snapshot?.room?.runtime_settings?.members?.[id]?.role);
  const reserve = state.partners.find((partner) => partner.id === id)?.quota_reserve;
  const text = [role ? t("角色：{p0}", { p0: roleName(role) }) : '', share,
    Number.isFinite(reserve) ? t("额度只剩约 {p0}%，先派给别人", { p0: reserve }) : ''].filter(Boolean).join(' · ');
  if (holder.textContent !== text) holder.textContent = text;
}
function renderMemberQuota(id) {
  const holder = document.getElementById(`member-quota-${id}`);
  if (!holder) return;
  // Only a status in doubt outlines the card; running out of quota is an ordinary wait.
  const entries = memberQuotaWindows(id); const blocked = memberAvailabilityLine(availabilityInfo(id))?.tone === 'ask';
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), entries, blocked]);
  holder.parentNode?.classList.toggle('quota-blocked', blocked);
  if (holder.dataset.fingerprint === fingerprint) return;
  holder.dataset.fingerprint = fingerprint;
  holder.replaceChildren(...entries.map(({ window, remaining, stale }) => {
    const level = remaining <= 0 ? ' zero' : remaining < 20 ? ' low' : '';
    const row = element('div', '', `quota-row${level}`);
    const label = quotaShortLabel(window);
    const meter = element('progress'); meter.max = 100; meter.value = remaining; meter.textContent = `${remaining}%`;
    meter.setAttribute('aria-label', t("{p0}额度剩余 {p1}%", { p0: label, p1: remaining }));
    const reset = Number.isFinite(window.resets_at) && remaining < 20 ? t(" · {p0} 重置", { p0: timeLabel(window.resets_at, true) }) : '';
    const value = element('span', `${stale ? t("上次 ") : ''}${remaining}%${reset}`, 'quota-value');
    if (Number.isFinite(window.resets_at)) value.title = t("重置：{p0}", { p0: timeLabel(window.resets_at, true) });
    const name = element('span', label);
    const full = ({ 'kimi:monthTotal': t("Kimi Code · 月度总额度"), 'kimi:monthCode': t("Kimi Code · 月度代码额度") })[window.name];
    if (full) name.title = full;
    row.append(name, meter, value);
    return row;
  }));
}
function renderNavQuota() {
  const holder = document.getElementById('nav-quota');
  if (!holder) return;
  const values = state.participantIds.map((id) => {
    const blocked = availabilityInfo(id).blocked; const entries = memberQuotaWindows(id);
    return [id, blocked ? 0 : entries.length ? Math.min(...entries.map((entry) => entry.remaining)) : null];
  }).filter(([, value]) => value !== null);
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), values.map(([id, value]) => [id, displayName(id), value])]);
  if (holder.dataset.fingerprint === fingerprint) return;
  holder.dataset.fingerprint = fingerprint;
  holder.replaceChildren(...values.map(([id, value]) => {
    const meter = element('progress', `${value}%`, value <= 0 ? 'zero' : value < 20 ? 'low' : ''); meter.max = 100; meter.value = value;
    meter.title = t("{p0}：剩余 {p1}%", { p0: displayName(id), p1: value });
    return meter;
  }));
}

function currentAccount(id) {
  const partner = state.partners.find((entry) => entry.id === id);
  const value = partner?.account;
  return { partner, account: value && typeof value === 'object' && !Array.isArray(value) ? value : null };
}
function canRefreshAccount(partner, account) {
  return !!partner && activeEntry(partner) && account?.source !== 'unsupported' && account?.reason !== 'account_adapter_unsupported';
}
function accountEmail(account) {
  return typeof account?.email === 'string' && account.email.trim() ? account.email.trim().slice(0, 254) : null;
}
// Tools report plans as lower-case ids such as "max"; show them as the product names people know.
function planName(plan) {
  const known = { free: t("免费版"), pro: 'Pro', plus: 'Plus', max: 'Max', team: 'Team', enterprise: 'Enterprise', business: 'Business' };
  const key = plan.trim().toLowerCase();
  return Object.hasOwn(known, key) ? known[key] : /^[a-z]/.test(plan) ? plan[0].toUpperCase() + plan.slice(1) : plan;
}
function accountFieldText(account, field) {
  const value = field === 'email' ? accountEmail(account) : typeof account?.plan === 'string' && account.plan ? planName(account.plan) : null;
  if (value) return value;
  const reason = account?.field_reasons?.[field];
  const label = field === 'email' ? t("邮箱") : t("订阅");
  if (reason === 'provider_not_provided') return t("供应商未提供{p0}", { p0: label });
  if (reason === 'profile_query_unavailable') return t("本次未取得{p0}", { p0: label });
  if (reason === 'account_changed') return t("账号已变化，请重新查询");
  return field === 'email' ? t("邮箱未获取") : t("本次查询未提供");
}
function accountLoginText(account) {
  const email = accountEmail(account); if (email) return email;
  const display = account?.source === 'kimi_managed_oauth' ? account.display_identity : null;
  const nickname = typeof display?.nickname === 'string' && display.nickname.trim() ? display.nickname.trim().slice(0, 100) : null;
  if (typeof display?.masked_phone === 'string' && /^\+[0-9]{1,4} \*{4} [0-9]{4}$/.test(display.masked_phone)) {
    return t("手机号：{p0}{p1}", { p0: display.masked_phone, p1: nickname ? t(" · 昵称：{p0}", { p0: nickname }) : '' });
  }
  return nickname ? t("昵称：{p0}", { p0: nickname }) : accountFieldText(account, 'email');
}
function accountLoginCaption(account) {
  const text = accountLoginText(account);
  if (account?.stale) return t("上次确认：{p0} · 待刷新", { p0: text });
  return accountEmail(account) ? t("当前登录：{p0}", { p0: text }) : text;
}
function providerAccountReasons(reason) {
  return ({ account_check_failed: t("这次账号检查未成功，可以稍后刷新。"),
    account_check_timeout: t("这次检查超时，可以稍后刷新。"),
    auth_unconfirmed: t("当前登录尚未确认，请在对应工具中检查账号。"),
    account_adapter_unsupported: t("这个接入方式暂不提供账号与额度查询。"),
    account_changed: t("登录账号已变化，旧账号额度已清除。请重新查询当前账号。"),
    account_protocol_invalid: t("工具没有返回可确认的账号数据。"),
    account_read_unavailable: t("工具暂时无法提供账号信息。"),
    account_request_rejected: t("工具未接受这次账号查询，可以稍后再试。"),
    account_output_limit: t("工具返回的数据超出本次读取范围，无法确认账号信息。"),
    account_refresh_cancelled: t("本次查询已取消，保留上次快照。"),
    provider_usage_unavailable: t("本次额度查询未成功，可以稍后刷新；不代表额度已用完。"),
    provider_usage_auth_unconfirmed: t("这次额度查询未能确认授权，可刷新账号或在对应工具中核对登录。"),
    provider_usage_rate_limited: t("本次额度查询被限频，暂未取得新数据，可以稍后刷新。"),
    provider_usage_temporarily_unavailable: t("额度查询服务暂时不可用，可以稍后刷新。"),
    provider_usage_transport_error: t("本次额度查询连接未成功，可以稍后刷新。"),
    provider_usage_invalid_response: t("本次返回的数据无法确认额度，可以稍后重新查询。"),
    provider_usage_not_exposed: t("当前接入尚未取得订阅额度。"),
    provider_quota_not_supported: t("当前工具版本未提供可读取的额度，可到官方用量页查看。"),
    cli_missing: t("本机未找到对应工具，暂时无法查询。"),
  })[reason] || t("供应商未提供可确认的数据。");
}
function accountFooter(id, { scope = 'member', compact = false } = {}) {
  const details = element('details', '', `provider-account${compact ? ' compact-account' : ''}`); details.id = `${scope === 'member' ? '' : `${scope}-`}provider-account-${id}`;
  details.dataset.compact = String(compact);
  details.dataset.detailKind = 'account';
  const summary = element('summary'); summary.dataset.focusKey = 'account';
  summary.append(element('span', t("账号与额度")), element('span', '', 'quota-summary'));
  const body = element('div', '', 'provider-account-body');
  details.append(summary, body);
  details.addEventListener('toggle', () => trackAccountPanel(details, id));
  updateAccountFooter(details, id);
  return details;
}
function trackAccountPanel(details, id) {
  if (details.open) state.accountPanels.set(details.id, { details, id });
  else if (state.accountPanels.get(details.id)?.details === details) state.accountPanels.delete(details.id);
  syncAccountWatchTimer();
  if (details.open) void refreshWatchingAccounts();
}
// Shown to at most one decimal: a provider's 57.99999999999999 is 58, and a
// value just above 0 or below 100 never reads as empty or full.
function percentValue(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 100) return null;
  const shown = Math.round(value * 10) / 10;
  return shown === 0 && value > 0 ? 0.1 : shown === 100 && value < 100 ? 99.9 : shown;
}
// "5小时" / "5小时额度" as whole keys; stripping 窗口 from a translated label failed in English.
function quotaWindowName(minutes, quota = false) {
  if (!Number.isFinite(minutes) || minutes <= 0) return '';
  if (minutes % 1440 === 0) return quota ? t("{p0}天额度", { p0: minutes / 1440 }) : t("{p0}天", { p0: minutes / 1440 });
  if (minutes % 60 === 0) return quota ? t("{p0}小时额度", { p0: minutes / 60 }) : t("{p0}小时", { p0: minutes / 60 });
  return quota ? t("{p0}分钟额度", { p0: minutes }) : t("{p0}分钟", { p0: minutes });
}
function quotaWindowLabel(minutes) {
  if (!Number.isFinite(minutes) || minutes <= 0) return '';
  return minutes % 1440 === 0 ? t("{p0}天窗口", { p0: minutes / 1440 }) : minutes % 60 === 0 ? t("{p0}小时窗口", { p0: minutes / 60 }) : t("{p0}分钟窗口", { p0: minutes });
}
function updateAccountFooter(details, id) {
  const { partner, account } = currentAccount(id);
  const compact = details.dataset.compact === 'true';
  const quota = account?.quota;
  const windows = (quota?.state === 'available' || quota?.stale === true) && Array.isArray(quota.windows) ? quota.windows.filter((entry) => entry && typeof entry === 'object') : [];
  const status = state.accountRefreshId === id ? t("正在查询…") : !account || account.state === 'not_checked' ? t("未检查")
    : windows.length ? t("{p0} 个额度窗口{p1}", { p0: windows.length, p1: quota.stale ? t(" · 上次记录") : '' }) : t("本次未取得额度");
  details.firstChild.lastChild.textContent = status;
  const body = details.lastChild;
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), [account, partner?.workload_level, state.workloadSavingId, state.workloadRevision, partner?.account_source, partnerWorking(partner), state.accountSourceBusy, state.accountSourceErrors.get(id), partner?.lifecycle_state, state.accountRefreshErrors.get(id), state.accountAuto.get(id) !== false, state.accountRefreshId, state.managementBusy, state.busy, !!state.pending, partner?.availability, partner?.quota_reserve, availabilityInfo(id).stale, state.availabilityError, state.availabilityRecheck?.id]]);
  if (body.dataset.fingerprint === fingerprint) return;
  body.dataset.fingerprint = fingerprint;
  body.replaceChildren();
  body.append(element('h4', t("供应商账号额度")), element('p', t("这里查看账号订阅限制；任务 Token 在“任务会话与 Token”中单独显示。"), 'detail-caption'));
  const facts = element('dl', '', 'monitor-facts');
  const fact = (label, value) => facts.append(element('dt', label), element('dd', value));
  fact(account?.stale ? t("上次确认登录") : t("当前登录"), accountLoginText(account));
  fact(t("订阅"), accountFieldText(account, 'plan'));
  fact(t("认证"), typeof account?.auth_method === 'string' && account.auth_method ? account.auth_method : t("未提供"));
  fact(t("额度范围"), quota?.scope === 'provider_account' ? t("账号共享额度，未按单模型拆分")
    : quota?.scope === 'provider_account_and_model' ? t("账号共享与模型专属额度分别列示，不能相加")
      : quota?.scope === 'model' ? t("模型专属额度，不代表账号总余额") : t("供应商未说明额度范围"));
  fact(t("最近检查"), Number.isFinite(account?.checked_at) ? `${timeLabel(account.checked_at, true)}${account.stale ? t(" · 旧快照") : ''}` : t("尚未检查"));
  const sourceLabels = { kimi_managed_oauth: t("Kimi Code 本机会员登录"), codex_app_server: t("Codex 原生账号查询"), claude_auth_status: t("Claude Code 登录状态"), unsupported: t("当前接入方式未支持") };
  fact(t("账号来源"), sourceLabels[account?.source] || t("尚未获取"));
  const loginSource = accountLoginSource(partner);
  if (loginSource) fact(t("登录方式"), loginSource.kind === 'profile_home' ? t("豆窝独立登录") : t("本机登录"));
  if (compact) {
    body.append(element('p', `${account?.stale ? t("上次确认") : t("当前登录")}${interfaceColon()}${accountLoginText(account)}`, 'compact-account-identity'),
      element('p', `${accountFieldText(account, 'plan')} · ${Number.isFinite(account?.checked_at) ? `${timeLabel(account.checked_at, true)}${account.stale ? t(" · 旧快照") : ''}` : t("尚未查询")}`, 'detail-caption'));
    const more = element('details', '', 'compact-account-more'); more.append(element('summary', t("来源与账号范围")), facts); body.append(more);
  } else body.append(facts);
  const availability = availabilityInfo(id);
  if (availability.value && (availability.blocked || !(['error', 'unavailable'].includes(quota?.state) && quota?.reason)))
    body.append(element('p', availabilityText(availability), `availability-note${availability.blocked ? ' blocked' : ''}`));
  const recheck = availabilityRecheckControl(id); if (recheck) body.append(recheck);
  if (Number.isFinite(partner?.quota_reserve) && !availability.blocked)
    body.append(element('p', t("额度只剩约 {p0}%，豆窝先把活派给其他伙伴，没人能接时才用它。", { p0: partner.quota_reserve }), 'availability-note quota-reserve'));
  if (state.accountRefreshErrors.has(id)) body.append(element('p', state.accountRefreshErrors.get(id), 'quota-unavailable'));
  if (account?.state === 'error' && account.reason && account.reason !== (quota?.reason || account.reason))
    body.append(element('p', t("账号检查：{p0}", { p0: providerAccountReasons(account.reason) }), 'quota-unavailable'));
  if (windows.length && (quota?.state === 'error' || account?.state === 'error'))
    body.append(element('p', providerAccountReasons(quota?.reason || account?.reason), 'quota-unavailable'));
  if (windows.length) {
    for (const window of windows) {
      const row = element('section', '', 'quota-window');
      const remaining = percentValue(window.remaining_percent); const used = percentValue(window.used_percent);
      const duration = quotaWindowLabel(window.window_minutes);
      const rawName = typeof window.name === 'string' && window.name.trim() ? window.name : '';
      const pool = rawName.match(/^([a-zA-Z0-9_-]+):(primary|secondary)$/);
      const kimiWindow = ({ 'kimi:limit5h': t("Kimi Code · 5小时额度"), 'kimi:limit7d': t("Kimi Code · 7天额度"), 'kimi:monthTotal': t("Kimi Code · 月度总额度"), 'kimi:monthCode': t("Kimi Code · 月度代码额度") })[rawName];
      // adapters.py names Claude's windows in Chinese; known names are translated here like Kimi's.
      const claudeModel = /^claude:model_scoped:/.test(rawName) && typeof window.label === 'string' ? window.label.split(' · ')[0].trim() : '';
      const claudeWindow = ({ 'claude:five_hour': t("Claude · 5小时窗口"), 'claude:seven_day': t("Claude · 7天窗口") })[rawName]
        || (claudeModel ? t("{p0} · 模型周额度", { p0: claudeModel }) : '');
      const label = kimiWindow || claudeWindow || (typeof window.label === 'string' && window.label.trim() ? window.label.trim().slice(0, 200)
        : pool ? t("{p0} 额度池 · {p1}", { p0: ({ codex: 'Codex', claude: 'Claude Code' })[pool[1]] || pool[1], p1: duration || (pool[2] === 'primary' ? t("主窗口") : t("第二窗口")) })
        : rawName || duration || t("供应商额度窗口"));
      const heading = element('div', '', 'quota-window-heading');
      heading.append(element('strong', label), element('span', remaining !== null ? t("剩余 {p0}%", { p0: remaining }) : used !== null ? t("已用 {p0}%", { p0: used }) : t("比例未提供")));
      row.append(heading);
      if (remaining !== null || used !== null) {
        const meter = element('progress'); meter.max = 100; meter.value = remaining !== null ? remaining : used;
        meter.setAttribute('aria-label', t("{p0} · {p1}比例", { p0: label, p1: remaining !== null ? t("剩余") : t("已用") }));
        meter.textContent = `${meter.value}%`;
        row.append(meter);
      }
      const reset = Number.isFinite(window.resets_at) ? t("重置：{p0}", { p0: timeLabel(window.resets_at, true) }) : t("重置时间未提供");
      const scope = window.scope === 'model' || window.model ? t("模型专属{p0}", { p0: typeof window.model === 'string' && window.model ? ` · ${window.model.slice(0, 120)}` : '' })
        : window.surface ? t("场景专属额度") : window.scope === 'provider_account' || quota.scope === 'provider_account' ? t("账号共享") : t("范围待确认");
      row.append(element('p', `${scope} · ${duration ? `${duration} · ` : ''}${reset}`, 'detail-caption'));
      body.append(row);
    }
    const quotaSources = { kimi_code_web_oauth_usage: t("Kimi Code 本机会员登录额度查询"), claude_sdk_get_usage: t("Claude Code 原生额度查询"), codex_app_server_account_rate_limits: t("Codex 原生额度查询") };
    body.append(element('p', t("额度来源：{p0}", { p0: quotaSources[quota.source] || t("供应商额度记录") }), 'detail-caption'));
    body.append(element('p', t("额度更新：{p0}{p1}", { p0: Number.isFinite(quota.checked_at) ? timeLabel(quota.checked_at, true) : t("未提供"), p1: quota.stale ? t(" · 旧快照，请刷新") : '' }), 'detail-caption'));
  } else {
    body.append(element('p', !account || account.state === 'not_checked' ? t("还没有检查这个账号。点下方刷新，只查询登录和额度，不会启动模型任务。")
      : providerAccountReasons(quota?.reason || account.reason), 'quota-unavailable'));
  }
  if (quota?.source === 'kimi_code_web_oauth_usage' && !windows.some((window) => window.name === 'kimi:monthTotal')) body.append(element('p', t("月度总额度：本次查询未提供。5小时或7天窗口不代表月度总余额。"), 'detail-caption'));
  const usageLink = nativeSetupLink(t("官方用量页"), quota?.usage_url, (message) => showNotice(message, 'error'));
  if (usageLink) { usageLink.dataset.usageLink = id; body.append(usageLink, element('p', quota.usage_url, 'native-login-url')); }
  const shared = Array.isArray(account?.shared_profile_ids) ? account.shared_profile_ids.filter((value) => value !== id && agentPattern.test(value)) : [];
  const tool = ({ claude: 'Claude Code', codex: 'Codex', kimi: 'Kimi Code' })[partner?.adapter] || t("对应工具");
  const ownHome = loginSource?.kind === 'profile_home';
  body.append(element('p', ownHome ? t("使用豆窝为它单独保存的 {p0} 登录，不影响本机登录和其他伙伴。", { p0: tool })
    : shared.length ? t("与{p0}共用本机 {p1} 登录。改名或新建会话不会切换账号。", { p0: interfaceList(shared.map(displayName)), p1: tool })
    : account?.scope === 'default_cli_login' || ['claude', 'codex', 'kimi'].includes(partner?.adapter)
      ? t("使用本机 {p0} 的当前登录。改名或新建会话不会切换账号。", { p0: tool }) : t("当前接入方式未提供独立账号信息。"), 'account-scope-note'));
  if (ownHome && !accountSignedIn(account) && typeof loginSource.login_command === 'string') body.append(...accountLoginCommand(loginSource.login_command));
  const refresh = element('button', state.accountRefreshId === id ? t("正在查询…") : t("刷新账号与额度"), 'quiet-button'); refresh.type = 'button'; refresh.dataset.focusKey = 'account-refresh';
  refresh.disabled = !canRefreshAccount(partner, account) || !!state.accountRefreshId || state.managementBusy || state.busy || !!state.pending;
  refresh.addEventListener('click', () => void refreshProviderAccount(id));
  const auto = element('label', '', 'account-auto-refresh');
  const checkbox = element('input'); checkbox.type = 'checkbox'; checkbox.checked = state.accountAuto.get(id) !== false;
  checkbox.dataset.focusKey = 'account-auto'; checkbox.disabled = !canRefreshAccount(partner, account);
  checkbox.setAttribute('aria-label', t("展开时每分钟更新 {p0} 的账号与额度", { p0: displayName(id) }));
  checkbox.addEventListener('change', () => {
    state.accountAuto.set(id, checkbox.checked);
    syncAccountWatchTimer();
    if (checkbox.checked) void refreshWatchingAccounts();
  });
  auto.append(checkbox, element('span', t("展开时每分钟更新")));
  body.append(refresh, auto);
  if (loginSource) body.append(...accountSourceControls(id, partner, loginSource));
  if (partner) body.append(workloadControl(id, partner));
  if (!compact) body.append(element('p', availabilityWatchSupported()
    ? t("只查询登录与额度，不启动模型。面板收起后停止单独刷新；前台仍每分钟检查可用状态，页面隐藏后暂停。")
    : t("只查询登录与额度，不启动模型。面板收起或页面隐藏后停止自动查询。"), 'detail-caption'));
}
// How much this account takes on; the setting belongs to the account, so aliases change together.
function workloadControl(id, partner) {
  const row = element('label', '', 'account-workload');
  const select = element('select'); select.dataset.focusKey = 'account-workload';
  select.disabled = state.workloadSavingId === id || state.managementBusy;
  for (const [value, label] of [['low', t("少")], ['normal', t("中")], ['high', t("多")]]) {
    const option = element('option', label); option.value = value; select.append(option);
  }
  select.value = partner.workload_level || 'normal';
  select.setAttribute('aria-label', t("{p0}所用账号的工作量", { p0: displayName(id) }));
  select.addEventListener('change', () => void saveWorkload(id, select.value));
  row.append(element('span', t("工作量")), select);
  // A light confirmation next to the choice; it clears itself a few seconds later.
  const saving = state.workloadSavingId === id; const saved = state.workloadSaved?.id === id;
  const status = element('span', saving ? t("正在保存…") : saved ? t("已保存") : '', `workload-status${saved ? ' saved' : ''}`);
  status.setAttribute('role', 'status'); status.hidden = !saving && !saved;
  row.append(status);
  const holder = element('div', '', 'account-workload-setting');
  holder.append(row, element('p', t("额度高的账号选多，低的选少。使用同一账号的伙伴一起变化。用于分配审查和还没开工的任务；任务里单独设置时以任务为准。"), 'detail-caption'));
  return holder;
}
async function saveWorkload(id, level) {
  state.workloadSavingId = id; renderMembers(state.snapshot); renderGlobalPartners();
  try {
    acceptPartners(await api(workspaceApi.partner(id, 'workload'), 'POST', { level }));
    const saved = { id }; state.workloadSaved = saved;
    setTimeout(() => {
      if (state.workloadSaved !== saved) return;
      state.workloadSaved = null; state.workloadRevision = (state.workloadRevision || 0) + 1;
      renderMembers(state.snapshot); renderGlobalPartners();
    }, 3000);
  } catch (error) {
    state.workloadSaved = null;
    showNotice(error.message || t("工作量未确认保存，请重试。"), 'error');
  } finally {
    state.workloadSavingId = null; state.workloadRevision = (state.workloadRevision || 0) + 1;
    renderMembers(state.snapshot); renderGlobalPartners();
  }
}
function accountRefreshKey(id) {
  const partner = state.partners.find((entry) => entry.id === id);
  // A profile with its own login is its own account; aliases share the local one.
  return ['claude', 'codex'].includes(partner?.adapter) && accountLoginSource(partner)?.kind !== 'profile_home' ? `${partner.adapter}:default_cli_login` : id;
}
function accountLoginSource(partner) {
  const source = partner?.account_source;
  return source && typeof source === 'object' && ['local', 'profile_home'].includes(source.kind) ? source : null;
}
function accountSignedIn(account) {
  // An aged snapshot of a confirmed login still means signed in; only an unchecked or unconfirmed login needs the command.
  return !!account && !['not_checked', 'unavailable'].includes(account.state) && typeof account.auth_method === 'string' && !!account.auth_method;
}
function partnerWorking(partner) {
  return partner?.monitor?.status === 'working' || (Array.isArray(partner?.monitor?.current_tasks) && partner.monitor.current_tasks.some((task) => task?.status === 'working'));
}
// Beanest never signs in for the user: it shows the exact command to run in a terminal.
function accountLoginCommand(command) {
  const code = element('code', command, 'account-login-command');
  const copy = element('button', t("复制登录命令"), 'quiet-button account-login-copy'); copy.type = 'button'; copy.dataset.focusKey = 'account-login-copy';
  copy.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(command); managerNotice(t("登录命令已复制。在终端运行并完成登录后，回到这里点“刷新账号与额度”。")); }
    catch { managerNotice(t("没能自动复制，请手动选中命令复制。"), true); }
  });
  return [element('p', t("这个独立登录还没有确认账号。在终端运行下面的命令登录，豆窝不会替你登录："), 'account-login-hint'), code, copy];
}
function accountSourceControls(id, partner, source) {
  const ownHome = source.kind === 'profile_home';
  const button = element('button', state.accountSourceBusy === id ? t("正在切换…") : ownHome ? t("改回本机登录") : t("改用独立登录"), 'quiet-button account-source-switch');
  button.type = 'button'; button.dataset.focusKey = 'account-source'; button.dataset.accountSource = id;
  const working = partnerWorking(partner);
  button.disabled = working || !activeEntry(partner) || !!state.accountSourceBusy || !!state.accountRefreshId || state.managementBusy || state.busy || !!state.pending;
  button.addEventListener('click', () => void setAccountSource(id, ownHome ? 'local' : 'profile_home'));
  const nodes = [button];
  if (working) nodes.push(element('p', t("{p0}正在执行任务，结束后才能切换登录方式。", { p0: displayName(id) }), 'quota-unavailable'));
  else nodes.push(element('p', ownHome ? t("改回后使用本机登录；独立登录目录和其中的登录会保留，之后可以再切回。")
    : t("独立登录只给这位伙伴用，不影响本机登录和其他伙伴。已有任务的会话不会带到新登录里。"), 'detail-caption'));
  if (state.accountSourceErrors.has(id)) nodes.push(element('p', state.accountSourceErrors.get(id), 'quota-unavailable'));
  return nodes;
}
async function setAccountSource(id, source) {
  if (state.accountSourceBusy || state.accountRefreshId || state.managementBusy || state.busy || state.pending) return;
  state.accountSourceBusy = id; state.accountSourceErrors.delete(id);
  renderMembers(state.snapshot); renderGlobalPartners();
  try {
    acceptPartners(await api(workspaceApi.partner(id, 'account-source'), 'POST', { source }));
    state.accountRefreshTimes.delete(accountRefreshKey(id));
    managerNotice(source === 'profile_home' ? t("已改用独立登录。登录后点“刷新账号与额度”查看账号。") : t("已改回本机登录。"));
  } catch (error) {
    state.accountSourceErrors.set(id, error.message);
    await api(workspaceApi.partners).then(acceptPartners).catch(() => {});
  } finally {
    state.accountSourceBusy = null;
    renderMembers(state.snapshot); renderGlobalPartners();
  }
}
function visibleAccountPanels() {
  const visible = [];
  for (const [key, panel] of state.accountPanels) {
    if (!panel.details.open || $(key) !== panel.details) { state.accountPanels.delete(key); continue; }
    if (key.startsWith('manager-') && state.manager !== 'partners') continue;
    if (key.startsWith('composer-') && state.usagePopover?.id !== panel.id) continue;
    const { partner, account } = currentAccount(panel.id);
    if (!canRefreshAccount(partner, account)) continue;
    if (typeof panel.details.getClientRects === 'function' && !panel.details.getClientRects().length) continue;
    if (state.accountAuto.get(panel.id) !== false) visible.push(panel);
  }
  return visible;
}
function syncAccountWatchTimer() {
  if (state.accountTimer !== null) { clearTimeout(state.accountTimer); state.accountTimer = null; }
  const panels = visibleAccountPanels();
  if (document.hidden || state.accountRefreshId || !panels.length) return;
  const now = Date.now();
  const nextDue = Math.min(...panels.map(({ id }) => {
    const last = state.accountRefreshTimes.get(accountRefreshKey(id));
    return Number.isFinite(last) ? last + 60000 : now;
  }));
  // Use each profile's start time, rather than an interval phase followed by a
  // second elapsed-time gate. Slow checks must not turn a minute into two.
  const blocked = state.managementBusy || state.busy || state.pending;
  const delay = Math.max(blocked ? 60000 : 0, nextDue - now);
  state.accountTimer = setTimeout(() => {
    state.accountTimer = null;
    void refreshWatchingAccounts();
  }, delay);
}
async function refreshWatchingAccounts() {
  syncAccountWatchTimer();
  if (document.hidden || state.accountRefreshId || state.managementBusy || state.busy || state.pending) return;
  const due = visibleAccountPanels().find(({ id }) => {
    const { partner } = currentAccount(id);
    const last = state.accountRefreshTimes.get(accountRefreshKey(id));
    return partner && activeEntry(partner) && (!Number.isFinite(last) || Date.now() - last >= 60000);
  });
  if (due) await refreshProviderAccount(due.id, { automatic: true });
}
async function refreshProviderAccount(id, { automatic = false } = {}) {
  if (state.accountRefreshId || state.managementBusy || state.busy || state.pending) return;
  const { partner, account } = currentAccount(id);
  if (!canRefreshAccount(partner, account)) return;
  state.accountRefreshId = id;
  syncAccountWatchTimer();
  const startedAt = Date.now();
  state.accountRefreshErrors.delete(id);
  state.accountRefreshTimes.set(accountRefreshKey(id), startedAt);
  renderMembers(state.snapshot); renderGlobalPartners();
  try {
    acceptPartners(await api(workspaceApi.partner(id, 'account/refresh'), 'POST', {}));
    if (!automatic) managerNotice(t("账号检查已完成；供应商未提供的数据会标明无法获取。没有启动模型。"));
  } catch (error) {
    state.accountRefreshErrors.set(id, error.uncertain ? t("这次查询结果尚未确认，显示的仍是已有快照；不会自动重发同一请求。") : error.message);
    // Reads may recover a completed check after a lost response; they never repeat the native query.
    await api(workspaceApi.partners).then(acceptPartners).catch(() => {});
  } finally {
    state.accountRefreshId = null;
    // A stalled transport must not cause an immediate retry loop on return.
    if (Date.now() - startedAt >= 60000) state.accountRefreshTimes.set(accountRefreshKey(id), Date.now());
    renderMembers(state.snapshot); renderGlobalPartners();
    syncAccountWatchTimer();
    void refreshWatchingAccounts();
  }
}
