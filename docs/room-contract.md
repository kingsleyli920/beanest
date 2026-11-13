# 房间与多轮协作契约

当前契约对应共享任务、同房间多轮目标、可选持续目标推进、用户验收及受限文件产物接口。一个 room 保留稳定成员身份和有序聊天历史；用户可显式邀请已接入伙伴加入现有共享任务或普通文字聊天，原成员保持。普通新用户目标建立 run，推进目标以 objective 关联多个阶段，记录实际调用，不要求逐阶段接受，默认不设置总轮数、调用数或时长上限；用户明确提供的受支持停止条件仍生效。普通续轮沿用各成员专用原生 session。已交付清单、文件版本、预算和验收记录保留。文本及静态文件模式关闭 Agent 原生工具；显式 `native_project` 另按统一权限启用本机 CLI 工具。宿主核对文件发布，不自动回写源目录。`python_sandbox` 的受限 Python runner 与原生本机执行是不同路径。日常操作和验证边界见 [README](../README.zh-CN.md)。

## 单源与分工

- `room_store.py` / `room_service.py`：房间、稳定成员身份、全局有序事件、事务认领、有限自动路由与取消。直接复用 `adapters.invoke`。
- `team_store.py`：同一房间的 run 历史、用户验收、共享任务、版本化分工确认、自主认领、DAG依赖、具体求助及独立结果互审；任务交付与单次模型调用分别存储。`collaboration_view.py`根据实际任务、文件与执行记录生成交付展示，不采用模型自称“完成”作为验收。
- `room_server.py`：复用既有 loopback HTTP 安全壳，提供房间 API；浏览器和桌面窗口使用同一服务。
- `room_ui/`：统一群聊界面，展示真实成员/任务/状态与消息来源；没有手动“转交审核”阶段按钮。
- `project_catalog.py` / `folder_workspace.py` / `folder_protocol.py`：项目metadata登记、默认文件夹模式的显式捕获、SHA blobs/不可变index与宿主按需查找/读取/搜索。`project_workspace.py`保留高级指定文件及新建文本/Python的完整JSON快照。源目录不写入，主机不导入生成文件。
- `partner_manager.py`：Claude/Codex/Kimi 独立伙伴配置、只读安装发现，以及用户配置的本机 JSON 连接器模板和显式接入检查。
- `runtime_settings.py`：每轮统一宿主权限及逐成员模型/effort的严格规范化和目录校验；不是原生整机工具的授权入口。
- `execution_policy.py` / `objective_store.py`：显式推进策略规范化，以及目标总账、阶段、暂停、检查与决策的持久化；沿用现有调度器。
- `artifact_export.py` / `svg_preview.py`：校验当前文件快照后生成不含私有元数据的 ZIP，以及白名单静态 SVG 预览。
- `python_runner.py`：可选的新建 Python 隔离执行，不接收已有项目模式，runner 不调用 provider。
- `workflow_trial.py`：显式启动的受控多阶段试验驱动，持久化阶段来源和总配额；不作为日常产品工作流，也不自动恢复模型任务。
- `desktop/`：独立桌面容器与 Python 服务生命周期。采用 Electron 44.9.3；本机薄壳带专用 bundle ID，由启动脚本生成，不是可移植发行包。

## HTTP / 服务契约

JSON 响应采用以下形状；新服务的错误仅返回受控 code，不返回原始异常。API 经过 token / Host / Origin 校验。Web 和 MCP 使用独立作用域凭据；静态首页只注入 Web token，MCP 发现文件只发布 MCP token。认证按实际凭据确定来源，不能靠改 URL 冒充用户。作用域不匹配统一返回 `403 token_scope_rejected`；详见 [SECURITY.md](../SECURITY.md)。

- `GET /api/status` → `{app:"local-agent-rooms", capabilities:{text_only:true, tools:false, execution_policy:true}, autonomous_work_active:boolean, ...}`；不得调用模型，仅状态/能力。旧服务可缺少推进策略标记；认证检查可由显式服务初始化或单独 doctor 完成。
- `GET /api/rooms` → `{rooms:[{id,goal,status,created_at,updated_at,...}]}`；共享任务条目另有 `outcome_state`，有objective时另提供受控`objective_status`。界面优先区分阶段已交付、目标已交付、暂停、需决定及停止，不把awaiting_continue写成等待验收；旧条目缺字段保持原展示。
- `GET /api/rooms/<uuid>/code-status` 保留选定工作区的只读详情，并返回 `delivery_repository` 与 `delivery_summary`（`scope:task_linked_workspaces`）。登记目录为真实 Git 仓库时默认作为交付目标；否则需显式指定。`main/remote/workspace` 只核对交付目标，已接受报告中的可读文件引用只供证据与详情，不能因此获得必须发布整个仓库的角色。主分支祖先、当前工作区、内部 S 相对捕获基线的变化、实际主线 blob SHA/mode及保留私树分别核对；内部 baseline Git 不是实际源仓。显式目标优先核对交付文件，其他证据中的完美副本也不能冒充目标已包含成果。部分匹配保留计数与缺失，不丢文件或冒全部完成。仅 `?refresh_remote=1` 对目标的规范 GitHub HTTPS/SSH 远端执行只读 `ls-remote`，普通读取不联网、不 fetch。`request` 只给目标仓库和真实目标分支，普通消息/outbox仍处理提交同步要求；宿主不执行 Git 写操作。 `delivery_summary.branch` 给出目标仓库当前分支（`name/detached/mainline/relation/ahead/behind/work_branch/upstream`，目标状态不可读时为 null）。`work_branch` 只在目标处于有名分支、`relation` 为 `unmerged` 且提交不在本地主分支时为真；此时 `remote` 改为工作分支的推送状态（`work_branch_not_pushed`、`work_branch_no_upstream`、`work_branch_upstream_unknown`），只读本机跟踪引用，不做 `ls-remote`，也没有 `checked_at`。`main` 的 `state` 和 `reason` 不变，由界面按中性显示；主分支、游离 HEAD 和 `published` 的判断都不放松。
- `POST /api/rooms/<uuid>/delivery-repository` 仅用户 Web 路径允许，MCP 拒绝；正文严格只有 `workspace_id`，不能提交任意目录。宿主重新发现候选，排除内部 native baseline，并核对 Git 根、common-dir 与 device/inode 后持久到 `room_delivery_repositories`。普通同房后续 run 保留选择，其他房间不自动继承；读取不写绑定。目录暂不可读与实际 Git 身份改变分别标明，不能悄悄改选。保存不发送模型工作；保存中或结果未知时，界面不能沿用旧目标派发同步，读回确认只用 GET、不自动重发 POST。
- `POST /api/rooms`，`{goal,target,request_id,mode?,work_mode?,participants?,project_id?,collaboration?,runtime_settings?,execution_policy?}`；mode 默认 `text`，另有 `python_artifacts`、`text_artifacts`、`project_folder`、`project_files` 和 runner ready 才提供的 `python_sandbox`。`work_mode` 与成果类型独立，见下节。`participants` 为 1–8 个不重复的新房可用伙伴 ID；生产默认需已启用且最近发现安装可用；连接器另需协议ready。省略时兼容已有调用者的可用目录；目录超过8位时拒绝并要求显式选择，UI总是传显式子集。target 必须为本间成员 ID 或 `all`（兼容旧 `both`）。`collaboration:true`要求2–8位成员，target的首位接收者只担任初始清单整理人，全部成员参加规划、确认和认领；未带推进策略且collaboration省略或false时保持旧协议，旧协议的文件模式须选一位首写者。显式推进策略下单成员也使用任务板并明确自检。`project_folder`与`project_files`必须带对应类型的已登记`project_id`，其他模式不能带它。已有项目的UI默认选择folder，API省略mode仍为text。用户提交才创建并启动 → room snapshot。
- `GET /api/rooms/<uuid>` → room snapshot。
- `POST /api/rooms/<uuid>/members`，精确正文 `{request_id,participants:[profile_id]}`，仅 Web 用户可调用；MCP 返回 `403 user_member_invitation_required`。支持既有共享任务及普通 `text` 房；旧无共享任务的文件房返回 `invitation_mode_unsupported`。事务追加成员，总数沿用 8 位上限；同 key/body 或另一 key 邀请已在房间的成员均不重复加入或派发。运行中的规划/工作阶段只给新成员安排既有 plan/coordinate，原在途调用与 owner/epoch 不重置；已交付、暂停、停止、需回复或 checkpoint 阶段先加入待命，不创建目标或恢复任务。既有权限、清理和可用性规则保持。新原生成员首次工作树来自当前确切 canonical，不继承它未参与的旧 handoff 私有差异。邀请复用 `user_outbox` 的 `invite` kind 和小 ack；刷新核原 key，归档/移除仍使未提交请求失效。

