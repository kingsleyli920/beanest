# Security · 安全说明

## Reporting a vulnerability

Please do not disclose exploitable details, credentials, or private runtime records in a public issue. Use [GitHub private vulnerability reporting](https://github.com/kingsleyli920/beanest/security/advisories/new) when available. If that feature is unavailable, open an issue requesting a private contact channel without including the vulnerability details. This early-stage project does not promise a response-time SLA.

Include affected versions, a minimal reproduction using fictitious data, impact, and any relevant execution or permission mode. Do not attach a complete runtime directory, database, connection file, provider transcript, or raw diagnostic bundle.

## Execution boundary

- Beanest is a local, single-user application. Its loopback HTTP service and connection credentials are not a public hosting interface. Do not expose them through port forwarding, a tunnel, or a shared web server.
- Provider tools are installed and authenticated separately. Beanest does not bundle credentials or grant model access.
- Project copies are not operating-system sandboxes. Full Access permits native commands and networking; an agent may access files outside the copy using the permissions of your account.
- Read-only and file-ownership checks serve their documented execution modes. They do not make untrusted native executables safe.
- Cancellation and recovery depend on confirmed identity and cleanup. Unknown outcomes are retained rather than silently replayed or marked successful.
- Runtime data can contain private information even when secrets and provider diagnostics are filtered. Keep `runtime/`, `local-config/`, `.env*`, databases, logs, and connection files private.

The source repository does not include installed dependencies, provider CLIs, or a standalone signed/notarized app. Dependencies and local builds need their own maintenance. See [third-party notices](THIRD_PARTY_NOTICES.md).

## 漏洞报告

请勿在公开 issue 中披露可利用细节、凭据或私有运行记录。可用时优先通过 [GitHub 私密漏洞报告](https://github.com/kingsleyli920/beanest/security/advisories/new) 联系维护者；若未开启该功能，可先提交不含漏洞细节的 issue，请求私密联系方式。本项目仍处早期，不承诺固定响应时限。

请提供受影响版本、使用虚构数据的最小复现、影响及相关执行/权限模式。不要附上完整运行目录、数据库、连接文件、供应商会话或原始诊断包。

豆窝面向本机单用户，loopback 服务及连接凭据不应公开、转发或通过隧道共享。原生 Agent 单独安装和登录。开发副本不是操作系统沙箱，Full Access 可以执行命令、联网及访问当前账号有权限的其他文件；文件归属规则不能让不可信程序变得安全。

取消和恢复仍需确认身份与清理，不确定结果保留记录，不静默重放或标记成功。即使过滤了部分敏感字段，运行数据也可能包含私密内容。请保护运行目录、本机配置、数据库、日志与连接文件。源码不捆绑依赖、Agent CLI 或独立签名安装包，依赖和本机构建仍需独立维护。
