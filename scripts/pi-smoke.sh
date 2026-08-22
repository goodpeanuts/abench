#!/usr/bin/env bash
# pi-smoke.sh — pi 基座 Agent 冒烟测试（headless）
#
# 用法：
#   bash scripts/pi-smoke.sh                  # 等价于 --no-call
#   bash scripts/pi-smoke.sh --no-call        # 只做静态检查，不发 API 请求
#   bash scripts/pi-smoke.sh --source-config --no-call
#                                             # 隔离加载仓库源配置，不读写 ~/.pi
#   bash scripts/pi-smoke.sh --source-config \
#     --provider wodex --model claude-opus-5
#                                             # 真实调用一次（需要对应密钥）
#   bash scripts/pi-smoke.sh --source-config \
#     --provider deepseek --model deepseek-v4-pro
#                                             # DeepSeek 真实调用，自动使用 max
# 环境变量：PI_SMOKE_PROVIDER / PI_SMOKE_MODEL 可代替命令行参数。
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src_models="$repo_root/config/pi/models.json"
src_settings="$repo_root/config/pi/settings.json"
pi_bin="${PI_BIN:-}"
call_timeout_seconds="${PI_SMOKE_TIMEOUT_SECONDS:-300}"
if [[ ! "${call_timeout_seconds}" =~ ^[1-9][0-9]*$ ]]; then
  printf '%s\n' "PI_SMOKE_TIMEOUT_SECONDS 必须为正整数" >&2
  exit 2
fi

provider="${PI_SMOKE_PROVIDER:-}"
model="${PI_SMOKE_MODEL:-}"
mode="no-call"
use_source_config=0
smoke_config_dir=""
out=""
err=""
active_pi_pid=""

cleanup() {
  if [[ -n "${active_pi_pid}" ]] && kill -0 "${active_pi_pid}" 2>/dev/null; then
    kill "${active_pi_pid}" 2>/dev/null || true
    sleep 1
    kill -9 "${active_pi_pid}" 2>/dev/null || true
    wait "${active_pi_pid}" 2>/dev/null || true
  fi
  active_pi_pid=""
  [[ -z "${out}" ]] || rm -f -- "${out}"
  [[ -z "${err}" ]] || rm -f -- "${err}"
  if [[ -n "${smoke_config_dir}" && "$(basename "${smoke_config_dir}")" == abench-pi-smoke.* ]]; then
    rm -rf -- "${smoke_config_dir}"
  fi
}
trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-call) mode="no-call"; shift ;;
    --call) mode="call"; shift ;;
    --source-config) use_source_config=1; shift ;;
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

if [[ "${use_source_config}" -eq 1 ]]; then
  smoke_config_dir="$(mktemp -d "${TMPDIR:-/tmp}/abench-pi-smoke.XXXXXX")"
  cp "${src_models}" "${smoke_config_dir}/models.json"
  cp "${src_settings}" "${smoke_config_dir}/settings.json"
  export PI_CODING_AGENT_DIR="${smoke_config_dir}"
fi
config_dir="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
models_file="${config_dir}/models.json"
settings_file="${config_dir}/settings.json"

# source-config 与已安装模式都从同一个 Pi 配置目录读取 models/settings。
cd "${repo_root}"

fail=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1" >&2; fail=1; }

# ---------- 通用检查 ----------

check_pi_binary() {
  if [[ -z "$pi_bin" ]]; then
    pi_bin="$(command -v pi || true)"
  fi
  if [[ -z "$pi_bin" || ! -x "$pi_bin" ]]; then
    fail "未找到 pi 可执行文件（请先安装 @earendil-works/pi-coding-agent）"
    return 1
  fi
  pass "pi 可执行：${pi_bin}，版本 $("$pi_bin" --version 2>/dev/null)"
}

