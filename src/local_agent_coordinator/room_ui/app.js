'use strict';

const t = (key, params) => window.BeanestI18n.t(key, params);
const localizedLabels = (value) => window.BeanestI18n.labels(value);
const interfaceWordSpace = () => window.BeanestI18n.getLocale() === 'en' ? ' ' : '';
const interfaceComma = () => window.BeanestI18n.getLocale() === 'en' ? ', ' : '，';
// Joining punctuation for text assembled outside t(), so English never shows 、：；（）「」.
const englishInterface = () => window.BeanestI18n.getLocale() === 'en';
const interfaceList = (items) => items.join(englishInterface() ? ', ' : '、');
const interfaceColon = () => englishInterface() ? ': ' : '：';
const interfaceSemicolon = () => englishInterface() ? '; ' : '；';
const interfaceParen = (text) => englishInterface() ? ` (${text})` : `（${text}）`;
const interfaceQuote = (text) => englishInterface() ? `“${text}”` : `「${text}」`;
const interfaceStop = () => englishInterface() ? '. ' : '。';
const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="lac-token"]').content;
let providers = [];
const names = localizedLabels(Object.assign(Object.create(null), { user: '你', both: '全体', all: '全体', service: '豆窝协作', system: '豆窝协作', mcp: 'MCP 接入' }));
const initials = localizedLabels(Object.assign(Object.create(null), { user: '我', service: '∴', system: '∴', mcp: 'M' }));
const beanStyles = localizedLabels([
  { id: 'sprout', name: '芽芽豆', description: '小叶子' },
  { id: 'peach', name: '桃桃豆', description: '小花' },
  { id: 'sky', name: '云朵豆', description: '云朵发型' },
  { id: 'lavender', name: '芋泥豆', description: '圆眼镜' },
  { id: 'sunshine', name: '奶黄豆', description: '小星星' },
  { id: 'cocoa', name: '可可豆', description: '小围巾' },
  { id: 'mint', name: '薄荷豆', description: '音乐耳机' },
  { id: 'coral', name: '珊瑚豆', description: '蝴蝶结' },
  { id: 'ocean', name: '海盐豆', description: '水手帽' },
  { id: 'rose', name: '玫瑰豆', description: '小贝雷帽' },
]);
const beanStyleIds = new Set(beanStyles.map((style) => style.id));
const statusLabels = localizedLabels({
  running: '协作进行中', completed: '本轮已回复', queued: '等待开始',
  working: '正在处理', waiting_peer: '等待伙伴', needs_user: '等你处理',
  idle: '空闲', failed: '执行失败', cancelled: '已停止', interrupted: '已中断',
  budget_exhausted: '调用预算已用完', unknown: '状态待确认',
  ready_for_user: '已交付，待你验收', accepted: '本轮已接受',
});
const teamStageLabels = localizedLabels({
  team_plan: '讨论目标与规划分工', team_coordinate: '确认分工并认领任务',
  team_work: '推进已认领的子任务', team_review: '独立审核伙伴的交付',
  team_help: '回答伙伴的具体求助', team_user: '处理你的补充说明',
});
const eventLabels = localizedLabels({
  member_status: '成员状态', job_created: '任务已登记', job_claimed: '任务已认领',
  job_finished: '本轮任务结束', room_status: '任务状态', warning: '需要注意', usage: '用量快照',
});
const executionLabels = localizedLabels({
  team_plan_changed: '分工已更新。',
  team_claim_conflict: '该任务已有伙伴认领，等待协调。',
  team_write_path_conflict: '修改范围重叠，等待调整。',
  team_plan_stalled: '尚未形成可执行分工。',
  team_result_missing: '本次提交缺少必需的文件或任务结果。',
  protocol_turn_incomplete: '本次回复未完整结束，已保存的改动和记录保留。',
  execution_initialization_unconfirmed: '任务已记录，但隔离运行环境未就绪；没有派发模型或运行代码，请核对原请求。',
  execution_unavailable: '隔离执行环境未就绪，没有运行代码。',
  execution_limit_reached: '本次任务两次隔离运行额度已用完，后续修改保留为文件，等待你决定。',
  execution_version_stale: '运行结果对应较早的文件版本，不能算作当前版本的验证。',
  budget_exhausted: '本次任务调用预算已用完，后续工作没有自动派发。',
  deadline_exceeded: '本轮期限已到，执行结果仍需核对。',
  objective_calls_exhausted: '本目标的调用上限已到，进度已保留，没有继续派发。',
  objective_deadline_exceeded: '本目标的时限已到，进度已保留，没有重新计时。',
  objective_stage_limit: '本目标已达到阶段上限，成果和记录仍保留。',
  objective_checkpoint_disagreed: '伙伴对下一步的判断不一致，需要你决定。',
  objective_scope_confirmation: '下一步可能超出原目标，需要你明确安排。',
  objective_decision_required: '等待你的回复，具体问题见聊天。',
  all_partners_quota_exhausted: '伙伴的额度都用完了，进度已保留。有伙伴额度恢复后会自动继续。',
  objective_progress_stalled: '暂时没有可核查的新进展，已停下等待处理。',
  no_progress: '重复处理同一结果，没有新的进展。补充要求后可以继续。',
  native_no_progress: '同一工具连续失败，任务文件没有变化，已暂停。',
  native_progress_unconfirmed: '任务文件状态未能确认，已暂停，避免重复操作。',
  objective_unavailable: '当前记录尚不能安排下一步，请核对任务状态。',
  invalid_execution_policy: '任务设置与当前服务不兼容，请更新本地服务后继续。',
  session_identity_unknown: '会话身份尚未确认，不会自动续接或重新派发。',
  session_identity_changed: '返回的会话身份发生变化，已停止自动续接。',
  partner_account_home_changed: '这位伙伴已换了登录方式，原会话留在之前的登录里，不能用新账号接着跑。改回原登录方式，或开新任务。',
  invalid_agent_response: '伙伴回复不符合协作协议，本轮需要核对。',
  invalid_agent_action: '伙伴提出了无法接受的协作动作，本轮需要核对。',
  invalid_adapter_response: '未取得可确认的供应商响应。',
  unexpected_tool_event: '观察到本次文字任务不允许的工具活动。',
  service_restarted_no_retry: '服务已重启，未完成任务保留为中断，不会自动重跑。',
  service_restarted: '服务已重启，本轮没有自动重跑。',
  working_without_next_step: '伙伴表示仍有工作，但没有给出下一步，需要你补充。',
  context_too_large: '当前上下文超出本次协作范围，未继续调用。',
  invoke_interrupted: '本轮调用已中断，结果尚需核对。',
  invoke_failed: '本轮调用未取得可确认的结果。',
  provider_failed: '供应商调用未完成，请在伙伴与额度中核对异常。',
  native_preflight_failed: '伙伴的模型服务连接失败，提示词没有发出。',
  subscription_limit_reached: '订阅额度已用完，请在对应工具查看恢复时间。',
  cli_nonzero_exit: '本地 CLI 未正常完成，请在伙伴与额度中核对异常。',
  unknown_provider_error: '供应商返回未知异常，请在伙伴与额度中核对记录。',
  process_or_output_failure: '本地调用或输出中断，结果尚需核对。',
  cancel_requested: '已请求停止，正在核对本服务登记的执行。',
  service_shutdown: '本地服务正在关闭，执行结果仍需核对。',
  project_revision_conflict: '文件版本已变化，已拒绝覆盖；请先核对当前草案。',
  project_file_conflict: '文件内容与原版本不一致，已拒绝覆盖。',
  project_owner_conflict: '这份文件由另一位伙伴负责，改动未保存。',
  project_commit_unconfirmed: '文件提交是否完成尚待核对，没有自动重试。',
  project_unsafe_path: '文件路径不符合新建项目范围，改动未保存。',
  objective_binding_changed: '这份交付的版本无法确认，请从当前任务重新打开；没有替换成最新正文。',
  invalid_project_files: '文件提案格式不符合要求，改动未保存。',
  artifact_sensitive_content: '文件提案包含疑似敏感内容，未保存或转交。',
});
const errorLabels = localizedLabels({
  outbox_intervening_control: '后续停止或管理操作已取代这条发送意图，未重新发送。',
  outbox_retire_unavailable: '发送记录尚不能结束，原文仍由豆窝保留。',
  outbox_unconfirmed: '原请求的执行结果尚未确认，原文保留。',
  outbox_in_flight: '原请求仍在处理中，正在核对结果，不会重复发送。',
  outbox_scope_conflict: '当前工作区与原发送记录不一致，未重新发送。',
  outbox_not_found: '这条发送记录暂未找到，原文保留，没有重新发送。',
  user_outbox_required: '这条发送记录需要在已认证的豆窝界面中核对。',
  objective_binding_changed: '任务状态或版本已变化，这次操作未完成。请刷新后重试。',
  user_execution_policy_required: '推进方式需要你在豆窝界面明确选择，伙伴不能代为修改。',
  objective_unavailable: '这个目标暂时不能继续，请核对当前状态。',
  objective_conflict: '目标记录已经变化，请刷新后再操作。',
  objective_already_started: '这个目标已经开始推进，没有重复创建。',
  objective_continue_unavailable: '当前没有可以直接开始的下一步，请查看具体问题。',
  objective_not_running: '这个目标当前没有在推进，请查看暂停或停止原因。',
  objective_not_paused: '这个目标当前不是已暂停状态，请刷新后核对。',
  objective_answer_required: '请回答当前问题后继续同一目标。',
  objective_answer_unavailable: '当前不能沿原目标继续回答，请查看具体问题。',
  objective_reassessment_unavailable: '当前暂时不能重新核对目标，请刷新后查看状态与剩余条件。',
  objective_restart_unavailable: '当前不能继续未完成工作，请核对停止原因。',
  objective_restart_unsafe: '执行清理或身份尚未确认，暂时不能继续未完成工作。',
  native_handoff_unavailable: '当前记录还不能承接完整开发，请核对任务状态。',
  native_permission_required: '请先选择本次完整开发的统一权限。',
  native_adapter_unavailable: '有伙伴暂不支持本机完整开发，请检查工具状态。',
  native_adapter_unsupported: '完整开发目前只支持 Claude Code 和 Codex；所选伙伴中有尚未接入的工具，没有自动删去成员。',
  no_progress: executionLabels.no_progress,
  native_no_progress: executionLabels.native_no_progress,
  native_progress_unconfirmed: executionLabels.native_progress_unconfirmed,
  native_cleanup_unconfirmed: '本机执行的清理尚未确认，暂时不会继续派发。',
  media_not_found: '这份链接暂未关联当前聊天的附件。',
  media_unavailable: '附件当前不可读取，已有记录保留。',
  media_changed: '附件已有新版本，请刷新附件后重新打开。',
  media_access_expired: '附件访问已过期，请重新打开。',
  media_range_invalid: '无法读取这段媒体内容，请重新打开或下载。',
  media_unsafe_path: '这份链接不在本聊天可读取的文件范围内。',
  media_ambiguous_reference: '这个相对链接对应多份附件，请从图片与视频列表选择。',
  native_recovery_unavailable: '这次恢复请求未完成，已有记录与成果保留。',
  native_recovery_cleanup_unconfirmed: '上次本机执行的清理尚未确认，暂时不能继续。',
  native_recovery_workspace_unavailable: '开发副本暂时无法安全核对，没有重新派发。',
  native_recovery_unpublished_changes: '上次留下的改动已保留，本次尚未启动续作。',
  native_recovery_binding_changed: '当前文件版本已变化，请重新核对现场后再试。',
  native_recovery_diagnostic_unconfirmed: '现场记录暂时对不上，已保留文件和记录；核对后再继续。',
  objective_calls_exhausted: '本目标的调用上限已到，成果和记录保留。',
  objective_deadline_exceeded: '本目标的时限已到，没有重新计时。',
  objective_stage_limit: '本目标的阶段上限已到，已有成果和记录保留。',
  all_partners_quota_exhausted: '伙伴的额度都用完了，正在等额度恢复，进度已保留。',
  invalid_execution_policy: '任务设置与当前服务不兼容，请更新本地服务后继续。',
  invalid_runtime_settings: '运行设置不符合要求，请重新选择权限、模型和思考强度。',
  runtime_settings_unsupported: '这个接入暂不支持指定运行设置，可以选择工具默认。',
  runtime_options_required: '请先在权限与模型中读取这位伙伴的模型选项，再提交任务。',
  runtime_model_unsupported: '这个模型不在工具返回的可选列表中，请刷新模型选项后重选。',
  runtime_effort_unsupported: '当前接入未提供所选思考强度，请重新读取选项后选择。',
  user_runtime_settings_required: '运行设置需要你在豆窝界面操作，伙伴不能替你修改。',
  agent_quota_exhausted: '有伙伴的额度已用完，请先调整参与伙伴或等待额度状态更新。',
  agent_quota_unconfirmed: '当前登录待确认，之前的额度限制仍保留。请先刷新账号状态。',
  availability_recheck_unavailable: '当前不能重新允许使用，请刷新账号与额度状态后再确认。',
  user_account_required: '这项账号操作需要你在豆窝界面明确确认。',
  account_adapter_unsupported: '这个接入方式暂不支持账号与额度查询。',
  invalid_account_source: '这个登录方式不受支持，请刷新后再试。',
  partner_account_source_busy: '这位伙伴正在执行任务，结束后才能切换登录方式。',
  partner_account_home_changed: '这位伙伴已换了登录方式，原会话留在之前的登录里，不能用新账号接着跑。改回原登录方式，或开新任务。',
  invalid_project_lifecycle: '这个项目管理操作不符合要求，请刷新后再试。',
  invalid_partner_lifecycle: '这个伙伴管理操作不符合要求，请刷新后再试。',
  invalid_room_lifecycle: '这个任务管理操作不符合要求，请刷新后再试。',
  project_inactive: '项目已归档或移除。请在项目管理中恢复后再选入新任务。',
  partner_inactive: '伙伴已归档或移除。请在伙伴与额度中恢复后再选入新任务。',
  room_archived: '任务已归档，仍可阅读。恢复后才能继续。',
  room_removed: '任务已移除，仍可阅读。恢复后才能继续。',
  room_lifecycle_busy: '任务仍在处理或等待清理，结束后再归档或移除。',
  team_cleanup_unconfirmed: '执行清理尚未确认，暂时不能归档或移除。',
  project_lifecycle_busy: '这个项目仍有任务在处理，结束后再归档或移除。',
  partner_lifecycle_busy: '这个伙伴的上一项操作仍在处理，已有工作和记录保留。',
  partner_cleanup_unconfirmed: '还没确认这个伙伴已停止，改动和记录已保留。',
  partner_handoff_binding_changed: '保留的改动版本已有变化，接手暂未完成。',
  partner_configuration_unavailable: '伙伴配置暂时不可用，操作尚未完成。',
  partner_disabled: '这个伙伴已停用。', partner_archived: '这个伙伴已归档。', partner_removed: '这个伙伴配置已移除。',
  partner_unavailable: '这个伙伴暂时不可用。', partner_lifecycle_pending: '正在处理这个伙伴的操作。',
  user_lifecycle_required: '归档、恢复与移除请在豆窝界面中操作。',
  invalid_work_mode: '请选择“方案”或“动手”，再发送任务。',
  plan_read_only: '这一轮是方案模式，不能修改文件。要改文件，切到“动手”再发一句话。',
  permission_read_only: '本轮统一权限为只读，不能修改隔离副本。可在下一轮调整权限。',
  invalid_arguments: '这次输入不符合要求，请检查目标和接收者。',
  invalid_goal: '请填写目标。',
  invalid_text: '请填写补充说明。',
  invalid_event_cursor: '聊天记录位置已变化，已有内容保留，可以重新读取。',
  text_reference_conflict: '这段原文已有新版本，未用旧预览代替原文。',
  text_reference_not_found: '这段原文暂时不可读取，已有记录保留。',
  event_too_large: '单条历史记录超过本机读取容量，未跳过或截短该记录。',
  room_state_too_large: '当前状态暂时超过读取容量，已有聊天和草稿保留。',
  message_too_large: '这段文字超过本机单条消息的传输容量，未发送。请拆成几条发送。',
  request_too_large: '这次请求超过本机传输容量，未发送。原文和设置仍保留。',
  invalid_message: '请检查补充说明后再发送。',
  invalid_prompt: '请检查输入内容后再发送。',
  invalid_target: '请选择本次任务已登记的伙伴或全体。',
  member_limit_reached: '当前聊天已达到可接入的伙伴数量。',
  invitation_mode_unsupported: '这份旧记录暂不支持在原聊天邀请伙伴。',
  user_member_invitation_required: '请从豆窝界面邀请伙伴。',
  invalid_participants: '请选择 1–8 位已启用且不重复的伙伴；接收者须属于本次成员。',
  invalid_project_request: '项目登记信息不完整，请检查名称、目录和文件列表。',
  invalid_project_path: '项目目录不符合要求，请填写可访问的完整本机目录路径。',
  project_unsafe_path: '这个路径不能用：项目必须是一个普通文件夹，不能是单个文件或替身。',
  invalid_project_files: '有文件不能用。只能选项目里的文本文件（源码、文档、配置），图片、压缩包这类二进制文件不行。',
  project_file_unavailable: '有文件或文件夹找不到、读不了，请核对路径和访问权限。',
  project_sensitive_content: '选定文件含疑似敏感内容，未带入协作；请移除凭据或私密记录后再添加。',
  project_limit_exceeded: '选定文件超出当前范围：最多 12 个，每个 16 KiB，总计 48 KiB。',
  project_capture_limit: '项目超出当前隔离副本的容量范围，尚未开始处理；请缩小项目目录后重试。',
  project_source_changed: '项目来源在准备过程中发生变化，未接受本次快照；请确认文件稳定后重试。',
  invalid_project_inspection: '伙伴的文件查找请求不符合当前范围，未读取额外内容。',
  project_not_found: '没有找到这个项目，请刷新项目列表后重新选择。',
  project_catalog_corrupt: '项目登记记录无法读取，请检查本地存储；不会自动覆盖或重建。',
  project_not_seeded: '项目文件尚未准备好，请核对任务记录；本次操作未被接受。',
  unsafe_partner_storage: '伙伴存储目录不符合安全要求，请检查本地目录和权限。',
  invalid_partner_storage: '伙伴登记记录无法读取，请检查本地存储；不会自动覆盖。',
  invalid_partner_profile: '伙伴信息不符合要求，请检查标识、名称、头像和接入工具。',
  invalid_partner_connection: '请检查本机连接器的完整路径、逐行参数与调用方式；这里不接受 shell 命令。',
  unsupported_partner_adapter: '这个接入工具尚无可用适配器，不能仅靠改名接通新供应商。',
  partner_identity_conflict: '这个伙伴标识已存在或身份冲突，请刷新记录并使用不同标识。',
  partner_limit_reached: '已达到当前伙伴登记上限，请整理已有伙伴后再添加。',
  invalid_partner_enabled: '伙伴启停状态不符合要求，请刷新管理记录后操作。',
  unknown_partner: '没有找到这位伙伴，请刷新伙伴列表。',
  invalid_check_clock: '接入检查时间无效，请检查本机时钟和本地服务。',
  partner_management_unavailable: '当前服务不提供伙伴管理，请检查服务版本或启动方式。',
  agent_unavailable: '这位伙伴当前未接入，历史记录保留；请检查适配器配置。',
  invalid_request: '请求标识无效，请重新打开本地页面。',
  room_not_found: '没有找到这个任务，请刷新任务列表。',
  room_cancelled: '这个任务已经停止，记录仍可查看。',
  room_not_running: '这个任务暂时不能接收新消息，请核对状态。',
  room_closed: '这个任务已关闭，记录仍可查看。',
  room_interrupted: '这个任务因服务中断而停止，不会自动重跑。',
  room_budget_exhausted: '这个任务的调用预算已用完，记录仍可查看。',
  budget_exhausted: '这个任务的调用预算已用完，记录仍可查看。',
  request_conflict: '请求标识与已保存的内容不一致，请先核对任务记录。',
  runtime_in_use: '已有服务使用这个运行目录，请检查本地服务。',
  project_requires_first_author: '新代码草案请选择一位伙伴先写，另一位随后自动审核。',
  project_initialization_unconfirmed: '任务初始化的结果尚待核对，请保留原请求，不要另开重复目标。',
  project_commit_unconfirmed: '文件提交是否完成尚待核对，请保留原请求。',
  team_run_conflict: '这一轮的版本已经变化，请刷新核对后再操作。',
  team_run_not_ready: '这一轮仍有工作或检查在进行，请先等待当前结果。',
  team_task_not_found: '这一轮里找不到这一项，刷新后再看看。',
  task_already_withdrawn: '这一项已经撤掉了。',
  task_withdraw_finished: '这一项已经做完，不能再撤。',
  task_withdraw_busy: '伙伴正在处理，这一步结束后才能撤。',
  task_withdraw_dependents: '还有任务要用到它，需要一起撤掉。',
  task_withdraw_unconfirmed: '上一步的结果还没核对完，先点“继续推进”让豆窝核对，之后再撤。',
  task_withdrawal_pending: '撤掉的改动还在从项目里移除，稍后再导出。',
  user_task_withdraw_required: '撤掉任务要你在豆窝界面里自己操作。',
  team_retry_required: '这次需要明确的新尝试，不会自动重跑原请求。',
  team_cleanup_unconfirmed: '上一轮执行或清理尚未确认，暂不能开始新的尝试。',
  team_run_limit: '这段协作已达到迭代上限，历史和成果仍保留。',
  user_acceptance_required: '接受本轮需要你在豆窝界面明确操作。',
  user_fork_required: '另开任务需要你在豆窝界面明确操作。',
  fork_source_unavailable: '已保存文件暂不可安全复用，请核对原任务成果；没有另开任务。',
  project_export_unavailable: '当前没有可下载的文件成果。',
  project_export_corrupt: '成果文件未通过完整性核对，未开始下载。',
  invalid_project_revision: '文件版本无效，请刷新成果后再下载。',
  forbidden: '本地连接授权已失效，请从正在运行的服务重新打开窗口。',
  token_required: '本地连接授权已失效，请从正在运行的服务重新打开窗口。',
});
// These failures are reported before a workspace operation is accepted. Keep
// uncertain room initialization/commit errors outside this set.
const workspaceRejections = new Set([
  'member_limit_reached', 'invitation_mode_unsupported', 'user_member_invitation_required', 'invalid_participants', 'invalid_project_request', 'invalid_project_path', 'invalid_project_files',
  'project_file_unavailable', 'project_sensitive_content', 'project_limit_exceeded', 'project_not_found',
  'project_capture_limit', 'project_source_changed', 'invalid_project_inspection',
  'project_catalog_corrupt', 'project_not_seeded', 'unsafe_partner_storage', 'invalid_partner_storage',
  'invalid_partner_profile', 'unsupported_partner_adapter', 'partner_identity_conflict',
  'partner_limit_reached', 'invalid_partner_enabled', 'unknown_partner', 'invalid_check_clock',
  'partner_management_unavailable',
]);
const closedStatuses = new Set(['cancelled', 'interrupted', 'budget_exhausted', 'failed']);
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const agentPattern = /^[a-z][a-z0-9_-]{0,31}$/;
const transportLimits = Object.freeze({ max_message_utf8_bytes: 1048576, max_request_utf8_bytes: 8388608 });
const utf8Bytes = (text) => new TextEncoder().encode(text).byteLength;
function validUserText(text) { return typeof text === 'string' && utf8Bytes(text) <= transportLimits.max_message_utf8_bytes; }
function checkUserText(text) {
  if (validUserText(text)) return true;
  showNotice(errorLabels.message_too_large, 'error'); return false;
}
const pendingStorageKey = 'lac-rooms-pending-v1';
const lifecycleStorageKey = 'lac-room-lifecycle-v1';
const outboxDatabaseName = 'beanest-message-outbox-v1';
let outboxDatabase;
let outboxConnection;
let outboxWrites = Promise.resolve();
let outboxRestored = false;
let outboxCommitted = false;
let outboxCleanupPending = false;
const outboxRetired = new Set();
function readSessionJson(key) { try { return JSON.parse(sessionStorage.getItem(key) || 'null'); } catch (_error) { return null; } }
const pendingPartnerStorageKey = 'lac-partner-add-pending-v1';
const workspaceApi = {
  projects: '/api/projects', partners: '/api/partners',
  partner: (id, action) => `/api/partners/${encodeURIComponent(id)}/${action}`,
};
const state = {
  rooms: [], selected: null, snapshot: null, target: 'all', targetExplicit: false, connected: false, connectionChecked: false,
  mode: 'text', modeExplicit: false, workMode: 'build', taskModes: ['text'], nativeProjectSupported: false, projectFingerprint: '', executionFingerprint: '',
  catalog: [], memberFingerprint: '', participantIds: [],
  selectedParticipants: null, partnerDirectorySeen: false, selectedProject: '', projects: [], partners: [], adapters: [],
  memberInvitationSupported: false, memberInvitations: new Map(), memberInviting: new Set(), memberInvitationFingerprint: null,
  discovery: null, discoveryOpened: false, discoveryInFlight: false, discoveryLastOpened: null,
  nativeLogins: new Map(), nativeInstalls: new Map(), firstRunOpen: false, firstRunDismissed: false, firstRunFingerprint: '', noUsableTool: false, nativeLoginBusy: new Set(), nativeLoginErrors: new Map(), nativeLoginTimer: null, nativeSetupFingerprint: null,
  connectionTemplates: [], connectionProtocol: null, configuringPartnerId: null,
  beanStyleOptions: beanStyles, selectedBeanStyle: 'sprout', beanStyleChosen: false,
  pendingPartner: null, pendingPartnerScopeChanged: false,
  partnerSaveUncertain: false, partnerVerifying: false, projectChoosing: false,
  contextMenu: null, usagePopover: null, usageButtons: new Map(), partnerControls: new Map(), activityNodes: new Map(), taskOptions: null, handoffHistory: null,
  lifecycleViews: { projects: 'active', partners: 'active', rooms: 'active' }, lifecycleConfirm: null, roomLifecycleSupported: false, roomNativeSessionCleanupSupported: false, messageReceiptsSupported: false, projectDeletionSupported: false, partnerLifecycleSupported: false, partnerOperations: new Map(), partnerIntentEpoch: 0, partnerRequestGeneration: 0, partnerAppliedGeneration: 0, partnerOperationSequence: 0, partnerResponseContexts: new WeakMap(), partnerOperationChecks: new Set(), messageReceiptChecks: new Set(), projectDeletions: new Map(), lifecycleOperations: new Map(), lifecyclePolling: false, roomControlRequests: new Map(), stopRequests: new Set(), sidelinedRequests: new Map(), deletedRooms: new Set(),
  availabilityTimer: null, availabilityRefreshing: false, availabilityLastStarted: null, availabilityError: '', availabilityRecheck: null, taskWithdraw: null, taskWithdrawalSupported: false,
  accountRefreshId: null, accountTimer: null, accountPanels: new Map(), accountAuto: new Map(), accountRefreshTimes: new Map(), accountRefreshErrors: new Map(),
  accountSourceBusy: null, accountSourceErrors: new Map(),
  manager: null, managerReturnFocus: null, managementBusy: false, projectRename: null, projectListFingerprint: '', partnerListFingerprint: '',
  hostOutboxSupported: false, hostOutboxScope: null, hostOutboxPolling: false, hostOutboxRows: new Map(), hostOutboxRequests: new Map(), hostOutboxRetirements: new Map(), hostOutboxRetiring: new Set(), hostAutomaticAttempts: new Set(),
  actionError: null, busy: false, busyRequest: null, refreshing: false, pending: null, epoch: 0, authScope: null, scopeChanged: false,
  exportBusy: false, exportMessage: '', exportRoom: null,
  forkSource: null,
  codeStatusSupported: false, codeStatusEntries: new Map(), codeStatusRendered: null, mediaSupported: false, media: null, mediaHistoryChoices: new Map(), reader: null, materialsRoom: null, runtimeOptions: null, runtimeSettings: null, runtimeScope: null, runtimeDraftDirty: false, runtimePermissionExplicit: false, workModeDraftDirty: false, runtimeDraftToken: null, workModeDraftToken: null, runtimeLoading: false, runtimeRefreshId: null, runtimeMessage: '', runtimePopoverOpen: false, runtimePopoverTrigger: null, runtimePartner: null,
  executionPolicySupported: false, continuousObjectiveSupported: false, naturalContinuationSupported: false, naturalRuntimeSupported: false, roomPagingSupported: false, transportLimits: null,
  deliveryLimit: 256, deliveryScope: '',
  drafts: new Map(), roomNodes: new Map(), eventNodes: new Map(), progressNodes: new Map(),
  renderedRoom: null, jobsFingerprint: '', collaborationFingerprint: '', unseen: 0,
  roomTabs: new Map(), deliveryPending: 0, earlierRuns: new Map(),
};
$('task-mode').value = 'text';

// The "now" card, the four room views and the progress board. They read the
// same snapshot as the conversation; actions forward to the existing controls
// so every request still goes through one code path.
// Only a block that asks for a human choice or a permission belongs in the
// decision area. Reply mistakes, format errors and coordination blocks are
// replanned by the service when work continues, so they read as progress.
const decisionBlockCauses = new Set(['permission_required', 'user_decision']);
function taskNeedsUser(task) { return task?.status === 'blocked' && decisionBlockCauses.has(task.block_cause); }
// A task held only because partners ran out of quota simply waits to continue; nothing about it is rearranged.
const quotaBlockCodes = new Set(['all_partners_quota_exhausted', 'subscription_limit_reached', 'agent_quota_exhausted', 'quota_exhausted']);
function taskQuotaHeld(task) { return task?.status === 'blocked' && task.block_cause == null && quotaBlockCodes.has(task.blocked_reason); }
function taskAutoBlocked(task) { return task?.status === 'blocked' && !decisionBlockCauses.has(task.block_cause) && !taskQuotaHeld(task); }
// A task the user withdrew stays listed but is never counted as work to do, done or delivered.
function taskWithdrawn(task) { return task?.status === 'withdrawn'; }
const blockCodePattern = /^[a-z][a-z0-9_]{2,80}$/;
// Mirrors native_outcome.FORMAT_ERRORS (REPLY_MISTAKES included): the service corrects these itself.
const replyCorrectedCodes = new Set(['team_task_permission', 'team_plan_locked', 'invalid_target', 'team_files_forbidden',
  'team_capability_unavailable', 'native_json_files_forbidden', 'team_write_path_conflict', 'invalid_agent_response',
  'invalid_team_commands', 'invalid_team_task', 'invalid_team_dependencies', 'invalid_project_files', 'invalid_text',
  'team_task_not_found', 'team_result_missing', 'message_too_large', 'native_unchanged_reason_required']);
// A scope conflict is a state conflict, so the service never corrects it inside
// the call. It still asks nothing of the user: stop_policy continues the
// objective, so the page words it the same way.
const selfCorrectedCodes = new Set([...replyCorrectedCodes, 'team_scope_conflict']);
// Mirrors stop_policy.py: only these stops wait for the user (USER_STOPS),
// keep the scene for inspection (KEEP_SCENE) or have their own quota wait.
// The host continues every other stop of an autonomous objective once per
// board state; a stop that comes back without progress is the user's.
const userStopCodes = new Set(['cancel_requested', 'lifecycle_requested', 'explicit_new_attempt', 'budget_exhausted', 'deadline_exceeded',
  'objective_calls_exhausted', 'objective_deadline_exceeded', 'objective_stage_limit', 'objective_no_progress',
  'objective_decision_required', 'objective_scope_confirmation', 'objective_checkpoint_disagreed',
  'partner_account_home_changed']);
const keepSceneCodes = new Set(['project_commit_unconfirmed', 'native_cleanup_unconfirmed', 'team_cleanup_unconfirmed',
  'native_recovery_cleanup_unconfirmed', 'session_identity_changed', 'session_identity_unknown',
  'native_recovery_unpublished_changes', 'native_side_effects_unconfirmed', 'execution_unknown']);
function stopContinuesAutomatically(code) {
  return typeof code === 'string' && !userStopCodes.has(code) && !keepSceneCodes.has(code) && code !== 'all_partners_quota_exhausted';
}
function objectivePolicy(snapshot) {
  const objective = currentObjective(snapshot);
  return objective?.execution_policy || snapshot.current_run?.execution_policy || (objective ? { mode: objective.mode } : null);
}
function autonomousObjective(snapshot) {
  return activeEntry(snapshot.room) && objectivePolicy(snapshot)?.mode === 'autonomous';
}
function autoContinuedAlready(snapshot, code) {
  const events = Array.isArray(snapshot.events) ? snapshot.events : [];
  const lastUser = events.reduce((seq, event) => event.kind === 'message' && event.sender === 'user' && event.seq > seq ? event.seq : seq, 0);
  return events.some((event) => event.kind === 'objective_auto_continued' && event.seq > lastUser
    && event.code === code && event.run_id === snapshot.current_run?.id);
}
// A blocked board the host reopens by itself: coordination requests and
// tasks the host blocked on a call's failure (team_store.resume_recoverable_block).
function taskBlockContinuesAutomatically(snapshot, blocked) {
  return autonomousObjective(snapshot) && blocked.length > 0 && !autoContinuedAlready(snapshot, 'task_block')
    && blocked.every((task) => task.block_cause === 'coordination_required' || task.block_cause == null && stopContinuesAutomatically(task.blocked_reason));
}
// room_service._auto_continue sends only these natural continuations, and only when they can be sent.
const autoContinueActions = ['restart-objective', 'recover-native'];
function autoContinueReady(snapshot) {
  const natural = snapshot.continuation;
  if (natural && typeof natural === 'object' && typeof natural.action === 'string') return autoContinueActions.includes(natural.action) && natural.can_send === true;
  return autoContinueActions.some((id) => liveControl(id) && !document.getElementById(id).disabled);
}
function quotaWaitContinues(snapshot) {
  return autonomousObjective(snapshot) && objectivePolicy(snapshot)?.quota_failover === true;
}
// The service's own stop_kind decides when present ("auto", "quota_wait" or
// null); the mirror above only serves an older service without it.
function stopKind(snapshot, code) {
  const repeated = stopContinuesAutomatically(code) && autonomousObjective(snapshot) && autoContinuedAlready(snapshot, code);
  if (Object.hasOwn(snapshot, 'stop_kind')) return ['auto', 'quota_wait'].includes(snapshot.stop_kind) ? snapshot.stop_kind : repeated ? 'repeated' : null;
  if (code === 'all_partners_quota_exhausted') return quotaWaitContinues(snapshot) ? 'quota_wait' : null;
  if (!stopContinuesAutomatically(code) || !autonomousObjective(snapshot) || !autoContinueReady(snapshot)) return null;
  return repeated ? 'repeated' : 'auto';
}
function roomStopKind(room) {
  const snapshot = state.snapshot?.room.id === room.id ? state.snapshot : null;
  const value = snapshot && Object.hasOwn(snapshot, 'stop_kind') ? snapshot.stop_kind : room.stop_kind;
  return ['auto', 'quota_wait'].includes(value) ? value : null;
}
function blockedTaskReason(task) {
  if (task?.block_cause === 'permission_required') return t("需要权限确认：这一步要用到当前权限以外的操作。同意就按原计划做，不同意伙伴会换一种做法。");
  if (task?.block_cause === 'user_decision') {
    const raw = typeof task.blocked_reason === 'string' ? task.blocked_reason.trim() : '';
    return raw && !blockCodePattern.test(raw) ? raw.length > 240 ? `${raw.slice(0, 240)}…` : raw : t("伙伴需要你定一个方向，具体问题见“讨论”。");
  }
  if (task?.block_cause === 'external_dependency') return t("在等外部条件，满足后点“继续”接着做。");
  return t("这项需要重新安排，豆窝会接着处理，不用你选。");
}
// Once the service confirms the stop, the "checking" wording of cancel_requested no longer applies.
function stoppedReasonMessage(snapshot, code) {
  if (['cancel_requested', 'lifecycle_requested'].includes(code) && snapshot?.cancellation_confirmed === true) return t("已按你的要求停下。");
  return executionMessage(code);
}
function stopReasonMessage(code) {
  return selfCorrectedCodes.has(code) ? t("有任务需要重新安排，点“继续推进”后会自动处理。") : executionMessage(code);
}
function blockedTaskTechnical(task) {
  const raw = typeof task?.blocked_reason === 'string' ? task.blocked_reason.trim() : '';
  if (task?.block_cause === 'user_decision' && !blockCodePattern.test(raw)) return typeof task.block_cause === 'string' ? task.block_cause : '';
  return [typeof task?.block_cause === 'string' ? task.block_cause : '', raw].filter(Boolean).join('\n');
}
function techDetails(raw, summary = t("技术细节")) {
  const details = element('details', '', 'tech-details');
  details.append(element('summary', summary), element('code', raw));
  return details;
}
// Quick choices only fill the composer; the user still reviews and sends.
function fillAnswerDraft(text) {
  const message = $('message');
  if (message.disabled) return;
  const draft = message.value;
  if (draft.includes(text)) { message.focus(); return; }
  const combined = draft.trim() ? `${draft}\n${text}` : text;
  if (!checkUserText(combined)) return;
  message.value = combined; saveDraft(); renderControls(); message.focus();
}
function quickAnswers(task) {
  if (task.block_cause !== 'permission_required') return [];
  const title = taskDisplayTitle(task.title || task.id);
  return [[t("同意，按原计划做"), t("同意「{p0}」需要的权限，按原计划继续。", { p0: title })],
    [t("不同意，换个做法"), t("不同意「{p0}」需要的权限，请换一种不需要这个权限的做法。", { p0: title })]];
}
function blockedTaskBody(task, canAnswer, className) {
  const body = element('div', '', className);
  body.append(element('p', blockedTaskReason(task)));
  const answers = canAnswer ? quickAnswers(task) : [];
  if (answers.length) {
    const row = element('div', '', 'quick-row');
    for (const [label, text] of answers) {
      const button = element('button', label, 'btn sm'); button.type = 'button'; button.title = t("填入输入框：{p0}", { p0: text });
      button.addEventListener('click', () => fillAnswerDraft(text));
      row.append(button);
    }
    body.append(row, element('p', t("点选项只会把回复填进输入框，确认后再发送。"), 'quick-note'));
  }
  const raw = blockedTaskTechnical(task);
  if (raw) body.append(techDetails(raw));
  return body;
}

const nowPrimaryIds = ['accept-run', 'continue-current', 'continue-objective', 'recover-native', 'native-handoff', 'restart-objective',
  'start-objective', 'start-suggested-goal', 'build-from-plan', 'retry-run'];
function liveControl(id) {
  const node = document.getElementById(id);
  return node && !node.hidden ? node : null;
}
function controlCaption(node) {
  const siblings = [...(node.parentNode?.children || [])];
  const next = siblings[siblings.indexOf(node) + 1];
  return next?.classList?.contains('detail-caption') ? next.textContent : '';
}
function nowSummary(snapshot) {
  const room = snapshot.room; const objective = currentObjective(snapshot); const run = snapshot.current_run;
  const status = objective?.status; const outcome = terminalTeamState(snapshot) || snapshot.collaboration?.outcome?.state;
  const tasks = (Array.isArray(snapshot.team?.tasks) ? snapshot.team.tasks : []).filter((task) => task && typeof task === 'object' && !taskWithdrawn(task));
  const stage = Number.isSafeInteger(objective?.stage) ? t("第 {p0} 阶段", { p0: objective.stage }) : Number.isSafeInteger(run?.number) ? t("第 {p0} 轮", { p0: run.number }) : '';
  const label = (text, time) => [text, stage, Number.isFinite(time) ? timeLabel(time) : ''].filter(Boolean).join(' · ');
  const done = tasks.filter((task) => ['accepted', 'self_checked'].includes(task.status)).length;
  if (!activeEntry(room)) return { tone: 'stop', label: lifecycleLabels[lifecycleState(room)], title: t("这份任务{p0}", { p0: lifecycleLabels[lifecycleState(room)] }),
    text: t("聊天和交付仍可阅读。要继续，先在右上角“···”的“管理任务”里恢复。") };
  const autoBlocked = tasks.filter(taskAutoBlocked);
  const autoNote = autoBlocked.length ? t("有 {p0} 项需要重新安排，点“继续推进”后会自动处理。", { p0: autoBlocked.length }) : '';
  if (status === 'needs_user' || !objective && outcome === 'needs_user') {
    const next = boundObjectiveNextStep(snapshot);
    const blocked = next?.origin === 'task_block' ? tasks.filter(taskNeedsUser) : [];
    const canAnswer = objective?.can_answer === true;
    const blockAuto = Object.hasOwn(snapshot, 'stop_kind') ? snapshot.stop_kind === 'auto'
      : taskBlockContinuesAutomatically(snapshot, tasks.filter((task) => task.status === 'blocked'));
    if (next?.origin === 'task_block' && !blocked.length) return blockAuto
      ? { tone: 'go', autoContinue: true, label: label(t("马上自动继续")), canAnswer,
        title: t("有几项要重新安排，豆窝会自动接着做"), text: t("几秒后自动继续，不用你操作。想补充要求可以直接在下面说。") }
      : { tone: 'stop', label: label(t("已暂停")), canAnswer,
        title: t("有几项需要重新安排"), text: t("点“继续推进”或在下面补充要求，豆窝会自动重新安排，不用逐项处理。") };
    const question = !blocked.length && typeof next?.question === 'string' ? next.question.trim() : '';
    const permission = blocked.length && blocked.every((task) => task.block_cause === 'permission_required');
    return { tone: 'ask', label: label(t("等你决定")), blocked, canAnswer,
      title: blocked.length ? permission ? t("有一步要你同意权限") : t("伙伴需要你定个方向") : t("伙伴有个问题要你回答"),
      question: question.length > 220 ? `${(question.split('\n').find((line) => line.trim()) || question).slice(0, 200)}…` : question,
      text: [canAnswer ? t("在下面回复后，伙伴会沿原目标接着做。") : t("先看看问题；在你回复前豆窝不会重新开始。"), autoNote].filter(Boolean).join(' ') };
  }
  if (status === 'paused') return objective.pause_requested === true
    ? { tone: 'stop', label: label(t("正在暂停")), title: t("等当前这一步做完就停"), text: t("不会再派发新的工作。") }
    : { tone: 'stop', label: label(t("已暂停")), title: t("推进已暂停"), text: [t("继续后沿原目标接着做，已有成果都在。"), autoNote].filter(Boolean).join(' ') };
  if (status === 'awaiting_continue') {
    const next = boundObjectiveNextStep(snapshot);
    return { tone: 'ask', label: label(t("等你说继续")), title: t("这一阶段做完了"),
      text: typeof next?.summary === 'string' && next.summary.trim() ? next.summary.trim().slice(0, 240) : t("点“继续推进”进入下一阶段，也可以先在下面补充要求。") };
  }
  if (status === 'complete' || ['ready_for_user', 'accepted'].includes(outcome)) {
    const selfOnly = tasks.filter((task) => task.status === 'self_checked' || task.status === 'accepted' && task.review_kind === 'self_check').length;
    const reviewed = tasks.filter((task) => task.status === 'accepted' && task.review_kind !== 'self_check').length;
    // Peer review here means a partner read the work; say so when nothing was run.
    const readOnlyReview = ['not_run', 'static_review'].includes(snapshot.collaboration?.outcome?.validation?.scope);
    const parts = [tasks.length ? selfOnly ? reviewed ? t("{p0} 项做完了：{p1} 项有另一位伙伴看过，{p2} 项只做了自查。", { p0: tasks.length, p1: reviewed, p2: selfOnly })
      : t("{p0} 项做完了，都只做了自查，没有别的伙伴看过。", { p0: tasks.length })
      : reviewed === 1 ? t("这 1 项有另一位伙伴看过并认可。") : t("{p0} 项都有另一位伙伴看过并认可。", { p0: reviewed }) : '',
      tasks.length && reviewed && readOnlyReview ? t("检查是读内容，没有运行测试或程序。") : '', state.deliveryPending ? t("代码还有改动没交付，见“交付”。") : ''].filter(Boolean);
    const accepted = Number.isFinite(run?.accepted_at) || outcome === 'accepted';
    // Chinese sentences join without a space; English ones with one.
    const joined = (items) => items.join(interfaceWordSpace());
    if (status === 'complete' && isReadOnlyDiscussion(snapshot) && !accepted) return { tone: 'done', label: label(t("已完成")), title: readOnlyLabel(snapshot, true),
      text: joined([...parts, readOnlyBuild(snapshot) ? t("要改文件，在“设置”里把权限换成 {p0}。", { p0: writablePermissionName(snapshot) }) : t("要开工，切到“动手”再发一句话。")]) };
    if (status === 'complete') return { tone: 'done', label: label(t("已完成")), title: accepted ? t("你已确认完成") : t("目标已完成"),
      text: joined(parts) || t("有新要求可以直接在下面说。") };
    return { tone: 'done', label: label(accepted ? t("已接受") : t("已交付")), title: accepted ? t("这一轮你已经接受") : t("伙伴说做完了，等你验收"),
      text: joined(parts) || t("看看成果，不满意直接在下面说哪里不对。") };
  }
  const stopReason = typeof objective?.stop_reason === 'string' ? objective.stop_reason : '';
  const doneNote = done ? t("已完成的 {p0} 项成果都在。", { p0: done }) : t("已有成果和记录都在。");
  if (['stopped', 'interrupted'].includes(status) && !hostResultInterruption(objective)) {
    const stoppedLabel = label(t("已停止"), room.updated_at);
    const kind = stopKind(snapshot, stopReason);
    if (kind === 'quota_wait') return { tone: 'stop', autoContinue: true, label: label(t("等额度恢复"), room.updated_at),
      title: t("伙伴额度都用完了，恢复后自动继续"), text: [doneNote, t("有伙伴额度恢复后豆窝会自己接着做，不用你操作。")].join(' ') };
    if (kind === 'auto' || kind === 'repeated') {
      const restartedStop = ['service_restarted', 'service_restarted_no_retry', 'service_shutdown'].includes(stopReason);
      return kind === 'repeated'
        ? { tone: 'stop', label: stoppedLabel, title: t("自动继续过一次，又停在同一处"),
          text: [t("没有新进展，所以不再自动继续。点“继续推进”再试，或在下面补充要求。"), doneNote].join(' ') }
        : { tone: 'go', autoContinue: true, label: label(t("马上自动继续"), room.updated_at),
          title: restartedStop ? t("豆窝服务重启过，马上自动接着做") : t("推进停了一下，马上自动接着做"),
          text: [t("几秒后自动继续，不用你操作。"), doneNote].join(' ') };
    }
  }
  if (outcome === 'unknown' || status === 'unknown' || room.status === 'unknown') return { tone: 'bad', label: label(t("结果待确认")),
    title: t("上一步的结果还没确认"), text: t("执行结果还没核对清楚，豆窝不会自动重跑。可以先看看讨论里的记录。") };
  const stopped = ['stopped', 'interrupted'].includes(status) || outcome === 'stopped' || closedStatuses.has(room.status);
  if (stopped) {
    const reason = typeof objective?.stop_reason === 'string' ? objective.stop_reason : '';
    const restarted = ['service_restarted', 'service_restarted_no_retry', 'service_shutdown'].includes(reason);
    return { tone: 'stop', label: label(status === 'interrupted' ? t("已中断") : t("已停止"), room.updated_at),
      title: hostResultInterruption(objective) ? t("伙伴的结果还没确认，推进停在这里") : restarted ? t("豆窝服务重启过，推进停在这里")
        : reason === 'all_partners_quota_exhausted' ? t("伙伴额度都用完了，推进停在这里") : status === 'interrupted' ? t("推进中断了") : t("推进已停止"),
      text: hostResultInterruption(objective) ? hostResultInterruptionMessage(snapshot)
        // A restart title already says what happened; the shutdown message would read as still closing.
        : [reason && !selfCorrectedCodes.has(reason) && !restarted ? stoppedReasonMessage(snapshot, reason) : '', done ? t("已完成的 {p0} 项成果都在，不会自动重跑。", { p0: done }) : t("已有成果和记录都在，不会自动重跑。"), autoNote].filter(Boolean).join(' ') };
  }
  const working = $('work-indicator').hidden ? '' : $('work-indicator-text').textContent;
  if (['running', 'checkpoint'].includes(status) || working || ['running', 'queued'].includes(room.status) || outcome === 'in_progress') {
    return { tone: 'go', label: label(t("进行中")), title: status === 'checkpoint' ? t("伙伴在核对这一阶段，安排下一步") : t("伙伴正在推进"),
      text: [working, tasks.length ? t("{p0} / {p1} 项已完成。", { p0: done, p1: tasks.length }) : ''].filter(Boolean).join(' · ') };
  }
  return { tone: 'done', label: label($('room-status').textContent), title: t("这一轮已经回复"), text: t("有新要求可以直接在下面说。") };
}
function nowAction(id, label, className) {
  const original = document.getElementById(id);
  original.dataset.mirrored = 'true';
  return { id, label: label || original.textContent, className, disabled: original.disabled, title: original.title || '', caption: original.disabled ? controlCaption(original) : '' };
}
function renderNowCard() {
  const card = $('now-card'); const snapshot = state.snapshot;
  $('conversation').classList.toggle('answering', currentObjective(snapshot)?.status === 'needs_user' && currentObjective(snapshot)?.can_answer === true && !$('message').disabled);
  if (!snapshot || state.selected !== snapshot.room.id || state.reader) { card.hidden = true; return; }
  const summary = nowSummary(snapshot);
  const actions = [];
  // Primary first, then secondary, the destructive stop last.
  const primary = nowPrimaryIds.find((id) => liveControl(id));
  const pause = liveControl('pause-objective'); const stop = liveControl('cancel-room');
  if (primary) {
    const labels = { 'accept-run': t("验收这一轮") };
    const original = document.getElementById(primary);
    // An automatic continue needs no click; the button only lets the user go now.
    actions.push(nowAction(primary, labels[primary] || (original.textContent === t("继续") ? t("继续推进") : ''), summary.autoContinue ? 'btn' : 'btn pri'));
  }
  if (summary.tone === 'ask' && summary.canAnswer) actions.push({ id: 'message', label: t("写回复"), className: actions.length ? 'btn' : 'btn pri', focus: true });
  if (pause) actions.push(nowAction('pause-objective', '', actions.length ? 'btn' : 'btn pri'));
  const blockedPrimary = !summary.autoContinue && actions.find((action) => action.className.includes('pri') && action.disabled);
  if (blockedPrimary) actions.push({ id: 'refresh', label: t("重新读取状态"), className: 'btn', disabled: $('refresh').disabled });
  if (stop && summary.tone === 'go' && !summary.autoContinue) actions.push(nowAction('cancel-room', t("停止任务"), 'btn ghost danger'));
  else if (stop) delete stop.dataset.mirrored;
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), summary.tone, summary.label, summary.title, summary.text, summary.question,
    (summary.blocked || []).map((task) => [task.id, task.title, task.blocked_reason, task.block_cause]), summary.canAnswer, $('message').disabled, actions]);
  card.hidden = false;
  if (card.dataset.fingerprint === fingerprint) return;
  card.dataset.fingerprint = fingerprint;
  card.className = `now-card tone-${summary.tone}`;
  $('now-label').textContent = summary.label;
  $('now-title').textContent = summary.title;
  $('now-text').textContent = summary.text;
  const detail = $('now-detail'); detail.replaceChildren();
  if (summary.question) detail.append(element('p', summary.question, 'now-text'));
  // Decision tasks only; anything the service replans stays on the progress board.
  const blocked = summary.blocked || [];
  for (const task of blocked.slice(0, 3)) {
    const box = blockedTaskBody(task, summary.canAnswer && !$('message').disabled, 'now-task');
    box.insertBefore(element('b', taskDisplayTitle(task.title || task.id)), box.firstChild);
    detail.append(box);
  }
  if (blocked.length > 3) detail.append(element('p', t("另有 {p0} 项，见“进展”。", { p0: blocked.length - 3 }), 'now-text'));
  if (blockedPrimary?.caption) detail.append(element('p', t("“{p0}”暂时用不了：{p1}", { p0: blockedPrimary.label, p1: blockedPrimary.caption }), 'now-blocked'));
  $('now-actions').replaceChildren(...actions.map((action) => {
    const button = element('button', action.label, action.className); button.type = 'button';
    button.disabled = !!action.disabled; button.dataset.mirror = action.id; if (action.title) button.title = action.title;
    button.addEventListener('click', () => {
      const target = document.getElementById(action.id);
      if (action.focus) { target?.focus(); return; }
      if (target && !target.disabled) target.click();
    });
    return button;
  }));
}

function currentRoomTab() {
  if (!state.snapshot || state.selected !== state.snapshot.room.id) return null;
  if (state.reader) return 'reader';
  if (materialsOpen()) return 'materials';
  return state.roomTabs.get(state.snapshot.room.id) || ((state.snapshot.team?.tasks || []).length ? 'progress' : 'discussion');
}
function selectRoomTab(name) {
  if (!state.snapshot || state.reader) return;
  if (name === 'materials') { openMaterials(); return; }
  state.roomTabs.set(state.snapshot.room.id, name);
  if (materialsOpen()) state.materialsRoom = null;
  renderControls();
  $(`tab-${name}`).focus({ preventScroll: true });
}
function renderTabs() {
  const tab = currentRoomTab(); const conversation = $('conversation');
  if (tab) conversation.dataset.tab = tab; else delete conversation.dataset.tab;
  $('room-tabs').hidden = !tab || tab === 'reader';
  for (const name of ['progress', 'discussion', 'delivery', 'materials']) {
    $(`tab-${name}`).setAttribute('aria-selected', String(tab === name));
    $(`tab-${name}`).tabIndex = tab === name ? 0 : -1;
  }
  const tasks = (state.snapshot?.team?.tasks || []).filter((task) => task && typeof task === 'object');
  const blocked = tasks.filter(taskNeedsUser).length;
  const progressCount = $('tab-progress-count');
  progressCount.hidden = !tasks.length;
  const counted = tasks.filter((task) => !taskWithdrawn(task));
  progressCount.textContent = blocked ? t("{p0} 项等你", { p0: blocked }) : `${counted.filter((task) => ['accepted', 'self_checked'].includes(task.status)).length}/${counted.length}`;
  progressCount.className = `tab-count${blocked ? ' ask' : ''}`;
  $('tab-delivery-count').hidden = !state.deliveryPending;
  $('tab-delivery-count').textContent = String(state.deliveryPending || '');
  $('delivery-empty').hidden = !$('code-status-card').hidden;
  renderProgressPane();
}

function roomGroup(room) {
  if (!activeEntry(room)) return 'none';
  const activity = roomWorkActivity(room); const objective = room.objective_status;
  if (activity && ['working', 'queued'].includes(activity.state) && (objective !== 'paused' || room.objective_pause_requested === true)) return 'go';
  if (roomStopKind(room)) return 'go';
  if (['needs_user', 'awaiting_continue'].includes(objective)) return 'ask';
  if (['running', 'checkpoint'].includes(objective)) return 'go';
  if (['paused', 'stopped', 'interrupted'].includes(objective)) return 'stop';
  if (objective === 'complete') return 'done';
  if (['needs_user', 'ready_for_user'].includes(room.outcome_state) || room.status === 'needs_user') return 'ask';
  if (room.outcome_state === 'unknown' || room.status === 'unknown') return 'bad';
  if (room.outcome_state === 'accepted' || room.status === 'completed') return 'done';
  if (['running', 'queued'].includes(room.status) || room.outcome_state === 'in_progress') return 'go';
  return 'stop';
}
const roomGroupLabels = localizedLabels({ ask: '需要你', go: '进行中', stop: '已停止', done: '已完成' });

const themePreferenceKey = 'beanest:theme';
function themePreference() {
  try { const value = localStorage.getItem(themePreferenceKey); return ['light', 'dark'].includes(value) ? value : 'system'; } catch (_) { return 'system'; }
}
function applyTheme(preference = themePreference()) {
  const root = document.documentElement;
  if (root) { if (preference === 'system') delete root.dataset.theme; else root.dataset.theme = preference; }
  const button = document.getElementById('interface-theme');
  if (button) button.textContent = ({ light: t("外观：浅色"), dark: t("外观：深色") })[preference] || t("外观：跟随系统");
}



class RequestError extends Error {
  constructor(message, { uncertain = false, code = '', details = null } = {}) {
    super(message);
    this.uncertain = uncertain;
    this.code = code;
    this.details = details;
  }
}

function element(tag, content = '', className = '') {
  const node = document.createElement(tag);
  node.textContent = content;
  if (className) node.className = className;
  return node;
}

function codeStatusEntry(roomId) {
  if (!state.codeStatusEntries.has(roomId)) state.codeStatusEntries.set(roomId,
    { workspaceId: null, workspaceExplicit: false, data: null, loaded: false, loading: false, error: '', request: 0, runId: null, runActive: false, repository: null, repositoryEditing: false, repositoryDraft: '', repositorySaving: false, repositoryError: '' });
  return state.codeStatusEntries.get(roomId);
}
function codeRepository(entry) {
  const value = entry.repository || entry.data?.delivery_repository;
  return value && ['bound', 'unbound', 'unknown'].includes(value.state) && Array.isArray(value.candidates)
    && value.candidates.every((candidate) => typeof candidate.workspace_id === 'string' && candidate.workspace_id
      && typeof candidate.label === 'string') ? value : null;
}
function codeRepositoryNotice(repository) {
  if (repository.state === 'unbound') return t("尚未指定交付仓库");
  if (repository.reason === 'delivery_repository_unavailable') return t("交付仓库暂时无法读取");
  if (repository.reason === 'delivery_repository_save_unconfirmed') return t("交付仓库保存结果待确认");
  return t("交付仓库需要重新确认");
}
function renderCodeRepository(entry) {
  const repository = codeRepository(entry); $('code-repository').hidden = !repository;
  if (!repository) return;
  const selected = repository.candidates.find((candidate) => candidate.workspace_id === repository.workspace_id);
  const selectedLabel = selected ? platformDisplayText(selected.label, selected.label_i18n) : null;
  const currentBranch = repository.state === 'bound' ? entry.data?.delivery_summary?.branch : null;
  const branchLabel = currentBranch?.name || (currentBranch?.detached ? t("当前提交（未在分支上）") : '');
  $('code-repository-label').textContent = repository.state === 'bound'
    ? t("交付仓库：{p0}", { p0: [selectedLabel || t("已指定仓库"), branchLabel].filter(Boolean).join(' · ') }) : repository.state === 'unbound' ? t("交付仓库")
      : repository.reason === 'delivery_repository_unavailable' ? t("交付仓库：{p0}（暂时无法读取）", { p0: selectedLabel || t("已指定仓库") }) : codeRepositoryNotice(repository);
  $('code-repository-edit').textContent = repository.workspace_id ? t("更换") : t("选择仓库");
  $('code-repository-edit').disabled = !state.connected || entry.repositorySaving;
  $('code-repository-picker').hidden = !entry.repositoryEditing;
  const choices = repository.candidates.map((candidate) => {
    const duplicate = repository.candidates.some((other) => other.workspace_id !== candidate.workspace_id && other.label === candidate.label);
    const path = entry.data?.workspaces?.find((workspace) => workspace.id === candidate.workspace_id)?.path;
    const label = platformDisplayText(candidate.label, candidate.label_i18n);
    return { ...candidate, displayLabel: duplicate && typeof path === 'string' ? `${label} · ${path}` : label };
  });
  const select = $('code-repository-choice'); const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), choices]);
  if (select.dataset.candidates !== fingerprint) {
    select.dataset.candidates = fingerprint;
    const placeholder = element('option', repository.candidates.length ? t("请选择交付仓库") : t("暂未找到可选的 Git 仓库")); placeholder.value = '';
    select.replaceChildren(placeholder, ...choices.map((candidate) => {
      const option = element('option', candidate.displayLabel); option.value = candidate.workspace_id; return option;
    }));
  }
  select.value = entry.repositoryDraft; select.disabled = entry.repositorySaving;
  $('code-repository-save').disabled = entry.repositorySaving || !state.connected || !repository.candidates.some((candidate) => candidate.workspace_id === entry.repositoryDraft);
  $('code-repository-save').textContent = entry.repositorySaving ? t("正在保存…") : t("保存");
  $('code-repository-cancel').disabled = entry.repositorySaving;
  $('code-repository-error').hidden = !entry.repositoryError; $('code-repository-error').textContent = entry.repositoryError;
}
$('code-repository-edit').addEventListener('click', () => {
  if (!state.selected) return;
  const entry = codeStatusEntry(state.selected), repository = codeRepository(entry);
  if (!repository || entry.repositorySaving) return;
  entry.repositoryEditing = true; entry.repositoryDraft = repository.workspace_id || ''; entry.repositoryError = '';
  renderCodeStatus(); $('code-repository-choice').focus();
});
$('code-repository-choice').addEventListener('change', () => {
  if (!state.selected) return;
  const entry = codeStatusEntry(state.selected); if (entry.repositorySaving) return;
  entry.repositoryDraft = $('code-repository-choice').value; renderCodeStatus();
});
$('code-repository-cancel').addEventListener('click', () => {
  if (!state.selected) return;
  const entry = codeStatusEntry(state.selected); if (entry.repositorySaving) return;
  entry.repositoryEditing = false; entry.repositoryError = ''; renderCodeStatus(); $('code-repository-edit').focus();
});
$('code-repository-save').addEventListener('click', async () => {
  const room = state.selected; if (!room || state.snapshot?.room.id !== room || !state.connected) return;
  const entry = codeStatusEntry(room), repository = codeRepository(entry), workspaceId = entry.repositoryDraft;
  if (!repository || entry.repositorySaving || !repository.candidates.some((candidate) => candidate.workspace_id === workspaceId)) return;
  ++entry.request; entry.loading = false;
  entry.repositorySaving = true; entry.repositoryError = '';
  const adopt = (data) => {
    if (!Array.isArray(data?.workspaces) || !['ready', 'not_git', 'unavailable', 'unbound'].includes(data.state)
        || !codeRepository({ repository: data.delivery_repository })) throw new Error('unconfirmed_code_status');
    entry.data = data; entry.workspaceId = data.workspace_id; entry.repository = data.delivery_repository || null;
    entry.loaded = true; entry.error = '';
  };
  try {
    renderCodeStatus();
    const response = await api(`/api/rooms/${room}/delivery-repository`, 'POST', { workspace_id: workspaceId });
    if (!codeRepository({ repository: response.delivery_repository }) || response.delivery_repository.state !== 'bound' || response.delivery_repository.workspace_id !== workspaceId) throw new Error('unconfirmed_repository');
    entry.repository = response.delivery_repository; entry.data = null; entry.workspaceId = null; entry.workspaceExplicit = false; entry.loaded = false;
    entry.repositoryEditing = false;
    if (state.selected === room) await refreshCodeStatus(true, true);
  } catch (_error) {
    // A lost save response is checked by a read, never by replaying the mutation.
    try {
      ++entry.request; entry.loading = false;
      const data = await api(`/api/rooms/${room}/code-status`);
      adopt(data);
      if (data.delivery_repository?.state === 'bound' && data.delivery_repository.workspace_id === workspaceId) {
        entry.repositoryEditing = false; entry.workspaceExplicit = false;
      } else entry.repositoryError = t("保存未确认；已重新核对当前交付仓库，你的选择已保留。");
    } catch (_readError) {
      entry.data = null; entry.workspaceId = null; entry.workspaceExplicit = false; entry.loaded = false;
      entry.repository = { ...repository, state: 'unknown', reason: 'delivery_repository_save_unconfirmed', workspace_id: null };
      entry.repositoryError = t("暂未确认是否保存，当前选择已保留。请刷新核对后再试。");
    }
  } finally {
    entry.repositorySaving = false; if (state.selected === room) renderCodeStatus();
  }
});

// Only a result old enough to have drifted during a run is worth calling out;
// the age comes from the room poll re-render, never from an extra Git read.
const CODE_STATUS_OLD_SECONDS = 600;
function codeStatusOldMinutes(observed, active) {
  if (!active || !Number.isFinite(observed)) return 0;
  const seconds = Date.now() / 1000 - observed;
  return seconds >= CODE_STATUS_OLD_SECONDS ? Math.floor(seconds / 60) : 0;
}
function codeRunActive(snapshot = state.snapshot) {
  if (snapshot?.room?.work_activity?.state === 'working' || snapshot?.live?.jobs?.some((job) => job.status === 'working')) return true;
  return !!snapshot?.current_run && !['complete', 'completed', 'ready_for_user', 'needs_user', 'stopped', 'interrupted', 'cancelled', 'unknown'].includes(snapshot.current_run.status);
}
function deliveryWorkBranch(delivery) {
  return delivery?.branch?.work_branch === true && delivery.branch.name ? delivery.branch : null;
}
function deliveryPendingOnlyFor(part, reasons) {
  return part?.state === 'pending' && reasons.includes(part.reason)
    && (part.checks || []).every((check) => check.state === 'confirmed' || reasons.includes(check.reason));
}
// A pending scope that needs no reminder: 'normal' lasts as long as the work
// stays on its branch, 'running' only until the task ends.
function deliveryScopeExpected(delivery, kind, active) {
  const workBranch = deliveryWorkBranch(delivery);
  if (kind === 'main') return workBranch && deliveryPendingOnlyFor(delivery.main, ['branch_not_on_mainline']) ? 'normal' : null;
  if (!active) return null;
  if (kind === 'workspace') return deliveryPendingOnlyFor(delivery.workspace, ['uncommitted_tracked_changes']) ? 'running' : null;
  return kind === 'remote' && workBranch && deliveryPendingOnlyFor(delivery.remote, ['work_branch_not_pushed', 'work_branch_no_upstream']) ? 'running' : null;
}
function taskGitDeliveryState(delivery, active = false) {
  const kinds = ['main', 'remote', ...['private', 'workspace'].filter((kind) => delivery[kind]?.state)];
  const states = kinds.map((kind) => deliveryScopeExpected(delivery, kind, active) ? 'progress' : delivery[kind]?.state);
  return states.includes('pending') ? 'pending' : states.every(state => state === 'confirmed') ? 'confirmed'
    : states.every(state => state === 'confirmed' || state === 'progress') ? 'progress' : 'unknown';
}
function workBranchDeliveryMessage(delivery, branch, active) {
  const remote = delivery.remote;
  if (delivery.private?.state === 'pending' || !active && delivery.workspace?.state === 'pending') return t("还有工作区成果待提交");
  if (active) return t("任务进行中，工作分支 {p0} 还在更新", { p0: branch.name });
  if (remote?.reason === 'work_branch_no_upstream') return t("工作分支 {p0} 未设远端", { p0: branch.name });
  if (remote?.state === 'pending') return t("工作分支 {p0} 还有 {p1} 次提交没推送", { p0: branch.name, p1: branch.upstream?.ahead });
  return remote?.state === 'confirmed' ? t("工作分支 {p0} 已推送", { p0: branch.name }) : t("工作分支 {p0} 的推送状态未知", { p0: branch.name });
}
function workBranchPushStatus(branch, remote) {
  const behind = branch.upstream?.behind;
  const body = remote?.state === 'confirmed' ? Number.isSafeInteger(behind) && behind > 0 ? t("工作分支已推送，远端还多 {p0} 次提交", { p0: behind }) : t("工作分支已推送")
    : remote?.reason === 'work_branch_no_upstream' ? t("工作分支未设远端")
      : remote?.state === 'pending' ? t("工作分支还有 {p0} 次提交没推送", { p0: branch.upstream?.ahead }) : t("工作分支的推送状态未知");
  return `${t("远端")}${interfaceColon()}${body}`;
}
function deliveryStatusBody(text) {
  const colon = interfaceColon(), index = text.indexOf(colon);
  return index < 0 ? text : text.slice(index + colon.length);
}
function taskDeliveryMessage(delivery, gitState, active = false) {
  const main = delivery.main?.state, remote = delivery.remote?.state, privateState = delivery.private?.state;
  const branch = deliveryWorkBranch(delivery);
  if (branch && deliveryScopeExpected(delivery, 'main', active)) return workBranchDeliveryMessage(delivery, branch, active);
  const workspaceRunning = deliveryScopeExpected(delivery, 'workspace', active) === 'running';
  if (main === 'pending') return t("还有任务成果未合入主分支");
  if (privateState === 'pending' || delivery.workspace?.state === 'pending' && !workspaceRunning) return t("还有工作区成果待提交");
  if (workspaceRunning) return t("任务进行中，工作区还有改动没提交");
  if (gitState === 'confirmed') return t("已提交代码已合入并同步 GitHub");
  if (main === 'confirmed') {
    if (remote === 'confirmed') return privateState === 'unknown' || delivery.workspace?.state === 'unknown' ? t("已提交代码已同步，工作区成果尚未核齐") : t("已提交代码已合入并同步 GitHub");
    // No GitHub remote means nothing will be pushed or checked later; say so instead of "not checked yet".
    if (delivery.remote?.reason === 'github_remote_unavailable') return t("主分支已合入。这个仓库没有 GitHub 远端，不会推送，也不用核对。");
    return remote === 'pending' ? t("主分支已合入，GitHub 还有内容待同步") : t("主分支已合入，GitHub 尚未核对");
  }
  return remote === 'confirmed' ? t("GitHub 当前主分支已同步，任务成果尚未核齐")
    : remote === 'pending' ? t("GitHub 还有提交待同步，任务成果尚未核齐") : t("主分支成果和 GitHub 状态尚未核齐");
}
function taskDeliveryStatus(part, kind) {
  if (part?.state === 'confirmed') return kind === 'main' ? t("主分支：已提交代码已合入")
    : kind === 'private' ? t("伙伴工作区：无额外未交付改动") : kind === 'workspace' ? t("工作区：无已跟踪改动待提交") : t("GitHub：当前主分支已同步");
  const reasons = {
    delivery_repository_unbound: t("尚未指定交付仓库"), delivery_repository_changed: t("交付仓库已变化，请重新选择"), delivery_repository_unavailable: t("交付仓库暂时无法读取，可刷新重试"),
    uncommitted_tracked_changes: t("还有已跟踪的改动未提交"), branch_not_on_mainline: t("还有分支上的提交未合入"),
    published_work_not_on_mainline: t("还有已交付成果未合入"), private_work_not_published: t("伙伴工作区还有成果未提交到共享项目"),
    retained_work_not_published: t("保留的任务成果尚未提交到共享项目"),
    published_work_baseline_unknown: t("缺少本任务起点记录，暂不能核齐成果"),
    published_work_mapping_unknown: t("尚未确认交付文件与主分支的对应位置"), published_work_unavailable: t("暂时无法读取交付文件进行核对"), published_work_mainline_unconfirmed: t("暂时无法比对任务成果与主分支"),
    private_workspace_unconfirmed: t("伙伴工作区存在冲突或缺少核对记录"), private_workspace_unavailable: t("暂时无法读取伙伴工作区"),
    retained_work_changed: t("保留成果已变化，需要重新核对"), retained_work_unconfirmed: t("保留成果缺少共享项目核对记录"),
    retained_work_unavailable: t("暂时无法读取保留的任务成果"), no_linked_repository: t("尚未找到本任务关联的 Git 仓库"),
    local_mainline_not_on_remote: t("当前主分支还有提交未同步"), not_checked: t("尚未检查远端，点击刷新核对"),
    github_remote_unavailable: t("没有 GitHub 远端，不会推送"), github_remote_rewrite: t("远端地址有重写配置，暂无法核对"),
    remote_mainline_unknown: t("暂未找到远端主分支"), remote_ancestry_unknown: t("远端与本地主分支的关系尚未确认"),
    workspace_unavailable: t("关联项目的 Git 状态暂不可用"), directory_unavailable: t("关联项目目录暂不可用"),
    mainline_unknown: t("尚未识别项目主分支"), mainline_ambiguous: t("存在多个主分支候选，尚未确定目标"),
    unborn_branch: t("仓库尚无提交"), git_unavailable: t("本机暂时无法使用 Git"), git_timeout: t("读取状态超时，可刷新重试"),
    git_filter_required: t("仓库文件转换配置暂不支持读取"), git_output_too_large: t("状态较多，尚未读取完整"),
    code_status_timeout: t("本次代码核对等待超时，可刷新重试"),
    git_invalid_output: t("Git 返回的状态暂时无法识别"), git_failed: t("Git 查询未成功，可刷新重试"), unsupported_mainline_file: t("主分支中的文件类型暂无法核对"),
  };
  const fallback = part?.state === 'pending' ? t("还有成果待处理，来源未提供具体原因") : t("来源未提供可确认的核对结果");
  return `${kind === 'main' ? t("主分支") : kind === 'private' ? t("伙伴工作区") : kind === 'workspace' ? t("工作区") : 'GitHub'}${interfaceColon()}${reasons[part?.reason] || fallback}`;
}
function taskDeliveryScopeStatus(delivery, kind, workspaces = []) {
  const branch = deliveryWorkBranch(delivery);
  if (branch && kind === 'main' && deliveryScopeExpected(delivery, 'main', false)) {
    return `${t("主分支")}${interfaceColon()}${t("在工作分支 {p0} 上，比 {p1} 多 {p2} 次提交", { p0: branch.name, p1: branch.mainline, p2: branch.ahead })}`;
  }
  if (branch && kind === 'remote') return workBranchPushStatus(branch, delivery.remote);
  const issues = (Array.isArray(delivery.issues) ? delivery.issues : []).filter((issue) => issue.scope === kind && issue.state !== 'confirmed');
  const checks = (Array.isArray(delivery[kind]?.checks) ? delivery[kind].checks : []).filter((check) => check.state !== 'confirmed');
  const problems = [...issues, ...checks];
  if (!problems.length) return taskDeliveryStatus(delivery[kind], kind);
  const messages = [...new Set(problems.map((issue) => {
    const workspace = workspaces.find((item) => item.id === issue.workspace_id);
    // Only the bound repository is named above the cards; any other workspace still needs its name here.
    const partner = workspace && state.snapshot?.members?.find((member) => member.id === workspace.provider)?.name;
    const namedAbove = workspace && workspace.id === state.codeStatusEntries.get(state.selected)?.repository?.workspace_id && !(delivery.coverage?.repositories > 1);
    const label = workspace && (partner || (namedAbove ? '' : platformDisplayText(workspace.label, workspace.label_i18n)));
    return taskDeliveryStatus(issue, kind) + (label ? interfaceParen(label) : '');
  }))];
  return messages.map((message, index) => index ? deliveryStatusBody(message) : message).join('；');
}
function taskPublishedMessage(published) {
  const files = published.files; const parts = [];
  if (Number.isSafeInteger(files?.total) && files.total > 0 && Number.isSafeInteger(files.matched)
      && files.matched >= 0 && files.matched <= files.total) {
    parts.push(t("工作快照对照：{p0}/{p1} 项与主分支一致。", { p0: files.matched, p1: files.total }));
    if (Number.isSafeInteger(files.missing) && files.missing > 0) parts.push(t("主分支未找到相同路径：{p0} 项。", { p0: files.missing }));
    if (Number.isSafeInteger(files.different) && files.different > 0) parts.push(t("与主分支内容或文件属性不同：{p0} 项。", { p0: files.different }));
    if (Number.isSafeInteger(files.missing) && Number.isSafeInteger(files.different)) {
      const unknown = files.total - files.matched - files.missing - files.different;
      if (unknown > 0) parts.push(t("另有 {p0} 项尚未完成内容对照。", { p0: unknown }));
    }
  } else if (published.state === 'confirmed') parts.push(t("工作快照对照：记录中的内容与主分支一致。"));
  else {
    const reasons = { published_work_baseline_unknown: t("缺少本任务起点记录，暂不能核齐成果"),
      published_work_unavailable: t("暂时无法读取，尚未完成核对"), published_work_mainline_unconfirmed: t("暂时无法比对交付文件与主分支") };
    parts.push(t("工作快照对照：{p0}。", { p0: reasons[published.reason] || (published.state === 'pending'
      ? t("尚未匹配到相同内容") : t("尚未确认与主分支的对应位置")) }));
  }
  parts.push(t("这是累计工作快照的内容对照，不代表待提交、待合并或必须发布的文件清单。"));
  return parts.join(interfaceWordSpace());
}
function codeStateMessage(data) {
  if (data?.state === 'ready') {
    const count = data.uncommitted?.total;
    const tracked = data.uncommitted?.tracked, untracked = data.uncommitted?.untracked;
    if (Number.isSafeInteger(tracked) && tracked >= 0 && Number.isSafeInteger(untracked) && untracked >= 0) {
      if (data.uncommitted.conflicted > 0) return t("有 {p0} 个文件冲突待处理", { p0: data.uncommitted.conflicted });
      if (tracked > 0) return t("有 {p0} 个已跟踪文件待提交", { p0: tracked });
      const mainline = data.mainline;
      if (mainline?.state === 'ready') {
        if (mainline.relation === 'merged') return t("已提交内容已合入 {p0}", { p0: mainline.name });
        if (mainline.relation === 'same_branch') return t("当前在主分支 {p0}", { p0: mainline.name });
        if (mainline.relation === 'unmerged') return t("已提交内容尚未合入 {p0}", { p0: mainline.name });
      }
      return untracked > 0 ? t("已跟踪文件无待提交改动") : t("工作区干净");
    }
    return Number.isSafeInteger(count) && count >= 0 ? count ? t("有 {p0} 个文件未提交", { p0: count }) : t("工作区干净") : t("未提交状态暂不可用");
  }
  const reasons = { directory_unavailable: t("项目目录暂不可用"), no_tracked_project: t("此目录尚无 Git 跟踪记录"),
    git_unavailable: t("本机暂时无法使用 Git"), git_filter_required: t("仓库使用自定义文件转换，暂未读取状态"), git_timeout: t("读取 Git 状态超时"), git_output_too_large: t("改动较多，状态暂未读完整") };
  return reasons[data?.reason] || (data?.state === 'not_git' ? t("此目录未使用 Git") : data?.state === 'unbound' ? t("未关联本机项目") : t("代码状态暂不可用"));
}
function renderCodeStatus() {
  const roomId = state.snapshot?.room.id; const card = $('code-status-card');
  // A room without a linked project has no repository to check (the service answers no_project_workspace
  // with nothing to choose); the delivery tab then says where its results are instead.
  const room = state.snapshot?.room; const known = roomId ? state.codeStatusEntries.get(roomId) : null;
  const noProject = !!room && !room.project_id && room.mode !== 'native_project' && !known?.error && !known?.repositorySaving && (known?.repository?.state || 'unbound') === 'unbound'
    && (!known?.data || known.data.state === 'unbound' && !(known.data.workspaces || []).length && !(known.data.delivery_repository?.candidates || []).length);
  card.hidden = !state.codeStatusSupported || !roomId || noProject;
  $('delivery-empty').textContent = noProject ? t("这个任务没有关联项目，没有代码要交付。伙伴的回复在“讨论”里，整理好的成果和文件在“资料”里。")
    : t("这次任务没有可核对的代码仓库。交付的文档和文件在“讨论”最后的交付卡片和“资料”里。");
  if (card.hidden) { state.codeStatusRendered = null; state.deliveryPending = 0; return; }
  const entry = codeStatusEntry(roomId); const data = entry.data;
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), codeStatusOldMinutes(data?.observed_at, codeRunActive()), [roomId, entry.workspaceId, data, entry.loading, entry.error, entry.repository, entry.repositoryEditing, entry.repositoryDraft, entry.repositorySaving, entry.repositoryError, state.connected, codeRunActive(), state.busy, !!state.pending, $('code-merge-target').value, naturalContinuation(state.snapshot, $('code-merge-target').value)?.canSend, state.snapshot?.members?.map((member) => member.id), roomReadOnlyNow(state.snapshot)]]);
  if (fingerprint === state.codeStatusRendered) return;
  state.codeStatusRendered = fingerprint;
  renderCodeRepository(entry);
  const select = $('code-workspace'); const selectedBefore = select.value;
  select.replaceChildren();
  for (const workspace of data?.workspaces || []) {
    const option = element('option', platformDisplayText(workspace.label, workspace.label_i18n)); option.value = workspace.id; select.append(option);
  }
  select.value = entry.workspaceId || data?.workspace_id || '';
  if (selectedBefore !== select.value) $('code-status-details').open = false;
  $('code-workspace-control').hidden = !(data?.workspaces?.length);
  $('code-status-refresh').disabled = entry.loading || entry.repositorySaving || !state.connected;
  const available = !!data && data.workspace_id === entry.workspaceId;
  const active = codeRunActive(); const stale = active || !state.connected || entry.loading || !!entry.error; const summary = $('code-local-summary');
  const oldMinutes = available ? codeStatusOldMinutes(data.observed_at, active) : 0;
  const oldAge = oldMinutes ? oldMinutes >= 60 ? t("{p0} 小时", { p0: Math.floor(oldMinutes / 60) }) : t("{p0} 分钟", { p0: oldMinutes }) : '';
  const checkPrefix = oldAge ? t("{p0}前的结果：", { p0: oldAge }) : stale ? t("上次检查：") : '';
  summary.textContent = entry.loading ? t("正在读取代码状态…") : entry.error || `${available && data.state === 'ready' ? checkPrefix : ''}${codeStateMessage(available ? data : null)}`;
  const delivery = available ? data?.delivery_summary : null;
  const deliveryKnown = delivery?.scope === 'task_linked_workspaces';
  const gitState = deliveryKnown ? taskGitDeliveryState(delivery, active) : null;
  const headline = $('code-status-summary');
  headline.textContent = available && data.state === 'ready' ? t("{p0}尚未取得本任务的交付核对记录", { p0: checkPrefix }) : summary.textContent;
  headline.className = 'code-status-summary';
  $('code-delivery-states').hidden = !deliveryKnown && (!available || data.state !== 'ready');
  $('code-delivery-note').hidden = $('code-delivery-states').hidden;
  let workspaceStatus = $('code-delivery-workspace');
  if (!workspaceStatus) {
    workspaceStatus = element('p'); workspaceStatus.id = 'code-delivery-workspace'; $('code-delivery-states').append(workspaceStatus);
  }
  workspaceStatus.hidden = !deliveryKnown || !['confirmed', 'pending', 'unknown'].includes(delivery.workspace?.state);
  workspaceStatus.textContent = '';
  let privateStatus = $('code-delivery-private');
  if (!privateStatus) {
    privateStatus = element('p'); privateStatus.id = 'code-delivery-private'; $('code-delivery-states').append(privateStatus);
  }
  privateStatus.hidden = !deliveryKnown || !delivery.private;
  privateStatus.textContent = '';
  $('code-delivery-main').textContent = t("主分支：当前来源未返回任务成果记录");
  $('code-delivery-remote').textContent = t("GitHub：当前来源未返回远端核对结果");
  $('code-delivery-note').textContent = t("可刷新重新读取，或请伙伴核对并提交同步。");
  if (deliveryKnown) {
    const prefix = checkPrefix;
    headline.textContent = prefix + taskDeliveryMessage(delivery, gitState, active);
    $('code-delivery-main').textContent = prefix + taskDeliveryScopeStatus(delivery, 'main', data.workspaces);
    $('code-delivery-remote').textContent = prefix + taskDeliveryScopeStatus(delivery, 'remote', data.workspaces);
    if (!workspaceStatus.hidden) workspaceStatus.textContent = prefix + taskDeliveryScopeStatus(delivery, 'workspace', data.workspaces);
    if (delivery.private) {
      privateStatus.textContent = prefix + taskDeliveryScopeStatus(delivery, 'private', data.workspaces);
      privateStatus.className = delivery.private.state === 'confirmed' ? 'detail-caption' : '';
    }
    $('code-delivery-note').textContent = delivery.main?.state !== 'confirmed' && delivery.remote?.state === 'confirmed'
      ? t("GitHub 已同步的是当前主分支；上面的任务成果问题仍需核对，不能据此判断全部交付完成。")
      : t("主分支核对已提交代码，工作区核对尚未交付的成果；GitHub 核对当前主分支。");
  }
  renderDeliverySteps(deliveryKnown ? delivery : null, data?.workspaces || [], codeRepository(entry), active);
  $('code-published-note').hidden = !deliveryKnown || !delivery.published;
  $('code-published-note').textContent = deliveryKnown && delivery.published
    ? checkPrefix + taskPublishedMessage(delivery.published) : '';
  const repository = codeRepository(entry);
  if (repository && repository.state !== 'bound') {
    headline.textContent = codeRepositoryNotice(repository);
    $('code-delivery-states').hidden = true;
    $('code-delivery-note').hidden = false;
    $('code-delivery-note').textContent = repository.reason === 'delivery_repository_unavailable'
      ? t("已有交付仓库保持不变，可以刷新重试。") : t("选择实际交付代码的仓库后，核对其主分支和 GitHub 同步情况。");
  }
  const counts = available && data.state === 'ready' ? data.uncommitted : null;
  const trackedKnown = Number.isSafeInteger(counts?.tracked) && counts.tracked >= 0;
  const merged = ['merged', 'same_branch'].includes(data?.mainline?.relation) && data?.mainline?.state === 'ready';
  const pendingTracked = trackedKnown ? counts.tracked > 0 : counts?.total > 0;
  summary.className = `code-status-summary${available && !stale && data.state === 'ready' && !pendingTracked && (counts?.total === 0 || merged) ? ' is-clean' : available && (pendingTracked && !active || data?.mainline?.relation === 'unmerged' && !data?.branch) ? ' is-dirty' : ''}`;
  if (deliveryKnown) headline.className = `code-status-summary${(!repository || repository.state === 'bound') && !stale && gitState === 'confirmed' ? ' is-clean' : gitState === 'pending' ? ' is-dirty' : ''}`;
  $('code-tracked-status').hidden = !trackedKnown;
  $('code-tracked-status').textContent = trackedKnown ? t("已跟踪文件：{p0}", { p0: counts.tracked ? t("{p0} 个有改动，尚未提交", { p0: counts.tracked }) : t("无待提交改动") }) : '';
  const untracked = counts?.untracked;
  $('code-untracked-status').hidden = !Number.isSafeInteger(untracked) || untracked <= 0;
  $('code-untracked-status').textContent = untracked > 0 ? t("另有 {p0} 项未纳入 Git（文件或目录）", { p0: untracked }) : '';
  $('code-untracked-note').hidden = $('code-untracked-status').hidden;
  const workspace = data?.workspaces?.find((item) => item.id === entry.workspaceId);
  const branch = available && data.state === 'ready'
    ? `${data.branch || (data.detached ? t("当前提交（未在分支上）") : t("分支不可用"))}${data.unborn ? t(" · 尚无提交") : ''}` : '';
  $('code-status-branch').textContent = [workspace?.label, branch].filter(Boolean).join(' · ');
  const observed = data?.observed_at;
  const freshness = Number.isFinite(observed) ? t("检查于 {p0}{p1}", { p0: timeLabel(observed, true), p1: !state.connected ? t(" · 本地连接已断开") : oldAge ? t(" · 任务进行中，这是 {p0}前的旧结果，点刷新查看最新状态", { p0: oldAge }) : active ? t(" · 任务进行中，刷新查看最新改动") : '' }) : '';
  const queryStatus = available ? entry.loading ? t("正在重新核对，以上为上次结果") : entry.error : '';
  const remoteCheckedAt = available && deliveryKnown ? delivery.remote?.checked_at : null;
  const remoteFreshness = Number.isFinite(remoteCheckedAt) ? t("GitHub 检查于 {p0}", { p0: timeLabel(remoteCheckedAt, true) }) : '';
  const pushBasis = available && deliveryKnown && deliveryWorkBranch(delivery) ? t("推送状态来自本机跟踪记录，未联网核对") : '';
  $('code-status-freshness').textContent = [freshness, remoteFreshness, pushBasis, queryStatus].filter(Boolean).join(' · ');
  const commit = available ? data.last_commit : null;
  $('code-last-commit').hidden = !commit;
  $('code-commit-subject').textContent = commit?.subject || ''; $('code-commit-hash').textContent = commit?.short_hash || '';
  $('code-commit-full').hidden = !commit; $('code-commit-full').textContent = commit ? t("最近提交：{p0}", { p0: commit.subject }) : '';
  const upstream = available && data.state === 'ready' ? data.upstream : null;
  $('code-upstream').hidden = !upstream;
  $('code-upstream').textContent = upstream?.state === 'ready' && Number.isSafeInteger(upstream.ahead) && upstream.ahead >= 0
    ? t("与跟踪分支{p0} 比较：领先 {p1} 次提交{p2} · 基于本地记录", { p0: upstream.name ? ` ${upstream.name}` : '', p1: upstream.ahead, p2: Number.isSafeInteger(upstream.behind) && upstream.behind > 0 ? t(" · 落后 {p0} 次提交", { p0: upstream.behind }) : '' })
    : upstream?.state === 'none' ? t("未设置跟踪分支") : upstream ? t("跟踪分支状态不可用") : '';
  renderCodeMerge(data, available && (!repository || repository.state === 'bound'), stale);
  $('code-status-details').hidden = !data || data.workspace_id !== entry.workspaceId || data.state === 'unbound' && !deliveryKnown;
  $('code-directory').textContent = workspace?.path ? t("检查目录：{p0}{p1}", { p0: workspace.path, p1: data.repository_root && data.repository_root !== workspace.path ? t("\nGit 仓库：{p0}", { p0: data.repository_root }) : '' }) : '';
  $('code-status-counts').textContent = counts ? [['staged', t("已暂存")], ['unstaged', t("未暂存")], ['untracked', t("未纳入 Git")], ['conflicted', t("冲突")]]
    .filter(([key]) => Number.isSafeInteger(counts[key]) && counts[key] >= 0).map(([key, label]) => `${label} ${counts[key]}`).join(' · ') : '';
  $('code-file-list').replaceChildren();
  for (const file of available && Array.isArray(data.files) ? data.files : []) {
    const status = `${file.index || ''}${file.worktree || ''}`;
    const label = status.includes('U') || ['AA', 'DD'].includes(status) ? t("冲突") : status.includes('?') ? t("未纳入 Git") : status.includes('D') ? t("删除") : status.includes('R') ? t("重命名") : status.includes('A') ? t("新增") : t("修改");
    $('code-file-list').append(element('li', `${label} · ${file.path}${file.original_path ? t("（原：{p0}）", { p0: file.original_path }) : ''}`));
  }
  const remaining = Number.isSafeInteger(counts?.total) ? counts.total - (data.files?.length || 0) : 0;
  $('code-files-more').hidden = !available || remaining <= 0;
  $('code-files-more').textContent = remaining > 0 ? t("另有 {p0} 个文件未在本次状态中列出。", { p0: remaining }) : '';
}
function renderDeliverySteps(delivery, workspaces, repository, active = false) {
  const steps = $('delivery-steps');
  steps.hidden = !delivery || !!repository && repository.state !== 'bound';
  $('code-status-card').classList.toggle('has-steps', !steps.hidden);
  const cells = steps.hidden ? [] : [['workspace', t("工作区")], ['main', t("主分支")], ['remote', deliveryWorkBranch(delivery) ? t("远端") : 'GitHub'], ['private', t("伙伴副本")]]
    .filter(([kind]) => ['main', 'remote'].includes(kind) || delivery[kind]);
  state.deliveryPending = cells.filter(([kind]) => delivery[kind]?.state === 'pending' && !deliveryScopeExpected(delivery, kind, active)).length;
  steps.replaceChildren(...cells.map(([kind, label]) => {
    const value = delivery[kind]?.state; const text = taskDeliveryScopeStatus(delivery, kind, workspaces);
    const expected = deliveryScopeExpected(delivery, kind, active);
    const tone = expected === 'running' ? 'wk' : expected === 'normal' ? 'todo' : value === 'confirmed' ? 'ok' : value === 'pending' ? 'ask' : 'st';
    const cell = element('div', '', `delivery-step ${expected || (value === 'pending' ? 'pending' : value === 'confirmed' ? 'confirmed' : 'unknown')}`);
    const title = element('span', '', 'delivery-step-title');
    title.append(element('span', ({ ok: '✓', ask: '!', st: '?' })[tone] || '', `si ${tone}`), element('span', label));
    if (expected === 'running') title.append(element('span', t("进行中"), 'delivery-step-tag'));
    cell.append(title, element('span', deliveryStatusBody(text)));
    return cell;
  }));
}
function codeDeliveryRequest(data, available) {
  if (!available) return null;
  if (data.delivery_summary?.scope === 'task_linked_workspaces') return data.delivery_summary.request;
  // Older hosts cannot prove task-wide delivery, but can still forward an
  // explicit request to a partner to inspect the selected repository.
  const root = data.repository_root;
  return data.state === 'ready' && typeof root === 'string' && root
    ? { available: true, repository_roots: [root] } : null;
}
// Read-only as the room's next work will run: the saved next-work settings when the host
// reports them, else the room's own mode and permission.
function roomReadOnlyNow(snapshot) {
  const desired = naturalRuntimeConfiguration(snapshot)?.desired;
  if (desired) return desired.work_mode === 'plan' || ['plan', 'read_only'].includes(desired.runtime_settings?.permission);
  return isReadOnlyDiscussion(snapshot);
}
function renderCodeMerge(data, available, stale) {
  const mainline = available && data.state === 'ready' ? data.mainline : null;
  const label = $('code-mainline'); label.hidden = !available || data.state !== 'ready';
  label.textContent = mainline?.state === 'ready'
    ? t("{p0}{p1} · 基于本地记录", { p0: stale ? t("上次检查：") : '', p1: mainline.relation === 'same_branch' ? t("当前就在主分支 {p0}", { p0: mainline.name })
      : mainline.relation === 'merged' ? t("已提交内容已合入主分支 {p0}", { p0: mainline.name })
        : mainline.relation === 'unmerged' ? t("已提交内容尚未合入 {p0} · 整条分支 {p1} 次提交", { p0: mainline.name, p1: mainline.ahead }) : t("与 {p0} 的合入状态未知", { p0: mainline.name }) })
    : t("主分支尚未确认，无法判断是否已合入；未提交与未推送状态单独列示。");
  const request = codeDeliveryRequest(data, available);
  const panel = $('code-merge-panel');
  const identity = JSON.stringify([state.selected, data?.workspace_id, request]);
  if (panel.dataset.identity !== identity) { panel.open = false; panel.dataset.identity = identity; $('code-merge-status').hidden = true; }
  // In a read-only room partners cannot commit or push; the request would spend a whole round for nothing.
  panel.hidden = request?.available !== true || roomReadOnlyNow(state.snapshot);
  if (panel.hidden) return;
  $('code-merge-description').textContent = t("本任务关联仓库：\n{p0}", { p0: (request.repository_roots || []).join('\n') });
  const options = $('code-merge-target'); const members = state.snapshot?.members || [];
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), members.map((member) => [member.id, member.name])]);
  if (options.dataset.members !== fingerprint) {
    const previous = options.value; options.dataset.members = fingerprint;
    options.replaceChildren(...[{ id: 'all', name: t("全体伙伴") }, ...members].map((member) => {
      const option = element('option', member.name || displayName(member.id)); option.value = member.id; return option;
    }));
    options.value = previous === 'all' || members.some((member) => member.id === previous) ? previous : 'all';
  }
  const bindingSaving = state.codeStatusEntries.get(state.selected)?.repositorySaving;
  const ready = state.connected && !state.busy && !state.managementBusy && !state.pending && !bindingSaving
    && naturalContinuation(state.snapshot, options.value)?.canSend === true;
  $('code-merge-send').disabled = !ready;
  $('code-merge-status').hidden = ready;
  if (!ready) { $('code-merge-status').textContent = bindingSaving ? t("交付仓库正在保存，完成后可发送核对要求。") : t("当前不能发送，请先查看聊天连接与任务状态；已有代码不会改变。"); }
}
async function sendCodeMergeRequest() {
  const snapshot = state.snapshot; const room = state.selected;
  const entry = state.codeStatusEntries.get(room);
  const request = codeDeliveryRequest(entry?.data, !!entry?.data);
  const target = $('code-merge-target').value; const natural = naturalContinuation(snapshot, target);
  const repository = entry && codeRepository(entry);
  if (!room || snapshot?.room.id !== room || !request?.available || (repository && repository.state !== 'bound') || !natural?.canSend || !state.connected
      || state.busy || state.managementBusy || state.pending || entry.repositorySaving
      || entry.data.workspace_id !== entry.workspaceId
      || (target !== 'all' && !snapshot.members.some((member) => member.id === target))) return;
  const targets = Array.isArray(request.targets) ? request.targets.filter((item) => typeof item.repository_root === 'string' && typeof item.target_branch === 'string' && item.target_branch).map((item) => `${item.repository_root} → ${item.target_branch}`).join(interfaceSemicolon()) : '';
  // Sent as the user's own message, so it follows the interface language like anything else they send.
  const text = t("请核对本任务所有伙伴已完成的相关成果，提交尚未提交的有效改动，核对实际默认主分支并合入，再推送到仓库已配置的 GitHub 远端，核对实际远端提交后报告结果。关联仓库：{p0}。{p1}请保留其他改动与成果，逐项核对未跟踪文件，只提交本任务应纳入版本管理的内容，不要一次 git add 全部文件，不要改写历史或强推，不要自行扩大权限；如无可用 GitHub 远端，请如实说明。",
    { p0: (request.repository_roots || []).join(interfaceSemicolon()), p1: targets ? t("上次检查的主分支：{p0}（执行时核对实际状态）。", { p0: targets }) : '' });
  if (!checkUserText(text)) return;
  saveDraft();
  // A dedicated action is a normal durable user message, detached from the
  // composer so its independent draft and recipient are never consumed.
  state.pending = capturePendingIntent({ kind: 'message', roomId: room, detached: true, composerIndependent: true,
    body: { request_id: crypto.randomUUID(), text, target, intent: 'continue', binding: { ...natural.binding } } }, snapshot);
  $('code-merge-panel').open = false;
  await submitPending();
}
$('code-merge-send').addEventListener('click', () => void sendCodeMergeRequest());
$('code-merge-target').addEventListener('change', renderCodeStatus);

async function refreshCodeStatus(force = false, refreshRemote = false) {
  if (!state.codeStatusSupported || !state.snapshot || state.selected !== state.snapshot.room.id) { renderCodeStatus(); return; }
  const roomId = state.selected; const entry = codeStatusEntry(roomId); const run = state.snapshot.current_run;
  const active = codeRunActive();
  const finished = entry.runId === run?.id && entry.runActive && !active;
  entry.runId = run?.id || null; entry.runActive = active;
  if (!force && !finished && (entry.loaded || entry.loading)) { renderCodeStatus(); return; }
  refreshRemote = refreshRemote || !entry.loaded || finished;
  const request = ++entry.request; const workspaceId = entry.workspaceExplicit ? entry.workspaceId : null;
  entry.loading = true; entry.error = ''; renderCodeStatus();
  try {
    const params = [];
    if (workspaceId) params.push(`workspace=${encodeURIComponent(workspaceId)}`);
    if (refreshRemote) params.push('refresh_remote=1');
    const query = params.length ? `?${params.join('&')}` : '';
    let data = await api(`/api/rooms/${roomId}/code-status${query}`);
    if (entry.request !== request) return;
    // An expired or invalidated remote observation needs one live check, not
    // recurring network work on every room poll or every unknown/error result.
    if (!refreshRemote && data?.delivery_repository?.state === 'bound'
        && data.delivery_summary?.scope === 'task_linked_workspaces' && data.delivery_summary.remote?.reason === 'not_checked') {
      const repositoryId = data.delivery_repository.workspace_id;
      const followup = await api(`/api/rooms/${roomId}/code-status${query}${query ? '&' : '?'}refresh_remote=1`);
      if (entry.request !== request) return;
      if (followup?.delivery_repository?.state !== 'bound' || followup.delivery_repository.workspace_id !== repositoryId) {
        throw new RequestError(t("交付仓库已变化，请重新选择"));
      }
      data = followup;
    }
    if (!Array.isArray(data?.workspaces) || !['ready', 'not_git', 'unavailable', 'unbound'].includes(data.state)
        || workspaceId && data.workspace_id !== workspaceId) throw new RequestError(t("项目代码状态暂时无法确认。"));
    entry.data = data; entry.workspaceId = data.workspace_id; entry.repository = data.delivery_repository || null;
    if (entry.repositoryError && entry.repository?.state === 'bound' && entry.repository.workspace_id === entry.repositoryDraft) {
      entry.repositoryError = ''; entry.repositoryEditing = false;
    }
    // A selected directory can be unavailable while the task-wide delivery
    // summary is valid. Only request failures belong in entry.error.
  } catch (error) { if (entry.request === request) entry.error = error.message || t("代码状态暂不可用，点击刷新重试。"); }
  finally { if (entry.request === request) { entry.loaded = true; entry.loading = false; if (state.selected === roomId) renderCodeStatus(); } }
}
$('code-status-refresh').addEventListener('click', () => void refreshCodeStatus(true, true));
$('code-workspace').addEventListener('change', () => {
  if (!state.snapshot) return;
  const entry = codeStatusEntry(state.snapshot.room.id); entry.workspaceId = $('code-workspace').value; entry.workspaceExplicit = true;
  $('code-status-details').open = false; void refreshCodeStatus(true);
});

// Media is a host-bound attachment, never an arbitrary path from model prose.
function mediaAsset(value) {
  return value && /^[0-9a-f]{64}$/.test(value.id || '') && /^[0-9a-f]{64}$/.test(value.sha256 || '')
    && typeof value.name === 'string' && typeof value.path === 'string'
    && ['image', 'video', 'audio', 'file'].includes(value.kind) && typeof value.mime === 'string'
    && Number.isSafeInteger(value.bytes) && value.bytes >= 0 && typeof value.available === 'boolean' ? value : null;
}
function currentMedia() {
  const roomId = state.snapshot?.room.id;
  if (!state.media || state.media.roomId !== roomId) {
    for (const node of $('media-items').children) for (const child of node.children)
      for (const image of child.children) if (image.tagName === 'IMG') image.removeAttribute('src');
    $('media-items').replaceChildren(); $('media-file-items').replaceChildren(); $('media-history-items').replaceChildren(); $('media-history-files').replaceChildren(); $('media-other').open = false;
    state.media = { roomId, assets: [], marker: null, loading: false, error: '', latestUserSeq: 0, limit: 8, historyLimit: 8, historyContext: null, filesFingerprint: null, historyFilesFingerprint: null, nodes: new Map(), tickets: new Map() };
  }
  return state.media;
}
async function mediaAccess(asset, roomId) {
  const media = currentMedia(); const key = `${asset.id}:${asset.sha256}`;
  const cached = media.tickets.get(key);
  if (cached?.ticket?.expires_at > Date.now() / 1000 + 30) return cached.ticket;
  if (cached?.promise) return cached.promise;
  const entry = {};
  entry.promise = api(`/api/rooms/${roomId}/media/${asset.id}/access`, 'POST', { sha256: asset.sha256 }).then((ticket) => {
    if (ticket?.asset_id !== asset.id || ticket.sha256 !== asset.sha256
        || !/^\/room-media\/[A-Za-z0-9_-]+$/.test(ticket.url || '') || !Number.isFinite(ticket.expires_at)
        || ticket.expires_at <= Date.now() / 1000) throw new RequestError(t("附件的版本或访问地址尚未确认。"));
    entry.ticket = ticket; entry.promise = null; return ticket;
  }).catch((error) => { media.tickets.delete(key); throw error; });
  media.tickets.set(key, entry); return entry.promise;
}
function releaseMedia() {
  for (const node of $('reader-body').children) if (['IMG', 'VIDEO', 'AUDIO'].includes(node.tagName)) {
    node.pause?.(); node.removeAttribute('src'); node.load?.();
  }
}
async function downloadMedia(asset, roomId) {
  if (state.selected !== roomId || state.deletedRooms.has(roomId)) return;
  try {
    const ticket = await mediaAccess(asset, roomId);
    if (state.selected !== roomId || state.deletedRooms.has(roomId)) return;
    const link = element('a'); link.href = `${ticket.url}?download=1`; link.download = asset.name;
    document.body.append(link); link.click(); link.remove();
  } catch (error) { showNotice(error.message || t("附件暂时无法下载。"), 'error'); }
}
function openMedia(asset, roomId = state.selected) {
  if (!state.mediaSupported || !mediaAsset(asset) || !asset.available || !state.selected || state.selected !== roomId) return;
  const trigger = document.activeElement; const returnTrigger = state.reader?.returnTrigger; closeReader();
  state.reader = { returnTrigger, mediaTrigger: trigger, roomId: state.selected, mediaAsset: asset, requestToken: crypto.randomUUID(), renderedKey: '', chatScroll: $('timeline').scrollTop };
  renderReader(); $('reader-body').focus({ preventScroll: true });
}
function renderMediaReader(reader) {
  if (reader.roomId !== state.snapshot?.room.id) { closeReader(); return; }
  $('task-materials').hidden = true; $('delivery-reader').hidden = false; $('timeline').hidden = true; $('composer').hidden = true;
  $('terminal-composer-note').hidden = true; $('runtime-settings').hidden = true; $('new-events').hidden = true;
  $('reader-source').hidden = true; $('reader-navigation').hidden = true; $('reader-update').hidden = true;
  const asset = reader.mediaAsset;
  $('reader-title').textContent = asset.name; $('reader-meta').textContent = `${asset.path} · ${(asset.bytes / 1024 / 1024).toFixed(1)} MB`;
  if (reader.renderedKey) return;
  reader.renderedKey = `${asset.id}:${asset.sha256}`;
  $('reader-body').className = 'reader-body media-reader';
  $('reader-body').replaceChildren(element('p', t("正在读取附件…"), 'detail-caption'));
  void mediaAccess(asset, reader.roomId).then((ticket) => {
    if (state.reader !== reader || reader.roomId !== state.selected) return;
    const body = $('reader-body'); body.replaceChildren();
    if (['image', 'video', 'audio'].includes(asset.kind)) {
      const player = element(asset.kind === 'image' ? 'img' : asset.kind);
      player.className = 'media-preview';
      if (asset.kind === 'image') player.alt = asset.name;
      else { player.controls = true; player.preload = 'metadata'; player.autoplay = false; player.playsInline = true; }
      const status = element('p', '', 'detail-caption'); status.hidden = true;
      player.addEventListener('error', () => {
        if (state.reader !== reader) return;
        status.hidden = false; status.textContent = t("暂时无法显示或播放此格式，可以下载查看，或重新读取附件。");
        const retry = element('button', t("重新读取附件"), 'quiet-button'); retry.type = 'button';
        retry.addEventListener('click', () => {
          currentMedia().tickets.delete(`${asset.id}:${asset.sha256}`); releaseMedia(); reader.renderedKey = ''; renderReader();
        });
        status.replaceChildren(element('span', status.textContent), retry);
      });
      player.src = ticket.url; body.append(player, status);
    } else body.append(element('p', t("这个文件可下载后查看。"), 'detail-caption'));
    const download = element('button', t("下载文件"), 'quiet-button'); download.type = 'button'; download.id = 'media-download';
    download.addEventListener('click', () => void downloadMedia(asset, reader.roomId)); body.append(download);
  }).catch((error) => {
    if (state.reader === reader) $('reader-body').replaceChildren(element('p', error.message || t("附件暂时无法读取。"), 'notice error'));
  });
}
function mediaCard(asset, media) {
  const card = element('article', '', 'media-card'); card.dataset.mediaId = asset.id;
  const view = element('button', '', 'media-open'); view.type = 'button'; view.disabled = !asset.available;
  view.setAttribute('aria-label', (asset.kind === 'video' ? t("查看视频：{p0}", { p0: asset.name }) : asset.kind === 'audio' ? t("查看音频：{p0}", { p0: asset.name })
    : asset.kind === 'image' ? t("查看图片：{p0}", { p0: asset.name }) : t("查看文件：{p0}", { p0: asset.name })));
  view.append(element('span', asset.kind === 'video' ? t("▶ 视频") : asset.kind === 'audio' ? t("♪ 音频") : asset.kind === 'image' ? t("图片") : t("文件"), 'media-kind'));
  if (asset.kind === 'image' && asset.available) {
    const image = element('img'); image.alt = asset.name; image.loading = 'lazy'; image.className = 'media-thumbnail'; view.append(image);
    void mediaAccess(asset, media.roomId).then((ticket) => { if (state.media === media && image.isConnected !== false) image.src = ticket.url; }).catch(() => { image.hidden = true; });
  }
  view.append(element('strong', asset.name), element('small', asset.available ? asset.path : t("文件当前不可用")));
  view.addEventListener('click', () => openMedia(asset, media.roomId)); card.append(view);
  const download = element('button', t("下载"), 'quiet-button'); download.type = 'button'; download.disabled = !asset.available;
  download.addEventListener('click', () => void downloadMedia(asset, media.roomId)); card.append(download);
  return card;
}
function mediaHistoryContext() {
  const snapshot = state.snapshot;
  const userSeq = (snapshot?.events || []).reduce((latest, event) => event.sender === 'user'
    && (event.kind === 'message' || event.kind === 'objective_decision') && Number.isSafeInteger(event.seq)
    ? Math.max(latest, event.seq) : latest, state.media?.roomId === snapshot?.room.id ? state.media.latestUserSeq : 0);
  return { userSeq, key: `${state.authScope}:${snapshot?.room.id}:${snapshot?.current_run?.id}:${userSeq}` };
}
function isCurrentMedia(asset, userSeq) {
  const snapshot = state.snapshot; const source = asset.source;
  // Publication can follow a technical carry: the generation run is not the
  // delivery run. This host projection only controls default presentation.
  if (source && Object.hasOwn(source, 'delivery')) {
    const delivery = source.delivery;
    return delivery?.run_id === snapshot?.current_run?.id && Number.isSafeInteger(delivery.seq) && delivery.seq > userSeq;
  }
  if (!source || source.run_id !== snapshot?.current_run?.id || !source.task_id || !Number.isSafeInteger(source.result_version)) return false;
  const task = snapshot.team?.tasks?.find((item) => item.id === source.task_id && item.result_version === source.result_version);
  if (!task || source.result_version < 1) return false;
  // A document's current run label is not delivery provenance. Only the host's
  // matching task completion after the latest supplement makes a new delivery.
  return snapshot.events.some((event) => event.kind === 'team_command' && event.command === 'complete'
    && event.task_id === source.task_id && event.run_id === source.run_id && event.seq > userSeq);
}
function renderMediaFiles(assets, holder, media, fingerprintField) {
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), assets.map((asset) => [asset.id, asset.sha256, asset.available, asset.name])]);
  if (fingerprint === media[fingerprintField]) return;
  media[fingerprintField] = fingerprint; holder.replaceChildren();
  for (const asset of assets) {
    const row = element('li'); const download = element('button', t("下载：{p0}", { p0: asset.name }), 'quiet-button'); download.type = 'button';
    download.disabled = !asset.available; download.title = asset.available ? asset.path : t("文件当前不可用");
    download.addEventListener('click', () => void downloadMedia(asset, media.roomId)); row.append(download); holder.append(row);
  }
}
function renderMediaShelf() {
  const media = currentMedia(); const visible = state.mediaSupported && !!state.snapshot;
  $('room-media').hidden = !visible || !media.assets.length && !media.error;
  $('media-status').hidden = !media.error; $('media-status').textContent = media.error;
  $('media-refresh').disabled = media.loading || !state.connected;
  if (!visible) { $('media-items').replaceChildren(); return; }
  const context = mediaHistoryContext();
  const fresh = media.assets.filter((asset) => isCurrentMedia(asset, context.userSeq));
  const history = media.assets.filter((asset) => !fresh.includes(asset));
  const playable = fresh.filter((asset) => asset.kind !== 'file');
  const previous = history.filter((asset) => asset.kind !== 'file');
  const files = fresh.filter((asset) => asset.kind === 'file');
  const historyFiles = history.filter((asset) => asset.kind === 'file');
  $('room-media').hidden = !visible || !fresh.length;
  $('media-heading').hidden = !fresh.length;
  $('media-history').hidden = !history.length;
  $('media-history').parentNode.hidden = !history.length && !media.error;
  $('media-history-label').textContent = t("此前附件 · {p0}", { p0: history.length });
  if (media.historyContext !== context.key) {
    media.historyContext = context.key;
    let open = state.mediaHistoryChoices.get(context.key) === true;
    try { open = sessionStorage.getItem(`beanest-media-history:${context.key}`) === 'open' || open; } catch (_) { /* Viewing stays usable without storage. */ }
    $('media-history').open = open;
  }
  const groups = [[playable.slice(0, media.limit), $('media-items')], [previous.slice(0, media.historyLimit), $('media-history-items')]];
  const keys = new Set(groups.flatMap(([items]) => items.map((asset) => `${asset.id}:${asset.sha256}:${asset.available}`)));
  for (const [key, node] of media.nodes) if (!keys.has(key)) { node.remove(); media.nodes.delete(key); }
  for (const [items, holder] of groups) for (const asset of items) {
    const key = `${asset.id}:${asset.sha256}:${asset.available}`;
    if (!media.nodes.has(key)) media.nodes.set(key, mediaCard(asset, media));
    const node = media.nodes.get(key); if (node.parentNode !== holder) holder.append(node);
  }
  $('media-more').hidden = playable.length <= media.limit;
  $('media-history-more').hidden = previous.length <= media.historyLimit;
  $('media-other').hidden = !files.length;
  $('media-other-label').textContent = t("其他附件 · {p0}", { p0: files.length });
  renderMediaFiles(files, $('media-file-items'), media, 'filesFingerprint');
  renderMediaFiles(historyFiles, $('media-history-files'), media, 'historyFilesFingerprint');
  renderMaterials();
}
async function refreshRoomMedia(force = false) {
  if (!state.mediaSupported || !state.snapshot || state.selected !== state.snapshot.room.id) { renderMediaShelf(); return; }
  const media = currentMedia(); const snapshot = state.snapshot;
  const marker = `${snapshot.current_run?.id || ''}:${snapshot.project?.revision || 0}:${snapshot.through_seq ?? snapshot.events.at(-1)?.seq ?? 0}`;
  if (media.loading || !force && media.marker === marker) return;
  media.loading = true; renderMediaShelf();
  let follow = false;
  try {
    const result = await api(`/api/rooms/${media.roomId}/media`);
    if (state.media !== media || state.selected !== media.roomId) return;
    if (!Array.isArray(result?.assets) || result.assets.some((asset) => !mediaAsset(asset))
        || result.latest_user_seq !== undefined && (!Number.isSafeInteger(result.latest_user_seq) || result.latest_user_seq < 0)) throw new RequestError(t("附件清单暂时无法确认。"));
    follow = !state.reader && !materialsOpen() && nearBottom();
    media.latestUserSeq = Math.max(media.latestUserSeq, result.latest_user_seq ?? 0);
    media.assets = [...result.assets].sort((a, b) => Number(a.kind === 'file') - Number(b.kind === 'file')); media.marker = marker; media.error = '';
  } catch (error) { if (state.media === media) media.error = error.message || t("附件暂时无法加载。"); }
  finally {
    media.loading = false;
    if (state.media === media) {
      renderMediaShelf();
      if (follow && !state.reader && !materialsOpen() && state.selected === media.roomId) scrollToLatest();
    }
  }
}
function referencedMediaAsset(reference, doc) {
  let candidates = currentMedia().assets.filter((asset) => asset.reference_aliases?.includes(reference.href));
  if (doc && candidates.length > 1) candidates = candidates.filter((asset) => asset.source?.document_path === doc.path
    && (!doc.binding?.run_id || asset.source?.run_id === doc.binding.run_id));
  return candidates.length === 1 ? candidates[0] : null;
}
function mediaReferenceNode(reference, doc = null) {
  if (!state.mediaSupported) return null;
  const roomId = state.snapshot?.room.id;
  const button = element('button', reference.label || (reference.image ? t("查看图片") : t("查看文件")), 'media-reference'); button.type = 'button';
  const inline = referencedMediaAsset(reference, doc);
  if (reference.image && inline?.kind === 'image' && inline.available) {
    const image = element('img'); image.alt = reference.label || inline.name; image.loading = 'lazy';
    button.className += ' media-image-reference'; button.append(image);
    void mediaAccess(inline, roomId).then((ticket) => { if (state.selected === roomId && button.isConnected !== false) image.src = ticket.url; }).catch(() => { image.hidden = true; });
  }
  button.addEventListener('click', async () => {
    if (state.selected !== roomId || button.disabled) return;
    button.disabled = true;
    try {
      const matched = referencedMediaAsset(reference, doc);
      const result = matched ? { asset: matched }
        : await api(`/api/rooms/${roomId}/media/resolve?reference=${encodeURIComponent(reference.href)}`);
      if (state.selected !== roomId) return;
      const asset = mediaAsset(result?.asset);
      if (!asset || !asset.available) throw new RequestError(t("这份附件当前不可读取。"));
      openMedia(asset, roomId);
    } catch (error) { if (state.selected === roomId) showNotice(error.message || t("这份链接暂未关联可读取的附件。"), 'error'); }
    finally { button.disabled = false; }
  });
  return button;
}
function appendMediaReferences(holder, text, doc = null) {
  if (!state.mediaSupported) return;
  const references = window.BeanestMarkdown.localReferences(text); const seen = new Set();
  const links = element('div', '', 'media-references');
  for (const reference of references) if (!seen.has(reference.href)) { seen.add(reference.href); links.append(mediaReferenceNode(reference, doc)); }
  if (links.children.length) holder.append(links);
}
$('media-refresh').addEventListener('click', () => void refreshRoomMedia(true));
$('media-more').addEventListener('click', () => { currentMedia().limit += 8; renderMediaShelf(); });
$('media-history-more').addEventListener('click', () => { currentMedia().historyLimit += 8; renderMediaShelf(); });
$('media-history').addEventListener('toggle', () => {
  const media = currentMedia(); if (!media.historyContext) return;
  const open = $('media-history').open; state.mediaHistoryChoices.set(media.historyContext, open);
  try { sessionStorage.setItem(`beanest-media-history:${media.historyContext}`, open ? 'open' : 'closed'); } catch (_) { /* Optional view preference only. */ }
});

function fileReadingTitle(file) {
  if (/\.(?:md|markdown)$/i.test(file.path)) {
    const heading = file.content.slice(0, 16 * 1024).match(/^\s*#\s+(.+?)\s*#*\s*$/m);
    if (heading?.[1]?.trim()) return heading[1].trim().slice(0, 120);
  }
  return file.path.split('/').pop() || file.path;
}

function deliveryDocuments(snapshot) {
  if (!snapshot) return [];
  const documents = [];
  const project = snapshot.project;
  const run = snapshot.current_run;
  if (project?.available && Number.isSafeInteger(project.revision) && Array.isArray(project.files)) {
    for (const file of project.files) {
      if (typeof file.path !== 'string' || typeof file.content !== 'string' || !/^[0-9a-f]{64}$/.test(file.sha256 || '')) continue;
      documents.push({ key: `${snapshot.room.id}:${project.revision}:${file.sha256}:${file.path}`, path: file.path,
        title: fileReadingTitle(file), file: true, content: file.content, markdown: /\.(?:md|markdown)$/i.test(file.path),
        meta: t("文件版本 {p0} · {p1}", { p0: project.revision, p1: !file.owner || file.owner === 'source' ? t("项目原稿") : displayName(file.owner) }), revision: project.revision, sha256: file.sha256 });
    }
  }
  const objective = currentObjective(snapshot); const delivery = objective?.delivery;
  if (project?.available && delivery && Number.isSafeInteger(project.revision) && project.revision >= 0
      && /^[0-9a-f]{64}$/.test(project.snapshot_sha256 || '') && delivery.revision === project.revision
      && delivery.digest === project.snapshot_sha256 && Array.isArray(delivery.files)) {
    for (const file of delivery.files.slice(0, 1024)) {
      if (file?.verified !== true || typeof file.path !== 'string' || !file.path || !/^[0-9a-f]{64}$/.test(file.sha256 || '')
          || documents.some((doc) => doc.file && doc.path === file.path && doc.sha256 === file.sha256)) continue;
      const binding = runBinding({ id: file.run_id, revision: file.revision, digest: file.digest });
      if (!binding || !Number.isSafeInteger(binding.revision)) continue;
      documents.push({ key: `${snapshot.room.id}:delivery:${objective.id}:${binding.run_id}:${binding.revision}:${binding.digest}:${file.sha256}:${file.path}`,
        path: file.path, title: file.path.split('/').pop() || file.path, file: true, lazy: true, objectiveId: objective.id,
        sourceRoomId: snapshot.room.id, binding: { ...binding, path: file.path, sha256: file.sha256 },
        revision: binding.revision, sha256: file.sha256, markdown: /\.(?:md|markdown)$/i.test(file.path) });
    }
  }
  for (const doc of documents) {
    const provenance = objectiveDeliveredFile(snapshot, doc);
    if (!provenance) continue;
    doc.deliveryReview = provenance.review_kind === 'self_check' ? t("只做了自查") : provenance.review_kind === 'independent_review' ? t("伙伴看过") : t("审核方式待核对");
    doc.meta = t("本目标成果 · 文件版本 {p0} · {p1}", { p0: doc.revision, p1: doc.deliveryReview });
  }
  const tasks = Array.isArray(snapshot.collaboration?.tasks) ? snapshot.collaboration.tasks : [];
  for (const task of tasks.slice(0, 64)) {
    if (typeof task.result !== 'string' || !task.result.trim()) continue;
    const taskRecord = snapshot.team?.tasks?.find((entry) => entry.id === task.id) || task;
    const hasFileDelivery = Array.isArray(taskRecord.result_files) && taskRecord.result_files.some((result) =>
      documents.some((doc) => doc.file && doc.path === result.path && doc.sha256 === result.sha256));
    if ((run?.work_mode || snapshot.room.work_mode) !== 'plan' && hasFileDelivery) continue;
    documents.push({ key: `${snapshot.room.id}:${run?.id || ''}:${run?.digest || ''}:task:${task.id || task.title}:${task.result_text_ref?.sha256 || task.result}`,
      path: `task:${task.id || task.title}`, title: task.title ? taskDisplayTitle(task.title) : t("任务答复"), content: task.result, textRef: task.result_text_ref || null, lazy: !!task.result_text_ref, markdown: true,
      meta: t("本轮答复 · {p0} · {p1}", { p0: task.owner ? displayName(task.owner) : t("伙伴"), p1: taskStatusLabels[task.status] || t("待核对") }) });
  }
  if ((run?.work_mode || snapshot.room.work_mode) === 'plan' && !tasks.length) {
    const memberIds = new Set((snapshot.members || []).map((member) => member.id || member.provider));
    for (const event of snapshot.events.filter((event) => (!run || event.run_id === run.id) && event.kind === 'message'
        && memberIds.has(event.sender) && typeof event.text === 'string' && event.text.trim()).slice(-8)) {
      documents.push({ key: `${snapshot.room.id}:${run?.id || 'single'}:${run?.digest || ''}:message:${event.seq}:${event.text}`,
        path: `message:${event.seq}`, title: t("{p0} 的答复", { p0: displayName(event.sender) }), content: event.text, markdown: true, meta: t("本轮方案讨论") });
    }
  }
  const inherited = baselineDocuments(snapshot).filter((baseline) => !documents.some((doc) => {
    const source = objectiveDeliveredFile(snapshot, doc);
    return source && (source.source_room_id ?? snapshot.room.id) === baseline.sourceRoomId
      && source.run_id === baseline.binding.run_id && source.revision === baseline.binding.revision
      && source.digest === baseline.binding.digest && source.path === baseline.path && source.sha256 === baseline.sha256
      && source.review_kind === baseline.reviewKind;
  }));
  return documents.concat(inherited);
}

function renderDocumentShortcuts(snapshot) {
  const box = $('room-documents');
  const documents = deliveryDocuments(snapshot).filter((doc) => doc.file && /\.(?:md|markdown|txt|rst|adoc|html?)$/i.test(doc.path));
  box.hidden = !documents.length;
  const isCurrent = (doc) => !doc.historical && !!objectiveDeliveredFile(snapshot, doc);
  const current = documents.filter(isCurrent), earlier = documents.filter((doc) => !isCurrent(doc));
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), [snapshot?.room.id, current.map((doc) => doc.key), documents.map((doc) => [doc.key, doc.title, doc.deliveryReview, doc.meta])]]);
  if (box.dataset.fingerprint === fingerprint) return;
  box.dataset.fingerprint = fingerprint;
  const link = (doc) => {
    const button = readerLink(doc);
    if (!doc.deliveryReview && doc.meta) button.append(element('small', doc.meta, 'detail-caption'));
    return button;
  };
  if (box.dataset.room !== snapshot?.room.id) $('document-more').open = false;
  box.dataset.room = snapshot?.room.id || '';
  $('document-shortcuts').replaceChildren(...current.slice(0, 6).map(link));
  const more = current.slice(6).concat(earlier);
  $('document-more').hidden = !more.length;
  $('document-more-label').textContent = `${current.length > 6 ? t("更多与此前文档") : t("此前与已保存文档")} · ${more.length}`;
  $('document-more-items').replaceChildren(...more.map(link));
}

function baselineDocuments(snapshot) {
  const objective = currentObjective(snapshot); const baseline = objective?.baseline;
  const sourceRoomId = baseline?.source_room_id === undefined ? snapshot.room.id : baseline.source_room_id;
  const binding = runBinding({ id: baseline?.run_id, revision: baseline?.revision, digest: baseline?.digest });
  if (!uuidPattern.test(sourceRoomId) || !binding || !Number.isSafeInteger(binding.revision) || baseline.verified !== true || !Array.isArray(baseline.files)) return [];
  return baseline.files.filter((file) => typeof file.path === 'string' && file.path
    && /^[0-9a-f]{64}$/.test(file.sha256 || '') && file.run_id === binding.run_id).map((file) => ({
    key: `${snapshot.room.id}:baseline:${objective.id}:${sourceRoomId}:${binding.run_id}:${binding.revision}:${binding.digest}:${file.sha256}:${file.path}`,
    path: file.path, title: file.path.split('/').pop() || file.path, file: true, historical: true,
    objectiveId: objective.id, sourceRoomId, binding: { ...binding, path: file.path, sha256: file.sha256 },
    sha256: file.sha256, revision: binding.revision, markdown: /\.(?:md|markdown)$/i.test(file.path),
    reviewKind: file.review_kind,
    deliveryReview: file.review_kind === 'self_check' ? t("只做了自查") : file.review_kind === 'independent_review' ? t("伙伴看过") : t("审核方式待核对"),
    meta: t("沿用的交付（上一轮） · 文件版本 {p0}", { p0: binding.revision }),
  }));
}

function objectiveDeliveredFile(snapshot, doc) {
  const delivery = currentObjective(snapshot)?.delivery; const project = snapshot.project;
  if (!doc.file || doc.historical || !delivery || delivery.revision !== project?.revision || delivery.digest !== project?.snapshot_sha256) return null;
  return Array.isArray(delivery.files) ? delivery.files.find((file) => file.verified === true && file.path === doc.path && file.sha256 === doc.sha256) || null : null;
}

function readerLink(doc, className = 'delivery-file') {
  const button = element('button', '', className); button.type = 'button';
  if (doc.file) {
    button.append(element('strong', doc.title, 'delivery-file-title'));
    if (doc.path !== doc.title) button.append(element('small', doc.path, 'delivery-file-path'));
    if (doc.deliveryReview) button.append(element('small', doc.deliveryReview, 'detail-caption'));
    button.setAttribute('aria-label', `${doc.title} · ${doc.path}`);
  } else button.textContent = doc.title;
  button.dataset.documentPath = doc.path;
  button.dataset.documentKey = doc.key;
  button.addEventListener('click', () => openDelivery(doc.key, button));
  return button;
}
function requiresBoundRead(doc) { return doc?.historical === true || doc?.lazy === true; }

function openDelivery(key, trigger = null) {
  const doc = deliveryDocuments(state.snapshot).find((entry) => entry.key === key);
  if (!doc) { showNotice(t("这份内容的版本已变化，请从当前交付中重新打开。")); return; }
  const previous = state.reader;
  if (previous) previous.positions.set(previous.renderedKey, $('reader-body').scrollTop);
  state.reader = { returnTrigger: previous?.returnTrigger || trigger, roomId: state.snapshot.room.id, runId: state.snapshot.current_run?.id, key, path: doc.path, source: false,
    page: Math.floor(deliveryDocuments(state.snapshot).findIndex((entry) => entry.key === key) / 128),
    boundObjectiveId: requiresBoundRead(doc) ? doc.objectiveId : null, boundSourceRoomId: doc.sourceRoomId || null,
    loading: requiresBoundRead(doc), requestToken: crypto.randomUUID(), loadedDocument: null, error: '',
    fileSha: doc.sha256, content: doc.content, renderedKey: '', positions: previous?.positions || new Map(), chatScroll: previous?.chatScroll ?? $('timeline').scrollTop };
  renderReader(); $('reader-body').focus({ preventScroll: true });
  if (requiresBoundRead(doc)) void readBoundDelivery(state.reader, doc);
}

async function readBoundDelivery(reader, doc) {
  const requestToken = reader.requestToken;
  const stillCurrent = () => state.reader === reader && state.snapshot?.room.id === reader.roomId
    && state.reader.requestToken === requestToken
    && reader.boundSourceRoomId === doc.sourceRoomId
    && currentObjective()?.id === reader.boundObjectiveId
    && deliveryDocuments(state.snapshot).some((entry) => entry.key === doc.key);
  try {
    const binding = doc.binding;
    const query = Object.entries(binding || {}).map(([key, value]) => `${key}=${encodeURIComponent(value)}`).join('&');
    if (doc.textRef) {
      const content = await readExactText(doc.textRef);
      if (state.reader !== reader || !deliveryDocuments(state.snapshot).some((entry) => entry.key === doc.key)) return;
      reader.loadedDocument = { ...doc, content }; reader.loading = false; reader.renderedKey = ''; renderReader(); return;
    }
    const response = await api(`/api/rooms/${encodeURIComponent(doc.sourceRoomId)}/file?${query}`);
    if (!stillCurrent()) return;
    if (Object.entries(binding).some(([key, value]) => response?.[key] !== value) || typeof response.content !== 'string' || !crypto.subtle) throw new RequestError(executionLabels.objective_binding_changed);
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(response.content));
    const sha = [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('');
    if (!stillCurrent()) return;
    if (sha !== binding.sha256) throw new RequestError(executionLabels.objective_binding_changed);
    reader.loadedDocument = { ...doc, content: response.content, title: fileReadingTitle({ path: doc.path, content: response.content }) };
  } catch (error) {
    if (!stillCurrent()) return;
    reader.error = error.code === 'objective_binding_changed' ? executionLabels.objective_binding_changed
      : error.message || t("这份交付暂时无法读取，请稍后重新打开。");
  }
  if (!stillCurrent()) return;
  reader.loading = false; reader.renderedKey = ''; renderReader();
}

function materialsOpen() { return !!state.materialsRoom && state.materialsRoom === state.snapshot?.room.id; }
function renderMaterials() {
  if (state.materialsRoom && state.materialsRoom !== state.snapshot?.room.id) state.materialsRoom = null;
  const open = materialsOpen();
  $('task-materials-open').hidden = !state.snapshot;
  $('task-materials-open').disabled = !!state.reader;
  $('task-materials-open').setAttribute('aria-expanded', String(open));
  $('task-materials').hidden = !open || !!state.reader;
  $('timeline').hidden = open || !!state.reader;
  if (open) $('new-events').hidden = true;
  $('reader-back').textContent = open ? t("← 返回任务资料") : t("← 返回聊天");
  $('task-materials-empty').hidden = !$('materials-outcomes').hidden || !$('room-documents').hidden || !$('media-history').hidden;
  $('task-materials-refresh').disabled = currentMedia().loading || !state.connected || !state.mediaSupported;
  $('materials-media-status').hidden = !currentMedia().error;
  $('materials-media-status').textContent = currentMedia().error;
}
function openMaterials() {
  if (!state.snapshot || state.reader) return;
  state.materialsRoom = state.snapshot.room.id;
  renderControls(); $('task-materials-close').focus({ preventScroll: true });
}
function closeMaterials() {
  if (!materialsOpen()) return;
  if (state.reader) closeReader();
  state.materialsRoom = null; renderControls();
  $('task-materials-open').focus({ preventScroll: true });
}
$('task-materials-open').addEventListener('click', openMaterials);
$('task-materials-close').addEventListener('click', closeMaterials);
$('task-materials-refresh').addEventListener('click', () => void refreshRoomMedia(true));
$('task-materials').addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !event.defaultPrevented) { event.preventDefault(); closeMaterials(); }
});

function closeReader() {
  const previous = state.reader;
  releaseMedia(); state.reader = null; $('delivery-reader').hidden = true; $('timeline').hidden = false; $('composer').hidden = false;
  if (previous) $('timeline').scrollTop = previous.chatScroll;
  renderControls();
  const trigger = previous?.returnTrigger || previous?.mediaTrigger;
  if (materialsOpen()) { if (trigger?.isConnected !== false && trigger) trigger.focus({ preventScroll: true }); else $('task-materials-close').focus({ preventScroll: true }); }
  else if (previous?.mediaTrigger?.isConnected !== false && previous?.mediaTrigger && !previous.mediaTrigger.hidden) previous.mediaTrigger.focus({ preventScroll: true });
}

function renderReader() {
  const reader = state.reader;
  if (!reader) return;
  $('task-materials-open').disabled = true;
  if (reader.mediaAsset) { renderMediaReader(reader); return; }
  $('reader-navigation').hidden = false;
  if (reader.roomId !== state.snapshot?.room.id || (reader.boundObjectiveId
    ? reader.boundObjectiveId !== currentObjective()?.id : reader.runId !== state.snapshot?.current_run?.id)) { closeReader(); return; }
  $('task-materials').hidden = true; $('delivery-reader').hidden = false; $('timeline').hidden = true; $('composer').hidden = true; $('terminal-composer-note').hidden = true; $('runtime-settings').hidden = true; $('new-events').hidden = true;
  const docs = deliveryDocuments(state.snapshot).sort((a, b) => Number(!!b.markdown) - Number(!!a.markdown));
  let doc = docs.find((entry) => entry.key === reader.key);
  if (requiresBoundRead(doc)) {
    if (reader.loadedDocument?.key === doc.key) doc = reader.loadedDocument;
    else {
      $('reader-title').textContent = doc.title; $('reader-meta').textContent = doc.meta;
      $('reader-source').hidden = true; $('reader-update').hidden = true;
      if (reader.renderedKey !== (reader.loading ? 'historical-loading' : 'historical-error')) {
        reader.renderedKey = reader.loading ? 'historical-loading' : 'historical-error';
        $('reader-body').replaceChildren(element('p', reader.loading ? doc.historical ? t("正在读取并核对上一轮的文件版本…") : t("正在读取并核对交付文件版本…")
          : reader.error || t("这份交付暂时无法读取，请重新打开。"), 'detail-caption'));
      }
    }
  }
  if (!doc && reader.fileSha && !reader.boundObjectiveId) {
    const unchanged = docs.find((entry) => entry.path === reader.path && entry.sha256 === reader.fileSha && entry.content === reader.content);
    if (unchanged) {
      for (const source of [false, true]) {
        const prior = `${reader.key}:${source}`;
        const next = `${unchanged.key}:${source}`;
        if (reader.positions.has(prior)) reader.positions.set(next, reader.positions.get(prior));
      }
      reader.key = unchanged.key;
      reader.renderedKey = `${unchanged.key}:${reader.source}`;
      doc = unchanged;
      $('reader-meta').textContent = doc.meta;
    }
  }
  $('reader-navigation-summary').textContent = t("切换成果 · {p0} 份", { p0: docs.length });
  reader.page = Math.max(0, Math.min(reader.page || 0, Math.ceil(docs.length / 128) - 1));
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), [reader.page, docs.map((entry) => [entry.key, entry.title])]]);
  if ($('reader-files').dataset.fingerprint !== fingerprint) {
    $('reader-files').dataset.fingerprint = fingerprint;
    $('reader-files').replaceChildren(...docs.slice(reader.page * 128, (reader.page + 1) * 128).map((entry) => readerLink(entry, 'reader-file')));
    if (docs.length > 128) {
      const paging = element('div', '', 'reader-pagination');
      const previous = element('button', t("上一批"), 'quiet-button'); previous.type = 'button'; previous.disabled = reader.page === 0;
      const next = element('button', t("下一批"), 'quiet-button'); next.type = 'button'; next.disabled = (reader.page + 1) * 128 >= docs.length;
      previous.addEventListener('click', () => { if (state.reader) { state.reader.page -= 1; renderReader(); } });
      next.addEventListener('click', () => { if (state.reader) { state.reader.page += 1; renderReader(); } });
      paging.append(previous, element('span', t("第 {p0} / {p1} 批 · 共 {p2} 份", { p0: reader.page + 1, p1: Math.ceil(docs.length / 128), p2: docs.length }), 'detail-caption'), next);
      $('reader-files').append(paging);
    }
  }
  for (const button of $('reader-files').children) button.setAttribute('aria-pressed', String(button.dataset.documentKey === doc?.key));
  if (requiresBoundRead(doc) && !reader.loadedDocument) return;
  $('reader-source').hidden = !doc?.markdown;
  $('reader-source').textContent = reader.source ? t("阅读排版") : t("查看源文");
  $('reader-source').setAttribute('aria-pressed', String(reader.source));
  if (!doc) {
    if (reader.renderedKey !== 'stale') {
      $('reader-title').textContent = t("内容有了新版本"); $('reader-meta').textContent = t("为避免混淆，旧正文已收起。");
      $('reader-body').replaceChildren(); const latest = docs.find((entry) => entry.path === reader.path);
      $('reader-update').hidden = false; $('reader-update').replaceChildren(element('p', t("文件有了新版本。请选择上方内容，或打开这个文件的最新版本。")));
      if (latest) $('reader-update').append(readerLink(latest, 'quiet-button'));
      reader.renderedKey = 'stale';
    }
    return;
  }
  const renderedKey = `${doc.key}:${reader.source}`;
  $('reader-title').textContent = doc.title; $('reader-meta').textContent = doc.meta;
  if (reader.renderedKey === renderedKey) return;
  reader.renderedKey = renderedKey;
  $('reader-update').hidden = true;
  $('reader-body').replaceChildren(); $('reader-body').className = `reader-body ${doc.markdown && !reader.source ? 'markdown-document' : 'reader-plain'}`;
  if (doc.markdown && !reader.source) window.BeanestMarkdown.render(doc.content, $('reader-body'), { localReference: (reference) => mediaReferenceNode(reference, doc) });
  else {
    $('reader-body').append(element('pre', doc.content, 'file-content'));
  }
  $('reader-body').scrollTop = reader.positions.get(renderedKey) || 0;
}

$('reader-back').addEventListener('click', closeReader);
$('reader-body').addEventListener('keydown', (event) => {
  if (state.reader && event.key === 'Escape' && !event.defaultPrevented && !document.fullscreenElement) { event.preventDefault(); closeReader(); }
});
$('reader-source').addEventListener('click', () => {
  if (!state.reader) return;
  state.reader.positions.set(state.reader.renderedKey, $('reader-body').scrollTop);
  state.reader.source = !state.reader.source; renderReader();
});

// Mirrors agent_registry.default_bean_style: each built-in tool starts with its own look.
const defaultBeanStyles = { claude: 'peach', codex: 'sprout', kimi: 'sky' };
function defaultBeanStyle(adapter) { return defaultBeanStyles[adapter] || 'sprout'; }
function beanStyle(id, descriptor) {
  // Appearance is a live preference; the room still owns its frozen identity.
  const preferred = state.partners.find((partner) => partner.id === id);
  if (beanStyleIds.has(preferred?.bean_style)) return preferred.bean_style;
  const agent = descriptor || state.snapshot?.members.find((member) => member.id === id)
    || state.catalog.find((member) => member.id === id) || state.partners.find((member) => member.id === id);
  if (beanStyleIds.has(agent?.bean_style)) return agent.bean_style;
  return defaultBeanStyle(agent?.adapter || agent?.provider || id);
}

function avatar(id, descriptor) {
  const source = ['user', 'service', 'system', 'mcp'].includes(id);
  const tone = source ? (id === 'user' ? 'user' : 'service') : `agent-tone-${Math.max(0, providers.indexOf(id)) % 4}`;
  const node = element('span', '', `avatar ${tone}${source ? '' : ` bean-avatar bean-style-${beanStyle(id, descriptor)}`}`);
  node.setAttribute('aria-hidden', 'true');
  if (source) {
    node.textContent = initials[id] || '?';
  } else {
    node.dataset.agent = id;
    const body = element('span', '', 'bean-body');
    body.append(element('span', '', 'bean-eyes'), element('span', '', 'bean-smile'));
    const initial = typeof descriptor?.avatar === 'string' && [...descriptor.avatar].length <= 2 && descriptor.avatar.trim()
      ? descriptor.avatar : initials[id] || '?';
    node.append(body, element('span', '', 'bean-accessory'), element('span', initial, 'avatar-initial'));
  }
  return node;
}

function rememberAgents(agents) {
  for (const agent of agents) {
    if (!agent || !agentPattern.test(agent.id) || ['user', 'service', 'system', 'mcp', 'both', 'all'].includes(agent.id)) continue;
    if (!providers.includes(agent.id)) providers.push(agent.id);
    names[agent.id] = typeof agent.name === 'string' ? agent.name : agent.id;
    initials[agent.id] = typeof agent.avatar === 'string' && [...agent.avatar].length <= 2 && agent.avatar.trim() ? agent.avatar : initials[agent.id] || [...names[agent.id]][0]?.toUpperCase() || '?';
  }
}

function setCatalog(agents) {
  if (!Array.isArray(agents) || agents.some((agent) => !agent || !agentPattern.test(agent.id)
      || ['user', 'service', 'system', 'mcp', 'both', 'all'].includes(agent.id)
      || typeof agent.name !== 'string' || !agent.name.trim() || agent.name.length > 80)
      || new Set(agents.map((agent) => agent.id)).size !== agents.length) {
    throw new RequestError(t("伙伴清单暂不可用，请检查本地服务版本。"));
  }
  state.catalog = agents;
  rememberAgents(agents);
  const eligible = new Set(agents.filter((agent) => selectableAgent(agent, { retainBlocked: true })).map((agent) => agent.id));
  const available = new Set(agents.filter((agent) => selectableAgent(agent)).map((agent) => agent.id));
  // The first tool that becomes usable counts as the default choice, not as something left unticked.
  const firstUsable = state.noUsableTool && available.size > 0;
  state.noUsableTool = available.size === 0;
  const defaults = agents.filter((agent) => available.has(agent.id) && !isAdditionalSession(agent) && ['claude', 'codex', 'kimi'].includes(agent.adapter || agent.id)).map((agent) => agent.id);
  // Until someone picks by hand, a tool that appears after the page opened joins the default choice.
  const appeared = state.defaultsSeen ? defaults.filter((id) => !state.defaultsSeen.has(id)) : [];
  state.defaultsSeen = new Set([...(state.defaultsSeen || []), ...defaults]);
  if (state.selectedParticipants === null || firstUsable && !state.selectedParticipants.length) {
    state.selectedParticipants = defaults.length ? defaults : agents.filter((agent) => available.has(agent.id) && !isAdditionalSession(agent)).slice(0, 2).map((agent) => agent.id);
  } else {
    state.selectedParticipants = state.selectedParticipants.filter((id) => eligible.has(id));
    if (!state.participantsChosen && !state.selected) state.selectedParticipants = [...new Set([...state.selectedParticipants, ...appeared])];
  }
  renderParticipantPicker();
}

// Every effort level a tool may report; an unknown one is shown as the tool wrote it.
function effortLabels() {
  return { none: t("不思考"), minimal: t("极低"), low: t("低"), medium: t("中"), high: t("高"), xhigh: t("很高"), ultra: t("极高"), max: t("最高"), auto: t("自动") };
}

function isAdditionalSession(agent) {
  return ['claude', 'codex'].includes(agent.adapter) && agent.id !== agent.adapter;
}

function renderParticipantPicker() {
  const picker = $('participant-picker');
  const extraPicker = $('extra-session-picker');
  const existing = new Map([...picker.children, ...extraPicker.children].map((node) => [node.dataset.agent, node]));
  const candidates = state.catalog.filter(activeEntry).filter((agent) => !state.partners.some((partner) => partner.id === agent.id && !activeEntry(partner)));
  const nodes = candidates.map((agent) => {
    let label = existing.get(agent.id);
    if (!label) {
      label = element('label', '', 'participant-option');
      label.dataset.agent = agent.id;
      const input = element('input');
      input.type = 'checkbox'; input.dataset.participant = agent.id;
      input.addEventListener('change', () => {
        if (state.selected || state.busy || state.managementBusy || state.pending || !selectableAgent(agent, { retainBlocked: !input.checked })) {
          input.checked = (state.selectedParticipants || []).includes(agent.id); return;
        }
        state.participantsChosen = true;
        const selected = new Set(state.selectedParticipants || []);
        if (input.checked) selected.add(agent.id); else selected.delete(agent.id);
        if (selected.size > 8) { input.checked = false; showNotice(t("一次最多选择 8 个会话。"), 'error'); return; }
        state.selectedParticipants = state.catalog.filter((entry) => selected.has(entry.id)).map((entry) => entry.id);
        saveDraft(); renderMembers(null); renderControls();
      });
      label.append(input, avatar(agent.id, agent), element('span', agent.name));
      if (isAdditionalSession(agent)) label.append(element('small', t("{p0} 的独立会话", { p0: agent.adapter === 'codex' ? 'Codex' : 'Claude Code' }), 'session-caption'));
    }
    label.firstChild.checked = (state.selectedParticipants || []).includes(agent.id);
    // Set on every render so a language switch also renames the screen-reader label.
    label.firstChild.setAttribute('aria-label', t("邀请 {p0}", { p0: agent.name }));
    label.firstChild.disabled = state.busy || state.managementBusy || !!state.pending || !selectableAgent(agent, { retainBlocked: label.firstChild.checked });
    const availability = availabilityInfo(agent.id);
    let availabilityNote = [...label.children].find((node) => node.classList.contains('participant-availability'));
    if (!availabilityNote) { availabilityNote = element('small', '', 'participant-availability'); label.append(availabilityNote); }
    // A tool that is not installed or signed in says so here, the same words as its partner card.
    const setupNote = notSetUp(agent) ? memberLoginCaption(null, agent.id) : '';
    const note = setupNote || (availability.value ? availability.summary : '');
    availabilityNote.hidden = !note; availabilityNote.textContent = note;
    label.classList.toggle('quota-blocked', availability.blocked || !!setupNote);
    const portrait = label.children[1];
    const styleClass = `bean-style-${beanStyle(agent.id, agent)}`;
    if (!portrait.classList.contains(styleClass)) portrait.replaceWith(avatar(agent.id, agent));
    label.classList.toggle('chosen', label.firstChild.checked);
    return label;
  });
  const extras = new Set(candidates.filter(isAdditionalSession).map((agent) => agent.id));
  for (const [container, children] of [[picker, nodes.filter((node) => !extras.has(node.dataset.agent))],
    [extraPicker, nodes.filter((node) => extras.has(node.dataset.agent))]]) {
    if (children.length !== container.children.length || children.some((node, index) => container.children[index] !== node)) container.replaceChildren(...children);
  }
  $('extra-session-choice').hidden = !extras.size;
  const selectedExtras = (state.selectedParticipants || []).filter((id) => extras.has(id)).length;
  $('extra-session-choice-label').textContent = t("更多会话（高级）{p0}", { p0: selectedExtras ? t(" · 已选 {p0}", { p0: selectedExtras }) : '' });
  $('participant-count').textContent = t("{p0} 已选", { p0: (state.selectedParticipants || []).length });
  // A disabled partner is left out of the picker and the home page; name it and say where to turn it back on.
  const note = disabledPartnersNote();
  for (const id of ['participant-disabled-note', 'home-disabled-note']) {
    if ($(id).textContent !== note) $(id).textContent = note;
    $(id).hidden = !note;
  }
}
function disabledPartnersNote() {
  const disabled = state.partners.filter((partner) => partner.enabled === false && activeEntry(partner)).map((partner) => partner.name || displayName(partner.id));
  return disabled.length ? t("已停用：{p0}。要用的话，到“伙伴与额度”里点“启用”。", { p0: interfaceList(disabled) }) : '';
}

function memberCard(id, descriptor = {}) {
  const card = element('article', '', 'member-card');
  card.id = `member-${id}`;
  const heading = element('div', '', 'member-heading');
  const identity = element('div');
  const name = element('h3', names[id]); name.id = `name-${id}`;
  const status = element('span', t("未启动"), 'member-status'); status.id = `status-${id}`;
  const email = element('p', '', 'account-email'); email.id = `account-email-${id}`;
  identity.append(name, email);
  const adapter = descriptor.adapter || descriptor.provider;
  if (adapter) identity.append(element('p', ({ claude: 'Claude Code', codex: 'Codex' })[adapter] || adapter, 'adapter-identity'));
  const model = element('button', '', 'member-model'); model.type = 'button'; model.id = `member-model-${id}`;
  model.setAttribute('aria-haspopup', 'dialog'); model.setAttribute('aria-controls', 'runtime-settings-popover');
  model.addEventListener('click', () => void openRuntimePopover({ partnerId: id, trigger: model }));
  heading.append(avatar(id, descriptor), identity, model);
  if (state.partners.some((partner) => partner.id === id)) heading.append(appearanceButton(id));
  const doing = element('p', '', 'member-doing'); doing.append(status);
  const task = element('p', '', 'current-task'); task.id = `task-${id}`;
  const publicProgress = element('div', '', 'member-public-progress'); publicProgress.id = `public-progress-${id}`;
  const availability = element('p', '', 'member-availability'); availability.id = `availability-${id}`;
  const usageSummary = element('p', '', 'usage-summary'); usageSummary.id = `usage-summary-${id}`;
  const details = element('details', '', 'member-details');
  const session = element('p', '', 'session-id'); session.id = `session-${id}`;
  const fields = element('dl', '', 'usage-fields'); fields.id = `usage-${id}`;
  const configurationHistory = element('p', '', 'detail-caption'); configurationHistory.id = `configuration-history-${id}`; configurationHistory.hidden = true;
  details.append(element('summary', t("任务会话与 Token")), session,
    element('p', t("本任务的供应商用量快照，与账号订阅额度分开。"), 'detail-caption'), fields, configurationHistory);
  const quota = element('div', '', 'member-quota'); quota.id = `member-quota-${id}`;
  const workload = element('p', '', 'member-workload'); workload.id = `member-workload-${id}`;
  card.append(heading, doing, element('p', t("当前任务"), 'member-caption'), task, publicProgress, availability, quota, workload, usageSummary, details, accountFooter(id));
  return card;
}

function partnerAvailability(id) {
  const value = state.partners.find((partner) => partner.id === id)?.availability
    || state.catalog.find((agent) => agent.id === id)?.availability;
  return value?.scope === 'default_cli_login' && ['available', 'unknown', 'blocked'].includes(value.state)
    && typeof value.selectable === 'boolean' ? value : null;
}
function observedQuotaLabel(id) {
  const quota = currentAccount(id).account?.quota;
  if (quota?.state !== 'available' || !Array.isArray(quota.windows) || !quota.windows.length) return null;
  const windows = quota.windows.filter((window) => window && typeof window === 'object');
  if (!windows.length) return null;
  const window = windows.length === 1 ? windows[0] : null;
  const remaining = percentValue(window?.remaining_percent);
  return { quota, text: window && remaining !== null
    ? t("{p0}{p1}剩余 {p2}%", { p0: quota.stale ? t("上次：") : '', p1: quotaWindowName(window.window_minutes) || t("账号额度"), p2: remaining })
    : t("{p0}取得 {p1} 个额度窗口", { p0: quota.stale ? t("上次") : t("已"), p1: windows.length }) };
}
function availabilityInfo(id) {
  const value = partnerAvailability(id);
  const blocked = value?.state === 'blocked' && value.selectable === false;
  const stale = value?.stale === true || Number.isFinite(value?.observed_at) && Date.now() / 1000 - value.observed_at > 90;
  const reasons = { subscription_limit_reached: t("工具已报告订阅额度用完"), quota_exhausted: t("供应商共享额度已用完"),
    quota_reset_due: t("已到预计重置时间，仍需重新确认"), quota_unknown: t("当前接入还没查到额度；明确报告限额时会暂停使用"),
    account_check_failed: t("这次检查未成功，保留上次记录"), account_identity_unknown: t("当前登录待确认，之前的额度限制仍保留，请刷新账号"),
    adapter_unsupported: t("当前接入暂未支持额度查询") };
  const observed = !blocked && ['quota_unknown', 'adapter_unsupported'].includes(value?.reason) ? observedQuotaLabel(id) : null;
  const knownExhausted = ['subscription_limit_reached', 'quota_exhausted'].includes(value?.reason);
  const summary = blocked ? knownExhausted ? t("额度已用完 · 暂不能开始") : value?.reason === 'account_identity_unknown' ? t("当前登录待确认 · 暂不能开始") : t("可用状态待确认 · 暂不能开始")
    : observed ? observed.text : value?.state === 'available' ? t("额度够用") : quotaPendingLabel(id);
  const detail = observed ? t("{p0} · 额度更新：{p1}{p2}", { p0: observed.text, p1: Number.isFinite(observed.quota.checked_at) ? timeLabel(observed.quota.checked_at, true) : t("时间未提供"), p2: observed.quota.stale ? t(" · 旧快照") : '' }) : value ? [reasons[value.reason] || (blocked ? t("暂不能开始新任务") : summary),
    Number.isFinite(value.observed_at) ? t("上次检查 {p0}", { p0: timeLabel(value.observed_at, true) }) : t("检查时间未提供"),
    stale ? t("旧快照，等待更新") : '',
    Number.isFinite(value.identity_checked_at) && value.identity_checked_at <= Date.now() / 1000 && value.identity_checked_at !== value.observed_at ? t("登录核查 {p0}", { p0: timeLabel(value.identity_checked_at, true) }) : '', Number.isFinite(value.resets_at) ? t("预计重置 {p0}，届时仍需确认", { p0: timeLabel(value.resets_at, true) }) : '',
    state.availabilityError ? t("这次更新未成功，显示的仍是已有记录") : ''].filter(Boolean).join(' · ') : '';
  return { value, blocked, stale, summary, detail };
}
// The partner card carries one plain line; the full record lives under 账号与额度.
// A quota wait is an ordinary state, so only a login or status in doubt asks for attention.
function memberAvailabilityLine(info) {
  if (!info.value || !info.blocked) return null;
  const reason = info.value.reason;
  if (['subscription_limit_reached', 'quota_exhausted'].includes(reason)) return { tone: 'wait',
    text: Number.isFinite(info.value.resets_at) ? t("额度用完 · {p0} 恢复", { p0: timeLabel(info.value.resets_at, true) }) : t("额度用完 · 供应商没给恢复时间，每小时自动试一次") };
  if (reason === 'quota_reset_due') return { tone: 'wait', text: t("额度应已恢复 · 点“账号与额度”确认") };
  if (reason === 'account_identity_unknown') return { tone: 'ask', text: t("登录待确认 · 点“账号与额度”刷新") };
  return { tone: 'ask', text: t("状态待确认 · 点“账号与额度”刷新") };
}
function partnerSetupState(id) {
  const partner = state.partners.find((entry) => entry.id === id) || state.catalog.find((entry) => entry.id === id);
  return state.adapters.find((adapter) => adapter.id === (partner?.adapter || id))?.setup?.state || null;
}
// One sign-in wording for every place that shows it (partner card, management list, task options).
// A failed or old account or quota check never reads as signed out: only the tool's own
// setup check says "还差登录".
function memberLoginCaption(account, id) {
  const setupState = partnerSetupState(id);
  if (setupState === 'not_installed') return t("还没安装");
  if (setupState === 'login_required') return t("还差登录");
  if (!account || account.state === 'not_checked') return setupState === 'ready' ? t("已登录") : t("登录未检查");
  const known = !!accountEmail(account) || typeof account.plan === 'string' && !!account.plan || setupState === 'ready';
  if (account.stale || account.state === 'error') return known ? t("已登录 · 待刷新") : t("登录状态没查到");
  return t("已登录");
}
function availabilityText(info) {
  const parts = info.detail ? info.detail.split(' · ') : [];
  if (!parts.length) return info.summary;
  return [...new Set([info.summary, ...parts])].join(' · ');
}
function blockedParticipants() { return state.participantIds.filter((id) => availabilityInfo(id).blocked); }
function availabilityBlockMessage() {
  const blocked = blockedParticipants(); if (!blocked.length) return '';
  const reasons = blocked.map((id) => `${displayName(id)}${interfaceColon()}${availabilityInfo(id).summary.split(' · ')[0]}`).join(interfaceSemicolon());
  return state.selected ? state.memberInvitationSupported
    ? t("{p0}。等待状态更新，或在“伙伴”中查看可邀请的伙伴。", { p0: reasons })
    : t("{p0}。本任务成员固定；等待状态更新，或开新任务调整伙伴。", { p0: reasons })
    : t("{p0}。请取消勾选或等待状态更新，当前不会发送任务。", { p0: reasons });
}
function quotaFailoverSelection() {
  const policy = executionPolicyPayload(); const blocked = blockedParticipants();
  if (!policy?.quota_failover || !blocked.length || blocked.some((id) => !['subscription_limit_reached', 'quota_exhausted'].includes(availabilityInfo(id).value?.reason))) return null;
  const available = state.participantIds.filter((id) => !blocked.includes(id) && state.catalog.some((agent) => agent.id === id && eligibleAgent(agent)));
  return available.length ? { blocked, available } : null;
}
function submissionAvailabilityBlocked() { return blockedParticipants().length > 0 && !quotaFailoverSelection(); }
function submissionAvailabilityMessage() {
  const failover = quotaFailoverSelection();
  if (!failover) return availabilityBlockMessage();
  const ongoing = ['running', 'checkpoint'].includes(currentObjective()?.status) && !terminalTeamState(state.snapshot);
  return ongoing ? t("{p0} 正在接续任务；{p1} 额度不足。", { p0: interfaceList(failover.available.map(displayName)), p1: interfaceList(failover.blocked.map(displayName)) })
    : t("将由 {p0} 继续；{p1} 额度不足。", { p0: interfaceList(failover.available.map(displayName)), p1: interfaceList(failover.blocked.map(displayName)) });
}
function canRequestAvailabilityRecheck(id) {
  const info = availabilityInfo(id);
  const checkedAt = info.value?.identity_checked_at; const now = Date.now() / 1000;
  // The limit may be old while the login has just been confirmed. Its age
  // remains visible, but only the current identity gates a manual recheck.
  const identityFresh = Number.isFinite(checkedAt) && checkedAt <= now && now - checkedAt <= 90;
  return info.blocked && info.value?.can_request_recheck === true && identityFresh;
}
function availabilityRecheckControl(id) {
  const info = availabilityInfo(id);
  if (!info.blocked || info.value?.can_request_recheck !== true) return null;
  const button = element('button', t("我已恢复额度，重新允许使用"), 'quiet-button availability-recheck'); button.type = 'button';
  button.dataset.availabilityRecheck = id; button.dataset.focusKey = 'availability-recheck';
  button.disabled = !canRequestAvailabilityRecheck(id) || !!state.availabilityRecheck;
  button.title = !canRequestAvailabilityRecheck(id) ? t("当前登录尚未核查或登录核查记录已过期，请先刷新账号状态。") : t("只重新允许发起任务，不会验证额度或启动模型。");
  button.addEventListener('click', () => openAvailabilityRecheck(id, button));
  return button;
}
function renderAvailabilityRecheck() {
  const operation = state.availabilityRecheck; if (!operation) return;
  $('availability-recheck-title').textContent = t("重新允许 {p0}？", { p0: displayName(operation.id) });
  $('availability-recheck-description').textContent = t("与它共用本机 CLI 登录的会话也会重新允许使用。这里只把额度状态改为“尚未确认”，不表示供应商额度已经恢复。");
  $('availability-recheck-submit').disabled = operation.busy || !canRequestAvailabilityRecheck(operation.id);
  $('availability-recheck-cancel').disabled = operation.busy;
  $('availability-recheck-error').hidden = !operation.error && canRequestAvailabilityRecheck(operation.id);
  $('availability-recheck-error').textContent = operation.error || t("账号或额度记录已变化，请关闭后查看最新状态。");
}
function openAvailabilityRecheck(id, trigger) {
  if (!canRequestAvailabilityRecheck(id) || state.availabilityRecheck) return;
  const fallback = $(`composer-usage-${id}`);
  closeUsagePopover(); closeTaskOptions({ restoreFocus: false }); closeRuntimePopover({ restoreFocus: false }); closeContextMenu();
  state.availabilityRecheck = { id, trigger, fallback, busy: false, error: '' };
  $('availability-recheck-dialog').hidden = false;
  syncModalInert();
  renderAvailabilityRecheck(); $('availability-recheck-cancel').focus({ preventScroll: true });
}
function closeAvailabilityRecheck() {
  const operation = state.availabilityRecheck; if (!operation || operation.busy) return;
  state.availabilityRecheck = null; $('availability-recheck-dialog').hidden = true;
  syncModalInert();
  renderMembers(state.snapshot); renderGlobalPartners();
  const card = [...$('partners-list').children, ...$('additional-partners-list').children].find((node) => node.dataset.partner === operation.id);
  const target = nodeContains(document.body, operation.trigger) ? operation.trigger
    : state.manager && card ? card : operation.fallback && nodeContains(document.body, operation.fallback) ? operation.fallback : $('open-partners');
  target.focus({ preventScroll: true });
}
async function submitAvailabilityRecheck() {
  const operation = state.availabilityRecheck;
  if (!operation || operation.busy || !canRequestAvailabilityRecheck(operation.id)) return;
  operation.busy = true; operation.error = ''; renderAvailabilityRecheck();
  try {
    acceptPartners(await api(workspaceApi.partner(operation.id, 'availability/recheck'), 'POST', {}));
    operation.busy = false; closeAvailabilityRecheck();
    const note = t("已重新允许使用，额度仍未确认。下次真实任务仍可能遇到限额。没有启动模型。");
    if (state.manager) managerNotice(note); else showNotice(note);
  } catch (error) {
    operation.busy = false;
    operation.error = error.uncertain ? t("结果尚未确认，先查看最新记录；不会自动重复这项操作。") : error.message;
    await api(workspaceApi.partners).then(acceptPartners).catch(() => {});
    renderAvailabilityRecheck();
  }
}
$('availability-recheck-cancel').addEventListener('click', closeAvailabilityRecheck);
$('availability-recheck-submit').addEventListener('click', () => void submitAvailabilityRecheck());
$('availability-recheck-dialog').addEventListener('pointerdown', (event) => { if (event.target === $('availability-recheck-dialog')) closeAvailabilityRecheck(); });
document.addEventListener('keydown', (event) => {
  if (!state.availabilityRecheck) return;
  if (event.key === 'Escape') { event.preventDefault(); closeAvailabilityRecheck(); }
  else if (event.key === 'Tab') {
    event.preventDefault();
    const buttons = [$('availability-recheck-cancel'), $('availability-recheck-submit')].filter((button) => !button.disabled);
    if (buttons.length) buttons[(buttons.indexOf(document.activeElement) + 1) % buttons.length].focus({ preventScroll: true });
  }
});

function availabilityWatchSupported() {
  return state.partners.some((partner) => activeEntry(partner) && partner.enabled !== false && partnerAvailability(partner.id));
}
function syncAvailabilityWatchTimer() {
  if (state.availabilityTimer !== null) { clearTimeout(state.availabilityTimer); state.availabilityTimer = null; }
  if (document.hidden || !state.connected || state.availabilityRefreshing || !availabilityWatchSupported()) return;
  const next = Number.isFinite(state.availabilityLastStarted) ? state.availabilityLastStarted + 60000 : Date.now();
  state.availabilityTimer = setTimeout(() => { state.availabilityTimer = null; void refreshAvailability(); }, Math.max(0, next - Date.now()));
}
async function refreshAvailability({ immediate = false } = {}) {
  if (document.hidden || !state.connected || state.availabilityRefreshing || !availabilityWatchSupported()) return;
  if (!immediate && Number.isFinite(state.availabilityLastStarted) && Date.now() - state.availabilityLastStarted < 60000) { syncAvailabilityWatchTimer(); return; }
  state.availabilityRefreshing = true; state.availabilityLastStarted = Date.now(); state.availabilityError = '';
  const began = state.availabilityLastStarted; syncAvailabilityWatchTimer();
  try { acceptPartners(await api('/api/availability/refresh', 'POST', {})); }
  catch (error) {
    state.availabilityError = error.message;
    // A lost response may still have updated cached observations. Read once;
    // never replay a native refresh to resolve an uncertain transport result.
    await api(workspaceApi.partners).then(acceptPartners).catch(() => {});
  } finally {
    state.availabilityRefreshing = false;
    if (Date.now() - began >= 60000) state.availabilityLastStarted = Date.now();
    renderParticipantPicker(); renderMembers(state.snapshot); renderGlobalPartners(); renderControls();
    syncAvailabilityWatchTimer();
  }
}


// Roles: what each partner does in one room. The room stores the whole role,
// so editing or deleting a custom role later never changes a room already using it.
const builtinRoles = ['builder', 'reviewer', 'planner'];
const roleAbilityIds = ['plan', 'work', 'review', 'checkpoint'];
const workloadLevels = ['low', 'normal', 'high'];
const customRolesKey = 'beanest.customRoles';
function roleAbilityLabels() { return { plan: t("规划"), work: t("干活"), review: t("审查"), checkpoint: t("阶段检查") }; }
function builtinRoleInfo() {
  return { builder: { name: t("开发"), duty: t("领实现任务来做，不审查别人，也不写计划。") },
    reviewer: { name: t("审查"), duty: t("只审别人交付的改动，不领实现任务，也不参加规划。") },
    planner: { name: t("规划"), duty: t("写计划、做阶段检查，不领实现任务。") } };
}
function copyRole(role) {
  if (!role || typeof role !== 'object' || typeof role.id !== 'string') return null;
  if (builtinRoles.includes(role.id)) return { id: role.id };
  if (!/^custom:[a-z0-9][a-z0-9-]{0,39}$/.test(role.id) || typeof role.name !== 'string' || !role.name.trim()
      || typeof role.duty !== 'string' || !Array.isArray(role.abilities)) return null;
  const abilities = roleAbilityIds.filter((ability) => role.abilities.includes(ability));
  return abilities.length ? { id: role.id, name: role.name.trim().slice(0, 20), duty: role.duty.trim().slice(0, 300), abilities } : null;
}
function roleName(role) { return !role ? t("全能") : builtinRoleInfo()[role.id]?.name || role.name; }
function roleDuty(role) {
  if (!role) return t("规划、干活、审查、阶段检查都参与，和不设角色时一样。");
  const builtin = builtinRoleInfo()[role.id];
  if (builtin) return builtin.duty;
  const abilities = role.abilities.map((ability) => roleAbilityLabels()[ability]).join(t("、"));
  return role.duty ? t("{p0}（负责：{p1}）", { p0: role.duty, p1: abilities }) : t("负责：{p0}", { p0: abilities });
}
function savedCustomRoles() {
  try {
    const value = JSON.parse(localStorage.getItem(customRolesKey) || '[]');
    return Array.isArray(value) ? value.map(copyRole).filter((role) => role?.id.startsWith('custom:')).slice(0, 50) : [];
  } catch (_) { return []; }
}
function saveCustomRoles(roles) {
  try { localStorage.setItem(customRolesKey, JSON.stringify(roles.slice(0, 50))); } catch (_) { /* The room keeps its own copy of each role. */ }
}
function copyRuntimeSettings(settings) {
  const members = {};
  for (const [id, choice] of Object.entries(settings?.members || {})) {
    if (!agentPattern.test(id) || !choice || typeof choice !== 'object') continue;
    const role = copyRole(choice.role); const workload = workloadLevels.includes(choice.workload) ? choice.workload : null;
    members[id] = { model: typeof choice.model === 'string' ? choice.model : null, effort: typeof choice.effort === 'string' ? choice.effort : null,
      ...(role ? { role } : {}), ...(workload ? { workload } : {}) };
  }
  return { permission: ['plan', 'read_only', 'workspace_write', 'full_access'].includes(settings?.permission) ? settings.permission : 'workspace_write', members };
}
function validRuntimeSettings(value) {
  return value && typeof value === 'object' && !Array.isArray(value)
    && ['plan', 'read_only', 'workspace_write', 'full_access'].includes(value.permission)
    && value.members && typeof value.members === 'object' && !Array.isArray(value.members)
    && Object.entries(value.members).every(([id, choice]) => agentPattern.test(id) && choice && typeof choice === 'object'
      && !Array.isArray(choice) && (choice.model == null || typeof choice.model === 'string')
      && (choice.effort == null || typeof choice.effort === 'string')
      && (choice.role == null || typeof choice.role === 'object' && !Array.isArray(choice.role))
      && (choice.workload == null || typeof choice.workload === 'string'));
}
function naturalRuntimeConfiguration(snapshot = state.snapshot) {
  if (!state.naturalRuntimeSupported || !snapshot?.room) return null;
  let value = snapshot.runtime_configuration;
  // A reply-only room started without choosing models records no settings;
  // its calls already use each tool's default, so that is what the user edits.
  if (value?.desired && value.desired.runtime_settings == null && snapshot.room.mode && snapshot.room.mode !== 'native_project') {
    const members = Object.fromEntries((snapshot.members || []).filter((member) => agentPattern.test(member?.id || ''))
      .map((member) => [member.id, { model: null, effort: null }]));
    value = { ...value, desired: { ...value.desired, runtime_settings: { permission: 'workspace_write', members } } };
  }
  if (!value || typeof value.can_edit !== 'boolean' || !['plan', 'build'].includes(value.desired?.work_mode)
      || !validRuntimeSettings(value.desired?.runtime_settings) || typeof value.pending !== 'boolean') return null;
  return value;
}
function runtimeFingerprint(value) {
  const settings = copyRuntimeSettings(value);
  return JSON.stringify([settings.permission, [...state.participantIds].sort().map((id) =>
    [id, settings.members[id]?.model || null, settings.members[id]?.effort || null, settings.members[id]?.role || null, settings.members[id]?.workload || null])]);
}
function pendingRuntimeChanges(snapshot = state.snapshot) {
  const configuration = naturalRuntimeConfiguration(snapshot);
  if (!configuration) return {};
  const changes = {};
  if (state.workMode !== configuration.desired.work_mode) changes.work_mode = state.workMode;
  if (runtimeFingerprint(state.runtimeSettings) !== runtimeFingerprint(configuration.desired.runtime_settings)) changes.runtime_settings = runtimePayload();
  return changes;
}
function messageHasRuntimeSettings(body) {
  return !!body && (Object.hasOwn(body, 'runtime_settings') || Object.hasOwn(body, 'work_mode'));
}
function requestOwnsRuntimeDraft(request, draft, field) {
  const mode = field === 'workMode';
  const submitted = mode ? request.body?.work_mode : request.body?.runtime_settings;
  if (submitted === undefined || !draft) return false;
  const token = mode ? 'workModeDraftToken' : 'runtimeDraftToken';
  const value = mode ? 'workMode' : 'runtimeSettings';
  // An equal value after a new edit is still a newer intent. Tokens are local
  // outbox metadata; the immutable HTTP body and request identity stay intact.
  if (!request[token] || request[token] !== draft[token]) return false;
  return mode ? draft[value] === submitted
    : JSON.stringify(copyRuntimeSettings(draft[value])) === JSON.stringify(copyRuntimeSettings(submitted));
}
function clearAcceptedRuntimeDraft(request) {
  const draft = state.drafts.get(request.roomId);
  for (const field of ['runtime', 'workMode']) {
    const dirty = `${field}DraftDirty`;
    if (requestOwnsRuntimeDraft(request, draft, field)) draft[dirty] = false;
    if (state.selected === request.roomId && requestOwnsRuntimeDraft(request, state, field)) state[dirty] = false;
  }
}
function nativePermissionAvailable() {
  const room = state.snapshot?.room;
  return state.nativeProjectSupported && (room ? room.mode === 'native_project' || state.snapshot?.current_run?.can_native_handoff === true
    : state.mode === 'native_project' && state.projects.some((project) => project.id === state.selectedProject && project.selection_kind === 'folder' && activeEntry(project)));
}
function runtimeSubmissionError() {
  if (state.mode === 'native_project' && !state.selected && !state.nativeProjectSupported) return t("当前服务尚不支持完整开发，已保留你的设置，请更新服务后再发送。");
  if (state.runtimeSettings?.permission === 'full_access' && !nativePermissionAvailable()) return t("Full Access 只用于已接入的完整开发任务，请先确认项目与开发方式。");
  return '';
}
function runtimePermissionName(permission) {
  const name = ({ plan: 'Plan', read_only: t("只读"), workspace_write: t("副本内修改"), full_access: 'Full Access' })[permission] || t("未记录");
  // Only native rooms moved to Plan and Full Access; these names are current elsewhere.
  const room = state.snapshot?.room; const native = room ? room.mode === 'native_project' : state.mode === 'native_project';
  return native && nativePermissionAvailable() && ['read_only', 'workspace_write'].includes(permission) ? t("历史设置：{p0}", { p0: name }) : name;
}
function nativePlanPermissionsAvailable() {
  return nativePermissionAvailable() && state.runtimeOptions?.permissions?.some((permission) => permission.id === 'plan' && permission.legacy !== true);
}
function applyNativePermissionDefault() {
  if (state.selected || state.runtimePermissionExplicit || !nativePlanPermissionsAvailable() || state.runtimeSettings?.permission === 'full_access') return;
  state.runtimeSettings = { ...copyRuntimeSettings(state.runtimeSettings), permission: 'plan' };
}
function hasRoleChoices(settings) {
  return Object.values(copyRuntimeSettings(settings).members).some((choice) => choice.role || choice.workload);
}
function setMemberChoice(id, patch) {
  const next = copyRuntimeSettings(state.runtimeSettings);
  const merged = { model: null, effort: null, ...next.members[id], ...patch };
  if (!merged.role) delete merged.role;
  if (!merged.workload) delete merged.workload;
  next.members[id] = merged; state.runtimeSettings = next; state.runtimeDraftDirty = true; state.runtimeDraftToken = crypto.randomUUID();
  saveDraft(); renderRuntimeSettings(); renderControls();
}
function runtimePayload() {
  // Roles and workload apply in every room, so choosing them sends settings even before model options are read.
  if (state.runtimeOptions?.scope !== 'coordinator_owned_only' && !state.snapshot?.current_run?.runtime_settings && !state.snapshot?.room?.runtime_settings && !naturalRuntimeConfiguration()
      && !hasRoleChoices(state.runtimeSettings)) return null;
  const settings = copyRuntimeSettings(state.runtimeSettings);
  // Preserve the user's requested permission across plan → build. The host
  // owns effective_permission and always enforces read-only during planning.
  settings.members = Object.fromEntries(state.participantIds.map((id) => [id, settings.members[id] || { model: null, effort: null }]));
  return settings;
}
function acceptRuntimeOptions(value) {
  if (value?.scope !== 'coordinator_owned_only' || !value.partners || !Array.isArray(value.permissions)) return false;
  state.runtimeOptions = value; applyNativePermissionDefault();
  rememberModelNames(value.partners);
  return true;
}
// Model names come with the model options, which a reload does not fetch. The last names
// seen are kept on this device so a saved choice still reads "Opus 5.5", not "default".
const modelNamesKey = 'beanest:model-names-v1';
function savedModelNames() {
  try { const value = JSON.parse(localStorage.getItem(modelNamesKey) || '{}'); return value && typeof value === 'object' && !Array.isArray(value) ? value : {}; } catch (_) { return {}; }
}
function rememberModelNames(partners) {
  const names = savedModelNames();
  for (const [id, entry] of Object.entries(partners || {})) {
    const models = Array.isArray(entry?.models) ? entry.models.filter((model) => model && typeof model.id === 'string' && model.id.length <= 200) : [];
    if (!models.length) continue;
    names[id] = Object.fromEntries(models.slice(0, 100).map((model) => [model.id, { name: typeof model.name === 'string' ? model.name.slice(0, 120) : '',
      resolved_model: typeof model.resolved_model === 'string' ? model.resolved_model.slice(0, 200) : null }]));
  }
  try { localStorage.setItem(modelNamesKey, JSON.stringify(names)); } catch (_) { /* Names are a display convenience only. */ }
}
// The model list as last seen, for labels before (or without) loading the live options.
function knownModels(id) {
  const live = state.runtimeOptions?.partners?.[id]?.models;
  if (Array.isArray(live) && live.length) return live;
  const saved = savedModelNames()[id];
  return saved && typeof saved === 'object' ? Object.entries(saved).map(([model, value]) => ({ id: model, name: value?.name || '', resolved_model: value?.resolved_model || null })) : [];
}
// Without any list, still avoid raw ids: "default" reads as the tool's default, a path keeps its last part.
function fallbackModelLabel(id) {
  if (id === 'default') return t("默认推荐");
  return id.split('/').filter(Boolean).at(-1) || id;
}
async function loadRuntimeOptions() {
  if (state.runtimeLoading) return;
  state.runtimeLoading = true; state.runtimeMessage = ''; renderRuntimeSettings();
  try { if (!acceptRuntimeOptions(await api('/api/runtime-options'))) state.runtimeMessage = t("当前服务尚未提供运行设置。"); }
  catch (_error) { state.runtimeMessage = t("暂时无法读取设置。已有任务保持原来的设置。"); }
  finally { state.runtimeLoading = false; renderRuntimeSettings(); }
}
async function refreshRuntimeOptions(id) {
  if (state.runtimeRefreshId || runtimeContext().locked || runtimePartnerReadOnly(id)) return;
  state.runtimeRefreshId = id; state.runtimeMessage = ''; renderRuntimeSettings();
  try {
    if (!acceptRuntimeOptions(await api(workspaceApi.partner(id, 'runtime/refresh'), 'POST', {}))) state.runtimeMessage = t("工具没有返回可确认的模型选项。");
  } catch (error) { state.runtimeMessage = error.message || t("这次未能读取模型选项，可以稍后再试。"); }
  finally { state.runtimeRefreshId = null; renderRuntimeSettings(); }
}
function syncModalInert() {
  document.querySelector('.app-shell').inert = !!(state.manager || state.taskOptions || state.runtimePopoverOpen || state.availabilityRecheck || state.lifecycleConfirm || state.appearance || state.taskWithdraw);
  $('workspace-manager').inert = !!(state.lifecycleConfirm || state.availabilityRecheck || state.appearance);
}
function dialogControls(container) {
  const controls = [];
  const visit = (parent) => {
    for (const child of parent.children) {
      if (child.hidden || typeof child.getClientRects === 'function' && !child.getClientRects().length) continue;
      if (['BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'SUMMARY'].includes(child.tagName) && !child.disabled) controls.push(child);
      if (child.tagName !== 'DETAILS' || child.open) visit(child);
      else if (child.firstChild?.tagName === 'SUMMARY') controls.push(child.firstChild);
    }
  };
  visit(container); return controls;
}
function defaultExecutionPolicy() { return { mode: 'autonomous', max_seconds: null, max_calls: null, quota_failover: true }; }
function copyExecutionPolicy(policy) {
  if (!policy || typeof policy !== 'object' || Array.isArray(policy) || !['manual', 'autonomous'].includes(policy.mode)
      || Object.keys(policy).some((key) => !['mode', 'max_seconds', 'max_calls', 'quota_failover'].includes(key))) return null;
  const defaults = defaultExecutionPolicy(policy.mode);
  const seconds = policy.max_seconds === undefined ? defaults.max_seconds : policy.max_seconds;
  const calls = policy.max_calls === undefined ? defaults.max_calls : policy.max_calls; const failover = policy.quota_failover === undefined ? true : policy.quota_failover;
  const validCalls = calls === null && policy.mode === 'autonomous' || Number.isSafeInteger(calls) && calls >= 1 && calls <= 256;
  const validSeconds = seconds === null && policy.mode === 'autonomous' || [7200, 14400, 28800].includes(seconds);
  return validSeconds && validCalls && typeof failover === 'boolean'
    ? { mode: policy.mode, max_seconds: seconds, max_calls: calls, quota_failover: failover } : null;
}
// Historical policies remain readable, but new work never inherits artificial caps.
function executionPolicyPayload() {
  return state.executionPolicySupported && state.continuousObjectiveSupported ? defaultExecutionPolicy() : null;
}
function policySubmissionError() {
  return state.executionPolicySupported && !state.continuousObjectiveSupported
    ? t("请更新本地服务后继续；这个旧版本尚不支持持续工作。") : '';
}
function contextWorkMode() { return state.snapshot?.current_run?.work_mode || state.snapshot?.room?.work_mode; }
function closeTaskOptions({ restoreFocus = true } = {}) {
  if (!state.taskOptions) return;
  const { trigger } = state.taskOptions; state.taskOptions = null;
  $('task-options-popover').hidden = true; trigger.setAttribute('aria-expanded', 'false');
  syncModalInert();
  if (restoreFocus && trigger.isConnected !== false && !trigger.hidden) trigger.focus({ preventScroll: true });
}
function openTaskOptions(section, trigger) {
  if (state.taskOptions) closeTaskOptions({ restoreFocus: false });
  closeRuntimePopover({ restoreFocus: false }); closeUsagePopover(); closeContextMenu();
  state.taskOptions = { section, trigger };
  $('task-options-popover').hidden = false; trigger.setAttribute('aria-expanded', 'true');
  syncModalInert();
  if (section === 'project') $('project-choice').open = true;
  if (section === 'more') $('more-settings').open = true;
  const target = section === 'partners' ? state.selected ? $('room-member-invitation').hidden ? $('frozen-participants') : $('room-member-invitation') : $('participant-picker') : section === 'project' ? state.selected ? $('frozen-project') : $('project-picker') : $('more-settings');
  const control = dialogControls(target).find((entry) => !entry.disabled) || (target.tagName === 'SELECT' && !target.disabled ? target : null);
  (control || (section === 'project' && state.selected ? target : $('task-options-close'))).focus({ preventScroll: true }); target.scrollIntoView?.({ block: 'nearest' });
}
for (const section of ['partners', 'project', 'more']) {
  const trigger = $(`task-options-${section}`); trigger.addEventListener('click', () => openTaskOptions(section, trigger));
}
$('task-options-close').addEventListener('click', () => closeTaskOptions());
$('task-options-popover').addEventListener('pointerdown', (event) => { if (event.target === $('task-options-popover')) closeTaskOptions(); });
document.addEventListener('keydown', (event) => {
  if (!state.taskOptions) return;
  if (event.key === 'Escape') { event.preventDefault(); closeTaskOptions(); }
  else if (event.key === 'Tab') {
    const controls = dialogControls($('task-options-popover')); if (!controls.length) return;
    event.preventDefault(); const index = controls.indexOf(document.activeElement);
    controls[(index + (event.shiftKey ? -1 : 1) + controls.length) % controls.length].focus({ preventScroll: true });
  }
});
function runtimeContext() {
  const room = state.snapshot?.room; const run = state.snapshot?.current_run;
  const objective = currentObjective(); const newGoal = run?.can_start_suggested_goal === true;
  const objectiveActive = objective && !['complete', 'stopped', 'interrupted'].includes(objective.status);
  const nativeHandoff = state.nativeProjectSupported && run?.can_native_handoff === true;
  const nativeRecovery = state.nativeProjectSupported && room?.mode === 'native_project'
    && ['stopped', 'interrupted'].includes(objective?.status) && !newGoal
    && (run?.can_recover_native === true || !!run?.native_recovery_kind || state.pending?.kind === 'native_recovery');
  const configuration = naturalRuntimeConfiguration();
  const editable = state.selected && state.naturalRuntimeSupported ? configuration?.can_edit === true : !nativeRecovery && (!state.selected || !!runBinding(run) && (nativeHandoff || newGoal || !objectiveActive && (run.can_continue === true || run.can_retry === true)));
  const locked = !editable || state.busy || state.managementBusy || !!state.pending || !state.connected || room && !activeEntry(room);
  const recorded = configuration ? room?.runtime_settings : run?.runtime_settings || room?.runtime_settings;
  const choices = copyRuntimeSettings((configuration || editable) ? state.runtimeSettings : recorded);
  const workMode = (configuration || editable) ? state.workMode : run?.work_mode || room?.work_mode;
  const unsent = configuration && Object.keys(pendingRuntimeChanges()).length > 0;
  const supported = state.runtimeOptions?.scope === 'coordinator_owned_only';
  // Reply-only rooms never choose settings: their calls use each tool's own.
  const unrecorded = !editable && !recorded && (!room?.mode || room.mode === 'native_project');
  return { room, run, editable, locked, recorded, unrecorded, choices, workMode, supported, newGoal, nativeHandoff, nativeRecovery, configuration, unsent };
}
function runtimePartnerReadOnly(id) {
  return state.partnerLifecycleSupported && state.snapshot?.members?.some((member) => member.id === id && member.configuration_dispatchable === false) === true;
}
function runtimeModelLabel(model, models = []) {
  if (model.id === 'default') {
    const version = models.find((entry) => entry.id !== 'default' && entry.resolved_model && entry.resolved_model === model.resolved_model);
    return version ? t("默认推荐（{p0}）", { p0: version.name || version.id }) : t("默认推荐");
  }
  return typeof model.name === 'string' && model.name ? model.name : model.id;
}
function runtimeShortcutModelLabel(model, models) {
  if (model?.id !== 'default') return runtimeModelLabel(model, models);
  const resolved = models.find((entry) => entry.id !== 'default' && entry.resolved_model && entry.resolved_model === model.resolved_model);
  return resolved ? t("{p0}（默认）", { p0: resolved.name || resolved.id }) : t("默认推荐");
}
function renderRuntimeShortcuts(context = runtimeContext()) {
  const { room, editable, recorded, unrecorded, choices, workMode, newGoal, nativeRecovery, configuration, unsent } = context;
  const caption = configuration ? unsent ? t("发消息后生效 · ") : configuration.pending ? t("后续工作生效 · ") : t("设置 · ") : room ? editable ? newGoal ? t("新目标 · ") : t("下一轮 · ") : t("本轮 · ") : '';
  $('runtime-settings-summary').textContent = configuration ? t("{p0}权限：{p1}", { p0: caption, p1: workMode === 'plan' ? t("方案只读") : runtimePermissionName(choices.permission) }) : nativeRecovery ? t("继续沿用本任务设置") : unrecorded ? t("权限：未记录")
    : t("{p0}权限：{p1}", { p0: editable && room ? newGoal ? t("新目标 · ") : t("下一轮 · ") : '', p1: workMode === 'plan' ? t("方案只读") : runtimePermissionName(choices.permission) });
  for (const id of state.participantIds) {
    const group = state.partnerControls.get(id); if (!group) continue;
    const button = group.firstChild;
    const historical = runtimePartnerReadOnly(id);
    const partnerCaption = historical ? t("原有设置 · 仅查看 · ") : caption;
    const choice = historical ? recorded?.members?.[id] : choices.members[id];
    const models = knownModels(id);
    const model = models.find((entry) => entry.id === choice?.model);
    const fullModelLabel = unrecorded ? t("未记录模型") : choice?.model ? model ? runtimeModelLabel(model, models) : fallbackModelLabel(choice.model) : t("跟随工具");
    const modelLabel = model ? runtimeShortcutModelLabel(model, models) : fullModelLabel;
    const effort = unrecorded ? t("未记录强度") : effortLabels()[choice?.effort] || choice?.effort || t("默认强度");
    const shortEffort = effort === t("默认强度") ? t("默认") : effort;
    const roleSuffix = copyRole(choice?.role) ? ` · ${roleName(copyRole(choice.role))}` : '';
    const text = `${displayName(id)} · ${modelLabel} · ${shortEffort}${roleSuffix}${historical ? t(" · 仅查看") : ''} ⌄`;
    if (button.dataset.label !== text) {
      button.dataset.label = text;
      const configuration = element('span', '', 'shortcut-configuration');
      configuration.append(element('span', modelLabel, 'shortcut-model'), element('span', '·', 'shortcut-separator'), element('span', `${shortEffort}${roleSuffix}${historical ? t(" · 仅查看") : ''}`, 'shortcut-effort'));
      button.replaceChildren(element('span', displayName(id), 'shortcut-name'), configuration, element('span', '⌄', 'shortcut-arrow'));
    }
    const workloadName = { low: t("少"), normal: t("中"), high: t("多") }[choice?.workload];
    button.title = `${partnerCaption}${displayName(id)} · ${fullModelLabel} · ${effort}${roleSuffix}${workloadName ? t(" · 工作量{p0}", { p0: workloadName }) : ''}${historical ? t(" · 仅查看") : ''}${model?.resolved_model ? t(" · 工具标识：{p0}", { p0: model.resolved_model }) : ''}`;
    button.setAttribute('aria-label', t("{p0}{p1}的模型与思考强度：{p2}，{p3}{p4}", { p0: partnerCaption, p1: displayName(id), p2: fullModelLabel, p3: effort, p4: historical ? t("，原有设置仅供查看") : !editable ? configuration ? t("，当前不可修改") : t("，本轮已固定") : '' }));
    button.setAttribute('aria-expanded', String(state.runtimePopoverOpen && state.runtimePartner === id));
    const cardModel = document.getElementById(`member-model-${id}`);
    if (cardModel) {
      // A default effort adds nothing to the narrow card button, so it shows only the model there.
      cardModel.textContent = (shortEffort === t("默认") ? `${modelLabel}` : `${modelLabel} · ${shortEffort}`) + `${roleSuffix} ⌄`; cardModel.title = button.title;
      cardModel.setAttribute('aria-label', button.getAttribute('aria-label'));
      cardModel.setAttribute('aria-expanded', button.getAttribute('aria-expanded'));
    }
  }
  $('composer-usage-bar').dataset.nextRun = String(!!room && editable);
  $('composer-usage-bar').setAttribute('aria-label', t("{p0}伙伴模型与额度", { p0: caption }));
  $('runtime-shortcuts-caption').textContent = configuration ? unsent ? t("发消息后生效") : configuration.pending ? t("后续工作生效") : t("设置") : room ? editable ? newGoal ? t("新目标") : t("下一轮") : t("本轮") : '';
  $('runtime-shortcuts-caption').hidden = !room;
}
function renderRuntimeSettings() {
  const { room, editable, locked, recorded, unrecorded, choices, workMode, supported, newGoal, nativeHandoff, configuration, unsent } = runtimeContext();
  $('runtime-settings').hidden = !!state.reader || materialsOpen();
  renderRuntimeShortcuts();
  const permission = choices.permission;
  $('runtime-settings-title').textContent = state.runtimePartner ? t("{p0} · 模型、角色与工作量", { p0: displayName(state.runtimePartner) })
    : t("{p0}任务权限", { p0: editable && room ? newGoal ? t("新目标 · ") : t("下一轮 · ") : '' });
  $('runtime-settings-note').textContent = !editable ? workMode === 'plan' ? t("本轮设置已经固定，方案阶段强制只读。预设的动手权限不会在本轮生效。") : t("本轮设置已经固定，不会改变正在处理的任务。")
    : workMode === 'plan' ? t("方案阶段强制只读。下面预设开始动手后的权限；选择只读后，下一轮也不会自动提升权限。")
      : t("按所选权限处理任务；本机工具使用对应的原生权限模式。");
  if (nativePermissionAvailable()) $('runtime-settings-note').textContent = nativeHandoff ? t("这些设置用于接下来的本机开发，旧记录保持。Full Access 可按任务要求读写本机文件、执行命令和联网。")
    : t("Plan 使用工具的原生规划模式；Full Access 使用工具的最高权限，可按任务要求读写本机文件、执行命令和联网。");
  if (nativePermissionAvailable() && permission === 'read_only') $('runtime-settings-note').textContent = t("历史只读设置：保留原有只读语义；可用操作由对应工具决定。{p0}", { p0: $('runtime-settings-note').textContent });
  if (nativePermissionAvailable() && workMode === 'plan') $('runtime-settings-note').textContent += t(" 方案阶段仍强制只读，所选动手权限不会在本轮生效。");
  if (state.runtimePartner) $('runtime-settings-note').textContent = !editable ? t("本轮模型和思考强度已固定，不会改变正在处理的任务。")
    : room ? newGoal ? t("这里的选择只用于你明确开始的新目标。") : t("这里的选择只影响下一轮。") : t("为这个伙伴选择模型、思考强度、角色和工作量。");
  if (configuration) {
    $('runtime-settings-title').textContent = state.runtimePartner ? t("{p0} · 模型、角色与工作量", { p0: displayName(state.runtimePartner) }) : t("后续工作权限");
    $('runtime-settings-note').textContent = !configuration.can_edit ? t("当前不能修改设置。已有设置保持。")
      : unsent ? t("还没生效：发一条消息才会提交，只发“继续”也可以。正在运行的调用不受影响，这一轮结束后切换。")
        : configuration.pending ? t("已保存，后续工作开始生效。正在运行的调用不变。")
          : t("修改后随下一条消息提交，正在运行的调用不变。");
    if (configuration.can_edit && state.runtimePartner) $('runtime-settings-note').textContent = configuration.pending && !unsent
      ? t("设置已提交，将用于后续调用；当前调用不变。")
      : t("发送消息或点击“继续”后，新模型和思考强度用于后续调用；当前调用不变。");
    if (!configuration.can_edit && configuration.reason) $('runtime-settings-note').textContent += ` ${errorLabels[configuration.reason] || executionMessage(configuration.reason)}`;
    if (unsent || configuration.pending) {
      const activeJob = pagedRoom(state.snapshot) ? state.snapshot.live?.jobs?.find((job) => job.provider === state.runtimePartner && job.status === 'working') : null;
      const actual = activeJob?.runtime_settings || room?.runtime_settings;
      const choice = actual?.members?.[state.runtimePartner];
      const actualLabel = state.runtimePartner ? choice
        ? `${choice.model ? (() => { const models = knownModels(state.runtimePartner); const model = models.find((entry) => entry.id === choice.model);
          return model ? runtimeModelLabel(model, models) : fallbackModelLabel(choice.model); })() : t("工具默认")} · ${effortLabels()[choice.effort] || choice.effort || t("默认强度")}`
        : t("未记录") : (activeJob?.work_mode || room?.work_mode) === 'plan' ? t("方案只读") : runtimePermissionName(activeJob?.permission || actual?.permission);
      $('runtime-settings-note').textContent += t(" 当前设置：{p0}。", { p0: actualLabel });
    }
    if (workMode === 'plan') $('runtime-settings-note').textContent += t(" 方案阶段始终只读。");
    if (!state.runtimePartner && nativePermissionAvailable() && permission === 'full_access') $('runtime-settings-note').textContent += t(" Full Access 可按任务要求读写本机文件、执行命令和联网。");
  }
  if (state.runtimePartner && runtimePartnerReadOnly(state.runtimePartner)) {
    $('runtime-settings-title').textContent = t("{p0} · 原有设置", { p0: displayName(state.runtimePartner) });
    $('runtime-settings-note').textContent = t("这个伙伴不再接收新工作。原有模型和思考强度仅供查看；接手伙伴仍可单独设置。");
  }
  $('runtime-settings-message').hidden = !state.runtimeMessage;
  $('runtime-settings-message').textContent = state.runtimeMessage;
  const body = $('runtime-settings-body');
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), [state.participantIds, state.runtimeOptions, choices, workMode, locked, state.runtimeLoading, state.runtimeRefreshId, editable, !!recorded, state.runtimePartner, nativePermissionAvailable(), state.participantIds.map((id) => runtimePartnerReadOnly(id)), state.roleEditor, savedCustomRoles()]]);
  if (body.dataset.fingerprint === fingerprint) return;
  const focusedId = nodeContains(body, document.activeElement) ? document.activeElement.id : '';
  body.dataset.fingerprint = fingerprint; body.replaceChildren();
  if (unrecorded) { body.append(element('p', t("这份记录没有保存权限、模型或思考强度，无法确认当时的设置。"), 'detail-caption')); return; }
  const label = element('label', workMode === 'plan' ? t("动手时统一权限 ") : t("统一权限 "), 'runtime-permission'); const select = element('select'); select.id = 'runtime-permission';
  label.hidden = !!state.runtimePartner;
  const nativePermissions = nativePlanPermissionsAvailable();
  const permissionChoices = nativePermissions ? [['plan', 'Plan'], ['full_access', 'Full Access']] : [['read_only', t("只读")], ['workspace_write', t("允许修改隔离副本")]];
  if (nativePermissions && ['read_only', 'workspace_write'].includes(permission)) permissionChoices.push([permission, runtimePermissionName(permission)]);
  if (!nativePermissions && (nativePermissionAvailable() || permission === 'full_access')) permissionChoices.push(['full_access', 'Full Access']);
  for (const [id, text] of permissionChoices) { const option = element('option', text); option.value = id; option.disabled = id === 'full_access' && !nativePermissionAvailable(); select.append(option); }
  select.value = permission; select.disabled = locked || !supported;
  select.addEventListener('change', () => {
    if (select.disabled) return;
    if (select.value === 'full_access' && !nativePermissionAvailable()) return;
    state.runtimeSettings = { ...choices, permission: select.value }; state.runtimePermissionExplicit = true; state.runtimeDraftDirty = true; state.runtimeDraftToken = crypto.randomUUID(); saveDraft(); renderControls();
  });
  label.append(select); body.append(label);
  if (!supported) body.append(element('p', state.runtimeLoading ? t("正在读取设置…") : t("展开后读取本地设置；模型保持工具默认，不会自行查询模型或启动任务。"), 'detail-caption'));
  const list = element('div', '', 'runtime-partners');
  for (const id of state.participantIds) {
    const catalog = state.runtimeOptions?.partners?.[id];
    const models = Array.isArray(catalog?.models) ? catalog.models.filter((model) => typeof model?.id === 'string' && model.id) : [];
    const historical = runtimePartnerReadOnly(id);
    const choice = (historical ? recorded?.members?.[id] : choices.members[id]) || { model: null, effort: null };
    const card = element('section', '', 'runtime-partner'); card.id = `runtime-card-${id}`; card.setAttribute('tabindex', '-1');
    card.hidden = state.runtimePartner !== id;
    const heading = element('div', '', 'runtime-partner-heading');
    heading.append(avatar(id), element('strong', displayName(id)));
    const refresh = element('button', state.runtimeRefreshId === id ? t("正在读取…") : t("读取模型选项"), 'quiet-button'); refresh.type = 'button'; refresh.id = `runtime-refresh-${id}`;
    refresh.disabled = historical || locked || !supported || catalog?.supported === false || !!state.runtimeRefreshId;
    refresh.addEventListener('click', () => void refreshRuntimeOptions(id)); heading.append(refresh); card.append(heading);
    const modelLabel = element('label', t("模型 ")); const modelSelect = element('select'); modelSelect.id = `runtime-model-${id}`;
    const defaultModel = element('option', t("跟随工具设置")); defaultModel.value = ''; modelSelect.append(defaultModel);
    for (const model of models) { const option = element('option', runtimeModelLabel(model, models)); option.value = model.id;
      if (typeof model.resolved_model === 'string') option.title = t("工具标识：{p0}", { p0: model.resolved_model });
      modelSelect.append(option); }
    if (choice.model && !models.some((model) => model.id === choice.model)) { const option = element('option', `${choice.model}${interfaceParen(historical ? t("原有设置") : t("已设置，尚未核对"))}`); option.value = choice.model; modelSelect.append(option); }
    modelSelect.value = choice.model || ''; modelSelect.disabled = historical || locked || !supported;
    modelSelect.addEventListener('change', () => {
      if (modelSelect.disabled) return;
      const next = copyRuntimeSettings(state.runtimeSettings); next.members[id] = { ...next.members[id], model: modelSelect.value || null, effort: null }; state.runtimeSettings = next; state.runtimeDraftDirty = true; state.runtimeDraftToken = crypto.randomUUID(); saveDraft(); renderRuntimeSettings();
    });
    modelLabel.append(modelSelect);
    const defaults = models.filter((model) => model.is_default === true);
    const followedModel = !choice.model && defaults.length === 1 ? defaults[0] : null;
    const selectedModel = models.find((model) => model.id === choice.model) || followedModel;
    const efforts = Array.isArray(selectedModel?.efforts) ? selectedModel.efforts.filter((effort) => typeof effort === 'string' && effort) : [];
    const effortLabel = element('label', t("思考强度 ")); const effortSelect = element('select'); effortSelect.id = `runtime-effort-${id}`;
    const defaultEffort = element('option', t("工具默认")); defaultEffort.value = ''; effortSelect.append(defaultEffort);
    for (const effort of efforts) { const option = element('option', effortLabels()[effort] || effort); option.value = effort; effortSelect.append(option); }
    if (choice.effort && !efforts.includes(choice.effort)) { const option = element('option', `${choice.effort}${interfaceParen(historical ? t("原有设置") : t("已设置，尚未核对"))}`); option.value = choice.effort; effortSelect.append(option); }
    effortSelect.value = choice.effort || ''; effortSelect.disabled = historical || locked || !supported || !selectedModel || !efforts.length;
    effortSelect.addEventListener('change', () => {
      if (effortSelect.disabled) return;
      const next = copyRuntimeSettings(state.runtimeSettings); next.members[id] = { ...choice,
        model: choice.model || (effortSelect.value ? followedModel?.id : null), effort: effortSelect.value || null };
      state.runtimeSettings = next; state.runtimeDraftDirty = true; state.runtimeDraftToken = crypto.randomUUID(); saveDraft(); renderRuntimeSettings();
    });
    effortLabel.append(effortSelect); card.append(modelLabel, effortLabel);
    if (followedModel && efforts.length) card.append(element('p', t("当前工具默认：{p0}。选择思考强度时会同时选定此模型。", { p0: runtimeModelLabel(followedModel, models) }), 'detail-caption'));
    if (selectedModel && !efforts.length) card.append(element('p', t("当前接入未提供这个模型的可选思考强度，沿用工具设置。"), 'detail-caption'));
    const catalogNote = state.runtimeRefreshId === id ? t("正在从本机工具读取模型选项…") : state.runtimeLoading ? t("正在读取本地模型目录…")
      : ['claude_cli_help_aliases', 'claude_sdk_initialize'].includes(catalog?.catalog_source) ? t("工具提供的模型目录；是否可用仍取决于当前登录和订阅。")
      : catalog?.reason === 'runtime_options_unavailable' ? t("这次未能读取模型选项。可以使用工具默认，或稍后再试。")
        : catalog?.state === 'unavailable' || catalog?.supported === false ? t("此接入暂未提供模型选项，可使用工具默认。")
        : !models.length ? t("尚未读取可选模型。点“读取模型选项”只查询工具能力，不启动任务。") : '';
    if (catalogNote) card.append(element('p', catalogNote, 'detail-caption'));
    renderRoleControls(card, id, choice, !!(historical || locked));
    list.append(card);
  }
  body.append(list);
  if (focusedId) $(focusedId)?.focus({ preventScroll: true });
}
function renderRoleControls(card, id, choice, disabled) {
  const customs = savedCustomRoles();
  const section = element('div', '', 'runtime-role');
  const roleLabel = element('label', t("角色 ")); const roleSelect = element('select'); roleSelect.id = `runtime-role-${id}`;
  const options = [['all', t("全能（默认）")], ...builtinRoles.map((role) => [role, builtinRoleInfo()[role].name]), ...customs.map((role) => [role.id, role.name])];
  // A custom role this room keeps but this device no longer lists still shows as chosen.
  if (choice.role?.id?.startsWith('custom:') && !customs.some((role) => role.id === choice.role.id)) options.push([choice.role.id, choice.role.name]);
  options.push(['new', t("新建角色…")]);
  for (const [value, text] of options) { const option = element('option', text); option.value = value; roleSelect.append(option); }
  // While a new role is being written the list says so, not the role it will replace.
  roleSelect.value = state.roleEditor?.partner === id && !state.roleEditor.id ? 'new' : choice.role?.id || 'all'; roleSelect.disabled = disabled;
  roleSelect.addEventListener('change', () => {
    if (roleSelect.disabled) return;
    if (roleSelect.value === 'new') { state.roleEditor = { partner: id, id: null, name: '', duty: '', abilities: ['work'], error: '' }; renderRuntimeSettings(); $('role-editor-name')?.focus(); return; }
    state.roleEditor = null;
    const role = roleSelect.value === 'all' ? null : builtinRoles.includes(roleSelect.value) ? { id: roleSelect.value }
      : customs.find((entry) => entry.id === roleSelect.value) || (choice.role?.id === roleSelect.value ? choice.role : null);
    setMemberChoice(id, { role });
  });
  roleLabel.append(roleSelect); section.append(roleLabel);
  if (!(state.roleEditor?.partner === id && !state.roleEditor.id)) section.append(element('p', roleDuty(choice.role), 'detail-caption role-duty'));
  const custom = choice.role?.id?.startsWith('custom:') ? choice.role : null;
  if (custom && !disabled && state.roleEditor?.partner !== id) {
    const actions = element('div', '', 'role-actions');
    const edit = element('button', t("编辑角色"), 'quiet-button'); edit.type = 'button'; edit.id = `role-edit-${id}`;
    edit.addEventListener('click', () => { state.roleEditor = { partner: id, id: custom.id, name: custom.name, duty: custom.duty, abilities: [...custom.abilities], error: '' }; renderRuntimeSettings(); $('role-editor-name')?.focus(); });
    const remove = element('button', t("从角色列表删除"), 'quiet-button'); remove.type = 'button'; remove.id = `role-delete-${id}`;
    remove.addEventListener('click', () => { saveCustomRoles(savedCustomRoles().filter((role) => role.id !== custom.id)); setMemberChoice(id, { role: null }); });
    actions.append(edit, remove); section.append(actions);
  }
  if (state.roleEditor?.partner === id && !disabled) section.append(roleEditor(id));
  const loadLabel = element('label', t("工作量 ")); const loadSelect = element('select'); loadSelect.id = `runtime-workload-${id}`;
  for (const [value, text] of [['', t("跟随账号设置")], ['low', t("少")], ['normal', t("中")], ['high', t("多")]]) { const option = element('option', text); option.value = value; loadSelect.append(option); }
  loadSelect.value = choice.workload || ''; loadSelect.disabled = disabled;
  loadSelect.addEventListener('change', () => { if (!loadSelect.disabled) setMemberChoice(id, { workload: loadSelect.value || null }); });
  loadLabel.append(loadSelect);
  section.append(loadLabel, element('p', t("只对这个任务生效，覆盖账号上的工作量设置。"), 'detail-caption'));
  card.append(section);
}
function roleEditor(id) {
  const draft = state.roleEditor;
  const form = element('div', '', 'role-editor');
  const nameLabel = element('label', t("角色名称 ")); const name = element('input'); name.id = 'role-editor-name'; name.maxLength = 20; name.value = draft.name;
  name.addEventListener('input', () => { draft.name = name.value; });
  nameLabel.append(name);
  const dutyLabel = element('label', t("职责说明 ")); const duty = element('textarea'); duty.id = 'role-editor-duty'; duty.maxLength = 300; duty.rows = 2; duty.value = draft.duty;
  duty.placeholder = t("一句话说清这个伙伴负责什么，会原样告诉它。");
  duty.addEventListener('input', () => { draft.duty = duty.value; });
  dutyLabel.append(duty);
  const abilities = element('fieldset', '', 'role-abilities'); abilities.append(element('legend', t("参与哪些环节")));
  for (const ability of roleAbilityIds) {
    const label = element('label', ''); const box = element('input'); box.type = 'checkbox'; box.id = `role-editor-ability-${ability}`;
    box.checked = draft.abilities.includes(ability);
    box.addEventListener('change', () => { draft.abilities = roleAbilityIds.filter((value) => value === ability ? box.checked : draft.abilities.includes(value)); });
    label.append(box, element('span', roleAbilityLabels()[ability])); abilities.append(label);
  }
  const error = element('p', draft.error || '', 'role-editor-error'); error.id = 'role-editor-error'; error.hidden = !draft.error; error.setAttribute('role', 'alert');
  const actions = element('div', '', 'role-actions');
  const save = element('button', t("保存角色"), 'primary-button'); save.type = 'button'; save.id = 'role-editor-save';
  save.addEventListener('click', () => {
    const role = copyRole({ id: draft.id || `custom:${crypto.randomUUID().replaceAll('-', '').slice(0, 12)}`, name: draft.name, duty: draft.duty, abilities: draft.abilities });
    if (!role) { draft.error = !draft.name.trim() ? t("请填写角色名称。") : t("至少选一个环节。"); renderRuntimeSettings(); return; }
    const roles = savedCustomRoles(); const index = roles.findIndex((entry) => entry.id === role.id);
    if (index >= 0) roles[index] = role; else roles.push(role);
    saveCustomRoles(roles); state.roleEditor = null; setMemberChoice(id, { role });
  });
  const cancel = element('button', t("取消"), 'quiet-button'); cancel.type = 'button'; cancel.id = 'role-editor-cancel';
  cancel.addEventListener('click', () => { state.roleEditor = null; renderRuntimeSettings(); $(`runtime-role-${id}`)?.focus(); });
  actions.append(save, cancel);
  form.append(nameLabel, dutyLabel, abilities, error, actions);
  return form;
}
function phaseLabel(phase) {
  return { plan: t("规划"), coordinate: t("协调"), work: t("干活"), review: t("审查"), checkpoint: t("阶段检查"),
    checkpoint_review: t("检查复核"), help: t("互助"), user: t("回复你") }[phase] || t("其他");
}
function focusRuntimeTarget() {
  const target = state.runtimePartner ? $(`runtime-model-${state.runtimePartner}`) : $('runtime-permission');
  const focus = target && !target.disabled ? target : state.runtimePartner ? $(`runtime-card-${state.runtimePartner}`) : $('runtime-settings-close');
  focus?.focus({ preventScroll: true }); focus?.scrollIntoView?.({ block: 'nearest' });
}
function focusUnchangedRuntimeTarget(expectedId) {
  if (!expectedId || document.activeElement?.id !== expectedId) return null;
  focusRuntimeTarget(); return document.activeElement?.id || null;
}
async function openRuntimePopover({ partnerId = null, trigger = $('runtime-settings') } = {}) {
  if (state.runtimePopoverOpen) return;
  closeTaskOptions({ restoreFocus: false }); closeUsagePopover(); closeContextMenu();
  state.runtimePopoverTrigger = trigger; state.runtimePartner = partnerId;
  state.runtimePopoverOpen = true;
  $('runtime-settings-popover').hidden = false;
  trigger.setAttribute('aria-expanded', 'true');
  syncModalInert();
  renderRuntimeSettings(); focusRuntimeTarget(); let focusId = document.activeElement?.id || null;
  await loadRuntimeOptions();
  if (!state.runtimePopoverOpen || state.runtimePartner !== partnerId) return;
  focusId = focusUnchangedRuntimeTarget(focusId);
  if (runtimeContext().editable && partnerId && !runtimePartnerReadOnly(partnerId) && state.runtimeOptions?.partners?.[partnerId]?.supported !== false) await refreshRuntimeOptions(partnerId);
  if (state.runtimePopoverOpen) focusUnchangedRuntimeTarget(focusId);
}
function closeRuntimePopover({ restoreFocus = true } = {}) {
  if (!state.runtimePopoverOpen) return;
  state.runtimePopoverOpen = false;
  $('runtime-settings-popover').hidden = true;
  const trigger = state.runtimePopoverTrigger;
  trigger?.setAttribute('aria-expanded', 'false');
  syncModalInert();
  if (restoreFocus) {
    const focus = trigger?.isConnected !== false && !trigger?.hidden ? trigger : $('runtime-settings');
    if (!focus?.hidden) focus?.focus({ preventScroll: true });
  }
  state.runtimePopoverTrigger = null; state.runtimePartner = null;
}
$('runtime-settings').addEventListener('click', () => void openRuntimePopover());
$('runtime-settings-close').addEventListener('click', () => closeRuntimePopover());
$('runtime-settings-popover').addEventListener('pointerdown', (event) => {
  if (event.target === $('runtime-settings-popover')) closeRuntimePopover();
});
document.addEventListener('keydown', (event) => {
  if (!state.runtimePopoverOpen) return;
  if (event.key === 'Escape') { event.preventDefault(); closeRuntimePopover(); }
  else if (event.key === 'Tab') {
    const controls = dialogControls($('runtime-settings-popover'));
    if (controls.length) {
      event.preventDefault();
      const current = controls.indexOf(document.activeElement);
      controls[(current + (event.shiftKey ? -1 : 1) + controls.length) % controls.length].focus({ preventScroll: true });
    }
  }
});

function roomUsage(id) { return state.snapshot?.members.find((member) => member.id === id)?.usage || null; }
function accountQuotaSummary(id) {
  const availability = availabilityInfo(id);
  if (availability.blocked) return { text: availability.summary, shortText: availability.value.reason === 'account_identity_unknown' ? t("待确认")
    : ['subscription_limit_reached', 'quota_exhausted'].includes(availability.value.reason) ? t("额度用完") : t("暂不可用"), detail: availability.detail, blocked: true };
  const { account } = currentAccount(id); const quota = account?.quota;
  if (state.accountRefreshId && accountRefreshKey(state.accountRefreshId) === accountRefreshKey(id)) return { text: t("正在查额度") };
  if (!account || account.state === 'not_checked' || quota?.state === 'not_checked') return { text: t("查额度"), action: true };
  const windows = (quota?.state === 'available' || quota?.stale === true) && Array.isArray(quota.windows) ? quota.windows : [];
  const shared = windows.filter((window) => window && !window.model && !window.surface
    && (window.scope === 'provider_account' || (!window.scope && quota.scope === 'provider_account')));
  const weekly = shared.filter((window) => window?.kind === 'weekly_all' || window?.window_minutes === 10080);
  const selected = weekly.length === 1 ? weekly[0] : shared.length === 1 ? shared[0] : null;
  const remaining = percentValue(selected?.remaining_percent);
  if (selected && remaining !== null) return { text: t("{p0}{p1}剩余 {p2}%", { p0: quota.stale ? t("上次：") : '', p1: quotaWindowName(selected.window_minutes, true) || t("账号额度"), p2: remaining }), shortText: t("{p0}{p1}剩余 {p2}%", { p0: quota.stale ? t("上次：") : '', p1: quotaWindowName(selected.window_minutes), p2: remaining }), remaining };
  if (windows.length) return { text: windows.every((window) => window?.scope === 'model')
    ? t("{p0}{p1} 个模型额度窗口", { p0: quota.stale ? t("上次：") : '', p1: windows.length }) : t("{p0}{p1} 个额度窗口", { p0: quota.stale ? t("上次：") : '', p1: windows.length }) };
  return { text: quotaPendingLabel(id) };
}
function renderComposerUsage() {
  const container = $('composer-usage-items'); const remaining = new Set(state.participantIds);
  for (const [id, group] of state.partnerControls) if (!remaining.has(id)) { group.remove(); state.partnerControls.delete(id); state.usageButtons.delete(id); }
  for (const [index, id] of state.participantIds.entries()) {
    let button = state.usageButtons.get(id);
    if (!button) {
      const group = element('div', '', 'partner-controls'); group.dataset.partner = id;
      const model = element('button', '', 'partner-model-button'); model.type = 'button'; model.id = `runtime-shortcut-${id}`;
      model.setAttribute('aria-haspopup', 'dialog'); model.setAttribute('aria-controls', 'runtime-settings-popover');
      model.addEventListener('click', () => void openRuntimePopover({ partnerId: id, trigger: model }));
      button = element('button', '', 'composer-usage-chip'); button.type = 'button'; button.id = `composer-usage-${id}`;
      button.setAttribute('aria-haspopup', 'dialog'); button.setAttribute('aria-controls', 'composer-usage-popover');
      button.addEventListener('click', () => openUsagePopover(id, button));
      group.append(model, button); state.partnerControls.set(id, group);
      state.usageButtons.set(id, button);
    }
    const quota = accountQuotaSummary(id);
    const availability = availabilityInfo(id);
    const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), [displayName(id), quota, availability.detail]]);
    if (button.dataset.fingerprint !== fingerprint) {
      button.dataset.fingerprint = fingerprint;
      button.classList.toggle('quota-blocked', availability.blocked);
      button.title = [quota.text, availability.detail].filter(Boolean).join(' · ');
      button.setAttribute('aria-label', t("{p0}的账号额度：{p1}。打开用量详情", { p0: displayName(id), p1: quota.text }));
      button.replaceChildren(element('span', quota.shortText || (quota.text === t("查额度") ? quota.text : quota.blocked ? t("额度受限") : quota.text.startsWith(t("当前未取得额度")) ? t("额度未取得") : quota.text), quota.action ? 'usage-query-action' : 'usage-quota-text'));
    }
    button.setAttribute('aria-expanded', String(state.usagePopover?.id === id));
    const group = state.partnerControls.get(id);
    if (container.children[index] !== group) container.insertBefore(group, container.children[index] || null);
  }
  $('composer-usage-empty').hidden = state.participantIds.length > 0;
  if (state.usagePopover && !remaining.has(state.usagePopover.id)) closeUsagePopover();
  renderRuntimeShortcuts();
  renderUsagePopover();
}
function openUsagePopover(id, trigger) {
  if (!state.participantIds.includes(id)) return;
  if (state.usagePopover?.id === id) { closeUsagePopover({ restoreFocus: true }); return; }
  closeContextMenu(); closeUsagePopover();
  state.usagePopover = { id, trigger };
  trigger.classList.toggle('usage-popover-anchor', true);
  $('composer-usage-popover').hidden = false;
  renderUsagePopover();
  $('composer-usage-close').focus({ preventScroll: true });
  const account = $(`composer-provider-account-${id}`);
  if (account) trackAccountPanel(account, id);
  renderComposerUsage();
}
function renderUsagePopover() {
  const opened = state.usagePopover; if (!opened) return;
  const { id } = opened;
  $('composer-usage-title').textContent = t("{p0} · 用量与额度", { p0: displayName(id) });
  const holder = $('composer-usage-account');
  let account = $(`composer-provider-account-${id}`);
  if (!account) { account = accountFooter(id, { scope: 'composer', compact: true }); account.open = true; holder.replaceChildren(account); }
  else updateAccountFooter(account, id);
  const fields = $('composer-usage-token-fields'); const usage = roomUsage(id);
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), usage]);
  if (fields.dataset.fingerprint !== fingerprint) { fields.dataset.fingerprint = fingerprint; renderUsage(fields, usage); }
}
function closeUsagePopover({ restoreFocus = false } = {}) {
  const opened = state.usagePopover; if (!opened) return;
  state.usagePopover = null;
  opened.trigger.classList.remove('usage-popover-anchor'); opened.trigger.setAttribute('aria-expanded', 'false');
  $('composer-usage-popover').hidden = true;
  $('composer-usage-account').replaceChildren();
  syncAccountWatchTimer();
  if (restoreFocus && nodeContains(document.body, opened.trigger)) opened.trigger.focus({ preventScroll: true });
}
$('composer-usage-close').addEventListener('click', () => closeUsagePopover({ restoreFocus: true }));
document.addEventListener('pointerdown', (event) => {
  if (state.usagePopover && !nodeContains($('composer-usage-popover'), event.target) && !nodeContains(state.usagePopover.trigger, event.target)) closeUsagePopover();
}, true);
document.addEventListener('keydown', (event) => {
  if (!state.usagePopover) return;
  if (event.key === 'Escape') { event.preventDefault(); closeUsagePopover({ restoreFocus: true }); }
  else if (event.key === 'Tab') {
    const focusable = [];
    const addFocusable = (node) => {
      if (node.hidden || node.disabled) return;
      if (node.tagName === 'SUMMARY' && node.parentNode?.classList.contains('compact-account')) return;
      if (typeof node.getClientRects === 'function' && !node.getClientRects().length) return;
      focusable.push(node);
    };
    const visit = (parent) => {
      for (const child of parent.children) {
        if (child.hidden) continue;
        if (['BUTTON', 'INPUT', 'SUMMARY'].includes(child.tagName)) addFocusable(child);
        if (child.tagName === 'DETAILS' && !child.open) { if (child.firstChild?.tagName === 'SUMMARY') addFocusable(child.firstChild); }
        else visit(child);
      }
    };
    visit($('composer-usage-popover'));
    if (focusable.length) { event.preventDefault(); const current = focusable.indexOf(document.activeElement); focusable[(current + (event.shiftKey ? -1 : 1) + focusable.length) % focusable.length].focus({ preventScroll: true }); }
  }
});

function syncParticipants(snapshot) {
  const participants = snapshot?.members || state.catalog.filter((agent) => (state.selectedParticipants || []).includes(agent.id));
  rememberAgents(participants);
  state.participantIds = participants.map((agent) => agent.id).filter((id) => agentPattern.test(id) && providers.includes(id));
  if (state.target === 'both') state.target = 'all';
  // A new task's recipient must be one of its partners: one left out since
  // (out of quota, unticked) falls back like the default does.
  if (!state.selected && (state.target === 'all' && !state.targetExplicit
      || state.target !== 'all' && !state.participantIds.includes(state.target))) {
    state.target = state.participantIds[0] || 'all';
  }
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), participants.map((agent) => [agent.id, agent.name, initials[agent.id], agent.adapter, agent.provider, beanStyle(agent.id, agent), state.partners.some((partner) => partner.id === agent.id)])]);
  if (fingerprint === state.memberFingerprint) return;
  state.memberFingerprint = fingerprint;
  $('member-cards').replaceChildren(...participants.filter((agent) => state.participantIds.includes(agent.id)).map((agent) => memberCard(agent.id, agent)));
  $('empty-avatars').replaceChildren(...participants.map((agent) => {
    const pal = element('div', '', 'bean-pal');
    pal.append(avatar(agent.id, agent), element('span', names[agent.id], 'bean-pal-name'));
    return pal;
  }));
  const picker = $('target-picker');
  picker.replaceChildren(element('span', t("先接手"), 'target-label'));
  for (const id of ['all', ...state.participantIds]) {
    const button = element('button', `@${displayName(id)}`, 'target');
    button.type = 'button'; button.dataset.target = id;
    button.addEventListener('click', () => chooseRecipient(id));
    picker.append(button);
  }
}

let recipientMenu = null;
let composingMessage = false;
function recipientProblem() {
  return state.selected && state.target !== 'all' && !state.participantIds.includes(state.target)
    ? t("原收件人已不在当前聊天，请重新选择；不会改发全体。") : '';
}
function closeRecipientMenu() {
  recipientMenu = null;
  $('recipient-menu').hidden = true;
  $('recipient-button').setAttribute('aria-expanded', 'false');
  $('message').removeAttribute('aria-activedescendant');
}
function recipientOptions(query = '') {
  const needle = query.toLocaleLowerCase();
  return ['all', ...state.participantIds].filter((id) => {
    const aliases = id === 'all' ? t("all 全体 所有人 大家") : `${id} ${displayName(id)} ${names[id] || ''} ${state.catalog.find((agent) => agent.id === id)?.adapter || ''}`;
    return aliases.toLocaleLowerCase().includes(needle);
  });
}
function renderRecipientMenu() {
  if (!recipientMenu) return;
  const options = recipientOptions(recipientMenu.query);
  recipientMenu.options = options;
  recipientMenu.index = Math.min(recipientMenu.index, Math.max(0, options.length - 1));
  $('recipient-menu').replaceChildren(...options.map((id, index) => {
    const option = element('button', id === 'all' ? t("全体") : displayName(id), 'recipient-option');
    option.type = 'button'; option.id = `recipient-option-${index}`;
    option.setAttribute('role', 'option'); option.setAttribute('aria-selected', String(index === recipientMenu.index));
    option.addEventListener('mousedown', (event) => event.preventDefault());
    option.addEventListener('click', () => chooseRecipient(id));
    return option;
  }));
  if (!options.length) $('recipient-menu').append(element('span', t("没有匹配的聊天成员"), 'detail-caption'));
  $('recipient-menu').hidden = false;
  $('recipient-button').setAttribute('aria-expanded', 'true');
  if (options.length) $('message').setAttribute('aria-activedescendant', `recipient-option-${recipientMenu.index}`);
  else $('message').removeAttribute('aria-activedescendant');
}
function chooseRecipient(id) {
  if ($('message').disabled || state.pending || state.busy || !['all', ...state.participantIds].includes(id)) return;
  const input = $('message');
  if (recipientMenu?.token) {
    const { start, end, text } = recipientMenu.token;
    // Never delete text after a click if the user or a late update changed it.
    if (input.value.slice(start, end) === text) {
      input.value = input.value.slice(0, start) + input.value.slice(end);
      input.setSelectionRange?.(start, start);
    }
  }
  state.target = id; state.targetExplicit = true;
  closeRecipientMenu(); saveDraft(); renderControls(); input.focus({ preventScroll: true });
}
function updateRecipientQuery() {
  if (composingMessage || !state.selected || $('message').disabled) return;
  const input = $('message');
  const caret = typeof input.selectionStart === 'number' ? input.selectionStart : input.value.length;
  if (typeof input.selectionEnd === 'number' && input.selectionEnd !== caret) { closeRecipientMenu(); return; }
  const before = input.value.slice(0, caret);
  const match = /(?:^|\s)@([^\s@]*)$/.exec(before);
  if (!match) { closeRecipientMenu(); return; }
  const start = caret - match[1].length - 1;
  recipientMenu = { query: match[1], index: 0, token: { start, end: caret, text: input.value.slice(start, caret) } };
  renderRecipientMenu();
}
function renderRecipientControl() {
  $('recipient-control').hidden = !state.selected;
  const problem = recipientProblem();
  $('recipient-button').textContent = state.target === 'all' ? t("发给全体 ⌄") : t("发给 @{p0} ⌄", { p0: displayName(state.target) });
  $('recipient-button').title = t("指定这条补充由谁处理；群聊历史仍共享。");
  $('recipient-button').disabled = !state.selected || $('message').disabled;
  $('recipient-clear').hidden = state.target === 'all';
  $('recipient-clear').disabled = $('recipient-button').disabled;
  const obj = currentObjective();
  $('recipient-note').textContent = problem || (state.target !== 'all' && (['paused', 'needs_user', 'stopped', 'interrupted'].includes(obj?.status))
    ? t("补充发给 {p0}；原任务仍按现有分工继续。", { p0: displayName(state.target) }) : '');
  $('recipient-note').hidden = !$('recipient-note').textContent;
  if ($('recipient-button').disabled) closeRecipientMenu();
  else if (recipientMenu) renderRecipientMenu();
}

function showNotice(message = '', type = '') {
  $('notice').textContent = message;
  $('notice').className = `notice ${type}`;
  $('notice').hidden = !message;
}

function renderActionError() {
  const error = state.actionError;
  const visible = error && error.roomId === (state.selected || 'new');
  $('action-error').textContent = visible ? error.message : '';
  $('action-error').hidden = !visible;
}
function clearActionError() {
  state.actionError = null; renderActionError();
}
function showActionError(message, roomId, action) {
  if ((state.selected || 'new') !== roomId) return;
  state.actionError = { roomId, action, message }; renderActionError();
}

function renderConnection() {
  $('connection').textContent = !state.connectionChecked ? t("正在连接本地服务…")
    : state.connected ? t("本地服务已连接") : t("本地服务未连接");
}
function setConnection(connected) {
  state.connectionChecked = true;
  state.connected = connected;
  renderConnection();
  $('connection-dot').classList.toggle('connected', connected);
  for (const room of state.rooms) updateRoomWorkIndicator(room, state.roomNodes.get(room.id));
  syncAvailabilityWatchTimer(); renderNativeSetup();
}

function statusClass(status) {
  return Object.hasOwn(statusLabels, status) ? status : 'unknown';
}

function statusBadge(status) {
  return element('span', statusLabels[statusClass(status)], `status-badge ${statusClass(status)}`);
}

function displayName(id) {
  return Object.hasOwn(names, id) ? names[id] : t("来源待确认");
}

// Mirrors project_catalog._file_list so a typed list gets a message about its actual problem.
const projectTextExtensions = new Set(['.py', '.js', '.ts', '.tsx', '.jsx', '.json', '.md', '.txt', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.css', '.html',
  '.xml', '.sql', '.rs', '.go', '.java', '.kt', '.swift', '.c', '.h', '.cpp', '.hpp', '.sh', '.bash', '.gd', '.tscn', '.tres', '.vue', '.svelte',
  '.properties', '.csv', '.rst', '.cmake', '.svg']);
function explicitFilesProblem(files) {
  if (!files.length) return t("请至少填写一个文件路径，每行一个。");
  if (files.length > 12) return t("最多 12 个文件，现在填了 {p0} 个。", { p0: files.length });
  const absolute = files.find((file) => file.startsWith('/') || file.startsWith('~') || /^[A-Za-z]:[\\/]/.test(file));
  if (absolute) return t("请写相对项目文件夹的路径，例如 src/main.py，不要写完整路径：{p0}", { p0: absolute });
  const outside = files.find((file) => file.includes('\\') || file.split('/').some((part) => !part || part === '.' || part === '..'));
  if (outside) return t("路径里不能有 ..、. 或空的一段，文件要在项目文件夹里面：{p0}", { p0: outside });
  const seen = new Set(); const repeated = files.find((file) => { const key = file.toLowerCase(); if (seen.has(key)) return true; seen.add(key); return false; });
  if (repeated) return t("这个文件填了两次：{p0}", { p0: repeated });
  const binary = files.find((file) => { const name = file.split('/').at(-1); const dot = name.lastIndexOf('.');
    return !['LICENSE', 'Makefile', 'Dockerfile'].includes(name) && !(dot > 0 && projectTextExtensions.has(name.slice(dot).toLowerCase())); });
  if (binary) return t("{p0} 不是能处理的文本文件。只能选源码、文档、配置这类文本文件。", { p0: binary });
  return '';
}
function sameFolder(a, b) { return a.replace(/\/+$/, '') === b.replace(/\/+$/, ''); }
// The same folder (and, for chosen files, the same files) is registered once; say under which name.
function registeredProjectAt(path, files) {
  const existing = state.projects.find((project) => lifecycleState(project) !== 'removed' && sameFolder(project.path, path)
    && (files ? project.selection_kind !== 'folder' && [...project.files].sort().join('\n') === [...files].sort().join('\n') : project.selection_kind === 'folder'));
  if (!existing) return '';
  return activeEntry(existing) ? t("这个文件夹已登记为「{p0}」，直接选它就行。", { p0: existing.name })
    : t("这个文件夹已登记为「{p0}」，现在在“已归档”里，恢复后就能用。", { p0: existing.name });
}
function broadFolderWarning(path) {
  const clean = path.replace(/\/+$/, '') || '/';
  if (/^\/(?:Users|home)\/[^/]+$/.test(clean)) return t("这是整个用户文件夹，里面有很多和项目无关的私人文件。建议只选项目所在的文件夹。确定要登记，就再点一次“添加项目”。");
  if (/^\/(?:Users|home|Volumes\/[^/]+)?$/.test(clean)) return t("这是整块磁盘或系统文件夹，范围太大。建议只选项目所在的文件夹。确定要登记，就再点一次“添加项目”。");
  return '';
}
function projectLimitMessage(folder) {
  return folder ? t("项目或这批修改超过本机处理上限；未保存这批修改，已有成果保留。") : errorLabels.project_limit_exceeded;
}

function executionMessage(code) {
  if (code === 'project_limit_exceeded' && state.snapshot?.room.mode === 'project_folder') return projectLimitMessage(true);
  return Object.hasOwn(executionLabels, code) ? executionLabels[code] : t("本轮结果需要核对。");
}

function timeLabel(seconds, full = false) {
  if (!Number.isFinite(seconds) || seconds <= 0) return t("时间未知");
  const options = full ? { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' } : { hour: '2-digit', minute: '2-digit' };
  return new Date(seconds * 1000).toLocaleString(window.BeanestI18n.getLocale(), options);
}

// The one name a task goes by in menus and confirmations is the sidebar's, from the room list.
// The open room's own goal field can hold the latest round's text, which the sidebar never shows.
function roomDisplayTitle(room) {
  const listed = state.rooms.find((entry) => entry.id === room?.id);
  if (listed) return roomTitle(listed);
  const objective = room?.id && state.snapshot?.room.id === room.id ? currentObjective() : null;
  return roomTitle(objective ? { ...room, objective_goal: objective.original_goal } : room);
}
function roomTitle(room) {
  const goalText = room?.goal ?? room?.goal_preview;
  const objectiveText = room?.objective_goal ?? room?.objective_goal_preview;
  const original = typeof objectiveText === 'string' && objectiveText.trim() ? objectiveText : goalText;
  const goal = typeof original === 'string' ? original.trim().replace(/\s+/g, ' ') : '';
  const firstLine = goal.split(/[。！？\n]/u)[0] || goal;
  // The list clamps titles to two lines; this cap only bounds very long first sentences (English needs more than 32).
  return firstLine.length > 60 ? `${firstLine.slice(0, 60)}…` : firstLine || t("未命名任务");
}

function roomFromLocation() {
  const match = /^#room=([0-9a-f-]+)$/i.exec(window.location?.hash || '');
  return match && uuidPattern.test(match[1]) ? match[1].toLowerCase() : null;
}
function rememberRoomLocation(id = null) {
  if (!window.location) return;
  const hash = id && uuidPattern.test(id) ? `#room=${id.toLowerCase()}` : '';
  if (window.location.hash === hash) return;
  if (window.history?.replaceState) window.history.replaceState(null, '', `${window.location.pathname || '/'}${window.location.search || ''}${hash}`);
  else window.location.hash = hash;
}
function leaveUnavailableRoom({ preserveLocation = false } = {}) {
  startNewRoom({ preserveRoomHash: preserveLocation });
  // A pending creation has no room ID. Keep its original receipt and body,
  // but never leave an inaccessible room selected behind that receipt.
  if (state.selected) {
    ++state.epoch; state.selected = null; state.snapshot = null;
    clearActionError(); closeReader(); renderRooms(); renderEvents(null); renderMembers(null); renderControls();
  }
  if (!preserveLocation) rememberRoomLocation();
}

function roomPath(id) {
  if (!uuidPattern.test(id)) throw new RequestError(t("任务记录格式异常，请刷新状态。"));
  return `/api/rooms/${id}`;
}

async function api(path, method = 'GET', body) {
  if (state.roomPagingSupported && path.startsWith('/api/partners') && !path.includes('view=')) path += `${path.includes('?') ? '&' : '?'}view=compact`;
  const encoded = body === undefined ? undefined : JSON.stringify(body);
  if (encoded && utf8Bytes(encoded) > (state.roomPagingSupported || path.endsWith('/messages/receipt') ? transportLimits.max_request_utf8_bytes : 16 * 1024)) {
    throw new RequestError(state.roomPagingSupported ? errorLabels.request_too_large : t("当前服务的传输容量不足，原文已保留，请更新服务后再发送。"));
  }
  const partnerContext = path.startsWith('/api/partners') ? { intentEpoch: state.partnerIntentEpoch, generation: ++state.partnerRequestGeneration } : null;
  const codeStatusRead = method === 'GET' && /^\/api\/rooms\/[^/]+\/code-status(?:\?|$)/.test(path);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), method === 'GET' ? 20000 : 75000);
  try {
    const response = await fetch(path, {
      method, headers: { 'X-LAC-Token': token, ...(encoded === undefined ? {} : { 'Content-Type': 'application/json' }) },
      body: encoded, signal: controller.signal, credentials: 'same-origin', mode: 'same-origin', redirect: 'error', cache: 'no-store',
    });
    const result = await response.json();
    if (!result || typeof result !== 'object') throw new RequestError(t("本地服务返回了无法识别的记录。"), { uncertain: method !== 'GET' });
    if (!response.ok || result.error) {
      const code = typeof result.error === 'string' ? result.error : '';
      const folder = path === workspaceApi.projects ? body?.selection_kind === 'folder'
        : (body?.mode || state.snapshot?.room.mode || state.mode) === 'project_folder';
      const message = code === 'project_limit_exceeded' ? projectLimitMessage(folder)
        // Registering a folder that is missing or unreadable is about the folder, not "selected files".
        : code === 'project_file_unavailable' && path === workspaceApi.projects && body?.selection_kind === 'folder' ? t("找不到这个文件夹，或者没有权限读取。请核对路径。")
        : errorLabels[code];
      throw new RequestError(message || t("请求未完成，请核对本地服务与任务状态。"), {
        code, uncertain: method !== 'GET' && !workspaceRejections.has(code)
          && (response.status >= 500 || !Object.hasOwn(errorLabels, code)),
        details: result,
      });
    }
    if (partnerContext) state.partnerResponseContexts.set(result, partnerContext);
    return result;
  } catch (error) {
    if (error instanceof RequestError) throw error;
    if (codeStatusRead) throw new RequestError(controller.signal.aborted
      ? t("本次代码查询等待超时，尚未取得新结果；可以稍后重试。")
      : t("本次代码查询未完成，尚未取得新结果；可以稍后重试。"), { code: controller.signal.aborted ? 'code_status_timeout' : 'code_status_unavailable' });
    throw new RequestError(t("连接中断或等待超时，请先刷新。如果刚重启过豆窝，旧浏览器地址可能失效，请从桌面版重新打开。"), { uncertain: method !== 'GET' });
  } finally {
    clearTimeout(timeout);
  }
}

function managerNotice(message = '', error = false) {
  $('manager-notice').textContent = message;
  $('manager-notice').hidden = !message;
  $('manager-notice').className = `notice${error ? ' error' : ''}`;
}

const lifecycleLabels = localizedLabels({ active: '当前', archived: '已归档', removed: '已移除' });
function lifecycleState(item) { return ['archived', 'removed'].includes(item?.lifecycle_state) ? item.lifecycle_state : 'active'; }
function activeEntry(item) { return lifecycleState(item) === 'active'; }
function eligibleAgent(agent) {
  const registered = state.partners.find((partner) => partner.id === agent.id);
  const latest = state.partnerLifecycleSupported && latestPartnerOperation(agent.id);
  const removed = latest?.action === 'remove' && latest.status === 'completed';
  return !removed && !pendingPartnerOperation(agent.id) && agent.enabled !== false && activeEntry(agent) && (!registered || (registered.enabled !== false && activeEntry(registered)));
}
function notSetUp(agent) {
  return ['not_installed', 'login_required'].includes(state.adapters.find((adapter) => adapter.id === (agent?.adapter || agent?.id))?.setup?.state);
}
function selectableAgent(agent, { retainBlocked = false } = {}) {
  return eligibleAgent(agent) && !notSetUp(agent) && (retainBlocked || !availabilityInfo(agent.id).blocked);
}
function quotaPendingLabel(id) {
  const agent = state.catalog.find((entry) => entry.id === id);
  const registered = state.partners.find((entry) => entry.id === id);
  // "Still usable" only when the checkbox beside it can actually be ticked.
  const usable = agent && selectableAgent(agent, { retainBlocked: true }) && registered?.selectable === true && registered.installation?.state !== 'missing';
  return t("当前未取得额度{p0}", { p0: usable ? t(" · 仍可使用") : '' });
}
function visibleEntries(kind, items) { return items.filter((item) => lifecycleState(item) === state.lifecycleViews[kind]); }
function renderLifecycleFilter(kind, items) {
  const picker = $(`${kind}-filter`);
  if (kind === 'rooms' && state.roomLifecycleSupported || kind === 'projects' && state.projectDeletionSupported || kind === 'partners' && state.partnerLifecycleSupported) {
    for (const option of [...picker.children]) if (option.getAttribute('value') === 'removed') option.remove();
    if (state.lifecycleViews[kind] === 'removed') state.lifecycleViews[kind] = 'active';
  }
  if ((kind === 'rooms' && !state.roomLifecycleSupported || kind === 'projects' && !state.projectDeletionSupported || kind === 'partners' && !state.partnerLifecycleSupported) && ![...picker.children].some((option) => option.getAttribute('value') === 'removed')) {
    const option = element('option', t("已移除")); option.setAttribute('value', 'removed'); picker.append(option);
  }
  for (const option of picker.children) {
    const value = option.getAttribute('value');
    option.value = value;
    option.textContent = `${lifecycleLabels[value]} · ${items.filter((item) => lifecycleState(item) === value).length}`;
  }
  picker.value = state.lifecycleViews[kind];
}
function lifecycleBusy(kind, item) {
  if (kind === 'partners' && state.partnerLifecycleSupported) return !!pendingPartnerOperation(item.id);
  if (kind === 'rooms' && state.roomLifecycleSupported) return ['pending', 'unknown'].includes(state.lifecycleOperations.get(item.id)?.status);
  if (kind === 'projects' && state.projectDeletionSupported) return state.projectDeletions.has(item.id);
  if (item?.lifecycle_busy === true || item?.lifecycle_can_change === false) return true;
  if (kind === 'rooms') {
    const snapshot = state.snapshot?.room.id === item.id ? state.snapshot : null;
    return item.status === 'running' || item.outcome_state === 'in_progress'
      || snapshot?.jobs.some((job) => ['working', 'queued'].includes(job.status)) === true;
  }
  return false;
}
function contextItemFingerprint(item) {
  return JSON.stringify([item?.id, item?.name || item?.goal, item?.objective_goal, lifecycleState(item), item?.status, item?.outcome_state, item?.objective_status, item?.lifecycle_can_change, item?.lifecycle_blocked_reason]);
}
function currentContextItem(kind, id) {
  return (kind === 'rooms' ? state.rooms : kind === 'projects' ? state.projects : state.partners).find((entry) => entry.id === id);
}
function nodeContains(parent, node) {
  while (node) { if (node === parent) return true; node = node.parentNode; }
  return false;
}
function nativeContextNeeded(event) {
  if (window.getSelection?.()?.toString().trim()) return true;
  let target = event.target;
  while (target) {
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName) || target.isContentEditable
      || (target.getAttribute?.('contenteditable') !== null && target.getAttribute?.('contenteditable') !== undefined
        && target.getAttribute('contenteditable') !== 'false')) return true;
    target = target.parentNode;
  }
  return false;
}
function closeContextMenu({ restoreFocus = false } = {}) {
  const context = state.contextMenu;
  if (!context) return;
  state.contextMenu = null;
  context.target.classList.remove('entry-context-anchor');
  context.target.setAttribute('aria-expanded', 'false');
  $('entry-context-menu').hidden = true;
  $('entry-context-actions').replaceChildren();
  if (restoreFocus) {
    const target = nodeContains(document.body, context.target) ? context.target : $(`${context.kind}-filter`);
    target.focus({ preventScroll: true });
  }
}
function openContextMenu(kind, id, target, event) {
  if (state.lifecycleConfirm) return;
  if (nativeContextNeeded(event)) { closeContextMenu(); return; }
  const item = currentContextItem(kind, id);
  if (!item) return;
  event.preventDefault();
  closeUsagePopover();
  closeContextMenu();
  state.contextMenu = { kind, id, target, itemFingerprint: contextItemFingerprint(item) };
  target.classList.toggle('entry-context-anchor', true);
  target.setAttribute('aria-expanded', 'true');
  $('entry-context-title').textContent = `${kind === 'rooms' ? t("管理任务") : kind === 'projects' ? t("管理项目") : t("管理伙伴")} · ${kind === 'rooms' ? roomDisplayTitle(item) : item.name}`;
  const actions = lifecycleActions(kind, item, { beforeAction: () => closeContextMenu({ restoreFocus: true }) });
  for (const button of actions.children) if (button.tagName === 'BUTTON') {
    button.setAttribute('role', 'menuitem'); button.setAttribute('tabindex', '-1');
  }
  $('entry-context-actions').replaceChildren(actions);
  $('entry-context-menu').hidden = false;
  const first = [...actions.children].find((node) => node.tagName === 'BUTTON' && !node.disabled);
  (first || $('entry-context-menu')).focus({ preventScroll: true });
}
function bindContextTarget(target, kind, id) {
  target.classList.toggle('context-enabled', true);
  target.setAttribute('aria-haspopup', 'menu'); target.setAttribute('aria-expanded', 'false');
  target.setAttribute('aria-controls', 'entry-context-menu');
  target.setAttribute('aria-keyshortcuts', 'Shift+F10');
  if (target.tagName !== 'BUTTON') target.setAttribute('tabindex', '0');
  target.addEventListener('contextmenu', (event) => openContextMenu(kind, id, target, event));
  target.addEventListener('keydown', (event) => {
    if (event.key === 'ContextMenu' || (event.key === 'F10' && event.shiftKey)) openContextMenu(kind, id, target, event);
  });
}
document.addEventListener('pointerdown', (event) => {
  if (state.contextMenu && !nodeContains($('entry-context-menu'), event.target)) closeContextMenu();
}, true);
document.addEventListener('scroll', (event) => {
  if (state.contextMenu && !nodeContains($('entry-context-menu'), event.target)) closeContextMenu();
}, true);
window.addEventListener('resize', () => closeContextMenu());
document.addEventListener('keydown', (event) => {
  if (!state.contextMenu) return;
  if (event.key === 'Escape') { event.preventDefault(); closeContextMenu({ restoreFocus: true }); return; }
  if (event.key === 'Tab') { closeContextMenu({ restoreFocus: true }); return; }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const buttons = [...$('entry-context-actions').firstChild.children].filter((node) => node.tagName === 'BUTTON' && !node.disabled);
  if (!buttons.length) return;
  const current = buttons.indexOf(document.activeElement);
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
    : (current + (event.key === 'ArrowUp' ? -1 : 1) + buttons.length) % buttons.length;
  buttons[next].focus({ preventScroll: true });
});

function lifecycleActions(kind, item, { beforeAction = () => {} } = {}) {
  const actions = element('div', '', 'lifecycle-actions');
  const status = lifecycleState(item);
  const busy = kind === 'projects' && state.projectDeletionSupported ? state.projectDeletions.has(item.id) : lifecycleBusy(kind, item);
  const permanent = kind === 'rooms' && state.roomLifecycleSupported || kind === 'projects' && state.projectDeletionSupported || kind === 'partners' && state.partnerLifecycleSupported;
  const choices = status === 'active' ? [['archive', t("归档")], ['remove', t("移除")]]
    : [['restore', t("恢复")], ...(status === 'archived' || permanent ? [['remove', t("移除")]] : [])];
  if (kind === 'projects' && status !== 'removed') {
    // Renaming changes only the registered display name; it is edited in place on the card.
    const rename = element('button', t("改名"), 'quiet-button'); rename.type = 'button';
    rename.dataset.projectRename = item.id;
    rename.disabled = state.managementBusy || state.projectRename?.id === item.id
      || (state.projectDeletionSupported && state.projectDeletions.has(item.id));
    rename.setAttribute('aria-label', `${t("改名")}${interfaceWordSpace()}${englishInterface() ? t("项目").toLowerCase() : t("项目")}${interfaceColon()}${item.name}`);
    rename.addEventListener('click', () => {
      if (rename.disabled || state.managementBusy) return;
      beforeAction(); startProjectRename(item.id);
    });
    actions.append(rename);
  }
  for (const [action, originalLabel] of choices) {
    const label = permanent && action === 'remove' ? kind === 'partners' ? t("移除配置") : t("永久删除") : originalLabel;
    const button = element('button', label, `quiet-button${action === 'remove' ? ' danger' : ''}`); button.type = 'button';
    button.dataset.lifecycleAction = action; button.dataset.lifecycleKind = kind; button.dataset.lifecycleId = item.id;
    button.disabled = state.managementBusy || (!permanent && (state.busy || !!state.pending)) || (permanent ? busy : action !== 'restore' && busy);
    const noun = kind === 'rooms' ? t("任务") : kind === 'projects' ? t("项目") : t("伙伴");
    button.setAttribute('aria-label', `${label}${interfaceWordSpace()}${englishInterface() ? noun.toLowerCase() : noun}${interfaceColon()}${kind === 'rooms' ? roomDisplayTitle(item) : item.name}`);
    if (permanent && busy) button.title = kind === 'partners' ? t("正在处理这个伙伴的操作。") : kind === 'projects' ? t("正在核对项目登记删除结果。") : t("正在停止任务并处理记录，请稍候。");
    else if (busy && action !== 'restore') button.title = item.lifecycle_blocked_reason === 'team_cleanup_unconfirmed'
      ? t("执行清理尚未确认，暂时不能归档或移除。") : t("任务仍在处理或等待清理，结束后可整理。");
    button.addEventListener('click', () => {
      if (state.managementBusy || (!permanent && (state.busy || state.pending)) || (permanent ? lifecycleBusy(kind, item) : action !== 'restore' && lifecycleBusy(kind, item))) return;
      beforeAction();
      if (action === 'remove') confirmLifecycle(kind, item); else void mutateLifecycle(kind, item.id, action);
    });
    actions.append(button);
  }
  if (permanent && state.lifecycleOperations.has(item.id)) {
    const operation = state.lifecycleOperations.get(item.id);
    if (operation.status !== 'completed') actions.append(element('p', lifecycleOperationText(operation), 'detail-caption'));
    if (operation.status === 'unknown') {
      const check = element('button', t("核对处理结果"), 'quiet-button'); check.type = 'button';
      check.addEventListener('click', async () => { await checkLifecycleOperation(operation); await refresh(); }); actions.append(check);
    }
  }
  else if (kind === 'projects' && state.projectDeletionSupported && busy) actions.append(element('p', t("项目登记删除尚未完成，原项目文件和已有聊天保留。"), 'detail-caption'));
  else if (kind === 'partners' && state.partnerLifecycleSupported && busy) actions.append(element('p', t("正在处理这个伙伴，已有聊天和改动保留。"), 'detail-caption'));
  else if (busy) actions.append(element('p', item.lifecycle_blocked_reason === 'team_cleanup_unconfirmed'
    ? t("执行清理尚未确认，暂时不能归档或移除。记录和成果仍可查看。")
    : t("仍有任务在处理或等待清理，结束后可归档或移除。"), 'detail-caption'));
  return actions;
}
function canDetachPendingRequest() {
  const request = state.pending;
  if (state.scopeChanged || !request) return false;
  return state.messageReceiptsSupported && request.kind === 'message'
    || request.kind === 'create' && state.hostOutboxSupported && request.hostOutbox?.scope === state.hostOutboxScope;
}
function detachMessageForNavigation() {
  if (!canDetachPendingRequest()) return;
  const request = state.pending;
  if (request.kind === 'create' && $('message').value.trim() === request.body.goal?.trim()) {
    $('message').value = ''; forgetComposerDraft('new');
  }
  request.detached = true;
  settleIntervenedRequest(request, state.busyRequest === request ? 'sending' : 'unknown');
}
function finishMessageReceipt(request) {
  retireOutboxRequest(request);
  const text = request.body?.text;
  clearAcceptedRuntimeDraft(request);
  if (state.pending === request) { state.pending = null; rememberPending(); }
  if (state.busyRequest === request) { state.busyRequest = null; state.busy = false; }
  state.sidelinedRequests.delete(request.body?.request_id || request.receipt_id);
  const draft = state.drafts.get(request.roomId);
  if (!request.restoredFromHost && draft?.text?.trim() === text && !draft.runtimeDraftDirty && !draft.workModeDraftDirty) forgetComposerDraft(request.roomId);
  if (!request.restoredFromHost && state.selected === request.roomId && $('message').value.trim() === text) $('message').value = '';
  rememberLifecycleOperations();
}
function rejectUndeliveredMessage(request, error) {
  retireOutboxRequest(request, 'abandoned');
  const body = request.body;
  if (state.pending === request) { state.pending = null; rememberPending(); }
  state.sidelinedRequests.delete(body.request_id || request.receipt_id);
  const draft = state.drafts.get(request.roomId) || {};
  const restoreRuntime = !!body.runtime_settings && (!draft.runtimeDraftDirty || requestOwnsRuntimeDraft(request, draft, 'runtime'));
  const restoreMode = !!body.work_mode && (!draft.workModeDraftDirty || requestOwnsRuntimeDraft(request, draft, 'workMode'));
  state.drafts.set(request.roomId, { ...draft, text: draft.text || body.text,
    ...(restoreRuntime ? { runtimeSettings: copyRuntimeSettings(body.runtime_settings), runtimeDraftDirty: true, runtimeDraftToken: request.runtimeDraftToken } : {}),
    ...(restoreMode ? { workMode: body.work_mode, workModeDraftDirty: true, workModeDraftToken: request.workModeDraftToken } : {}) });
  if (state.selected === request.roomId) {
    if (body.runtime_settings && (!state.runtimeDraftDirty || requestOwnsRuntimeDraft(request, state, 'runtime'))) {
      state.runtimeSettings = copyRuntimeSettings(body.runtime_settings); state.runtimeDraftDirty = true; state.runtimeDraftToken = request.runtimeDraftToken;
    }
    if (body.work_mode && (!state.workModeDraftDirty || requestOwnsRuntimeDraft(request, state, 'workMode'))) {
      state.workMode = body.work_mode; state.workModeDraftDirty = true; state.workModeDraftToken = request.workModeDraftToken;
    }
    showActionError(error.message, request.roomId, 'message');
  }
  rememberLifecycleOperations();
}
async function checkMessageReceipt(request) {
  if (request.hostOutbox) {
    if (state.hostOutboxSupported && request.hostOutbox.scope === state.hostOutboxScope) {
      try { settleHostDelivery(request, await readHostOutbox(request)); await flushHostRetirements(); } catch (_error) { /* Preserve the exact original intent. */ }
    }
    return;
  }
  const key = request.body?.request_id;
  if (!state.messageReceiptsSupported || request.kind !== 'message' || request.intervened === true
      || state.deletedRooms.has(request.roomId) || !request.body?.text || state.messageReceiptChecks.has(key)) return;
  state.messageReceiptChecks.add(key);
  let redelivering = false;
  try {
    let result;
    try { result = await api(`${roomPath(request.roomId)}/messages/receipt`, 'POST', request.body); }
    catch (error) {
      if (error.code !== 'message_request_not_found') throw error;
      if (request.intervened === true || state.deletedRooms.has(request.roomId) || state.scopeChanged
          || messageHasRuntimeSettings(request.body) && !state.naturalRuntimeSupported) return;
      // The journal, not a network failure or a new binding, authorizes same-request redelivery.
      if (!state.roomPagingSupported && request.body.text.length > 3000) return;
      try { await rememberPending(); } catch (_error) { throw new RequestError(t("原文仍保留，但发送记录尚未可靠保存，没有重新发送。"), { uncertain: true }); }
      redelivering = true;
      const snapshot = await api(`${roomPath(request.roomId)}/messages${state.roomPagingSupported ? '?response=ack' : ''}`, 'POST', request.body);
      if ((snapshot?.acknowledged === true ? snapshot.room_id : snapshot?.room?.id) !== request.roomId) throw new RequestError(t("发送结果的任务身份不匹配。"), { uncertain: true });
      result = { received: true, room_id: request.roomId, request_id: key };
    }
    if (result?.received !== true || result.room_id !== request.roomId || result.request_id !== key) {
      throw new RequestError(t("消息回执身份尚未确认。"), { uncertain: true });
    }
    finishMessageReceipt(request);
    renderControls();
    await refresh();
  } catch (error) {
    if (error.code === 'room_deleted') { forgetDeletedRoom(request.roomId); return; }
    if (redelivering && error.code && !error.uncertain && error.code !== 'request_conflict'
        && request.intervened !== true && !state.deletedRooms.has(request.roomId)) {
      rejectUndeliveredMessage(request, error); return;
    }
    if (error.code === 'request_conflict') {
      request.receiptBlocked = true; request.receiptError = error.code;
      if (state.pending === request) showNotice(t("消息尚未确认，请核对原文。草稿已保留，没有重新发送。"), 'error');
    }
  } finally { state.messageReceiptChecks.delete(key); rememberLifecycleOperations(); renderControls(); }
}
async function refreshMessageReceipts() {
  if (!state.messageReceiptsSupported || state.scopeChanged) return;
  const requests = [...state.sidelinedRequests.values()].filter(({ request, receipt }) => request.detached === true && receipt === 'unknown').map(({ request }) => request);
  if (state.pending?.kind === 'message' && !state.busy) requests.push(state.pending);
  const unique = new Map(requests.map((request) => [request.body?.request_id, request]));
  for (const request of unique.values()) if (!request.receiptBlocked && !request.storageFailed) await checkMessageReceipt(request);
}
function markRoomIntervention(id, marker) {
  state.roomControlRequests.set(id, marker);
  for (const { request } of state.sidelinedRequests.values()) if (request.roomId === id) request.intervened = true;
  for (const request of state.hostOutboxRequests.values()) if (request.roomId === id) request.intervened = true;
  rememberLifecycleOperations();
  if (state.pending?.roomId === id) { state.pending.intervened = true; rememberPending(); }
}
function settleIntervenedRequest(request, receipt) {
  request.receipt_id ||= request.body?.request_id || crypto.randomUUID();
  if (receipt === 'received') retireOutboxRequest(request);
  if (receipt === 'received' || state.deletedRooms.has(request.roomId)) request.body = { request_id: request.body?.request_id || request.receipt_id };
  state.sidelinedRequests.set(request.receipt_id, { request, receipt });
  if (state.pending === request) { state.pending = null; rememberPending(); }
  if (state.busyRequest === request) { state.busyRequest = null; state.busy = false; }
  rememberLifecycleOperations();
}
function parkIntervenedRequest(id) {
  if (state.pending?.roomId === id && state.pending.intervened === true) settleIntervenedRequest(state.pending, 'unknown');
}
function forgetDeletedRoom(id) {
  if (!uuidPattern.test(id)) return;
  state.deletedRooms.add(id); roomHistories.delete(id);
  if (roomFromLocation() === id) rememberRoomLocation();
  if (state.actionError?.roomId === id) clearActionError();
  if (state.pending?.roomId === id) { state.pending.intervened = true; settleIntervenedRequest(state.pending, 'unknown'); }
  for (const { request } of state.sidelinedRequests.values()) {
    if (request.roomId === id) request.body = { request_id: request.body?.request_id || request.receipt_id };
  }
  for (const [key, request] of state.hostOutboxRequests) if (request.roomId === id) {
    request.intervened = true; request.body = { request_id: key };
    state.hostOutboxRequests.delete(key); state.hostOutboxRows.delete(key);
  }
  forgetComposerDraft(id); state.rooms = state.rooms.filter((room) => room.id !== id);
  for (const partner of [...state.partners, ...state.catalog]) {
    if (Array.isArray(partner.monitor?.current_tasks)) partner.monitor.current_tasks = partner.monitor.current_tasks.filter((task) => task.room_id !== id);
  }
  state.partnerListFingerprint = ''; renderGlobalPartners();
  if (state.forkSource?.roomId === id) state.forkSource = null;
  if (state.handoffHistory?.roomId === id || state.handoffHistory?.sourceRoomId === id) {
    state.handoffHistory = null; $('earlier-messages').replaceChildren(); $('earlier-discussion').hidden = true;
  }
  if (state.contextMenu?.id === id) { closeContextMenu(); $('entry-context-title').textContent = ''; }
  if (state.selected === id) {
    rememberRoomLocation();
    ++state.epoch; state.selected = null; state.snapshot = null;
    closeReader(); $('reader-body').replaceChildren(); $('reader-files').replaceChildren();
    for (const key of ['reader-title', 'reader-meta', 'reader-update']) $(key).textContent = '';
    state.reader = null; state.eventNodes.clear(); state.progressNodes.clear(); $('events').replaceChildren();
    state.renderedRoom = null; $('message').value = ''; renderEvents(null); renderMembers(null);
  }
  if (!state.pending) { $('pending-description').textContent = ''; $('pending-title').textContent = ''; }
  if (!state.lifecycleConfirm) { $('lifecycle-confirm-title').textContent = t("永久删除任务？"); $('lifecycle-confirm-description').textContent = ''; }
  if (state.exportRoom === id) { state.exportRoom = null; state.exportMessage = ''; }
  rememberPending(); rememberLifecycleOperations(); renderRooms(); renderControls();
}
async function stopRoomIndependently(id) {
  if (state.stopRequests.has(id)) return;
  clearActionError();
  const marker = crypto.randomUUID(); markRoomIntervention(id, marker); state.stopRequests.add(id); renderControls();
  try {
    let result = await api(`/api/rooms/${encodeURIComponent(id)}/cancel${state.roomPagingSupported ? '?response=ack' : ''}`, 'POST', {});
    if (result?.acknowledged === true && result.room_id === id) {
      parkIntervenedRequest(id);
      try { result = await readRoomSnapshot(id); } catch (_error) { showActionError(t("停止请求已收到，当前状态暂未加载完成。"), id, 'cancel'); return; }
    }
    if (result?.room?.id !== id) throw new RequestError(t("停止结果的任务身份不匹配。"));
    setConnection(true); parkIntervenedRequest(id);
    if (state.selected === id && state.roomControlRequests.get(id) === marker) applySnapshot(result);
  } catch (error) {
    if (error.code && !error.uncertain) showActionError(error.message, id, 'cancel');
    else showNotice(t("停止结果尚未确认，记录仍保留。可以再次停止；不会重新派发原请求。"), 'error');
  }
  finally { state.stopRequests.delete(id); renderRooms(); renderControls(); }
}
async function refreshLifecycleOperations() {
  if (!state.roomLifecycleSupported || state.lifecyclePolling) return;
  state.lifecyclePolling = true;
  try {
    for (const operation of [...state.lifecycleOperations.values()]) {
      if (['pending', 'unknown'].includes(operation.status)) await checkLifecycleOperation(operation);
    }
  } finally { state.lifecyclePolling = false; renderRooms(); renderControls(); }
}
function rememberLifecycleOperations() {
  if (!state.authScope) return;
  if (outboxRestored) rememberPending();
  try { sessionStorage.setItem(lifecycleStorageKey, JSON.stringify({ scope: state.authScope,
    operations: [...state.lifecycleOperations.values()].filter((operation) => ['pending', 'unknown', 'failed'].includes(operation.status)),
    partner_operations: [...state.partnerOperations.values()], ...(!outboxCommitted ? { sidelined_requests: JSON.parse(sessionStorage.getItem(lifecycleStorageKey) || '{}').sidelined_requests || [] } : {}), project_deletions: [...state.projectDeletions.values()], deleted_rooms: [...state.deletedRooms] })); }
  catch (_error) { /* Keep the same operation in memory when browser storage is unavailable. */ }
}
function restoreLifecycleOperations() {
  try {
    const legacy = readSessionJson(lifecycleStorageKey);
    const durable = state.restoredOutbox?.scope === state.authScope ? state.restoredOutbox : null;
    const saved = legacy?.scope === state.authScope ? { ...legacy, ...(durable ? { sidelined_requests: durable.sidelined_requests, deleted_rooms: [...new Set([...(legacy.deleted_rooms || []), ...(durable.deleted_rooms || [])])] } : {}) }
      : durable ? { ...durable, operations: [] } : null;
    if (!saved || saved.scope !== state.authScope || !Array.isArray(saved.operations)) return;
    for (const intent of saved.project_deletions || []) if (uuidPattern.test(intent.project_id) && uuidPattern.test(intent.request_id)) state.projectDeletions.set(intent.project_id, intent);
    for (const id of saved.deleted_rooms || []) if (uuidPattern.test(id)) state.deletedRooms.add(id);
    for (const operation of saved.partner_operations || []) {
      if (agentPattern.test(operation?.provider) && uuidPattern.test(operation?.request_id) && ['pending', 'unknown', 'completed', 'failed'].includes(operation.status)
          && ['enable', 'disable', 'archive', 'restore', 'remove'].includes(operation.action)) {
        operation.observedGeneration = 0; operation.intentOrder = Number.isSafeInteger(operation.intentOrder) ? operation.intentOrder : ++state.partnerOperationSequence;
        state.partnerOperationSequence = Math.max(state.partnerOperationSequence, operation.intentOrder);
        state.partnerOperations.set(operation.request_id, operation);
      }
    }
    for (const receipt of saved.sidelined_requests || []) {
      if ((receipt?.request?.intervened === true || receipt?.request?.detached === true) && uuidPattern.test(receipt.request.roomId)
          && ['sending', 'unknown', 'received', 'blocked'].includes(receipt.receipt)) {
        if (receipt.receipt === 'sending') receipt.receipt = 'unknown';
        if (!receipt.request.hostOutbox) receipt.request.legacyUnknown = true;
        if (state.pending?.kind === 'message' && state.pending.body.request_id === receipt.request.body?.request_id) receipt.request = state.pending;
        state.sidelinedRequests.set(receipt.request.body?.request_id || receipt.request.receipt_id, receipt);
      }
    }
    for (const operation of saved.operations) {
      if (!uuidPattern.test(operation.room_id) || !uuidPattern.test(operation.request_id)
          || !['archive', 'restore', 'remove'].includes(operation.action)
          || !['pending', 'unknown', 'failed'].includes(operation.status)
          || operation.id && !uuidPattern.test(operation.id)) continue;
      state.lifecycleOperations.set(operation.room_id, operation);
    }
  } catch (_error) { /* Invalid cached metadata never authorizes an action. */ }
}
function acceptLifecycleOperation(operation) {
  if (!state.roomLifecycleSupported || !operation || !uuidPattern.test(operation.id)
      || !uuidPattern.test(operation.room_id) || !['archive', 'restore', 'remove'].includes(operation.action)
      || !['pending', 'completed', 'failed'].includes(operation.status)) return;
  const previous = state.lifecycleOperations.get(operation.room_id);
  const otherRequest = previous?.request_id && previous.request_id !== operation.request_id;
  if (otherRequest && (['pending', 'unknown'].includes(previous.status)
      || previous.status === 'failed' && previous.originatedHere === true && operation.status === 'completed')) return;
  state.lifecycleOperations.set(operation.room_id, { ...(otherRequest ? {} : previous), ...operation }); rememberLifecycleOperations();
}
function acceptLifecycleOperations(operations) {
  if (Array.isArray(operations)) for (const operation of operations) acceptLifecycleOperation(operation);
}
function lifecycleOperationText(operation) {
  if (operation.status === 'pending' && operation.action === 'restore') return t("正在恢复任务…");
  if (operation.status === 'pending') return operation.action === 'remove' ? t("正在停止任务并永久删除…") : t("正在停止任务并归档…");
  if (operation.status === 'unknown') return t("操作结果尚未确认，记录仍保留。");
  if (operation.status === 'failed') {
    const reasons = { team_cleanup_unconfirmed: t("执行清理尚未确认"), purge_files_incomplete: t("文件清理未完成"),
      purge_workspace_busy: t("工作副本仍在使用"), purge_source_in_use: t("仍有任务依赖这份记录"),
      purge_dependency_unconfirmed: t("关联资料尚未核对"), purge_reference_conflict: t("关联资料存在冲突"), storage_unavailable: t("本地存储暂不可用") };
    const reason = reasons[operation.error] || t("本地整理遇到问题");
    return operation.action === 'remove' ? t("永久删除未完成：{p0}。记录仍保留。", { p0: reason }) : t("整理未完成：{p0}。记录仍保留。", { p0: reason });
  }
  return operation.action === 'remove' ? t("任务已永久删除。") : operation.action === 'archive' ? t("任务已归档。") : t("任务已恢复。");
}
async function checkLifecycleOperation(operation) {
  const path = operation.id ? `/api/room-lifecycle-operations/${encodeURIComponent(operation.id)}`
    : `/api/room-lifecycle-operations?request_id=${encodeURIComponent(operation.request_id)}`;
  try { applyLifecycleResult(await api(path), operation); }
  catch (error) {
    // Only an authoritative journal miss permits re-delivery of the original user action.
    if (error.code === 'lifecycle_operation_not_found' && state.roomLifecycleSupported
        && operation.originatedHere === true && !operation.id && uuidPattern.test(operation.request_id)) {
      try {
        const result = await api(`/api/rooms/${encodeURIComponent(operation.room_id)}/lifecycle`, 'POST',
          { action: operation.action, request_id: operation.request_id });
        applyLifecycleResult(result, operation);
        return;
      } catch (retryError) {
        if (retryError.code && !retryError.uncertain) {
          state.lifecycleOperations.set(operation.room_id, { ...operation, status: 'failed', error: retryError.code });
          rememberLifecycleOperations(); return;
        }
      }
    }
    state.lifecycleOperations.set(operation.room_id, { ...operation, status: 'unknown' }); rememberLifecycleOperations();
  }
}
function applyLifecycleResult(result, expected = null) {
  const operation = result?.lifecycle_operation;
  if (!operation || operation.room_id !== result.room_id || !uuidPattern.test(operation.id)
      || !['pending', 'completed', 'failed'].includes(operation.status)
      || expected && (operation.room_id !== expected.room_id || operation.action !== expected.action
        || operation.request_id !== expected.request_id || expected.id && operation.id !== expected.id)) throw new RequestError(t("整理结果的任务身份不匹配。"));
  if (operation.action === 'remove' && operation.status === 'completed' && result.deleted !== true
      || result.deleted === true && (operation.action !== 'remove' || operation.status !== 'completed')) {
    throw new RequestError(t("永久删除结果尚未确认。"));
  }
  setConnection(true); acceptLifecycleOperation(operation);
  if (operation.status !== 'failed') parkIntervenedRequest(result.room_id);
  if (result.deleted === true && operation.status === 'completed' && operation.action === 'remove') {
    forgetDeletedRoom(result.room_id);
  } else if (result.room?.id === state.selected && Array.isArray(result.events) && Array.isArray(result.members)) applySnapshot(result);
  showNotice(lifecycleOperationText(operation), operation.status === 'failed' ? 'error' : '');
}
async function mutateRoomLifecycle(id, action) {
  if (['pending', 'unknown'].includes(state.lifecycleOperations.get(id)?.status)) return;
  clearActionError();
  const request_id = crypto.randomUUID();
  markRoomIntervention(id, request_id);
  state.lifecycleOperations.set(id, { room_id: id, action, request_id, status: 'pending', originatedHere: true });
  rememberLifecycleOperations();
  state.managementBusy = true; renderControls(); renderRooms();
  try { applyLifecycleResult(await api(`/api/rooms/${encodeURIComponent(id)}/lifecycle`, 'POST', { action, request_id }), state.lifecycleOperations.get(id)); }
  catch (error) {
    if (error.code && !error.uncertain) {
      const operation = { room_id: id, action, request_id, status: 'failed', error: error.code, originatedHere: true };
      state.lifecycleOperations.set(id, operation); rememberLifecycleOperations();
      showNotice(lifecycleOperationText(operation), 'error');
      return;
    }
    state.lifecycleOperations.set(id, { room_id: id, action, request_id, status: 'unknown', originatedHere: true }); rememberLifecycleOperations();
    await checkLifecycleOperation(state.lifecycleOperations.get(id));
    if (state.lifecycleOperations.get(id)?.status === 'unknown') showNotice(t("操作结果尚未确认，豆窝会继续核对。"), 'error');
  } finally { state.managementBusy = false; await refresh(); renderControls(); renderRooms(); }
}
function applyProjectDeletion(result, intent) {
  const receipt = result?.project_deletion;
  if (result?.project_id !== intent.project_id || receipt?.project_id !== intent.project_id
      || receipt.request_id !== intent.request_id || !['pending', 'completed'].includes(receipt.status)
      || receipt.status === 'completed' && result.deleted !== true) throw new RequestError(t("项目登记删除结果尚未确认。"), { uncertain: true });
  if (receipt.status === 'completed' && result.deleted === true) {
    state.projectDeletions.delete(intent.project_id);
    if (Array.isArray(result.projects)) acceptProjects(result);
    else acceptProjects({ projects: state.projects.filter((project) => project.id !== intent.project_id) });
    managerNotice(t("项目登记已永久删除，原项目文件和已有聊天保留。"));
  } else state.projectDeletions.set(intent.project_id, intent);
  rememberLifecycleOperations(); renderProjects();
}
async function checkProjectDeletion(intent) {
  try { applyProjectDeletion(await api(`/api/project-deletions?request_id=${encodeURIComponent(intent.request_id)}`), intent); }
  catch (error) {
    if (error.code === 'project_deletion_not_found') {
      try { applyProjectDeletion(await api(`/api/projects/${encodeURIComponent(intent.project_id)}/lifecycle`, 'POST', { action: 'remove', request_id: intent.request_id }), intent); }
      catch (retryError) { intent.error = retryError.code || null; }
    } else intent.error = error.code || null;
    rememberLifecycleOperations();
  }
}
async function removeProjectRegistration(id) {
  if (state.projectDeletions.has(id)) return;
  const intent = { project_id: id, request_id: crypto.randomUUID() };
  state.projectDeletions.set(id, intent); rememberLifecycleOperations(); renderProjects();
  try { applyProjectDeletion(await api(`/api/projects/${encodeURIComponent(id)}/lifecycle`, 'POST', { action: 'remove', request_id: intent.request_id }), intent); }
  catch (error) {
    intent.error = error.code || null;
    if (error.uncertain) await checkProjectDeletion(intent);
    if (state.projectDeletions.has(id)) managerNotice(intent.error
      ? t("项目登记删除未完成，记录仍保留。请核对本地服务返回的问题。")
      : t("项目登记删除尚未完成，记录仍保留。豆窝会继续核对。"), true);
  } finally { rememberLifecycleOperations(); renderProjects(); renderControls(); }
}
function renderLegacyRoomReview() {
  const status = $('room-lifecycle-status');
  const operations = state.roomLifecycleSupported ? [...state.lifecycleOperations.values()].filter((operation) => operation.status !== 'completed') : [];
  status.hidden = !operations.length; status.replaceChildren();
  for (const operation of operations) {
    const row = element('div', '', 'lifecycle-operation');
    row.append(element('p', `${roomTitle(state.rooms.find((room) => room.id === operation.room_id) || {})}${interfaceColon()}${lifecycleOperationText(operation)}`, 'detail-caption'));
    if (operation.status === 'unknown') {
      const check = element('button', t("核对处理结果"), 'quiet-button'); check.type = 'button';
      check.addEventListener('click', async () => { await checkLifecycleOperation(operation); renderRooms(); }); row.append(check);
    }
    status.append(row);
  }
  const container = $('legacy-room-review'); container.hidden = !state.roomLifecycleSupported;
  if (!state.roomLifecycleSupported) return;
  const removed = state.rooms.filter((room) => lifecycleState(room) === 'removed');
  container.hidden = !removed.length;
  $('legacy-room-summary').textContent = t("整理旧移除记录（{p0}）", { p0: removed.length });
  const list = $('legacy-room-list');
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), [removed, [...state.lifecycleOperations.values()], state.managementBusy, state.busy, !!state.pending]]);
  if (list.dataset.fingerprint === fingerprint) return;
  list.dataset.fingerprint = fingerprint; list.replaceChildren();
  for (const room of removed) {
    const card = element('article', '', 'manager-card');
    const read = element('button', roomTitle(room), 'quiet-button'); read.type = 'button'; read.title = room.goal || '';
    read.addEventListener('click', () => void selectRoom(room.id));
    card.append(read, lifecycleActions('rooms', room)); list.append(card);
  }
}
function pendingPartnerOperation(id) {
  return state.partnerLifecycleSupported && [...state.partnerOperations.values()].filter((operation) => operation.provider === id && ['pending', 'unknown'].includes(operation.status))
    .sort((a, b) => b.intentOrder - a.intentOrder)[0];
}
function latestPartnerOperation(id) {
  return [...state.partnerOperations.values()].filter((operation) => operation.provider === id).sort((a, b) => b.intentOrder - a.intentOrder)[0];
}
function validPartnerIntent(intent) {
  if (!intent.body || intent.body.request_id !== intent.request_id || Object.keys(intent.body).length !== 2) return false;
  return ['enable', 'disable'].includes(intent.action) ? intent.suffix === 'enabled' && intent.body.enabled === (intent.action === 'enable')
    : intent.suffix === 'lifecycle' && ['archive', 'restore', 'remove'].includes(intent.action) && intent.body.action === intent.action;
}
// One whole sentence per action: an action verb dropped into a shared sentence read "Partner configuration Disable." in English.
const partnerOperationSentences = {
  enable: { failed: "启用未完成。{p0}", unknown: "正在核对启用结果，原请求已保留。", pending: "启用尚未完成，正在保留改动并确认停止。{p0}", done: "伙伴已启用。{p0}" },
  disable: { failed: "停用未完成。{p0}", unknown: "正在核对停用结果，原请求已保留。", pending: "停用尚未完成，正在保留改动并确认停止。{p0}", done: "伙伴已停用。{p0}" },
  archive: { failed: "归档未完成。{p0}", unknown: "正在核对归档结果，原请求已保留。", pending: "归档尚未完成，正在保留改动并确认停止。{p0}", done: "伙伴已归档。{p0}" },
  restore: { failed: "恢复未完成。{p0}", unknown: "正在核对恢复结果，原请求已保留。", pending: "恢复尚未完成，正在保留改动并确认停止。{p0}", done: "伙伴已恢复。{p0}" },
  remove: { failed: "移除配置未完成。{p0}", unknown: "正在核对移除结果，原请求已保留。", pending: "移除尚未完成，正在保留改动并确认停止。{p0}", done: "伙伴配置已移除。{p0}" },
  other: { failed: "伙伴操作未完成。{p0}", unknown: "正在核对伙伴操作结果，原请求已保留。", pending: "伙伴操作尚未完成，正在保留改动并确认停止。{p0}", done: "伙伴操作已完成。{p0}" },
};
function partnerOperationSettled(operation) {
  return operation?.status === 'completed' && !operation.error && !operation.targets?.some((target) => !['done', 'superseded'].includes(target.state));
}
function partnerOperationText(operation) {
  const sentences = partnerOperationSentences[operation.action] || partnerOperationSentences.other;
  if (operation.status === 'failed') return t(sentences.failed, { p0: errorLabels[operation.error] || executionMessage(operation.error) });
  if (operation.status === 'unknown') return t(sentences.unknown);
  const error = operation.error ? errorLabels[operation.error] || executionMessage(operation.error) : '';
  if (operation.status !== 'completed') return t(sentences.pending, { p0: error ? ` ${error}` : '' });
  const waiting = operation.targets?.some((target) => !['done', 'superseded'].includes(target.state));
  return t(sentences.done, { p0: waiting ? t("已有工作的改动已保留，接手情况见原聊天。") : t("已有聊天和成果保留。") });
}
function acceptPartnerOperation(operation, expected = null, context = null) {
  if (!operation || !uuidPattern.test(operation.id) || !uuidPattern.test(operation.request_id) || !agentPattern.test(operation.provider)
      || !['enable', 'disable', 'archive', 'restore', 'remove'].includes(operation.action) || !['pending', 'completed'].includes(operation.status)
      || !['stopping', 'retaining', 'configuration', 'completed'].includes(operation.stage) || !Array.isArray(operation.targets)
      || operation.targets.some((target) => !uuidPattern.test(target.room_id) || !['pending', 'retained', 'waiting_receiver', 'inspecting', 'inspected', 'done', 'superseded', 'blocked'].includes(target.state))) return false;
  if (expected && (expected.request_id !== operation.request_id || expected.provider !== operation.provider || expected.action !== operation.action)) return false;
  const previous = state.partnerOperations.get(operation.request_id);
  if (previous && (previous.status === 'completed' && operation.status !== 'completed'
      || context && context.generation < (previous.observedGeneration || 0))) return true;
  state.partnerOperations.set(operation.request_id, { ...previous, ...operation,
    intentOrder: previous?.intentOrder || ++state.partnerOperationSequence, observedGeneration: context?.generation || previous?.observedGeneration || 0 });
  rememberLifecycleOperations(); return true;
}
async function applyPartnerOperationResult(result, intent) {
  if (result?.partner_lifecycle_operation?.action === 'remove' && result.partner_lifecycle_operation.status === 'completed' && result.deleted !== true) throw new RequestError(t("伙伴配置移除结果尚未确认。"), { uncertain: true });
  if (!acceptPartnerOperation(result?.partner_lifecycle_operation, intent, state.partnerResponseContexts.get(result))) throw new RequestError(t("伙伴操作结果尚未确认。"), { uncertain: true });
  acceptPartners(result);
  if (latestPartnerOperation(intent.provider)?.request_id === intent.request_id) managerNotice(partnerOperationText(state.partnerOperations.get(intent.request_id)), !!state.partnerOperations.get(intent.request_id).error);
}
async function checkPartnerOperation(intent) {
  if (!state.partnerLifecycleSupported || state.scopeChanged || state.partnerOperationChecks.has(intent.request_id)) return;
  state.partnerOperationChecks.add(intent.request_id);
  try {
    let result;
    try { result = await api(`/api/partners/lifecycle-operations/${encodeURIComponent(intent.request_id)}`); }
    catch (error) {
      if (error.code !== 'partner_lifecycle_operation_not_found' || !validPartnerIntent(intent)) throw error;
      result = await api(`/api/partners/${encodeURIComponent(intent.provider)}/${intent.suffix}`, 'POST', intent.body);
    }
    await applyPartnerOperationResult(result, intent);
  } catch (error) {
    if (error.code && !error.uncertain && error.code !== 'partner_lifecycle_operation_not_found') { intent.error = error.code; if (intent.status === 'unknown') intent.status = 'failed'; }
    managerNotice(error.message || t("伙伴操作结果尚未确认，已有记录保留。"), true);
  } finally { state.partnerOperationChecks.delete(intent.request_id); rememberLifecycleOperations(); renderGlobalPartners(); renderControls(); }
}
async function refreshPartnerOperations() {
  if (!state.partnerLifecycleSupported || state.scopeChanged) return;
  for (const operation of [...state.partnerOperations.values()]) {
    if (['pending', 'unknown'].includes(operation.status) || operation.targets?.some((target) => !['done', 'superseded'].includes(target.state))) await checkPartnerOperation(operation);
  }
}
async function mutatePartnerOperation(provider, action) {
  if (!state.partnerLifecycleSupported || !state.connected || state.scopeChanged || pendingPartnerOperation(provider)) return;
  ++state.partnerIntentEpoch;
  const requestId = crypto.randomUUID(); const enabledAction = ['enable', 'disable'].includes(action);
  const intent = { provider, action, request_id: requestId, status: 'unknown', intentOrder: ++state.partnerOperationSequence, suffix: enabledAction ? 'enabled' : 'lifecycle',
    body: enabledAction ? { enabled: action === 'enable', request_id: requestId } : { action, request_id: requestId } };
  state.partnerOperations.set(requestId, intent); rememberLifecycleOperations(); renderGlobalPartners(); renderControls();
  try { await applyPartnerOperationResult(await api(`/api/partners/${encodeURIComponent(provider)}/${intent.suffix}`, 'POST', intent.body), intent); }
  catch (error) {
    if (error.uncertain) await checkPartnerOperation(intent);
    else { state.partnerOperations.delete(requestId); managerNotice(error.message, true); }
  } finally { rememberLifecycleOperations(); renderGlobalPartners(); renderControls(); }
}

function confirmLifecycle(kind, item) {
  state.lifecycleConfirm = { kind, id: item.id, previousFocus: document.activeElement };
  const removing = kind === 'rooms' ? roomDisplayTitle(item) : item.name;
  $('lifecycle-confirm-title').textContent = kind === 'projects' ? t("移除项目“{p0}”？", { p0: removing }) : kind === 'partners' ? t("移除伙伴“{p0}”？", { p0: removing }) : t("移除任务“{p0}”？", { p0: removing });
  $('lifecycle-confirm-description').textContent = kind === 'projects'
    ? t("只移除豆窝里的项目登记。磁盘上的项目文件和已有聊天都会保留。")
    : kind === 'partners' ? t("只移除豆窝里的伙伴配置。不会卸载工具、删除 Claude 或 Codex 的原生会话；已有聊天会保留。")
      : t("只把这份任务记录移到“已移除”。聊天、交付文件和原生会话会保留。");
  const permanent = kind === 'rooms' && state.roomLifecycleSupported || kind === 'projects' && state.projectDeletionSupported || kind === 'partners' && state.partnerLifecycleSupported;
  if (permanent && kind === 'projects') {
    $('lifecycle-confirm-title').textContent = t("永久删除项目登记“{p0}”？", { p0: item.name });
    $('lifecycle-confirm-description').textContent = t("只永久删除豆窝里的项目登记，无法恢复。磁盘上的原项目文件和已有聊天中的项目副本都会保留。");
  } else if (permanent && kind === 'partners') {
    $('lifecycle-confirm-title').textContent = t("移除伙伴配置“{p0}”？", { p0: item.name });
    $('lifecycle-confirm-description').textContent = t("移除豆窝中的这个伙伴配置；已有聊天和成果保留，正在进行的工作会先停下并保住改动。");
  } else if (permanent) {
    $('lifecycle-confirm-title').textContent = t("永久删除任务“{p0}”？", { p0: roomDisplayTitle(item) });
    $('lifecycle-confirm-description').textContent = state.roomNativeSessionCleanupSupported
      ? t("将停止这项任务，永久删除豆窝聊天、成果、专用工作副本和专用本机会话，无法恢复。其它聊天、原项目文件和账号不受影响；供应商云端记录不在清理范围。")
      : t("将停止这项任务，永久删除豆窝中的聊天、交付文件和专用工作副本，无法恢复。原项目文件、原终端和模型供应商保存的会话不会删除。");
  }
  $('lifecycle-confirm-note').textContent = permanent && kind === 'partners' ? t("原工具、登录账号和已有聊天不会删除。不会启动模型。") : permanent && kind === 'projects' ? t("只整理项目登记，不会停止已有任务或启动模型。") : permanent ? t("正在运行也可以删除；豆窝会先停止执行并完成清理。不会启动模型。") : t("之后可以在“已移除”中恢复。此操作不会启动模型。");
  $('lifecycle-confirm-submit').textContent = permanent ? kind === 'partners' ? t("移除配置") : t("永久删除") : t("移除登记");
  $('lifecycle-confirm').hidden = false; syncModalInert();
  $('lifecycle-confirm-cancel').focus({ preventScroll: true });
}
function closeLifecycleConfirm() {
  const previousFocus = state.lifecycleConfirm?.previousFocus;
  state.lifecycleConfirm = null; $('lifecycle-confirm').hidden = true; syncModalInert();
  previousFocus?.focus({ preventScroll: true });
}
async function mutateLifecycle(kind, id, action) {
  if (state.managementBusy) return;
  if (kind === 'partners' && state.partnerLifecycleSupported) { await mutatePartnerOperation(id, action); return; }
  if (kind === 'rooms' && state.roomLifecycleSupported) { await mutateRoomLifecycle(id, action); return; }
  if (kind === 'projects' && action === 'remove' && state.projectDeletionSupported) { await removeProjectRegistration(id); return; }
  if (state.busy || state.pending) return;
  state.managementBusy = true; renderControls(); renderProjects(); renderGlobalPartners(); renderRooms();
  try {
    const result = await api(`/api/${kind}/${encodeURIComponent(id)}/lifecycle`, 'POST', { action });
    if (kind === 'projects') acceptProjects(result);
    else if (kind === 'partners') acceptPartners(result);
    else if (result?.room?.id === state.selected) applySnapshot(result);
    await refresh();
    const message = action === 'restore' ? t("已恢复，可以继续使用。") : action === 'archive' ? t("已归档，可在“已归档”中恢复。已有聊天保留。") : t("已移除登记，可在“已移除”中恢复。");
    if (kind === 'rooms') showNotice(message); else managerNotice(message);
  } catch (error) {
    const message = error.uncertain ? t("操作结果尚未确认，请刷新列表核对；不会自动重试。") : error.message;
    await refresh().catch(() => {});
    if (kind === 'rooms') showNotice(message, 'error'); else managerNotice(message, true);
  } finally {
    state.managementBusy = false; renderControls(); renderProjects(); renderGlobalPartners(); renderRooms();
  }
}
document.addEventListener('keydown', (event) => {
  if (!state.lifecycleConfirm) return;
  if (event.key === 'Escape') { event.preventDefault(); closeLifecycleConfirm(); }
  else if (event.key === 'Tab') {
    event.preventDefault();
    const cancel = $('lifecycle-confirm-cancel'); const submit = $('lifecycle-confirm-submit');
    (document.activeElement === cancel ? submit : cancel).focus({ preventScroll: true });
  }
});
$('lifecycle-confirm-cancel').addEventListener('click', closeLifecycleConfirm);
$('lifecycle-confirm-submit').addEventListener('click', () => {
  const operation = state.lifecycleConfirm; if (!operation) return;
  closeLifecycleConfirm(); void mutateLifecycle(operation.kind, operation.id, 'remove');
});
for (const kind of ['projects', 'partners', 'rooms']) $(`${kind}-filter`).addEventListener('change', () => {
  closeContextMenu();
  state.lifecycleViews[kind] = ['active', 'archived', 'removed'].includes($(`${kind}-filter`).value) ? $(`${kind}-filter`).value : 'active';
  if (kind === 'projects') renderProjects(); else if (kind === 'partners') renderGlobalPartners(); else renderRooms();
  if (kind === 'rooms') $('rooms').scrollTop = 0; else $(`${kind}-view`).scrollTop = 0;
});





function setHistoryExpanded(expanded) {
  $('rooms').classList.toggle('history-expanded', expanded);
  $('toggle-history').setAttribute('aria-expanded', String(expanded));
  $('toggle-history').textContent = `${expanded ? t("收起记录") : t("任务记录")} · ${visibleEntries('rooms', state.rooms).length}`;
}

function roomWorkActivity(room) {
  return ['working', 'queued', 'idle'].includes(room?.work_activity?.state) ? room.work_activity : null;
}
function updateRoomWorkIndicator(room, card) {
  const status = card?.lastChild?.firstChild; const indicator = status?.firstChild;
  if (!indicator) return;
  const activity = roomWorkActivity(room);
  const working = state.connected && activeEntry(room) && activity?.state === 'working';
  indicator.hidden = !working; indicator.classList.toggle('is-working', working);
  const label = status.lastChild.textContent;
  const accessible = working ? `${label}${interfaceComma()}${activity.draining ? t("正在结束当前处理") : t("正在工作")}` : label;
  if (status.getAttribute('aria-label') !== accessible) status.setAttribute('aria-label', accessible);
}
function renderRooms() {
  renderLegacyRoomReview();
  renderLifecycleFilter('rooms', state.rooms);
  const visibleRooms = visibleEntries('rooms', state.rooms);
  $('room-count').textContent = String(visibleRooms.length);
  setHistoryExpanded($('toggle-history').getAttribute('aria-expanded') === 'true');
  $('room-list-empty').hidden = visibleRooms.length > 0;
  $('room-list-empty').textContent = state.lifecycleViews.rooms === 'active' ? t("还没有任务。创建后可以在这里继续。") : t("这里没有{p0}的任务。", { p0: lifecycleLabels[state.lifecycleViews.rooms] });
  const remaining = new Set();
  if (state.contextMenu?.kind === 'rooms') {
    const item = currentContextItem('rooms', state.contextMenu.id);
    if (contextItemFingerprint(item) !== state.contextMenu.itemFingerprint) closeContextMenu({ restoreFocus: true });
  }
  const rooms = [...visibleRooms].sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
  const grouped = state.lifecycleViews.rooms === 'active';
  $('rooms').classList.toggle('grouped', grouped);
  const groups = new Map(); const firstInGroup = new Set();
  for (const room of rooms) {
    const group = roomGroup(room);
    if (!groups.has(group)) { groups.set(group, 0); firstInGroup.add(room.id); }
    groups.set(group, groups.get(group) + 1);
  }
  for (const [position, room] of rooms.entries()) {
    remaining.add(room.id);
    let card = state.roomNodes.get(room.id);
    if (!card) {
      card = element('button', '', 'room-card');
      card.type = 'button';
      const status = element('span', '', 'room-card-status');
      const indicator = element('span', '', 'room-work-spinner'); indicator.hidden = true; indicator.setAttribute('aria-hidden', 'true');
      status.append(indicator, element('span', '', 'room-card-status-text'));
      const meta = element('div', '', 'room-card-meta'); meta.append(status, element('time'));
      card.append(element('div', '', 'room-card-title'), meta);
      card.addEventListener('click', () => selectRoom(room.id));
      bindContextTarget(card, 'rooms', room.id);
      state.roomNodes.set(room.id, card);
    }
    card.classList.toggle('selected', room.id === state.selected);
    card.setAttribute('aria-current', room.id === state.selected ? 'page' : 'false');
    const group = roomGroup(room);
    card.dataset.group = group;
    card.dataset.groupFirst = String(grouped && firstInGroup.has(room.id) && Object.hasOwn(roomGroupLabels, group));
    card.dataset.groupLabel = Object.hasOwn(roomGroupLabels, group) ? `${roomGroupLabels[group]} · ${groups.get(group)}` : '';
    card.disabled = state.busy && !canDetachPendingRequest();
    card.firstChild.textContent = roomTitle(room);
    card.title = typeof room.objective_goal === 'string' && room.objective_goal.trim() ? room.objective_goal : typeof room.goal === 'string' ? room.goal : t("目标");
    const objectiveStatus = state.executionPolicySupported && Object.hasOwn(objectiveRoomLabels, room.objective_status) ? sidebarObjectiveLabel(room) : null;
    const activity = roomWorkActivity(room);
    const status = !activeEntry(room) ? lifecycleLabels[lifecycleState(room)] : activity?.draining && activity.state === 'working' ? t("正在暂停")
      : activity?.state === 'queued' ? t("等待开始") : objectiveStatus || (Object.hasOwn(outcomeLabels, room.outcome_state) ? outcomeLabels[room.outcome_state] : statusLabels[statusClass(room.status)]);
    const statusText = card.lastChild.firstChild.lastChild;
    if (statusText.textContent !== status) statusText.textContent = status;
    const time = timeLabel(room.updated_at || room.created_at, true);
    if (card.lastChild.lastChild.textContent !== time) card.lastChild.lastChild.textContent = time;
    updateRoomWorkIndicator(room, card);
    if ($('rooms').children[position] !== card) $('rooms').insertBefore(card, $('rooms').children[position] || null);
  }
  for (const [id, card] of state.roomNodes) {
    if (!remaining.has(id)) {
      if (state.contextMenu?.kind === 'rooms' && state.contextMenu.id === id) closeContextMenu({ restoreFocus: true });
      card.remove(); state.roomNodes.delete(id);
    }
  }
}


const taskStatusLabels = localizedLabels({
  open: '待认领', assigned: '已分配，等待开始', working: '正在处理', waiting_peer: '等待伙伴',
  review: '等伙伴检查', accepted: '已完成 · 伙伴看过', self_checked: '已完成 · 只做了自查', blocked: '遇到阻塞', unknown: '结果待确认',
  interrupted: '已中断', cancelled: '已取消', withdrawn: '已撤掉',
});
const objectiveLabels = localizedLabels({
  running: '正在推进目标', checkpoint: '正在安排下一步', awaiting_continue: '这一阶段已交付', needs_user: '需要你回复',
  complete: '本次工作已完成', paused: '已暂停推进', stopped: '目标推进已停止', interrupted: '推进中断，结果待确认',
});
const objectiveRoomLabels = localizedLabels({
  running: '目标推进中', checkpoint: '核对下一步', awaiting_continue: '阶段已交付', needs_user: '等你回复',
  complete: '目标已交付', paused: '已暂停推进', stopped: '目标已停止', interrupted: '推进中断，待核对',
});
function sidebarObjectiveLabel(room) {
  const open = state.snapshot?.room.id === room.id ? state.snapshot : null;
  if (open && idleReadOnlyDiscussion(open)) return readOnlyLabel(open);
  // A finished read-only discussion is "讨论已结束" in the list too, as on its card and partner cards.
  if (room.objective_status === 'complete' && (open ? isReadOnlyDiscussion(open) : room.work_mode === 'plan')) return open ? readOnlyLabel(open, true) : t("讨论已结束");
  const stop = roomStopKind(room);
  if (stop) return stop === 'quota_wait' ? t("等额度恢复") : t("马上自动继续");
  if (room.objective_status !== 'paused') return objectiveRoomLabels[room.objective_status];
  const activity = roomWorkActivity(room);
  if (activity) return activity.state === 'working' ? t("正在暂停") : t("已暂停");
  const snapshot = state.snapshot?.room.id === room.id ? state.snapshot : null;
  const pauseRequested = snapshot?.objective?.pause_requested ?? room.objective_pause_requested;
  const inflight = snapshot?.jobs?.some((job) => ['working', 'queued'].includes(job.status))
    || snapshot?.native_executions?.some((execution) => execution.status === 'working')
    || snapshot?.executions?.some((execution) => execution.status === 'working');
  if (pauseRequested === true || inflight) return t("正在暂停");
  return pauseRequested === false ? t("已暂停") : t("暂停");
}
function isReadOnlyDiscussion(snapshot = state.snapshot, job = null) {
  if (!state.naturalRuntimeSupported || !snapshot?.room) return false;
  const claim = jobClaim(snapshot, job);
  const mode = claim?.work_mode || snapshot.room.work_mode;
  const runtime = claim?.runtime_settings || snapshot.room.runtime_settings;
  return mode === 'plan' || ['read_only', 'plan'].includes(runtime?.effective_permission);
}
// Build mode under a read-only permission (Plan in a development room) is not a plan
// discussion: the work mode is build, only the permission keeps partners from editing.
function readOnlyBuild(snapshot = state.snapshot) {
  return isReadOnlyDiscussion(snapshot) && actualWorkMode(snapshot) === 'build';
}
function writablePermissionName(snapshot = state.snapshot) {
  return snapshot?.room?.mode === 'native_project' ? 'Full Access' : t("副本内修改");
}
// What starts the work after a plan. The "按方案开始" button exists only for a room without an
// objective; with one (autonomous, the default), the user switches to build and sends a message.
function planNextStepText() {
  const room = state.snapshot && state.selected === state.snapshot.room.id ? state.snapshot.room : null;
  if (room ? !currentObjective() : !state.executionPolicySupported) return t("聊清楚后，可以点“按方案开始”。");
  const native = room ? room.mode === 'native_project' : state.mode === 'native_project';
  const planPermission = (state.runtimeSettings?.permission || room?.runtime_settings?.permission) === 'plan';
  return [t("聊清楚后，切到“动手”再发一句话就会开工。"),
    native && planPermission ? t("开发任务还要在“设置”里把权限从 Plan 换成 Full Access，不然伙伴只能看不能改。") : ''].filter(Boolean).join(interfaceWordSpace());
}
function readOnlyLabel(snapshot = state.snapshot, ended = false) {
  return readOnlyBuild(snapshot) ? ended ? t("已回复 · 只读权限") : t("只读权限") : ended ? t("讨论已结束") : t("方案讨论");
}
function idleReadOnlyDiscussion(snapshot = state.snapshot) {
  if (!isReadOnlyDiscussion(snapshot) || !activeEntry(snapshot.room)) return false;
  const objective = currentObjective(snapshot);
  if (objective && !['running', 'checkpoint'].includes(objective.status)) return false;
  if (['unknown', 'stopped', 'needs_user'].includes(terminalTeamState(snapshot) || snapshot.collaboration?.outcome?.state)) return false;
  return !(snapshot.jobs || []).some((job) => ['working', 'queued'].includes(job.status))
    && !(snapshot.native_executions || []).some((execution) => execution.status === 'working')
    && !(snapshot.executions || []).some((execution) => execution.status === 'working');
}
function actualWorkMode(snapshot = state.snapshot) {
  if (!state.naturalRuntimeSupported) return snapshot?.current_run?.work_mode || snapshot?.room?.work_mode;
  const job = snapshot?.jobs?.find((entry) => entry.status === 'working');
  const claim = jobClaim(snapshot, job);
  return claim?.work_mode || snapshot?.room?.work_mode;
}
function hostResultInterruption(objective) { return objective?.status === 'interrupted' && objective.interruption_kind === 'host_result_validation'; }
const hostResultInterruptionText = "结果提交未通过格式校验，已有记录保留。继续后由伙伴重新提交并核对。";
function hostResultInterruptionMessage(snapshot) {
  const outcome = snapshot?.collaboration?.outcome;
  const reason = boundPlatformDisplayText(outcome?.reason, outcome?.reason_i18n);
  const action = boundPlatformDisplayText(outcome?.next_action, outcome?.next_action_i18n);
  return reason && action ? [reason, action].join(interfaceWordSpace()) : t(hostResultInterruptionText);
}
function objectiveLabel(objective) {
  // A stop the service resumes by itself is titled like the card at the top, never as a plain stop.
  const resume = state.snapshot && Object.hasOwn(state.snapshot, 'stop_kind') ? state.snapshot.stop_kind : null;
  if (['stopped', 'interrupted', 'needs_user'].includes(objective.status) && resume === 'quota_wait') return t("等额度恢复");
  if (['stopped', 'interrupted', 'needs_user'].includes(objective.status) && resume === 'auto') return t("马上自动继续");
  if (hostResultInterruption(objective)) return t("处理遇到技术问题"); if (idleReadOnlyDiscussion()) return readOnlyLabel(); if (objective.status === 'complete' && isReadOnlyDiscussion()) return readOnlyLabel(state.snapshot, true); return objective.status === 'paused' && objective.pause_requested === true ? t("正在暂停，等待当前处理结束") : objectiveLabels[objective.status]; }
function currentObjective(snapshot = state.snapshot) {
  const objective = snapshot?.objective;
  return state.executionPolicySupported && objective && uuidPattern.test(objective.id)
    && Object.hasOwn(objectiveLabels, objective.status) && snapshot.current_run?.objective_id === objective.id ? objective : null;
}
function nextStepBinding(snapshot = state.snapshot) {
  const objective = currentObjective(snapshot); const next = objective?.next_step;
  if (objective?.status !== 'awaiting_continue' || next?.decision !== 'continue' || next.scope !== 'current_goal' || next.can_start !== true) return null;
  const binding = runBinding({ id: next.binding?.run_id, revision: next.binding?.revision, digest: next.binding?.digest });
  return binding && JSON.stringify(binding) === JSON.stringify(runBinding(snapshot?.current_run)) ? binding : null;
}
// New hosts own continuation decisions; the client sends only the bound user message.
function naturalContinuation(snapshot = state.snapshot, target = state.target) {
  if (!state.naturalContinuationSupported || !snapshot?.team || !snapshot.current_run) return null;
  const value = snapshot.continuation; const raw = value?.binding;
  const binding = raw && runBinding({ id: raw.run_id, revision: raw.revision, digest: raw.digest });
  const current = runBinding(snapshot.current_run);
  const valid = binding && current && Object.keys(raw).length === 3
    && JSON.stringify(binding) === JSON.stringify(current);
  const recipient = target !== 'all' && target !== 'both' ? value?.recipients?.[target] : null;
  const eligibility = typeof recipient?.can_send === 'boolean' ? recipient : value;
  return { canSend: eligibility?.can_send === true && !!valid, reason: valid ? eligibility?.reason : 'native_recovery_binding_changed', binding: valid ? binding : null };
}
function capturePendingIntent(request, snapshot = state.snapshot) {
  const source = request.roomId && snapshot?.room?.id === request.roomId && pagedRoom(snapshot) ? snapshot.through_seq : null;
  return { ...request, sourceSeq: Number.isSafeInteger(source) && source >= 0 ? source : null };
}
async function submitNaturalContinuation(text, snapshot = state.snapshot) {
  if (state.busy || state.managementBusy || state.pending || !state.connected || !state.selected
      || snapshot?.room.id !== state.selected || !activeEntry(state.snapshot?.room) || typeof text !== 'string'
      || !text.trim() || !checkUserText(text)) return;
  const current = naturalContinuation(state.snapshot); const captured = naturalContinuation(snapshot);
  if (recipientProblem()) { showActionError(recipientProblem()); return; }
  if (!current?.canSend || !captured?.canSend || JSON.stringify(current.binding) !== JSON.stringify(captured.binding)) return;
  const changes = pendingRuntimeChanges();
  if (Object.keys(changes).length && (naturalRuntimeConfiguration()?.can_edit !== true || runtimeSubmissionError())) {
    showActionError(runtimeSubmissionError() || t("当前不能修改设置，草稿已保留。"), state.selected, 'message'); return;
  }
  state.pending = capturePendingIntent({ kind: 'message', roomId: state.selected, body: { text: text.trim(), target: state.target,
    request_id: crypto.randomUUID(), intent: 'continue', binding: { ...current.binding }, ...changes } });
  await submitPending();
}
// Continue uses the host's current capability and binding, never a guessed quota state.
function continuationAction(snapshot = state.snapshot) {
  if (!snapshot || !activeEntry(snapshot.room) || !runBinding(snapshot.current_run)) return null;
  const natural = naturalContinuation(snapshot);
  if (natural) return natural.canSend ? 'natural' : null;
  const run = snapshot.current_run; const objective = currentObjective(snapshot);
  if (state.nativeProjectSupported && snapshot.room.mode === 'native_project' && run.can_recover_native === true) return 'native_recovery';
  if (state.nativeProjectSupported && snapshot.room.mode === 'project_folder' && run.can_native_handoff === true) return 'native_handoff';
  if (state.continuousObjectiveSupported && run.can_restart_objective === true) return 'restart';
  if (nextStepBinding(snapshot)) return 'continue';
  if (objective?.status === 'paused' && objective.pause_requested !== true) return 'resume';
  return null;
}
// The English interface tells people to type continue, so both languages count as the bare command.
function isContinueCommand(text) {
  const value = text.trim();
  return /^(?:请)?继续(?:工作|推进|吧)?[。！!\s]*$/u.test(value)
    || /^(?:please\s+)?(?:continue|keep going|resume)(?:\s+(?:working|please))?[.!\s]*$/iu.test(value);
}
async function continueCurrentWork(snapshot = state.snapshot, text = '') {
  const action = continuationAction(state.snapshot);
  if (!action || snapshot?.room.id !== state.selected || JSON.stringify(runBinding(snapshot.current_run)) !== JSON.stringify(runBinding(state.snapshot.current_run))) return;
  if (action === 'native_recovery') await submitNativeRecovery(snapshot, text);
  else await submitObjectiveAction(action, snapshot);
}
function objectiveBudgetText(objective) {
  const budget = objective?.budget;
  const count = (value) => Number.isSafeInteger(value) && value >= 0 ? String(value) : t("未提供");
  return t("已开始 {p0} 次 · 排队 {p1} 次", { p0: count(budget?.used), p1: count(budget?.queued) });
}
async function submitNativeRecovery(snapshot, text = '') {
  if (naturalContinuation(state.snapshot)) { await submitNaturalContinuation(text || t("继续"), snapshot); return; }
  if (!state.nativeProjectSupported || state.busy || state.managementBusy || state.pending || !state.connected
      || snapshot?.room.id !== state.selected || snapshot.room.mode !== 'native_project' || !activeEntry(state.snapshot?.room)
      || state.snapshot?.current_run?.can_recover_native !== true) return;
  const binding = runBinding(snapshot.current_run);
  if (!binding || JSON.stringify(binding) !== JSON.stringify(runBinding(state.snapshot.current_run))) return;
  if (typeof text !== 'string' || !checkUserText(text)) return;
  const body = { request_id: crypto.randomUUID(), ...binding };
  if (text.trim() && !isContinueCommand(text)) body.user_message = text.trim();
  state.pending = capturePendingIntent({ kind: 'native_recovery', roomId: state.selected, body });
  await submitPending();
}
function canReassessObjective(snapshot = state.snapshot) {
  return state.executionPolicySupported && !state.busy && !state.managementBusy && !state.pending && state.connected
    && snapshot?.room.id === state.selected && activeEntry(snapshot.room) && !lifecycleBusy('rooms', snapshot.room)
    && snapshot.current_run?.can_reassess_objective === true && !!currentObjective(snapshot) && !!runBinding(snapshot.current_run);
}
async function submitObjectiveReassessment(snapshot) {
  if (!canReassessObjective(state.snapshot)) return;
  const binding = runBinding(snapshot?.current_run);
  if (snapshot?.room.id !== state.selected || !binding || JSON.stringify(binding) !== JSON.stringify(runBinding(state.snapshot.current_run))) return;
  state.pending = capturePendingIntent({ kind: 'objective_reassessment', roomId: state.selected, objectiveId: currentObjective(snapshot).id,
    body: { request_id: crypto.randomUUID(), ...binding } });
  await submitPending();
}
async function submitObjectiveAction(kind, snapshot = state.snapshot, text = '') {
  if (naturalContinuation(state.snapshot) && ['restart', 'continue', 'answer', 'resume'].includes(kind)) {
    await submitNaturalContinuation(text || t("继续"), snapshot); return;
  }
  if (!state.executionPolicySupported || state.busy || state.managementBusy || state.pending || !state.connected
      || !state.selected || !activeEntry(state.snapshot?.room) || snapshot?.room.id !== state.selected) return;
  if (['objective', 'suggested', 'restart', 'native_handoff'].includes(kind) && (policySubmissionError() || runtimeSubmissionError())) { showNotice(policySubmissionError() || runtimeSubmissionError(), 'error'); return; }
  const objective = currentObjective(snapshot); let body;
  if (kind === 'native_handoff') {
    const binding = runBinding(snapshot.current_run);
    if (!state.nativeProjectSupported || !binding || snapshot.current_run?.can_native_handoff !== true) return;
    body = { request_id: crypto.randomUUID(), ...binding, execution_policy: executionPolicyPayload() || defaultExecutionPolicy(), runtime_settings: runtimePayload() || copyRuntimeSettings(state.runtimeSettings) };
    body.runtime_settings.members = Object.fromEntries(state.participantIds.map((id) => [id, body.runtime_settings.members[id] || { model: null, effort: null }]));
  } else if (kind === 'restart') {
    const binding = runBinding(snapshot.current_run);
    if (!state.continuousObjectiveSupported || !binding || snapshot.current_run?.can_restart_objective !== true) return;
    body = { request_id: crypto.randomUUID(), ...binding, execution_policy: executionPolicyPayload() || defaultExecutionPolicy() };
  } else if (kind === 'answer') {
    const binding = runBinding(snapshot.current_run);
    if (!objective || objective.can_answer !== true || objective.status !== 'needs_user' || !binding || typeof text !== 'string' || !text.trim() || !checkUserText(text)) return;
    body = { request_id: crypto.randomUUID(), objective_id: objective.id, ...binding, text: text.trim() };
  } else if (kind === 'objective') {
    const binding = runBinding(snapshot.current_run);
    if (objective || !binding || snapshot.current_run?.can_start_objective !== true) return;
    body = { request_id: crypto.randomUUID(), ...binding, execution_policy: executionPolicyPayload() || defaultExecutionPolicy() };
  } else if (kind === 'suggested') {
    const next = objective?.next_step; const binding = runBinding(snapshot.current_run);
    if (!binding || snapshot.current_run.can_start_suggested_goal !== true || next?.scope !== 'new_goal' || typeof next.goal !== 'string' || !next.goal.trim() || !checkUserText(next.goal)) return;
    body = { request_id: crypto.randomUUID(), ...binding, text: next.goal, target: state.target, retry: false, work_mode: state.workMode,
      execution_policy: executionPolicyPayload() || defaultExecutionPolicy() };
    const runtime = runtimePayload(); if (runtime) body.runtime_settings = runtime;
  } else if (kind === 'continue') {
    const binding = nextStepBinding(snapshot); if (!binding) return;
    body = { request_id: crypto.randomUUID(), ...binding };
  } else {
    const holdsWait = ['auto', 'quota_wait'].includes(stopKind(snapshot, objective?.stop_reason));
    if (!objective || (kind === 'pause' ? !['running', 'checkpoint'].includes(objective.status) && !holdsWait : kind !== 'resume' || objective.status !== 'paused' || objective.pause_requested === true)) return;
    body = { request_id: crypto.randomUUID(), objective_id: objective.id };
  }
  state.pending = capturePendingIntent({ kind, roomId: state.selected, body }); await submitPending();
}
function boundObjectiveNextStep(snapshot) {
  const next = currentObjective(snapshot)?.next_step;
  const binding = runBinding({ id: next?.binding?.run_id, revision: next?.binding?.revision, digest: next?.binding?.digest });
  return binding && JSON.stringify(binding) === JSON.stringify(runBinding(snapshot.current_run)) ? next : null;
}
function completionSuggestion(snapshot = state.snapshot) {
  const next = boundObjectiveNextStep(snapshot);
  return currentObjective(snapshot)?.status === 'complete' && typeof next?.goal === 'string' && next.goal.trim() && validUserText(next.goal) ? next.goal : null;
}
const completionAdviceQuestion = '请基于本轮已完成内容和未覆盖项，建议现在最值得做的一步并说明理由；不要重复已完成工作。';
function fillCompletionDraft(snapshot, text) {
  if (state.snapshot?.room.id !== snapshot.room.id || currentObjective(state.snapshot)?.status !== 'complete'
      || !boundObjectiveNextStep(snapshot) || !boundObjectiveNextStep(state.snapshot)
      || JSON.stringify(runBinding(state.snapshot.current_run)) !== JSON.stringify(runBinding(snapshot.current_run))
      || $('message').disabled) return;
  const draft = $('message').value;
  const alreadyIncluded = draft.trim() === text.trim() || draft.endsWith(`\n\n${text}`);
  const combined = alreadyIncluded ? draft : draft.trim() ? `${draft}\n\n${text}` : text;
  if (!checkUserText(combined)) return;
  $('message').value = combined; saveDraft(); renderControls(); $('message').focus();
}
function useCompletionSuggestion(snapshot, goal) {
  if (completionSuggestion(state.snapshot) !== goal || completionSuggestion(snapshot) !== goal) return;
  fillCompletionDraft(snapshot, goal);
}
function requestCompletionAdvice(snapshot) {
  if (completionSuggestion(snapshot) || completionSuggestion(state.snapshot)) return;
  fillCompletionDraft(snapshot, t(completionAdviceQuestion));
}
function completionReportSource(snapshot) {
  const next = boundObjectiveNextStep(snapshot), objective = currentObjective(snapshot);
  if (!next) return '';
  const digest = objective.checkpoint_proposal?.proposal_digest;
  const source = typeof digest === 'string' && [...snapshot.events].reverse().find((event) => {
    const binding = runBinding({ id: event.binding?.run_id, revision: event.binding?.revision, digest: event.binding?.digest });
    return event.kind === 'team_command' && event.command === 'checkpoint' && event.objective_id === objective.id
      && event.proposal_digest === digest && event.run_id === next.binding.run_id
      && binding && JSON.stringify(binding) === JSON.stringify(runBinding(snapshot.current_run))
      && snapshot.members.some((member) => member.id === event.sender);
  });
  return source ? t("本轮完成评估原文 · {p0}{p1}", { p0: displayName(source.sender), p1: Number.isFinite(source.created_at) ? ` · ${timeLabel(source.created_at, true)}` : '' })
    : t("本轮完成评估原文");
}
function renderCurrentTaskQuestion(result, snapshot, objective, expanded) {
  const next = boundObjectiveNextStep(snapshot);
  if (objective?.status !== 'needs_user' || next?.decision !== 'needs_user'
      || typeof next.question !== 'string' || !next.question.trim()) return;
  const inChat = next.origin !== 'task_block' && snapshot.events.some(event => event.run_id === snapshot.current_run.id
    && event.objective_id === objective.id && checkpointQuestionEvent(event, snapshot).checkpoint_question === true
    && checkpointQuestionEvent(event, snapshot).text === next.question);
  if (inChat) return;
  result.hidden = false; result.className = 'collaboration-card outcome-card outcome-needs_user';
  result.append(element('h2', t("当前需要处理")));
  for (const task of snapshot.team?.tasks || []) {
    if (next.origin === 'task_block' && taskNeedsUser(task)) {
      result.append(element('p', t("{p0}：{p1}", { p0: taskDisplayTitle(task.title || task.id), p1: blockedTaskReason(task) }), 'detail-caption'));
    }
  }
  const replanned = next.origin === 'task_block' ? (snapshot.team?.tasks || []).filter(taskAutoBlocked).length : 0;
  if (replanned) result.append(element('p', t("有 {p0} 项需要重新安排，点“继续推进”后会自动处理。", { p0: replanned }), 'detail-caption'));
  // Task blocks are joined from stored reason codes; keep that literal record folded.
  const codedQuestion = next.origin === 'task_block' && /[：:]\s*[a-z][a-z0-9_]{3,}\s*(?:[；;\n]|$)/m.test(next.question);
  if (codedQuestion && next.question.length <= 400) {
    const details = element('details', '', 'tech-details');
    details.append(element('summary', t("技术细节（原始记录）")), element('p', next.question, 'outcome-question'));
    result.append(details);
  } else if (next.question.length > 400) {
    result.append(element('p', `${(next.question.split('\n').find(line => line.trim()) || next.question).slice(0, 160)}…`, 'outcome-question'));
    const details = element('details', '', 'current-task-question');
    details.dataset.outcomeDetail = 'current-task-question'; details.open = expanded.get('current-task-question') || false;
    details.append(element('summary', t("查看完整问题")), element('p', next.question, 'outcome-question'));
    result.append(details);
  } else result.append(element('p', next.question, 'outcome-question'));
  if (objective.can_answer === true) result.append(element('p', t("请在下方回复；继续时沿用当前权限设置。"), 'detail-caption'));
}

function renderObjectiveNextStep(result, snapshot, objective, summaryTarget = result, questionShown = null, foldQuestion = false) {
  const recorded = objective.next_step;
  const binding = runBinding({ id: recorded?.binding?.run_id, revision: recorded?.binding?.revision, digest: recorded?.binding?.digest });
  const next = binding && JSON.stringify(binding) === JSON.stringify(runBinding(snapshot.current_run)) ? recorded : null;
  const summaryLabel = foldQuestion ? t("此前记录") : objective.status === 'complete' ? t("完成情况") : t("下一步");
  const technical = questionShown || foldQuestion ? summaryTarget : result;
  if (typeof next?.summary === 'string' && next.summary && next.summary.trim() !== questionShown?.trim()
      && ![...summaryTarget.children].some((node) => node.textContent.trim() === next.summary.trim())) summaryTarget.append(element('p', `${summaryLabel}${interfaceColon()}${next.summary}`, 'outcome-next'));
  if (typeof next?.question === 'string' && next.question && next.question.trim() !== questionShown?.trim()
      && !(foldQuestion && [...summaryTarget.children].some((node) => node.textContent.trim() === next.question.trim()))) (foldQuestion ? summaryTarget : result).append(element('p', next.question, 'outcome-question'));
  if (objective.can_answer === true && !questionShown && !foldQuestion) result.append(element('p', t("在下方回答后继续同一目标，仍使用现有目标和权限；不会新建目标。"), 'detail-caption'));
  if (next?.decision === 'continue' && (next.scope === 'new_goal' || !nextStepBinding(snapshot) && objective.status === 'awaiting_continue')) {
    let message = t("下一步的范围或版本尚未确认，当前不会自动开始。");
    if (next.scope === 'new_goal') message = naturalContinuation(snapshot)
      ? t("这个建议超出当前目标。你可以在这里明确后续要求，保留这段聊天。")
      : t("这个建议超出当前目标。请用“新任务”明确安排，不会自动开工。");
    technical.append(element('p', message, 'detail-caption'));
  } else if (!next && objective.status === 'awaiting_continue') result.append(element('p', recorded
    ? t("下一步的范围或版本尚未确认，当前不会自动开始。") : t("还没有明确的下一步，请补充要求；不会猜测后直接开工。"), 'detail-caption'));
  if (typeof objective.stop_reason === 'string' && objective.stop_reason) {
    const explanation = stopReasonMessage(objective.stop_reason);
    if (explanation.trim() !== questionShown?.trim() && ![...technical.children].some((node) => node.textContent.trim() === explanation.trim())) {
      technical.append(element('p', explanation, 'detail-caption'));
    }
  }
  const tasks = snapshot.team?.tasks || [];
  const selfChecked = tasks.filter((task) => task.status === 'self_checked' || task.review_kind === 'self_check');
  if (objective.status === 'complete' && (next?.review_kind === 'self_check' || selfChecked.length)) {
    result.append(element('p', selfChecked.length ? t("{p0} 项只做了自查，没有别的伙伴看过。", { p0: selfChecked.length })
      : t("本次完成评估由单个伙伴核对；成果的审核范围见文件与任务记录。"), 'review-scope-note'));
    const details = element('details', '', 'review-scope-details'); details.dataset.outcomeDetail = 'review-scope';
    details.append(element('summary', t("查看审核详情")));
    if (next?.review_kind === 'self_check') details.append(element('p', t("完成评估由单个伙伴核对，不代表所有成果都未经互审。"), 'detail-caption'));
    for (const task of selfChecked) details.append(element('p', t("{p0}：只做了自查，没有别的伙伴看过。", { p0: task.title || task.id }), 'detail-caption'));
    result.append(details);
  } else {
    if (next?.review_kind === 'self_check') result.append(element('p', t("本次完成评估由单个伙伴核对；各项成果的审核范围见文件与任务记录。"), 'review-scope-note'));
    if (selfChecked.length) result.append(element('p', t("{p0} 项只做了自查，没有别的伙伴看过：{p1}。", { p0: selfChecked.length, p1: interfaceList(selfChecked.map((task) => task.title || task.id)) }), 'review-scope-note'));
  }
}

function renderObjectiveTakeovers(result, snapshot, objective) {
  const retired = Object.entries(objective.retired_partners || {}).filter(([id, info]) => agentPattern.test(id) && info?.cleanup_confirmed === true);
  const runIds = new Set([snapshot.current_run?.id, ...(snapshot.runs || []).filter((run) => run.objective_id === objective.id).map((run) => run.id)]);
  const events = snapshot.events.filter((event) => event.kind === 'task_taken_over' && runIds.has(event.run_id)
    && agentPattern.test(event.previous_owner) && agentPattern.test(event.owner) && event.previous_owner !== event.owner);
  if (!retired.length && !events.length) return;
  const box = element('details', '', 'objective-takeovers');
  const latest = events.at(-1);
  box.append(element('summary', latest ? t("{p0} 已接手伙伴任务 · 查看分工变化", { p0: displayName(latest.owner) }) : t("有伙伴因额度受限停下 · 查看安排")));
  for (const [id, info] of retired) box.append(element('p', t("{p0}：{p1}本次执行已确认清理；仍保留在任务成员中。", { p0: displayName(id), p1: quotaBlockCodes.has(info.reason) ? t("额度已用完，本目标不再安排它。") : executionMessage(info.reason) }), 'detail-caption'));
  for (const event of events.slice(-16)) {
    const task = event.run_id === snapshot.current_run?.id ? snapshot.team?.tasks?.find((entry) => entry.id === event.task_id) : null;
    box.append(element('p', t("{p0} 接手 {p1} 的{p2}。", { p0: displayName(event.owner), p1: displayName(event.previous_owner), p2: task?.title ? interfaceQuote(taskDisplayTitle(task.title)) : t("子任务") }), 'detail-caption'));
  }
  box.append(element('p', t("接手的伙伴沿用这个目标定好的权限和自己的模型设置；一个人做完的会注明只做了自查。"), 'detail-caption'));
  result.append(box);
}
const outcomeLabels = localizedLabels({
  in_progress: '协作进行中', needs_user: '需要你回复', ready_for_user: '已交付，待你验收',
  accepted: '本轮已接受', stopped: '协作已停止', unknown: '结果待确认', round_finished: '本轮已回复，目标未验收',
});

function legacyCollaboration(snapshot) {
  const closed = closedStatuses.has(snapshot.room.status);
  const finished = snapshot.room.status === 'completed';
  const warning = [...snapshot.events].reverse().find((event) => event.kind === 'warning' && typeof event.code === 'string');
  const failed = [...snapshot.jobs].reverse().find((job) => typeof job.error === 'string' && job.error);
  const reason = closed ? (warning ? stoppedReasonMessage(snapshot, warning.code) : failed ? executionMessage(failed.error) : t("记录状态：{p0}。", { p0: statusLabels[statusClass(snapshot.room.status)] }))
    : finished ? t("这是历史协作记录。本轮回复已结束；没有记录整个目标的验收结果。") : t("这是旧版协作记录，尚未记录可追踪的任务拆分。");
  const mode = snapshot.room.mode;
  return {
    mode: 'legacy', summary: '', tasks: [],
    outcome: { state: closed ? 'stopped' : finished ? 'round_finished' : snapshot.room.status === 'needs_user' ? 'needs_user' : 'in_progress',
      reason, next_action: finished ? t("请阅读双方回复与交付内容，再决定是否验收或补充要求。") : closed ? t("请先核对停止原因与已保存内容，再决定后续工作。") : t("查看讨论与伙伴状态；需要补充时可在下方发送。"),
      validation: { scope: ['project_files', 'project_folder'].includes(mode) ? 'static_review' : 'not_run', revision: null,
        summary: ['project_files', 'project_folder'].includes(mode) ? t("此{p0}任务模式支持隔离改稿与静态互审，未运行项目或测试。{p1}请按消息核对实际检查范围。改稿保存在豆窝，原项目未改动。", { p0: snapshot.members.length === 1 ? '' : t("旧"), p1: Number.isSafeInteger(snapshot.project?.revision) ? t("已保存文件版本 {p0}，不代表该版本已完成检查。", { p0: snapshot.project.revision }) : '' })
          : mode === 'python_sandbox' ? t("隔离运行的具体版本与退出结果见执行记录；这些记录不等于目标已验收。") : t("没有记录执行验证；伙伴的文字答复不等于目标已验收。") }, artifacts: [], remaining: [] },
  };
}

function currentQuotaFailure(snapshot) {
  const failure = snapshot?.current_run?.native_quota_failure;
  return failure?.reason === 'provider_quota_exhausted'
    && typeof failure.source_job_id === 'string'
    && snapshot.members.some((member) => member.agent === failure.provider || member.id === failure.provider)
    ? failure : null;
}
function currentQuotaHandoff(snapshot, failure = currentQuotaFailure(snapshot)) {
  const handoff = snapshot?.current_run?.native_quota_handoff;
  return failure && handoff?.source_job_id === failure.source_job_id
    && ['pending', 'in_progress', 'blocked'].includes(handoff.state) ? handoff : null;
}
function quotaFailureDescription(failure, snapshot) {
  const availability = availabilityInfo(failure.provider).value;
  const reset = Number.isFinite(availability?.resets_at)
    ? t("预计重置 {p0}，届时仍需确认。", { p0: timeLabel(availability.resets_at, true) }) : t("恢复时间未提供。");
  const handoff = currentQuotaHandoff(snapshot, failure);
  if (handoff) {
    const progress = handoff.state === 'blocked'
      ? t("自动接手暂未完成：{p0}{p1}", { p0: errorLabels[handoff.reason] || executionMessage(handoff.reason), p1: naturalContinuation(snapshot)?.canSend ? t("可以补充要求，消息会保留；不会因此重试受阻的调用。") : '' })
      : t("正在自动核对保留的改动，并安排可用伙伴接手，无需点击继续。{p0}", { p0: naturalContinuation(snapshot)?.canSend ? t("可以在下方补充要求。") : '' });
    return t("{p0} 的订阅额度已用完。{p1}已有成果和改动保留。{p2}", { p0: displayName(failure.provider), p1: reset, p2: progress });
  }
  const receiver = snapshot.current_run.native_recovery_provider;
  const knownReceiver = snapshot.members.some((member) => member.agent === receiver || member.id === receiver);
  const next = snapshot.current_run.can_recover_native === true
    ? knownReceiver ? t("点击“继续”，由 {p0} 接着做。", { p0: displayName(receiver) }) : t("点击“继续”接着完成原任务。")
    : t("暂不能接续，请先查看下方的具体原因。");
  return t("{p0} 的订阅额度已用完。{p1}已有成果和改动保留。{p2}", { p0: displayName(failure.provider), p1: reset, p2: next });
}
// Translate only source-bound platform descriptors; opaque model/user parts stay literal.
function boundPlatformDisplayText(value, descriptor) {
  if (typeof value !== 'string' || !Array.isArray(descriptor?.parts)) return null;
  if (!descriptor.parts.every((part) => part && (typeof part.key === 'string' || typeof part.text === 'string'))) return null;
  const source = descriptor.parts.map((part) => typeof part.key === 'string'
    ? part.key.replace(/\{(p\d+)\}/g, (token, name) => Object.hasOwn(part.params || {}, name) ? String(part.params[name]) : token)
    : part.text).join('');
  // A later projection may replace a field while retaining older metadata.
  if (source !== value) return null;
  return descriptor.parts.map((part) => typeof part.key === 'string' ? t(part.key, part.params || {}) : part.text).join('');
}
function platformDisplayText(value, descriptor) {
  return boundPlatformDisplayText(value, descriptor) ?? value;
}
function localizedCollaboration(projection) {
  const outcome = projection.outcome;
  const display = (object, field) => platformDisplayText(object[field], object[`${field}_i18n`]);
  return { ...projection, summary: display(projection, 'summary'),
    tasks: (projection.tasks || []).map((task) => ({ ...task, waiting_for: display(task, 'waiting_for') })),
    outcome: { ...outcome, reason: display(outcome, 'reason'), next_action: display(outcome, 'next_action'),
      validation: outcome.validation ? { ...outcome.validation, summary: display(outcome.validation, 'summary') } : outcome.validation,
      remaining: (outcome.remaining || []).map((value, index) => platformDisplayText(value, outcome.remaining_i18n?.[index])) } };
}

function renderCollaboration(snapshot) {
  const plan = $('collaboration-plan');
  const result = $('collaboration-outcome');
  if (!snapshot) {
    plan.hidden = true; result.hidden = true; $('materials-outcomes').hidden = true; $('materials-outcomes').replaceChildren();
    plan.replaceChildren(); result.replaceChildren();
    plan.dataset.room = '';
    state.collaborationFingerprint = '';
    return false;
  }
  const projection = snapshot.collaboration;
  const collaboration = projection && ['team', 'legacy'].includes(projection.mode) && projection.outcome
    ? localizedCollaboration(projection) : legacyCollaboration(snapshot);
  const runScope = `${snapshot.room.id}:${snapshot.current_run?.id || ''}`;
  if (state.deliveryScope !== runScope) { state.deliveryScope = runScope; state.deliveryLimit = 256; }
  const objective = currentObjective(snapshot);
  const natural = naturalContinuation(snapshot);
  const quotaFailure = currentQuotaFailure(snapshot);
  const discussionIdle = idleReadOnlyDiscussion(snapshot);
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), [snapshot.room.id, lifecycleState(snapshot.room), collaboration, snapshot.current_run, objective, state.runtimeSettings,
    state.executionPolicySupported, state.continuousObjectiveSupported, state.naturalContinuationSupported, state.nativeProjectSupported, snapshot.continuation, snapshot.native_workspaces, snapshot.native_handoff, snapshot.project?.revision,
    snapshot.project?.files?.map((file) => [file.path, file.sha256, typeof file.content === 'string']), snapshot.team?.tasks?.map((task) => [task.id, task.status, task.reviewer, task.result_files, task.block_cause]),
    snapshot.room.work_mode, snapshot.room.runtime_settings, discussionIdle, quotaFailure ? partnerAvailability(quotaFailure.provider) : null, snapshot.events.filter((event) => event.kind === 'task_taken_over'), state.forkSource?.roomId, state.deliveryLimit]]);
  if (fingerprint === state.collaborationFingerprint) return false;
  state.collaborationFingerprint = fingerprint;
  const keepExpanded = plan.dataset.room === runScope && Boolean(plan.firstChild?.open);
  const preservedDetails = new Map(result.dataset.room === runScope ? [...result.children].filter((node) => node.dataset.outcomeDetail).map((node) => [node.dataset.outcomeDetail, Boolean(node.open)]) : []);
  const deliveryExpanded = result.dataset.room === runScope
    && Boolean([...result.children].find((node) => node.classList.contains('delivery-details'))?.open);
  plan.dataset.room = runScope;
  result.dataset.room = runScope;
  plan.replaceChildren(); result.replaceChildren();
  const materials = $('materials-outcomes');
  const oldHistory = materials.firstChild;
  const historyOpen = materials.dataset.room === runScope && Boolean(oldHistory?.open);
  materials.dataset.room = runScope; materials.replaceChildren(); materials.hidden = true;
  const tasks = Array.isArray(collaboration.tasks) ? collaboration.tasks : [];
  plan.hidden = collaboration.mode !== 'team';
  if (!plan.hidden) {
    const board = element('details', '', 'collaboration-board');
    board.open = keepExpanded;
    const accepted = tasks.filter((task) => task?.status === 'accepted' && task.review_kind !== 'self_check').length;
    const selfChecked = tasks.filter((task) => task?.status === 'self_checked' || task.status === 'accepted' && task.review_kind === 'self_check').length;
    const counted = tasks.filter((task) => !taskWithdrawn(task)).length;
    board.append(element('summary', tasks.length ? selfChecked ? t("任务分工 · {p0}/{p1} 已完成 · {p2} 项只做了自查", { p0: accepted + selfChecked, p1: counted, p2: selfChecked }) : t("任务分工 · {p0}/{p1} 已完成，伙伴看过", { p0: accepted, p1: counted }) : t("任务分工 · 正在规划")));
    if (typeof collaboration.summary === 'string' && collaboration.summary) board.append(element('p', collaboration.summary, 'collaboration-summary'));
    if (!tasks.length) board.append(element('p', t("正在整理分工，伙伴随后可认领任务。"), 'detail-caption'));
    const list = element('ol', '', 'collaboration-tasks');
    for (const task of tasks) {
      if (!task || typeof task.title !== 'string') continue;
      const item = element('li', '', 'collaboration-task');
      const heading = element('div', '', 'collaboration-task-heading');
      const taskLabel = Object.hasOwn(taskStatusLabels, task.status) ? taskStatusLabels[task.status] : t("状态待确认");
      const title = element('strong', taskDisplayTitle(task.title)); title.title = task.title;
      heading.append(title, element('span', task.review_kind === 'self_check'
        ? ['accepted', 'self_checked'].includes(task.status) ? taskStatusLabels.self_checked : t("{p0} · 完成后只做自查", { p0: taskLabel }) : taskLabel, 'task-state'));
      item.append(heading, element('p', task.owner ? t("负责人：{p0}", { p0: displayName(task.owner) }) : t("负责人：等待伙伴认领"), 'task-owner'));
      const reviewTask = task.status === 'review' ? snapshot.team?.tasks?.find((entry) => entry.id === task.id && entry.status === 'review') : null;
      if (typeof reviewTask?.reviewer === 'string' && agentPattern.test(reviewTask.reviewer)) {
        item.append(element('p', t("本次审核：{p0}", { p0: displayName(reviewTask.reviewer) }), 'task-reviewer'));
      }
      if (typeof task.waiting_for === 'string' && task.waiting_for) item.append(element('p', t("正在等待：{p0}", { p0: task.waiting_for }), 'task-waiting'));
      if (typeof task.result === 'string' && task.result) item.append(element('p', `${task.result}${task.result_truncated ? t("…（节选，完整原文可在成果中阅读）") : ''}`, 'task-result'));
      list.append(item);
    }
    board.append(list);
    plan.append(board);
  }
  const outcome = collaboration.outcome;
  const outcomeState = terminalTeamState(snapshot) || (Object.hasOwn(outcomeLabels, outcome.state) ? outcome.state : 'unknown');
  result.hidden = false;
  result.className = `collaboration-card outcome-card outcome-${outcomeState}`;
  const progressing = !discussionIdle && snapshot.current_run?.can_start_objective !== true
    && !['needs_user', 'stopped', 'unknown'].includes(outcomeState)
    && (objective ? ['running', 'checkpoint'].includes(objective.status) : outcomeState === 'in_progress');
  const waitingForReply = !hostResultInterruption(objective) && (objective?.status === 'needs_user' || outcomeState === 'needs_user');
  if (progressing || waitingForReply && objective && objective.next_step?.question) {
    const documents = deliveryDocuments(snapshot);
    const question = objective?.next_step?.question || outcome.reason;
    const recorded = waitingForReply ? [...new Set([outcome.reason, objective?.next_step?.summary, outcome.validation?.summary]
      .filter((text) => typeof text === 'string' && text.trim() && text.trim() !== question?.trim()))] : [];
    result.hidden = !documents.length && !recorded.length && !(waitingForReply && exportableProject(snapshot));
    materials.hidden = result.hidden; result.hidden = true;
    if (!materials.hidden) {
      const key = waitingForReply ? 'waiting-history' : 'running-history';
      const history = element('details', '', waitingForReply ? 'waiting-delivery-history' : 'running-delivery-history');
      history.dataset.outcomeDetail = key; history.open = historyOpen;
      history.append(element('summary', `${waitingForReply ? t("此前成果与记录") : t("此前成果")}${documents.length ? t(" · {p0} 份", { p0: documents.length }) : ''}`));
      const files = element('div', '', 'delivery-file-list');
      for (const doc of documents) files.append(readerLink(doc));
      history.append(files);
      for (const text of recorded) history.append(element('p', text, 'detail-caption'));
      if (waitingForReply && exportableProject(snapshot)) {
        const download = element('button', t("导出 ZIP"), 'quiet-button'); download.type = 'button'; download.id = 'download-delivery';
        download.addEventListener('click', () => void downloadProject()); history.append(download);
      }
      materials.append(history);
    }
    if (waitingForReply) renderCurrentTaskQuestion(result, snapshot, objective, preservedDetails);
    renderNativeWorkspaceNotes(result, snapshot);
    if (result.children.length) result.hidden = false;
    if (quotaFailure) {
      result.hidden = false;
      result.append(element('p', quotaFailureDescription(quotaFailure, snapshot), 'current-quota-failure'));
    }
    return true;
  }
  renderNativeWorkspaceNotes(result, snapshot);
  const singlePartner = collaboration.mode === 'legacy' && snapshot.members.length === 1;
  const singlePartnerReasons = { in_progress: t("伙伴正在直接处理你的目标，回复与进展见对话。"),
    round_finished: t("本轮回复已结束，请阅读回复及交付内容。"), needs_user: t("请查看伙伴的回复，补充它需要的信息。") };
  const resumeKind = Object.hasOwn(snapshot, 'stop_kind') && ['auto', 'quota_wait'].includes(snapshot.stop_kind) ? snapshot.stop_kind : null;
  // The stop line under the title already says why and what happens next; the generic stop text would contradict it.
  const reason = discussionIdle ? readOnlyBuild(snapshot) ? t("现在是只读权限，伙伴只能看不能改。要改文件，在“设置”里把权限换成 {p0}。", { p0: writablePermissionName(snapshot) })
    : t("现在没在做事。可以接着讨论，或切到“动手”开工。已有成果和改动都在。") : resumeKind ? resumeKind === 'auto' ? t("几秒后自动继续，不用你操作。") : ''
    : singlePartner && Object.hasOwn(singlePartnerReasons, outcomeState) ? singlePartnerReasons[outcomeState] : outcome.reason;
  const accepted = Number.isFinite(snapshot.current_run?.accepted_at);
  const technicalInterruption = hostResultInterruption(objective);
  const completed = !discussionIdle && (objective ? objective.status === 'complete'
    : ['ready_for_user', 'accepted', 'round_finished'].includes(outcomeState));
  const foldedResult = completed || technicalInterruption;
  const report = foldedResult ? element('details', '', technicalInterruption ? 'technical-interruption-history' : 'completion-report') : result;
  if (foldedResult) { const key = technicalInterruption ? 'technical-history' : 'report'; report.dataset.outcomeDetail = key; report.open = preservedDetails.get(key) || false; report.append(element('summary', technicalInterruption ? t("此前问题与记录") : t("查看完整总结"))); }
  result.append(element('h2', discussionIdle ? readOnlyLabel(snapshot) : objective ? objective.status === 'complete' ? t("本轮已交付") : objectiveLabel(objective)
    : accepted && ['accepted', 'ready_for_user'].includes(outcomeState) ? t("本轮已接受") : singlePartner && outcomeState === 'round_finished' ? t("本轮已回复") : outcomeLabels[outcomeState]));
  if (collaboration.mode === 'legacy') result.append(element('p', singlePartner ? t("单伙伴任务") : t("历史记录 · 旧版协作流程"), 'collaboration-context'));
  if (technicalInterruption) result.append(element('p', hostResultInterruptionMessage(snapshot), 'outcome-reason'));
  if (typeof reason === 'string' && reason) (foldedResult ? report : result).append(element('p', reason, 'outcome-reason'));
  if (objective && !discussionIdle && !completed) renderObjectiveNextStep(result, snapshot, objective, report, null, technicalInterruption);
  const suggestion = completed ? completionSuggestion(snapshot) : null;
  if (completed) {
    const boundNext = objective && boundObjectiveNextStep(snapshot);
    if (objective) result.append(element('p', t("本轮已结束，无需回复或确认；有新要求直接在下方发送。"), 'completion-guidance'));
    const next = element('section', '', 'completion-next'); next.append(element('h3', t("接下来")));
    next.append(element('p', suggestion || (!objective ? t("需要调整时，直接在下方补充要求；已有交付与聊天保留。") : boundNext
      ? t("模型本轮未提出新的执行目标。需要进一步安排时，可以先请伙伴建议下一步。")
      : t("当前完成记录的版本尚未确认，请刷新后核对下一步。")), suggestion ? 'completion-suggestion' : 'detail-caption'));
    if (boundNext && !suggestion) {
      const ask = element('button', t("请伙伴建议下一步"), 'quiet-button'); ask.type = 'button'; ask.id = 'ask-completion-advice';
      ask.addEventListener('click', () => requestCompletionAdvice(snapshot));
      next.append(ask, element('p', t("只加入输入框；你可修改后自行发送。"), 'detail-caption'));
    }
    const replies = deliveryDocuments(snapshot).filter((doc) => !doc.file && !doc.historical && doc.path.startsWith('task:'));
    if (replies.length) {
      next.append(element('h4', t("本轮分工与答复")), element('p', t("以下为阶段记录，其中的后续安排可能已在本轮完成，不代表新的执行目标。"), 'detail-caption'));
      const links = element('div', '', 'delivery-file-list completion-reply-links');
      for (const doc of replies) links.append(readerLink(doc));
      next.append(links);
    }
    result.append(next);
    if (typeof boundNext?.summary === 'string' && boundNext.summary.trim()) {
      result.append(element('p', completionReportSource(snapshot), 'detail-caption completion-report-source'),
        element('p', boundNext.summary, 'completion-summary-preview'));
    }
    if (objective) renderObjectiveNextStep(result, snapshot, objective, report);
    const reviewDetails = [...result.children].find((node) => node.dataset.outcomeDetail === 'review-scope');
    if (reviewDetails) reviewDetails.open = preservedDetails.get('review-scope') || false;
  }
  if (objective) renderObjectiveTakeovers(result, snapshot, objective);
  if (!objective && outcomeState === 'ready_for_user' && !accepted) {
    result.append(element('p', t("本轮已交付，正在等你的下一项要求。是否达到你的目标仍由你验收。"), 'detail-caption'));
    if (!natural && (snapshot.current_run?.work_mode || snapshot.room.work_mode || 'legacy') === 'legacy') result.append(element('p', snapshot.current_run?.can_continue === true ? t("这是历史任务，排查报告不会自动变成开发任务。要继续，请在下方写明接下来要做什么，再点“开始下一轮”。") : t("这是历史任务，排查报告不会自动变成开发任务。要继续开发，请用当前项目开新任务，并写明后续要求。"), 'detail-caption'));
  }
  if (accepted) result.append(element('p', t("你已于 {p0} 接受本轮交付。", { p0: timeLabel(snapshot.current_run.accepted_at, true) }), 'detail-caption'));
  const documents = deliveryDocuments(snapshot);
  const currentResults = (snapshot.team?.tasks || []).flatMap((task) => Array.isArray(task.result_files) ? task.result_files : []);
  // The project snapshot and outcome inventory persist across rounds. Only a
  // current task's exact file binding proves delivery; planning never writes.
  const savedOnly = (doc) => !doc.historical && doc.file && (objective ? !objectiveDeliveredFile(snapshot, doc) : snapshot.current_run && (snapshot.current_run.work_mode === 'plan'
    || !currentResults.some((file) => file.path === doc.path && file.sha256 === doc.sha256)));
  const savedDocuments = documents.filter(savedOnly);
  const inheritedDocuments = documents.filter((doc) => doc.historical);
  const currentDocuments = documents.filter((doc) => !doc.historical && !savedOnly(doc));
  if (objective?.delivery?.missing_or_changed?.length) result.append(element('p', t("部分目标成果与已交付版本不一致，未列为可核对的交付。请查看已保存文件与任务记录。"), 'detail-caption'));
  if (objective?.baseline?.files?.length && objective.baseline.verified !== true) result.append(element('p', t("上一轮交付的绑定尚未确认，暂不展示为沿用成果。"), 'detail-caption'));
  let remainingDocuments = state.deliveryLimit; let shownDocuments = 0;
  for (const [entries, group] of [[currentDocuments, 'current'], [inheritedDocuments, 'baseline'], [savedDocuments, 'saved']]) {
    if (!entries.length) continue;
    const historical = group !== 'current';
    const delivery = element('details', '', historical ? `delivery-content ${group === 'baseline' ? 'baseline-file-history' : 'saved-file-history'}` : 'delivery-content');
    const key = technicalInterruption ? `technical-${group}` : group;
    delivery.dataset.outcomeDetail = key; delivery.open = preservedDetails.get(key) || false;
    delivery.append(element('summary', group === 'baseline' ? t("沿用的交付（上一轮）") : group === 'saved' ? t("已保存文件（未列入本轮交付）")
      : t("{p0} · {p1} 份", { p0: objective ? t("本目标成果与答复") : snapshot.current_run?.work_mode === 'plan' ? t("方案与答复") : t("交付内容"), p1: entries.length })),
    element('p', group === 'baseline' ? t("保留上一轮的确切版本，单独阅读；不计为这个目标新完成的修改。") : group === 'saved'
      ? t("这些文件仍可阅读，不代表{p0}完成了修改或交付。", { p0: objective ? t("本目标") : t("本轮") }) : objective ? t("汇总这个目标各阶段的已核对成果。点击直接阅读，无需下载。") : t("点击直接阅读，无需下载。"), 'detail-caption'));
    const files = element('div', '', 'delivery-file-list');
    const visible = entries.slice(0, remainingDocuments);
    remainingDocuments -= visible.length; shownDocuments += visible.length;
    for (const doc of visible) files.append(readerLink(doc));
    delivery.append(files); result.append(delivery);
  }
  if (documents.length > shownDocuments) {
    const more = element('button', t("再显示一批 · 还有 {p0} 份", { p0: documents.length - shownDocuments }), 'quiet-button'); more.type = 'button'; more.id = 'more-delivery-files';
    more.addEventListener('click', () => { state.deliveryLimit += 256; renderCollaboration(state.snapshot); renderControls(); });
    result.append(more);
  }
  if (!completed && quotaFailure) result.append(element('p', quotaFailureDescription(quotaFailure, snapshot), 'current-quota-failure'));
  const actions = element('div', '', 'delivery-actions');
  const nativeHandoff = !natural && state.nativeProjectSupported && snapshot.current_run?.can_native_handoff === true;
  const nativeRecoveryOffered = !natural && state.nativeProjectSupported && snapshot.room.mode === 'native_project' && !!runBinding(snapshot.current_run)
    && typeof snapshot.current_run.can_recover_native === 'boolean'
    && (snapshot.current_run.can_recover_native || outcomeState === 'unknown' || snapshot.current_run.status === 'unknown' || snapshot.room.status === 'unknown');
  const nativeHandoffOffered = !natural && state.nativeProjectSupported && snapshot.room.mode === 'project_folder' && !!runBinding(snapshot.current_run)
    && typeof snapshot.current_run?.can_native_handoff === 'boolean'
    && (terminalTeamState(snapshot) === 'stopped' || ['stopped', 'interrupted'].includes(objective?.status))
    && (nativeHandoff || snapshot.current_run?.can_restart_objective !== true);
  const continuation = continuationAction(snapshot);
  const showRecovery = nativeRecoveryOffered && (!continuation || continuation === 'native_recovery');
  const showHandoff = nativeHandoffOffered && (!continuation || continuation === 'native_handoff');
  if (completed && suggestion) {
    const add = element('button', t("加入输入框"), 'quiet-button'); add.type = 'button'; add.id = 'add-completion-suggestion';
    add.addEventListener('click', () => useCompletionSuggestion(snapshot, suggestion)); actions.append(add);
  } else if (!completed && !currentQuotaHandoff(snapshot, quotaFailure) && natural && (terminalTeamState(snapshot) || ['stopped', 'interrupted', 'paused', 'awaiting_continue'].includes(objective?.status))) {
    const next = element('button', t("继续"), 'quiet-button primary-action'); next.type = 'button'; next.id = 'continue-current';
    next.disabled = !natural.canSend; next.addEventListener('click', () => void submitNaturalContinuation(t("继续"), snapshot));
    actions.append(next);
    if (!natural.canSend) actions.append(element('span', errorLabels[natural.reason] || executionMessage(natural.reason), 'detail-caption'));
  } else if (!completed && continuation === 'continue') {
    const next = element('button', t("继续"), 'quiet-button primary-action'); next.type = 'button'; next.id = 'continue-objective';
    next.addEventListener('click', () => void submitObjectiveAction('continue', snapshot)); actions.append(next);
  } else if (!objective && state.executionPolicySupported && runBinding(snapshot.current_run) && snapshot.current_run?.can_start_objective === true
      && !natural && !continuation && !showRecovery && !showHandoff) {
    const arrange = element('button', t("继续"), 'quiet-button primary-action'); arrange.type = 'button'; arrange.id = 'start-objective';
    arrange.addEventListener('click', () => void submitObjectiveAction('objective', snapshot));
    arrange.disabled = snapshot.current_run.can_start_objective !== true;
    const policy = executionPolicyPayload() || defaultExecutionPolicy();
    actions.append(arrange, element('span', snapshot.current_run.can_start_objective === true ? t("接着完成原目标，保留已有成果，不重复执行已交付的任务。")
      : executionMessage(snapshot.current_run.objective_start_reason || 'objective_unavailable'), 'detail-caption'));
  }
  if (!completed && !natural && objective?.next_step?.scope === 'new_goal' && snapshot.current_run?.can_start_suggested_goal === true
      && typeof objective.next_step.goal === 'string' && objective.next_step.goal.trim() && validUserText(objective.next_step.goal)) {
    const start = element('button', t("开始建议任务（新目标）"), 'quiet-button primary-action'); start.type = 'button'; start.id = 'start-suggested-goal';
    start.addEventListener('click', () => void submitObjectiveAction('suggested', snapshot));
    const policy = executionPolicyPayload() || defaultExecutionPolicy();
    actions.append(start, element('span', t("开始新的目标，原目标记录保留。"), 'detail-caption'));
  }
  if (showRecovery) {
    const recover = element('button', t("继续"), 'quiet-button primary-action'); recover.type = 'button'; recover.id = 'recover-native';
    recover.disabled = snapshot.current_run.can_recover_native !== true;
    recover.addEventListener('click', () => void submitNativeRecovery(snapshot));
    const reason = snapshot.current_run.native_recovery_reason || 'native_recovery_unavailable';
    const receiver = snapshot.current_run.native_recovery_provider;
    const receiverNote = snapshot.members.some((member) => member.agent === receiver || member.id === receiver) ? t("由 {p0} 接着做。", { p0: displayName(receiver) }) : '';
    const recoveryDescription = receiverNote + (snapshot.current_run.native_recovery_kind === 'partial_work'
      ? t("接着完成原任务。伙伴会先核对保留的改动，再接着做；已有成果、权限和记录保持，不重复执行上次调用。")
      : t("接着完成原任务，沿用原目标、分工和权限，保留已有成果和记录；先核对现场，不重复执行上次调用。"));
    actions.append(recover, element('span', recover.disabled ? errorLabels[reason] || executionMessage(reason)
      : receiverNote || t("接着完成原任务。"), 'detail-caption'));
    if (!recover.disabled) {
      const details = element('details', '', 'continuation-details');
      details.append(element('summary', t("续作说明")), element('p', recoveryDescription, 'detail-caption')); actions.append(details);
    }
  }
  if (showHandoff) {
    const handoff = element('button', t("继续"), 'quiet-button primary-action'); handoff.type = 'button'; handoff.id = 'native-handoff';
    handoff.disabled = !nativeHandoff; handoff.addEventListener('click', () => void submitObjectiveAction('native_handoff', snapshot));
    const reason = snapshot.current_run.native_handoff_reason || 'native_handoff_unavailable';
    actions.append(handoff, element('span', nativeHandoff
      ? t("以完整开发接着做 · {p0}。承接原目标、成员与未完成分工，保留旧记录；使用新工作目录和新会话，按所选原生权限执行。", { p0: runtimePermissionName(copyRuntimeSettings(state.runtimeSettings).permission) })
      : errorLabels[reason] || executionMessage(reason), 'detail-caption'));
  }
  if (!natural && state.continuousObjectiveSupported && !showHandoff && !showRecovery && runBinding(snapshot.current_run) && typeof snapshot.current_run?.can_restart_objective === 'boolean'
      && (['stopped', 'interrupted'].includes(objective?.status) || terminalTeamState(snapshot) === 'stopped' || closedStatuses.has(snapshot.room.status))) {
    const restart = element('button', t("继续"), 'quiet-button primary-action'); restart.type = 'button'; restart.id = 'restart-objective';
    restart.disabled = snapshot.current_run.can_restart_objective !== true;
    restart.addEventListener('click', () => void submitObjectiveAction('restart', snapshot));
    const policy = executionPolicyPayload() || defaultExecutionPolicy();
    const reason = snapshot.current_run.restart_objective_reason || 'objective_restart_unavailable';
    actions.append(restart, element('span', restart.disabled ? errorLabels[reason] || executionMessage(reason)
      : t("接着完成原目标，已有成果和记录保留，不重复执行上次调用。"), 'detail-caption'));
  }
  const planCanContinue = snapshot.current_run ? snapshot.current_run.work_mode === 'plan'
    && runBinding(snapshot.current_run) && snapshot.current_run.can_continue === true
    : snapshot.room.work_mode === 'plan' && snapshot.room.status === 'completed'
      && !snapshot.jobs.some((job) => ['queued', 'working'].includes(job.status));
  if (!objective && planCanContinue && (snapshot.current_run && snapshot.room.mode !== 'text' || state.taskModes.includes('text_artifacts'))) {
    const fileContainer = !!snapshot.current_run && ['project_folder', 'project_files', 'text_artifacts', 'python_artifacts', 'python_sandbox'].includes(snapshot.room.mode);
    const build = element('button', fileContainer ? t("按方案开始") : t("用此方案开文件任务"), 'quiet-button primary-action');
    build.type = 'button'; build.id = 'build-from-plan';
    build.addEventListener('click', () => {
      if (state.snapshot?.room.id !== snapshot.room.id || JSON.stringify(runBinding(state.snapshot?.current_run)) !== JSON.stringify(runBinding(snapshot.current_run))) return;
      if (fileContainer) void submitRunAction('run', { workMode: 'build', text: t("按本轮已交付的方案继续实施：{p0}\n请自主认领、分工修改、需要时求助并互相审核。未完成或未验证的部分如实说明。", { p0: (snapshot.current_run.goal || snapshot.room.goal).slice(0, 2600) }) });
      else void buildFromLegacyPlan();
    });
    actions.append(build, element('span', fileContainer ? t("继续本轮方案，开始新一轮实际修改。完整讨论仍可在聊天查看。") : t("新建可交付文件的任务，带上当前方案摘录；原聊天保留。"), 'detail-caption'));
  }
  if (!objective && runBinding(snapshot.current_run) && snapshot.current_run.can_accept === true) {
    const accept = element('button', t("接受本轮"), 'quiet-button'); accept.type = 'button'; accept.id = 'accept-run';
    accept.addEventListener('click', () => void submitRunAction('accept'));
    actions.append(accept, element('span', t("只记录本轮验收，不会自动开工。"), 'detail-caption'));
  }
  if (!natural && !objective && runBinding(snapshot.current_run) && snapshot.current_run.can_retry === true
      && !nativeHandoff && !nativeRecoveryOffered && !(state.continuousObjectiveSupported && snapshot.current_run.can_restart_objective === true)) {
    const retry = element('button', t("新的尝试（新会话）"), 'quiet-button'); retry.type = 'button'; retry.id = 'retry-run';
    retry.addEventListener('click', () => void submitRunAction('run', { retry: true }));
    actions.append(retry, element('span', t("保留本轮记录，明确开始新一轮尝试。"), 'detail-caption'));
  }
  if (forkBinding(snapshot)) {
    const fork = element('button', state.forkSource?.roomId === snapshot.room.id ? t("取消另开任务") : t("另开任务继续"), 'quiet-button');
    fork.type = 'button'; fork.id = 'fork-room';
    fork.addEventListener('click', prepareFork);
    actions.append(fork);
  }
  if (exportableProject(snapshot)) {
    const download = element('button', t("导出 ZIP"), 'quiet-button'); download.type = 'button'; download.id = 'download-delivery';
    download.addEventListener('click', () => void downloadProject());
    actions.append(download);
  }
  if (actions.children.length) result.append(actions);
  const validation = outcome.validation;
  const validationLabels = { not_run: t("没有运行测试或程序"), static_review: t("伙伴读过内容，没有运行项目或测试"), native_review: t("本机工具协作与审核；具体执行范围见记录"), sandbox_execution: t("隔离执行记录，仍需核对验收范围"),
    self_check: t("只做了自查，没有别的伙伴看过"), mixed_review: t("有的只做了自查，见详情") };
  const artifacts = Array.isArray(outcome.artifacts) ? outcome.artifacts : [];
  const remaining = Array.isArray(outcome.remaining) ? outcome.remaining.filter((item) => typeof item === 'string' && item) : [];
  const details = element('details', '', 'delivery-details');
  details.open = deliveryExpanded;
  details.append(element('summary', `${snapshot.current_run ? t("查看检查与文件清单") : t("查看检查与交付详情")}${artifacts.length ? (snapshot.current_run ? t(" · {p0} 个已保存文件", { p0: artifacts.length }) : t(" · {p0} 个文件", { p0: artifacts.length })) : ''}${remaining.length ? t(" · {p0} 项待处理", { p0: remaining.length }) : ''}`));
  if (validation) {
    const scope = Object.hasOwn(validationLabels, validation.scope) ? validationLabels[validation.scope] : t("验证范围待确认");
    const revision = Number.isSafeInteger(validation.revision) ? t(" · 文件版本 {p0}", { p0: validation.revision }) : '';
    if (!foldedResult) result.append(element('p', t("检查：{p0}", { p0: scope }), 'outcome-validation'));
    details.append(element('p', t("检查范围：{p0}{p1}", { p0: scope, p1: revision }), 'detail-caption'));
    if (typeof validation.summary === 'string' && validation.summary) details.append(element('p', validation.summary, 'detail-caption'));
  }
  if (artifacts.length) {
    details.append(element('h3', snapshot.current_run ? t("工作区文件清单（不代表本轮交付）") : t("交付文件")));
    const list = element('ul', '', 'outcome-artifacts');
    for (const artifact of artifacts) {
      if (!artifact || typeof artifact.path !== 'string') continue;
      const item = element('li');
      item.append(element('strong', artifact.path));
      if (Number.isSafeInteger(artifact.revision)) item.append(element('span', t(" · 版本 {p0}", { p0: artifact.revision })));
      if (typeof artifact.sha256 === 'string') item.append(element('small', `SHA256 ${artifact.sha256}`, 'artifact-hash'));
      list.append(item);
    }
    details.append(list);
  }
  if (remaining.length) {
    details.append(element('h3', t("尚未完成")));
    const list = element('ul', '', 'outcome-remaining');
    for (const item of remaining) list.append(element('li', item));
    details.append(list);
  }
  if (validation || artifacts.length || remaining.length) result.append(details);
  if (foldedResult && report.children.length > 1) result.append(report);
  const nextAction = objective ? null : completed ? t("可在下方继续提出要求，已有成果和聊天保留。") : natural && ['ready_for_user', 'accepted'].includes(outcomeState) ? t("查看成果；需要调整时，直接在下方补充。") : outcomeState === 'ready_for_user' && snapshot.current_run?.can_continue === true ? t("在下方写明下一项要求，再点“开始下一轮”。已有报告和记录保留。") : singlePartner && Object.hasOwn(singlePartnerReasons, outcomeState) ? t("阅读回复后，可在下方补充要求。") : outcome.next_action;
  if (typeof nextAction === 'string' && nextAction) result.append(element('p', t("下一步：{p0}", { p0: nextAction }), 'outcome-next'));
  return true;
}

const usageNames = localizedLabels({
  input_tokens: '输入 Token', output_tokens: '输出 Token', cached_input_tokens: '缓存输入 Token',
  cache_read_input_tokens: '缓存读取 Token', cache_creation_input_tokens: '缓存写入 Token',
  total_tokens: '供应商报告的总 Token', source: '来源', scope: '范围', provider: '供应商',
  model: '模型', cost: '供应商费用字段', cost_usd: '供应商费用（USD）',
});

// usage.source is an internal id such as "claude_cli"; name the tool that reported the numbers.
function usageSourceLabel(source) {
  const tools = { claude: 'Claude Code', codex: 'Codex', kimi: 'Kimi Code' };
  const tool = source.match(/^(claude|codex|kimi)[._]/)?.[1];
  return tool ? t("{p0} 回传的用量", { p0: tools[tool] }) : t("工具回传的用量");
}
function renderUsage(fields, usage) {
  fields.replaceChildren();
  if (!usage || typeof usage !== 'object') {
    fields.append(element('dd', t("尚未取得用量数据，费用和订阅额度不填零。")));
    return;
  }
  const row = (label, value) => fields.append(element('dt', label), element('dd', value));
  row(t("数据来自"), typeof usage.source === 'string' ? usageSourceLabel(usage.source) : t("不可用"));
  row(t("统计方式"), usage.scope === 'provider_reported_latest_result' ? t("供应商最新一次结果快照") : t("历史快照，统计方式未确认"));
  // Current snapshots wrap provider fields in values; older local records may
  // contain the fields directly. Neither form is summed into a session total.
  const values = usage.values && typeof usage.values === 'object' ? usage.values : usage;
  for (const [key, value] of Object.entries(values)) {
    if (['source', 'scope', 'job_id', 'observed_at', 'values', 'cost', 'cost_usd'].includes(key)) continue;
    row(Object.hasOwn(usageNames, key) ? usageNames[key] : key,
      Number.isSafeInteger(value) && value >= 0 ? String(value)
        : ['model', 'provider'].includes(key) && typeof value === 'string' && value.trim() ? value.slice(0, 200) : t("不可用"));
  }
  if (Number.isFinite(usage.observed_at)) row(t("更新时间"), timeLabel(usage.observed_at, true));
  row(t("费用"), t("不可用"));
  row(t("订阅剩余额度"), t("不可用"));
}

async function readExactText(ref) {
  if (!ref || !uuidPattern.test(ref.room_id) || !/^[0-9a-f]{64}$/.test(ref.sha256 || '') || state.deletedRooms.has(ref.room_id)) throw new RequestError(t("这段原文的版本尚未确认。"));
  const query = Object.entries(ref).filter(([key]) => key !== 'room_id').map(([key, value]) => `${key}=${encodeURIComponent(value)}`).join('&');
  const result = await api(`${roomPath(ref.room_id)}/text?${query}`);
  if (Object.entries(ref).some(([key, value]) => result?.[key] !== value) || result.truncated !== false || typeof result.text !== 'string') throw new RequestError(t("原文版本已变化，未用预览代替原文。"));
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(result.text));
  const sha = [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('');
  if (sha !== ref.sha256) throw new RequestError(t("原文内容与记录版本不一致。"));
  return result.text;
}
function renderFullGoal(snapshot) {
  const ref = snapshot?.objective?.original_goal_text_ref || snapshot?.room?.goal_text_ref;
  const truncated = snapshot?.objective?.original_goal_truncated || snapshot?.room?.goal_truncated;
  const details = $('goal-original'); const key = ref && truncated ? JSON.stringify(ref) : '';
  details.hidden = !key;
  if (details.dataset.ref !== key) { details.dataset.ref = key; details.open = false; $('goal-original-text').textContent = ''; $('goal-original-text').dataset.loaded = ''; }
}
$('goal-original').addEventListener('toggle', async () => {
  const details = $('goal-original'); const key = details.dataset.ref;
  if (!details.open || !key || $('goal-original-text').dataset.loaded === key) return;
  $('goal-original-text').textContent = t("正在读取完整要求…");
  try {
    const text = await readExactText(JSON.parse(key));
    if (details.dataset.ref === key && details.open) { $('goal-original-text').textContent = text; $('goal-original-text').dataset.loaded = key; }
  } catch (error) { if (details.dataset.ref === key) $('goal-original-text').textContent = error.message; }
});
function appendBoundText(container, label, ref) {
  if (!ref) return;
  const details = element('details', '', 'bound-text'); details.append(element('summary', label));
  const body = element('pre', '', 'file-content'); details.append(body); let loaded = false; let loading = false;
  details.addEventListener('toggle', async () => {
    if (!details.open || loaded || loading) return;
    loading = true; body.textContent = t("正在读取完整原文…");
    try { const text = await readExactText(ref); if (!state.deletedRooms.has(ref.room_id) && details.parentNode) { body.textContent = text; loaded = true; } }
    catch (error) { body.textContent = error.message; }
    finally { loading = false; }
  }); container.append(details);
}
function jobClaim(snapshot, job) {
  if (pagedRoom(snapshot)) return job?.status === 'working' && Number.isFinite(job.claimed_at)
    ? { created_at: job.claimed_at, work_mode: job.work_mode, runtime_settings: job.runtime_settings } : null;
  return job && [...snapshot.events].reverse().find((event) => event.kind === 'job_claimed' && event.job_id === job.id);
}

function activityRunEvents(snapshot) {
  const runId = snapshot?.current_run?.id || snapshot?.team?.run_id;
  return (snapshot?.events || []).filter((event) => !runId || event.run_id === runId);
}
function nativeActionName(event) {
  const operation = ({ read_file: t("读取文件"), edit_file: t("修改文件"), write_file: t("写入文件"), search_files: t("查找文件"), run_command: t("执行命令"),
    search_web: t("搜索网页"), fetch_web: t("读取网页"), update_plan: t("更新计划"), delegate: t("分配任务"), generate_image: t("生成图片"), use_tool: t("使用工具") })[event?.operation];
  if (operation) return operation;
  return ({ Bash: t("执行命令"), PowerShell: t("执行命令"), command_execution: t("执行命令"),
    Read: t("读取文件"), Edit: t("修改文件"), Write: t("修改文件"), file_change: t("修改文件"),
    WebFetch: t("联网查询"), WebSearch: t("联网查询"), web_search: t("联网查询") })[event?.tool_kind] || t("使用工具");
}
// Whole sentences per tool action: "In progress: Read file" read as a label in English.
const nativeActionSentences = {
  read_file: ["正在读取文件", "处理中 · 最近完成：读取文件"], edit_file: ["正在修改文件", "处理中 · 最近完成：修改文件"],
  write_file: ["正在写入文件", "处理中 · 最近完成：写入文件"], search_files: ["正在查找文件", "处理中 · 最近完成：查找文件"],
  run_command: ["正在执行命令", "处理中 · 最近完成：执行命令"], search_web: ["正在搜索网页", "处理中 · 最近完成：搜索网页"],
  fetch_web: ["正在读取网页", "处理中 · 最近完成：读取网页"], update_plan: ["正在更新计划", "处理中 · 最近完成：更新计划"],
  delegate: ["正在分配任务", "处理中 · 最近完成：分配任务"], generate_image: ["正在生成图片", "处理中 · 最近完成：生成图片"],
  lookup_web: ["正在联网查询", "处理中 · 最近完成：联网查询"], use_tool: ["正在使用本机工具", "处理中 · 最近完成：使用工具"],
};
function nativeActionKind(event) {
  if (Object.hasOwn(nativeActionSentences, event?.operation)) return event.operation;
  return ({ Bash: 'run_command', PowerShell: 'run_command', command_execution: 'run_command', Read: 'read_file', Edit: 'edit_file',
    Write: 'edit_file', file_change: 'edit_file', WebFetch: 'lookup_web', WebSearch: 'lookup_web', web_search: 'lookup_web' })[event?.tool_kind] || 'use_tool';
}
function nativeActionSentence(event, running) { return t(nativeActionSentences[nativeActionKind(event)][running ? 0 : 1]); }
function activityAge(timestamp) {
  if (!Number.isFinite(timestamp)) return '';
  const seconds = Math.floor(Date.now() / 1000 - timestamp);
  if (seconds < 0) return t("最近更新 {p0}", { p0: timeLabel(timestamp) });
  return seconds < 60 ? t("最近更新 {p0}秒前", { p0: seconds }) : seconds < 3600 ? t("最近更新 {p0}分钟前", { p0: Math.floor(seconds / 60) })
    : seconds < 86400 ? t("最近更新 {p0}小时前", { p0: Math.floor(seconds / 3600) }) : t("最近更新 {p0}天前", { p0: Math.floor(seconds / 86400) });
}
function memberUncertainty(snapshot, member) {
  const runId = snapshot?.current_run?.id || snapshot?.team?.run_id;
  const events = activityRunEvents(snapshot);
  const jobs = (snapshot?.jobs || []).filter((job) => job.provider === member.id && (!runId || pagedRoom(snapshot) && (!job.run_id || job.run_id === runId)
    || !pagedRoom(snapshot) && events.some((event) => ['job_created', 'job_claimed'].includes(event.kind) && event.job_id === job.id)));
  const reason = jobs.at(-1)?.error || snapshot?.objective?.stop_reason || snapshot?.team?.stop_reason;
  if (['session_identity_unknown', 'session_identity_changed', 'native_session_identity_mismatch'].includes(reason)) {
    return { label: t("身份待核查"), task: reason === 'native_session_identity_mismatch'
      ? t("返回的专用会话身份与本次任务不匹配，已停止自动续接。") : executionMessage(reason) };
  }
  return { label: t("结果待核对"), task: reason === 'protocol_turn_incomplete' ? executionMessage(reason)
    : t("本次执行结果尚未确认，已有记录保留。") };
}
function configurationMemberActivity(member, activeJob) {
  if (!state.partnerLifecycleSupported || member.configuration_dispatchable !== false || activeJob) return null;
  const labels = { partner_disabled: t("已停用"), partner_archived: t("已归档"), partner_removed: t("已移除配置"),
    partner_unavailable: t("暂不可用"), partner_lifecycle_pending: t("配置处理中") };
  const label = labels[member.configuration_dispatch_reason];
  if (!label || member.identity_uncertain && member.configuration_handoff_completed !== true) return null;
  const transferred = member.configuration_handoff_completed === true;
  return { status: transferred ? 'idle' : 'waiting_peer', label,
    task: transferred ? t("工作已交接。这个伙伴不再接新工作，已有聊天和成果保留。")
      : member.configuration_dispatch_reason === 'partner_lifecycle_pending' ? t("正在保留改动并处理配置，停止与接手尚未确认。")
        : member.configuration_handoff_state && !['done', 'superseded'].includes(member.configuration_handoff_state)
          ? t("这个伙伴不再接新工作，接手尚未完成。现有改动和记录保留。") : t("这个伙伴不再接新工作，已有聊天和成果保留。"),
    historyWarning: transferred && member.identity_uncertain ? t("此前存在未确认的执行结果，原记录仍保留在本机工具执行记录中。") : null };
}

function roomInvitationCandidates() {
  const invitation = state.snapshot?.member_invitation;
  const members = new Set((state.snapshot?.members || []).map((member) => member.id));
  return Array.isArray(invitation?.candidates) ? invitation.candidates.filter((candidate) => candidate
    && agentPattern.test(candidate.id) && typeof candidate.name === 'string' && !members.has(candidate.id)) : [];
}
function renderRoomMemberInvitation() {
  const box = $('room-member-invitation');
  box.hidden = !state.selected || !state.memberInvitationSupported;
  if (box.hidden) return;
  const invitation = state.snapshot?.member_invitation;
  const request = state.memberInvitations.get(state.selected);
  const busy = state.memberInviting.has(state.selected);
  const candidates = roomInvitationCandidates();
  $('member-invitation-hint').textContent = invitation?.supported === false ? t("这份记录暂不支持在原聊天邀请伙伴。原成员和成果保持。")
    : currentObjective()?.status === 'complete' ? t("邀请后可继续补充，已有成果保留；不会重做已交付的工作。")
      : t("新伙伴可按需参考已有讨论与成果，接上当前协作；原伙伴继续。");
  const room = state.selected;
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), [room, candidates, state.connected, invitation?.can_invite, busy, request?.body.request_id]]);
  if (state.memberInvitationFingerprint !== fingerprint) {
    state.memberInvitationFingerprint = fingerprint;
    const options = candidates.map((candidate) => {
      const button = element('button', t("邀请 {p0}", { p0: candidate.name }), 'quiet-button'); button.type = 'button'; button.dataset.inviteMember = candidate.id;
      button.disabled = !state.connected || invitation?.can_invite !== true || busy || !!request;
      button.addEventListener('click', () => { if (state.selected === room) void inviteRoomMember(candidate.id); }); return button;
    });
    $('member-invitation-options').replaceChildren(...options);
  }
  const status = $('member-invitation-status');
  status.hidden = !request && invitation?.can_invite === true && candidates.length > 0;
  status.textContent = busy ? t("正在提交邀请…") : request ? request.invitationError || t("邀请结果正在核对，不会重复提交。")
    : invitation?.reason ? ({ agent_unavailable: t("当前没有其他可邀请的伙伴，可在“伙伴与额度”查看已接入工具。"), room_archived: t("聊天已归档，恢复后可邀请。"), room_removed: t("这份聊天已移除。"), member_limit_reached: t("当前聊天已达到可接入的伙伴数量。"), invitation_mode_unsupported: t("这份旧记录暂不支持邀请。") })[invitation.reason] || t("当前暂时无法邀请，原聊天仍保留。")
      : candidates.length ? '' : t("当前没有其他可邀请的伙伴，可在“伙伴与额度”查看已接入工具。");
  const check = $('member-invitation-check'); check.hidden = !request; check.disabled = busy || !state.connected;
  check.textContent = request && state.hostOutboxRows.get(request.body.request_id)?.delivery.can_send === true ? t("继续邀请") : t("核对邀请状态");
}
async function refreshInvitedMembers(request) {
  const room = state.selected, epoch = state.epoch;
  if (room !== request.roomId || state.deletedRooms.has(room)) return;
  try {
    const snapshot = await readRoomSnapshot(room);
    if (state.selected === room && state.epoch === epoch && snapshot?.room?.id === room) applySnapshot(snapshot);
  } catch (_error) { /* Receipt is authoritative; a later read refreshes the member list without replaying invitation. */ }
}
async function submitMemberInvitation(request, { checkOnly = false } = {}) {
  const room = request.roomId;
  if (!state.connected || state.memberInviting.has(room)) return;
  state.memberInviting.add(room); request.invitationError = ''; renderRoomMemberInvitation();
  try {
    let entry = await stageHostOutbox(request);
    if (!settleHostDelivery(request, entry) && entry.delivery.state === 'staged' && entry.delivery.can_send && !checkOnly) {
      state.hostAutomaticAttempts.add(request.body.request_id); request.attempted = true;
      const response = await api(dispatchPath(request), 'POST', request.body);
      if (response?.acknowledged !== true || response.room_id !== room || response.request_id !== undefined && response.request_id !== request.body.request_id)
        throw new RequestError(t("邀请回执尚未确认。"), { uncertain: true });
      entry = await readHostOutbox(request); settleHostDelivery(request, entry);
    }
    if (entry.delivery.state === 'received') await refreshInvitedMembers(request);
    else request.invitationError = t("邀请尚未确认，原请求已保留；可核对状态，不会另发一份。");
  } catch (error) {
    request.invitationError = error.uncertain ? t("邀请结果尚未确认，原请求已保留；不会重复提交。") : error.message || t("邀请未完成，原聊天保持。");
    if (!error.uncertain) {
      queueHostRetirement(request, 'abandoned');
      if (state.memberInvitations.get(room) === request) state.memberInvitations.delete(room);
      if (state.selected === room) showActionError(request.invitationError, room, 'invite');
    }
  } finally {
    state.memberInviting.delete(room); renderRoomMemberInvitation();
    if (state.hostOutboxSupported) void flushHostRetirements();
  }
}
async function inviteRoomMember(provider) {
  const snapshot = state.snapshot, room = state.selected;
  if (!room || !state.connected || !state.memberInvitationSupported || !state.hostOutboxSupported
      || snapshot?.member_invitation?.can_invite !== true || state.memberInvitations.has(room)
      || !roomInvitationCandidates().some((candidate) => candidate.id === provider)) return;
  saveDraft();
  const request = { kind: 'invite', roomId: room, sourceSeq: pagedRoom(snapshot) ? snapshot.through_seq
    : (snapshot.events || []).reduce((seq, event) => Math.max(seq, Number.isSafeInteger(event.seq) ? event.seq : 0), 0),
    body: { request_id: crypto.randomUUID(), participants: [provider] } };
  state.memberInvitations.set(room, request); await submitMemberInvitation(request);
}
$('member-invitation-check').addEventListener('click', () => {
  const request = state.memberInvitations.get(state.selected);
  if (request) void submitMemberInvitation(request, { checkOnly: state.hostOutboxRows.get(request.body.request_id)?.delivery.can_send !== true });
});
$('member-invitation-manage').addEventListener('click', (event) => openManager('partners', event.currentTarget || $('member-invitation-manage')));

function memberActivity(snapshot, member) {
  const id = member.id; const runEvents = activityRunEvents(snapshot);
  const runId = snapshot?.current_run?.id || snapshot?.team?.run_id;
  const jobs = (snapshot?.jobs || []).filter((job) => job.provider === id && (pagedRoom(snapshot) || !runId || runEvents.some((event) => ['job_created', 'job_claimed'].includes(event.kind) && event.job_id === job.id)));
  const job = jobs.find((entry) => entry.status === 'working') || jobs.find((entry) => entry.status === 'queued');
  const relevantKinds = new Set(['job_created', 'job_claimed', 'job_finished', 'member_status', 'project_inspection',
    'team_task_claimed', 'team_task_completed', 'team_task_reviewed', 'team_help_requested', 'team_help_finished', 'native_tool_activity', 'native_readonly_discussion']);
  const events = runEvents.filter((event) => relevantKinds.has(event.kind) && Number.isFinite(event.created_at)
    && (event.sender === id || event.recipient === id || jobs.some((entry) => entry.id === event.job_id)));
  const last = job?.latest_native_activity || events.reduce((found, event) => !found || event.created_at > found.created_at ? event : found, null);
  const claim = jobClaim(snapshot, job);
  const own = (snapshot?.team?.tasks || []).filter((task) => task.owner === id);
  const objective = currentObjective(snapshot);
  const retired = objective?.retired_partners?.[id];
  const outcome = snapshot?.current_run?.status === 'stopped' ? 'stopped' : terminalTeamState(snapshot) || snapshot?.collaboration?.outcome?.state;
  let status = 'idle'; let label = t("空闲"); let task = t("现在没在做事");
  if (!snapshot) return { status: 'idle', label: state.selected ? t("正在读取记录") : t("未开始"), task: state.selected ? t("正在读取本次任务状态。") : t("发出目标后，在这里看进展。"), last: null, claim: null, job: null };
  const configuration = configurationMemberActivity(member, job);
  if (configuration) { ({ status, label, task } = configuration); }
  else if (member.identity_uncertain) {
    const uncertainty = memberUncertainty(snapshot, member); status = 'unknown'; label = uncertainty.label; task = uncertainty.task;
  }
  else if (!activeEntry(snapshot.room)) { label = lifecycleLabels[lifecycleState(snapshot.room)]; task = t("历史记录可查看，恢复后再继续。"); }
  else if (!job && member.joined_pending === true) { label = t("已加入，等待安排"); task = objective?.status === 'complete' ? t("已有成果保留，有新要求可继续补充。") : t("可按需参与当前协作，原伙伴继续处理已有工作。"); }
  else if (job && state.partnerLifecycleSupported && (job.partner_handoff_inspection === true || member.configuration_dispatchable === false)) {
    status = job.status;
    label = job.partner_handoff_inspection === true ? job.status === 'queued' ? t("等待读取接手记录") : t("只读核对接手进度")
      : job.status === 'queued' ? t("等待开始") : t("正在处理");
    task = job.partner_handoff_inspection === true ? t("正在只读核对保留的改动和记录，不修改文件。")
      : teamMemberTask({ ...snapshot, jobs }, member) || t("当前处理尚未结束，配置变化不代表已经停止。");
    if (objective?.status === 'paused' && objective.pause_requested) task += t(" 当前处理结束后暂停。");
  }
  else if (!job && objective && ['stopped', 'interrupted', 'needs_user'].includes(objective.status)) {
    status = objective.status === 'needs_user' ? 'needs_user' : objective.status;
    label = objectiveLabel(objective); task = hostResultInterruption(objective) ? hostResultInterruptionMessage(snapshot) : objective.stop_reason ? stopReasonMessage(objective.stop_reason)
      : objective.next_step?.question || t("当前进度已保留，请查看下一步与任务记录。");
  } else if (!job && objective?.status === 'complete') { label = isReadOnlyDiscussion(snapshot) ? readOnlyLabel(snapshot, true) : Number.isFinite(snapshot.current_run?.accepted_at) ? t("目标已确认完成") : t("目标已交付"); task = readOnlyBuild(snapshot)
    ? t("现在是只读权限，伙伴只能看不能改。要改文件，在“设置”里把权限换成 {p0}。", { p0: writablePermissionName(snapshot) })
    : isReadOnlyDiscussion(snapshot) ? t("回复和建议都在聊天里。要开工，切到“动手”再发一句话。") : t("查看这个目标的成果；只做了自查的部分会单独注明。"); }
  else if (!job && retired?.cleanup_confirmed === true) {
    status = 'needs_user'; label = t("本轮暂不参与");
    const takeover = [...(snapshot.events || [])].reverse().find((event) => event.kind === 'task_taken_over' && event.previous_owner === id
      && event.owner && (snapshot.team?.tasks || []).some((entry) => entry.owner === event.owner && ['assigned', 'working', 'waiting_peer', 'review'].includes(entry.status) && (!event.task_id || entry.id === event.task_id)));
    const handedOn = takeover ? t("{p0} 正在接着做。", { p0: displayName(takeover.owner) }) : t("本轮安排保持。");
    task = retired.reason === 'native_preflight_failed' ? t("模型服务连接失败，本目标不再安排它。{p0}", { p0: handedOn }) : t("此前额度受限。{p0}当前额度见下方。", { p0: handedOn });
  }
  else if (!job && objective?.status === 'awaiting_continue') { label = t("阶段已交付"); task = t("下一步已列在交付卡；无需逐阶段接受。"); }
  else if (objective?.status === 'paused' && !job) { label = objectiveLabel(objective); task = objective.pause_requested ? t("等当前处理结束后，不再派发下一项。") : t("当前进度保留，继续后接着处理原目标。"); }
  else if (!job && idleReadOnlyDiscussion(snapshot)) { label = readOnlyLabel(snapshot); task = readOnlyBuild(snapshot)
    ? t("现在是只读权限，伙伴只能看不能改。要改文件，在“设置”里把权限换成 {p0}。", { p0: writablePermissionName(snapshot) })
    : t("现在没在做事。可以接着讨论，或切到“动手”开工。"); }
  else if (!objective && outcome !== 'stopped' && outcome !== 'unknown' && (outcome === 'accepted' || Number.isFinite(snapshot.current_run?.accepted_at))) { label = t("本轮已接受"); task = t("已有交付已接受，可补充下一项要求。"); }
  else if (outcome === 'ready_for_user' && !objective) { label = t("本轮已交付"); task = t("等你验收，或补充下一项要求。"); }
  else if (!objective && outcome === 'unknown') { status = 'unknown'; label = t("状态待核对"); task = t("执行状态尚未确认，请查看具体记录。"); }
  else if (!objective && (outcome === 'stopped' || closedStatuses.has(snapshot.room.status))) {
    const latestJob = jobs.at(-1); const ownError = latestJob?.status === 'failed' ? latestJob.error : null;
    const reason = ownError || snapshot.team?.stop_reason;
    status = ownError ? 'failed' : 'stopped';
    label = reason === 'budget_exhausted' || snapshot.room.status === 'budget_exhausted' ? t("调用上限已到") : ownError ? t("本次处理未完成") : t("本轮已停止");
    task = reason ? `${ownError ? '' : t("协作已停止：")}${ownError ? executionMessage(reason) : stoppedReasonMessage(snapshot, reason)}` : t("保留已完成内容，请查看剩余项。");
  } else if (!objective && (outcome === 'needs_user' || member.status === 'needs_user')) { status = 'needs_user'; label = t("等你处理"); task = teamMemberTask({ ...snapshot, jobs: [] }, member) || t("需要你补充信息或作出决定。"); }
  else if (job) {
    status = job.status;
    label = job.status === 'queued' ? t("等待开始") : job.kind === 'team_review' || job.kind === 'review' ? t("正在审核")
      : ['team_help', 'help'].includes(job.kind) ? t("正在协助") : ['team_plan', 'team_coordinate'].includes(job.kind) ? t("正在分工")
        : snapshot.current_run?.work_mode === 'plan' ? t("整理方案") : t("正在处理");
    task = snapshot.team ? teamMemberTask({ ...snapshot, jobs }, member) : t("按当前任务推进，具体动作未记录。");
    if (objective?.status === 'checkpoint') { label = job.status === 'queued' ? t("等待判断下一步") : t("正在判断下一步"); task = t("核对已有成果与原目标，决定继续、完成或向你提问。"); }
    const native = job.status === 'working' && (job.latest_native_activity || [...runEvents].reverse().find((event) => event.kind === 'native_tool_activity' && event.job_id === job.id && event.sender === id));
    if (native && ['started', 'running', 'completed', 'failed'].includes(native.state)) {
      label = native.state === 'failed' ? t("工具返回异常") : nativeActionSentence(native, ['running', 'started'].includes(native.state));

    }
    const inspection = runEvents.find((event) => event.kind === 'native_partial_inspection_queued' && event.job_id === job.id);
    if (inspection || job.native_partial_inspection === true) {
      label = job.status === 'queued' ? t("等待核对上次进度") : t("正在核对上次进度");
      task = t("只读核对上次留下的改动和记录。");
    }
    if (job.native_readonly_discussion === true || runEvents.some((event) => event.kind === 'native_readonly_discussion' && event.job_id === job.id)) {
      label = job.status === 'queued' ? t("等待讨论") : t("只读讨论");
      task = t("查阅保留的改动并回复，不修改或发布文件。");
    }
    if (objective?.status === 'paused' && objective.pause_requested) task += t(" 当前处理结束后暂停。");
  } else {
    const waiting = own.find((entry) => entry.status === 'waiting_peer');
    const review = own.find((entry) => entry.status === 'review');
    const blocked = own.find((entry) => ['blocked', 'unknown'].includes(entry.status));
    const assigned = own.find((entry) => entry.status === 'assigned');
    if (waiting || review) { status = 'waiting_peer'; label = waiting ? t("等伙伴协助") : t("等伙伴审核"); }
    else if (blocked) { status = 'needs_user'; label = t("遇到阻碍"); }
    else if (assigned) { status = 'waiting_peer'; label = t("等待接手或依赖"); }
    else if (member.status === 'working' || member.status === 'queued') { status = 'unknown'; label = t("阶段待核对"); }
    else if (['failed', 'unknown', 'interrupted'].includes(member.status)) { status = member.status; label = statusLabels[member.status]; }
    task = snapshot.team ? teamMemberTask({ ...snapshot, jobs: [] }, member) : t("现在没有在进行的阶段。");
    if (objective?.status === 'checkpoint') { status = 'waiting_peer'; label = t("安排下一步"); task = t("正在安排下一阶段，暂时没有在做的事。"); }
  }
  return { status, label, task: task || t("具体阶段待核对。"), historyWarning: configuration?.historyWarning, last, claim: status === 'working' ? claim : null,
    job: ['working', 'queued'].includes(status) ? job : null,
    legacyTask: !snapshot.team && typeof member.current_task === 'string' ? member.current_task : null };
}
function compactActivityLabel(info) {
  if (Object.values(nativeActionSentences).some(([, done]) => info.label === t(done))) return t("正在处理");
  // Labels arrive translated, so match them against the translated source copy.
  const compact = { '正在使用本机工具': t("正在处理"), '正在读取文件': t("正在读文件"), '正在修改文件': t("正在改文件"),
    '正在写入文件': t("正在改文件"), '正在查找文件': t("正在查找"), '正在执行命令': t("正在执行"),
    '正在搜索网页': t("正在搜索"), '正在读取网页': t("正在查询"), '正在联网查询': t("正在查询"),
    '正在更新计划': t("整理计划"), '正在分配任务': t("正在分工"), '正在生成图片': t("正在出图"),
    '正在核对上次进度': t("正在核对"), '等待核对上次进度': t("等待核对"), '正在判断下一步': t("安排下一步"),
    '等待判断下一步': t("等待安排"), '正在暂停，等待当前处理结束': t("正在暂停"),
    '只读核对接手进度': t("只读核对"), '等待读取接手记录': t("等待核对"),
    '目标推进已停止': t("已停止"), '目标已确认完成': t("已完成") };
  const source = Object.keys(compact).find((key) => t(key) === info.label);
  return source ? compact[source] : info.label;
}
function renderPartnerActivity(snapshot) {
  const members = snapshot?.members || state.catalog.filter((agent) => state.participantIds.includes(agent.id));
  $('partner-activity').hidden = !state.selected || !members.length;
  const remaining = new Set(members.map((member) => member.id));
  for (const [id, card] of state.activityNodes) if (!remaining.has(id)) { card.remove(); state.activityNodes.delete(id); }
  for (const [index, member] of members.entries()) {
    const info = memberActivity(snapshot, member); let card = state.activityNodes.get(member.id);
    if (!card) { card = element('details', '', 'partner-activity-card'); card.id = `partner-activity-${member.id}`; state.activityNodes.set(member.id, card); }
    const elapsed = info.claim ? Math.max(0, Math.floor(Date.now() / 1000 - info.claim.created_at)) : null;
    const duration = elapsed === null ? '' : elapsed >= 60 ? t("{p0}分{p1}秒", { p0: Math.floor(elapsed / 60), p1: elapsed % 60 }) : t("{p0}秒", { p0: elapsed });
    const age = activityAge(info.last?.created_at) + (Number.isFinite(info.last?.created_at) && Date.now() / 1000 - info.last.created_at >= 60 ? t("，尚无新操作记录") : '');
    const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), [member.name, info, duration, age, state.connected]]);
    if (card.dataset.fingerprint !== fingerprint) {
      const refocus = document.activeElement === card.firstChild;
      card.dataset.fingerprint = fingerprint; card.className = `partner-activity-card activity-${info.status}${state.connected ? '' : ' activity-stale'}`;
      const summary = element('summary'); const heading = element('span', '', 'activity-identity');
      const name = element('strong', member.name || displayName(member.id)); name.title = member.name || displayName(member.id);
      const shortLabel = compactActivityLabel(info);
      const status = element('span', `${state.connected ? '' : t("上次状态 · ")}${shortLabel}`, 'activity-state'); status.title = info.label;
      heading.append(avatar(member.id, member), name, status); summary.append(heading); summary.title = info.task;
      const details = element('div', '', 'activity-details-body');
      if (shortLabel !== info.label) details.append(element('p', info.label, 'detail-caption'));
      details.append(element('p', info.task, 'activity-task'));
      if (age) details.append(element('small', age, 'activity-time'));
      if (pagedRoom(snapshot) && info.job?.status === 'working') {
        const choice = info.job.runtime_settings?.members?.[member.id];
        details.append(element('p', t("本次配置：{p0} · {p1}{p2}", { p0: info.job.work_mode === 'plan' ? t("方案") : t("动手"), p1: runtimePermissionName(info.job.permission), p2: choice ? ` · ${choice.model || t("工具默认")} · ${choice.effort || t("默认强度")}` : '' }), 'detail-caption'));
      }
      if (info.claim) details.append(element('p', t("本次认领于 {p0} · 距今 {p1}", { p0: timeLabel(info.claim.created_at, true), p1: duration })));
      if (info.last?.kind === 'project_inspection') details.append(element('p', t("最近记录确认查看过项目资料；没有提供具体文件或正在使用的工具。")));
      if (info.last?.kind === 'native_tool_activity' && info.last.state === 'running') details.append(element('p',
        t("{p0}这只表示工具仍在运行，不表示已有新成果或测试通过。", { p0: Number.isFinite(info.last.elapsed_time_seconds) && info.last.elapsed_time_seconds >= 0 ? t("供应商记录工具已运行 {p0} 秒。", { p0: Math.floor(info.last.elapsed_time_seconds) }) : '' }), 'detail-caption'));
      if (info.historyWarning) details.append(element('p', info.historyWarning, 'detail-caption'));
      if (info.legacyTask) details.append(element('p', t("最近派发说明：{p0}", { p0: info.legacyTask })));
      details.append(element('p', t("按实际工具记录更新；不展示模型思考。"), 'detail-caption'));
      card.replaceChildren(summary, details);
      if (refocus) summary.focus({ preventScroll: true });
    }
    const container = $('partner-activity-cards'); if (container.children[index] !== card) container.insertBefore(card, container.children[index] || null);
  }
  $('partner-activity-note').hidden = state.connected;
  $('partner-activity-note').textContent = state.connected ? '' : state.connectionChecked ? t("本地服务未连接，以下是上次读取到的状态。") : t("正在连接本地服务…");
}

function taskDisplayTitle(title) {
  return typeof title === 'string' ? title.replace(/^\s*\[(?:100|[1-9]?\d)%\]\s*/u, '').trim() || t("未命名任务") : t("子任务");
}
function teamMemberTask(snapshot, member) {
  if (!snapshot?.team || !member) return null;
  const tasks = Array.isArray(snapshot.team.tasks) ? snapshot.team.tasks : [];
  const jobs = snapshot.jobs.filter((job) => job.provider === member.id);
  const job = jobs.find((entry) => entry.status === 'working') || jobs.find((entry) => entry.status === 'queued');
  if (job) {
    const waiting = job.status === 'queued';
    // Whole sentences: "In progress: Review “x”" read as a label in English; the Chinese is unchanged.
    if (['team_plan', 'team_coordinate', 'team_user'].includes(job.kind)) return t(waiting ? "等待开始{p0}" : "正在{p0}", { p0: teamStageLabels[job.kind] });
    const bindings = {
      team_work: ['owner', waiting ? 'assigned' : 'working'],
      team_review: ['reviewer', 'review'],
      team_help: ['help_to', 'waiting_peer'],
    };
    if (Object.hasOwn(bindings, job.kind)) {
      const [role, status] = bindings[job.kind];
      const candidates = tasks.filter((task) => job.task_id ? task.id === job.task_id : task[role] === member.id && task.status === status);
      // Jobs currently expose no task_id in the public snapshot. Only name a
      // task when the recorded role and lifecycle identify exactly one.
      const task = candidates.length === 1 ? candidates[0] : null;
      const title = task && typeof task.title === 'string' ? interfaceQuote(taskDisplayTitle(task.title)) : t("子任务（具体任务待核对）");
      const mode = jobClaim(snapshot, job)?.work_mode || snapshot.room.work_mode || snapshot.current_run?.work_mode;
      let action = mode === 'plan' ? 'plan' : 'work';
      if (job.kind === 'team_review') action = 'review';
      else if (job.kind === 'team_help') action = 'help';
      else if (mode !== 'plan' && (task?.capability === 'python' || task?.write_paths?.length)) action = 'build';
      const sentences = {
        plan: ["正在整理方案{p0}", "等待开始整理方案{p0}"], work: ["正在处理{p0}", "等待开始处理{p0}"],
        review: ["正在审核{p0}", "等待开始审核{p0}"], help: ["正在协助处理{p0}", "等待开始协助处理{p0}"], build: ["正在开发{p0}", "等待开始开发{p0}"],
      };
      return t(sentences[action][waiting ? 1 : 0], { p0: title });
    }
    return waiting ? t("等待开始处理团队任务，具体分工待核对") : t("正在处理团队任务，具体分工待核对");
  }
  const owned = tasks.filter((task) => task.owner === member.id);
  const waitingForHelp = owned.find((task) => task.status === 'waiting_peer');
  if (waitingForHelp) return t("「{p0}」等待{p1}协助", { p0: taskDisplayTitle(waitingForHelp.title), p1: waitingForHelp.help_to ? displayName(waitingForHelp.help_to) : t("伙伴") });
  const awaitingReview = owned.find((task) => task.status === 'review');
  if (awaitingReview) return t("「{p0}」已提交，等待独立审核", { p0: taskDisplayTitle(awaitingReview.title) });
  const pending = owned.find((task) => task.status === 'assigned');
  if (pending) {
    const dependencies = Array.isArray(pending.dependencies) ? pending.dependencies
      .map((id) => tasks.find((task) => task.id === id)).filter((task) => task && !['accepted', 'self_checked', 'withdrawn'].includes(task.status)) : [];
    return dependencies.length ? t("「{p0}」等待依赖：{p1}", { p0: taskDisplayTitle(pending.title), p1: interfaceList(dependencies.map((task) => taskDisplayTitle(task.title))) }) : t("「{p0}」已认领，等待派发", { p0: taskDisplayTitle(pending.title) });
  }
  const blocked = owned.find((task) => ['blocked', 'unknown', 'interrupted', 'cancelled'].includes(task.status));
  // Unfinished work carries into the next attempt unless the user stopped it, as on the progress board.
  const userCancelled = userCancelCodes.has(currentObjective(snapshot)?.stop_reason) || userCancelCodes.has(snapshot?.team?.stop_reason);
  if (blocked && (['interrupted', 'cancelled'].includes(blocked.status) && !userCancelled || taskQuotaHeld(blocked))) return t("「{p0}」待继续", { p0: taskDisplayTitle(blocked.title) });
  if (blocked) return t("「{p0}」{p1}，具体原因见“进展”", { p0: taskDisplayTitle(blocked.title), p1: taskStatusLabels[blocked.status] });
  return t("现在没在做事。做完的内容在“交付”里看。");
}

function livenessMinutes(seconds) {
  const minutes = Math.floor(Math.max(0, seconds) / 60);
  return minutes < 60 ? t("{p0} 分钟", { p0: minutes }) : t("{p0} 小时 {p1} 分钟", { p0: Math.floor(minutes / 60), p1: minutes % 60 });
}
const partnerQuietWarningSeconds = 600;
// Answers "is it stuck?" from recorded timestamps only. A heartbeat proves the tool process is alive, nothing more.
function partnerLiveness(job, tool, publicMessage) {
  if (job?.status !== 'working') return null;
  const now = Date.now() / 1000;
  const lastRecord = Math.max(tool?.created_at || 0, publicMessage?.created_at || 0, job.claimed_at || 0);
  if (!lastRecord) return null;
  const quiet = Math.max(0, now - lastRecord);
  if (quiet >= partnerQuietWarningSeconds) return { warning: true, text: t("已 {p0}没有新记录，可能卡住了", { p0: livenessMinutes(quiet) }) };
  const parts = [quiet < 60 ? t("刚刚有活动") : t("{p0}前有活动", { p0: livenessMinutes(quiet) })];
  const toolRunning = ['started', 'running'].includes(tool?.state);
  const elapsed = toolRunning ? (Number.isFinite(tool.elapsed_time_seconds) ? tool.elapsed_time_seconds : now - tool.created_at) : null;
  if (elapsed !== null && elapsed >= 60) parts.push(t("当前工具已运行 {p0}", { p0: livenessMinutes(elapsed) }));
  if (Number.isFinite(job.claimed_at) && now - job.claimed_at >= 60) parts.push(t("这一步已做 {p0}", { p0: livenessMinutes(now - job.claimed_at) }));
  if (publicMessage && now - publicMessage.created_at >= 300) parts.push(t("上次发言 {p0}前", { p0: livenessMinutes(now - publicMessage.created_at) }));
  return { warning: false, text: parts.join(' · ') };
}
const progressStatusStaleSeconds = 600;
// Shows what a long command wrote to its progress file. Untrusted display text: never a tool event, a message or proof of success.
function progressStatusLine(job, status) {
  if (job?.status !== 'working' || typeof status?.text !== 'string' || !status.text || !Number.isFinite(status.created_at)) return null;
  const age = Math.max(0, Date.now() / 1000 - status.created_at);
  const line = t("进度：{p0} · {p1}", { p0: status.text, p1: age < 60 ? t("刚刚") : t("{p0} 分钟前", { p0: Math.floor(age / 60) }) });
  return age >= progressStatusStaleSeconds ? line + t("（可能已过期）") : line;
}
function renderMemberPublicProgress(snapshot, member) {
  const holder = $(`public-progress-${member.id}`);
  const events = activityRunEvents(snapshot);
  const jobs = (snapshot?.jobs || []).filter((job) => job.provider === member.id
    && (pagedRoom(snapshot) || !snapshot?.current_run || events.some((event) => ['job_created', 'job_claimed'].includes(event.kind) && event.job_id === job.id)));
  const active = jobs.find((job) => job.status === 'working') || jobs.find((job) => job.status === 'queued');
  const projectedJob = active || (pagedRoom(snapshot) ? [...jobs].filter((job) => job.latest_public_progress).sort((a, b) => b.latest_public_progress.seq - a.latest_public_progress.seq)[0] : null);
  const projectedProgress = projectedJob?.latest_public_progress;
  const publicMessage = projectedProgress ? { ...projectedProgress, job_id: projectedJob.id, text: projectedProgress.text_preview,
    truncated: projectedProgress.producer_truncated, original_characters: projectedProgress.original_characters } : [...events].reverse().find((event) => event.kind === 'message' && event.native_progress === true
    && event.sender === member.id && typeof event.text === 'string' && event.text.trim()
    && (active ? event.job_id === active.id : jobs.some((job) => job.id === event.job_id)));
  const tool = active && (active.latest_native_activity || [...events].reverse().find((event) => event.kind === 'native_tool_activity'
    && event.sender === member.id && event.job_id === active.id && ['started', 'running', 'completed', 'failed'].includes(event.state)));
  const scope = `${snapshot?.room.id || ''}:${active?.id || 'history'}`;
  const liveness = partnerLiveness(active, tool, publicMessage);
  const progressStatus = active && progressStatusLine(active, active.latest_progress_status || [...events].reverse().find((event) => event.kind === 'native_progress_status'
    && event.sender === member.id && event.job_id === active.id));
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), [scope, publicMessage, tool, liveness, progressStatus]]);
  if (holder.dataset.fingerprint === fingerprint) return;
  const expanded = holder.dataset.scope === scope && holder.firstChild?.open === true;
  holder.dataset.fingerprint = fingerprint; holder.dataset.scope = scope; holder.replaceChildren();
  holder.hidden = !snapshot;
  if (!snapshot) return;
  if (liveness) { const line = element('p', liveness.text, `partner-liveness${liveness.warning ? ' warning' : ''}`); line.id = `partner-liveness-${member.id}`; holder.append(line); }
  if (progressStatus) { const line = element('p', progressStatus, 'partner-progress-status'); line.id = `partner-progress-status-${member.id}`; holder.append(line); }
  if (!publicMessage) {
    const text = tool ? `${['started', 'running'].includes(tool.state) ? nativeActionSentence(tool, true) : `${tool.state === 'failed' ? t("最近返回异常：") : t("最近返回：")}${nativeActionName(tool)}`} · ${timeLabel(tool.created_at)}`
      : active ? t("还没收到这次工作的进展消息。") : t("现在没在做事，之前的进展在聊天里。");
    // The idle line repeats the member's task line, so the card hides it.
    // Without a progress message the line only repeats the status above it (idle, or the latest tool action).
    holder.append(element('p', text, `public-progress-fallback${!active || tool ? ' idle' : ''}`)); return;
  }
  const details = element('details', '', 'public-progress-details'); details.open = expanded;
  const summary = element('summary');
  const characters = [...publicMessage.text];
  summary.append(element('span', `${active ? t("本次进展原话") : t("此前进展原话（已结束）")} · ${timeLabel(publicMessage.created_at)}`, 'public-progress-source'));
  if (active) summary.append(element('span', characters.length > 160 ? `${characters.slice(0, 160).join('')}…` : publicMessage.text, 'public-progress-preview'));
  details.append(summary);
  const original = element('p', publicMessage.text, 'public-progress-original'); details.append(original);
  if (projectedProgress?.text_truncated) {
    const read = element('button', t("读取完整原话"), 'quiet-button'); read.type = 'button';
    read.addEventListener('click', async () => {
      read.disabled = true;
      try { const text = await readExactText(projectedProgress.text_text_ref || projectedProgress.text_ref); if (state.selected === snapshot.room.id && holder.dataset.scope === scope) { original.textContent = text; read.remove(); } }
      catch (error) { read.disabled = false; showNotice(error.message, 'error'); }
    }); details.append(read);
  }
  if (publicMessage.truncated === true && Number.isSafeInteger(publicMessage.original_characters) && publicMessage.original_characters > characters.length) {
    details.append(element('p', t("仅显示前 {p0} 字，原文 {p1} 字", { p0: characters.length, p1: publicMessage.original_characters }), 'public-progress-source'));
  }
  const jump = element('button', t("查看原话"), 'quiet-button'); jump.type = 'button'; jump.id = `public-progress-jump-${member.id}`;
  const roomId = snapshot.room.id;
  jump.addEventListener('click', async () => {
    if (state.selected !== roomId || state.snapshot?.room.id !== roomId) return;
    try {
    if (projectedProgress && !state.eventNodes.has(publicMessage.seq)) {
      while (state.selected === roomId && historyFor(roomId).hasBefore && !historyFor(roomId).events.has(publicMessage.seq)) {
        const history = historyFor(roomId); const page = await readEventPage(roomId, history.through, { before_seq: history.first }); mergeEventPage(history, page, 'before');
      }
      if (state.selected !== roomId) return;
      state.snapshot.events = [...historyFor(roomId).events.values()].sort((a, b) => a.seq - b.seq); renderEvents(state.snapshot, { preservePosition: true });
    }
    const current = state.snapshot.events.find((event) => event.seq === publicMessage.seq);
    if (current?.job_id !== publicMessage.job_id || current?.sender !== member.id || !projectedProgress && current?.text !== publicMessage.text) return;
    closeReader();
    const node = state.eventNodes.get(publicMessage.seq)?.node;
    if (node) { node.setAttribute('tabindex', '-1'); node.scrollIntoView?.({ block: 'center' }); node.focus({ preventScroll: true }); }
    } catch (error) { if (state.selected === roomId) showNotice(error.message, 'error'); }
  });
  details.append(jump); holder.append(details);
}

const cacheMissNoteSeconds = 3600;
function renderCacheMissNote(snapshot) {
  const note = $('cache-miss-note'); const miss = snapshot?.latest_native_cache_miss;
  const written = miss?.cache_creation_input_tokens;
  // One old miss says nothing about the calls since, so the note retires instead of staying up for the whole room.
  const show = Number.isSafeInteger(written) && written > 0 && Date.now() / 1000 - miss.created_at < cacheMissNoteSeconds;
  note.hidden = !show;
  if (!show) { note.replaceChildren(); delete note.dataset.fingerprint; return; }
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), miss.provider, miss.seq, written, miss.created_at]);
  if (note.dataset.fingerprint === fingerprint) return;
  note.dataset.fingerprint = fingerprint;
  const amount = englishInterface() ? `${Math.round(written / 1000)}K` : `${Math.round(written / 10000)} 万`;
  note.replaceChildren(element('span', t("{p0} 这次调用没用上缓存，多写了约 {p1} token", { p0: displayName(miss.provider), p1: amount })),
    element('time', timeLabel(miss.created_at, true), 'cache-miss-time'));
}

function renderMembers(snapshot) {
  syncParticipants(snapshot);
  renderCacheMissNote(snapshot);
  const loading = !!state.selected && !snapshot;
  const objective = currentObjective(snapshot);
  for (const provider of state.participantIds) {
    const member = snapshot?.members.find((entry) => entry.id === provider);
    const { account } = currentAccount(provider);
    const availability = availabilityInfo(provider);
    const line = memberAvailabilityLine(availability);
    $(`availability-${provider}`).hidden = !line;
    $(`availability-${provider}`).textContent = line ? line.text : '';
    $(`availability-${provider}`).classList.toggle('blocked', line?.tone === 'ask');
    $(`availability-${provider}`).classList.toggle('waiting', line?.tone === 'wait');
    $(`account-email-${provider}`).textContent = memberLoginCaption(account, provider);
    updateAccountFooter($(`provider-account-${provider}`), provider);
    const hasJobs = snapshot?.jobs?.some((job) => job.provider === provider);
    const waitingForPeer = member?.status === 'idle' && !member.identity_uncertain
      && snapshot?.team?.tasks?.some((task) => task.owner === provider && task.status === 'waiting_peer');
    const displayStatus = waitingForPeer ? 'waiting_peer' : member?.status;
    const uncertainty = member?.identity_uncertain ? memberUncertainty(snapshot, member) : null;
    const retirement = objective?.retired_partners?.[provider];
    const retiredActivity = member && ((objective?.status === 'running' && snapshot.room.status === 'running') || objective?.status === 'paused')
      && activeEntry(snapshot.room) && !terminalTeamState(snapshot) && !member.identity_uncertain
      && ['idle', 'needs_user'].includes(member.status) && retirement?.cleanup_confirmed === true
      && ['agent_quota_exhausted', 'subscription_limit_reached', 'native_preflight_failed'].includes(retirement.reason)
      && !snapshot.jobs.some((job) => job.provider === provider && ['queued', 'working'].includes(job.status))
      ? memberActivity(snapshot, member) : null;
    const paused = objective?.status === 'paused' && activeEntry(snapshot?.room) && !member?.identity_uncertain
      && !['failed', 'unknown', 'interrupted', 'cancelled'].includes(member?.status)
      ? sidebarObjectiveLabel({ ...snapshot.room, objective_status: 'paused', objective_pause_requested: objective.pause_requested }) : null;
    const activity = member && snapshot?.team && activeEntry(snapshot.room)
      && !member.identity_uncertain && !['failed', 'unknown', 'interrupted', 'cancelled'].includes(member.status)
      ? memberActivity(snapshot, member) : null;
    const configurationActivity = member && state.partnerLifecycleSupported && (member.configuration_dispatchable === false
      || snapshot.jobs.some((job) => job.provider === provider && ['working', 'queued'].includes(job.status) && job.partner_handoff_inspection === true))
      ? memberActivity(snapshot, member) : null;
    const taskActivity = activity && activity.label !== t("空闲") ? activity : null;
    const preferredActivity = configurationActivity || retiredActivity;
    const pausedTask = paused === t("正在暂停") ? memberActivity(snapshot, member).task : t("继续后接着做。");
    const notStarted = (!snapshot && !loading) || (member && !member.identity_uncertain && !member.session_id && !hasJobs && displayStatus === 'idle');
    $(`name-${provider}`).textContent = member?.name || names[provider];
    // While the room resumes by itself, a partner stopped with it reads as waiting to continue, like its tasks.
    const resumes = snapshot && roomStopKind(snapshot.room) && ['interrupted', 'cancelled', 'stopped'].includes(member?.status) && !member.identity_uncertain;
    // Out of quota while the room waits for it: say so once; the line below already gives the return time.
    const quotaWaiting = snapshot && roomStopKind(snapshot.room) === 'quota_wait' && member && !member.identity_uncertain
      && ['agent_quota_exhausted', 'subscription_limit_reached'].includes(retirement?.reason);
    $(`status-${provider}`).textContent = quotaWaiting ? t("等额度恢复") : resumes ? t("待继续") : preferredActivity ? preferredActivity.label : paused || taskActivity?.label || (member?.identity_uncertain ? uncertainty.label : (loading ? t("正在读取") : (notStarted ? t("未启动") : statusLabels[statusClass(displayStatus)])));
    $(`status-${provider}`).className = `member-status ${preferredActivity ? statusClass(preferredActivity.status) : paused ? paused === t("正在暂停") ? 'working' : 'idle' : notStarted || loading ? '' : statusClass(preferredActivity?.status || taskActivity?.status || displayStatus)}`;
    $(`task-${provider}`).textContent = quotaWaiting ? teamMemberTask(snapshot, member) || t("额度恢复后接着做。") : preferredActivity ? preferredActivity.task : paused ? pausedTask : taskActivity ? taskActivity.task : member?.identity_uncertain ? uncertainty.task
      : teamMemberTask(snapshot, member) || (typeof member?.current_task === 'string' && member.current_task ? member.current_task
        : loading ? t("正在读取本次任务的实际分工…") : notStarted ? t("开始任务后，在这里查看分工。") : t("现在没在做事"));
    const historicalWarning = configurationActivity?.historyWarning;
    $(`configuration-history-${provider}`).hidden = !historicalWarning; $(`configuration-history-${provider}`).textContent = historicalWarning || '';
    renderMemberPublicProgress(snapshot, member || { id: provider });
    renderMemberQuota(provider);
    renderMemberWorkload(provider, snapshot?.workload);
    $(`session-${provider}`).textContent = member?.session_id ? `${member.identity_uncertain ? t("最后已知会话") : t("原生会话")}${interfaceColon()}${member.session_id}` : (notStarted ? t("尚未创建会话") : t("会话身份尚未返回或确认"));
    const usage = member?.usage;
    $(`usage-summary-${provider}`).textContent = usage == null ? t("任务用量快照不可用") : t("已收到任务用量快照");
    const fields = $(`usage-${provider}`);
    const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), usage ?? null]);
    if (fields.dataset.fingerprint !== fingerprint) {
      fields.dataset.fingerprint = fingerprint;
      renderUsage(fields, usage);
    }
  }
  renderProject(snapshot?.project);
  renderExecutions(snapshot?.executions);
  renderNativeExecutions(snapshot);
  const budget = objective?.budget || snapshot?.current_run?.budget || snapshot?.budget;
  $('budget-label').textContent = t("任务处理记录");
  $('budget-used').textContent = Number.isFinite(budget?.used) ? t("{p0} 次", { p0: budget.used }) : '—';
  $('objective-budget').hidden = !objective; $('objective-budget').textContent = objective ? objectiveBudgetText(objective) : '';
  const conversation = snapshot?.conversation_budget;
  $('conversation-budget').hidden = !conversation;
  $('conversation-budget').textContent = conversation
    ? t("历史累计：{p0} 轮 · {p1} 次调用", { p0: Number.isFinite(conversation.run_count) ? conversation.run_count : t("不可用"), p1: Number.isFinite(conversation.used) ? conversation.used : t("不可用") }) : '';
  const phases = Array.isArray(snapshot?.workload?.phases) ? snapshot.workload.phases.filter((row) => Number.isInteger(row?.percent)) : [];
  $('phase-load').hidden = !phases.length;
  $('phase-load').textContent = phases.length ? t("本轮消耗（按 Token 折算）：{p0}", { p0: phases.map((row) => `${phaseLabel(row.phase)} ${row.percent}%`).join(' · ') }) : '';
  renderRunHistory(snapshot);
  renderComposerUsage();
  renderNavQuota();
  renderPartnerActivity(snapshot);
  const jobs = snapshot?.jobs || [];
  $('job-count').textContent = pagedRoom(snapshot) && Number.isSafeInteger(snapshot.job_count) && snapshot.job_count > jobs.length ? t("近 {p1} / 共 {p0}", { p0: snapshot.job_count, p1: jobs.length }) : String(jobs.length);
  $('jobs-empty').hidden = jobs.length > 0;
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), jobs]);
  if (fingerprint !== state.jobsFingerprint) {
    state.jobsFingerprint = fingerprint;
    $('jobs').replaceChildren();
    for (const job of jobs) {
      const item = element('li', '', 'job');
      const heading = element('div', '', 'job-heading');
      heading.append(element('span', `${displayName(job.provider)} · ${String(job.id).slice(0, 8)}`), statusBadge(job.status));
      item.append(heading, element('p', typeof job.text === 'string' ? job.text : t("任务摘要不可用"), 'job-text'));
      if (job.text_truncated) appendBoundText(item, t("完整派发说明"), job.text_text_ref);
      if (job.error) item.append(element('p', executionMessage(job.error), 'detail-caption'));
      $('jobs').append(item);
    }
  }
}

function renderRunHistory(snapshot) {
  const records = Array.isArray(snapshot?.runs) ? snapshot.runs : [];
  const history = $('run-history');
  history.hidden = !records.length;
  $('run-count').textContent = pagedRoom(snapshot) && Number.isSafeInteger(snapshot.run_count) && snapshot.run_count > records.length ? t("共 {p0} · 相关 {p1}", { p0: snapshot.run_count, p1: records.length }) : String(records.length);
  const scope = snapshot?.room.id || '';
  if (history.dataset.room !== scope) history.open = false;
  history.dataset.room = scope;
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), records]);
  if ($('runs').dataset.fingerprint === fingerprint) return;
  $('runs').dataset.fingerprint = fingerprint;
  $('runs').replaceChildren(...records.map((run) => {
    const item = element('li', '', 'job');
    const accepted = Number.isFinite(run.accepted_at);
    const label = accepted ? t("你已接受") : Object.hasOwn(outcomeLabels, run.status) ? outcomeLabels[run.status] : statusLabels[statusClass(run.status)];
    item.append(element('strong', t("第 {p0} 轮 · {p1}", { p0: Number.isSafeInteger(run.number) ? run.number : '?', p1: label })),
      element('p', typeof run.goal === 'string' ? run.goal : t("本轮目标未记录"), 'job-text'));
    if (run.goal_truncated) appendBoundText(item, t("完整要求"), run.goal_text_ref);
    if (Number.isSafeInteger(run.revision)) item.append(element('p', t("交付文件版本 {p0}{p1}", { p0: run.revision, p1: accepted ? t(" · {p0}接受", { p0: timeLabel(run.accepted_at, true) }) : '' }), 'detail-caption'));
    return item;
  }));
}

function runBinding(run) {
  if (!run || !uuidPattern.test(run.id) || !(run.revision === null || (Number.isSafeInteger(run.revision) && run.revision >= 0))
      || typeof run.digest !== 'string' || !/^[0-9a-f]{64}$/.test(run.digest)) return null;
  return { run_id: run.id, revision: run.revision, digest: run.digest };
}

function forkBinding(snapshot) {
  const run = snapshot?.current_run;
  return run?.can_fork === true && Number.isSafeInteger(run.revision) ? runBinding(run) : null;
}

async function prepareFork() {
  if (state.busy || state.managementBusy || state.pending || !state.connected || !state.selected || !activeEntry(state.snapshot?.room)) return;
  const binding = forkBinding(state.snapshot);
  if (!binding) return;
  if (state.forkSource?.roomId === state.selected) {
    $('message').value = state.forkSource.originalText;
    state.forkSource = null;
  } else {
    state.forkSource = { roomId: state.selected, binding, originalText: $('message').value };
    if (!$('message').value.trim()) {
      const source = state.snapshot; const roomId = state.selected;
      try {
        const ref = source.current_run?.goal_text_ref || source.room.goal_text_ref;
        const text = ref ? await readExactText(ref) : source.current_run?.goal || source.room.goal || '';
        if (state.selected !== roomId || state.snapshot?.current_run?.id !== source.current_run?.id || $('message').value.trim()) return;
        $('message').value = text;
      } catch (error) { if (state.selected === roomId) { state.forkSource = null; showNotice(error.message, 'error'); } return; }
    }
  }
  renderCollaboration(state.snapshot);
  renderControls();
  $('message').focus({ preventScroll: true });
}

async function submitFork(text) {
  if (blockedParticipants().length) { showNotice(availabilityBlockMessage(), 'error'); return; }
  const source = state.forkSource;
  if (state.managementBusy || !activeEntry(state.snapshot?.room) || !source || source.roomId !== state.selected || !forkBinding(state.snapshot)) return;
  state.pending = capturePendingIntent({ kind: 'fork', roomId: source.roomId,
    body: { text, target: state.target, request_id: crypto.randomUUID(), ...source.binding } });
  await submitPending();
}

async function buildFromLegacyPlan() {
  if (blockedParticipants().length) { showNotice(availabilityBlockMessage(), 'error'); return; }
  if (state.busy || state.managementBusy || state.pending || !activeEntry(state.snapshot?.room) || !state.connected || !state.taskModes.includes('text_artifacts')) return;
  const snapshot = state.snapshot;
  const run = snapshot?.current_run;
  if (run ? run.work_mode !== 'plan' || run.can_continue !== true || !runBinding(run)
    : snapshot?.room.work_mode !== 'plan' || snapshot.room.status !== 'completed'
      || snapshot.jobs.some((job) => ['queued', 'working'].includes(job.status))) return;
  const excerpts = deliveryDocuments(snapshot).map((doc) => `${doc.title}\n${doc.content}`).join('\n\n').slice(0, 1800);
  const goal = t("按以下方案实际制作并交付文件。需要时分工、求助并互审；不要只再写方案。\n原目标：{p0}\n方案摘录（摘录外内容未带入）：\n{p1}", { p0: (run?.goal || snapshot.room.goal).slice(0, 700), p1: excerpts });
  state.pending = capturePendingIntent({ kind: 'create', roomId: null, body: { goal, mode: 'text_artifacts', work_mode: 'build',
    target: state.target === 'all' ? state.participantIds[0] : state.target, request_id: crypto.randomUUID(),
    participants: [...state.participantIds], ...(runtimePayload() ? { runtime_settings: runtimePayload() } : {}), ...(state.participantIds.length >= 2 ? { collaboration: true } : {}) } });
  await submitPending();
}

async function submitRunAction(kind, { retry = false, text, workMode = state.workMode } = {}) {
  const intentSnapshot = state.snapshot;
  if (kind === 'run' && (policySubmissionError() || runtimeSubmissionError())) { showNotice(policySubmissionError() || runtimeSubmissionError(), 'error'); return; }
  if (kind === 'run' && submissionAvailabilityBlocked()) { showNotice(submissionAvailabilityMessage(), 'error'); return; }
  if (state.busy || state.managementBusy || state.pending || !state.connected || !state.selected || !activeEntry(state.snapshot?.room)) return;
  const run = state.snapshot?.current_run;
  const binding = runBinding(run);
  const allowed = kind === 'accept' ? run?.can_accept : retry ? run?.can_retry : run?.can_continue;
  if (!binding || allowed !== true) return;
  const body = { request_id: crypto.randomUUID(), ...binding };
  if (kind === 'run') {
    let original = text;
    if (!original && retry) {
      try { original = run.goal_text_ref ? await readExactText(run.goal_text_ref) : run.goal; }
      catch (error) { showNotice(error.message, 'error'); return; }
      if (state.snapshot?.current_run?.id !== run.id || state.pending || state.busy) return;
    }
    body.text = (original || '').trim();
    if (!body.text || !checkUserText(body.text)) return;
    body.target = state.target; body.retry = retry;
    body.work_mode = workMode;
    const runtime = runtimePayload(); if (runtime) body.runtime_settings = runtime;
    const policy = executionPolicyPayload(); if (policy) body.execution_policy = policy;
  }
  state.pending = capturePendingIntent({ kind, roomId: state.selected, body }, intentSnapshot);
  await submitPending();
}

function renderProject(project) {
  $('project-details').hidden = !project;
  if (!project) { $('project-files').replaceChildren(); state.projectFingerprint = ''; return; }
  // The room state sends file names only (content_available); a delivered file opens in the reader instead.
  const readable = state.snapshot ? deliveryDocuments(state.snapshot).filter((doc) => doc.file) : [];
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), project, readable.map((doc) => doc.key)]);
  if (fingerprint === state.projectFingerprint) return;
  state.projectFingerprint = fingerprint;
  $('project-files').replaceChildren();
  const folder = project.kind === 'folder' || project.format === 'beanest-folder-project-v1';
  $('download-artifacts').textContent = folder ? t("下载已修改文件") : t("下载成果");
  $('project-status').textContent = project.available
    ? t("{p0}已保存版本 {p1} · {p2}{p3}", { p0: project.source?.name ? `${project.source.name} · ` : '', p1: project.revision, p2: folder ? t("项目共 {p0} 个文件 · 已修改文件 {p1} 个 · ", { p0: Number.isSafeInteger(project.total_files) ? project.total_files : t("未知"), p1: project.files?.length || 0 }) : '', p3: project.source ? t("改稿保存在豆窝，不会改动原项目，暂不运行。") : (project.execution === 'sandboxed_unittest' ? t("执行情况见隔离执行记录。") : t("未执行代码或测试。")) })
    : t("代码文件暂不可读取，请检查本地记录；不会自动新建替代版本。");
  if (state.snapshot?.room.mode === 'native_project') $('project-status').textContent = project.available
    ? t("开发副本 · 已保存版本 {p0} · {p1} 个已保存改动。本机工具按任务要求和所选原生权限执行，具体结果见记录。", { p0: project.revision, p1: project.files?.length || 0 })
    : t("开发副本的文件暂不可读取，请检查记录，不会自动替换。");
  if (!project.available || !Array.isArray(project.files)) return;
  if (folder) {
    const labels = { gitignore: t("忽略规则"), excluded_directory: t("缓存/构建目录"), sensitive_path: t("凭据路径"),
      sensitive_content: t("敏感内容"), symlink: t("符号链接"), invalid_path: t("不支持的路径"), special_file: t("特殊文件") };
    const excluded = project.excluded;
    if (excluded && typeof excluded === 'object' && !Array.isArray(excluded)) {
      const counts = [];
      let other = 0;
      for (const [reason, count] of Object.entries(excluded)) {
        if (!Number.isSafeInteger(count) || count <= 0) continue;
        if (Object.hasOwn(labels, reason)) counts.push(t("{p0} {p1} 项", { p0: labels[reason], p1: count }));
        else other += count;
      }
      if (other) counts.push(t("其它排除项 {p0} 项", { p0: other }));
      $('project-files').append(element('p', t("跳过：{p0}（目录按一项计）。", { p0: counts.length ? interfaceList(counts) : t("0 项") }), 'detail-caption'));
    } else {
      $('project-files').append(element('p', t("跳过项统计暂不可用；目录按一项计，不推算目录内文件数量。"), 'detail-caption'));
    }
  }
  for (const file of project.files) {
    const details = element('details', '', 'project-file');
    details.append(element('summary', `${file.path} · ${!file.owner || file.owner === 'source' ? t("项目原稿") : displayName(file.owner)}`),
      element('p', t("版本 {p0} · SHA256 {p1}", { p0: project.revision, p1: file.sha256 }), 'detail-caption'));
    if (typeof file.content === 'string') details.append(element('pre', file.content, 'file-content'));
    else {
      const doc = readable.find((entry) => entry.path === file.path && entry.sha256 === file.sha256);
      if (doc) {
        const open = element('button', t("打开阅读"), 'quiet-button'); open.type = 'button'; open.dataset.openProjectFile = file.path;
        open.addEventListener('click', () => openDelivery(doc.key, open));
        details.append(open);
      } else details.append(element('p', !exportableProject(state.snapshot) ? t("这里不直接显示正文。")
        : t("这里不直接显示正文，点上面的“{p0}”可以拿到全部文件。", { p0: $('download-artifacts').textContent }), 'detail-caption'));
    }
    if (file.path.endsWith('.svg') && typeof file.sha256 === 'string') {
      const preview = element('button', t("预览矢量图"), 'quiet-button'); preview.type = 'button';
      const holder = element('div', '', 'svg-preview');
      const roomId = state.snapshot?.room.id;
      preview.addEventListener('click', () => void previewSvg(preview, holder, { roomId, revision: project.revision, file }));
      details.append(preview, holder);
    }
    $('project-files').append(details);
  }
  if (project.source) {
    $('project-files').append(element('p', t("来源目录：{p0}", { p0: project.source.path || t("不可用") }), 'detail-caption'));
    for (const change of Array.isArray(project.changes) ? project.changes : []) {
      const details = element('details', '', 'project-file');
      details.append(element('summary', `${change.path} · ${change.status || t("变更待核对")}`), element('pre', typeof change.diff === 'string' ? change.diff : t("差异不可用"), 'file-content'));
      $('project-files').append(details);
    }
  }
}

async function previewSvg(button, holder, { roomId, revision, file }) {
  if (button.disabled || !uuidPattern.test(roomId) || !Number.isSafeInteger(revision)) return;
  button.disabled = true;
  holder.replaceChildren(element('p', t("正在核对静态矢量图…"), 'detail-caption'));
  try {
    const preview = await api(`${roomPath(roomId)}/preview?revision=${revision}&path=${encodeURIComponent(file.path)}`);
    if (preview.content_type !== 'image/svg+xml' || preview.source_sha256 !== file.sha256
        || !/^[0-9a-f]{64}$/.test(preview.sha256 || '') || typeof preview.content_base64 !== 'string'
        || preview.content_base64.length > 256 * 1024 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(preview.content_base64)) throw new Error('unconfirmed_preview');
    const bytes = Uint8Array.from(atob(preview.content_base64), (character) => character.charCodeAt(0));
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    const actual = [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('');
    if (actual !== preview.sha256) throw new Error('unconfirmed_preview');
    const img = element('img'); img.alt = t("{p0} 的静态矢量图预览", { p0: file.path });
    img.src = `data:image/svg+xml;base64,${preview.content_base64}`;
    holder.replaceChildren(img, element('p', t("静态矢量图预览；源文件可在上方核对。"), 'detail-caption'));
  } catch (_error) {
    holder.replaceChildren(element('p', t("这份 SVG 暂不能安全预览，可查看源码；已交付文件仍保留。"), 'detail-caption'));
  } finally { button.disabled = false; }
}

function exportableProject(snapshot) {
  const project = snapshot?.project;
  return project?.available === true && Number.isSafeInteger(project.revision) && project.revision >= 0
    && /^[0-9a-f]{64}$/.test(project.snapshot_sha256 || '') && Array.isArray(project.files) && project.files.length > 0;
}

function renderExportControls() {
  for (const id of ['download-artifacts', 'download-delivery']) {
    const button = $(id);
    if (button) button.disabled = state.exportBusy || state.busy || !!state.pending || !state.connected || !exportableProject(state.snapshot);
  }
  const note = state.exportRoom === state.selected ? state.exportMessage : '';
  $('export-note').hidden = !note;
  $('export-note').textContent = note;
}

function validExportBase64(value) {
  // Validate the short padded tail separately; repeating a four-character
  // regex group can exhaust the browser stack on a valid multi-MiB archive.
  return value.length > 0 && value.length % 4 === 0
    && !/[^A-Za-z0-9+/]/.test(value.slice(0, -4))
    && /^(?:[A-Za-z0-9+/]{4}|[A-Za-z0-9+/]{3}=|[A-Za-z0-9+/]{2}==)$/.test(value.slice(-4));
}

async function downloadProject() {
  if (state.exportBusy || state.busy || state.pending || !state.connected || !exportableProject(state.snapshot)) return;
  const snapshot = state.snapshot;
  const project = snapshot.project;
  state.exportBusy = true; state.exportRoom = snapshot.room.id; state.exportMessage = t("正在核对固定版本并准备下载…");
  renderExportControls();
  try {
    const result = await api(`${roomPath(snapshot.room.id)}/export?revision=${project.revision}`);
    if (state.deletedRooms.has(snapshot.room.id)) return;
    // Folder exports contain changed files only. Allow the bounded 16 MiB
    // content plus 8 MiB manifest/ZIP overhead after base64 encoding.
    const folder = project.kind === 'folder';
    const maxEncodedBytes = folder ? 32 * 1024 * 1024 : 256 * 1024;
    const maxFiles = folder ? 20000 : 16;
    if (result.revision !== project.revision || result.snapshot_sha256 !== project.snapshot_sha256
        || !Number.isSafeInteger(result.files_count) || result.files_count < 1 || result.files_count > maxFiles
        || result.files_count !== project.files.length || result.content_type !== 'application/zip'
        || typeof result.filename !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]*\.zip$/.test(result.filename)
        || !/^[0-9a-f]{64}$/.test(result.sha256 || '') || typeof result.content_base64 !== 'string'
        || result.content_base64.length > maxEncodedBytes || !validExportBase64(result.content_base64)) {
      throw new RequestError(t("下载内容与选定文件版本不一致，未保存文件。"));
    }
    const bytes = Uint8Array.from(atob(result.content_base64), (character) => character.charCodeAt(0));
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    const actual = [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('');
    if (actual !== result.sha256 || bytes[0] !== 0x50 || bytes[1] !== 0x4b) throw new RequestError(t("下载文件完整性核对失败，未保存文件。"));
    if (state.deletedRooms.has(snapshot.room.id)) return;
    const url = URL.createObjectURL(new Blob([bytes], { type: result.content_type }));
    const anchor = element('a');
    try {
      anchor.href = url; anchor.download = result.filename;
      document.body.append(anchor); anchor.click();
    } finally { anchor.remove(); URL.revokeObjectURL(url); }
    state.exportMessage = t("已准备版本 {p0} 的下载。文件保存在你选择的位置，不会回写原项目。", { p0: result.revision });
  } catch (error) {
    state.exportMessage = error instanceof RequestError ? error.message : t("下载未完成，请核对本地连接后重试。");
  } finally { state.exportBusy = false; renderExportControls(); }
}

function renderExecutions(executions) {
  const records = Array.isArray(executions) ? executions : [];
  $('executions-details').hidden = !records.length;
  const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), records]);
  if (fingerprint === state.executionFingerprint) return;
  state.executionFingerprint = fingerprint;
  $('execution-records').replaceChildren();
  const labels = { queued: t("等待隔离运行"), working: t("隔离运行中"), completed: t("进程已结束"),
    failed: t("运行失败"), timed_out: t("已超时"), output_limited: t("输出超出上限"), cancelled: t("已取消"),
    interrupted: t("已中断，不重跑"), stale: t("对应旧版本"), unavailable: t("环境不可用"), unknown: t("结果待确认") };
  for (const record of records) {
    const details = element('details', '', 'project-file');
    const result = record.result_summary || record.result;
    details.append(element('summary', t("版本 {p0} · {p1}", { p0: record.revision, p1: labels[record.status] || t("状态待确认") })));
    details.append(element('p', t("运行 {p0} · SHA256 {p1}", { p0: record.id, p1: record.snapshot_sha256 }), 'detail-caption'));
    if (result) {
      details.append(element('p', t("退出码 {p0} · {p1}", { p0: Number.isInteger(result.exit_code) ? result.exit_code : t("不可用"), p1: result.cleanup_confirmed ? t("已确认清理") : t("清理待确认") }), 'detail-caption'));
      if (!record.result_summary) details.append(element('p', t("以下为程序输出，需核对{p0}。", { p0: result.truncated ? t("；内容已截短") : '' }), 'detail-caption'));
      if (!record.result_summary) details.append(element('pre', `${result.stdout || ''}${result.stderr ? `\n${result.stderr}` : ''}`, 'file-content'));
      else appendBoundText(details, t("完整执行记录"), record.result_text_ref);
    }
    $('execution-records').append(details);
  }
}

function renderNativeWorkspaceNotes(container, snapshot) {
  const workspaces = Array.isArray(snapshot.native_workspaces) ? snapshot.native_workspaces : [];
  const issues = workspaces.flatMap((workspace) => [
    ...(Array.isArray(workspace.conflicts) ? workspace.conflicts.map((item) => ({ ...item, provider: workspace.provider, conflict: true })) : []),
    ...(Array.isArray(workspace.blocked) ? workspace.blocked.map((item) => ({ ...item, provider: workspace.provider })) : []),
  ]);
  if (!issues.length) return;
  const details = element('details', '', 'native-workspace-issues');
  details.append(element('summary', t("有 {p0} 项改动需要处理，尚未纳入交付", { p0: issues.length })));
  const reasons = { native_binary_not_imported: t("二进制改动尚未导入"), native_deletion_not_imported: t("删除操作尚未导入"),
    native_path_not_owned: t("超出本任务授权写入范围"), native_symlink_refused: t("符号链接未导入"), native_mode_change_not_imported: t("文件权限改动尚未导入"),
    native_change_too_large: t("改动超过本次导入范围"), native_merge_conflict: t("文件版本冲突"), native_unresolved_conflict: t("旧版本冲突尚未明确解决，改动未导入") };
  const list = element('ul');
  for (const item of issues) list.append(element('li', `${displayName(item.provider)} · ${typeof item.path === 'string' ? item.path : t("文件路径未提供")}${interfaceColon()}${item.conflict ? t("需核对新旧版本冲突") : reasons[item.reason] || t("改动未导入，原因待核对")}`));
  details.append(list, element('p', t("这些改动保留为待处理项，不表示已合入成果或完成目标。"), 'detail-caption')); container.append(details);
}
function renderNativeExecutions(snapshot) {
  const records = Array.isArray(snapshot?.native_executions) ? snapshot.native_executions : [];
  $('native-executions-details').hidden = !records.length;
  const container = $('native-execution-records'); const fingerprint = JSON.stringify([window.BeanestI18n.getLocale(), [snapshot?.room.id, records]]);
  if (container.dataset.fingerprint === fingerprint) return;
  container.dataset.fingerprint = fingerprint; container.replaceChildren();
  for (const record of records) {
    const details = element('details', '', 'project-file');
    const count = (value) => Number.isSafeInteger(value) && value >= 0 ? String(value) : t("未提供");
    details.append(element('summary', `${displayName(record.provider)} · ${record.status === 'working' ? t("本机处理中") : record.status === 'completed' ? t("本机进程已结束") : t("本机处理结果待核对")}`),
      element('p', t("权限：{p0} · 工具调用 {p1} 次 · 工具返回 {p2} 次", { p0: runtimePermissionName(record.permission), p1: count(record.tool_calls), p2: count(record.tool_results) }), 'detail-caption'),
      element('p', t("退出码 {p0} · {p1}", { p0: Number.isInteger(record.exit_code) ? record.exit_code : t("未提供"), p1: record.cleanup_confirmed === true ? t("已确认清理") : t("清理尚未确认") }), 'detail-caption'),
      element('p', record.report_trust === 'native_tool_observation' ? t("来源：本机工具实际记录。它不等于测试、游戏操作或目标已验收。") : t("这条记录的来源尚未确认。"), 'detail-caption'));
    if (record.failure_diagnostic?.reason === 'provider_quota_exhausted') {
      details.append(element('p', t("供应商已明确报告订阅额度用完。已有工具操作不等于任务完成；请查看当前续作入口和额度恢复时间。"), 'detail-caption'));
    }
    if (Number.isFinite(record.duration_seconds) && record.duration_seconds >= 0) details.append(element('p', t("本次处理 {p0} 秒", { p0: record.duration_seconds }), 'detail-caption'));
    container.append(details);
  }
}

function terminalTeamState(snapshot) {
  if (snapshot?.current_run?.status === 'stopped') return 'stopped';
  const outcome = snapshot?.collaboration?.mode === 'team' ? snapshot.collaboration.outcome?.state : null;
  const phase = snapshot?.team?.phase;
  if (outcome === 'unknown' || phase === 'unknown') return 'unknown';
  if (outcome === 'stopped' || ['stopped', 'cancelled', 'interrupted'].includes(phase)) return 'stopped';
  return null;
}

function roomNeedsStop(snapshot) {
  if (!snapshot?.room || !activeEntry(snapshot.room)) return false;
  const runId = snapshot.current_run?.id || snapshot.team?.run_id;
  const currentOrUnbound = (recordRun) => !runId || !recordRun || recordRun === runId;
  const jobIsCurrentOrUnbound = (job) => {
    if (!runId) return true;
    if (pagedRoom(snapshot)) return !job.run_id || job.run_id === runId;
    const runs = (snapshot.events || []).filter((event) => ['job_created', 'job_claimed'].includes(event.kind)
      && event.job_id === job.id && event.run_id).map((event) => event.run_id);
    return !runs.length || runs.includes(runId);
  };
  // Real queued/working records remain stoppable across run boundaries. Only
  // explicitly older unresolved receipts are excluded from the current control.
  const inFlight = (snapshot.jobs || []).some((job) => ['queued', 'working'].includes(job.status)
      || job.status === 'unknown' && jobIsCurrentOrUnbound(job))
    || (snapshot.executions || []).some((record) => ['queued', 'working'].includes(record.status)
      || currentOrUnbound(record.team_run_id) && (record.status === 'unknown' || (record.result_summary || record.result)?.cleanup_confirmed === false))
    || (snapshot.native_executions || []).some((record) => currentOrUnbound(record.run_id) && record.cleanup_confirmed === false);
  const stateUnknown = snapshot.team?.phase === 'unknown' || snapshot.collaboration?.outcome?.state === 'unknown';
  if (inFlight || snapshot.cancellation_confirmed === false || stateUnknown) return true;
  if (snapshot.cancellation_confirmed === true) return false;
  const objective = currentObjective(snapshot);
  if (objective?.status === 'complete' || snapshot.room.status === 'completed' || ['ready_for_user', 'accepted'].includes(snapshot.collaboration?.outcome?.state)) return false;
  return true;
}

function renderWorkIndicator(snapshot) {
  const box = $('work-indicator'); const text = $('work-indicator-text');
  const objective = currentObjective(snapshot);
  const unavailable = !state.connected || !snapshot || !!state.reader || !activeEntry(snapshot.room)
    || ['needs_user', 'complete', 'stopped', 'interrupted'].includes(objective?.status)
    || !objective && ['paused', 'needs_user', 'completed', 'accepted'].includes(snapshot?.room?.status);
  const jobs = unavailable ? [] : (pagedRoom(snapshot) ? snapshot.live?.jobs || [] : snapshot.jobs || []);
  const working = jobs.filter((job) => job.status === 'working');
  const queued = objective?.status === 'paused' ? [] : jobs.filter((job) => job.status === 'queued');
  const jobText = (job) => {
    const name = displayName(job.provider);
    const phase = job.phase || job.team?.job_phase;
    // Whole sentences: "Kimi Code Working" read as a label in English.
    return objective?.status === 'paused' || objective?.pause_requested ? t("{p0} 正在结束当前处理", { p0: name })
      : job.native_partial_inspection || job.partner_handoff_inspection ? t("{p0} 正在核对上次进度", { p0: name })
        : ['checkpoint', 'checkpoint_review'].includes(phase) ? t("{p0} 正在核对下一步", { p0: name })
          : job.native_readonly_discussion ? t("{p0} 正在讨论", { p0: name }) : t("{p0} 正在处理", { p0: name });
  };
  const value = working.length ? working.map(jobText).join(' · ') : queued.length
    ? t("{p0} 等待开始", { p0: interfaceList([...new Set(queued.map((job) => displayName(job.provider)))]) }) : '';
  box.hidden = !value; box.classList.toggle('is-working', working.length > 0);
  // Polling may update timing elsewhere, but only a real phase/name transition
  // changes this polite live region. The animated dots carry no spoken content.
  if (text.textContent !== value) text.textContent = value;
}
function renderControls() {
  renderActionError();
  const room = state.snapshot?.room;
  const objective = currentObjective();
  const loading = !!state.selected && !room;
  const inactive = !!room && !activeEntry(room);
  const blocked = state.busy || !!state.pending || state.managementBusy;
  const title = room ? roomDisplayTitle(room) : (loading ? roomTitle(state.rooms.find((entry) => entry.id === state.selected)) : t("新任务"));
  const headerGoal = typeof (objective?.original_goal ?? room?.goal) === 'string' ? (objective?.original_goal ?? room.goal).trim().replace(/\s+/g, ' ') : '';
  const headerSentence = headerGoal.split(/[。！？\n]/u)[0] || headerGoal;
  $('room-title').textContent = room && headerSentence ? headerSentence.length > 200 ? `${headerSentence.slice(0, 200)}…` : headerSentence : title;
  $('room-title').title = objective?.original_goal || room?.goal || title;
  const runCaption = Number.isSafeInteger(state.snapshot?.current_run?.number) ? t("第 {p0} 轮 · ", { p0: state.snapshot.current_run.number }) : '';
  const displayedWorkMode = actualWorkMode();
  const workModeCaption = displayedWorkMode === 'plan' ? t("先聊方案") : displayedWorkMode === 'build' ? t("直接动手") : t("历史方式");
  $('room-subtitle').textContent = loading ? t("正在读取已保存的记录，不会重新启动模型。") : (room ? t("{p0}{p1} · {p2}创建 · {p3}", { p0: runCaption, p1: workModeCaption, p2: timeLabel(room.created_at, true), p3: state.snapshot?.collaboration?.mode === 'team' ? t("分工、讨论与交付都在这里") : state.snapshot?.members.length === 1 ? t("单伙伴任务；打开只读取，不会重跑") : t("历史记录；打开只读取，不会重跑") }) : t("选工具，写下要做的事。"));
  $('room-status').hidden = !room;
  $('room-lifecycle-menu').hidden = !room;
  $('room-more').hidden = !room;
  const lifecycleFingerprint = JSON.stringify([window.BeanestI18n.getLocale(), [room?.id, lifecycleState(room), room?.status, room?.lifecycle_can_change, room?.lifecycle_blocked_reason, state.managementBusy, state.busy, !!state.pending,
    state.roomLifecycleSupported, state.lifecycleOperations.get(room?.id), state.connected, state.executionPolicySupported, state.snapshot?.current_run?.can_reassess_objective, runBinding(state.snapshot?.current_run), objective?.id,
    state.snapshot?.jobs.map((job) => [job.id, job.status])]]);
  if ($('room-lifecycle-actions').dataset.fingerprint !== lifecycleFingerprint) {
    $('room-lifecycle-actions').dataset.fingerprint = lifecycleFingerprint;
    const actions = room ? lifecycleActions('rooms', room) : null;
    if (actions && canReassessObjective()) {
      const reassess = element('button', t("重新核对目标"), 'quiet-button'); reassess.type = 'button'; reassess.id = 'reassess-objective';
      reassess.title = t("沿用原目标与权限，重新核对完成情况；不重放旧调用或代你接受。");
      reassess.addEventListener('click', () => void submitObjectiveReassessment(state.snapshot)); actions.append(reassess);
    }
    $('room-lifecycle-actions').replaceChildren(...(actions ? [actions] : []));
  }
  $('room-lifecycle-note').hidden = !inactive;
  $('room-lifecycle-note').textContent = inactive ? t("这份任务{p0}，聊天和交付仍可阅读。点“管理任务 → 恢复”后可以继续。", { p0: lifecycleLabels[lifecycleState(room)] }) : '';
  const teamStopped = terminalTeamState(state.snapshot);
  const outcomeState = teamStopped || state.snapshot?.collaboration?.outcome?.state;
  const teamDelivered = state.snapshot?.collaboration?.mode === 'team' && outcomeState === 'ready_for_user';
  const currentRun = state.snapshot?.current_run;
  const canAnswer = objective?.status === 'needs_user' && objective.can_answer === true;
  if (objective) $('room-subtitle').textContent = `${Number.isSafeInteger(objective.stage) ? t("第 {p0} 阶段 · ", { p0: objective.stage }) : ''}${objective.mode === 'autonomous' ? t("自主推进") : t("逐步推进")} · ${workModeCaption}`;
  if (idleReadOnlyDiscussion()) $('room-subtitle').textContent = t("{p0} · 原目标与进度保留", { p0: readOnlyLabel() });
  $('source-plan-note').hidden = !currentRun?.source_plan;
  $('source-plan-note').textContent = currentRun?.source_plan?.truncated
    ? t("方案较长，这轮带上了部分内容。完整方案仍在上一轮，遇到未带入的细节需要再核对。")
    : currentRun?.source_plan ? t("已带上上一轮交付的方案，不含完整讨论。聊天记录仍保留在这里。") : '';
  const preparingFork = state.forkSource?.roomId === room?.id && !!forkBinding(state.snapshot);
  const natural = naturalContinuation();
  const continuation = continuationAction();
  const canContinue = !objective && !inactive && !teamStopped && !!runBinding(currentRun) && currentRun.can_continue === true;
  const configuration = naturalRuntimeConfiguration();
  const currentWorkMode = configuration ? room?.work_mode : currentRun?.work_mode || room?.work_mode || 'legacy';
  const modeEditable = room && state.naturalRuntimeSupported ? configuration?.can_edit === true : !room || canContinue || currentRun?.can_start_suggested_goal === true;
  $('work-mode-label').textContent = configuration ? t("后续工作") : room ? (currentRun?.can_start_suggested_goal === true ? t("新目标怎么做") : canContinue ? t("下一轮怎么做") : t("本轮方式")) : t("这次怎么做");
  for (const value of ['plan', 'build']) {
    const button = $(`work-mode-${value}`);
    button.disabled = blocked || inactive || !state.connected || !modeEditable;
    button.setAttribute('aria-pressed', String((modeEditable ? state.workMode : currentWorkMode) === value));
  }
  $('work-mode-note').textContent = teamStopped ? t("本轮已停止，已有内容保留。") : room && !modeEditable
    ? currentWorkMode === 'plan' ? t("正在先聊方案，这轮不会修改文件。") : currentWorkMode === 'build' ? t("正在按目标推进，完成后给你看成果。") : t("这份旧记录没有区分方案与动手模式。")
    : state.workMode === 'plan' ? `${t("先讨论和查资料，不修改文件。")}${interfaceWordSpace()}${planNextStepText()}`
      : t("直接分工并制作成果，遇到需要你决定的事再问你。");
  if (room && canContinue) $('work-mode-note').textContent += t(" 选择只影响下一轮。");
  if (objective?.status === 'paused') $('work-mode-note').textContent = sidebarObjectiveLabel({ ...room, objective_status: 'paused', objective_pause_requested: objective.pause_requested }) === t("正在暂停")
    ? t("正在暂停，等当前处理结束。") : t("已暂停，继续后接着做。");
  else if (objective?.status === 'complete') $('work-mode-note').textContent = t("本目标已交付，无需确认；有新要求可直接发送消息。");
  else if (objective?.status === 'awaiting_continue') $('work-mode-note').textContent = t("点“继续”接着完成原目标，无需先确认交付。");
  else if (objective?.status === 'needs_user') $('work-mode-note').textContent = canAnswer ? t("回答当前问题，沿原目标继续。") : t("请先查看需要你决定的问题；在你回复前不会重新开始。");
  else if (objective && ['stopped', 'interrupted'].includes(objective.status)) $('work-mode-note').textContent = state.continuousObjectiveSupported && currentRun?.can_restart_objective === true
    ? t("点“继续”接着做，已有成果和记录保留。") : t("本目标已停下，记录与成果保留；不会自动重新开始。");
  $('room-status').textContent = room ? (inactive ? lifecycleLabels[lifecycleState(room)] : !teamStopped && Number.isFinite(currentRun?.accepted_at) ? t("本轮已接受") : Object.hasOwn(outcomeLabels, outcomeState) ? outcomeLabels[outcomeState] : statusLabels[statusClass(room.status)]) : '';
  if (objective && !inactive) $('room-status').textContent = objective.status === 'complete' && Number.isFinite(currentRun?.accepted_at) ? t("你已确认完成") : objectiveLabel(objective);
  if (!inactive && idleReadOnlyDiscussion()) $('room-status').textContent = readOnlyLabel();
  $('room-status').className = `status-badge ${statusClass(room?.status)}`;
  $('new-room').disabled = blocked && !canDetachPendingRequest();
  $('refresh').disabled = state.busy || state.refreshing;
  $('cancel-room').hidden = !roomNeedsStop(state.snapshot);
  $('cancel-room').disabled = !room || inactive || !state.connected || state.stopRequests.has(room.id);
  // A stop the host resumes by itself (a quota wait, an automatic continue) can be held too.
  const resumesItself = ['auto', 'quota_wait'].includes(state.snapshot ? stopKind(state.snapshot, objective?.stop_reason) : null);
  $('pause-objective').hidden = !objective || inactive || !(['running', 'checkpoint', 'paused'].includes(objective.status) || resumesItself)
    || !!natural && objective.status === 'paused' && objective.pause_requested !== true;
  $('pause-objective').textContent = objective?.status === 'paused' ? objective.pause_requested === true ? t("正在暂停…") : t("继续推进") : t("暂停推进");
  $('pause-objective').title = objective?.status === 'paused' ? t("沿原目标恢复，权限保持") : t("当前处理结束后暂停，不再派发下一项");
  $('pause-objective').disabled = blocked || !state.connected || objective?.pause_requested === true;
  if (natural && objective?.status !== 'paused') $('work-mode-note').textContent = objective?.status === 'complete' ? isReadOnlyDiscussion() ? t("讨论已结束，可以接着聊；切到动手并发送后继续开发。已有改动保持。") : t("本目标已交付，有修改或新要求可以直接在这里说。") : t("可随时补充要求，伙伴会接着处理。");
  else if (continuation) $('work-mode-note').textContent = t("接着完成原任务，已有成果和记录保留。");
  if (idleReadOnlyDiscussion()) $('work-mode-note').textContent = t("现在没在做事。可以接着讨论，或切到“动手”开工。");
  if (configuration && (configuration.pending || Object.keys(pendingRuntimeChanges()).length)) $('work-mode-note').textContent = configuration.pending && !Object.keys(pendingRuntimeChanges()).length
    ? t("设置已保存，后续工作开始生效。当前调用不变。") : t("新设置随下一条消息提交，当前调用不变。");
  const canCompose = !inactive && !blocked && !loading && state.connected && state.participantIds.length > 0
    && (natural || continuation || (objective ? canAnswer || preparingFork || ['running', 'checkpoint'].includes(objective.status) && !teamStopped && !closedStatuses.has(room?.status)
      : preparingFork || canContinue || (!teamStopped && !teamDelivered && !closedStatuses.has(room?.status))));
  const mode = room?.mode || state.mode;
  const nativeMode = mode === 'native_project';
  const projectMode = mode === 'project_files' || mode === 'project_folder' || nativeMode;
  const textArtifacts = mode === 'text_artifacts';
  const artifactMode = mode === 'python_artifacts' || mode === 'python_sandbox' || textArtifacts || projectMode;
  const sandboxMode = mode === 'python_sandbox';
  $('new-room-setup').hidden = !!state.selected;
  $('team-setup-note').hidden = !!state.selected;
  $('team-setup-note').textContent = state.participantIds.length >= 2
    ? t("先由谁整理分工？伙伴会认领任务、按需要求助并交叉检查。需要你决定或遇到阻碍时，会在聊天中说明。")
    : t("当前选了一个工具，将直接处理你的目标。可再邀请伙伴共同拆分与互审。");
  const selectedProject = state.projects.find((project) => project.id === state.selectedProject);
  $('project-choice-label').textContent = selectedProject ? t("项目：{p0}{p1}", { p0: selectedProject.name, p1: nativeMode ? t(" · 完整开发") : t(" · 隔离修改，原项目保持") }) : t("关联项目（可选）");
  $('project-workflow-control').hidden = !!room || !state.nativeProjectSupported || selectedProject?.selection_kind !== 'folder';
  $('project-workflow').value = nativeMode ? 'native_project' : 'project_folder'; $('project-workflow').disabled = blocked || !!room || !state.connected;
  $('frozen-participants').hidden = !state.selected;
  $('frozen-participants').textContent = t("当前伙伴：{p0}{p1}", { p0: interfaceList(state.participantIds.map(displayName)), p1: state.memberInvitationSupported ? '' : t(" · 这份记录的成员固定") });
  renderRoomMemberInvitation();
  $('task-options-partners').textContent = t("伙伴 · {p0}", { p0: state.participantIds.length });
  const linkedProject = room ? state.projects.find((project) => project.id === room.project_id) : selectedProject;
  // A permanently deleted project leaves the room's own copy; use the name the room recorded and say it was deleted.
  const recordedName = typeof state.snapshot?.project?.source?.name === 'string' && state.snapshot.project.source.name.trim() ? state.snapshot.project.source.name.trim() : '';
  const projectDeleted = !!room?.project_id && !linkedProject && state.projectsAccepted === true;
  const roomProjectName = linkedProject?.name || recordedName;
  const deletedLabel = roomProjectName ? t("{p0}（已删除）", { p0: roomProjectName }) : t("项目已删除");
  $('task-options-project').textContent = linkedProject ? linkedProject.name : room?.project_id ? projectDeleted ? deletedLabel : roomProjectName || t("关联项目") : t("项目");
  $('room-project-chip').hidden = !room?.project_id;
  $('room-project-chip').textContent = room?.project_id ? linkedProject?.name || (projectDeleted ? deletedLabel : roomProjectName || t("关联项目")) : '';
  $('task-options-project').title = room ? room.project_id ? projectDeleted
    ? t("关联的项目“{p0}”已从豆窝删除。原项目文件不受影响，这个任务里的副本和成果还在。", { p0: roomProjectName || t("未记录名称") })
    : t("本任务关联项目：{p0}（固定）", { p0: roomProjectName || t("记录未找到") }) : t("本任务没有关联项目")
    : selectedProject ? t("关联项目：{p0}", { p0: selectedProject.name }) : t("选择关联项目");
  $('frozen-project').hidden = !room;
  $('frozen-project').textContent = !room?.project_id ? t("本任务没有关联项目。") : projectDeleted
    ? t("关联的项目“{p0}”已从豆窝删除。原项目文件不受影响，这个任务里的副本和成果还在。", { p0: roomProjectName || t("未记录名称") })
    : t("关联项目：{p0} · 本任务项目固定。", { p0: roomProjectName || t("项目记录未找到") });
  for (const id of ['task-options-partners', 'task-options-project', 'task-options-more']) $(id).disabled = blocked || loading;
  $('project-picker').disabled = blocked || !state.connected;
  $('composer-projects').disabled = blocked;
  const hasArtifactModes = state.taskModes.includes('python_artifacts') || state.taskModes.includes('text_artifacts');
  $('advanced-execution').hidden = !!state.selected || !!state.selectedProject || !hasArtifactModes;
  $('task-mode-control').hidden = !!state.selected || !!state.selectedProject || (!hasArtifactModes && state.mode === 'text');
  $('task-mode').disabled = blocked || !state.connected;
  $('task-mode').value = state.mode;
  $('sandbox-mode').hidden = !state.taskModes.includes('python_sandbox');
  $('sandbox-mode').disabled = $('sandbox-mode').hidden;
  $('text-artifacts-mode').hidden = !state.taskModes.includes('text_artifacts');
  $('text-artifacts-mode').disabled = $('text-artifacts-mode').hidden;
  $('scope-summary').textContent = nativeMode ? t("本机开发 · 原生权限") : projectMode ? t("改稿保存在豆窝 · 原项目不变") : sandboxMode ? t("Python 协作与隔离测试 · 使用现有订阅")
    : textArtifacts ? t("文档、网页与静态矢量图 · 不运行 · 使用现有订阅") : artifactMode ? t("新建 Python 改稿 · 暂不运行 · 使用现有订阅") : t("聊天协作 · 使用现有订阅");
  $('scope-description').textContent = nativeMode ? t("按统一权限调用本机工具协作。Plan 使用原生规划模式；Full Access 可按任务要求读写本机文件、执行命令和联网。具体操作与结果见执行记录。") : projectMode
    ? (mode === 'project_folder' ? t("在完整项目的隔离副本中按需查找和读取文件，所读内容会发送给所选伙伴对应的模型服务供应商。伙伴分工修改和互审，原项目不变；暂不运行项目、安装依赖或自动合入。已修改文件可在伙伴面板查看。") : t("只处理你选定的文本文件。发出目标后，文件正文会发送给所选伙伴对应的模型服务供应商。伙伴讨论、修改和互审后，改稿保存在豆窝，不会改动原项目；暂不运行项目、安装依赖或自动合入。改了哪些内容可在伙伴面板查看。"))
    : sandboxMode
    ? t("只处理本工具新建的 Python 文件。伙伴互审后，服务可在本机无网络容器中运行标准库 unittest，每版本最多一次、本次任务最多两次；不接入现有项目、不安装依赖。程序输出不等于独立验收。")
    : textArtifacts
    ? t("新建文档、网页源码或 SVG 矢量图，由伙伴分工和互审，文件保存在豆窝。仅安全预览静态 SVG，不执行网页或代码；这不是图片模型接入，不会改动原项目。")
    : artifactMode
    ? t("新建的改稿只保存在豆窝，伙伴会互相审核；暂不运行代码或测试，不会改动现有项目。结果未确认的请求不会自动重发。")
    : t("先围绕你输入的内容讨论，不会自行改动你的项目或运行代码。伙伴会在本次目标范围内协作；没有新任务时不会调用模型。结果未确认的请求不会自动重发。");
  const prospectiveMode = !room || canContinue ? state.workMode : currentWorkMode;
  if (prospectiveMode === 'plan') {
    $('scope-summary').textContent = t("先聊方案 · 不修改文件");
    $('scope-description').textContent = projectMode
      ? `${t("伙伴可以在隔离副本中按需查找和阅读项目，所读内容会发送给对应模型服务供应商。这轮只讨论方案，不修改文件、不运行项目。")}${interfaceWordSpace()}${planNextStepText()}`
      : `${t("这轮只讨论方案，不创建或修改文件，也不运行代码。")}${interfaceWordSpace()}${planNextStepText()}`;
  }
  $('message').disabled = !canCompose;
  $('message').placeholder = natural ? !natural.canSend ? t("当前无法发送，原因见下方") : objective?.status === 'complete' ? t("哪里还要调整？直接告诉伙伴") : t("补充要求，或说继续") : continuation === 'native_recovery' ? t("继续，或补充要求后接着做") : continuation ? t("说“继续”，接着完成原任务") : room ? t("补充要求，或告诉伙伴要调整什么") : t("说说这次想做什么");
  const policyInputError = policySubmissionError() || runtimeSubmissionError();
  $('send').disabled = !!recipientProblem() || !canCompose || (natural && !natural.canSend) || (!natural && !continuation && (!!policyInputError || (preparingFork ? blockedParticipants().length > 0 : submissionAvailabilityBlocked()))) || !$('message').value.trim()
    || (!room && ((!state.taskModes.includes(state.mode) && !(projectMode && state.projects.some((project) => project.id === state.selectedProject))) || (artifactMode && state.target === 'all')));
  renderRecipientControl();
  $('send-label').textContent = state.busy ? t("发送中") : natural ? t("发送") : continuation ? continuation === 'native_recovery' && $('message').value.trim() ? t("发送并继续") : t("发送") : canAnswer ? t("回答并继续") : preparingFork ? t("开始新任务") : canContinue ? t("开始下一轮") : room ? t("发送补充") : t("开始任务");
  $('character-count').hidden = true;
  const availabilityBlock = preparingFork ? availabilityBlockMessage() : submissionAvailabilityMessage();
  $('composer-note').textContent = natural ? !state.connected ? t("本地服务未连接，草稿已保留。") : !natural.canSend ? errorLabels[natural.reason] || executionMessage(natural.reason) : state.pending ? t("结果待确认，原请求已保留。") : '' : continuation ? t("说“继续”，或点上方“继续”。已有目标和权限保持。") : policyInputError || availabilityBlock || (inactive ? t("先恢复任务，再继续聊天或开始下一轮。") : state.pending ? t("先确认上一条，避免重复发送") : (!state.connected ? t("本地服务未连接。请先刷新；如果刚重启过豆窝，请从桌面版重新打开。")
    : preparingFork ? t("复用已保存文件，以新会话开始；原任务和未完成项保留，未被验收。确认目标后点“开始新任务”。")
    : canAnswer ? t("回答当前问题，沿同一目标继续。")
      : canContinue ? t("写下新的要求，在同一聊天继续下一轮；已有交付与记录保留")
      : teamStopped ? t("本轮不会继续派发，记录仍可查看；未确认结果不会自动重跑")
      : teamDelivered ? t("本轮已交付，正在等你的下一项要求。如需新修改，请点“新任务”继续。")
      : closedStatuses.has(room?.status) ? t("已经停止处理，记录还在这里")
        : room ? t("补充后继续处理") : !state.participantIds.length ? t("先选择参与工具") : state.workMode === 'plan' ? t("发送后先聊方案，不修改文件")
          : nativeMode ? t("按任务要求和所选原生权限执行") : projectMode ? t("修改保存在副本，原项目不变") : artifactMode ? t("发送后开始制作成果") : t("当前只能交付文字，请选择文件成果类型")));
  if (!state.connectionChecked) {
    $('connection').textContent = t("正在连接本地服务…");
    $('composer-note').textContent = t("正在连接本地服务…");
  }
  $('composer-note').hidden = natural ? !$('composer-note').textContent : !(policyInputError || canAnswer || availabilityBlock || inactive || state.pending || !state.connected || !state.participantIds.length);
  $('composer-note').title = $('composer-note').textContent;
  renderParticipantPicker();
  renderManagementControls();
  renderExportControls();
  for (const id of ['accept-run', 'retry-run', 'fork-room', 'build-from-plan']) {
    const button = $(id);
    const legacyQuotaGuard = id === 'fork-room' || id === 'build-from-plan' && !executionPolicyPayload();
    if (button) button.disabled = blocked || inactive || !state.connected || (id !== 'accept-run' && (legacyQuotaGuard ? blockedParticipants().length > 0 : submissionAvailabilityBlocked()));
  }
  for (const id of ['continue-objective', 'start-objective', 'start-suggested-goal', 'restart-objective', 'native-handoff', 'recover-native']) if ($(id)) $(id).disabled = blocked || inactive || !state.connected
    || (!['continue-objective', 'recover-native'].includes(id) && !!policyInputError) || (id === 'start-objective' && currentRun?.can_start_objective !== true)
    || (id === 'recover-native' && currentRun?.can_recover_native !== true)
    || (id === 'restart-objective' && currentRun?.can_restart_objective !== true) || (id === 'native-handoff' && currentRun?.can_native_handoff !== true) || (id === 'start-suggested-goal' && currentRun?.can_start_suggested_goal !== true);
  if ($('continue-current')) {
    $('continue-current').disabled = blocked || inactive || !state.connected || natural?.canSend !== true;
    $('continue-current').textContent = t("继续");
  }
  if ($('add-completion-suggestion')) $('add-completion-suggestion').disabled = $('message').disabled || blocked || inactive || !state.connected;
  const stoppedComposer = !!teamStopped && !preparingFork && !canContinue && !canAnswer && !continuation && !natural;
  $('composer').hidden = !!state.reader || materialsOpen() || stoppedComposer;
  $('terminal-composer-note').hidden = !!state.reader || materialsOpen() || !stoppedComposer;
  $('terminal-composer-note').textContent = stoppedComposer
    ? state.nativeProjectSupported && room.mode === 'native_project' && currentRun?.can_recover_native === true ? t("点“继续”即可接着做，已有成果和记录保留。")
      : state.nativeProjectSupported && currentRun?.can_native_handoff === true ? t("点“继续”承接完整开发。可先选择开发权限；已有成果和旧记录保留。")
      : state.continuousObjectiveSupported && currentRun?.can_restart_objective === true ? t("点“继续”接着处理原目标，已有成果和记录保留。")
      : objective ? t("本目标已停下，成果和记录保留。请先查看原因；如需开始新的目标，可点“新任务”。")
      : teamStopped === 'unknown' ? t("这轮的执行结果尚未确认。请先查看记录；不会自动重跑。你也可以新建任务。")
      : currentRun?.can_retry === true ? t("本轮已停止，记录仍在。要继续，请点交付区的“新的尝试（新会话）”，或新建任务。")
        : t("本轮已停止，记录仍在。当前不能接续这轮；你可以新建任务。")
    : '';
  if (availabilityBlock && stoppedComposer) $('terminal-composer-note').textContent += ` ${availabilityBlock}`;
  renderComposerUsage();
  renderPartnerActivity(state.snapshot);
  renderWorkIndicator(state.snapshot);
  renderMediaShelf();
  renderCodeStatus();
  renderRuntimeSettings(); renderAvailabilityRecheck(); renderMaterials();
  for (const button of document.querySelectorAll('[data-target]')) {
    button.disabled = !canCompose || (!room && artifactMode && button.dataset.target === 'all');
    button.setAttribute('aria-pressed', String(button.dataset.target === (state.target === 'both' ? 'all' : state.target)));
  }
  const pending = state.pending;
  $('pending-request').hidden = !pending;
  if (pending) {
    $('pending-request').className = `pending-request${state.busy ? ' sending' : ''}`;
    $('pending-title').textContent = state.busy ? t("正在等待本地服务确认…") : t("这次操作的结果尚未确认");
    const objectiveActions = { objective_reassessment: t("沿用原目标与权限重新核对完成情况"), native_recovery: t("接着处理原任务，沿用已有权限与设置"), native_handoff: t("以所选开发权限接着完成原目标，保留此前记录"), objective: t("继续这个目标并安排下一步"), restart: t("接着完成未完成工作，保留已完成成果"), continue: t("开始已确认的下一步"), answer: t("回答问题并沿同一目标继续"), suggested: t("明确开始建议的新目标"), pause: t("在当前处理结束后暂停"), resume: t("沿原目标恢复") };
    const original = Object.hasOwn(objectiveActions, pending.kind) ? objectiveActions[pending.kind] : pending.kind === 'create' ? t("目标「{p0}{p1}」", { p0: pending.body.goal.slice(0, 45), p1: pending.body.goal.length > 45 ? '…' : '' })
      : pending.kind === 'cancel' ? t("原任务的停止请求") : pending.kind === 'accept' ? t("本轮交付的接受记录")
        : pending.kind === 'fork' ? t("复用已保存文件另开的新任务")
        : pending.kind === 'run' ? (pending.body.retry ? t("明确开始新的尝试") : t("同一聊天的下一轮任务"))
          : pending.kind === 'message' && pending.body.intent === 'continue' ? isContinueCommand(pending.body.text) ? t("接着处理原任务") : t("发送补充要求并继续处理") : t("发给 {p0} 的补充说明", { p0: displayName(pending.body.target) });
    $('pending-description').textContent = state.scopeChanged ? t("{p0}的结果仍未确认，而且服务连接已改变。请先核对任务记录；此窗口保留旧请求，不会跨连接重发。", { p0: original }) : (state.busy ? t("{p0}。请稍候，重复点击不会创建另一份请求。", { p0: original }) : t("{p0}可能已经生效。先核对任务记录；需要时用原内容、原请求标识重试，不要另建同一目标。", { p0: original }));
    const hostEntry = pending.hostOutbox && state.hostOutboxRows.get(pending.body.request_id);
    if (hostEntry) { $('pending-title').textContent = hostEntry.delivery.state === 'staged' ? t("正在发送…") : t("正在核对发送结果…"); $('pending-description').textContent = hostDeliveryMessage(hostEntry); }
    const automaticReceipt = !hostEntry && pending.kind === 'message' && state.messageReceiptsSupported && !state.scopeChanged && !pending.storageFailed;
    if (pending.storageFailed) { $('pending-title').textContent = t("尚未发送"); $('pending-description').textContent = t("发送记录未能可靠保存。原文和设置仍保留，可以重试。"); }
    if (automaticReceipt && !state.busy) {
      $('pending-title').textContent = pending.receiptBlocked ? t("消息未发送完成") : t("正在核对发送结果…");
      $('pending-description').textContent = pending.receiptBlocked
        ? pending.receiptError === 'request_conflict' ? t("消息回执存在冲突，原文已保留，没有重新发送。可以先处理其它聊天。") : t("发送未完成，原文已保留。可以查看本任务记录，或先处理其它聊天。")
        : t("原文已保留，豆窝会自动核对。你可以先去其它聊天。");
    }
    $('retry-request').hidden = state.busy || automaticReceipt || !!hostEntry && hostEntry.delivery.can_send !== true;
    $('verify-request').hidden = automaticReceipt;
    $('retry-request').disabled = state.busy || !state.connected || state.scopeChanged || state.pending?.receiptBlocked === true;
    $('verify-request').disabled = state.busy || state.refreshing;
  }
  renderTabs();
  renderNowCard();
}

function applySnapshot(snapshot) {
  if (state.deletedRooms.has(snapshot?.room?.id)) return;
  if (!snapshot?.room || !uuidPattern.test(snapshot.room.id) || !Array.isArray(snapshot.events) || !Array.isArray(snapshot.members) || !Array.isArray(snapshot.jobs)) {
    throw new RequestError(t("本地服务返回的任务记录不完整。"), { uncertain: state.busy });
  }
  if (snapshot.room.id === state.snapshot?.room.id) {
    const maxSeq = (events) => events.reduce((value, event) => Math.max(value, Number.isSafeInteger(event.seq) ? event.seq : 0), 0);
    if ((pagedRoom(snapshot) ? snapshot.through_seq : maxSeq(snapshot.events)) < (pagedRoom(state.snapshot) ? state.snapshot.through_seq : maxSeq(state.snapshot.events))) return;
  }
  if (state.snapshot?.current_run?.id !== snapshot.current_run?.id && !state.drafts.has(snapshot.room.id)) {
    state.workMode = ['plan', 'build'].includes(snapshot.current_run?.work_mode) ? snapshot.current_run.work_mode : 'build';
  }
  state.snapshot = snapshot;
  if (state.selected === snapshot.room.id) rememberRoomLocation(snapshot.room.id);
  const runtimeScope = `${snapshot.room.id}:${snapshot.current_run?.id || ''}`;
  const configuration = naturalRuntimeConfiguration(snapshot);
  if (configuration) {
    if (state.runtimeScope !== runtimeScope) {
      const draft = state.drafts.get(snapshot.room.id);
      const pending = state.pending?.roomId === snapshot.room.id ? state.pending.body : null;
      state.runtimeDraftDirty = !!draft?.runtimeDraftDirty || !!pending?.runtime_settings;
      state.runtimeDraftToken = draft?.runtimeDraftToken || state.pending?.runtimeDraftToken || null;
      state.workModeDraftDirty = !!draft?.workModeDraftDirty || !!pending?.work_mode;
      state.workModeDraftToken = draft?.workModeDraftToken || state.pending?.workModeDraftToken || null;
      state.runtimeSettings = copyRuntimeSettings(pending?.runtime_settings || (draft?.runtimeDraftDirty ? draft.runtimeSettings : configuration.desired.runtime_settings));
      state.workMode = pending?.work_mode || (draft?.workModeDraftDirty ? draft.workMode : configuration.desired.work_mode);
    }
    if (!state.runtimeDraftDirty) state.runtimeSettings = copyRuntimeSettings(configuration.desired.runtime_settings);
    if (!state.workModeDraftDirty) state.workMode = configuration.desired.work_mode;
  } else if (state.runtimeScope !== runtimeScope) {
    state.runtimeSettings = copyRuntimeSettings(state.drafts.get(snapshot.room.id)?.runtimeSettings || snapshot.current_run?.runtime_settings || snapshot.room.runtime_settings);
  }
  state.runtimeScope = runtimeScope;
  // A read snapshot's room row does not carry the list endpoint's outcome_state.
  // Derive only this room's display state from its own recorded projection.
  const listedRoom = { ...snapshot.room };
  const objective = currentObjective(snapshot);
  if (objective) { listedRoom.objective_status = objective.status; listedRoom.objective_goal = objective.original_goal; listedRoom.objective_pause_requested = objective.pause_requested; }
  const outcomeState = terminalTeamState(snapshot) || (snapshot.collaboration?.mode === 'team' ? snapshot.collaboration.outcome?.state : null);
  if (Object.hasOwn(outcomeLabels, outcomeState)) listedRoom.outcome_state = outcomeState;
  const index = state.rooms.findIndex((room) => room.id === snapshot.room.id);
  if (index === -1) state.rooms.push(listedRoom);
  else state.rooms[index] = listedRoom;
  renderRooms();
  renderMembers(snapshot);
  renderEvents(snapshot);
  renderControls();
  void refreshRoomMedia();
  void refreshCodeStatus();
}

async function selectRoom(id, { restoringLocation = false } = {}) {
  if (!uuidPattern.test(id) || state.deletedRooms.has(id) || state.busy && !canDetachPendingRequest()) return;
  detachMessageForNavigation();
  if (state.selected !== id) clearActionError();
  closeTaskOptions({ restoreFocus: false });
  closeRuntimePopover({ restoreFocus: false });
  closeUsagePopover();
  closeContextMenu();
  setHistoryExpanded(false);
  saveDraft();
  closeRecipientMenu();
  closeReader();
  const epoch = ++state.epoch;
  state.selected = id;
  state.forkSource = null;
  state.snapshot = null;
  await loadComposerDraft(id);
  if (state.selected !== id || epoch !== state.epoch) return;
  const draft = state.drafts.get(id);
  const unresolved = [...state.sidelinedRequests.values()].find(({ request, receipt }) => request.roomId === id
    && request.detached === true && request.composerIndependent !== true && request.intervened !== true && ['sending', 'unknown', 'blocked'].includes(receipt));
  if (unresolved && state.messageReceiptsSupported && !state.pending) {
    state.pending = unresolved.request; rememberPending();
  }
  $('message').value = unresolved?.request.body?.text || draft?.text || '';
  state.target = unresolved?.request.body?.target || draft?.target || 'all';
  state.targetExplicit = draft?.targetExplicit ?? true;
  state.workMode = draft?.workMode || 'build';
  $('empty-room').hidden = true;
  $('events').replaceChildren(element('li', t("正在读取已保存的对话…"), 'detail-caption'));
  state.renderedRoom = `loading:${id}`;
  state.eventNodes.clear();
  renderProgressRecords(null, true);
  renderRooms(); renderMembers(null); renderControls();
  try {
    const snapshot = await readRoomSnapshot(id);
    if (epoch !== state.epoch || state.selected !== id) return;
    if (snapshot?.room?.id !== id) throw new RequestError(t("返回的任务身份不匹配，请核对本地服务。"));
    setConnection(true);
    applySnapshot(snapshot);
  } catch (error) {
    if (epoch !== state.epoch) return;
    let deleted = error.code === 'room_deleted';
    if (error.code === 'room_not_found') {
      try { const list = await api('/api/rooms'); deleted = Array.isArray(list.rooms) && !list.rooms.some((room) => room.id === id); }
      catch (_error) { /* A network failure never proves deletion. */ }
    }
    if (epoch !== state.epoch) return;
    if (deleted) { forgetDeletedRoom(id); showNotice(t("这项任务已被删除。")); }
    else if (error.code === 'forbidden') { leaveUnavailableRoom(); showNotice(error.message, 'error'); }
    else {
      if (restoringLocation) leaveUnavailableRoom({ preserveLocation: true });
      showNotice(error.message, 'error'); renderControls();
    }
  }
}

let roomSupplementRefresh = null;
function refreshRoomSupplements() {
  if (roomSupplementRefresh) return;
  // Receipt recovery can require several reads. Keep one pass in flight without
  // making the current task state wait for unrelated records to settle.
  roomSupplementRefresh = (async () => {
    try {
      await refreshLifecycleOperations();
      await refreshHostOutbox();
      await refreshPartnerOperations();
      if (state.projectDeletionSupported) for (const intent of [...state.projectDeletions.values()]) {
        if (!intent.error || intent.error === 'project_deletion_not_found') await checkProjectDeletion(intent);
      }
    } catch (_error) { /* Each operation retains its original identity for the next check. */ }
    finally { roomSupplementRefresh = null; }
  })();
}
async function refresh({ initial = false, restoreSelection = false } = {}) {
  if (state.refreshing || state.busy) return;
  state.refreshing = true;
  const epoch = state.epoch;
  const selected = state.selected;
  let receiptRefresh;
  try {
    if (outboxCleanupPending) void rememberPending();
    renderControls();
    const [status] = await Promise.allSettled([api('/api/status')]);
    if (status.status === 'fulfilled') configurePaging(status.value);
    const results = [status, ...await Promise.allSettled([api(state.roomPagingSupported ? '/api/rooms?view=compact' : '/api/rooms'), api(workspaceApi.projects), api(state.roomPagingSupported ? `${workspaceApi.partners}?view=compact` : workspaceApi.partners), ...(selected ? [readRoomSnapshot(selected)] : [])])];
    if (epoch !== state.epoch || state.busy) return;
    const [, list, projects, partners, snapshot] = results;
    const authorizationFailure = [status, list, snapshot].find((result) => result?.status === 'rejected' && result.reason.code === 'forbidden');
    if (authorizationFailure) { leaveUnavailableRoom(); setConnection(false); showNotice(authorizationFailure.reason.message, 'error'); return; }
    if (status.status === 'fulfilled') {
      state.roomLifecycleSupported = status.value.capabilities?.room_lifecycle_operations === true;
      state.messageReceiptsSupported = status.value.capabilities?.message_receipts === true;
      state.projectDeletionSupported = status.value.capabilities?.project_registration_deletion === true;
      state.partnerLifecycleSupported = status.value.capabilities?.partner_lifecycle_operations === true;
      state.roomNativeSessionCleanupSupported = status.value.capabilities?.room_native_session_cleanup === true;
      state.executionPolicySupported = status.value.capabilities?.execution_policy === true;
      state.nativeProjectSupported = status.value.capabilities?.native_project === true;
      state.naturalContinuationSupported = status.value.capabilities?.natural_continuation === true;
      state.naturalRuntimeSupported = status.value.capabilities?.natural_runtime_settings === true;
      state.continuousObjectiveSupported = status.value.capabilities?.continuous_objective === true;
      setCatalog(status.value.agents);
      acceptBeanStyles(status.value.bean_styles);
      state.taskModes = ['text', 'python_artifacts', 'python_sandbox', 'text_artifacts', 'native_project'].filter((mode) =>
        mode === 'text' || (Array.isArray(status.value.task_modes) && status.value.task_modes.includes(mode) && (mode !== 'native_project' || state.nativeProjectSupported)));
      if (!selected) renderMembers(null);
    }
    if (projects.status === 'fulfilled') acceptProjects(projects.value);
    if (partners.status === 'fulfilled') acceptPartners(partners.value);
    chooseDefaultArtifactMode();
    const discoveryError = initial ? await discoverOnOpen() : null;
    setConnection(status.status === 'fulfilled' || list.status === 'fulfilled');
    if (list.status === 'fulfilled' && Array.isArray(list.value.rooms)) {
      state.rooms = list.value.rooms.map((room) => ({ ...room, ...(typeof room?.goal_preview === 'string' ? { goal: room.goal_preview } : {}) })).filter((room) => room && uuidPattern.test(room.id) && !state.deletedRooms.has(room.id));
      acceptLifecycleOperations(list.value.lifecycle_operations);
      for (const room of state.rooms) acceptLifecycleOperation(room.lifecycle_operation);
      receiptRefresh = refreshMessageReceipts();
      refreshRoomSupplements();
      renderRooms();
    }
    if (epoch !== state.epoch || state.selected !== selected) return;
    if (snapshot?.status === 'rejected' && (snapshot.reason.code === 'room_deleted'
        || snapshot.reason.code === 'room_not_found' && list.status === 'fulfilled' && Array.isArray(list.value.rooms)
          && !list.value.rooms.some((room) => room.id === selected))) {
      forgetDeletedRoom(selected); showNotice(t("这项任务已被删除。")); return;
    }
    if (snapshot?.status === 'rejected' && snapshot.reason.code === 'forbidden') {
      leaveUnavailableRoom(); showNotice(snapshot.reason.message, 'error'); return;
    }
    if (snapshot?.status === 'fulfilled') {
      if (snapshot.value?.room?.id !== selected) throw new RequestError(t("返回的任务身份不匹配，请核对本地服务。"));
      applySnapshot(snapshot.value);
    }
    const failure = results.find((result) => result.status === 'rejected');
    if (failure) showNotice(failure.reason.message, 'error');
    else if (discoveryError) showNotice(discoveryError, 'error');
    else if (status.status === 'fulfilled' && status.value.background_error) showNotice(executionMessage(status.value.background_error), 'error');
    else if (!state.pending) showNotice();
    if ((initial || restoreSelection) && !state.selected && list.status === 'fulfilled' && Array.isArray(list.value.rooms)) {
      const hash = window.location?.hash || '';
      const requestedRoom = roomFromLocation();
      if (requestedRoom && state.rooms.some((room) => room.id === requestedRoom)) {
        await selectRoom(requestedRoom, { restoringLocation: true });
      } else if (hash) rememberRoomLocation();
    }

  } catch (_error) {
    if (epoch === state.epoch) {
      setConnection(false);
      showNotice(t("状态记录或伙伴清单暂时不可用，请刷新后再派发。"), 'error');
    }
  } finally {
    state.refreshing = false;
    renderControls();
  }
  // Finish startup receipt recovery without holding the room refresh lock.
  // Later activations can still read fresh task state while this is pending.
  if (initial && receiptRefresh) await receiptRefresh;
}

// Only unresolved requests live here. The scope index and complete original bodies
// commit together; sessionStorage is not large enough for escaped long messages.
function openOutbox() {
  if (!outboxDatabase) outboxDatabase = new Promise((resolve, reject) => {
    if (typeof indexedDB === 'undefined') { reject(new Error('outbox_unavailable')); return; }
    const request = indexedDB.open(outboxDatabaseName, 1);
    request.onupgradeneeded = () => request.result.createObjectStore('scopes', { keyPath: 'scope' });
    request.onsuccess = () => { outboxConnection = request.result; resolve(request.result); };
    request.onerror = () => reject(request.error || new Error('outbox_unavailable'));
    request.onblocked = () => reject(new Error('outbox_unavailable'));
  }).catch((error) => { outboxDatabase = null; throw error; });
  return outboxDatabase;
}
async function outboxTransaction(mode, operation) {
  const database = await openOutbox();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction('scopes', mode);
    let result;
    transaction.oncomplete = () => resolve(result);
    transaction.onerror = transaction.onabort = () => reject(transaction.error || new Error('outbox_write_failed'));
    operation(transaction.objectStore('scopes'), (value) => { result = value; }, transaction);
  });
}
function outboxSnapshot() {
  return JSON.parse(JSON.stringify({ scope: state.authScope,
    request: state.pending && !state.scopeChanged ? state.pending : null,
    sidelined_requests: [...state.sidelinedRequests.values()],
    deleted_rooms: [...state.deletedRooms] }));
}
// The host outbox survives ports and instances. It records user intent only;
// receiving or staging a row never means a model has completed its work.
const hostOutboxKinds = new Set(['invite', 'create', 'message', 'run', 'suggested', 'accept', 'fork', 'native_handoff', 'native_recovery', 'objective_reassessment', 'objective', 'restart', 'continue', 'answer', 'pause', 'resume']);
const hostDeliveryStates = new Set(['staged', 'received', 'in_flight', 'unconfirmed', 'conflict', 'deleted', 'retired']);
function sameRequestBody(left, right) {
  const ordered = (value) => Array.isArray(value) ? value.map(ordered) : value && typeof value === 'object'
    ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, ordered(value[key])])) : value;
  return JSON.stringify(ordered(left)) === JSON.stringify(ordered(right));
}
function validateHostOutbox(value, request = null) {
  if (!value || value.scope !== state.hostOutboxScope || !uuidPattern.test(value.request_id) || !hostOutboxKinds.has(value.kind)
      || !(value.room_id === null && value.kind === 'create' || uuidPattern.test(value.room_id))
      || !Number.isSafeInteger(value.seq) || value.seq < 1 || !Number.isSafeInteger(value.attempt_count) || value.attempt_count < 0
      || value.delivery?.state === 'received' && !uuidPattern.test(value.delivery.result_room_id)
      || !/^[0-9a-f]{64}$/.test(value.fingerprint || '') || !['pending', 'retired', 'deleted'].includes(value.status)
      || !hostDeliveryStates.has(value.delivery?.state) || typeof value.delivery.can_send !== 'boolean' || typeof value.delivery.can_retire !== 'boolean'
      || request && (value.request_id !== request.body?.request_id || value.kind !== request.kind || value.room_id !== (request.roomId || null)
        || request.hostOutbox && (value.scope !== request.hostOutbox.scope || value.fingerprint !== request.hostOutbox.fingerprint))) {
    throw new RequestError(t("发送记录的身份或内容尚未确认，未重新发送。"), { code: 'request_conflict' });
  }
  return value;
}
function hostOutboxPath(requestId, scope = state.hostOutboxScope) {
  return `/api/outbox/${encodeURIComponent(requestId)}?scope=${encodeURIComponent(scope)}`;
}
async function readHostOutbox(request) {
  const result = await api(hostOutboxPath(request.body.request_id, request.hostOutbox?.scope));
  const entry = validateHostOutbox(result?.outbox, request);
  if (entry.body !== null && entry.body !== undefined && !sameRequestBody(entry.body, request.body)) throw new RequestError(errorLabels.request_conflict, { code: 'request_conflict' });
  const { body: _body, ...summary } = entry;
  state.hostOutboxRows.set(entry.request_id, summary); return entry;
}
function knownHostRequest(requestId) {
  return state.pending?.body?.request_id === requestId ? state.pending
    : state.sidelinedRequests.get(requestId)?.request || state.hostOutboxRequests.get(requestId);
}
function bindHostOutbox(request, entry) {
  validateHostOutbox(entry, request);
  request.hostOutbox = { scope: entry.scope, fingerprint: entry.fingerprint };
  if (state.pending === request) state.scopeChanged = false;
  const { body: _body, ...summary } = entry;
  state.hostOutboxRows.set(entry.request_id, summary); state.hostOutboxRequests.set(entry.request_id, request);
}
async function stageHostOutbox(request) {
  if (request.hostOutbox) return readHostOutbox(request);
  let entry;
  try {
    const result = await api('/api/outbox', 'POST', { scope: state.hostOutboxScope, request_id: request.body.request_id,
      kind: request.kind, room_id: request.roomId || null, body: request.body, source_seq: request.roomId ? request.sourceSeq ?? null : null,
      ...(request.legacyUnknown || request.roomId && !Number.isSafeInteger(request.sourceSeq) ? { legacy_unknown: true } : {}) });
    entry = validateHostOutbox(result?.outbox, request);
  } catch (error) {
    if (!error.uncertain) throw error;
    // A lost stage reply is not permission to dispatch. Read the committed row.
    entry = await readHostOutbox(request);
  }
  bindHostOutbox(request, entry); return entry;
}
function pendingRoute(request) {
  const suffix = { invite: 'members', cancel: 'cancel', message: 'messages', run: 'runs', suggested: 'runs', accept: 'accept', fork: 'fork', native_handoff: 'native-handoff', native_recovery: 'recover-native', objective_reassessment: 'reassess-objective', objective: 'objective', restart: 'restart-objective', continue: 'continue', answer: 'answer', pause: 'pause', resume: 'resume' }[request.kind];
  return request.kind === 'create' ? '/api/rooms' : `${roomPath(request.roomId)}/${suffix}`;
}
function dispatchPath(request) {
  const query = [];
  if (state.roomPagingSupported || request.hostOutbox) query.push('response=ack');
  if (request.hostOutbox) query.push(`outbox_scope=${encodeURIComponent(request.hostOutbox.scope)}`);
  return pendingRoute(request) + (query.length ? `?${query.join('&')}` : '');
}
function hostDeliveryMessage(entry) {
  return ({ staged: t("发送记录已保存，正在接着发送。"), received: t("已发送，正在读取记录。"), in_flight: t("发送正在处理中，正在核对结果，不会重复发送。"),
    unconfirmed: t("上次发送结果尚未确认，原文保留，正在核对实际记录。"), conflict: t("这条发送意图已与后续操作冲突，原文保留，未重新发送。"),
    deleted: t("这条发送所属的任务已删除，不会重新发送。"), retired: t("这条发送记录已经结束，不会重新发送。") })[entry.delivery.state];
}
function queueHostRetirement(request, disposition) {
  if (!request?.hostOutbox) return;
  state.hostOutboxRetirements.set(request.body.request_id, { ...request.hostOutbox, disposition });
}
async function flushHostRetirements() {
  if (!state.hostOutboxSupported) return;
  for (const [requestId, intent] of [...state.hostOutboxRetirements]) {
    if (intent.scope !== state.hostOutboxScope || state.hostOutboxRetiring.has(requestId)) continue;
    state.hostOutboxRetiring.add(requestId);
    try {
      const result = await api(hostOutboxPath(requestId, intent.scope)); const entry = validateHostOutbox(result?.outbox);
      if (entry.fingerprint !== intent.fingerprint) continue;
      if (entry.status !== 'pending') { state.hostOutboxRetirements.delete(requestId); state.hostOutboxRequests.delete(requestId); state.hostOutboxRows.delete(requestId); continue; }
      if (!entry.delivery.can_retire) continue;
      const retired = await api(`/api/outbox/${encodeURIComponent(requestId)}/retire`, 'POST', intent);
      validateHostOutbox(retired?.outbox);
      if (retired.outbox.status !== 'pending') { state.hostOutboxRetirements.delete(requestId); state.hostOutboxRequests.delete(requestId); state.hostOutboxRows.delete(requestId); }
    } catch (_error) { /* The host row retains the original body until retirement is confirmed. */ }
    finally { state.hostOutboxRetiring.delete(requestId); }
  }
}
function settleHostDelivery(request, entry) {
  bindHostOutbox(request, entry);
  if (entry.delivery.state === 'received') {
    retireOutboxRequest(request);
    if (request.kind === 'invite' && state.memberInvitations.get(request.roomId)?.body.request_id === request.body.request_id) state.memberInvitations.delete(request.roomId);
    if (request.kind === 'message') finishMessageReceipt(request);
    else {
      if (state.pending === request) state.pending = null;
      state.sidelinedRequests.delete(request.body.request_id); rememberPending();
    }
    return true;
  }
  if (['deleted', 'retired'].includes(entry.delivery.state)) {
    if (entry.delivery.state === 'deleted' && request.roomId) forgetDeletedRoom(request.roomId);
    retireOutboxRequest(request, 'abandoned');
    if (state.pending === request) state.pending = null;
    state.sidelinedRequests.delete(request.body.request_id); state.hostOutboxRequests.delete(request.body.request_id);
    if (request.kind === 'invite' && state.memberInvitations.get(request.roomId)?.body.request_id === request.body.request_id) state.memberInvitations.delete(request.roomId);
    rememberPending();
    return true;
  }
  if (entry.delivery.state === 'conflict' && entry.delivery.can_retire) queueHostRetirement(request, 'abandoned');
  return false;
}
async function openRecoveredCreation(request, entry, navigationEpoch) {
  // A recovered create may finish the untouched welcome flow. Navigation and
  // new drafts always win over an older background result.
  if (!request.restoredFromHost || request.kind !== 'create' || entry.delivery.state !== 'received'
      || navigationEpoch !== state.epoch || state.selected || roomFromLocation()
      || state.pending || state.busy || state.manager || state.forkSource || $('message').value.trim()) return;
  await selectRoom(entry.delivery.result_room_id);
}
async function resumeHostIntent(request, navigationEpoch) {
  const key = request.body.request_id;
  if (state.hostAutomaticAttempts.has(key) || request.intervened || state.deletedRooms.has(request.roomId)) return;
  const entry = await readHostOutbox(request);
  if (settleHostDelivery(request, entry)) { await openRecoveredCreation(request, entry, navigationEpoch); return; }
  if (entry.delivery.state !== 'staged' || !entry.delivery.can_send) return;
  if (request.intervened || state.deletedRooms.has(request.roomId)) return;
  if (request.kind === 'invite' && !state.memberInvitationSupported) return;
  if (request.kind === 'message' && (messageHasRuntimeSettings(request.body) && !state.naturalRuntimeSupported
      || request.body.intent === 'continue' && !state.naturalContinuationSupported)) return;
  if ((request.body.mode === 'native_project' || request.body.runtime_settings?.permission === 'full_access' || ['native_recovery', 'native_handoff'].includes(request.kind)) && !state.nativeProjectSupported) return;
  if ((request.body.execution_policy || ['objective', 'restart', 'objective_reassessment', 'continue', 'suggested', 'answer', 'resume'].includes(request.kind)) && !state.executionPolicySupported) return;
  state.hostAutomaticAttempts.add(key); request.attempted = true;
  try {
    const result = await api(dispatchPath(request), 'POST', request.body);
    if (result?.acknowledged !== true || !uuidPattern.test(result.room_id)
        || !['create', 'fork', 'native_handoff'].includes(request.kind) && result.room_id !== request.roomId
        || result.request_id !== undefined && result.request_id !== key) throw new RequestError(t("发送回执尚未确认。"), { uncertain: true });
    const confirmed = await readHostOutbox(request);
    settleHostDelivery(request, confirmed);
    if (request.kind === 'invite' && confirmed.delivery.state === 'received') await refreshInvitedMembers(request);
    await openRecoveredCreation(request, confirmed, navigationEpoch);
  } catch (_error) {
    try { const confirmed = await readHostOutbox(request); settleHostDelivery(request, confirmed); await openRecoveredCreation(request, confirmed, navigationEpoch); } catch (_ignored) { /* Read-only polling will resume after connectivity returns. */ }
  }
}
async function refreshHostOutbox() {
  if (!state.hostOutboxSupported || state.hostOutboxPolling) return;
  state.hostOutboxPolling = true;
  const navigationEpoch = state.epoch;
  try {
    // Older browser requests may have been sent already. They never become
    // never-attempted merely because an old response or browser cache was lost.
    const legacy = [state.pending, ...[...state.sidelinedRequests.values()].map((entry) => entry.request)].filter(Boolean);
    if (!state.scopeChanged) for (const request of legacy) if (!request.hostOutbox && !['cancel', 'pause'].includes(request.kind)) {
      request.legacyUnknown ||= request.attempted === true;
      try { bindHostOutbox(request, await stageHostOutbox(request)); } catch (_error) { /* Preserve the old request without dispatch. */ }
    }
    let after = 0; let through;
    while (true) {
      const page = await api(`/api/outbox?scope=${encodeURIComponent(state.hostOutboxScope)}&after_seq=${after}${through === undefined ? '' : `&through_seq=${through}`}&limit=100`);
      if (page?.scope !== state.hostOutboxScope || !Array.isArray(page.entries) || !Number.isSafeInteger(page.through_seq)) throw new RequestError(t("发送记录尚未读完整。"));
      through ??= page.through_seq;
      if (page.through_seq !== through) throw new RequestError(t("发送记录位置已变化，未重新派发。"));
      for (const row of page.entries) {
        let summary = validateHostOutbox(row);
        let request = knownHostRequest(summary.request_id);
        if (!request) {
          const full = validateHostOutbox((await api(hostOutboxPath(summary.request_id))).outbox);
          if (full.status !== 'pending' || !full.body) continue;
          summary = full;
          if (full.body.request_id !== full.request_id) throw new RequestError(errorLabels.request_conflict);
          request = { kind: full.kind, roomId: full.room_id, body: full.body, sourceSeq: full.source_seq ?? null, detached: true, restoredFromHost: true };
          bindHostOutbox(request, full);
          if (request.kind === 'message') state.sidelinedRequests.set(request.body.request_id, { request, receipt: 'unknown' });
        }
        if (request.kind === 'invite') state.memberInvitations.set(request.roomId, request);
        if (settleHostDelivery(request, summary)) { if (request.kind === 'invite' && summary.delivery.state === 'received') await refreshInvitedMembers(request); await openRecoveredCreation(request, summary, navigationEpoch); continue; }
        if (summary.delivery.state === 'staged' && summary.delivery.can_send) await resumeHostIntent(request, navigationEpoch);
        else if (request.kind !== 'invite' && request.composerIndependent !== true && state.selected === request.roomId && !state.pending && !$('message').value.trim()) {
          state.pending = request; state.scopeChanged = false;
          $('message').value = request.body.goal ?? request.body.text ?? request.body.user_message ?? '';
          rememberPending();
        }
      }
      if (!page.has_more) break;
      if (!Number.isSafeInteger(page.last_seq) || page.last_seq <= after) throw new RequestError(t("发送记录游标没有前进。"));
      after = page.last_seq;
    }
    await flushHostRetirements();
  } catch (_error) { /* Existing drafts and original requests remain; no dispatch is inferred from a failed read. */ }
  finally { state.hostOutboxPolling = false; renderControls(); }
}

function outboxRequestKey(request) { return request?.body?.request_id || request?.receipt_id; }
function retireOutboxRequest(request, disposition = 'received') {
  const key = outboxRequestKey(request); if (key) outboxRetired.add(key);
  queueHostRetirement(request, disposition);
}
function rememberPending() {
  if (!outboxRestored || state.scopeChanged) return Promise.resolve();
  if (!state.authScope) return Promise.reject(new Error('outbox_scope_unavailable'));
  if (state.outboxConflict) return Promise.reject(new Error('outbox_request_conflict'));
  const saved = outboxSnapshot();
  const entries = new Map(saved.sidelined_requests.map((entry) => [outboxRequestKey(entry.request), entry]));
  if (saved.request) entries.set(outboxRequestKey(saved.request), { request: saved.request, receipt: 'unknown' });
  const retired = new Set(outboxRetired);
  const write = outboxWrites.catch(() => {}).then(() => outboxTransaction('readwrite', (store, _done, transaction) => {
    const indexRead = store.get(saved.scope);
    indexRead.onsuccess = () => {
      const index = indexRead.result || { scope: saved.scope, request_ids: [], deleted_rooms: [] };
      const deleted = new Set([...index.deleted_rooms || [], ...saved.deleted_rooms]);
      const keys = new Set([...(index.request_ids || []), ...entries.keys()]);
      const retained = []; let remaining = keys.size;
      const finish = () => { if (!remaining) store.put({ scope: saved.scope, request_ids: retained, deleted_rooms: [...deleted] }); };
      for (const key of keys) {
        const rowKey = `${saved.scope}:${key}`;
        const read = store.get(rowKey);
        read.onsuccess = () => {
          const previous = read.result;
          const next = entries.get(key);
          const request = next?.request || previous?.request;
          if (retired.has(key) || request && deleted.has(request.roomId)) store.delete(rowKey);
          else if (request) {
            if (previous && next && JSON.stringify(previous.request.body) !== JSON.stringify(next.request.body)) { transaction.abort(); return; }
            retained.push(key);
            if (next) store.put({ scope: rowKey, auth_scope: saved.scope, ...next });
          }
          remaining--; finish();
        };
      }
      finish();
    };
  })).then(() => {
    // This is only a small selection hint. The scope index remains the recovery
    // authority if a WebView cannot save the hint or a tab is reopened.
    try {
      if (saved.request) sessionStorage.setItem(pendingStorageKey, JSON.stringify({ scope: saved.scope, request_id: outboxRequestKey(saved.request) }));
      else sessionStorage.removeItem(pendingStorageKey);
    } catch (_error) { /* The committed scope index still contains every unresolved key. */ }
    outboxCommitted = true; outboxCleanupPending = false;
    if (state.actionError?.action === 'local_outbox_cleanup') clearActionError();
  });
  outboxWrites = write;
  void write.catch(() => {
    if (saved.deleted_rooms.length) {
      outboxCleanupPending = true;
      showActionError(t("聊天已删除，本机待发送缓存尚未确认清理。后台会重试，不会重新发送。"), state.selected || 'new', 'local_outbox_cleanup');
    }
  });
  return write;
}
async function readOutbox() {
  const hint = readSessionJson(pendingStorageKey);
  const scope = /^[0-9a-f]{64}$/.test(hint?.scope || '') ? hint.scope : state.authScope;
  return outboxTransaction('readonly', (store, done) => {
    const read = store.get(scope);
    read.onsuccess = () => {
      const index = read.result;
      if (!index) { done(null); return; }
      const keys = index.request_ids || []; const rows = new Map(); let remaining = keys.length;
      const finish = () => {
        if (remaining) return;
        const entries = keys.map((key) => rows.get(key)).filter(Boolean);
        const preferred = hint?.scope === scope ? hint.request_id || outboxRequestKey(hint.request) : null;
        const primary = entries.find((entry) => outboxRequestKey(entry.request) === preferred)
          || entries.find((entry) => entry.request?.detached !== true && entry.request?.intervened !== true);
        done({ scope, request: primary?.request || null,
          sidelined_requests: entries.filter((entry) => entry !== primary).map((entry) => ({ ...entry,
            request: { ...entry.request, detached: true }, receipt: entry.receipt === 'sending' ? 'unknown' : entry.receipt })),
          deleted_rooms: index.deleted_rooms || [] });
      };
      for (const key of keys) { const row = store.get(`${scope}:${key}`); row.onsuccess = () => { if (row.result) rows.set(key, row.result); remaining--; finish(); }; }
      finish();
    };
  });
}

async function restorePending() {
  try {
    if (!crypto.subtle) return;
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token));
    state.authScope = [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('');
    const legacy = readSessionJson(pendingStorageKey);
    let durable = null;
    try { durable = await readOutbox(); } catch (_error) { /* A send will report unavailable durable storage before dispatch. */ }
    state.restoredOutbox = durable;
    if (legacy?.request && durable?.request && outboxRequestKey(legacy.request) === outboxRequestKey(durable.request)
        && JSON.stringify(legacy.request.body) !== JSON.stringify(durable.request.body)) {
      legacy.request.receiptBlocked = true;
      state.outboxConflict = true;
    }
    const stored = state.outboxConflict ? legacy : legacy?.request && (!durable?.request || outboxRequestKey(legacy.request) !== outboxRequestKey(durable.request)) ? legacy : durable || legacy;
    if (legacy?.request && durable?.request && outboxRequestKey(legacy.request) !== outboxRequestKey(durable.request)) {
      state.restoredOutbox.sidelined_requests.push({ request: { ...durable.request, detached: true }, receipt: 'unknown' });
    }
    if (!stored?.request) return;
    const request = stored.request;
    const validRoom = request?.kind === 'create' || uuidPattern.test(request?.roomId);
    const objectiveAction = ['objective', 'restart', 'native_handoff', 'native_recovery', 'objective_reassessment', 'continue', 'answer', 'pause', 'resume'].includes(request?.kind);
    const validText = request?.kind === 'cancel' || (request?.kind === 'accept' || objectiveAction ? uuidPattern.test(request?.body?.request_id)
      && (request.kind !== 'answer' || typeof request.body.text === 'string' && request.body.text.trim() && validUserText(request.body.text))
      : typeof request?.body?.target === 'string' && agentPattern.test(request.body.target) && typeof (request.body.goal ?? request.body.text) === 'string' && validUserText(request.body.goal ?? request.body.text) && uuidPattern.test(request.body.request_id));
    const participants = request?.body?.participants;
    const validParticipants = participants === undefined || (Array.isArray(participants) && participants.length > 0 && participants.length <= 8 && participants.every((id) => typeof id === 'string' && agentPattern.test(id)) && new Set(participants).size === participants.length);
    const validProject = request?.body?.project_id === undefined || uuidPattern.test(request.body.project_id);
    const validWorkMode = request?.body?.work_mode === undefined || ['plan', 'build'].includes(request.body.work_mode);
    const validRun = !['run', 'suggested', 'accept', 'fork', 'objective', 'restart', 'native_handoff', 'native_recovery', 'objective_reassessment', 'continue', 'answer'].includes(request?.kind) || (runBinding({ id: request.body.run_id, revision: request.body.revision, digest: request.body.digest })
      && (request.kind !== 'run' || typeof request.body.retry === 'boolean')
      && (request.kind !== 'fork' || Number.isSafeInteger(request.body.revision)));
    const validObjective = !['pause', 'resume', 'answer'].includes(request?.kind) || uuidPattern.test(request.body.objective_id);
    const validPolicy = ['restart', 'native_handoff'].includes(request?.kind) ? !!copyExecutionPolicy(request?.body?.execution_policy)
      : request?.body?.execution_policy == null || !!copyExecutionPolicy(request.body.execution_policy);
    const validRestart = request?.kind !== 'restart' || Object.keys(request.body).length === 5
      && ['request_id', 'run_id', 'revision', 'digest', 'execution_policy'].every((key) => Object.hasOwn(request.body, key));
    const validHandoff = request?.kind !== 'native_handoff' || Object.keys(request.body).length === 6
      && ['request_id', 'run_id', 'revision', 'digest', 'execution_policy', 'runtime_settings'].every((key) => Object.hasOwn(request.body, key))
      && request.body.runtime_settings && ['plan', 'read_only', 'workspace_write', 'full_access'].includes(request.body.runtime_settings.permission);
    const messageKeys = ['text', 'target', 'request_id', 'work_mode', 'runtime_settings'];
    const naturalMessage = request?.kind === 'message' && (request.body.intent !== undefined || request.body.binding !== undefined);
    const validNaturalMessage = request?.kind !== 'message' || (Object.keys(request.body).every((key) => [...messageKeys, ...(naturalMessage ? ['intent', 'binding'] : [])].includes(key))
      && (!Object.hasOwn(request.body, 'runtime_settings') || validRuntimeSettings(request.body.runtime_settings))
      && (!naturalMessage || request.body.intent === 'continue' && request.body.binding && Object.keys(request.body.binding).length === 3
        && !!runBinding({ id: request.body.binding.run_id, revision: request.body.binding.revision, digest: request.body.binding.digest })));
    const validRecovery = request?.kind !== 'native_recovery' || [4, 5].includes(Object.keys(request.body).length)
      && ['request_id', 'run_id', 'revision', 'digest'].every((key) => Object.hasOwn(request.body, key))
      && Object.keys(request.body).every((key) => ['request_id', 'run_id', 'revision', 'digest', 'user_message'].includes(key))
      && (request.body.user_message === undefined || typeof request.body.user_message === 'string' && request.body.user_message.trim() && validUserText(request.body.user_message));
    const validReassessment = request?.kind !== 'objective_reassessment' || uuidPattern.test(request.objectiveId) && Object.keys(request.body).length === 4
      && ['request_id', 'run_id', 'revision', 'digest'].every((key) => Object.hasOwn(request.body, key));
    if (!['create', 'message', 'cancel', 'run', 'accept', 'fork', 'objective', 'restart', 'native_handoff', 'native_recovery', 'objective_reassessment', 'continue', 'suggested', 'answer', 'pause', 'resume'].includes(request?.kind)
        || !validRoom || !validText || !validParticipants || !validProject || !validRun || !validWorkMode || !validObjective || !validPolicy || !validRestart || !validHandoff || !validNaturalMessage || !validRecovery || !validReassessment) throw new Error();
    if (request.kind === 'message') {
      if (request.body.runtime_settings) request.runtimeDraftToken ||= crypto.randomUUID();
      if (request.body.work_mode) request.workModeDraftToken ||= crypto.randomUUID();
    }
    if (!request.hostOutbox) request.legacyUnknown = true;
    state.scopeChanged = stored.scope !== state.authScope;
    if (request.kind === 'message' && request.composerIndependent === true) {
      // Dedicated actions retain their original receipt independently of the
      // composer. Restoring one must not turn it into an unrelated draft.
      state.sidelinedRequests.set(request.body.request_id, { request, receipt: 'unknown' });
      return;
    }
    state.pending = request;
    if (request.kind === 'create') {
      $('message').value = request.body.goal;
      state.mode = ['python_artifacts', 'python_sandbox', 'project_files', 'project_folder', 'text_artifacts', 'native_project'].includes(request.body.mode) ? request.body.mode : 'text';
      state.selectedParticipants = participants || null;
      state.selectedProject = request.body.project_id || '';
      state.modeExplicit = true;
    }
    if (['create', 'run', 'suggested'].includes(request.kind) || request.kind === 'message' && messageHasRuntimeSettings(request.body)) {
      state.workMode = request.body.work_mode || 'build';
      state.runtimeSettings = copyRuntimeSettings(request.body.runtime_settings);
      if (request.kind === 'message') { state.runtimeDraftDirty = !!request.body.runtime_settings; state.workModeDraftDirty = !!request.body.work_mode; state.runtimeDraftToken = request.runtimeDraftToken || null; state.workModeDraftToken = request.workModeDraftToken || null; }
    }
    if (['run', 'fork', 'answer', 'message'].includes(request.kind)) $('message').value = request.body.text;
    if (request.kind === 'native_recovery' && request.body.user_message) $('message').value = request.body.user_message;
    state.target = request.body.target || 'all';
    state.targetExplicit = true;
    showNotice(t("已恢复上次待确认的请求。只读取记录，不会自动重发。"));
  } catch (_error) {
    try { sessionStorage.removeItem(pendingStorageKey); } catch (_ignored) { /* Storage may be unavailable. */ }
  }
}

async function submitPending() {
  if (state.busy || !state.pending || state.scopeChanged && !(state.hostOutboxSupported && state.pending.hostOutbox?.scope === state.hostOutboxScope)) return;
  const pending = state.pending;
  if (pending.hostOutbox && (!state.hostOutboxSupported || pending.hostOutbox.scope !== state.hostOutboxScope)) {
    showNotice(t("当前连接与原发送记录尚未对应，原文保留，没有重新发送。"), 'error'); return;
  }
  const originalText = pending.body.goal ?? pending.body.text ?? pending.body.user_message;
  if (originalText !== undefined && !checkUserText(originalText)) return;
  if (!state.roomPagingSupported && typeof originalText === 'string' && originalText.length > 3000) {
    showNotice(t("当前本地服务尚未支持长消息。原文已保留，没有发送，请更新服务后再试。"), 'error'); return;
  }
  if (pending.kind === 'message' && messageHasRuntimeSettings(pending.body) && !state.naturalRuntimeSupported) {
    showNotice(t("当前服务尚不支持这条消息里的运行设置，原文和设置已保留，没有重新发送。"), 'error'); return;
  }
  if (pending.receiptBlocked === true) { showNotice(t("这条消息的回执存在冲突，原文已保留，没有重新发送。"), 'error'); return; }
  if (pending.intervened === true) { showNotice(t("这项任务已另行停止或整理，原请求不会重新发送。")); return; }
  if (pending.kind === 'message' && pending.body.intent === 'continue' && !state.naturalContinuationSupported) {
    showNotice(t("当前服务尚不支持这条续作消息，原文和请求已保留，没有重新发送。请更新本地服务后再试。"), 'error'); return;
  }
  const needsPolicyCapability = ['objective', 'restart', 'objective_reassessment', 'continue', 'suggested', 'answer', 'pause', 'resume'].includes(pending.kind)
    || Object.hasOwn(pending.body, 'execution_policy');
  if (needsPolicyCapability && !state.executionPolicySupported) { showNotice(t("当前服务尚未提供自主推进入口，原请求已保留，没有重新发送。请更新桌面服务后再核对。"), 'error'); return; }
  const policy = pending.body.execution_policy;
  if (!state.continuousObjectiveSupported && (pending.kind === 'restart' || policy && (policy.max_calls === null || policy.max_seconds === null))) {
    showNotice(t("当前服务尚不支持这份持续推进请求，原请求已保留，没有重新发送。请更新服务后再核对。"), 'error'); return;
  }
  if (!state.nativeProjectSupported && (['native_handoff', 'native_recovery'].includes(pending.kind) || pending.body.mode === 'native_project' || pending.body.runtime_settings?.permission === 'full_access')) {
    showNotice(t("当前服务尚不支持完整开发，原请求已保留，没有降低权限或重新发送。请更新服务后再核对。"), 'error'); return;
  }
  if (pending.kind === 'message' && !Object.hasOwn(pending, 'runtimeDraftToken')) {
    pending.runtimeDraftToken = state.runtimeDraftToken; pending.workModeDraftToken = state.workModeDraftToken;
  }
  clearActionError();
  state.busy = true; state.busyRequest = pending;
  ++state.epoch;
  // Rendering and durable staging must release the same lock as dispatch.
  try {
    renderRooms(); renderControls(); showNotice();
    pending.storageFailed = false;
    // Stop/pause cannot start work. Browser storage must never delay the user's
    // control over a running process; their result still comes from the host.
    const stopsWork = ['cancel', 'pause'].includes(pending.kind);
    let receivedAck = null;
    if (!stopsWork && state.hostOutboxSupported) {
      try {
        const entry = await stageHostOutbox(pending);
        if (entry.delivery.state === 'received' && uuidPattern.test(entry.delivery.result_room_id)) receivedAck = { acknowledged: true, room_id: entry.delivery.result_room_id, request_id: pending.body.request_id };
        else if (entry.delivery.state !== 'staged' || !entry.delivery.can_send) {
          settleHostDelivery(pending, entry);
          if (state.busyRequest === pending) { state.busy = false; state.busyRequest = null; }
          if (state.pending === pending) showNotice(hostDeliveryMessage(entry), 'error');
          renderRooms(); renderControls(); return;
        }
        void rememberPending().catch(() => {});
        renderRooms(); renderControls();
      } catch (error) {
        if (state.busyRequest === pending) { state.busy = false; state.busyRequest = null; }
        if (!pending.detached && state.pending === pending) showActionError(t("发送记录尚未确认，原文保留，未派发。{p0}", { p0: error.message || '' }), pending.roomId || 'new', pending.kind);
        renderRooms(); renderControls(); return;
      }
    } else if (!stopsWork) {
      try { await rememberPending(); } catch (_error) {
        pending.storageFailed = true;
        if (state.busyRequest === pending) { state.busy = false; state.busyRequest = null; }
        showActionError(t("这条请求尚未发送：无法可靠保存发送记录。原文和设置仍保留，可以重试。"), pending.roomId || 'new', pending.kind);
        renderRooms(); renderControls(); return;
      }
    }
    if (pending.intervened || state.deletedRooms.has(pending.roomId)) {
      if (state.busyRequest === pending) { state.busy = false; state.busyRequest = null; }
      renderControls(); return;
    }
    try {
      if (!receivedAck) pending.attempted = true;
      let snapshot = receivedAck || await api(dispatchPath(pending), 'POST', pending.body);
      if ((state.roomPagingSupported || pending.hostOutbox) && snapshot?.acknowledged === true) {
        const acknowledgedRoom = snapshot.room_id;
        if (!uuidPattern.test(acknowledgedRoom) || !['create', 'fork', 'native_handoff'].includes(pending.kind) && acknowledgedRoom !== pending.roomId
            || snapshot.request_id !== undefined && snapshot.request_id !== pending.body.request_id) throw new RequestError(t("发送回执身份尚未确认。"), { uncertain: true });
        // An ACK proves submission, not that the following read succeeded. Never
        // replay the POST merely because history or metadata is temporarily offline.
        retireOutboxRequest(pending); pending.acknowledged = true; pending.acknowledgedRoomId = acknowledgedRoom;
        if (pending.intervened === true) { settleIntervenedRequest(pending, 'received'); return; }
        if (pending.detached === true) { finishMessageReceipt(pending); return; }
        if (state.pending === pending) state.pending = null;
        if (pending.kind === 'message') clearAcceptedRuntimeDraft(pending);
        const sentText = pending.body.goal ?? pending.body.text ?? pending.body.user_message;
        if (typeof sentText === 'string' && $('message').value.trim() === sentText.trim()) { $('message').value = ''; forgetComposerDraft(pending.roomId || 'new'); }
        state.selected = acknowledgedRoom; rememberRoomLocation(acknowledgedRoom); rememberPending();
        try { snapshot = await readRoomSnapshot(acknowledgedRoom); }
        catch (_error) { showActionError(t("已发送，当前记录暂未加载完成。无需再次发送。"), acknowledgedRoom, pending.kind); return; }
      }
      if (!snapshot?.room || !uuidPattern.test(snapshot.room.id)) throw new RequestError(t("请求可能已经生效，但返回记录无法确认。"), { uncertain: true });
      if (!['create', 'fork', 'native_handoff'].includes(pending.kind) && snapshot.room.id !== pending.roomId) throw new RequestError(t("请求可能已经生效，但返回的任务身份不匹配。"), { uncertain: true });
      if (pending.intervened === true) { settleIntervenedRequest(pending, 'received'); return; }
      if (pending.detached === true) { finishMessageReceipt(pending); return; }
      if (pending.kind === 'fork' && snapshot.room.id === pending.roomId) throw new RequestError(t("另开任务返回了原任务身份，结果仍需核对。"), { uncertain: true });
      if (pending.kind === 'native_handoff' && (snapshot.room.id === pending.roomId || snapshot.room.mode !== 'native_project'
          || snapshot.native_handoff?.source_room_id !== pending.roomId || snapshot.native_handoff?.source_run_id !== pending.body.run_id
          || snapshot.native_handoff?.revision !== pending.body.revision || snapshot.native_handoff?.digest !== pending.body.digest)) {
        throw new RequestError(t("承接结果的来源或开发方式尚未确认，原请求仍保留。"), { uncertain: true });
      }
      if (pending.kind === 'native_recovery' && (snapshot.room.mode !== 'native_project' || !uuidPattern.test(snapshot.current_run?.id)
          || snapshot.current_run.id === pending.body.run_id)) throw new RequestError(t("返回的现场恢复运行尚未确认，原请求仍保留。"), { uncertain: true });
      if (pending.kind === 'objective_reassessment' && (!uuidPattern.test(snapshot.current_run?.id) || snapshot.current_run.id === pending.body.run_id
          || snapshot.objective?.id !== pending.objectiveId || snapshot.current_run.objective_id !== pending.objectiveId)) {
        throw new RequestError(t("重新核对目标的运行尚未确认，原请求仍保留。"), { uncertain: true });
      }
      clearActionError();
      if (pending.kind === 'message') clearAcceptedRuntimeDraft(pending);
      setConnection(true);
      state.forkSource = null;
      const previousRoom = state.selected;
      state.selected = snapshot.room.id;
      applySnapshot(snapshot);
      if (['create', 'run', 'suggested'].includes(pending.kind) && ['plan', 'build'].includes(pending.body.work_mode)) state.workMode = pending.body.work_mode;
      const consumesContinue = ['native_recovery', 'native_handoff', 'restart', 'continue', 'resume'].includes(pending.kind) && isContinueCommand($('message').value);
      const consumesRecoveryMessage = pending.kind === 'native_recovery' && typeof pending.body.user_message === 'string' && pending.body.user_message === $('message').value.trim();
      const consumesDraft = consumesContinue || consumesRecoveryMessage || ['create', 'run', 'fork', 'message', 'cancel', 'answer'].includes(pending.kind);
      if (consumesDraft) forgetComposerDraft(pending.kind === 'create' ? 'new' : pending.roomId);
      if (previousRoom !== state.selected) state.renderedRoom = snapshot.room.id;
      if (consumesDraft) $('message').value = '';
      retireOutboxRequest(pending); state.pending = null;
      if (pending.kind === 'message') {
        clearAcceptedRuntimeDraft(pending);
        const configuration = naturalRuntimeConfiguration(snapshot);
        if (configuration) { state.runtimeSettings = copyRuntimeSettings(configuration.desired.runtime_settings); state.workMode = configuration.desired.work_mode; }
      }
      rememberPending();
    } catch (error) {
      if (error.code === 'room_deleted' && pending.roomId) { forgetDeletedRoom(pending.roomId); settleIntervenedRequest(pending, 'blocked'); return; }
      if (pending.acknowledged === true) {
        showActionError(t("已发送，记录待核对。无需再次发送。"), pending.acknowledgedRoomId, pending.kind);
        return;
      }
      if (pending.intervened === true) { settleIntervenedRequest(pending, error.uncertain ? 'unknown' : 'blocked'); return; }
      if (pending.detached === true) {
        if (!error.uncertain) { pending.receiptBlocked = true; pending.receiptError = error.code; }
        settleIntervenedRequest(pending, error.uncertain ? 'unknown' : 'blocked');
        if (error.uncertain) await checkMessageReceipt(pending);
        return;
      }
      if (error.uncertain) {
        setConnection(false);
        rememberPending();
        showNotice(state.messageReceiptsSupported && pending.kind === 'message' ? t("正在核对发送结果，原文已保留。") : t("结果未确认，原请求已保留。请先核对状态，再决定是否用同一请求重试。"), 'error');
        if (state.messageReceiptsSupported && pending.kind === 'message') await checkMessageReceipt(pending);
      } else {
        retireOutboxRequest(pending, 'abandoned'); state.pending = null;
        rememberPending();
        showActionError(error.message || t("请求未完成，请核对状态。"), pending.roomId || 'new', pending.kind);
      }
    }
  } finally {
    if (state.busyRequest === pending) { state.busy = false; state.busyRequest = null; }
    if (state.hostOutboxSupported) void flushHostRetirements();
    renderRooms(); renderControls();
  }
}

function composerDraftKey(id) {
  const scope = state.authScope;
  return scope ? `composer:${scope}:${id}` : null;
}
function persistComposerDraft(id, draft) {
  const key = composerDraftKey(id);
  if (!key) return;
  // Start from the existing connection in the input event, rather than
  // leaving a pending Promise before reload can close this document.
  const write = (database) => {
    const transaction = database.transaction('scopes', 'readwrite');
    transaction.onerror = transaction.onabort = () => {};
    const store = transaction.objectStore('scopes');
    if (draft && (draft.text || draft.runtimeDraftDirty || draft.workModeDraftDirty || id === 'new' && Array.isArray(draft.participants))) store.put({
      scope: key, text: draft.text, target: draft.target, targetExplicit: draft.targetExplicit,
      ...(id === 'new' && Array.isArray(draft.participants) ? { participants: draft.participants } : {}),
      ...composerDraftSettings(draft),
    });
    else store.delete(key);
    transaction.commit?.();
  };
  try {
    if (outboxConnection) write(outboxConnection);
    else void openOutbox().then(write).catch(() => {});
  } catch (_error) { /* The current draft stays in memory and remains sendable. */ }
}
function composerDraftSettings(draft) {
  const runtimeValid = validRuntimeSettings(draft.runtimeSettings);
  const modeValid = ['plan', 'build'].includes(draft.workMode);
  return {
    ...(runtimeValid ? { runtimeSettings: copyRuntimeSettings(draft.runtimeSettings) } : {}),
    runtimeDraftDirty: runtimeValid && draft.runtimeDraftDirty === true,
    runtimePermissionExplicit: runtimeValid && draft.runtimePermissionExplicit === true,
    runtimeDraftToken: runtimeValid && uuidPattern.test(draft.runtimeDraftToken) ? draft.runtimeDraftToken : null,
    ...(modeValid ? { workMode: draft.workMode } : {}),
    workModeDraftDirty: modeValid && draft.workModeDraftDirty === true,
    workModeDraftToken: modeValid && uuidPattern.test(draft.workModeDraftToken) ? draft.workModeDraftToken : null,
  };
}
function forgetComposerDraft(id) {
  state.drafts.delete(id); persistComposerDraft(id, null);
}
async function loadComposerDraft(id) {
  if (state.drafts.has(id)) return;
  const key = composerDraftKey(id);
  if (!key) return;
  try {
    const draft = await outboxTransaction('readonly', (store, done) => {
      const read = store.get(key); read.onsuccess = () => done(read.result);
    });
    if (!state.drafts.has(id) && draft && typeof draft.text === 'string' && validUserText(draft.text)
        && typeof draft.target === 'string' && (draft.target === 'all' || agentPattern.test(draft.target))) {
      const participants = id === 'new' && Array.isArray(draft.participants) && draft.participants.length <= 8
        && draft.participants.every((value) => typeof value === 'string' && agentPattern.test(value))
        && new Set(draft.participants).size === draft.participants.length ? draft.participants : undefined;
      state.drafts.set(id, { text: draft.text, target: draft.target, targetExplicit: true,
        ...(participants ? { participants } : {}), ...composerDraftSettings(draft) });
    }
  } catch (_error) { /* Storage unavailability does not block editing or Stop. */ }
}

function saveDraft() {
  state.drafts.set(state.selected || 'new', { text: $('message').value, target: state.target, mode: state.mode,
    workMode: state.workMode, modeExplicit: state.modeExplicit,
    participants: [...(state.selectedParticipants || [])], project: state.selectedProject, targetExplicit: state.targetExplicit, runtimeSettings: copyRuntimeSettings(state.runtimeSettings), runtimeDraftDirty: state.runtimeDraftDirty, runtimePermissionExplicit: state.runtimePermissionExplicit, workModeDraftDirty: state.workModeDraftDirty, runtimeDraftToken: state.runtimeDraftToken, workModeDraftToken: state.workModeDraftToken });
  persistComposerDraft(state.selected || 'new', state.drafts.get(state.selected || 'new'));
}

function chooseDefaultArtifactMode() {
  if (state.selected || state.selectedProject || state.modeExplicit || state.pending) return;
  state.mode = state.taskModes.includes('text_artifacts') ? 'text_artifacts' : 'text';
  if (state.mode !== 'text' && state.target === 'all') state.target = state.participantIds[0] || 'all';
}

function startNewRoom({ preserveRoomHash = false } = {}) {
  if ((state.busy || state.pending) && !canDetachPendingRequest()) return;
  detachMessageForNavigation();
  if (!preserveRoomHash) rememberRoomLocation();
  clearActionError();
  closeTaskOptions({ restoreFocus: false });
  closeRuntimePopover({ restoreFocus: false });
  closeUsagePopover();
  closeContextMenu();
  setHistoryExpanded(false);
  saveDraft();
  closeRecipientMenu();
  closeReader();
  ++state.epoch;
  state.selected = null;
  state.forkSource = null;
  state.snapshot = null;
  const draft = state.drafts.get('new');
  $('message').value = draft?.text || '';
  state.target = draft?.target || 'all';
  state.targetExplicit = draft?.targetExplicit || false;
  state.mode = draft?.mode || 'text';
  state.workMode = draft?.workMode || 'build';
  state.modeExplicit = draft?.modeExplicit || false;
  state.runtimeScope = 'new'; state.runtimeSettings = copyRuntimeSettings(draft?.runtimeSettings); state.runtimePermissionExplicit = draft?.runtimePermissionExplicit === true || draft?.mode === 'native_project' && !!draft?.runtimeDraftDirty && ['read_only', 'workspace_write'].includes(draft?.runtimeSettings?.permission); state.runtimeDraftDirty = !!draft?.runtimeDraftDirty; state.workModeDraftDirty = !!draft?.workModeDraftDirty; state.runtimeDraftToken = draft?.runtimeDraftToken || null; state.workModeDraftToken = draft?.workModeDraftToken || null;
  state.selectedParticipants = draft?.participants || state.selectedParticipants;
  if (draft?.participants) state.participantsChosen = true;
  state.selectedProject = state.projects.some((project) => project.id === draft?.project && activeEntry(project)) ? draft.project : '';
  state.selectedParticipants = (state.selectedParticipants || []).filter((id) => state.catalog.some((agent) => agent.id === id && selectableAgent(agent, { retainBlocked: true }))); 
  chooseDefaultArtifactMode();
  renderProjectPicker();
  showNotice(); renderRooms(); renderEvents(null); renderMembers(null); renderControls();
  $('timeline').scrollTop = 0;
  $('message').focus({ preventScroll: true });
}
