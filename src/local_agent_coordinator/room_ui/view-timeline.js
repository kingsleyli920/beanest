'use strict';

// Progress pane, the message timeline, and history paging.

const progressChipLabels = localizedLabels({
  open: '待认领', assigned: '等待开始', working: '进行中', waiting_peer: '等伙伴', review: '伙伴检查中', accepted: '伙伴看过',
  self_checked: '只做了自查', unknown: '结果待确认', interrupted: '待继续', cancelled: '待继续',
});
// Only the user's own stop really cancels unfinished tasks; any other stop carries them into the next attempt.
const userCancelCodes = new Set(['cancel_requested', 'lifecycle_requested']);
// One color per meaning, shared by the bar, the row icon and the legend.
const progressToneLabels = localizedLabels({ ok: '已完成', wk: '进行中', rev: '伙伴检查中', ask: '等你决定', wait: '待重新安排', st: '待继续', todo: '未开始' });
function progressTone(task) {
  if (task.status === 'accepted') return task.review_kind === 'self_check' ? 'self' : 'ok';
  if (task.status === 'blocked') return taskNeedsUser(task) ? 'ask' : taskQuotaHeld(task) ? 'st' : 'wait';
  return ({ self_checked: 'self', review: 'rev', working: 'wk', waiting_peer: 'wk', interrupted: 'st', cancelled: 'st', unknown: 'st' })[task.status] || 'todo';
}
function progressChip(task, cancelled = false) {
  if (task.status === 'accepted' && task.review_kind === 'self_check') return progressChipLabels.self_checked;
  if (cancelled && ['interrupted', 'cancelled'].includes(task.status)) return t("已取消");
  if (taskQuotaHeld(task)) return progressToneLabels.st;
  if (task.status === 'blocked') return taskNeedsUser(task) ? task.block_cause === 'permission_required' ? t("要你同意") : t("等你决定") : t("待重新安排");
  return Object.hasOwn(progressChipLabels, task.status) ? progressChipLabels[task.status] : t("结果待确认");
}
// The room state lists only the current run's tasks. When the room carries on into a new run
// (same goal or a follow-up), the tasks finished in earlier runs are read once from the run records
// (GET /runs?run_id=), so the progress pane still shows what is already done.
function earlierRunsOf(snapshot) {
  const current = snapshot.current_run;
  if (!current || !Number.isSafeInteger(current.number) || current.number < 2 || !Array.isArray(snapshot.runs)) return [];
  return snapshot.runs.filter((run) => run && uuidPattern.test(run.id) && run.id !== current.id && Number.isSafeInteger(run.number)
    && run.number < current.number && Number.isFinite(run.finished_at));  // a finished run has its own archived task list
}
function earlierRunProgress(snapshot) {
  const runs = earlierRunsOf(snapshot);
  if (!runs.length) return null;
  const key = `${snapshot.room.id}:${runs.map((run) => run.id).join(',')}`;
  let entry = state.earlierRuns.get(snapshot.room.id);
  if (entry?.key !== key) {
    entry = { key, state: 'loading', tasks: [] };
    state.earlierRuns.set(snapshot.room.id, entry);
    const roomId = snapshot.room.id;
    void Promise.all(runs.map((run) => api(`${roomPath(roomId)}/runs?run_id=${encodeURIComponent(run.id)}`))).then((records) => {
      entry.tasks = records.flatMap((record) => (Array.isArray(record?.team?.tasks) ? record.team.tasks : [])
        .filter((task) => task && typeof task.title === 'string' && ['accepted', 'self_checked'].includes(task.status)));
      entry.state = 'ready';
    }).catch(() => { entry.state = 'failed'; }).finally(() => {
      if (state.earlierRuns.get(roomId) === entry && state.snapshot?.room.id === roomId) renderProgressPane();
    });
  }
  return entry;
}
function earlierRunNode(entry) {
  if (!entry || entry.state === 'loading') return null;
  if (entry.state === 'failed') return element('p', t("前几轮做完的成果都还在，可以在“交付”里看。"), 'progress-note');
  if (!entry.tasks.length) return null;
  const box = element('details', '', 'earlier-runs');
  box.append(element('summary', t("前几轮已完成 {p0} 项", { p0: entry.tasks.length })));
  const list = element('ul', '', 'earlier-run-tasks');
  for (const task of entry.tasks) list.append(element('li', `✓ ${taskDisplayTitle(task.title)}`));
  box.append(list);
  return box;
}
function renderProgressPane() {
  const pane = $('progress-pane'); const snapshot = state.snapshot;
  if (!snapshot || state.selected !== snapshot.room.id) { pane.replaceChildren(); pane.dataset.fingerprint = ''; return; }
  const tasks = (Array.isArray(snapshot.team?.tasks) ? snapshot.team.tasks : []).filter((task) => task && typeof task.title === 'string');
  const objective = currentObjective(snapshot);
  const canAnswer = objective?.status === 'needs_user' && objective.can_answer === true && !$('message').disabled;
  const summary = typeof snapshot.collaboration?.summary === 'string'
    ? platformDisplayText(snapshot.collaboration.summary, snapshot.collaboration.summary_i18n).trim() : '';
  const resumes = ['auto', 'quota_wait'].includes(stopKind(snapshot, objective?.stop_reason));
  const cancelled = userCancelCodes.has(objective?.stop_reason) || userCancelCodes.has(snapshot.team?.stop_reason);
  const stLabel = cancelled ? t("已取消") : progressToneLabels.st;
  const earlier = earlierRunProgress(snapshot);
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), snapshot.room.id, tasks, objective?.stage, objective?.status, canAnswer,
    snapshot.team?.phase, summary, resumes, cancelled, snapshot.members.map((member) => [member.id, member.name, member.avatar]),
    earlier && [earlier.key, earlier.state, earlier.tasks.length]]);
  if (pane.dataset.fingerprint === fingerprint) return;
  pane.dataset.fingerprint = fingerprint;
  const nodes = [];
  if (tasks.length) {
    const tones = tasks.map(progressTone).map((tone) => tone === 'self' ? 'ok' : tone);
    const count = (tone) => tones.filter((value) => value === tone).length;
    const head = element('div', '', 'stage-head');
    const phase = Object.hasOwn(teamStageLabels, snapshot.team?.phase) ? teamStageLabels[snapshot.team.phase] : '';
    head.append(element('b', [Number.isSafeInteger(objective?.stage) ? t("第 {p0} 阶段", { p0: objective.stage }) : t("本轮分工"), phase].filter(Boolean).join(' · ')),
      element('span', [t("{p0} / {p1} 完成", { p0: count('ok'), p1: tasks.length }), count('ask') ? t("{p0} 项等你决定", { p0: count('ask') }) : '',
        count('wait') ? t("{p0} 项待重新安排", { p0: count('wait') }) : '', count('st') ? cancelled ? t("{p0} 项已取消", { p0: count('st') }) : t("{p0} 项待继续", { p0: count('st') }) : ''].filter(Boolean).join(' · ')));
    const segments = element('span', '', 'stage-seg'); segments.setAttribute('aria-hidden', 'true');
    for (const tone of tones) segments.append(element('i', '', tone));
    head.append(segments); nodes.push(head);
    const legend = element('div', '', 'stage-legend');
    for (const tone of ['ok', 'wk', 'rev', 'ask', 'wait', 'st', 'todo']) {
      if (!count(tone)) continue;
      const item = element('span', '', 'legend-item'); item.append(element('i', '', tone), element('span', `${tone === 'st' ? stLabel : progressToneLabels[tone]} ${count(tone)}`));
      legend.append(item);
    }
    nodes.push(legend);
    const note = cancelled ? count('wait') ? t("“待重新安排”的任务不用逐项处理：点“继续推进”后，豆窝会先重新安排，再接着做。") : ''
      : resumes ? count('wait') && count('st') ? t("“待重新安排”和“待继续”的任务不用逐项处理，自动继续后豆窝会先重新安排，再接着做。")
        : count('wait') ? t("“待重新安排”的任务不用逐项处理，自动继续后豆窝会先重新安排，再接着做。")
        : count('st') ? t("“待继续”的任务会在自动继续后接着做，不用逐项处理。") : ''
      : count('wait') && count('st') ? t("“待重新安排”和“待继续”的任务不用逐项处理：点“继续推进”后，豆窝会先重新安排，再接着做。")
        : count('wait') ? t("“待重新安排”的任务不用逐项处理：点“继续推进”后，豆窝会先重新安排，再接着做。")
        : count('st') ? t("“待继续”的任务在豆窝停下时还没做完，点“继续推进”后会接着做。") : '';
    if (note) nodes.push(element('p', note, 'progress-note'));
    if (summary) nodes.push(element('p', summary.length > 400 ? `${summary.slice(0, 400)}…` : summary, 'progress-summary'));
    const list = element('ol', '', 'task-board');
    for (const task of tasks) {
      const tone = progressTone(task);
      const row = element('li', '', `task-row tone-${tone}${taskNeedsUser(task) ? ' open' : ''}`);
      row.append(element('span', ({ ok: '✓', self: '✓', ask: '!', wait: '↻', st: 'Ⅱ' })[tone] || '', `si ${tone}`));
      const name = element('span', taskDisplayTitle(task.title), 'task-name'); name.title = task.title;
      if (typeof task.waiting_for === 'string' && task.waiting_for) name.append(element('small', t("等：{p0}", { p0: task.waiting_for })));
      const who = element('span', '', 'task-who');
      const member = (id) => snapshot.members.find((entry) => entry.id === id);
      if (agentPattern.test(task.owner || '')) { who.append(avatar(task.owner, member(task.owner))); who.title = displayName(task.owner); }
      else who.append(element('span', t("待认领")));
      if (agentPattern.test(task.reviewer || '') && task.reviewer !== task.owner) {
        who.append(element('span', '→'), avatar(task.reviewer, member(task.reviewer)));
        who.title = t("{p0} 负责，{p1} 审核", { p0: displayName(task.owner), p1: displayName(task.reviewer) });
      }
      row.append(name, who, element('span', progressChip(task, cancelled), `chip ${tone}`));
      if (task.status === 'blocked' && !taskQuotaHeld(task)) row.append(blockedTaskBody(task, canAnswer && taskNeedsUser(task), 'task-why'));
      list.append(row);
    }
    nodes.push(list);
  } else {
    nodes.push(element('p', snapshot.team?.phase === 'team_plan' ? t("伙伴正在分工，分好后会列在这里。") : t("这次没有拆分成任务，进展都在“讨论”里。"), 'pane-empty'));
  }
  const earlierNode = earlierRunNode(earlier);
  if (earlierNode) nodes.push(earlierNode);
  pane.replaceChildren(...nodes);
}