check_models_config() {
  if [[ ! -f "$models_file" ]]; then
    fail "未找到 ${models_file}；可先运行 pi-setup.sh，或使用 --source-config"
    return 1
  fi
  if ! node -e '
    const fs = require("fs");
    const models = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
    const settings = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
    const expected = {
      wodex: ["claude-opus-5", "max"],
      "wodex-gpt": ["gpt-5.6-sol", "xhigh"],
      moonshot: ["kimi-k3", "max"],
      dashscope: ["qwen3.8-max", "max"],
      deepseek: ["deepseek-v4-pro", "max"],
    };
    const expectedKeys = {
      wodex: "$WODEX_API_KEY",
      "wodex-gpt": "$WODEX_API_KEY",
      moonshot: "$MOONSHOT_API_KEY",
      dashscope: "$DASHSCOPE_API_KEY",
      deepseek: "$DEEPSEEK_API_KEY",
    };
    for (const [provider, [modelId]] of Object.entries(expected)) {
      const providerEntry = models.providers?.[provider];
      const entry = providerEntry?.models?.find((item) => item.id === modelId);
      if (!entry) throw new Error(`缺少 ${provider}/${modelId}`);
      if (entry.reasoning !== true) throw new Error(`${modelId}.reasoning 必须为 true`);
      if (entry.contextWindow !== 1000000) throw new Error(`${modelId}.contextWindow 必须为 1000000`);
      if (providerEntry.apiKey !== expectedKeys[provider]) {
        throw new Error(`${provider}.apiKey 必须只引用 ${expectedKeys[provider]}`);
      }
      if (provider.startsWith("wodex") && providerEntry.headers?.["User-Agent"] !== "3bench-pi/1.0") {
        throw new Error(`${provider} 必须覆盖被网关拦截的 SDK User-Agent`);
      }
    }
    const deepseek = models.providers?.deepseek;
    if (deepseek?.baseUrl !== "https://api.deepseek.com/anthropic") {
      throw new Error("deepseek.baseUrl 必须为官方 Anthropic 兼容端点");
    }
    if (deepseek.api !== "anthropic-messages") {
      throw new Error("deepseek.api 必须为 anthropic-messages");
    }
    if (deepseek.apiKey !== "$DEEPSEEK_API_KEY") {
      throw new Error("deepseek.apiKey 必须只引用 $DEEPSEEK_API_KEY");
    }
    if (settings.defaultThinkingLevel !== "max") {
      throw new Error("defaultThinkingLevel 必须为 max");
    }
    const expectedEnabled = [
      "wodex/claude-opus-5:max",
      "moonshot/kimi-k3:max",
      "dashscope/qwen3.8-max:max",
      "deepseek/deepseek-v4-pro:max",
      "wodex-gpt/gpt-5.6-sol:xhigh",
    ];
    if (JSON.stringify(settings.enabledModels) !== JSON.stringify(expectedEnabled)) {
      throw new Error(`enabledModels 必须为 ${JSON.stringify(expectedEnabled)}`);
    }
    if (settings.compaction?.enabled !== true || settings.compaction?.reserveTokens !== 100000) {
      throw new Error("compaction 必须启用且 reserveTokens 必须为 100000");
    }
  ' "$models_file" "$settings_file" 2>/dev/null; then
    fail "Pi 模型或项目 settings 未固定为最高思考档和 1M 上下文"
    return 1
  fi
  pass "配置策略正确：五模型均为最高思考档，contextWindow=1000000，压缩阈值=900000"
}

# ---------- --no-call：静态检查 ----------

no_call_checks() {
  check_pi_binary || return 0
  check_models_config || return 0

  # 列出模型：用占位密钥让 models.json 中的条目变为可见（只列清单，不发请求）。
  local listing
  if ! listing="$(
    env WODEX_API_KEY=smoke-placeholder \
        MOONSHOT_API_KEY=smoke-placeholder \
        DASHSCOPE_API_KEY=smoke-placeholder \
        DEEPSEEK_API_KEY=smoke-placeholder \
    PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0 \
      "$pi_bin" --list-models --offline 2>/dev/null
  )"; then
    fail "pi --list-models --offline 执行失败"
  else
    local expected missing=""
    for expected in "claude-opus-5" "gpt-5.6-sol" "kimi-k3" "qwen3.8-max" "deepseek-v4-pro"; do
      if ! grep -qF "$expected" <<<"$listing"; then
        missing="$missing $expected"
      fi
    done
    if [[ -n "$missing" ]]; then
      fail "模型清单中缺少:$missing"
    else
      pass "自定义模型均已加载（claude-opus-5 / gpt-5.6-sol / kimi-k3 / qwen3.8-max / deepseek-v4-pro）"
    fi
  fi

  # 密钥存在性（只报变量名，不读值）
  local var
  for var in WODEX_API_KEY MOONSHOT_API_KEY DASHSCOPE_API_KEY DEEPSEEK_API_KEY; do
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
  ' "$models_file" "$provider"
}

