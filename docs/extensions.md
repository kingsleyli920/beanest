# 扩展本地 Agent

成员、头像、能力与用量字段由 `AgentRegistry` 提供。服务、HTTP、MCP 和 UI 都从这里读取成员。内置 Claude Code、Codex 和 Kimi Code 三个原生适配器（Kimi 被发现后加入）；DeepSeek、Cursor 等尚未接入。

## 自动发现与手动添加

应用打开时刷新已支持工具的安装情况；复用已有服务的窗口也可请求 `POST /api/partners/discover`，正文为 `{}`。发现检查 Claude Code、Codex 和 Kimi Code 的 PATH 和固定安装位置；Kimi 已安装时会启动一次不发提示词的握手确认登录状态。不读取聊天、项目或凭据，不调用模型。GET 继续返回最近一次观察，不因页面轮询重复检测。已手动停用的工具保持停用，不创建重复身份，也不把新工具塞进旧房间。

安装、接入检查和当前任务是三件事：找到可执行文件不代表已登录；接入检查通过不代表正在工作；旧用量不代表剩余额度。“检查连接”是显式、有界的无推理检查，结果保留检查时间；认证、费用或余额无法核实时显示未知。

手动添加有三种用途：

- **已有工具的独立会话**：选择 `claude` / `codex`，增加名称和形象。它们复用本机已验证的原生适配器和同一登录，不是新增供应商、账号或额度。
- **先保存待配置工具**：`deepseek` 等模板允许先保存（以前保存的 Kimi JSON 连接器配置仍按原协议保留）。没有兼容连接器时显示“待配置”，不参与任务，也不会自行安装软件或调用 API。
- **接入兼容本机 CLI**：选择 `local_json`，或给上述模板配置实现 `beanest-json-v1` 的本机连接器。只有可执行文件存在、显式接入检查通过且没有停用，才进入新任务的可选名单。

DeepSeek 等名字只是模板，**不表示其原厂 CLI 已经适配**。把任意厂商命令填进去不会自动转换协议。原生适配器仍是未来优先的扩展方式；通用连接器为已有兼容工具和自行维护的本机封装提供一个明确入口。

`PartnerManager` 私有保存最多32个配置，每个新房间明确选择1–8位。新工具先登记身份和接入，再选择参与者。形象 `bean_style` 只作装饰，可选 `sprout`、`peach`、`sky`、`lavender`、`sunshine`、`cocoa`，不代表能力或权限。

启停只改变新房间的名单，已有房间保持成员与接入身份；停止已有工作使用房间取消。MCP 的 `partner_list` 只读监控，不提供新增、配置、启停、发现或接入检查。通用连接器的本机执行路径和参数不应转发给 MCP 客户端。

## 兼容 CLI 连接器

这是一个**显式信任本机可执行程序**的高级入口，不是插件下载器或操作系统沙箱。豆窝使用 `create_subprocess_exec` 传入字面 argv，不拼 shell 命令、不展开变量或占位符，不接受环境变量配置；直接 shell 启动器及指向它们的符号链接会被拒绝。它仍不能阻止一个被你信任的程序在内部做其他事。只连接自己维护或了解来源的程序，连接器须关闭自己的工具、文件写入与外部委派功能。

进程只继承操作系统必需环境，以及你自己的代理和 CA 证书设置（如 `HTTPS_PROXY`、`SSL_CERT_FILE`），不传递 API key 或提供方路由。不要把密钥写入参数。`billing` 只接受 `local`（本地运行）或 `existing_subscription`（现有订阅）；没有新增付费 API 模式，也不会帮你购买或注册服务。通用连接器的账务字段是**配置和连接器的声明**，豆窝没有据此核实厂商订阅或账单，`subscription_confirmed`、费用和余额保持未知；不要把“连接可用”理解为免费或订阅已获确认。Claude / Codex / Kimi 的原生订阅检查单独处理。

先保存待配置模板：

```json
{
  "id": "my_helper",
  "name": "我的本机伙伴",
  "adapter": "local_json",
  "bean_style": "sky"
}
```

上面是 `POST /api/partners` 正文。随后可用 `POST /api/partners/my_helper/connection` 首次配置；也可在新建时直接附上 `connection` 字段。以下路径和参数均为虚构示例：

```json
{
  "connection": {
    "protocol": "beanest-json-v1",
    "executable": "/opt/example/bin/beanest-helper",
    "args": ["--json"],
    "billing": "local"
  }
}
```

`executable` 必须为绝对路径，`args` 最多16项且每项最多512字符，不接受控制字符；不执行任何字符串插值。保存配置不会启动程序。一个待配置模板可首次填入连接器；配置好的执行路径、参数和账务声明不能原地替换，以免旧房间悄悄接到另一个程序。改变接入时新增一个身份；重复保存同一配置是幂等操作，且不会恢复停用设置。

### 无推理握手