function jobKind(event, jobs) {
  return event.job_kind || jobs.find((job) => job.id === event.job_id)?.kind;
}

function peerRequest(event, jobs) {
  return event.kind === 'job_created' && providers.includes(event.sender) && providers.includes(event.recipient)
    && event.sender !== event.recipient
    && ['review', 'help'].includes(jobKind(event, jobs));
}

function progressText(event, jobs, archive = false) {
  if (!archive && event.kind === 'run_started') return Number.isSafeInteger(event.number) && event.number === 1 && !event.retry ? t("开始工作") : t("已继续");
  if (event.kind === 'run_started') return t("{p0}协作已开始{p1}", { p0: Number.isSafeInteger(event.number) ? t("第 {p0} 轮", { p0: event.number }) : t("新一轮"), p1: event.retry ? t("，使用新的专用会话") : event.session_rotated ? t("，保留此前交付，这一轮换了新会话") : t("，保留此前交付和会话") });
  if (event.kind === 'round_handoff_recorded') return t("{p0} 写好了本轮交接", { p0: displayName(event.sender) });
  if (event.kind === 'task_handoff_recorded') return t("{p0} 写好了任务交接", { p0: displayName(event.sender) });
  if (event.kind === 'partner_session_rotated') return t("{p0} 换了新会话，只带这份交接", { p0: displayName(event.provider) });
  if (event.kind === 'user_message_redelivered') return t("{p0} 上次没回完你的消息，已重新发给它。", { p0: displayName(event.recipient) });
  if (event.kind === 'native_readonly_discussion') return t("{p0} 正在只读讨论，已有改动保持。", { p0: displayName(event.sender) });
  if (event.kind === 'run_accepted') return t("你已接受本轮交付；这项记录不会启动模型。");
  const taskTitle = typeof event.title === 'string' && event.title ? interfaceQuote(taskDisplayTitle(event.title)) : t("子任务");
  if (event.kind === 'team_command') {
    const action = { claim: t("已认领子任务"), complete: t("已提交子任务，等伙伴检查"),
      request_help: t("请求伙伴协助，具体问题见其消息"), review: t("已提交审核结果"),
      block: t("说明了当前阻碍，待处理项见任务清单") }[event.command];
    return `${displayName(event.sender)} ${action || t("已更新任务清单")}`;
  }
  if (event.kind === 'team_plan') return t("伙伴已整理任务分工，认领与进展见上方任务清单。");
  if (event.kind === 'team_task_created') return t("已登记{p0}，等待伙伴认领", { p0: taskTitle });
  if (event.kind === 'team_task_claimed') return t("{p0} 已认领{p1}", { p0: displayName(event.sender), p1: taskTitle });
  if (event.kind === 'team_task_completed') return t("{p0} 已提交{p1}，等伙伴检查", { p0: displayName(event.sender), p1: taskTitle });
  if (event.kind === 'team_task_reviewed') return t("{p0} 已提交{p1}的审核结果", { p0: displayName(event.sender), p1: taskTitle });
  if (event.kind === 'team_help_requested') return t("{p0} 向 {p1} 请求协助", { p0: displayName(event.sender), p1: displayName(event.recipient) });
  if (event.kind === 'team_help_finished') return t("{p0} 已回复协助请求", { p0: displayName(event.sender) });
  if (event.kind === 'task_taken_over') return t("{p0} 已接过 {p1} 的子任务，继续处理。", { p0: displayName(event.owner), p1: displayName(event.previous_owner) });
  if (event.kind === 'task_rebalanced') return t("为均衡工作量，{p0}已从 {p1} 改由 {p2} 负责。", { p0: taskTitle, p1: displayName(event.previous_owner), p2: displayName(event.owner) });
  if (event.kind === 'source_advanced_externally') {
    const repository = typeof event.repository === 'string' && event.repository ? event.repository : t("交付仓库");
    const count = Number.isSafeInteger(event.external_count) && event.external_count > 0 ? event.external_count : 0;
    if (!count) return t("{p0} 的提交历史在房间外变了，伙伴会先按最新代码核对任务，已完成的不重做。", { p0: repository });
    return event.complete === false
      ? t("房间外至少有 {p0} 个新提交进入 {p1}，伙伴会先按最新代码核对任务，已完成的不重做。", { p0: count, p1: repository })
      : t("房间外有 {p0} 个新提交进入 {p1}，伙伴会先按最新代码核对任务，已完成的不重做。", { p0: count, p1: repository });
  }
  if (event.kind === 'team_stopped') return typeof event.reason === 'string' && event.reason ? event.reason : t("团队协作已停止，原因与下一步见下方交付卡。");
  if (event.kind === 'execution_queued') return t("文件版本 {p0} 的隔离测试已排队", { p0: event.revision });
  if (event.kind === 'execution_started') return t("正在隔离运行文件版本 {p0}", { p0: event.revision });
  if (event.kind === 'execution_finished') return event.cleanup_confirmed === false
    ? t("版本 {p0} 的执行清理待确认，未自动重试", { p0: event.revision })
    : t("版本 {p0} 的隔离运行已结束，结果已记录", { p0: event.revision });
  if (event.kind === 'artifact_pending') return t("{p0} 的文件提案正在核对版本，尚未确认保存。", { p0: displayName(event.sender) });
  if (event.kind === 'artifact') return t("{p0} 关联文件版本 {p1}，未执行代码或测试。", { p0: displayName(event.sender), p1: event.revision });
  if (event.kind === 'warning') return executionMessage(event.code);
  if (event.kind === 'job_claimed') return ['review', 'team_review'].includes(jobKind(event, jobs))
    ? t("{p0} 开始处理审核任务", { p0: displayName(event.sender) }) : t("{p0} 开始处理任务", { p0: displayName(event.sender) });
  if (event.kind === 'job_finished') return `${displayName(event.sender)}${interfaceColon()}${statusLabels[statusClass(event.status)]}${event.error ? `${interfaceStop()}${executionMessage(event.error)}` : ''}`;
  if (event.kind === 'usage') return t("{p0} 的用量快照已更新", { p0: displayName(event.sender) });
  if (event.kind === 'job_created') {
    const kind = jobKind(event, jobs);
    if (kind === 'feedback') return t("{p0} 的反馈已排队给 {p1}", { p0: displayName(event.sender), p1: displayName(event.recipient) });
    if (peerRequest(event, jobs)) return kind === 'review'
      ? t("{p0} 向 {p1} 发起审核请求", { p0: displayName(event.sender), p1: displayName(event.recipient) })
      : t("{p0} 向 {p1} 发起求助", { p0: displayName(event.sender), p1: displayName(event.recipient) });
    return t("任务已登记，交给 {p0}", { p0: displayName(event.recipient) });
  }
  if (event.cancellation_confirmed === false) return t("停止尚未确认，请核对本次任务执行与会话身份。");
  if (event.cancellation_confirmed === true) return t("本服务登记的执行已确认停止。");
  return `${eventLabels[event.kind] || t("进展")}${interfaceColon()}${statusLabels[statusClass(event.status)]}`;
}