Snapshot 的 `member_invitation` 提供 `supported/can_invite/reason/candidates`，候选只有当前可接入且未在本房的伙伴，读取不启动 CLI。`members[].joined_pending` 仅表示确有邀请事件且加入后还没有实际 claim；界面显示待安排，不冒充参与过旧交付。加入以 `member_invited` 事件记录，不伪造用户补充消息；历史 run snapshot、交付与审核保持。
- `POST /api/rooms/<uuid>/messages`，`{text,target,request_id}` → room snapshot；补充当前目标的信息，仅为空闲成员排入有限队列，忙者在后续上下文读取，不隐式扩大预算或重开已交付任务。新目标使用 `/runs`。
- `POST /api/rooms/<uuid>/runs`，`{text,target,request_id,run_id,revision,digest,retry,work_mode?,runtime_settings?,execution_policy?}` → room snapshot；正文键集必须精确，`retry` 必须为 boolean。用户显式新目标建立下一轮；同objective的继续或回答改用下文专用路由，不重开总账。具体推进及重试门槛见下文。
- `POST /api/rooms/<uuid>/accept`，`{request_id,run_id,revision,digest}` → room snapshot；仅 Web 用户凭据可记录接受本轮，不关闭聊天。MCP 同名路由返回 `403 user_acceptance_required`。
- `POST /api/rooms/<uuid>/fork`，精确正文 `{text,target,request_id,run_id,revision,digest}` → **新房间** snapshot。仅 Web 用户可显式从已提交 Python 快照另开任务，`text` 最多3000字符；继承原成员、文件 owner 和 Python 模式，不开放 MCP，后者返回 `403 user_fork_required`。
- `GET /api/rooms/<uuid>/runs?run_id=<uuid>` → `{run,team,revision,digest}`；历史轮返回冻结清单、交付和审核记录，不返回归档的原生 session 映射。
- `GET /api/rooms/<uuid>/export?revision=N` → `{filename,content_type,content_base64,revision,snapshot_sha256,sha256,files_count}`，ZIP 字节以 base64 返回；`snapshot_sha256` 绑定原始文件快照，`sha256` 绑定 ZIP 原字节。
- `GET /api/rooms/<uuid>/preview?revision=N&path=<relative.svg>` → `{content_type,content_base64,sha256,source_sha256,revision,path}`；`sha256` 为安全预览字节 hash，`source_sha256` 为原文 hash。预览与导出均只读且只接受当前精确版本；`revision` 必填且只能出现一次，为十进制非负整数，重复或额外 query 键拒绝。
- `POST /api/rooms/<uuid>/cancel`，空对象 → room snapshot；取消后核对自身子进程，不把“停止派发”当退出。
- `GET /api/projects` → `{projects:[{id,name,path,files,created_at,selection_kind?}]}`，不返回文件正文；`POST /api/projects`接受默认目录登记`{name,path,selection_kind:"folder"}`，其记录为`files:[]`；旧`{name,path,files}`继续登记指定文件。均仅用户配置入口可用，不启动模型；目录登记不遍历或读取正文。
- `POST /api/projects/<id>/name`，正文严格为`{"name":"新名字"}`，只改登记里的显示名，返回`{project,projects}`；不动项目目录、路径、文件列表和已有任务的副本。名字去掉首尾空白后为1–80字，含控制字符或疑似凭据返回400 `invalid_project_request`，id不存在返回404 `project_not_found`，正在删除返回409 `project_deletion_pending`；名字不变不写盘。已归档项目也能改名。仅用户配置入口可用，MCP返回403。
- `GET /api/partners` → `{partners,supported_adapters,connection_templates,connection_protocol,discovery,bean_styles,observed_at,scope:"coordinator_owned_only"}`，包含已保存 health 与本服务 monitor，不触发检查。
- `POST /api/partners` 接受 `{id,name,adapter,avatar?,bean_style?,connection?,api_model?}`；Claude/Codex/Kimi 使用原生适配器，`local_json` 为待配置的本机 JSON 连接器模板，不表示厂商原生 CLI 已兼容（以前保存的 `deepseek` JSON 模板仍按原协议读取，新加的按下面的 API 模型伙伴）。`bean_style` 仅为固定装饰形象，由服务目录提供。
- **API 模型伙伴**：带 `api_model` 的伙伴由宿主 CLI（当前只有 `adapter:"claude"`，即 Claude Code）连到用户自己的 Anthropic 兼容接口，按量计费。`api_model` 严格为 `{host,base_url,key,currency,timezone,peak_windows,default_model,models,caps}`：`base_url` 必须是 https、有主机名、不带账号密码/query/fragment，结尾不能是 `/` 或 `/v1`（宿主自己会拼 `/v1/messages`）；`key` 只能是位置引用，`{kind:"env_file",path:"/绝对路径",variable:"变量名"}` 或 `{kind:"keychain",service,account?}`，出现其他字段（包括 key 值）返回 400 `invalid_api_key_reference`；`models` 为 1–16 个 `{id,input,output,cache_read,cache_write,peak?}`，价格按每百万 Token、用 `currency`（三个大写字母）计，`peak` 为同样四项的高峰价；`peak_windows` 为至多 8 段 `{start:"HH:MM",end:"HH:MM",days?:[0..6]}`（0 是周一，`start>end` 表示跨午夜，凌晨部分算开始那天），按 `timezone`（IANA 名称）判断；`caps` 为 `{daily,monthly}`，各为正数或 null（不限）。厂商名只出现在 `GET /api/partners` 的 `api_model_presets` 预设里，派活路径不区分厂商。
- 豆窝**从不保存 key**：`profiles.json`、事件、提示词、回执和 API 响应里只有位置引用。每次调用开始前读一次 key，只放进这一个宿主子进程的环境：`ANTHROPIC_BASE_URL` 与 `ANTHROPIC_AUTH_TOKEN`，外加该伙伴自己的 `CLAUDE_CONFIG_DIR`（伙伴目录下的 `homes/<id>`，与用户本机 Claude 登录分开）。其他伙伴、豆窝自己起的辅助进程（如进程表读取）都拿不到这两个变量；用户 shell 里的 `ANTHROPIC_*`/`OPENAI_*`/`*_BASE_URL` 一律不传。宿主输出在解析、写回执、显示进度之前先把 key 原文替换成 `[REDACTED]`。读不到 key 时调用不启动（`never_started`），按未发出提示词的失败交给同伴。命令行总带 `--model`（未选时用 `default_model`），并在 `--settings` 的 `env` 里把宿主的模型别名（`ANTHROPIC_MODEL`、`ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL`、`CLAUDE_CODE_SUBAGENT_MODEL`）指向同一模型。
- API 模型伙伴不跑订阅检查：`claude auth status`、额度 SDK 查询、限额探测都不对它执行，`POST /api/partners/<id>/account/refresh` 与 `account-source` 返回 409 `account_adapter_unsupported`。它的 `account` 为 `{source:"api_endpoint",auth_method:"api_key",endpoint:<主机名>,...}`；`POST /api/partners/<id>/check` 只核对宿主 CLI 版本和 key 能否读到，不请求接口，读不到为 `health.reason:"api_key_unavailable"`。模型目录直接取 `models`（`catalog_source:"api_model_catalog"`），不启动 CLI。
- 花费：每次调用返回后，按调用报告的 `input/output/cache_read/cache_creation` Token、所用模型和调用时刻是否在高峰时段计价，计入伙伴目录下 `api-spend.json`（权限 600）的当天与当月累计；新的一天或一月第一次读写时从零开始。没报告用量的调用计入 `month_unpriced_calls`，不计金额。伙伴条目带 `spend:{currency,day,day_spent,month,month_spent,month_calls,month_unpriced_calls,caps,capped,left_percent,state}`。到任一上限时 `availability` 为 `{state:"blocked",reason:"spend_cap_reached",source:"api_spend_ledger"}`，派活前的可用性检查按额度用完处理：不再派新活，已有任务交给其他伙伴；只剩它时房间按额度等待处理，等到上限解除再继续。剩余不到较紧那个上限的 15% 时进入储备（`quota_reserve` 为剩余百分比），与订阅额度的 15% 储备走同一套排序。花费记录读不出来时同样按封顶处理。金额是按用户自填价格的估算，不是账单。
- `POST /api/partners/<id>/api-model` 正文严格为 `{api_model:{...}}`，可改价格、上限、模型、高峰时段、时区和 key 位置；`host`、`base_url`、`currency` 是伙伴身份，改动返回 409 `partner_identity_conflict`（已有会话属于原接口，账本按原币种记）。改 key 位置会清掉上次检查结果。MCP 的 `GET /api/partners` 剥离 `api_model`。
- `POST /api/partners/discover` 接受空对象，刷新 PATH / 固定位置安装观察。Claude/Codex 只看安装位置；Kimi 已安装时额外做一次不发提示词的登录状态握手。不调用模型，不重写配置或恢复手动停用者。
- `POST /api/partners/<id>/connection` 接受 `{connection:{protocol:"beanest-json-v1",executable:"/absolute/executable",args:[],billing:"existing_subscription"|"local"}}`；用户须提供可信本机协议实现，不能用任意厂商 CLI 参数替代。未配置或未通过接入检查的模板不能参与；既有连接身份不能改写，冲突返回 `partner_identity_conflict`，非法连接返回 `invalid_partner_connection`。
- `POST /api/partners/<id>/enabled` 接受 `{enabled:boolean}`，改变新房间名单；`POST /api/partners/<id>/check` 接受空对象，显式执行有界版本、认证或协议 ready 检查，不调用模型。检查后刷新可选状态。

- `GET /api/runtime-options` → `{permissions,partners,scope}`；按伙伴返回本服务缓存的模型目录，不执行CLI。`POST /api/partners/<id>/runtime/refresh`接受空对象，显式执行有界官方模型元数据查询，不开启模型thread/turn。同一原生adapter共用55秒缓存与并发合并；不支持的连接器不能自定义模型。

目录中每项的`id`是实际传入CLI的选择，`name`是显示名，`efforts`仅属于该型号。Codex源为`codex_app_server_model_list`；Claude源为`claude_sdk_initialize`，只发送initialize并保留可选`resolved_model`，不发user或get_usage。别名当前解析的版本不表示永久pin；未声明supportsEffort的型号提供空efforts，不从其它型号或全局帮助继承。仅旧CLI成功initialize但缺models能力字段时可回退到标明来源的帮助别名目录，effort能力未知；超时、协议错误、空或畸形目录不生成猜测选项。用户打开模型选择器可显式触发该只读刷新，运行中只查看冻结快照，不刷新或改本轮配置。

`runtime_settings`为`{permission:"read_only"|"workspace_write"|"full_access",members:{"<participant-id>":{model:null|string,effort:null|string}}}`。仅当前成员可出现；effort必须属于所选模型的目录项。省略保持兼容，显式默认使用null；历史没有保存的配置仍为null。宿主按work_mode重新计算effective_permission，plan始终只读，客户端不能自行提高它。build+只读可讨论并交付文字，不写文件、不执行，即使成果类型为python_sandbox也不要求runner或执行验收；可写sandbox仍必须满足真实执行门。每轮冻结此配置，同轮全部续派、协助和互审使用相同选择；同一objective的自动阶段及继续动作也不改权限或模型，显式新用户目标才可重新选择。静态模式的原生 CLI 仅接收模型及 effort，原生工具关闭；`full_access` 只允许显式 `native_project`。权限由宿主及该原生适配器的实际能力共同执行，不能把工作目录当作操作系统沙箱。

模型目录与设置管理只开放Web用户作用域；MCP不能刷新目录或提交新的runtime_settings，返回`403 user_runtime_settings_required`，未带设置的兼容请求保持既有范围。目录可用不等于推理或账号额度保证，requested配置不冒充供应商未返回的effective模型/effort。

项目登记、伙伴添加、启停与健康检查不开放为 `/api/mcp/` 管理路由；该命名空间只能列目录并操作已有登记范围内的房间；伙伴列表剥离本机 `connection`（执行路径与 argv）。这里是本机单用户控制面，不提供对同用户恶意进程的安全隔离。

UI日常入口为“新任务”，默认选择Claude Code、Codex两种工具；写需求后点击“开始任务”。输入区只保留短文本框和一行操作；“伙伴”“项目”“···”按需打开任务选项浮层，底部每位伙伴的模型与effort入口、短额度入口相邻显示。模型浮层只展示点中的伙伴，统一权限另设入口。选2–8位时新建请求带`collaboration:true`；未带推进策略的单成员保持原直接答复协议，显式策略下则有任务板及自检。恢复旧待确认请求时不补字段、不改变原请求语义；带新策略的待确认请求遇到无capability服务时保留且拒绝重发，不删除字段冒充旧请求。“工具与状态”查看监控，“本次工具”查看当前成员。输入区不提供团队总时长、调用次数或逐段暂停设置，代码与测试选项按需展开。同聊顶部显示任务、负责人和等待项，末尾显示交付、具体停止原因、尚未完成项和下一步；不增加手动转交按钮。历史标题和日期仅改变展示，不改原目标或记录。同工具额外profile放在“更多会话（高级）”，仅勾选者参与；“新建独立会话”不增加新工具、账号或订阅，复用同一adapter和登录。ID、默认名称和角标可由UI生成；提交结果未知时保留同一ID和表单，仅GET核对，不能自动再POST。

桌面preload提供默认无参数`chooseProjectFolder()`与高级旧入口`chooseProjectFiles()`：固定IPC核对主窗口、主frame和同源，限制一个在途选择；前者只返回用户选定目录，后者返回目录及1–12个文件元数据，均不扫描或读取正文。成功只填表，点击“添加项目”后才调用登记API；取消保留表单。Web不具备该桥，保留手动填写。

生产默认服务在启动时做一次支持CLI安装位置检测，仅使用可信适配器的固定resolver，不执行CLI或读取配置/聊天/凭据。`GET /api/partners`另含`discovery:{state,observed_at,tools:[{adapter,name,installed}],scope:"supported_cli_locations"}`，每profile带`installation:{state:"found|missing|unknown"}`；不暴露安装路径。该快照与health认证检查独立，GET不重扫，显式 `/api/partners/discover` 可刷新观察。新房间只能选已启用且安装可用的伙伴；连接器还须通过协议 ready 检查。已有builtin身份复用，不重复生成profile、不改已保存配置或重新启用手动停用者。旧房间身份和处理路径不变；显式注入测试registry/manager可保持原有未启用检测行为。

服务 async 方法：`status()`、`create_room(goal,target,request_id,*,source="user",mode="text",participants=None,project_id=None,collaboration=False,work_mode=None,runtime_settings=None,execution_policy=None)`、`list_rooms()`、`read_room(room_id)`、`send_message(room_id,text,target,request_id)`、`cancel_room(room_id)`、`expire_tasks()`（共享 HTTP watchdog 调用）、`shutdown()`。返回普通可 JSON 序列化 dict。项目与伙伴另有 `list_projects()` / `register_project(name,path,files=None,*,selection_kind="files")` / `list_partners()` / `add_partner(...)` / `set_partner_enabled(id,enabled)` / `check_partner(id)`。服务和存储错误只向 HTTP 返回稳定 `code`。

新增服务方法为 `start_run(room_id,text,target,request_id,*,run_id,revision,digest,retry=False,source="user",work_mode=None,runtime_settings=None,execution_policy=None)`、`accept_run(room_id,request_id,*,run_id,revision,digest,source="user")`、`read_run(room_id,run_id)`、`export_room(room_id,revision)`、`preview_room(room_id,revision,path)`；伙伴另有 `refresh_partners()` / `configure_partner(id,connection)`。它们沿用同一存储和调度器，不启动第二个服务。

`fork_room(room_id,text,target,request_id,*,run_id,revision,digest,source="user")` 仅支持共享任务的 `python_artifacts/python_sandbox`，`current_run.can_fork` 表示当前是否可用；不因原房间8轮已满而禁止。来源必须匹配当前 run、已提交 revision 与完整快照 digest，有非空可读文件，无排队/在途工作、提交不确定或孤立执行；所有未知/中断调用和执行须有持久清理证明。原身份可不确定，但新房间必须使用新原生 session；原房间、历史与未知结论保留，文件字节/SHA/owner 原样种入新房间 revision 1，旧任务、互审和执行通过记录不继承，`artifact_forked` 记录来源。新房间使用普通预算，原房间累计不重置。精确重复 request_id 在源状态/CAS校验前返回已建新房；不同参数冲突，不重复派发。缺清理回执返回 `team_cleanup_unconfirmed`，普通取消不能仅因没有内存 worker 而补成成功证明；worker 只有在可信 adapter 调用及其取消清理已实际返回后持久记录 `job_cleanup`。执行清理异常、未知提交或来源不可读时保持拒绝，不自动新建。

