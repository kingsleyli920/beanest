# Contributing · 贡献说明

[English](#english) · [简体中文](#简体中文)

## English

Thank you for helping improve Beanest. Start with the [README](README.md), then the [room contract](docs/room-contract.md) or [adapter guide](docs/extensions.md) for the area you are changing. Detailed engineering documentation is currently in Chinese.

Use [issues](https://github.com/kingsleyli920/beanest/issues) for reproducible bugs and focused proposals, and [pull requests](https://github.com/kingsleyli920/beanest/pulls) for reviewable changes. Discuss new provider integrations or changes to task scheduling, permissions, billing, or recovery semantics before implementing a broad change.

### Working locally

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
node --test tests/room_ui.test.cjs
npm --prefix desktop ci
npm --prefix desktop test
```

Choose tests that exercise the boundary you changed. UI tests should cover language switching, dynamic updates, and preservation of user content when relevant. Provider integration tests should use deterministic substitutes by default; clearly distinguish them from live-provider verification. Do not invoke a real agent or paid service from automated tests.

Keep changes readable and scoped. Preserve explicit task identity, file ownership, permission checks, cancellation, and process cleanup. Do not turn unknown results into success or replay uncertain commands. Treat English and Chinese interface catalogs as one feature: update both, and leave user and agent messages untouched.

Explain the problem, resulting behavior, verification, and any remaining limits in your PR. Include sanitized reproduction data only. Never commit local configuration, tokens, connection files, databases, transcripts, private projects, or screenshots containing personal information. Report security issues according to [SECURITY.md](SECURITY.md).

Contributions are provided under the repository's [MIT License](LICENSE). Preserve third-party attribution and license notices; identify the source and license of any new dependency or asset.

## 简体中文

欢迎改进豆窝。先阅读[中文 README](README.zh-CN.md)，再根据改动范围查看[房间契约](docs/room-contract.md)或[适配器说明](docs/extensions.md)。详细工程文档目前主要为中文。

通过 [issue](https://github.com/kingsleyli920/beanest/issues) 提交可复现问题和明确建议，通过 [PR](https://github.com/kingsleyli920/beanest/pulls) 提交便于审阅的改动。新增供应商或改变调度、权限、计费与恢复语义时，先讨论范围。

本机安装和测试命令见上方。按实际改动选择测试；界面改动涉及语言时，检查切换、动态刷新以及用户原文保留。接入测试默认用确定性的供应商替身，明确区分替身验证和真实接入。自动测试不要调用真实模型或付费服务。

保持实现清晰、范围集中，保留任务身份、文件归属、权限、取消和进程清理边界。未知结果不能冒充成功，不重放不确定的命令。中英文目录一起维护，不翻译或改写用户与 Agent 的消息。

PR 说明问题、改后行为、验证方式和剩余限制。复现材料须脱敏，不提交本机配置、凭据、连接文件、数据库、聊天、私有项目或包含个人信息的截图。安全问题按 [SECURITY.md](SECURITY.md) 处理。

贡献按仓库 [MIT 许可证](LICENSE) 提供；引入依赖、代码片段或资产时说明来源和许可，保留所需告示。