function progressNode(event, jobs, archive = false) {
  const item = element('li', '', archive ? 'activity-record' : `event progress${(event.kind === 'warning' && event.code !== 'team_plan_changed') || event.cancellation_confirmed === false ? ' warning' : ''}`);
  item.dataset.seq = String(event.seq);
  const line = element('div', '', 'progress-line');
  line.append(element('span', archive ? `#${event.seq}` : '·', 'progress-marker'), element('span', progressText(event, jobs, archive), 'progress-text'));
  if (archive && event.job_id) line.append(element('span', String(event.job_id).slice(0, 8), 'progress-task'));
  line.append(element('time', timeLabel(event.created_at), 'event-time'));
  item.append(line);
  if (event.kind === 'round_handoff_recorded' || event.kind === 'task_handoff_recorded') { const details = handoffDetails(event.handoff); if (details) item.append(details); }
  return item;
}

function handoffDetails(handoff) {
  if (!handoff || typeof handoff.goal !== 'string') return null;
  const details = element('details', '', 'handoff-details');
  details.append(element('summary', t("查看交接内容")));
  for (const [label, entries] of [[t("目标"), [handoff.goal]], [t("已完成"), handoff.done], [t("已定决定"), handoff.decisions],
    [t("没验证的部分"), handoff.unverified], [t("关键文件"), handoff.key_files]]) {
    if (!Array.isArray(entries) || !entries.length) continue;
    details.append(element('div', label, 'handoff-label'));
    const list = element('ul', '', 'handoff-items');
    for (const entry of entries) list.append(element('li', String(entry)));
    details.append(list);
  }
  return details;
}