未知/中断 job 必须逐项具有 `job_cleanup.cleanup_confirmed=true`；旧版房间的 aggregate `cancellation_confirmed=true` 不能替代具体 worker 回执，`_stop` 也不得据此早退或因没有内存 worker 而确认清理。必要的可信操作员核对必须绑定确切 job 后补记逐项清理证明，不能把未知结果改成成功。

## 持续目标推进

服务通过 `status.capabilities.execution_policy:true` 声明策略能力；当前新请求统一 `mode:autonomous,max_seconds:null,max_calls:null`，额度接手开启。默认省略或 null 的总调用数与时长没有硬停；显式 autonomous 请求中的受支持 max_calls/max_seconds 约束总目标，阶段不会重置它们。旧 manual 模式兼容为自主持续推进，不恢复逐阶段审批；旧记录的底层历史计数保留。省略或 null 不把简单文字请求强制升级为 objective。运行方式、权限、成员模型和 effort 仍按现有契约冻结，持续推进不能提高权限。

新目标保存`objective_id`、原始总目标、绝对期限、总已用调用数及策略。每次实际认领扣总账，排队占用剩余预算；用户设置了明确上限时，自动阶段不重置总账或绝对期限；自主模式不设默认阶段数硬停。有实质文件变化的正常返修不按次数截断，重复未变证据按无进展处理。执行/修复先于checkpoint，旧worker持久清理回执未确认时不能转段或接手。`autonomous_work_active`为供桌面读取的只读布尔状态；不发模型提示，不含账号信息。

一个阶段完成后，只允许一次只读`checkpoint`提案及一次其他可用成员的`checkpoint_review`。提案包含继续/完成/需要用户、具体下一步、原目标引用及问题；审核绑定proposal_digest。范围引用与独立核对降低误扩展风险，不是语义正确性的认证。自动模式只派发确认属于当前目标的下一步；不同意见或新范围必须停下。同一人核对明确记为self_check。模型报告目标完成不写accepted_at，仍保留用户验收动作。

checkpoint上下文顶层`goal`始终为原总目标，当前阶段另列`current_stage_goal`，不能把阶段验收完成当成整个目标完成。提案和审核都绑定实际送给模型的直接用户补充水位；调用期间有新的直接补充时，旧调用记录保留，旧结论标记被更新取代，再核对原目标，不沿用旧完成结论。这个重新评估不重置预算、权限或期限，也不重放工作调用。

Web用户入口均返回当前snapshot，使用稳定request_id记录参数指纹，重复调用不重复派发：

| 路由 | 请求字段与用途 |
| --- | --- |
| `POST /api/rooms/<uuid>/objective` | `request_id,run_id,revision,digest,execution_policy`；仅can_start_objective时，将已交付旧目标明确纳入评估，冻结旧交付baseline，不改原run或accepted_at |
| `POST /api/rooms/<uuid>/continue` | `request_id,run_id,revision,digest`；精确匹配next_step.binding，只运行已冻结下一步，没有新prompt、策略或预算 |
| `POST /api/rooms/<uuid>/restart-objective` | `request_id,run_id,revision,digest,execution_policy`；仅权威 `can_restart_objective` 允许时，建立新目标/运行并结构化继承未完成任务，保留 owner、路径、依赖及已完成基线；新 epoch 重新核授权，旧终态、预算与调用不改、不重放 |
| `POST /api/rooms/<uuid>/reassess-objective` | 精确 `request_id,run_id,revision,digest`；仅权威 `can_reassess_objective` 允许且无在途、实际清理明确时，沿同一objective建立只读评估运行。原总目标、累计调用、期限、权限、模型、额度停用状态及交付保持，旧完成决定留存；不采用旧建议开启新预算，不代为接受、不重放旧工作。原生准备阶段继续通过现有同步及`collect([])`拒绝未发布或冲突改动，入口不预先保证整个CWD干净。界面入口在“管理任务 → 重新核对目标” |
| `POST /api/rooms/<uuid>/pause`及`/resume` | `request_id,objective_id`；暂停等待当前调用清理，恢复保留原目标、总已用和期限，不重放未知调用 |
| `POST /api/rooms/<uuid>/answer` | `request_id,objective_id,run_id,revision,digest,text`；仅can_answer允许的同目标决策回答，新阶段仍用原总账；扩大范围不能借此推进 |

这些管理动作及新execution_policy不开放MCP；Agent消息不能代替用户配置、回答、接受或新目标授权。需要回答时普通/runs返回objective_answer_required，避免回答被错误地记成新预算。超范围建议仅通过用户显式新目标请求开始，不能自动新建 objective 扩大原目标范围。

已完成目标之后收到补充，仅保存为待重新核对的记录；不创建空worker，也不把完成状态伪装为正在运行。显式重新核对时将补充纳入原目标评估。

`snapshot.objective`提供`id,original_goal,mode,execution_policy,status,deadline,budget,stage,next_step,can_answer,retired_partners,baseline,delivery`。下一步包含确切源绑定、范围、问题及can_start；can_start_objective和can_start_suggested_goal由服务结合清理、版本和生命周期计算，界面不能仅凭旧can_continue推断。

额度接手只接受已知额度原因，且仅在原已选的兼容成员之间进行；同一登录的多个别名共用额度，换别名不能绕过限额。原调用必须确认清理，接手增加assignment_epoch，内部grant冻结确切写路径、原owner和SHA；模型及HTTP/MCP不能提交或扩大grant。旧epoch晚回、错SHA、额外路径整批拒绝。原历史owner保留，新写入才更新当前owner。已完成任务不重做，待修改任务不派回已退役成员；剩下一位时任务为self_checked，不能称为独立审核通过。自主目标开启额度接手时，全员额度用完会保留进度，每个房间最多每 5 分钟复查一次；拿到额度恢复的证据（伙伴状态明确为 available，或用量窗口还有剩余）才沿同一任务板自动继续，完全没有用量数据时最多每小时试一次；身份或清理状态未知时停下并保留现场。

`objective.delivery`只汇总同目标各阶段已通过独立审核或自检的result_files，按路径保留最新确切SHA，并核对当前私有版本，提供缺失/变化清单。来源run、revision和digest保留；旧库存不当作新交付。明确接续旧目标的成果单列在baseline，不能算作本目标新写入。

`GET /api/rooms/<uuid>/file?run_id=<uuid>&revision=N&digest=<sha256>&path=<relative>&sha256=<sha256>`只读确切已交付历史正文。服务核源run、任务结果路径/SHA、版本digest和实际内容SHA；不匹配拒绝，不回退当前版本。响应为`{run_id,revision,digest,path,sha256,content}`；界面再次核绑定与正文SHA，不能将迟到或其它聊天的正文套入当前阅读器。

## 显式完整开发

`status.capabilities.native_project:true` 与 `task_modes` 共同声明支持；缺少能力时前端不得发送新模式或扩大权限。该模式仅从 Web 用户入口开启，MCP 不能为旧任务新增 Full Access。运行配置与当前目标冻结；模型/effort 可分别选择，权限由全体共用。Claude 的较低权限文件工具不代表具备与 Codex 相同的 shell 沙箱。Full Access 实际允许本机文件、命令及网络；开发副本不是文件系统围栏，只承诺宿主不自动合并回源项目。

`POST /api/rooms/<source>/native-handoff` 使用 `request_id,run_id,revision,digest,execution_policy,runtime_settings`；宿主核权威 `can_native_handoff`、身份、清理、来源与确切版本，冻结原目标及未完成板，并从同一登记项目捕获最新工作树。旧捕获 B、当前源 S、旧私有改动 D 按 SHA 三方交接，不静默用 D 覆盖 S。冲突原字节供负责伙伴在私有副本解决。新房间使用豆窝拥有的新原生会话，不接管用户原终端；旧房间与记录保留，幂等重试不重复派发。`objective.baseline.source_room_id` 指定旧成果正文的实际房间，阅读器须向该来源核对原 run/revision/digest/SHA，不能替换成当前房间。

每位伙伴保持独立、稳定的完整工作目录。宿主同步确切共享版本，保存尚未发布的本地改动，并按文件授权、当前 owner/epoch 和 SHA 发布实际变化；声明路径是授权上界，不要求为凑清单改动所有文件。未变路径需 `unchanged_paths` 理由，列出授权范围以外的文件只当作说明丢弃，不会让房间停下（真实越权改动仍由宿主比对拦住）；保留旧冲突版本需 `resolved_conflicts` 明确选择。伙伴在自己授权路径上的正常改动都会导入共享副本：文本、二进制（图片等，按原样字节保存为非文本条目）、删除（该版本索引里不再有这个文件；删掉的是原项目捕获过的文件时，快照用 `deleted_files` 列出，任务的 `result_files` 记为 `{path, sha256:null}`）和可执行位变化（索引条目的 `executable`）。伙伴已写进项目的文本即使看着像密钥（如 `apiKey: config.apiKey`）也照常导入，不再拒收。`.env`、密钥文件等凭据路径以及构建/缓存目录不复制进共享副本，留在私有树并列入 `generated`，不让任务失败。只有越权（`native_path_not_owned`）、未解决冲突和超过单文件上限的变化保留在私有树并明确列出，不能算成交付。审核、求助和检查点的上下文遇到二进制文件只给路径、大小和 SHA，不给正文；ZIP 导出带上二进制原样字节，清单的 `deleted` 列出已删除的捕获文件。

房间外的进展：完整开发房间若能确定真实交付仓库（本房间用户绑定的交付仓库；或本身就是 Git 仓库的登记目录；登记目录不是 Git 仓库时，沿用同一登记项目最近一次的用户绑定，`delivery_repository.source` 记为 `project_history`，在本房间重新选择即覆盖），宿主在每次派发原生调用前读取该仓库 HEAD，与房间上次派发时记录的 HEAD 比较。期间新增、且提交时间不落在本房间任何调用运行时段内的提交，视为房间外完成的工作（用户、其他 Agent 或其他房间），记一条 `source_advanced_externally` 系统事件（仓库名、前后 HEAD、最多30条提交摘要、是否完整），在聊天里显示，并作为 `context.external_source_changes` 附在本 run 的后续提示里，要求伙伴先核对当前仓库，已在外部完成或被取代的任务凭提交收尾、不重做。历史改写或分支切换使旧 HEAD 不再是祖先时记为不完整。升级前已有的房间首次观测时，以其最后一次成功完成的调用结束时刻的首父提交为起点（失败或取消的调用没有推进任务板）。房间调用运行期间别人做的提交会被算作房间自己的，这是有意的保守取舍；读取失败只跳过观测，不阻断调用。

