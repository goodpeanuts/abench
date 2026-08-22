#!/usr/bin/env bash
# pi-setup.sh — 把仓库内的 config/pi/models.json 安装到 ~/.pi/agent/models.json
#（pi 的自定义 provider 目录）。幂等：目标已存在时先备份再覆盖。
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$repo_root/config/pi/models.json"
dest_dir="$HOME/.pi/agent"
dest="$dest_dir/models.json"

[[ -f "$src" ]] || { printf '%s\n' "未找到源文件：$src" >&2; exit 1; }

command -v node >/dev/null 2>&1 || { printf '%s\n' "未找到 node（用于 JSON 校验）" >&2; exit 1; }
node -e 'JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"))' "$src" \
  || { printf '%s\n' "models.json 不是合法 JSON：$src" >&2; exit 1; }

mkdir -p "$dest_dir"
if [[ -f "$dest" ]]; then
  backup="$dest.bak.$(date +%Y%m%dT%H%M%S)"
  cp "$dest" "$backup"
  printf '%s\n' "已备份现有文件到：$backup"
fi
cp "$src" "$dest"
printf '%s\n' "已安装：$dest"
printf '%s\n' "可运行验证：bash scripts/pi-smoke.sh --no-call"
