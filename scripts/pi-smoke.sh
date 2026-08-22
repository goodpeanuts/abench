#!/usr/bin/env bash
# pi-smoke.sh — pi 基座 Agent 冒烟测试（headless）
#
# 用法：
#   bash scripts/pi-smoke.sh                  # 等价于 --no-call
#   bash scripts/pi-smoke.sh --no-call        # 只做静态检查，不发 API 请求
#   bash scripts/pi-smoke.sh --provider wodex --model claude-opus-5
#                                             # 真实调用一次（需要对应密钥）
# 环境变量：PI_SMOKE_PROVIDER / PI_SMOKE_MODEL 可代替命令行参数。
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src_models="$repo_root/config/pi/models.json"
installed_models="$HOME/.pi/agent/models.json"
call_timeout_seconds=120

provider="${PI_SMOKE_PROVIDER:-}"
model="${PI_SMOKE_MODEL:-}"
mode="no-call"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-call) mode="no-call"; shift ;;
    --call) mode="call"; shift ;;
    --provider) [[ $# -ge 2 ]] || { echo "--provider 需要一个值" >&2; exit 2; }; provider="$2"; shift 2 ;;
    --model) [[ $# -ge 2 ]] || { echo "--model 需要一个值" >&2; exit 2; }; model="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

# 指定了 provider+model（或 --call）则进入真实调用模式
if [[ "$mode" == "no-call" && -n "$provider" && -n "$model" ]]; then
  mode="call"
fi

fail=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1" >&2; fail=1; }

# ---------- 通用检查 ----------

check_pi_binary() {
  if ! command -v pi >/dev/null 2>&1; then
    fail "未找到 pi 可执行文件（请先安装 @earendil-works/pi-coding-agent）"
    return 1
  fi
  pass "pi 可执行：$(command -v pi)，版本 $(pi --version 2>/dev/null)"
}

check_installed_models() {
  if [[ ! -f "$installed_models" ]]; then
    fail "未找到 ${installed_models}，请先运行 bash scripts/pi-setup.sh"
    return 1
  fi
  if ! node -e 'JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"))' "$installed_models" 2>/dev/null; then
    fail "$installed_models 不是合法 JSON"
    return 1
  fi
  pass "provider 目录已安装且 JSON 合法：$installed_models"
}

# ---------- --no-call：静态检查 ----------

no_call_checks() {
  check_pi_binary || return 0
  check_installed_models || return 0

  # 列出模型：用占位密钥让 models.json 中的条目变为可见（只列清单，不发请求）。
  local listing
  if ! listing="$(
    env WODEX_API_KEY=smoke-placeholder \
        MOONSHOT_API_KEY=smoke-placeholder \
        DASHSCOPE_API_KEY=smoke-placeholder \
    pi --list-models --offline 2>/dev/null
  )"; then
    fail "pi --list-models --offline 执行失败"
  else
    local expected missing=""
    for expected in "claude-opus-5" "gpt-5-sol" "kimi-k3" "qwen3.8-max"; do
      if ! grep -qF "$expected" <<<"$listing"; then
        missing="$missing $expected"
      fi
    done
    if [[ -n "$missing" ]]; then
      fail "模型清单中缺少:$missing"
    else
      pass "自定义模型均已加载（claude-opus-5 / gpt-5-sol / kimi-k3 / qwen3.8-max）"
    fi
  fi

  # 密钥存在性（只报变量名，不读值）
  local var
  for var in WODEX_API_KEY MOONSHOT_API_KEY DASHSCOPE_API_KEY; do
    if [[ -n "${!var:-}" ]]; then
      pass "$var 已设置"
    else
      printf 'INFO  %s 未设置（真实调用前需要导出该变量）\n' "$var"
    fi
  done
}

# ---------- 真实调用模式 ----------

resolve_key_var() {
  node -e '
    const fs = require("fs");
    const cfg = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
    const p = (cfg.providers || {})[process.argv[2]];
    if (!p) { console.error("未知 provider：" + process.argv[2]); process.exit(1); }
    const m = String(p.apiKey || "").match(/^\$([A-Za-z_][A-Za-z0-9_]*)$/);
    if (!m) { console.error("provider " + process.argv[2] + " 的 apiKey 不是 $ENV_VAR 形式"); process.exit(1); }
    console.log(m[1]);
  ' "$installed_models" "$provider"
}

call_mode() {
  [[ -n "$provider" && -n "$model" ]] || {
    echo "真实调用需要 --provider 与 --model（或 PI_SMOKE_PROVIDER / PI_SMOKE_MODEL）" >&2
    exit 2
  }
  check_pi_binary || exit 1
  check_installed_models || exit 1

  local key_var
  key_var="$(resolve_key_var)" || exit 1
  if [[ -z "${!key_var:-}" ]]; then
    echo "缺少密钥：请先导出 ${key_var}（仅注入当前 shell，不要写入任何文件）" >&2
    exit 1
  fi

  local out err
  out="$(mktemp)"
  err="$(mktemp)"
  trap 'rm -f "${out:-}" "${err:-}"' EXIT

  echo "调用：pi -p --approve --no-session --mode json --provider $provider --model $model"
  local rc=0 pid i
  pi -p --approve --no-session --mode json \
    --provider "$provider" --model "$model" \
    "只回复：OK" >"$out" 2>"$err" &
  pid=$!
  for ((i = 0; i < call_timeout_seconds; i++)); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 1
    kill -9 "$pid" 2>/dev/null || true
    fail "超过 ${call_timeout_seconds}s 未完成，已终止 pi 进程"
    rc=124
  else
    wait "$pid" || rc=$?
  fi

  if [[ "$rc" -ne 0 ]]; then
    fail "pi 退出码 ${rc}（stderr 末尾）："
    tail -n 10 "$err" >&2 || true
    exit 1
  fi
  if grep -q '"type":"agent_end"' "$out" || grep -q '"type": *"agent_end"' "$out"; then
    pass "往返成功：退出码 0 且事件流包含 agent_end（provider=${provider} model=${model}）"
  else
    fail "退出码 0 但事件流中未找到 agent_end（stdout 末尾）："
    tail -n 10 "$out" >&2 || true
    exit 1
  fi
}

if [[ "$mode" == "call" ]]; then
  call_mode
else
  no_call_checks
fi

if [[ "$fail" -ne 0 ]]; then
  echo "冒烟测试未通过" >&2
  exit 1
fi
echo "冒烟测试通过"
