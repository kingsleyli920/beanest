#!/bin/bash
# Install or update 豆窝 Beanest from its GitHub Release.
#
#   curl -fsSL https://raw.githubusercontent.com/kingsleyli920/beanest/main/scripts/install-macos.sh | bash
#
# Picks the package for this Mac's chip, checks it against the release's
# SHA256SUMS and its code signature, quits a running 豆窝, replaces the app and
# opens it. Conversations and settings live outside the app and are kept.
#
# Optional environment:
#   BEANEST_VERSION       e.g. 0.3.0 (default: the latest release)
#   BEANEST_APPLICATIONS  install folder (default: /Applications, else ~/Applications)
#   BEANEST_DOWNLOAD_BASE where the release files are (default: the GitHub release)
#   BEANEST_ARCH          arm64 or x64 (default: this Mac's chip)
#   BEANEST_NO_OPEN=1     do not open the app afterwards
set -euo pipefail

REPO="kingsleyli920/beanest"
APP_NAME="豆窝.app"

say() { printf '%s\n' "$*"; }
fail() { printf '豆窝安装失败：%s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "只支持 macOS。"
major=$(sw_vers -productVersion | cut -d. -f1)
[ "$major" -ge 13 ] || fail "需要 macOS 13 或更新的系统，这台是 $(sw_vers -productVersion)。"

arch="${BEANEST_ARCH:-}"
if [ -z "$arch" ]; then
  arch=$(uname -m)
  # A terminal running under Rosetta reports x86_64 on an Apple chip.
  if [ "$arch" = "x86_64" ] && [ "$(sysctl -in sysctl.proc_translated)" = "1" ]; then arch="arm64"; fi
fi
case "$arch" in
  arm64) ;;
  x86_64|x64) arch="x64" ;;
  *) fail "不认识这台 Mac 的芯片：$arch" ;;
esac

version="${BEANEST_VERSION:-}"
if [ -z "$version" ]; then
  latest=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest") \
    || fail "连不上 GitHub，检查网络后再试。"
  version="${latest##*/tag/v}"
  case "$version" in
    [0-9]*.[0-9]*.[0-9]*) ;;
    *) fail "找不到已发布的版本。" ;;
  esac
fi
base="${BEANEST_DOWNLOAD_BASE:-https://github.com/$REPO/releases/download/v$version}"
package="Beanest-$version-macos-$arch.zip"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

say "正在下载豆窝 $version（$( [ "$arch" = arm64 ] && echo "Apple 芯片" || echo "Intel" )版）……"
curl -fL --progress-bar -o "$work/$package" "$base/$package" || fail "下载 $package 失败。"
curl -fsSL -o "$work/SHA256SUMS" "$base/SHA256SUMS" || fail "下载校验文件失败。"
expected=$(awk -v name="$package" '$2 == name || $2 == "*" name { print $1 }' "$work/SHA256SUMS")
[ -n "$expected" ] || fail "校验文件里没有 $package。"
actual=$(shasum -a 256 "$work/$package" | awk '{ print $1 }')
[ "$actual" = "$expected" ] || fail "下载的文件和发布的不一致，已停止安装。请重试。"

ditto -x -k "$work/$package" "$work/unpacked"
app="$work/unpacked/$APP_NAME"
[ -d "$app" ] || fail "安装包里没有 $APP_NAME。"
codesign --verify --deep --strict "$app" 2>/dev/null || fail "安装包签名校验没通过，已停止安装。"

target="${BEANEST_APPLICATIONS:-/Applications}"
if [ -z "${BEANEST_APPLICATIONS:-}" ] && [ ! -w "$target" ]; then
  target="$HOME/Applications"
fi
mkdir -p "$target"
dest="$target/$APP_NAME"

if pgrep -f "$dest/Contents/MacOS/" >/dev/null 2>&1; then
  say "正在退出正在运行的豆窝……"
  # By bundle id: a development build may share the display name.
  osascript -e 'tell application id "io.github.kingsleyli920.beanest" to quit' >/dev/null 2>&1 || true
  # The app gives its background service up to 20 seconds to stop.
  for _ in $(seq 1 120); do
    pgrep -f "$dest/Contents/MacOS/" >/dev/null 2>&1 || break
    sleep 0.5
  done
  pgrep -f "$dest/Contents/MacOS/" >/dev/null 2>&1 && fail "豆窝还没退出。如果它弹出了提示，点“好”；然后再运行一次这条命令。"
fi

if [ -e "$dest" ]; then
  mv "$dest" "$work/previous.app"
fi
if ! ditto "$app" "$dest"; then
  rm -rf "$dest"
  [ -e "$work/previous.app" ] && mv "$work/previous.app" "$dest"
  fail "无法写入 $target。"
fi

say "豆窝 $version 已装到 $dest"
if [ "${BEANEST_NO_OPEN:-}" != "1" ]; then
  open "$dest"
fi