协议类中断仅在服务端 `current_run.can_recover_native` 允许时，通过 Web 用户 `POST /api/rooms/{room_id}/recover-native` 恢复。请求为 `{request_id,run_id,revision,digest,user_message?}`，不接收新权限、策略或目标；MCP 不可调用；自主目标中，若[停止策略](#停止策略)判定可以自动继续，宿主会代用户发起一次恢复。可选补充正文与原绑定一起纳入幂等指纹，在同一恢复事务中写入用户消息，排队前供上下文读取；审计或事务失败不留下消息或新运行，客户端保留原请求与草稿，不恢复后另发。服务端核对实际进程清理、确切发布版本及私有目录 SHA，拒绝未发布修改、缺失或符号链接等未知现场。成功后原运行/unknown记录不变，同房间建立新运行及新会话，继承权限、模型和清单，当前有效策略不再执行旧人工上限；已交付结果接续审核，不重放旧调用。普通 retry 不得绕过该审计。房间详情与 state 轮询显示的 `can_recover_native` 可复用上一次审计结果，条件是房间记录、共享版本、各私有树的文件状态和本房间回执都没变，且距上次审计不到 30 秒；外部进程改了这些以外的东西时，按钮状态最多晚 30 秒更新。recover-native 和宿主代按的继续都会重新完整审计，不读这份结果。此核对只覆盖已管理文件与已知进程，并不证明 Full Access 的所有外部副作用均不存在。 旧版本曾把合法的后台任务元数据误判为 `provider_failed`，这类记录，仅可在用户显式恢复时追加辅助诊断审计：当前房间唯一收据须匹配原会话、实际工具计数及已知误拒标签，且不跟随符号链接、有界读取。成功另记 `native_recovery_attestation`，保留旧失败；辅助收据不是权限凭证，也不证明无外部副作用，仍不能绕过进程清理和已发布文件 SHA 审计。

Kimi 的握手、配置、提示发送及上下文压缩失败分别保留白名单诊断；不保存供应商原始错误正文或请求标识。`session/prompt` 已发送时，即使没有工具调用，也不能当作尚未执行而自动重放。仅规划/协调阶段的确切会话压缩 403，且本服务自己记录的原始通信明确证明为订阅上下文限制时，可作为原生恢复候选；历史 `native_auth_unconfirmed` 不整体放行。恢复 POST 需核对原作业上下文、提示摘要、会话、版本、权限与全工作区/进程清理审计，再复用上述新运行、新会话交接。旧会话和失败记录保留，不删除历史、不升级套餐或换计费路径；未知错误、写入副作用、现场漂移或证据变化仍拒绝。

Kimi 仅在本次确切失败 turn 的官方类型明确为 `APIProviderQuotaExhaustedError` 时分类为 `subscription_limit_reached`；HTTP 429 本身不能证明订阅额度耗尽。失败 turn 即便 ACP 返回 end_turn、进程退出码为0，也不当作 completed。未执行工具的额度拒绝沿既有自动退役路径；已发生工具副作用则保留失败事实，经全工作区、身份、归属、版本与清理审计后，走既有 partial 工作的只读继任检查，再允许原授权范围内的剩余工作。自主模式且额度接手开启时，此流程由system在原目标内自动推进，不新增人工继续/确认门；已停止或取消则不自动恢复。接手方复用正常派发的可用性规则：blocked或selectable=false拒绝，unknown但selectable=true不额外拦截，也不冒称额度已确认。首次接手受阻后，仅在绑定或非来源伙伴的真实可派状态变化时重新核对，不以轮询时间制造重复尝试。用户补充在交接期间保留并排队；即便技术检查受阻，也可只保存说明而不派发、不解除写入门，不因单伙伴额度耗尽永久锁住聊天。不得重放失败调用、直接接受旧命令或丢弃未发布改动。历史误归 `native_turn_unconfirmed` 的记录保留；恢复时须另核本服务记录的本次原始通信、作业提示与上述官方额度类型，普通429和不匹配现场不获得恢复权限。

Kimi 的这类额度拒绝也会记到账号层面：派发前记下当前 Kimi 登录的身份（官方用户 id 的摘要，再加本机私有盐做指纹，原值不出解析器、不落盘），拒绝后若刷新得到的仍是同一登录，就在 `quota-gates.json` 写入 `kimi_cli_limit`，所有共用该登录的伙伴在新任务和后续派发里都显示“额度用完”、不可选。Kimi CLI 的用量接口目前只报 5 小时窗口，这个窗口不能说明当月额度，因此不会解除记录；只有出现月度计量且未满，或用户在账号卡片里请求重新检查，才会放开。换成另一个 Kimi 登录不受影响。

Claude 的原生官方限额诊断由同一分类器处理：可信 `usage_limit_reached` 元数据，或 `result` 的 `subtype=success`、`is_error=true` 且全文匹配官方短限额通知，均分类为订阅限额；success 仅指传输封装，不代表调用成功。模型普通正文、工具输出、泛化非零退出均不能据此分类。工作阶段与总结/审核阶段复用伙伴退役及目标调度；所有子任务已验收时，仅换用可用的总结者，不重做任务。不足两位可用伙伴时沿既有 `self_check` 语义如实标明自检，不冒称独立互审。历史误归 `cli_nonzero_exit` 的总结调用，仅在确切 owned transcript、原提示与唯一原生回执、零工具、清理、完整工作区及版本审计通过后，恢复同一 run 的总结状态；旧失败记录保留，最终提交前重新核对现场。正常停止、取消、身份/版本漂移及真实授权边界仍优先。

原生工具活动与 `native_executions` 是真实工具观察；running/heartbeat 只表示活性，不计新增工具或实质进展。命令 exit 0、模型自述或互审都不自动成为注册测试通过。`native_review` 记录审核者收到的确切导出版本及真实工具活动，不能伪造静态模式逐文件全文读取回执。只有实际命令、输出及对应版本可支持执行结论。取消及额度交接必须尊重实际副作用、进程清理与身份；未知不能靠重新派发变成成功。原生默认无 600 秒硬截断，用户明确时限仍约束当前目标。这些协议约束不代表已通过所有供应商版本或长时间连续运行的验收；实际结论应附对应版本与执行证据。

## Snapshot

```json
{
  "room":{"id":"uuid","goal":"目标","status":"running","created_at":0,"updated_at":0,"mode":"text","project_id":null},
  "members":[{"id":"claude","provider":"claude","name":"Claude Code","status":"working","session_id":null,"current_task":"任务摘要","usage":null}],
  "events":[{"seq":1,"kind":"message","sender":"user","recipient":"both","text":"目标","job_id":null,"created_at":0}],
  "jobs":[{"id":"uuid","provider":"claude","kind":"task","status":"queued","text":"任务","parent_id":null}],
  "executions":[],
  "team":null,
  "current_run":null,"runs":[],"conversation_budget":null,
  "budget":{"used":0,"limit":null,"deadline":null},
  "objective":null,
  "capabilities":{"text_only":true,"tools":false,"execution_policy":true}
}
```

成员由 AgentRegistry 登记，内置 Claude Code、Codex 和 Kimi Code（Kimi 被发现后加入）；PartnerManager 可登记最多32个已支持适配器或待配置连接器的独立会话配置，不会默认创建额外配置。配置ID标识会话身份，adapter标识实际CLI，provider标识供应商；多配置不能算作多种工具或独立额度。status.agents 仅提供新房间可用成员的 id/name/avatar/adapter/provider/capabilities/usage_source/usage_fields；每间创建时保存所选成员、名称与接入描述；后续注册或启停不会自动改动它。用户显式邀请只追加新成员，不修改原成员身份、历史 job 或已冻结 run snapshot。`provider` 来自服务选择的 adapter，`session_id` 来自实际 CLI 输出并核对续接身份，不能由模型正文伪造。时间为 Unix 秒。成员状态包含 queued / working / waiting_peer / needs_user / idle / failed / cancelled / interrupted，未知明确显示 unknown。房间状态为 running / completed / ready_for_user / needs_user / failed / cancelled / interrupted / budget_exhausted / unknown。completed只表示旧协议本轮队列结束；ready_for_user只表示共享子任务已交付并满足适用检查、待用户验收；独立审核与self_checked须分别显示，都不表示用户目标已验收；用户接受本轮另由 `current_run.accepted_at` 及 `collaboration.outcome.state:"accepted"` 表示，房间仍可继续下一轮。使用量是供应商提供的最新快照，带来源与范围；无数据为 null，不求和冒充总消耗。

事件使用数据库全局自增 seq，常见 kind：message、member_status、job_created、job_claimed、job_finished、room_status、warning、usage、artifact、team_command、run_started、run_accepted、format_correction_queued/finished、execution_queued/started/finished、native_cache_miss、round_handoff_recorded/invalid、partner_session_rotated。team建立后的新增事件记录当前run_id，旧事件不补写；执行事件中的run_id是执行ID，不能替代execution.team_run_id。team_command只记录受控动作类型与task_id；可见模型message和共享投影用于聊天呈现，不把原始commands结构或用量播进聊天。usage 事件在会话身份确认时带 session_id。native_cache_miss 是 Claude 续接调用的只读诊断：缓存读取为 0、写入不低于阈值，且距同一会话上一次 usage 不到 5 分钟时记录 provider、job_id、run_id、phase、cache_creation_input_tokens、seconds_since_previous_call，不存提示词或响应正文，不影响调度；room_state 以 latest_native_cache_miss 给出房间最近一条；round_handoff_recorded 同理由 latest_round_handoff 给出。每种只保存所需可见字段；不保存供应商隐藏推理、工具参数、凭据或原始诊断。执行程序的输出另存为脱敏、截断的不可信观察，不进入正常 message。重连读取 snapshot 恢复时间线，不重新推理。

## 工作方式与直接阅读

`work_mode`只接受显式`plan`或`build`；非法值和显式null被拒绝。用户入口省略字段的新建旧调用保留`legacy`行为，已有数据库迁移也标为`legacy`；MCP入口里该字段可选，省略时新房间按`build`记录，与界面新建一致、可在界面改模型；两种情况都不把省略的字段写进请求指纹，不重写历史请求指纹；续轮省略则继承当前方式，显式字段纳入幂等指纹。room与每个run保存各自方式，job从可信记录取得，不能由伙伴正文切换。fork继承所引用源run的冻结方式。

`plan`只读项目，讨论结果通过message或任务complete.result交付。宿主在展开编辑或提交文件前拒绝非空files/edits，也拒绝任务写路径、Python工作能力及执行预约；提示词不是唯一门槛。`build`沿用既有隔离修改、owner/CAS、互审和执行类型限制，不开放shell或原目录回写。内部plan_ready仅确认分工，不是用户接受计划。“接受本轮”只记录验收；“按方案开始”显式POST一个build新run，并保留原run。

紧邻上一轮plan后开始build时，宿主从冻结的前轮任务结果构造`source_plan`，双方收到同一run/revision/digest绑定和正文hash。结果正文共享24KiB UTF-8上限，goal另限4KiB；截断和遗漏明确标识，不能自称带入完整历史。snapshot只提供来源摘要，不重复返回正文。单伙伴/旧text没有同房文件续轮能力时，界面明确新建文件任务，只传可见方案摘录而不伪装共享完整会话。

UI新任务默认build；没有关联项目且未显式选择其它成果类型时使用text_artifacts容器，plan阶段也保留这一容器供后续build写入。旧text记录不静默迁移。模式开关只影响新任务或明确的新一轮，进行中的权限不会随控件改变。

交付卡直接提供文件与方案阅读入口。Markdown由本机`markdown.js`通过固定DOM节点呈现，原文始终作为文本；不使用innerHTML，不执行HTML或脚本，不自动加载图片，不跟随相对路径读取宿主文件。仅http(s)链接可由用户点击打开。Markdown排版解析有明确的字符/行/块上限，长文提示排版截断；切换“查看源文”可通过纯文本节点阅读完整已返回正文，不要求下载ZIP。大量文件可分批选择，不因首批列表上限而失去阅读入口。当前文件阅读使用snapshot中的已返回正文；沿用的上一轮版本通过上述只读/file接口取确切历史正文并复核SHA，不调用模型。阅读绑定room/run/path/SHA，迟到或跨目标回包不能覆盖当前阅读器；同目标换阶段可保留冻结历史文档，不相关文件更新保持正在阅读的内容和位置。ZIP保留为可选导出。

## 登记与聊天的生命周期

项目、伙伴配置及房间元数据的`lifecycle_state`为`active|archived|removed`；旧记录按active处理。列表返回三态，界面默认筛选当前。Web管理端可POST `/api/projects/<id>/lifecycle`、`/api/partners/<id>/lifecycle`、`/api/rooms/<id>/lifecycle`，严格正文示例为`{"action":"archive"}`，action只接受`archive`、`restore`、`remove`。动作可重复，状态持久化；archive/remove均保留记录且可restore，不删除源目录、CLI、厂商原生会话或交付。

项目非active时不能捕获新任务；伙伴非active时不能用于新房间，历史房间保留冻结身份及隔离副本，项目和伙伴整理不级联聊天。房间非active时仍可查看、导出和安全取消，但发送、续轮、重试、验收及fork均需先恢复。归档不停止任务；在途worker、捕获、执行及未知清理会拒绝隐藏，存储事务与运行控制锁共同拦截竞态。旧单人正常completed且从未记录清理事件的条目可在静止且无显式失败时整理，这不是补造旧回执或放宽重试门。

账号读取是独立的Web管理操作：POST `/api/partners/<id>/account/refresh`，正文仅`{}`。GET仅返回内存缓存；同一默认CLI账号的配置共享有界刷新与节流，携来源、时间、过期和失败原因。原始账号响应、令牌和凭据不入运行数据库或日志；只保留显示所需的安全字段。MCP拒绝生命周期管理和账号刷新，并剔除账号资料。此功能不授予登录、切账号、消费额度重置或发邮件的权限。

伙伴的登录方式：Claude/Codex/Kimi 原生伙伴默认用本机登录，同一工具的所有伙伴共用。Web 可 POST `/api/partners/<id>/account-source`，正文严格为`{"source":"profile_home"}`或`{"source":"local"}`，把这一位伙伴切到“豆窝独立登录”或改回本机登录。独立登录的目录固定为`<runtime>/partners/homes/<profile_id>/`，由豆窝建立、权限0700；`profiles.json`只记`account_source:"profile_home"`，不存路径，旧档案没有该字段即为本机登录，不需要迁移。改回本机登录时目录及其中的登录保留，之后可再切回。该伙伴在任何房间有任务执行中时拒绝切换，返回`409 partner_account_source_busy`。MCP 列表剔除`account_source`，也不能调用此入口。
伙伴列表的`account_source`为`{kind:"local"|"profile_home",home,variable,login_command}`。`login_command`形如`CLAUDE_CONFIG_DIR='<dir>' '<豆窝实际选用的 claude>' auth login`（Codex 为`CODEX_HOME=… codex login`，Kimi 为`KIMI_CODE_HOME=… kimi login`），路径一律用单引号转义。豆窝只展示这条命令，由用户自己在终端运行；不会替用户执行任何 login 命令，也不读取凭据。
绑定独立目录的伙伴，其所有子进程只额外注入对应的一个变量（`CLAUDE_CONFIG_DIR`、`CODEX_HOME`或`KIMI_CODE_HOME`）：派发与续接、认证检查、账号与额度查询、模型目录查询、房间删除时的会话清理都在内。用户 shell 里自带的这三个变量仍被剥掉，不会透传；没有绑定的伙伴与以前完全一样。独立登录时不做多安装副本的登录择优，直接用已选定的那个二进制，避免一个未登录的目录改掉其他伙伴使用的安装。
账号缓存、刷新节流与`quota_gate`都按账号范围分组：本机登录仍是`claude`/`codex`，独立登录为`<adapter>:<profile_id>`，两者的额度阻塞互不影响；`account.scope`与`availability.scope`对应为`default_cli_login`或`profile_cli_home`。派发前照常刷新账号并按该范围核对已知限额，换登录不会绕过身份核对。
建在独立登录目录里的原生会话，会在新表`native_session_homes`记下所在目录；没有记录的就是本机登录（含全部旧会话），原`native_session_history`表结构不变。读取 Kimi wire、Kimi 受管配置、Claude 会话记录和恢复证据时都到这个目录下找；删除房间时按目录分组，各自在原目录里执行官方清理。已有会话的成员如果此后换了登录方式，派发前就以`partner_account_home_changed`停下、不调用模型，也不会把会话拿到另一个账号下续接；用户可以改回原登录方式继续，或开新任务。删除伙伴档案时保留其独立目录，因为历史房间的会话清理还要用到。

Kimi 账号展示以本机受管登录的官方资料为准。真实邮箱优先；缺邮箱时可展示官方昵称与已规范化的脱敏手机号，明确标注类型。原手机号不进入公开投影，官方账号标识仅用于本次前后身份一致性核对；会员名称直接采用官方字段，不从域名、昵称或手机号推断档位。

`quota_gate.py`负责已知账号限额，独立于伙伴的启停/归档偏好。伙伴返回`availability:{state,selectable,reason,source,observed_at,identity_checked_at,resets_at,stale,scope,can_request_recheck}`；state为available/unknown/blocked。只有新鲜Codex/Claude共享额度窗口或确切原生限额报告可改变门控；认证成功不证明有额度。默认CLI登录的别名共享门控，未知额度保持可选，已知阻塞不得静默移除用户已选成员。原生调用已在途时不冒称已取消。

Web可POST `/api/availability/refresh`空对象批量只读检查，每个账号范围（本机登录按adapter，独立登录按伙伴）一次并复用55秒缓存/并发合并；GET不启动CLI。页面可见时启动/回前台及每60秒请求，不依赖用量浮卡展开。页面上的额度显示靠前台刷新，页面隐藏、关闭及电脑睡眠期间不承诺持续更新；全员额度用完的房间由后端另外每 5 分钟复查一次；create/start_run及每个queued job在claim/扣预算前另查门控，调用明确报告限额后反馈状态。补充消息可先登记并排队，其实际派发仍受门控，不承诺messages请求同步返回409。创建或派发被阻时分别保留`agent_quota_exhausted`或`agent_quota_unconfirmed`原因，不自动换账号或新付费路径。

已知限制在私有0600原子日志中保存本机加盐HMAC身份与安全元数据，不保存邮箱或原始认证响应。重启后无法确认当前身份时保持blocked/account_identity_unknown；确认换账号才解除旧身份绑定。到达已知重置时间仅降为unknown，不直接证明恢复。Claude实际订阅限额且没有可信恢复时间、同账号身份核查新鲜时，Web可POST `/api/partners/<id>/availability/recheck`空对象，需用户明确确认；仅降为unknown/user_recheck_requested，不启动模型、不改登录或启停偏好。旧限额时间observed_at与当前身份核查identity_checked_at分别保留，不能用新登录检查伪装新额度。MCP不能调用这两个管理入口。

Claude额度读取使用本机原生stream-json控制通道，仅发送initialize和`get_usage(skip_behaviors:true)`，不发送user帧、模型提示、工具回执或行为请求。原生工具/MCP关闭、禁会话持久化并有输出/时间/进程组清理边界。账号来自前后`auth status`及必须可比较的initialize身份，发现切换、缺少SDK身份或非firstParty时不绑定额度。仅安全白名单窗口被规范化：共享5小时/7天与模型专属池分别保留label/scope/active/来源和重置时间。原始`is_active`仅指供应商选作单值摘要的窗口，不代表其它窗口停用；共享窗口已用100%时不因该标记为false而漏拦。专属模型或特定surface窗口不用于全账号禁选，额度不是美元账单。缺字段、不支持或查询失败明确显示未知，不退回读取凭据或抓取浏览器。官方状态栏也有额度字段，但本功能不安装或修改全局状态栏。

## 同一聊天的 run、验收与新尝试

旧协议房间的 `team` / `current_run` / `conversation_budget` 为 null，`runs` 为空，原行为不变。共享房间的 `current_run` 包含 `id,number,goal,status,created_at,finished_at,accepted_at,revision,digest,budget,can_accept,can_continue,can_retry`；`runs` 按 number 返回各轮历史摘要与预算，当前摘要另含能力标记。时间为 Unix 秒。文字模式 revision 为 null，digest 仍是 64 位小写十六进制 SHA256；文件模式取当前原始快照的 revision/digest。

`/runs` 和 `/accept` 必须原样提交当前 `current_run.id/revision/digest`。`text` 是完整新目标，非 `goal` 或 `expected_*` 别名。CAS 不匹配返回 `409 team_run_conflict`；严格类型或键集错误拒绝。`request_id` 对动作和完整参数幂等，相同请求只读回现况，参数改变则 `request_conflict`。并发的新目标最多一个成功；迟到点击不能接受或重开另一轮。

只有实际清单交付、适用审核及所需检查满足、无 queued/working 记录及真实在途 worker/执行时，才开放 `can_accept` / `can_continue`；自检不因此变成独立审核。`/accept` 记录 `accepted_at` 与 `run_accepted`，不调用模型，不代表整个聊天结束；旧普通续轮可直接用 `/runs` 提出修改，不要求先接受。objective阶段继续或回答改用专用入口，界面只在目标完成时提供最终确认。Agent 命令不能开下一轮或代用户接受，MCP 不提供验收工具。历史清单与验收不改写，文件仍保留不可变版本；下一轮重新规划，沿用冻结成员、普通原生 session 和既有文件 owner，不直接重跑 accepted 任务。既有共享房间升级绑定初始 run，不清空旧任务、身份或执行凭据。

当前共享任务及持续目标不按总轮数、调用次数或运行时长停止。历史 `used`、`deadline`、有限策略和失败可查，当前活动视图及调度不再执行这些人工预算。`conversation_budget` 只汇总实际派发和运行数量，不表示 Token、费用或当前上限。生命周期、确切版本、实际清理、取消及无进展检查仍生效。

### 停止策略

自主目标停下时，宿主按 `stop_policy.py` 决定是否等用户。只有两类停止码等用户：一是用户自己的停止、用户设的上限、目标本身提出的问题或无进展（`USER_STOPS`）；二是结果未知、需要保留现场的情况，例如清理、提交、会话身份或副作用无法确认（`KEEP_SCENE`）。全员额度用完（`all_partners_quota_exhausted`）走自己的等待路径：最多每 5 分钟复查，有额度恢复的证据才继续，没有任何用量数据时最多每小时试一次。其余停止码，包括以后新增的，都在约 15 秒后由宿主代用户点一次“继续”（`restart-objective` 或 `recover-native`）。宿主按“停止原因 + 任务板状态”算指纹，用户上次发言之后，同一指纹只自动继续一次；同样的停下再次出现且没有新进展，就留给用户。宿主自己因调用失败加上的任务阻塞（不是模型提出的问题）会原地重开。下文各处写的“停止”“等待用户”，在自主目标下都先按本策略处理。同房间的受限 Python 隔离执行另有资源与失败回收约束，不能据此声称原生开发总目标有次数限制。

一次调用在提示词发出之前就失败（适配器报告 `prompt_sent` 为假或 `never_started`，且进程清理已确认、没有工具活动）时，这次调用没有任何模型副作用，不算结果未知：宿主原地短暂退避重试一次；仍失败时该伙伴以 `native_preflight_failed` 退出本目标，未开工的任务交给其他伙伴，其余伙伴继续。只有没有其他可用伙伴或关闭了额度转交时才停下，停止码同样是 `native_preflight_failed`，可以继续，也会按上一段自动继续一次。提示词可能已发出、清理未确认或会话身份不符的失败仍按结果未知处理，不会重放。

协作房间里，原生项目以外的伙伴只回复内容，回复解析通过前什么都不会生效。它的进程在回复前死掉（`cli_nonzero_exit`、`protocol_turn_incomplete`，且没有观察到工具调用）时，这次调用没有任何影响：宿主原地重试一次，其他伙伴不受影响；仍失败时目标停下，未完成任务带入下一轮，由宿主自动继续。原生项目的崩溃走原有的审计恢复。Kimi 在只读权限下的工具授权请求不转成用户问题：原生项目里 Kimi 运行在 macOS `sandbox-exec` 写入沙箱中，只能写它自己的数据目录、临时目录、用户缓存和本次进度文件，工作目录始终不可写，宿主因此同意这一次工具调用，效果与 Codex 的只读沙箱一致；没有沙箱时（文字房间或系统缺少 `sandbox-exec`）宿主拒绝这一次，伙伴继续工作。Full Access 不进沙箱，豆窝也不比工具自己的最高权限更严：Codex 用 danger-full-access 加 approval never，Claude 用 bypassPermissions，Kimi 用它的 Never Ask 模式（Kimi Code 2 的 ACP 模式 `auto`；它的 `yolo` 实测仍会在 `rm -rf` 等命令前询问，与 ACP 里的模式说明相反），Kimi 若仍请求授权，宿主按用户已授权批准一次。Full Access 下伙伴把各自负责的改动同时写进登记的原项目，和用户直接使用这些工具时一样；用户明确限制时照限制办。其他可写权限下 Kimi 仍请求授权时，豆窝在调用进行中无法把问题交给用户，事后回答只会到达一次新调用、Kimi 会再次请求，所以宿主拒绝这一次，伙伴继续工作并在交付中说明哪项没有执行；只有 Kimi 不提供拒绝选项的请求仍停下交给用户。因为三家在只读的本机开发里都能运行不改文件的命令，这类房间的计划可以包含只需运行的任务（capability `python`，`write_paths` 为空）；图像任务、写入路径和 files/edits 仍被拒绝。文字房间只读时仍只接受 `text` 任务。

处于stopped/unknown/interrupted/cancelled终止阶段时，普通继续返回 `team_retry_required`，补充消息不解冻。用户可显式提交 `/runs` 且 `retry:true` 建立新尝试：先停止已跟踪工作并核清理，未确认返回 `team_cleanup_unconfirmed`；成功后使用新的原生 session，旧身份、失败、交付及调用预算保留，不能复用旧 session 伪装新尝试。执行缺 result 时，仅持久记录能证明已 queued 且从未 started 的预约可视为未启动；已启动、重启中断或来源不明且无 cleanup receipt 的记录都阻止新尝试。能力标记是展示提示，写入口仍重新核对全部门槛。

## 共享任务协作（新多成员任务）

`team`是共享任务单源，旧房为null；新房包含run_id、plan_author、phase、revision、stop_reason、成员对确切清单版本的确认，以及tasks。每项任务包含稳定id、title、description、acceptance、dependencies、write_paths、capability、owner、状态、具体求助及result/result_version/result_revision/result_digest/result_files和审核记录。任务状态open → assigned → working → review → accepted；单人自检明确为self_checked、review_kind:self_check，独立审核为review_kind:independent_review。任务亦可waiting_peer、blocked、unknown、interrupted或cancelled；模型单次返回done不改变整个目标的验收状态。

团队响应为`{message,state:"done|working|needs_user",commands:[...]}`，文件模式另带base_revision/files。commands每次最多16项，只允许propose、claim、plan_ready、complete、block、request_help、review。初始plan_author提出完整清单与验收条件，成员各自claim适合自己的任务，不能替另一成员认领；所有成员对同一revision执行plan_ready后才进入工作。最多8个真实子任务；依赖必须指向存在任务、无自依赖或环，依赖任务accepted或self_checked后才排下游，审核方式仍分别保留。不能为用满成员或延长时间制造确认、寒暄或额外任务。

独立互审由任务状态机安排，不必另建重复审核任务。若同一规划版本仍有未认领项，常规协调后可再给每位成员一次明确补救，要求实际认领或说明阻碍；补救签名持久保存，计入原调用与期限。仍无进展时明确记录`team_plan_stalled`及缺少的负责人/确认，不自动替成员认领，也不无限重复唤醒。

上述commands是存储的逻辑形状。原生严格schema按当前作业阶段限制type，采用14字段的固定封闭对象，未使用的字段必须为null；服务只剥离这份确切传输形状，随后仍按逻辑命令的完整键集、权限和任务版本校验。Claude原生调用例外：同一房间各阶段发送字节相同的并集schema，否则每次换阶段续接都会让整段提示词缓存失效；命令类型、can_propose和review/checkpoint_review的reason、checkpoint的summary非空，改由服务按作业真实阶段校验，违反时仍返回invalid_team_commands。自定义适配器可继续返回精简逻辑形状，未知、缺失或非空的多余字段不会被静默丢弃。Claude 并集schema的命令对象多一个传输字段 `handoff`（可为null），不进入逻辑命令；只有 checkpoint 命令携带时才被服务读取，其他命令即使填了也直接忽略，不触发格式纠错。消息不能为空且最多8000字符；propose.id必须是非空稳定标识，claim等task_id须引用同一标识，例如T1；仅未使用字段为null。规划阶段按确切清单版本及尚未认领的任务集合去重，允许分批认领；同一状态无进展不会无限唤醒成员。这里只描述协议，真实模型效果须单独记录。


### 轮次交接与换新会话

Claude 原生会话长期续接会把全部上下文带进每次调用。checkpoint 命令可选带 `handoff`：`goal`（≤1000字）、`done`/`decisions`/`unverified`（各≤12条，每条≤500字）、`key_files`（≤24条，每条≤240字），只写事实和位置。没写不算错误，也不多花一次纠错调用，只是下一轮不换会话；写坏了（超限或形状不对）同样不纠错，只记 `round_handoff_invalid`。收到后服务写 `round_handoff_recorded` 事件，带 run_id、objective_id、proposal_digest、内容和 `context_tokens`，取自本次调用最后一次 API 请求的 `input_tokens+cache_read_input_tokens+cache_creation_input_tokens`（result.usage 是整轮累计，只作拿不到逐次数据时的兜底）。

用户发起的新一轮（新一轮 `/runs`、完成目标后的自然续写 followup-objective、定向后续 directed-objective）开始时，只有同时满足才换会话：native_project 房间；交接人的 adapter 是 Claude；上一目标状态 complete，且这份交接属于刚结束那一轮、其 proposal_digest 等于最终通过的 checkpoint；context_tokens 不小于 200000；交接人身份确定、有当前 session、上一轮没有未知结果 job、清理回执齐全。换法是把该成员 `session_id` 置空并写 `partner_session_rotated`，旧 session 留在 `native_session_history`，删除房间时照常清理；全程不重放旧调用。同一轮内 checkpoint 的 continue、answer、reassess 都不换。只换 checkpoint 作者本人（`available_members` 的第一位）：其他 Claude 成员的会话没有这份用量读数，保持续接。

被换会话的成员下一个 job（无论阶段）在 context 带 `round_handoff`，提示词写明它只是上一轮记录，要按仓库现状核对，不增加权限也不扩大范围；目标、任务板、项目路径等其余上下文与平常相同。同一会话建立之后的 job 不再携带；首个 job 失败没建立会话时会再带一次。事件落在 SQLite，服务重启后交接和待带状态都保留。

服务按成员单在途调用派发，不同成员可并行处理互不冲突的路径。write_paths按大小写折叠检查：无依赖关系的任务不能共用路径；即使有先后依赖，不同负责人也不能认领同一路径。已有文本基线仅首轮在规划确认后按任务范围分配负责人；后续轮不得换掉既有路径负责人。只有该任务的work响应能提交其write_paths内、归自己的文件，plan/coordinate/review/help/user只能返回空files。协调层以owner、逐文件hash、base_revision和不可变快照提交，允许不相交文件的安全合并，冲突不自动覆盖或重试。这里不开放原生编辑器、shell或任意目录。

工作中发现任务拆分缺口时，伙伴须用结构化 `block.cause: coordination_required` 请求调整计划，不能仅在正文要求宿主重规划。该原因只请求正常计划协调，不授予文件、shell或额外业务权限；真实 `permission_required`、`user_decision`、外部依赖与旧未分类阻塞继续保留原等待规则，不能按正文关键词自动降级。自主目标在清理边界回到规划，新增任务仍由计划作者提出、成员本人认领、全员确认确切清单；已有成果、负责人及原写入范围保留。新增依赖、取消/暂停和无进展保护仍须校验。

`request_help{type,task_id,to,capability,text}`绑定当前工作任务和本间另一成员的具体问题，原任务进入waiting_peer；被请求者回复后，服务把message保存为help_result并排回原负责人继续，协助者不能改对方文件。`image`能力对所有当前成员明确不可用；提案或求助需要image时记录capability_unavailable并阻塞等待用户，不虚构图片、不换新API或计费路径。python能力仅文件模式可用，并不授予模型执行权限。

文件夹模式的普通work读取完成后，可在该安全边界先处理本间已等待的具体求助，再恢复原任务；不打断在途调用。格式纠正、容量纠正和执行修复仍绑定其确切任务与来源，不让出该续派位置。原有单成员在途、owner、版本、取消和清理门槛保持。

complete必须有具体result，文件任务还要有其声明路径的实际交付。协调层记录实际result_version、文件revision、digest和路径hash，再安排非作者的成员review。review绑定确切结果版本与实际受审快照；accept后为accepted，changes_requested允许一次有界修订，再次未通过则阻塞。不能拿正文自报版本、较早执行结果或单次job结束代替互审与验收。

文件夹模式在声明输出尚未创建时，消息和求助不会为收集成果metadata而读取不存在的文件；仅对当前确切版本内已修改的任务范围文件绑定hash。complete仍要求全部声明成果实际存在且归原负责人，沿用上述交付和精确版本审核门槛。

`collaboration_view`向snapshot添加`collaboration:{mode,phase,summary,tasks,outcome}`。tasks投影仅含id/title/owner/status/waiting_for/result；outcome含state、reason、next_action、validation、artifacts、remaining。state取in_progress、needs_user、ready_for_user、accepted、stopped、unknown或旧协议round_finished，不产生goal_achieved。所有任务accepted或self_checked且无活跃worker/执行，投影才可显示阶段交付；self_checked不代表独立审核，checkpoint-only轮可由绑定objective的检查结果展示阶段或目标结论。文件hash后来变化会降为需核对。python_sandbox还要求当前revision与snapshot_sha256精确匹配、执行退出码为整数0并确认清理；否则仍需处理。validation区分not_run、static_review、self_check、mixed_review和sandbox_execution，保留真实检查范围；静态互审不证明集成、测试或用户验收。

共享任务单次调用最多600秒，并受本轮剩余期限约束。到限、失败、未知、取消或重启保留具体原因与未完成项，不自动重放或为凑时长等待。非终止态的补充信息可让当前owner重新认领自己的blocked文字/Python任务；不恢复未知调用、不重开已accepted工作或重置预算。

模型上下文的`runtime_budget`来自协调层当前run快照，包含已用/上限、排队/在途数、剩余调用、扣除排队后的可用调用和剩余时间；已用包含本次已认领调用。可用调用不多于8次或剩余时间不多于120秒时，收尾提示要求停止宽泛探索，保留具体帮助、交付与独立审核机会。该提示不提高硬限额、不在聊天播报预算，也不把有证据的部分成果或已说明的证据缺口算作满足未完成验收；静态检查不冒充实际运行。

仅当 provider 已明确 completed、会话身份确证、无工具事件，且在任何 artifact_pending / 文件 apply 之前遇到白名单格式或命令结构错误时，允许一次格式纠正新调用。原 job 保留 failed、error、usage，原 commands/files 整包拒收；新 job 使用同一原生会话，绑定同一 run/task/result_version，并记录 correction_of、format_attempt=1、纠正排队与结果事件。新调用照扣预算和期限，重新经过当前 owner/scope/CAS 验证；第二次非法即停止。provider失败、超时、未知、工具事件、停止/取消、额度不足、owner/scope冲突、文件已提交或 project_commit_unconfirmed 都不走此路径。服务不猜任务ID，不从正文补全命令，不放松语义验证。

可信completed work声明完成、但未提供其全部写入路径时，`team_result_missing`可在同一格式纠正额度内反馈确切缺漏，整包原提案仍不保存。仅在原期限/调用预算足以容纳一次纠正和后续互审时续派；再次缺漏则明确停止，不把部分文件当完整交付，也不自动重跑历史失败。当前轮交付文件以本轮共享任务`result_files`的路径和SHA为准；工作区库存另列，不因文件仍存在就归为本轮成果。

### 长命令进度通道

一条前台命令阻塞时伙伴说不了话，工具参数和输出按设计又不外露，用户只看得到工具跑了多久。这条通道让任何项目的长命令（构建、训练、下载、数据处理、测试都算）自己报一行进度。

每次原生调用启动前，服务在 runtime 下的 `native-progress/` 建一个空文件（目录 0700，文件 0600），把绝对路径放进子进程环境变量 `BEANEST_PROGRESS_FILE`。Claude、Codex、Kimi 走同一套，不按项目或工具名分支。调用结束或取消时删掉文件，文件已经没了也不算错；建文件失败时调用照跑，只是没有这个变量。work/help 阶段的提示词告诉伙伴：命令要跑几分钟以上，就自己把进度接到这个文件，可以用管道或 tee 过滤命令原有的输出，项目不用改。

服务约每 15 秒读一次，只读末尾 4KB，用 O_NOFOLLOW 打开，符号链接和 FIFO 这类非普通文件都当没有内容。取最后一个非空行，去掉控制字符和终端转义序列，脱敏后截到 200 字。内容没变不写，同一 job 两条事件至少隔 30 秒。事件 kind 是 `native_progress_status`，带 `job_id`、`text`、`created_at`、`sender`、`run_id`，`room_state` 在每个 job 上投影 `latest_progress_status`。伙伴卡片只在 job 处于 working 时显示 `进度：{text} · {时间}`，超过 10 分钟没更新就加“（可能已过期）”。

文件内容由命令自己写，不可信，只用来展示。它不算工具活动和实质进展，不影响 no-progress 判定，不刷新卡片上的“有活动”时间，不进聊天时间线，也不进服务拼给模型的 context，更不能当成测试通过或任务完成的证据。

## 旧协议的有界伙伴往返

未带execution_policy、且collaboration省略或为false的房间保留原行为，不迁移历史任务。任意成员都可成为最初接收者。服务交给模型同一目标、自己/伙伴真实状态、必要上下文与当前 job。模型返回 JSON：`{message, actions:[], state:"done|working|needs_user"}`。actions 每轮至多一项，格式 `{type:"request", to:"OTHER_MEMBER_ID", kind:"review|help", text}`；只允许请求本间另一位仍可用的成员。同一目标、每位同时一个在途调用；不再按整个聊天的调用次数或期限停止。静态协议的单次工具通信仍有故障超时，原生开发调用不按目标总时长截断。调用计数是本服务的派发次数，不能等同于CLI内部API请求数、Token或费用。

默认CLI以原生schema约束完整回复，随后仍由服务严格校验；缺失或无效结构化结果不从说明文字中提取JSON、不自动重试。自定义适配器需显式提供结构化hook才使用此路径，见[扩展契约](extensions.md)。当审核/求助答复又向原请求者提出明确新请求时，该显式请求同时承担返回反馈，不再重复派发同版本审核；向第三位请求仍保留原反馈与新请求两路。入队失败只把仍在等待该返回的成员标为需处理，不覆盖正在工作或已排队的状态。

非folder旧协议审核/求助的context额外提供artifact_comparison：根据直接父任务持久化的artifact_pending.base_revision读取不可变旧版本，与实际review revision对照，只附变化文件的完整before_content及前后hash；不从伙伴正文里的版本号猜来源。旧正文按JSON编码后的UTF-8最多40KiB，超限的文件整体省略，before_content_available=false且complete=false；缺少来源明确available=false，不能自称核过遗漏内容。仍只附一份完整受审版本，并沿用整个prompt的128KiB硬限额；历史源读取失败不静默猜测。folder通过按需读取及捕获原稿比较提供证据，见[项目协作](projects.md)。

确证的Claude原生weekly-limit错误fixture可分类为subscription_limit_reached，服务保留该错误并停止，不自动重试或换付费路径；UI在状态/异常处给出处理提示，正常聊天不播报。此轮额度事实来自确切专用会话记录，实时stream-json是否携带同样标记尚未验证，未知错误仍保持通用失败。该分类不提供余额、账单或推算的恢复时间。

伙伴请求被持久化并由事务认领；接收者答复后，服务把反馈自动投递回原请求者（feedback job），此反馈不自动再反弹，只有显式的新 request 才继续。各成员平级，服务单一派发与认领仅用于防重，不把供应商固定为上级。没有新工作不为“保持忙碌”发模型调用。

副作用不明不自动重试。重启将未完成 job 标为 interrupted，保留事件和身份，不能自动重跑。取消只影响本服务登记的进程/room。私有 runtime SQLite 和工作目录不入 Git；服务实例锁避免多个进程同时接管同一 runtime。

## 验收

先用 fake adapter 验证有序来源、幂等、并行认领、双向请求/反馈、身份变化、超时、取消与重启。再检查真实页面和桌面窗口，最后在新沙盒提交简单文本任务，确认两个真实 provider 能自动往返并给出可核对的结果。

## 扩展与本机 MCP

适配器的唯一目录是 AgentRegistry，具体协议与接入验证见 [extensions.md](extensions.md)。UI 从状态描述和房间成员生成卡片与目标，不维护独立供应商名单。费用与用量在成员面板呈现，正常聊天不播报用量记录。

stdio `room_mcp` 仅连接已有 `room_server`，不实例化第二个服务或数据库。私有0600发现文件 `connection.json` 形状为 `{url,token,instance_id}`，其中 token 只包含独立 MCP 凭据，绝不发布 Web token。客户端固定三字段，重启后旧连接不自动换实例；`/api/mcp/` 还校验 `X-LAC-Instance`。MCP 凭据只准该命名空间，Web 凭据也不准冒作 MCP；认证按实际 token 产生 principal，再检查路由一致性。创建、补充、显式续轮的来源固定为 `mcp`，不能自报 `user` 或某个 provider；来源是接入入口标记，不是外部客户端身份认证。这不防御已控制同用户进程或读取本机首页 bootstrap 的程序。

MCP 共14项工具：`room_status`、`room_list`、`room_start`、`room_read`、`room_events`、`room_text`、`room_job`、`room_run`、`room_send`、`room_continue`、`room_export`、`room_cancel`、`project_list`、`partner_list`。`room_events`、`room_text`、`room_job`、`room_run` 只读，分别读取聊天消息原文、当前状态引用的完整原文、单次历史作业和单次历史运行；`project_list`、`partner_list` 目录只读，不执行健康检查；没有登记、伙伴管理或用户验收工具。`room_start` 支持 `participants/mode/work_mode/project_id/collaboration`（后者为严格boolean，默认false）；`room_continue` 接受与HTTP同名的run_id/revision/digest/retry及可选work_mode，供显式新目标，不自动重试未知动作。`room_export` 只读下载当前精确文件版本。start/send/continue 的 request_id 必需且稳定；响应未知只报告 `remote_state_unknown`，不自动重发。MCP连接限本机控制面，不用于公网或不可信多用户。

公共MCP客户端单次响应仍限2MiB，较大的room记录或ZIP需通过桌面/本地Web读取、下载；folder不扩大该限制，也不开放额外原生文件工具。

## 新建 Python 文件草案（不执行）

本节完整文件上下文及容量观察针对旧`ProjectWorkspace`模式，不能套用于默认已有项目folder。folder沿用相同精确替换和授权原则，但按需读文件，使用独立资源上限，见[项目协作](projects.md)。

`mode:"python_artifacts"`只保存本工具新建目录中的文件提案，不接受现有项目路径。旧协议使用一位首写者，不能以 `all` / `both` 同时开写，随后通过请求/反馈队列互审；共享任务协议先确认分工，再由成员按各自write_paths提交与互审。CLI工具仍关闭，不执行文件、测试、shell、依赖安装或网络请求，不能写“测试已通过”。

模型响应在基础三字段外必须有 `base_revision` 和 `files`。每个文件为 `{path,previous_sha256,content}`；新文件的 previous_sha256 为 null，修改须匹配原 hash。只接受顶层 ASCII Python 文件名，最多16个文件、正文合计64KiB；每个路径由实际首提交者负责，共享任务还须符合已确认的write_paths。其他伙伴提出建议或提交自己范围内的新文件，不能覆盖。context只提供一份完整受审版本，以及当前版本号/文件hash/owner；审查旧版本时明确标识，不能把未看过的新内容写成已审。

共享任务项目响应另支持 `edits:[{path,base_sha256,old_text,new_text}]`，原生严格schema要求该字段，未使用时为null或空数组；custom adapter可省略，旧协议形状不变。已有文件的小改动优先使用edits，新文件仍通过files提交；两者不能同时非空，非work阶段不得编辑。每批最多16项，old/new正文合计最多32KiB UTF-8。old_text非空且与new_text不同，必须在job的不可变base_revision原文中精确唯一匹配，包括空白和换行；base_sha256须匹配原文件，不能使用正则、模糊匹配或依次匹配前一项生成的文本。同文件各区间不重叠，宿主按原始位置逆序替换；new_text可空，只清空对应文字，不删除文件。

宿主先将整批edits展开为普通完整文件提案，再复用原有任务范围、owner、身份/取消、完整文件敏感内容扫描、该Python模式的64KiB项目上限和原子CAS提交。任一项无效均不发布该批文件；并行不同文件仍可合并，同路径变化及ABA仍拒绝。`invalid_project_edits/project_edit_conflict/project_edit_ambiguous/project_edit_overlap/project_edit_limit` 不加入格式纠正白名单，不自动重试。精确文件正文、SHA、结果版本、互审、执行和导出继续使用真实完整文件；edits不裁切输入上下文，不增权限、调用上限或超时。

仅非folder旧工作区模式的可信completed、身份确认、无工具work提案，在范围/owner/完整敏感内容检查通过，且工作区明确因合并项目UTF-8字节超限而整批拒收、确认未提交任何文件时，允许同task一次容量修正。宿主核对受影响路径从不可变base到当前的CAS链，反馈当前/拟写总字节、原上限和超额值（包括其他成员已提交的不同文件）；文件数、CAS、权限、未知或提交不确定等失败不触发。`capacity_attempts`、原failed job、用量、拒收proposal及`capacity_correction_of`持久保留，原owner按原scope重写一次后仍须互审；已运行peer不因此取消。须剩余原调用/时间额度并至少可安排修正与审核两次，格式纠正已用标记不重置。第二次超限正常停止，不扩大64KiB，不把容量问题加入FORMAT_ERRORS，不重放原拒收提案；提示中的`capacity_observation`是宿主容量事实，不授权删除必要行为。

非folder旧工作区模式的context另含 `current_project_capacity:{source_bytes,source_byte_limit,file_count,file_limit,unit:"UTF-8 bytes"}`。它来自当前工作区头版本：source_bytes是所有文件正文编码为UTF-8后的实际字节之和，file_count是实际文件数，限额来自该工作区配置；不是字符数、JSON转义后的长度或模型估算。该元数据描述当前提案基线容量，不表示旧审查版本已被重新核对。这里的source_byte_limit是底层工作区限额；任务另有更低容量目标时，仍须单独检查，不能据此将任务上限放宽到64KiB。

非folder共享任务的 `current_project_files` 各项另含 `source_bytes`，`current_project_capacity` 另含 `source_bytes_by_owner:{owner:bytes}`，均由宿主按实际文件正文与负责人测量。两者绑定 `proposal_base_revision` 的当前头版本；审核旧 `result_revision` 时，容量观察仍描述当前提案基线，不混作旧版本容量。它们仅补充提示上下文，不改变文件SHA、公开快照或导出形状。

非folder共享任务提示超过128KiB传输限制时，先将旧轮goal变为240字符的明确节选，保留其轮次、状态与版本证明；仍超限才让任务板的重复当前result引用完整 `task.result`，并仅保留最近两条聊天的240字符节选。`context_compaction` 标明已用压缩，节选带 `truncated/omitted_characters`。当前用户目标、完整当前task及依赖结果、精确受审文件正文/SHA和权限不裁切；这些必需材料仍超限则拒绝 `context_too_large`，不扩大限制、自动重试或增加调用。

`project_workspace.py` 维护私有 manifest、不可变 JSON 快照、owner、CAS 与提案幂等。文件内容始终作为数据保存；manifest 原子切换发布整批文件。历史读取使用严格 open-existing，缺失或损坏时不创建空仓替代。服务先核对 job 仍在执行、未取消 / 超时及会话归属，再同步提交文件与结束 job；文件提交与房间 SQLite 不是同一事务，故障时保留 `project_commit_unconfirmed`，不自动重写。

snapshot 的 `room.mode` 和可选 `project` 表示类型及文件快照。`project` 含 available、revision、files；本模式为 `execution:"disabled"`。消息的 artifact_revision 指出关联文件版本；独立任务记录的 completed 仍只表示本轮答复完毕。MCP room_start 可选相同 mode。文件正文 / 负责人 / 版本在伙伴面板展开，不作为正常聊天。该模式的集成与真实验证须单列，不能由纯存储测试推定通过。

## 新建文本产物、ZIP 与 SVG

`mode:"text_artifacts"` 从空基线建立独立文本工作区，保存文档、HTML/CSS、JSON、SVG等白名单UTF-8文件；不读取已有项目，不运行HTML、脚本或文件。使用独立格式 `beanest-text-artifacts-v1`，`source:null`，diff相对revision0；旧 `project_files` 仍须显式seed，不能用新格式绕过其已选路径边界。最多16文件、每文件16KiB、合计48KiB，路径与敏感内容沿用文本校验。SVG用text能力表示矢量产物，不冒充image/raster模型出图。

导出接口核对当前revision、每文件SHA、路径及限额，返回 `content_type:"application/zip"`；ZIP仅含文件与 `.beanest-export.json` 公共清单，不附聊天、session、源目录或运行记录。本接口不提供历史版本下载；过期revision返回 `project_revision_conflict`，损坏或空产物拒绝导出。base64为响应数据，不写入原项目或自动发布。

SVG预览只接受当前版本的精确 `.svg` 路径并核原文SHA，返回 `content_type:"image/svg+xml"`；预览内容是重新序列化的静态白名单子集，不等于原文字节。最大16KiB；拒绝script、外链/href、foreignObject、style元素或属性、DTD、实体声明、XML处理指令与CSS转义，仅允许静态presentation属性和本地引用。其它文件不预览，不执行用户代码；下载原文仍走ZIP。预览失败不修改已保存原文。

## 已有项目的默认文件夹协作

`mode:"project_folder"`使用`selection_kind:"folder"`的登记。登记只保存metadata；显式创建任务后才在可取消的私有暂存目录捕获源文件，完整捕获后发布baseline，失败不派模型、不以部分文件继续。后续只访问私有SHA blobs与不可变index。`snapshot.project.files`仅含当前版本已修改/新建文本，baseline统计、有限根入口与排除摘要另列，不提供全量正文prompt。

伙伴通过`project_request`返回有界结构化list/read/search；宿主绑定当前job/run、确切`inspected_revision`及剩余预算，保存观察并继续原任务/阶段，不另开放CLI工具。提交仍须原任务write_paths、owner、SHA/CAS/ABA和原子整批校验。审核必须读取交付的确切revision；读前宿主核文件SHA，截断内容不证明全文已审。

读取观察的`source_comparison`只对照私有捕获baseline1与该exact revision，提供前后SHA及有界unified diff；不读取源目录的当前版本。新文件无原稿，缺失/过大/二进制等情况返回明确不可用原因；截断明确标记，不能当作完整差异。该模式ZIP仅导出已修改/新建文件。所有排除、容量、截断和比较细节统一见[项目协作](projects.md)，不沿用下节选定文件或上节Python的总量限制。不支持项目运行、删除/rename或自动回写。

## 已有项目的高级指定文件改稿

`mode:"project_files"`使用已登记的`project_id`；旧协议选一位首写者，共享任务协议按已确认任务的路径分工。该登记路径仅接收用户给定的绝对目录及 1–12 个相对文件名，不递归扫描；每个文件最多16KiB，总计48KiB。路径为有限深度的安全 ASCII 相对路径，正文为 UTF-8 文本，支持白名单的源码、标记及文档类型；拒绝路径穿越、各层符号链接、硬链接、隐藏或凭据文件、二进制及明显敏感内容。私有登记只保存元数据，0600文件及0700目录配合锁与原子替换；损坏或登记文件缺失时停止，不静默清空。

登记时只验证选定文件；每个新房间创建时重新捕获，保存内容 hash 与整组 source_hash。原目录只读，捕获内容作为房间的数据提案，没有目录挂载、原地编辑、shell、依赖安装、项目测试或自动合入。文件正文会进入所选供应商上下文；source.path 元数据不传给房间模型，正文不做通用路径脱敏。

`ProjectWorkspace(kind="text")`在私有不可变版本保存基线，manifest与各快照记录kind，历史按自身类型严格验证。`seed(capture,owner)`建立基线，旧协议基线文件归首写者；共享任务模式先暂归初始清单整理人，规划确认后按任务路径分配负责人。后续沿用file owner / previous_sha256 / base_revision CAS / proposal_id幂等，不能覆盖对方路径。旧协议可新增自己负责的测试草案；共享任务模式严格保持已选择路径，不借任务声明扩展读取或写入范围。本模式不运行这些测试。草案最多16个文件、单文件16KiB、合计48KiB。

snapshot.project 包含 `source:{id,name,path,source_hash,files:[{path,sha256}]}` 与 `changes:[{path,status,previous_sha256,sha256,diff}]`，diff 相对不可变导入基线；`execution:"disabled"`。页面展示来源目录、各文件及 diff；房间模型只收到一份完整审查版本和必要元数据，不重复发送整份 diff。创建时捕获或seed失败，会停止已创建房间并返回 `project_initialization_unconfirmed`；相同request_id读回原房间，不再次捕获或派发。

真实端到端仅使用新建虚构文本计分项目验证：四个真实伙伴配置中选定两位，3次已有订阅调用，完成修改、互审和反馈，保存版本2，原目录内容未变。没有执行项目，不将这次结果推广为全目录、任意工具或其他供应商可用。

## 伙伴监控与接入检查

配置 `health` 是最近一次用户显式检查的观察：包含检查时间、CLI是否可用、版本、认证方式与订阅路径是否确认，状态为 ready / unavailable / unknown。版本及认证状态命令有界，不触发模型、不改登录、不返回凭据；新配置尚未检查时为unknown。检查不是持续在线或费用、剩余额度查询。

`monitor` 仅从本服务SQLite记录读取当前queued/working任务、waiting_peer / needs_user / unknown等需关注状态、最近活动、已登记会话数、完成/失败任务数、最近异常及最新有来源的用量。scope为 `coordinator_owned_only`，不扫描本机其他会话、聊天或进程；这里idle只表示本服务没有相应工作。费用和订阅余额不可用，不填零。新增或轮询配置不会自动启动模型。

## 可选隔离执行

`mode:"python_sandbox"` 沿用上述文件、owner、版本、模型权限和预算约束；只有 `status().sandbox_execution.available` 为 true 时才广告此模式。显式配置要求已运行的本机 Docker、Unix socket 和已缓存的官方 Python 固定 digest。只有独立 CLI 的 `prepare` 会下载镜像；服务不自动 pull、不启动虚拟机、不改全局配置。未准备好时文字和纯草案模式不受影响；新请求的房间元数据若已建立但 runner 不可用，返回 `execution_initialization_unconfirmed`，同 request_id 再读回原房间，不重新派发。

旧协议互审队列收尾或共享任务已互审后，服务先持久reserve，再将确切revision的JSON快照通过stdin交给runner。旧协议每房间每版本最多一次、每间两次；共享协议按team run分别每版本一次、每轮两次，同一服务全局同时一个执行；生成程序每次最多15秒，准备和回收另行有界。容器没有宿主目录/socket/凭据挂载，非root、cap-drop、no-new-privileges、禁网络、只读根文件系统、受限tmpfs，以及CPU/内存/PID/文件/输出限额。可信bootstrap完成初始化，再安装检查架构的不可撤销seccomp过滤器，禁止新进程、exec和网络syscall，最后才导入生成模块；安装失败停止执行。

snapshot.executions 含 `id`（独立执行 ID）、`team_run_id`（所属团队轮，旧协议为null）、revision、snapshot_sha256、status、parent_id、feedback_job_id 与 result。历史执行不删除；UI只将当前team_run_id及精确revision/digest的成功记录用于本轮就绪判定。结果中的 exit_code 和 cleanup_confirmed 是主机观察；stdout / stderr 经脱敏和截断，`report_trust:"untrusted"`，不作为认证测试报告。旧协议执行结束后自动创建 `kind:"execution"` 的模型反馈，context.execution_observation 必须按 feedback_job_id 绑定原记录；共享任务的首个已知非0失败可走下述同轮一次修复；未知或不符合门槛时停止并保留原因，不进入旧反馈链。运行期间产生新版本时，旧结果标为 stale，不归到新版本或触发错配反馈；退出0或程序自报通过都不能证明目标已验收。

共享任务允许一次**由已知执行失败触发的原任务修复**。仅首条已确认退出的 `failed`（兼容 `completed` 但非0）观察，要求严格整数非0退出、`reason:null`、`cleanup_confirmed:true`，精确绑定当前team_run_id/revision/digest，且未取消、未验收、无在途任务、无身份不确定、剩余调用/期限足够，才可进入。主机已核固定文件头；不从stdout里的文件名或指令推断权限。原始执行状态保留，不改成成功。

同一SQLite事务保存run的repair_execution_id、失败时path/SHA和原任务快照，再重开原文件任务及其DAG下游（含文字结论），保留owner、scope、依赖和原审核修订次数；无依赖的文字任务不重开。预算至少预留每项work及独立review。任务与job持久绑定该execution，context中的execution_observation始终为不可信观察；无新模型命令、任务或权限入口。修复后结果版本递增并重新独立互审，所有原文件hash集合必须有实际变化才可预约第二执行；仅revision变更不算。重复receipt/预约幂等，取消或重启不重放。第二次失败、无实际改动、预算不足、版本冲突、任何reason、timeout/输出限/unknown/清理不明均停止并保留原因，不启动第三次执行。

unknown 会停止 queued 工作并取消、等待在途模型；不自动重试或重放执行。取消须核对 runner 为该 run ID 持久记录的 cleanup receipt；只结束 Docker CLI 不等于容器已停。journal 在创建容器前记录身份，清理核对登记的 container ID / owner label；显式 recover 只回收这些记录，不重跑任务，无法确认则保留 unknown。重启将未完成执行标 interrupted，不自动启动 provider 或容器；缺少实际cleanup receipt的已启动执行仍为清理未确认，不能因重启或停止请求改判安全。

当前受控真实边界探针仅覆盖本机 Linux ARM64 容器，不声称 x86 或任意项目执行已验证。新建虚构加法任务已真实完成失败、自动求助修正和第二次运行；程序和测试仍可在同一解释器中伪造输出或提前退出，因此这里没有可信测试认证。验收应同时核对实际源码、固定版本、主机观察与程序输出。

## 内部产物阶段交接与受控试验

`RoomService.create_artifact_phase(goal,target,request_id,participants,*,source_room_id=None,source_revision=None,source_digest=None)` 创建一个新的 `python_sandbox` 房间。常规首阶段无来源；有来源时三字段必须完整，服务核对源房间已稳定、精确版本与digest、相同成员ID和adapter、会话身份无不确定性。复制的是经过验证的不可变Python产物数据，保留每路径owner，不复制运行目录或原生session；新阶段仍有独立会话和原有房间限额。该方法不暴露为HTTP/MCP入口。

`phase_checkpoint(room_id)` 返回 `{settled,snapshot,project_digest}`。只有房间completed、worker与隔离执行task均结束、无queued/working记录时，settled才为true。digest对纯产物快照以 `sort_keys=True, ensure_ascii=True, separators=(',',':')` 的canonical JSON计算SHA256；它绑定具体revision和文件内容，不依据模型正文自报。服务只提供交接机制，阶段计划、总配额与停止策略由显式试验驱动负责。

`workflow_trial` 的plan严格为 `{goal,phases:[{title,goal,target}]}`，最多16阶段；总体goal及每个阶段goal各最多3000字符，title最多160字符，target只能为claude或codex。总体goal供记录展示，每个阶段goal必须是可独立执行的完整说明。驱动固定邀请默认Claude/Codex两位，所有service操作通过同一 `RoomUIServer.runner.call` event loop。HTTP另线程提供观察界面，仅允许GET，拒绝发送、新建、取消和管理POST，避免页面操作绕过试验总配额。

库构造参数 `WorkflowTrial(...,source_byte_limit=65536,initial_source=None)` 可为一次显式试验设置更低产物上限。source_byte_limit必须是1–65536的整数，写入账本caps；阶段推进时重新按全部文件正文的实际UTF-8总字节核对，超出即以 `phase_source_limit_exceeded` 停止，不继续下一阶段。它不放宽底层64KiB硬限额，也不以程序输出或模型自报容量代替检查。

initial_source仅供受控代码为一个**新的显式试验**指定 `{room_id,revision,digest}`。调用者先核实旧试验属于本工具且资源已清理；驱动不会从历史自动寻找来源或推断授权。首个create前只读调用phase_checkpoint，要求通过本节完整机器推进门槛，并与输入的房间、版本、digest精确一致。旧基线作为待修稿可超过本次较低source_byte_limit，但仍须满足原64KiB硬限额；新child及后续阶段的完成产物都必须满足本次较低上限。后续继续采用前一新阶段的精确来源，不恢复旧job或原生session。CLI、HTTP和MCP均不暴露initial_source或自动恢复入口，重开已有账本仍不重放。

全程调度期限最多7200秒、96次模型调用、24次隔离执行；每阶段仍最多8次模型调用、900秒、2次执行。试验单次模型调用默认及上限600秒，并受房间剩余期限约束；旧协议日常默认仍120秒，共享任务单次上限另为600秒。启动下一阶段前重新读取已登记房间的实际used与execution条数，剩余至少8次调用和2次执行才允许开始；不是每次耗尽上限才扣账，也不通过重开新房绕过总限额。推进还需产物可读、两位成员均有实际completed任务，以及当前revision/digest对应的execution为completed、exit_code为整数0、cleanup_confirmed严格为true。以上只是机器推进条件，不是可信测试或需求验收。

CLI仅显式执行时才可能派发模型：`python -m local_agent_coordinator.workflow_trial --runtime ABS --plan PLAN_JSON --runner-root ABS --port 0`。runner须已显式准备，本入口不下载镜像。runtime与runner-root必须是无符号链接的绝对路径；新试验runtime须为空，拒绝已有数据库、工作区或其他内容，plan及runner配置放在外部。私有目录0700，账本和结果文件0600，单实例锁与原子替换保护稳定request_id、阶段room_id及来源revision/digest。create结果未知、阶段失败、资源预算不足或期限到达即停止，不换request_id重试；崩溃后重开只读取旧尝试并标记interrupted，不自动恢复。`--runtime ABS --status`仅读已有账本，不启动服务或模型。

`workflow-trial.json`保存阶段与已观测计数；`final-results.json`保存末次完整checkpoint，并标明phase_index、room_id、observed_at、terminal、active_phase_index、final_for_trial及 `last_observed_before_shutdown` 范围。停止或未知时 `totals_complete=false`，不能把旧阶段快照或已有计数当作清理后的最终实况。退出会关闭HTTP与服务；只有loop关闭无错误且 `shutdown_cleanup_confirmed is True` 才记录清理确认。清理另行有界，未确认则状态unknown；最终总数和产物仍需独立核对本试验记录。

`--seconds`、`--max-calls`、`--max-executions`、`--call-timeout`可显式降低对应上限，并记录进试验账本；source_byte_limit和initial_source仅为库选项，不增加CLI参数。新试验totals只统计它新建的阶段，不重复计入旧基线；同一轮的总调度时间、调用及执行预算仍须扣除所有旧尝试的实际消耗。调用者应传入剩余限额，不能以新目录或显式来源重置本轮7200秒、96次调用、24次执行总预算。

这条路径用于隔离试验，尚未通过1–2小时持续协作验收。它与日常共享任务清单是不同协议，不提供任意项目执行或自动断点续作；其历史结果不能替代新共享任务或产品长期工作流的真实验收。