点击“检查连接”只运行一次已配置的可执行文件和参数，向 stdin 写一个 JSON 对象并关闭 stdin。连接器须将 stdout 全部用于一个 JSON 对象，不输出 banner 或进度文字；stderr 不进入协议或持久健康结果。检查最长10秒，单个检查响应最多64KiB。请求没有 prompt，**连接器必须在此分支只检查自身状态，不调用模型**：

```json
{"protocol":"beanest-json-v1","kind":"check"}
```

成功响应必须具有以下全部字段，不允许额外字段：

```json
{
  "protocol": "beanest-json-v1",
  "kind": "check",
  "ready": true,
  "version": "1.2.3",
  "billing": "local",
  "capabilities": {"text_only": true, "tools": false}
}
```

`billing` 必须与保存的配置一致；`ready` 必须是布尔值，`version` 是受限版本号。未就绪时返回 `ready:false`。声明工具可用、协议不符、退出非零、超时或非完整 JSON 都不能通过；只找到可执行文件也不能通过。每次正式调用前再做同一握手，防止沿用已经失效的连接状态。

### 单次任务与显式续接

正式调用另起一个受豆窝管理的子进程，stdin 只有下面这个 JSON 对象。`prompt` 包含服务生成的当前上下文，`response_schema` 为该次响应 schema 或 `null`；首次 `session_id` 为 `null`，之后必须显式续接同一个标识。连接器不能使用“最近一次会话”猜测用户身份。

```json
{
  "protocol": "beanest-json-v1",
  "kind": "invoke",
  "prompt": "服务提供的当前任务和上下文",
  "session_id": null,
  "response_schema": null,
  "capabilities": {"text_only": true, "tools": false}
}
```

stdout 必须只返回一个封闭对象：

```json
{
  "protocol": "beanest-json-v1",
  "kind": "result",
  "status": "completed",
  "session_id": "example-session:1",
  "text": "{\"message\":\"文字答复\",\"state\":\"done\",\"actions\":[]}",
  "usage": {"input_tokens": 120, "output_tokens": 30},
  "tools_used": []
}
```

`text` 是字符串，内容须符合该次 prompt / schema 的[房间响应契约](room-contract.md)；上例只是旧单人文字任务。共享任务使用 commands 等相应字段，不能把上述 actions 固定套给所有任务。格式有效不代表内容正确，豆窝仍检查成员、命令、文件负责人及版本。

响应字段必须齐全且没有额外字段；`tools_used` 必须为空列表，续接标识必须与请求一致。session 标识最多128字符，只接受字母、数字、点、下划线、冒号和连字符，首字符须为字母或数字。`usage` 无法提供时为 `null`；只保留非负整数的 `input_tokens`、`output_tokens`、`cached_input_tokens`、`cache_read_input_tokens`、`cache_creation_input_tokens`，不把其它数值变成金额或额度。

提示和返回文字各最多128KiB，stdout / stderr 合计最多2MiB。调用受房间剩余时间和单次最多600秒限制；握手消耗也计入该次时间。取消、超时或输出过量会清理所拥有的整个进程组，结果未知不自动重试。请勿在连接器中脱离进程组启动常驻工作。

## 适配器契约

新增一个可信的 Python 适配器，再注册 `AgentSpec`。这不是从聊天或配置下载并执行任意插件的机制。

```python
from local_agent_coordinator.agent_registry import AgentRegistry, AgentSpec

async def invoke_helper(prompt, cwd, *, session_id=None, timeout=120):
    # 实现并验证原生接口、认证、计费、显式续接及子进程清理后再注册。
    raise NotImplementedError("Adapter integration is not implemented")

helper = AgentSpec(
    id="helper", name="Local Helper", avatar="H", invoke=invoke_helper,
    usage_source="helper_cli", usage_fields=("input_tokens", "output_tokens"),
)
```

`invoke` 返回普通字典：`session_id`、`text`、`status`、`usage`、`events`。服务选择适配器并核对实际 session，消息正文不能指定身份。适配器负责关闭禁止的工具、过滤凭据和隐藏推理、在取消或超时时清理所拥有的进程；服务再执行期限、次数、幂等和房间成员检查。

适配器也可显式提供 `AgentSpec.invoke_structured`：签名在 `invoke` 基础上接收 `response_schema` 关键字参数，返回同样的结果字典。服务只向该hook发送当前模式与成员对应的schema；自定义适配器未声明时继续使用原来的 `invoke`，不猜测参数或因TypeError重试。`default_registry(custom_invoke)`不会启用内置结构化hook。

