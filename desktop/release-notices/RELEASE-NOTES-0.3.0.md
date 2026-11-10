# 豆窝 Beanest 0.3.0

[简体中文](#简体中文) · [English](#english)

## 简体中文

豆窝是一个 Mac 应用，让你已经在用的 AI 编程工具（Claude Code、Codex、Kimi Code）一起做同一件事，你在一条对话里看进度。

这是第一个可以直接下载使用的版本。App 自带运行所需的 Python，不用另外安装 Python 或 Node.js。

### 这个版本能做什么

- 说一个目标，选哪些工具参加，在同一条对话里看它们做事。
- 它们会把工作拆成任务、认领、互相求助，也会互相检查对方交付的结果。
- 可以关联一个项目文件夹，只读讨论，或者让它们在这个项目的开发副本里改。
- 文档、代码、图片和视频可以在对话里直接看，也能看到改动有没有提交和推送到 GitHub。
- 某个工具的额度用完了，它手上的活交给还能用的工具；全部用完时，任务等额度恢复再继续。
- 还没装这些工具也可以：打开 App 后，首页可以一键运行各家的官方安装脚本，再用你自己的账号登录。
- 界面有中文和英文。

### 第一次打开

这个版本没有经过 Apple 公证，所以第一次打开要多走几步，之后就不用了。

1. 下载 `Beanest-0.3.0-macos-arm64.zip`，双击解压，得到“豆窝”。
2. 把“豆窝”拖到“应用程序”文件夹。
3. 双击打开。macOS 会提示无法验证这个 App，并且不让打开，点“完成”关掉提示。
4. 打开“系统设置 → 隐私与安全性”，往下翻到“安全性”，找到关于“豆窝”的提示，点“仍要打开”。
5. 按提示输入密码或用触控 ID 确认，再点一次“打开”。

### 支持范围

目前只能在 Apple 芯片（M 系列）的 Mac 上用，Intel Mac、Windows 和 Linux 都用不了。

### 需要你自己准备

至少要有一个 Claude、ChatGPT 或 Kimi 的会员。豆窝不提供账号，也不卖额度，用的是你自己账号的额度，额度和条款以各家为准。

### 这是早期版本

- 适合一个人在自己的 Mac 上用，粗糙的地方还不少。
- 对话和交付记录存在你的 Mac 上，服务只在本机运行，不要把它开放到网络上。这些工具工作时，会把内容发给各自的服务商。
- 开发副本不是沙箱。开启完全访问（Full Access）后，这些工具可以执行命令、联网，也能读到你账号能读到的其他文件。

### 反馈问题

到 https://github.com/kingsleyli920/beanest/issues 提一个 issue，写清楚你做了什么、看到了什么，有截图更好。不要贴账号、密钥这类内容。

## English

Beanest is a Mac app that lets the AI coding tools you already use (Claude Code, Codex, Kimi Code) work on the same goal while you follow along in one conversation.

This is the first version you can download and run directly. The app carries the Python it needs, so you don't have to install Python or Node.js.

### What this version can do

- Give it a goal, choose which tools take part, and watch their work in one conversation.
- They split the work into tasks, claim them, ask each other for help, and check each other's results.
- Link a project folder to discuss it read-only, or let them make changes in a development copy of the project.
- View documents, code, images and videos in the conversation, and see whether changes have been committed and pushed to GitHub.
- When one tool runs out of quota, its work goes to one that still has some. When all of them are out, the task waits and continues once quota is back.
- If you don't have these tools yet, the app's first screen can run each provider's official install script in one click, and you then sign in with your own account.
- The interface is available in English and Chinese.

### Opening it for the first time

This version is not notarized by Apple, so the first launch takes a few extra steps. You only do this once.

1. Download `Beanest-0.3.0-macos-arm64.zip` and double-click it to unzip. You get an app named 豆窝.
2. Drag 豆窝 into your Applications folder.
3. Double-click it. macOS says it can't verify the app and won't open it. Close that message with Done.
4. Open System Settings → Privacy & Security, scroll down to Security, find the message about 豆窝, and click Open Anyway.
5. Confirm with your password or Touch ID, then click Open.

### What it runs on

Macs with Apple silicon (M series) only. Intel Macs, Windows and Linux are not supported at this time.

### What you need

At least one paid plan with Claude, ChatGPT or Kimi. Beanest does not provide accounts or sell usage. It uses your own account's quota, and each provider's limits and terms apply.

### This is an early version

- It is meant for one person on their own Mac, and you should expect rough edges.
- Conversations and deliveries are kept on your Mac and the service runs locally. Do not expose it to a network. While they work, these tools send content to their own providers.
- A development copy is not a sandbox. With Full Access, these tools can run commands, use the network, and read other files your account can read.

### Reporting a problem

Open an issue at https://github.com/kingsleyli920/beanest/issues. Say what you did and what you saw; a screenshot helps. Do not paste account details or keys.
