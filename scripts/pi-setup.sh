#!/usr/bin/env bash
# pi-setup.sh — 把仓库内的 models/settings 安装到 ~/.pi/agent
#（pi 的自定义 provider 与默认策略目录）。models 备份后覆盖；settings 备份后
# 只合并本仓库拥有的最高思考档、模型清单与 compaction 字段。
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src_models="$repo_root/config/pi/models.json"
src_settings="$repo_root/config/pi/settings.json"
dest_dir="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
dest_models="$dest_dir/models.json"
dest_settings="$dest_dir/settings.json"

[[ -f "$src_models" ]] || { printf '%s\n' "未找到源文件：$src_models" >&2; exit 1; }
[[ -f "$src_settings" ]] || { printf '%s\n' "未找到源文件：$src_settings" >&2; exit 1; }

command -v node >/dev/null 2>&1 || { printf '%s\n' "未找到 node（用于 JSON 校验）" >&2; exit 1; }
node -e 'for (const p of process.argv.slice(1)) JSON.parse(require("fs").readFileSync(p, "utf8"))' \
  "$src_models" "$src_settings" \
  || { printf '%s\n' "Pi 配置不是合法 JSON" >&2; exit 1; }

mkdir -p "$dest_dir"
if [[ -f "$dest_settings" ]] && ! node -e \
  'JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"))' "$dest_settings"
then
  printf '%s\n' "现有 settings.json 无法解析，未修改任何目标文件：$dest_settings" >&2
  exit 1
fi

models_tmp="$(mktemp "$dest_dir/.models.json.tmp.XXXXXX")"
settings_tmp="$(mktemp "$dest_dir/.settings.json.tmp.XXXXXX")"
cleanup_tmp() {
  [[ -z "${models_tmp:-}" ]] || rm -f -- "$models_tmp"
  [[ -z "${settings_tmp:-}" ]] || rm -f -- "$settings_tmp"
}
trap cleanup_tmp EXIT

cp "$src_models" "$models_tmp"
if [[ -f "$dest_settings" ]]; then
  node -e '
    const fs = require("fs");
    const current = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
    const desired = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
    for (const key of ["defaultThinkingLevel", "enabledModels", "compaction"]) {
      current[key] = desired[key];
    }
    fs.writeFileSync(process.argv[3], JSON.stringify(current, null, 2) + "\n");
  ' "$dest_settings" "$src_settings" "$settings_tmp"
else
  cp "$src_settings" "$settings_tmp"
fi

stamp="$(date +%Y%m%dT%H%M%S).$$"
if [[ -f "$dest_models" ]]; then
  backup="$dest_models.bak.$stamp"
  cp "$dest_models" "$backup"
  printf '%s\n' "已备份现有文件到：$backup"
fi
if [[ -f "$dest_settings" ]]; then
  backup="$dest_settings.bak.$stamp"
  cp "$dest_settings" "$backup"
  printf '%s\n' "已备份现有文件到：$backup"
fi

mv -f "$models_tmp" "$dest_models"
models_tmp=""
mv -f "$settings_tmp" "$dest_settings"
settings_tmp=""
trap - EXIT
printf '%s\n' "已安装：$dest_models"
printf '%s\n' "已安装并合并策略：$dest_settings"
printf '%s\n' "可运行验证：bash scripts/pi-smoke.sh --no-call"
