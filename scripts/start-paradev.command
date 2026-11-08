#!/bin/zsh
set -euo pipefail
coordinator_root="$(cd "$(dirname "$0")/.." && pwd)"
paradev_lab="$coordinator_root/../open-source-lab/paradev-lab"
trial_launcher="$paradev_lab/.planning/start-trial.sh"
trial_cli_setup="$paradev_lab/.planning/configure-cross-review-cli.py"
export PATH="/opt/homebrew/bin:$PATH"
if [[ ! -f "$trial_launcher" || ! -f "$trial_cli_setup" ]]; then
  print "找不到本机 Paradev 试验环境，请在当前 Codex 聊天中检查配置。"
  exit 1
fi
if python3 - <<'CHECK'
import socket
try:
    with socket.create_connection(("127.0.0.1", 3005), timeout=1):
        pass
except OSError:
    raise SystemExit(1)
CHECK
then
  print "3005 端口已有服务。本机 Superset (paradev-lab) 若已打开，请直接切换到它；不再启动第二份。"
  exit 0
fi
if ! docker info >/dev/null 2>&1; then
  print "请先手动打开 Docker Desktop，等它启动完成后再双击本文件。"
  exit 1
fi
python3 "$trial_cli_setup"
print "启动本机 Paradev 开发版。首次编译可能需要等一会儿；请保留此终端窗口。"
print "本脚本不派发协作任务。打开新聊天后，由你点击‘交叉审核 / Cross review’里的‘开始’。"
exec "$trial_launcher"