resolve_thinking_level() {
  case "$1/$2" in
    wodex/claude-opus-5|moonshot/kimi-k3|dashscope/qwen3.8-max|deepseek/deepseek-v4-pro)
      printf '%s\n' "max"
      ;;
    wodex-gpt/gpt-5.6-sol)
      printf '%s\n' "xhigh"
      ;;
    *)
      printf '%s\n' "provider/model 组合未配置最高推理档：$1/$2" >&2
      return 1
      ;;
  esac
}

call_mode() {
  [[ -n "$provider" && -n "$model" ]] || {
    echo "真实调用需要 --provider 与 --model（或 PI_SMOKE_PROVIDER / PI_SMOKE_MODEL）" >&2
    exit 2
  }
  check_pi_binary || exit 1
  check_models_config || exit 1

  local key_var thinking_level
  key_var="$(resolve_key_var)" || exit 1
  thinking_level="$(resolve_thinking_level "$provider" "$model")" || exit 1
  if [[ -z "${!key_var:-}" ]]; then
    echo "缺少密钥：请先导出 ${key_var}（仅注入当前 shell，不要写入任何文件）" >&2
    exit 1
  fi

  out="$(mktemp)"
  err="$(mktemp)"

  echo "调用：pi -p --approve --no-session --mode json --provider $provider --model $model --thinking $thinking_level"
  local rc=0 pid i
  PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0 \
  "$pi_bin" -p --approve --no-session --mode json \
    --provider "$provider" --model "$model" \
    --thinking "$thinking_level" \
    --system-prompt "You are an API connectivity checker. Reply exactly OK." \
    --no-context-files \
    --no-tools \
    --no-extensions \
    --no-skills \
    --no-prompt-templates \
    --no-themes \
    "只回复：OK" >"$out" 2>"$err" &
  pid=$!
  active_pi_pid="$pid"
  for ((i = 0; i < call_timeout_seconds; i++)); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 1
    kill -9 "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    fail "超过 ${call_timeout_seconds}s 未完成，已终止 pi 进程"
    rc=124
  else
    wait "$pid" || rc=$?
  fi
  active_pi_pid=""

  if [[ "$rc" -ne 0 ]]; then
    fail "pi 退出码 ${rc}（stderr 末尾）："
    tail -n 10 "$err" >&2 || true
    exit 1
  fi
  local verdict
  verdict="$(python3 - "$out" <<'PY'
import json
import sys

saw_agent_end = False
saw_assistant_message = False
saw_malformed_event = False
final_stop = None
final_text = ""
with open(sys.argv[1], encoding="utf-8", errors="replace") as stream:
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            saw_malformed_event = True
            continue
        if event.get("type") == "agent_end":
            saw_agent_end = True
        if event.get("type") != "message_end":
            continue
        message = event.get("message") or {}
        if message.get("role") != "assistant":
            continue
        saw_assistant_message = True
        final_stop = message.get("stopReason")
        content = message.get("content") or []
        if isinstance(content, str):
            final_text = content
        else:
            final_text = "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )

if saw_malformed_event:
    print("malformed_event")
elif not saw_agent_end or not saw_assistant_message or not final_stop:
    print("no_agent_end")
elif final_stop == "error":
    print("error_stop")
elif final_stop not in {"stop", "endTurn"}:
    print("nonterminal_stop")
elif final_text.strip() != "OK":
    print("missing_ok")
else:
    print("ok")
PY
)" || verdict="no_agent_end"
  case "$verdict" in
    ok)
      pass "往返成功：agent_end、非错误停止且回复 OK（provider=${provider} model=${model} thinking=${thinking_level}）"
      ;;
    error_stop)
      fail "最终 assistant 消息 stopReason=error（事件流末尾）："
      tail -n 10 "$out" >&2 || true
      printf '%s\n' "stderr 末尾：" >&2
      tail -n 10 "$err" >&2 || true
      exit 1
      ;;
    missing_ok)
      fail "模型完成调用但最终回复不是精确的 OK（stdout 末尾）："
      tail -n 10 "$out" >&2 || true
      exit 1
      ;;
    malformed_event)
      fail "事件流包含非 JSON 输出（stdout 末尾）："
      tail -n 10 "$out" >&2 || true
      exit 1
      ;;
    nonterminal_stop)
      fail "最终 assistant 消息不是正常终态（stdout 末尾）："
      tail -n 10 "$out" >&2 || true
      exit 1
      ;;
    *)
      fail "退出码 0 但事件流中未找到 agent_end（stdout 末尾）："
      tail -n 10 "$out" >&2 || true
      exit 1
      ;;
  esac
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
