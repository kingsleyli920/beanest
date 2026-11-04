#!/bin/zsh
set -euo pipefail
project_dir="${0:A:h:h}"
desktop_dir="$project_dir/desktop"
python_path="$project_dir/.venv/bin/python"
if [[ ! -x "$python_path" ]]; then
  print 'Project Python environment not found. / 找不到项目 Python 环境。'
  print 'Run / 请运行：python3 -m venv .venv'
  print 'Then install the project / 然后安装项目：.venv/bin/python -m pip install -e .'
  exit 1
fi
if ! "$python_path" - <<'PY'
import local_agent_coordinator.room_server
PY
then
  print 'Room service code not found. Run from the project root / 找不到房间服务代码。请在项目根目录运行：.venv/bin/python -m pip install -e .'
  exit 1
fi
if [[ ! -d "$desktop_dir/node_modules/electron" || ! -d "$desktop_dir/node_modules/@electron/packager" ]]; then
  print 'First launch will install the locked Electron runtime. / 首次启动将安装锁定版本的 Electron 桌面运行时。'
  (cd "$desktop_dir" && npm ci)
fi
cd "$project_dir"
app_arch="$(node -p 'process.arch')"
app_path="$project_dir/runtime/desktop-build/app/豆窝-darwin-$app_arch/豆窝.app"
app_archive="$app_path/Contents/Resources/app.asar"
build_stamp="$project_dir/runtime/desktop-build/built-stamp"
needs_build=false
if [[ ! -f "$app_archive" || ! -f "$build_stamp" ]]; then
  needs_build=true
fi
for source_file in "$desktop_dir/main.js" "$desktop_dir/lifecycle.js" "$desktop_dir/menu.js" "$desktop_dir/locale.js" "$desktop_dir/project-picker.js" "$desktop_dir/power-guard.js" "$desktop_dir/external-pages.js" "$desktop_dir/preload.js" "$desktop_dir/build-local.js" "$desktop_dir/assets/beanest.icns" "$desktop_dir/package.json" "$desktop_dir/package-lock.json" "$project_dir/LICENSE" "$project_dir/THIRD_PARTY_NOTICES.md"; do
  if [[ "$source_file" -nt "$build_stamp" ]]; then
    needs_build=true
  fi
done
if [[ "$needs_build" == true ]]; then
  npm --prefix "$desktop_dir" run build:local
fi
exec "$app_path/Contents/MacOS/豆窝"