内置 CLI 使用原生格式约束：Codex 的 `--output-schema`、Claude 的 `--json-schema`，Kimi 也接收同一份 `response_schema`。Claude只读取唯一成功结果的 `structured_output`，不回退说明文字；其原生 `StructuredOutput` 仅视为格式事件，其他工具仍拒绝。Codex仅解析最后一条完整的非commentary回复，不拼接片段。临时schema在私有目录保存并清理，Unicode按实际UTF-8字节计上限；服务仍严格检查完整响应、身份、文件负责人和版本。参见[Codex官方命令说明](https://learn.chatgpt.com/docs/developer-commands#codex-exec)及[Claude官方结构化输出说明](https://code.claude.com/docs/en/headless#get-structured-output)。原生格式约束不增加执行权限，也不保证代码或审核结论正确。

- `text` 是一个协作响应 JSON，契约见 [room-contract.md](room-contract.md)。仅一个完整 JSON 代码框可被兼容解析，正文外夹带说明仍拒绝。
- `session_id` 必须是供应商实际返回的显式续接标识。默认 Claude/Codex 用 UUID 校验；其他适配器可提供 `normalize_session_id`，但最终仍受长度和字符边界限制，身份改变不会被静默接纳。
- `usage_source` 与 `usage_fields` 声明供应商字段来源。不可取得返回 `None`；数值不能被当作金额、余额或跨会话累计值。单位与缓存口径应在适配器说明中明确。
- 当前仅接受 `text_only=True, tools=False`。新增执行工具需要单独实现和验证权限、工作区与资源冲突约束，注册名称不会获得这些权限。

## 共享任务协议与能力边界

UI新建并选择2–8位成员时使用`collaboration:true`，HTTP/MCP调用者也可显式选择；省略或false保持旧actions协议，单工具房间不使用共享协议。新协议不是新的供应商适配器：仍调用各成员的原生CLI专用会话；文字和文件模式下工具关闭（`native_project` 另见[项目协作](projects.md)），完整响应改为`{message,state,commands}`，文件模式另带base_revision/files。结构化hook收到对应schema，自定义invoke仍由服务严格校验，不能把任意工具事件当作协作命令。

`team_store.py`是共享清单和状态单源：初始接收者提出最多8项真实交付，各成员对确切清单revision确认并各自claim；依赖必须构成DAG。每成员最多一个在途调用，不同成员可在不同路径并行；同路径不能跨负责人覆盖，文件提交仍由协调层检查owner、hash及版本。request_help绑定具体task、接收者、能力和问题，回答作为help_result返回原负责人；complete记录具体result及其实际结果版本/文件hash，再安排非作者review，不能凭模型正文自称完成。

所有子任务accepted只是ready_for_user，表示交付并互审、等待用户验收。`collaboration_view.py`基于记录生成tasks和outcome，保留具体原因、下一步、未完成项、文件版本与检查范围；只有当前版本精确匹配的隔离进程退出0且清理确认，才展示sandbox_execution，仍不等于目标验收。旧room保留team:null与round_finished语义，不补造共享分工或全局验收。

团队一次最多提出 8 项任务，单次调用最多 600 秒；不按总调用次数或总时长停止。适配器必须遵守实际传入timeout及取消清理，不因任务未完成而无界重试或绕过额度。失败、未知、取消及重启不会自动重放；自主目标下可恢复的停下按[停止策略](room-contract.md#停止策略)自动继续一次。

当前可用能力只有文字，以及文件模式中的选定文本隔离稿/新建Python文件提案。image对所有当前成员明确不可用；需要image的提案或求助记录capability_unavailable（自主目标下按停止策略自动重开一次，仍不可用则等待用户），不把工具名称、装饰形象或注册能力声明当作实际图片接入，不自动新增API或改变计费路径。文字和文件模式下，内置原生适配器关闭shell、源目录编辑和其它外部工具；通用连接器的无工具约束还取决于受信任程序正确实现协议，不是操作系统沙箱。python_sandbox的运行由协调层把固定快照交给已准备的受限runner，不授予模型工具权限。新增能力需另行实现真实边界与验证，不能由共享任务协议推导为任意项目自由开发。

在应用启动时把完整 registry 传给 `RoomService(..., registry=registry)`。内置目录在 `agent_registry.default_registry()`；若开发者决定加入新适配器，只在目录注册入口组合，UI、存储和 MCP 不应出现它的专用条件分支。自动安装发现仅支持显式可信resolver，新增原生供应商还须实现并验证自己的接入和定位路径。不会自动运行发现的未知命令；通用连接器只有经用户显式配置后才能执行握手，通过后才可派发。显式注入registry/manager不会隐式扫描本机，便于受控集成和测试。

## 成员与升级

每个房间在创建时固定用户选定的成员 ID、名称与接入描述。显式 `participants` 为 1–8 个不重复、已启用的 ID，接收者必须属于该子集；省略时兼容现有调用者的已启用目录；目录超过8位时拒绝并要求显式选择，UI总是发送显式选择。新注册成员只可选入新房间，历史房间不会突然多出参与者。伙伴配置停用不改变已有房间，移除或更换适配器不能将历史工作静默换成另一身份。`all` 代表本间全体，`both` 仅为旧版本兼容别名。

扩展验证须覆盖第三成员的创建、直接派发、求助/审核/反馈、并发认领、重复请求、取消、恢复和用量来源。现有第三成员测试使用模拟适配器，只证明扩展契约；不能当作新供应商已接通。真实集成前逐项核实可执行文件或服务、原生会话、用户自己的认证、实际计费、工具权限和取消路径，不自动引入新的付费 API。
