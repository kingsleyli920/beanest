#!/bin/zsh
# User-facing launcher; configuration stays private and project-local.
set -eu
project_dir="${0:A:h:h}"
config_path="$project_dir/local-config/ui-bridge.json"
python_path="$project_dir/.venv/bin/python"
if [[ ! -x "$python_path" || ! -f "$config_path" ]]; then
  print '缺少本机试用配置。请参阅 docs/local-ui.md 完成安装与配置。'
  exit 1
fi
# Reuse this workbench when it is already running; never stop an occupied port.
if "$python_path" - <<'PY'
from urllib.request import urlopen
import sys
try:
    with urlopen('http://127.0.0.1:8768/', timeout=2) as response:
        matching = ('LocalAgentCoordinator' in response.headers.get('Server', '')
                    and b'<title>Local Agent Coordinator' in response.read(65536))
except Exception:
    matching = False
sys.exit(0 if matching else 1)
PY
then
  exec /usr/bin/open 'http://127.0.0.1:8768/'
fi
cd "$project_dir"
exec "$python_path" -m local_agent_coordinator.ui_server --config "$config_path" --port 8768 --open