function checkpointQuestionEvent(event, snapshot) {
  const eventBinding = event.question_binding;
  const persistedQuestion = event.kind === 'objective_checkpoint_finished' && event.sender === 'system'
    && event.decision === 'needs_user' && typeof event.question === 'string' && event.question.trim()
    && uuidPattern.test(event.objective_id) && uuidPattern.test(event.run_id)
    && eventBinding?.run_id === event.run_id
    && runBinding({ id: eventBinding.run_id, revision: eventBinding.revision, digest: eventBinding.digest })
    && typeof event.proposal_digest === 'string' && /^[0-9a-f]{64}$/.test(event.proposal_digest);
  if (persistedQuestion) return { ...event, checkpoint_question: true, text: event.question };
  const objective = snapshot?.objective;
  const step = objective?.next_step;
  const source = step?.question_source;
  const binding = runBinding(snapshot?.current_run);
  if (objective?.status !== 'needs_user' || !source || source.sender !== 'system'
      || event.kind !== 'objective_checkpoint_finished' || event.sender !== 'system'
      || event.seq !== source.seq || event.objective_id !== objective.id
      || event.run_id !== binding?.run_id || event.decision !== 'needs_user'
      || JSON.stringify(step.binding) !== JSON.stringify(binding)
      || typeof step.question !== 'string' || !step.question.trim()) return event;
  return { ...event, checkpoint_question: true, text: step.question };
}
function isChatMessageEvent(event) {
  return checkpointQuestionEvent(event, null).checkpoint_question === true || event.kind === 'message' || event.kind === 'objective_decision' && event.sender === 'user'
    && (typeof event.text === 'string' || typeof event.text_preview === 'string');
}
// Partners write Markdown; their replies use the reader's safe renderer (known elements only,
// text set as text, only http(s) links). The user's own words stay exactly as typed, and an
// overlong reply stays plain rather than half formatted.
function messageTextNode(event, body) {
  const limits = window.BeanestMarkdown?.limits;
  const markdown = event.sender !== 'user' && limits && body.length <= limits.characters && body.split('\n').length <= limits.lines
    && /[*`#|>\[_-]|^\s*\d+\.\s/m.test(body);
  if (!markdown) return element('div', body, 'event-text');
  const node = element('div', '', 'event-text markdown-document chat-markdown');
  window.BeanestMarkdown.render(body, node);
  return node;
}
function eventNode(event, jobs, sourceMembers = null) {
  event = checkpointQuestionEvent(event, null);
  const request = peerRequest(event, jobs);
  if (!isChatMessageEvent(event) && !request) {
    const executionNeedsAttention = event.kind === 'execution_finished' && (event.cleanup_confirmed !== true
      || event.status !== 'completed' || !!event.error || Number.isInteger(event.exit_code) && event.exit_code !== 0);
    const show = event.kind === 'warning'
      || ['run_started', 'run_accepted', 'round_handoff_recorded', 'task_handoff_recorded', 'partner_session_rotated', 'user_message_redelivered'].includes(event.kind)
      || (event.kind === 'team_command' && ['claim', 'complete', 'request_help', 'review', 'block'].includes(event.command))
      || ['team_plan', 'team_task_created', 'team_task_claimed', 'team_task_completed', 'team_task_reviewed', 'team_help_requested', 'team_help_finished', 'team_stopped'].includes(event.kind)
      || event.kind === 'task_taken_over' || event.kind === 'task_rebalanced' || event.kind === 'source_advanced_externally'
      || executionNeedsAttention
      || typeof event.cancellation_confirmed === 'boolean'
      || (event.kind === 'job_created' && jobKind(event, jobs) === 'feedback');
    return show ? progressNode(event, jobs) : null;
  }
  const sender = Object.hasOwn(initials, event.sender) ? event.sender : 'service';
  const item = element('li', '', `event ${sender}${request ? ' request' : ''}`);
  item.dataset.seq = String(event.seq);
  const sourceMember = sourceMembers?.find((member) => (member.id || member.provider) === event.sender);
  const authorName = event.checkpoint_question === true ? t("豆窝协作") : sourceMember?.name || displayName(event.sender || 'service');
  const recipientName = sourceMembers?.find((member) => (member.id || member.provider) === event.recipient)?.name || displayName(event.recipient);
  const senderAvatar = avatar(sender, sourceMember);
  const content = element('div', '', 'event-content');
  const heading = element('div', '', 'event-heading');
  heading.append(element('span', authorName, 'event-author'));
  // A partner checking its own work addresses itself; "Kimi Code → Kimi Code" reads like a mistake.
  heading.append(event.recipient && event.recipient === event.sender ? element('span', t("自查"), 'event-recipient self-check')
    : element('span', `→ ${event.recipient ? recipientName : t("房间")}`, 'event-recipient'));
  if (request) heading.append(element('span', jobKind(event, jobs) === 'review' ? t("审核请求") : t("求助"), 'request-label'));
  heading.append(element('time', timeLabel(event.created_at, !!sourceMembers), 'event-time'));
  const body = typeof event.text === 'string' ? event.text : typeof event.text_preview === 'string' ? event.text_preview : t("消息正文暂不可用");
  content.append(heading, messageTextNode(event, body));
  appendMediaReferences(content, body);
  if (event.text_truncated === true) appendBoundText(content, t("读取完整原文"), event.text_text_ref || event.text_ref);
  const shownCharacters = [...body].length;
  if (event.native_progress === true && event.truncated === true
      && Number.isSafeInteger(event.original_characters) && event.original_characters > shownCharacters) {
    content.append(element('p', t("仅显示前 {p0} 字，原文 {p1} 字", { p0: shownCharacters, p1: event.original_characters }), 'detail-caption'));
  }
  if (event.kind === 'objective_decision' && event.sender === 'user') {
    content.append(element('span', t("用户回复"), 'event-task'));
  } else if (event.native_progress !== true && event.job_id) {
    const job = jobs.find((entry) => entry.id === event.job_id);
    const tag = element('span', ({ task: t("草案答复"), review: t("审核意见"), help: t("伙伴协助"), feedback: t("结合反馈"), execution: t("核对运行结果") })[job?.kind] || t("伙伴答复"), 'event-task');
    if (job && typeof job.text === 'string') tag.title = job.text;
    content.append(tag);
    if (Number.isSafeInteger(event.artifact_revision)) {
      content.append(element('span', t("文件版本 {p0}", { p0: event.artifact_revision }), 'event-task'));
    }
  } else if (event.native_progress !== true && event.kind === 'message') {
    content.append(element('span', t("任务目标与补充"), 'event-task'));
  }
  item.append(senderAvatar, content);
  return item;
}

function renderProgressRecords(snapshot, changedRoom) {
  let details = $('activity-records');
  if (!details) {
    details = element('details', '', 'activity-details');
    details.id = 'activity-records';
    const summary = element('summary');
    const label = element('span'); label.id = 'activity-records-label';
    const count = element('span');
    count.id = 'activity-count';
    summary.append(label, count);
    const list = element('ol', '', 'activity-records');
    list.id = 'activity-events';
    details.append(summary, list);
    document.querySelector('.jobs-details').after(details);
  }
  $('activity-records-label').textContent = t("进展记录 ");
  if (changedRoom) { $('activity-events').replaceChildren(); state.progressNodes.clear(); }
  const events = (snapshot?.events || []).filter((event) => event.kind !== 'message' && Number.isSafeInteger(event.seq)).sort((a, b) => a.seq - b.seq);
  details.hidden = !events.length;
  $('activity-count').textContent = String(events.length);
  for (const original of events) {
    const event = checkpointQuestionEvent(original, snapshot);
    const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), event]);
    const previous = state.progressNodes.get(event.seq);
    if (previous?.fingerprint === fingerprint) continue;
    const node = progressNode(event, snapshot.jobs || [], true);
    if (previous) previous.node.replaceWith(node);
    else {
      const next = [...$('activity-events').children].find((entry) => Number(entry.dataset.seq) > event.seq);
      $('activity-events').insertBefore(node, next || null);
    }
    state.progressNodes.set(event.seq, { fingerprint, node });
  }
}

function nearBottom() {
  const pane = $('timeline');
  return pane.scrollHeight - pane.scrollTop - pane.clientHeight < 70;
}

function scrollToLatest() {
  $('timeline').scrollTop = $('timeline').scrollHeight;
  state.unseen = 0;
  $('new-events').hidden = true;
}

function earlierDiscussionSource(snapshot = state.snapshot) {
  const source = snapshot?.native_handoff;
  if (snapshot?.native_handoff_source_available === false || state.deletedRooms.has(source?.source_room_id) || !state.nativeProjectSupported || !uuidPattern.test(source?.source_room_id) || source.source_room_id === snapshot?.room.id) return null;
  return { roomId: snapshot.room.id, sourceRoomId: source.source_room_id,
    key: JSON.stringify([snapshot.room.id, source.source_room_id, source.source_run_id, source.revision, source.digest]) };
}
function renderEarlierDiscussion(snapshot) {
  const source = earlierDiscussionSource(snapshot); const details = $('earlier-discussion');
  details.hidden = !source || state.roomPagingSupported && state.handoffHistory?.status !== 'loaded' && state.handoffHistory?.status !== 'loading' && state.handoffHistory?.status !== 'error';
  if (!source) { state.handoffHistory = null; details.open = false; details.dataset.fingerprint = ''; $('earlier-messages').replaceChildren(); return; }
  if (state.handoffHistory?.key !== source.key) {
    state.handoffHistory = { ...source, status: 'idle', messages: [], members: [], limit: state.roomPagingSupported ? Infinity : 80, nodes: new Map() };
    $('earlier-messages').replaceChildren();
    details.open = false;
  }
  const history = state.handoffHistory;
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), [history.key, history.status, history.limit, history.messages.length]]);
  if (details.dataset.fingerprint === fingerprint) return;
  details.dataset.fingerprint = fingerprint;
  $('earlier-summary').textContent = history.status === 'loading' ? t("正在读取更早的讨论…") : t("更早的讨论");
  $('earlier-note').textContent = history.status === 'error' ? t("这次没有读到此前讨论。当前聊天保持不变，可以重试。")
    : history.status === 'loaded' ? history.messages.length ? t("此前同一任务的真实讨论 · 只读") : t("此前任务没有已保存的聊天消息。")
      : t("展开后查看此前同一任务的讨论。");
  const retry = $('earlier-load'); retry.hidden = history.status !== 'error' && !(history.status === 'loaded' && history.messages.length > history.limit || history.hasBefore);
  retry.textContent = history.status === 'error' ? t("重新读取") : t("再看更早的消息");
  retry.disabled = history.status === 'loading';
  if (state.roomPagingSupported) retry.hidden = true;
  const list = $('earlier-messages');
  if (!state.roomPagingSupported) list.replaceChildren();
  for (const event of history.messages.slice(-history.limit)) {
    if (state.roomPagingSupported && history.nodes.has(event.seq)) continue;
    const node = eventNode(event, [], history.members);
    if (node) {
      node.dataset.sourceRoom = history.sourceRoomId; node.dataset.sourceSeq = String(event.seq);
      const next = [...list.children].find((entry) => Number(entry.dataset.sourceSeq) > event.seq);
      list.insertBefore(node, next || null); history.nodes?.set(event.seq, node);
    }
  }
}
async function loadEarlierDiscussion() {
  const history = state.handoffHistory;
  if (!history || history.status === 'loading' || history.status === 'loaded') return;
  history.status = 'loading'; renderEarlierDiscussion(state.snapshot);
  try {
    const source = await readRoomSnapshot(history.sourceRoomId);
    if (state.handoffHistory !== history || state.selected !== history.roomId || earlierDiscussionSource()?.key !== history.key) return;
    if (source?.room?.id !== history.sourceRoomId || !Array.isArray(source.events) || !Array.isArray(source.members)) throw new Error('source mismatch');
    const messages = source.events.filter(isChatMessageEvent);
    if (messages.some((event) => !Number.isSafeInteger(event.seq) || typeof event.text !== 'string' && typeof event.text_preview !== 'string')
        || new Set(messages.map((event) => event.seq)).size !== messages.length) throw new Error('invalid messages');
    history.messages = messages.sort((a, b) => a.seq - b.seq); history.hasBefore = state.roomPagingSupported && historyFor(history.sourceRoomId).hasBefore; history.members = source.members; history.status = 'loaded';
  } catch (_error) {
    if (state.handoffHistory !== history || state.selected !== history.roomId) return;
    history.status = 'error';
  }
  if (state.handoffHistory === history && earlierDiscussionSource()?.key === history.key) { if (state.roomPagingSupported) $('earlier-discussion').open = true; renderEarlierDiscussion(state.snapshot); }
}
$('earlier-discussion').addEventListener('toggle', () => { if ($('earlier-discussion').open) void loadEarlierDiscussion(); });
$('earlier-load').addEventListener('click', () => {
  const history = state.handoffHistory;
  if (history?.status === 'loaded') { history.limit += 80; renderEarlierDiscussion(state.snapshot); }
  else void loadEarlierDiscussion();
});

function renderEvents(snapshot, { preservePosition = false } = {}) {
  const roomId = snapshot?.room.id || null;
  const changedRoom = state.renderedRoom !== roomId;
  const follow = !preservePosition && !state.reader && !materialsOpen() && (changedRoom || nearBottom());
  if (changedRoom) {
    $('events').replaceChildren();
    state.eventNodes.clear();
    state.renderedRoom = roomId;
    state.unseen = 0;
  }
  renderEarlierDiscussion(snapshot);
  renderHistoryStatus(snapshot);
  renderFullGoal(snapshot);
  renderProgressRecords(snapshot, changedRoom);
  const collaborationChanged = renderCollaboration(snapshot);
  renderDocumentShortcuts(snapshot);
  $('empty-room').hidden = !!snapshot;
  if (!snapshot) { renderFirstRun(); $('new-events').hidden = true; return; }
  const events = [...snapshot.events].filter((event) => Number.isSafeInteger(event.seq)).sort((a, b) => a.seq - b.seq);
  let added = 0;
  for (const original of events) {
    const event = checkpointQuestionEvent(original, snapshot);
    const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), event]);
    const existing = state.eventNodes.get(event.seq);
    if (existing?.fingerprint === fingerprint) continue;
    const node = eventNode(event, snapshot.jobs || []);
    if (existing?.node && node) existing.node.replaceWith(node);
    else if (existing?.node) existing.node.remove();
    else if (node) {
      const next = [...$('events').children].find((entry) => Number(entry.dataset.seq) > event.seq);
      $('events').insertBefore(node, next || null);
      added += 1;
    }
    state.eventNodes.set(event.seq, { fingerprint, node });
  }
  if (follow && (changedRoom || added > 0 || collaborationChanged)) scrollToLatest();
  else if (added > 0 && !state.reader && !materialsOpen() && !preservePosition) {
    state.unseen += added;
    $('new-events').textContent = t("{p0} 条新记录 ↓", { p0: state.unseen });
    $('new-events').hidden = false;
  }
  renderReader();
}

// Metadata and history have separate versions. Loading an older page never
// replaces current permissions, eligibility, claims or the user's composer.
const roomHistories = new Map();
function pagedRoom(snapshot) { return snapshot?.projection === 'room_state_v1'; }
function configurePaging(status) {
  state.memberInvitationSupported = status?.capabilities?.room_member_invitation_v1 === true;
  state.mediaSupported = status?.capabilities?.room_media_v1 === true;
  state.codeStatusSupported = status?.capabilities?.room_code_status_v1 === true;
  state.hostOutboxSupported = status?.capabilities?.user_outbox_v1 === true && uuidPattern.test(status.user_outbox_scope || '');
  state.hostOutboxScope = state.hostOutboxSupported ? status.user_outbox_scope : null;
  state.roomPagingSupported = status?.capabilities?.room_paging_v1 === true
    && status.transport_limits?.max_message_utf8_bytes === transportLimits.max_message_utf8_bytes
    && status.transport_limits?.max_request_utf8_bytes === transportLimits.max_request_utf8_bytes;
  state.transportLimits = state.roomPagingSupported ? status.transport_limits : null;
}
function displayProjection(snapshot) {
  const preview = (value, field) => {
    if (value && typeof value[`${field}_preview`] === 'string') value[field] = value[`${field}_preview`];
  };
  preview(snapshot.room, 'goal'); preview(snapshot.current_run, 'goal'); preview(snapshot.objective, 'original_goal');
  for (const run of snapshot.runs || []) preview(run, 'goal');
  for (const job of snapshot.jobs || []) preview(job, 'text');
  for (const tasks of [snapshot.team?.tasks, snapshot.collaboration?.tasks]) for (const task of tasks || []) preview(task, 'result');
  const live = new Map((snapshot.live?.jobs || []).map((job) => [job.id, job]));
  snapshot.jobs = (snapshot.jobs || []).map((job) => live.has(job.id) ? { ...job, ...live.get(job.id) } : job);
  for (const job of live.values()) if (!snapshot.jobs.some((entry) => entry.id === job.id)) snapshot.jobs.push(job);
  return snapshot;
}
function historyFor(id) {
  if (!roomHistories.has(id)) roomHistories.set(id, { events: new Map(), through: 0, first: null, last: null, hasBefore: false, initialized: false, loading: false, error: '' });
  return roomHistories.get(id);
}
async function readEventPage(id, through, cursor = {}) {
  const query = { ...cursor, through_seq: through, limit: state.transportLimits.event_page_default_limit, max_bytes: state.transportLimits.event_page_default_bytes };
  const encoded = Object.entries(query).map(([key, value]) => `${key}=${encodeURIComponent(value)}`).join('&');
  const page = await api(`${roomPath(id)}/events?${encoded}`);
  if (page?.room_id !== id || page.through_seq !== through || !Array.isArray(page.events)
      || page.events.some((event, index) => !Number.isSafeInteger(event.seq) || event.seq > through || index > 0 && event.seq <= page.events[index - 1].seq)
      || cursor.before_seq !== undefined && page.events.some((event) => event.seq >= cursor.before_seq)
      || cursor.after_seq !== undefined && page.events.some((event) => event.seq <= cursor.after_seq)
      || (page.has_after || cursor.before_seq !== undefined && page.has_before) && !page.events.length) throw new RequestError(t("聊天记录分页不完整，已有记录仍保留。"));
  return page;
}
function mergeEventPage(history, page, direction) {
  for (const event of page.events) history.events.set(event.seq, event);
  const keys = [...history.events.keys()];
  history.first = keys.length ? Math.min(...keys) : null; history.last = keys.length ? Math.max(...keys) : null;
  if (direction !== 'after') history.hasBefore = page.has_before === true;
  if (direction !== 'after') history.through = Math.max(history.through, page.through_seq); history.initialized = true; history.error = '';
}
async function readRoomSnapshot(id) {
  if (!state.roomPagingSupported) return api(roomPath(id));
  const snapshot = await api(`${roomPath(id)}/state`);
  if (snapshot?.room?.id !== id || !pagedRoom(snapshot) || !Number.isSafeInteger(snapshot.through_seq)) throw new RequestError(t("任务当前状态尚未读完整。"));
  if (state.deletedRooms.has(id)) throw new RequestError(t("这项任务已被删除。"), { code: 'room_deleted' });
  const history = historyFor(id);
  try {
    if (!history.initialized) mergeEventPage(history, await readEventPage(id, snapshot.through_seq), 'tail');
    else if (snapshot.through_seq > history.through) {
      let cursor = history.last || 0;
      while (true) {
        const page = await readEventPage(id, snapshot.through_seq, { after_seq: cursor });
        mergeEventPage(history, page, 'after');
        if (!page.has_after) { history.through = snapshot.through_seq; break; }
        if (!page.last_seq || page.last_seq <= cursor) throw new RequestError(t("聊天记录游标没有前进，已暂停加载。"));
        cursor = page.last_seq;
      }
    }
  } catch (error) { history.error = error.message; }
  snapshot.events = [...history.events.values()].sort((a, b) => a.seq - b.seq);
  return displayProjection(snapshot);
}
function historyAnchor() {
  const pane = $('timeline'); const top = pane.getBoundingClientRect?.().top;
  const node = top === undefined ? null : [...$('events').children].find((entry) => entry.getBoundingClientRect?.().bottom > top);
  return { node, offset: node?.getBoundingClientRect?.().top, height: pane.scrollHeight, scroll: pane.scrollTop };
}
function restoreHistoryAnchor(anchor) {
  const pane = $('timeline');
  if (anchor.node?.parentNode && Number.isFinite(anchor.offset)) pane.scrollTop += anchor.node.getBoundingClientRect().top - anchor.offset;
  else pane.scrollTop = anchor.scroll + pane.scrollHeight - anchor.height;
}
function renderHistoryStatus(snapshot) {
  const status = $('history-status'); const history = snapshot && pagedRoom(snapshot) ? roomHistories.get(snapshot.room.id) : null;
  status.hidden = !history || !history.error && !history.loading && !history.hasBefore;
  status.textContent = history?.error ? t("更早的聊天暂未读完整：{p0} 向上滚动可以重试。", { p0: history.error })
    : history?.loading ? t("正在读取更早的聊天…") : history?.hasBefore ? t("向上滚动查看更早的聊天") : '';
}
async function loadOlderEvents() {
  const snapshot = state.snapshot; const id = state.selected;
  if (!pagedRoom(snapshot) || snapshot.room.id !== id || state.reader || $('timeline').scrollTop > 80) return;
  const history = historyFor(id);
  if (history.loading) return;
  if (!history.hasBefore) {
    if (earlierDiscussionSource()) {
      const source = state.handoffHistory;
      if (source?.status === 'idle' || source?.status === 'error') { $('earlier-discussion').open = true; await loadEarlierDiscussion(); }
      else if (source?.status === 'loaded' && source.hasBefore && !source.loadingOlder) {
        source.loadingOlder = true;
        try {
          const cached = historyFor(source.sourceRoomId);
          const page = await readEventPage(source.sourceRoomId, cached.through, { before_seq: cached.first });
          mergeEventPage(cached, page, 'before');
          if (state.selected !== id || state.handoffHistory !== source) return;
          const anchor = historyAnchor();
          source.messages = [...cached.events.values()].filter(isChatMessageEvent).sort((a, b) => a.seq - b.seq);
          source.hasBefore = cached.hasBefore;
          renderEarlierDiscussion(state.snapshot); restoreHistoryAnchor(anchor);
        } catch (error) { if (state.selected === id) showNotice(error.message, 'error'); }
        finally { source.loadingOlder = false; }
      }
    }
    return;
  }
  history.loading = true; renderHistoryStatus(snapshot);
  try {
    const page = await readEventPage(id, history.through, { before_seq: history.first });
    if (state.deletedRooms.has(id)) return;
    mergeEventPage(history, page, 'before');
    if (state.selected !== id || state.snapshot?.room.id !== id || state.reader) return;
    const anchor = historyAnchor();
    state.snapshot.events = [...history.events.values()].sort((a, b) => a.seq - b.seq);
    renderEvents(state.snapshot, { preservePosition: true }); restoreHistoryAnchor(anchor);
  } catch (error) { history.error = error.message; }
  finally { history.loading = false; if (state.selected === id) renderHistoryStatus(state.snapshot); }
}
