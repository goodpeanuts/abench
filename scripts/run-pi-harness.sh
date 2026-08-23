#!/usr/bin/env bash
# run-pi-harness.sh — 宿主机 pi harness + 容器桥接（abench）
#
# 架构：pi 在宿主机运行（有 LLM API 网络、使用本次运行隔离的 provider
# 目录），但模型**没有任何文件/shell/网络工具**：经 scripts/pi-emsctl-tool.mjs
# 注册的唯一工具 emsctl，把每次调用以固定 argv（不经 shell）桥接到
# `podman compose … exec -T main emsctl <子命令>`。容器保持隔离；宿主机上的
# 隐藏 scenario / 评测代码对模型不可达（硬封锁，非提示约束）。
# 被评分的动作日志来自 simulator sidecar（/tmp/ems-state/actions.jsonl，评分
# 权威），评分在宿主机跑 tests/harbor_verify.sh。
#
# 用法：
#   bash scripts/run-pi-harness.sh --provider dashscope --model qwen3.8-max
#   常用选项：--thinking max|--task <dir>|--bootstrap-only|
#             --skip-smoke|--keep
#   非最高思考档可显式加 --allow-any-thinking；该标记只放宽档位校验，仍会评分。
# 环境变量：PI_HARNESS_PROVIDER/MODEL/THINKING/SMOKE_TIMEOUT_SEC/
# TASK/PROJECT、PI_BIN。源配置只保存密钥变量名，harness 不主动序列化密钥值。
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src_models="$repo_root/config/pi/models.json"
src_settings="$repo_root/config/pi/settings.json"

provider="${PI_HARNESS_PROVIDER:-}"
model="${PI_HARNESS_MODEL:-}"
thinking="${PI_HARNESS_THINKING:-}"
agent_smoke_timeout_sec="${PI_HARNESS_SMOKE_TIMEOUT_SEC:-300}"
task_rel="${PI_HARNESS_TASK:-tasks/sffd-rolling-surge-01}"
project="${PI_HARNESS_PROJECT:-sffd-pi}"
pi_bin="${PI_BIN:-}"
bootstrap_only=0
skip_smoke=0
keep=0
allow_any_thinking=0
pi_ext="$repo_root/scripts/pi-emsctl-tool.mjs"
runtime_pi_dir=""
runtime_models=""
active_pi_pid=""
diagnostic=0
run_kind="evaluation"
COMPOSE=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --provider) [[ $# -ge 2 ]] || { echo "--provider 需要一个值" >&2; exit 2; }; provider="$2"; shift 2 ;;
    --model) [[ $# -ge 2 ]] || { echo "--model 需要一个值" >&2; exit 2; }; model="$2"; shift 2 ;;
    --thinking) [[ $# -ge 2 ]] || { echo "--thinking 需要一个值" >&2; exit 2; }; thinking="$2"; shift 2 ;;
    --task) [[ $# -ge 2 ]] || { echo "--task 需要一个值" >&2; exit 2; }; task_rel="$2"; shift 2 ;;
    --project) [[ $# -ge 2 ]] || { echo "--project 需要一个值" >&2; exit 2; }; project="$2"; shift 2 ;;
    --bootstrap-only) bootstrap_only=1; shift ;;
    --skip-smoke) skip_smoke=1; shift ;;
    --keep) keep=1; shift ;;
    --allow-any-thinking) allow_any_thinking=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

[[ -n "$provider" && -n "$model" ]] || {
  echo "必须指定 --provider 与 --model（或 PI_HARNESS_PROVIDER / PI_HARNESS_MODEL）" >&2
  exit 2
}

# 仓库约束（AGENTS.md「测试运行约定」）：所有评测运行必须使用模型相应的
# 最高思考档；默认拒绝降档。--allow-any-thinking 只放宽思考档校验，运行仍会评分。
resolve_max_thinking() {
  case "$1/$2" in
    wodex/claude-opus-5|moonshot/kimi-k3|dashscope/qwen3.8-max|deepseek/deepseek-v4-pro)
      printf '%s\n' "max" ;;
    wodex-gpt/gpt-5.6-sol)
      printf '%s\n' "xhigh" ;;
    *)
      printf '%s\n' "provider/model 组合未配置最高思考档：$1/$2" >&2
      return 1
      ;;
  esac
}
max_thinking="$(resolve_max_thinking "$provider" "$model")" || exit 2
if [[ -z "$thinking" ]]; then
  thinking="$max_thinking"
elif [[ "$thinking" != "$max_thinking" && "$allow_any_thinking" -ne 1 ]]; then
  echo "仓库约束：${provider}/${model} 的评测必须使用最高思考档 ${max_thinking}（收到 ${thinking}），不接受降档；诊断扫描请显式加 --allow-any-thinking" >&2
  exit 2
fi
case "$thinking" in
  off|minimal|low|medium|high|xhigh|max) ;;
  *) echo "推理档 ${thinking} 不在 pi 支持的档位内（off|minimal|low|medium|high|xhigh|max）" >&2; exit 2 ;;
esac
if [[ "$allow_any_thinking" -eq 1 ]]; then
  diagnostic=1
  run_kind="diagnostic"
fi

[[ "$agent_smoke_timeout_sec" =~ ^[1-9][0-9]*$ ]] || {
  echo "PI_HARNESS_SMOKE_TIMEOUT_SEC 必须是正整数秒" >&2
  exit 2
}

task_dir="$repo_root/$task_rel"
log() { printf '[pi-harness] %s\n' "$*"; }
if [[ "$diagnostic" -eq 1 ]]; then
  log "诊断模式：thinking=${thinking}，仍运行 verifier 并生成分数"
fi

cleanup() {
  if [[ -n "${active_pi_pid}" ]] && kill -0 "${active_pi_pid}" 2>/dev/null; then
    kill "${active_pi_pid}" 2>/dev/null || true
    for _ in $(seq 1 5); do
      kill -0 "${active_pi_pid}" 2>/dev/null || break
      sleep 1
    done
    kill -9 "${active_pi_pid}" 2>/dev/null || true
    wait "${active_pi_pid}" 2>/dev/null || true
  fi
  active_pi_pid=""
  if [[ "${keep:-0}" != "1" && "${#COMPOSE[@]}" -gt 0 ]]; then
    podman "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  if [[ -n "${runtime_pi_dir}" && -d "${runtime_pi_dir}" \
        && "$(basename "${runtime_pi_dir}")" == abench-pi-harness.* ]]; then
    rm -rf -- "${runtime_pi_dir}"
  fi
}
trap cleanup EXIT

classify_event_file() {
  python3 - "$1" "${2:-}" <<'PY'
import json
import sys

expected_text = sys.argv[2]
saw_agent_end = False
saw_assistant_message = False
saw_malformed_event = False
final_stop = None
final_text = ""
try:
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
except OSError:
    print("no_agent_end")
else:
    if saw_malformed_event:
        print("malformed_event")
    elif not saw_agent_end:
        print("no_agent_end")
    elif not saw_assistant_message or not final_stop:
        print("incomplete_assistant")
    elif final_stop == "error":
        print("error_stop")
    elif final_stop not in {"stop", "endTurn"}:
        print("nonterminal_stop")
    elif expected_text and final_text.strip() != expected_text:
        print("unexpected_text")
    else:
        print("ok")
PY
}

# ---------- 1. 预检 ----------

if [[ -z "$pi_bin" ]]; then
  pi_bin="$(command -v pi || true)"
fi
[[ -n "$pi_bin" && -x "$pi_bin" ]] || { log "未找到 pi CLI（可设 PI_BIN）"; exit 127; }
command -v node >/dev/null 2>&1 || { log "未找到 node"; exit 127; }
command -v python3 >/dev/null 2>&1 || { log "未找到 python3"; exit 127; }
command -v podman >/dev/null 2>&1 || { log "未找到 podman（本 harness 依赖 podman compose）"; exit 127; }
pi_version="$("$pi_bin" --version 2>/dev/null || echo unknown)"

[[ -f "$task_dir/instruction.md" && -f "$task_dir/scenario.json" \
   && -f "$task_dir/tests/harbor_verify.sh" ]] || { log "任务目录不完整：${task_dir}"; exit 2; }
compose_main="$task_dir/environment/docker-compose.yaml"
compose_local="$task_dir/environment/docker-compose.local.yaml"
[[ -f "$compose_main" && -f "$compose_local" ]] || { log "缺少 compose 文件"; exit 2; }
[[ -f "$pi_ext" ]] || { log "缺少扩展文件：${pi_ext}"; exit 2; }
[[ -f "$src_models" && -f "$src_settings" ]] || { log "缺少 Pi 源配置"; exit 2; }

runtime_pi_dir="$(mktemp -d "${TMPDIR:-/tmp}/abench-pi-harness.XXXXXX")"
cp "$src_models" "$runtime_pi_dir/models.json"
cp "$src_settings" "$runtime_pi_dir/settings.json"
runtime_models="$runtime_pi_dir/models.json"
export PI_CODING_AGENT_DIR="$runtime_pi_dir"
log "Pi 配置隔离：五模型 1M、最高思考档、900K 压缩策略"

log "配置预检：检查本次运行的隔离 models/settings"
bash "$repo_root/scripts/pi-smoke.sh" --no-call

# 密钥变量名从本次运行的隔离目录解析（只查存在性，不读值）
key_var="$(node -e '
  const fs = require("fs");
  const cfg = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
  const p = (cfg.providers || {})[process.argv[2]];
  if (!p) { console.error("未知 provider"); process.exit(1); }
  const m = String(p.apiKey || "").match(/^\$([A-Za-z_][A-Za-z0-9_]*)$/);
  if (!m) { console.error("apiKey 不是 $ENV_VAR 形式"); process.exit(1); }
  console.log(m[1]);
' "$runtime_models" "$provider")" || { log "无法从 ${runtime_models} 解析 ${provider} 的密钥变量"; exit 1; }
if [[ -z "${!key_var:-}" ]]; then
  log "缺少密钥：请先导出 ${key_var}（只注入当前 shell，不要写入任何文件）"
  exit 1
fi

# podman machine：默认停止，需要时启动并等待 socket 就绪
machine_state="$(podman machine inspect --format '{{.State}}' 2>/dev/null || true)"
if [[ "$machine_state" != "running" ]]; then
  log "podman machine 未运行（当前：${machine_state:-unknown}），正在启动……"
  podman machine start >&2
fi
machine_ready=0
for _ in $(seq 1 60); do
  if podman info >/dev/null 2>&1; then machine_ready=1; break; fi
  sleep 2
done
[[ "$machine_ready" = "1" ]] || { log "podman socket 60×2s 内未就绪"; exit 1; }
podman compose version >/dev/null 2>&1 || { log "podman compose 不可用"; exit 1; }

COMPOSE=(compose -f "$compose_main" -f "$compose_local" -p "$project")

# ---------- 2. run dir ----------

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
run_dir="$repo_root/results/pi-runs/${stamp}-${provider}-${model}-${thinking}-${run_kind}"
workspace="$run_dir/workspace"
state_dir="$run_dir/state"
verifier_dir="$run_dir/verifier"
mkdir -p "$workspace" "$state_dir" "$verifier_dir"
cp "$repo_root/AGENTS.md" "$workspace/AGENTS.md"
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
log "run 目录：$run_dir"

# ---------- 3. 清场并拉起容器 ----------

# simulator 以 load_existing=True 启动，残留的 simulator-state 卷会恢复旧回合，
# 因此每次运行前必须 down -v。
log "清理旧 compose 项目（down -v）"
podman "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true

log "拉起容器（up -d --wait，simulator healthcheck 等待 socket）"
if ! podman "${COMPOSE[@]}" up -d --wait >/dev/null; then
  log "--wait 不可用或超时，退回 up -d + 轮询"
  podman "${COMPOSE[@]}" up -d >/dev/null
  ready=0
  for _ in $(seq 1 120); do
    if podman "${COMPOSE[@]}" exec -T main python3 -c \
      'import os,sys; sys.exit(0 if os.path.exists("/tmp/ems/emsctl.sock") else 1)' >/dev/null 2>&1; then
      ready=1; break
    fi
    sleep 2
  done
  [[ "$ready" = "1" ]] || { log "120×2s 内 socket 未就绪"; exit 1; }
fi

# ---------- 4. 桥接冒烟 ----------

# 扩展经此环境变量拿到 compose 桥接参数（逗号分隔；本仓库路径不含逗号）
export PI_EMSCTL_BRIDGE_ARGS="-f,${compose_main},-f,${compose_local},-p,${project}"
observe_out="$(podman "${COMPOSE[@]}" exec -T main emsctl observe)" || {
  log "桥接 observe 失败（退出码 $?）"; exit 1
}
observe_ok="$(python3 -c '
import json, sys
try:
    payload = json.loads(sys.argv[1])
except json.JSONDecodeError:
    print("false"); raise SystemExit(0)
print("true" if payload.get("ok") is True else "false")
' "$observe_out")"
[[ "$observe_ok" = "true" ]] || { log "桥接 observe 未返回 ok:true：${observe_out:0:200}"; exit 1; }
log "桥接冒烟通过：emsctl observe -> ok:true"

if [[ "$bootstrap_only" = "1" ]]; then
  log "--bootstrap-only：验证到此为止，正在清理"
  exit 0
fi

# ---------- 4.5 agent 烟测（扩展加载 + 单工具注册） ----------

if [[ "$skip_smoke" -ne 1 ]]; then
  log "agent 烟测：一次最小调用验证扩展与唯一工具 emsctl 可用"
  agent_smoke_events="$runtime_pi_dir/agent-smoke-events.jsonl"
  agent_smoke_stderr="$runtime_pi_dir/agent-smoke-stderr.log"
  (
    cd "$workspace"
    export PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0
    exec "$pi_bin" -p "只回复：OK" --approve --no-session --mode json \
      --provider "$provider" --model "$model" --thinking "$thinking" \
      --system-prompt "You are an API connectivity checker. Reply exactly OK." \
      --no-context-files \
      --tools emsctl --extension "$pi_ext" \
      --no-extensions --no-skills --no-prompt-templates --no-themes
  ) >"$agent_smoke_events" 2>"$agent_smoke_stderr" &
  active_pi_pid=$!
  agent_smoke_rc=0
  agent_smoke_timed_out=0
  for ((elapsed = 0; elapsed < agent_smoke_timeout_sec; elapsed++)); do
    kill -0 "$active_pi_pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$active_pi_pid" 2>/dev/null; then
    agent_smoke_timed_out=1
    kill "$active_pi_pid" 2>/dev/null || true
    sleep 1
    kill -9 "$active_pi_pid" 2>/dev/null || true
  fi
  wait "$active_pi_pid" || agent_smoke_rc=$?
  active_pi_pid=""
  if [[ "$agent_smoke_timed_out" -eq 1 ]]; then
    log "agent 烟测超过 ${agent_smoke_timeout_sec}s，已终止"
    exit 124
  fi
  if [[ "$agent_smoke_rc" -ne 0 ]]; then
    log "agent 烟测失败：扩展加载或工具注册异常"
    tail -n 10 "$agent_smoke_stderr" >&2 || true
    exit 1
  fi
  agent_smoke_verdict="$(classify_event_file "$agent_smoke_events" "OK")" || agent_smoke_verdict="no_agent_end"
  if [[ "$agent_smoke_verdict" != "ok" ]]; then
    log "agent 烟测事件流异常：${agent_smoke_verdict}"
    tail -n 10 "$agent_smoke_events" >&2 || true
    exit 1
  fi
fi

# ---------- 5. 组装 prompt ----------

prompt_file="$run_dir/prompt.md"
{
  cat "$task_dir/instruction.md"
  cat <<PROMPT

## 工具与交互方式（重要）

你只有一个工具：emsctl。它就是任务环境唯一的环境表面——每次调用在隔离容
器内恰好执行一个 emsctl 子命令，并把输出的 JSON 对象原样返回。参数以字符
串数组给出：

- 观察当前状态：args = ["observe"]
- 派遣单位：args = ["dispatch","--incident","I002","--unit","AM207","--state-version","2","--action-id","d-101"]
- 推进到下一个事件：args = ["advance","--next-event"]
- 收尾：args = ["finish","--reason","no remaining work"]

规则：

- 工具报错表示结构化拒绝（状态版本过期 / action_id 重复 / 未知对象 /
  单位忙 / 能力不匹配）——读取错误中的 JSON 说明，改用最新的
  state_version 与全新的唯一 action_id 重试；完全相同的 action_id 重试是
  幂等的。
- 先 observe，再按上面的任务契约行动。完成只在 advance 处理服务完成事件
  后产生——持续推进到 done=true；仅当没有未服务的可派事故且没有在途/
  在场单位时才 finish（finish 后所有写命令与 advance 都会被拒绝，在途
  单位永不完成）。
- 你没有文件读写、shell 或网络工具；请直接在对话中推理并记住状态。
- 本次 harness 不设置主评测的墙钟截止时间；持续推进到 done=true 后再结束，
  不要为了假想的本地时间预算提前 finish。
PROMPT
} > "$prompt_file"
cp "$prompt_file" "$workspace/BRIDGE.md"

# ---------- 6. 启动 pi（等待自然退出） ----------

events_file="$run_dir/pi-events.jsonl"
stderr_file="$run_dir/pi-stderr.log"
log "启动 pi（单工具硬封锁、无主评测超时）：provider=${provider} model=${model} thinking=${thinking}"

(
  cd "$workspace"
  export PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0
  exec "$pi_bin" -p "$(<"$prompt_file")" \
    --approve --no-session --mode json \
    --provider "$provider" --model "$model" \
    --thinking "$thinking" \
    --tools emsctl --extension "$pi_ext" \
    --no-extensions --no-skills --no-prompt-templates --no-themes
) > "$events_file" 2> "$stderr_file" &
pi_pid=$!
active_pi_pid="$pi_pid"

pi_rc=0
wait "$pi_pid" || pi_rc=$?
active_pi_pid=""
log "pi 结束：退出码 ${pi_rc}（主评测无墙钟超时）"

# ---------- 7. 事件流判定 ----------
# pi 的 CLI 即使最后一轮以 API 错误收场也可能以 0 退出；判定失败仍先完成
# 状态收集、评分和 manifest，最后以非零退出码报告。

if [[ "$pi_rc" -ne 0 ]]; then
  verdict="pi_exit_error"
else
  # Keep event classification in an explicit branch.  This makes a classifier
  # failure observable without allowing shell parsing of any diagnostic text.
  if ! verdict="$(classify_event_file "$events_file")"; then
    verdict="no_agent_end"
  fi
fi

# ---------- 8. 收集 sidecar 状态 ----------

log "收集 simulator 状态（/tmp/ems-state）"
if ! podman "${COMPOSE[@]}" cp simulator:/tmp/ems-state/. "$state_dir/" >/dev/null 2>&1; then
  log "compose cp 失败，退回 tar 通道"
  podman "${COMPOSE[@]}" exec -T simulator tar cf - -C /tmp/ems-state . | tar -xf - -C "$state_dir"
fi
if [[ ! -s "$state_dir/actions.jsonl" ]]; then
  log "警告：${state_dir}/actions.jsonl 缺失或为空"
fi

# ---------- 9. 宿主机评分 ----------
# 三个 HARBOR_*_PATH 必须显式覆盖：脚本默认写 /logs/verifier/*，宿主机下
# mkdir 会失败。harbor_verify.sh 恒以 0 退出，分数在 reward.json。

reward_summary='null'
log "运行 verifier（harbor_verify.sh）"
HARBOR_STATE_DIR="$state_dir" \
HARBOR_SCENARIO="$task_dir/scenario.json" \
HARBOR_REWARD_PATH="$verifier_dir/reward.json" \
HARBOR_DETAILS_PATH="$verifier_dir/details.json" \
HARBOR_REPLAY_PATH="$verifier_dir/replay.json" \
PYTHON_BIN="${PYTHON_BIN:-python3}" \
  bash "$task_dir/tests/harbor_verify.sh" >> "$run_dir/harness.log" 2>&1 || \
  log "verifier 非零退出（详见 harness.log），继续写 manifest"
if [[ -f "$verifier_dir/reward.json" ]]; then
  reward_summary="$(python3 - "$verifier_dir/reward.json" "$verifier_dir/details.json" <<'PY'
import json, sys
try:
    reward = json.load(open(sys.argv[1]))
except (OSError, json.JSONDecodeError):
    print("null"); raise SystemExit(0)
try:
    details = json.load(open(sys.argv[2]))
except (OSError, json.JSONDecodeError):
    details = {}
print(json.dumps({
    "reward": reward,
    "action_count": details.get("action_count"),
    "replay_ok": (details.get("replay") or {}).get("ok"),
}))
PY
)"
  python3 - "$verifier_dir/reward.json" "$verifier_dir/details.json" <<'PY' || true
import json, sys
reward = json.load(open(sys.argv[1]))
details = json.load(open(sys.argv[2]))
print("[pi-harness] overall={overall} strict_pass={strict} integrity={integrity} "
      "action_count={count}".format(
          overall=reward.get("overall"), strict=reward.get("strict_pass"),
          integrity=reward.get("integrity"), count=details.get("action_count")))
PY
else
  log "警告：verifier 未产出 reward.json"
fi

# ---------- 10. run-manifest ----------

finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 - "$run_dir" "$task_rel" "$task_dir" "$provider" "$model" "$thinking" "emsctl" \
  "$pi_version" "$project" "$started_at" "$finished_at" \
  "$pi_rc" "$verdict" "$diagnostic" "$reward_summary" <<'PY'
import json, pathlib, sys

(run_dir, task, task_dir, provider, model, thinking, tools, pi_version, project,
 started, finished, pi_rc, verdict, diagnostic, reward_summary) = sys.argv[1:16]
root = pathlib.Path(run_dir)
task_dir = pathlib.Path(task_dir)
reward = None
action_count = None
if reward_summary != "null":
    try:
        payload = json.loads(reward_summary)
        reward = payload.get("reward")
        action_count = payload.get("action_count")
    except json.JSONDecodeError:
        pass


def _load(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _task_version():
    try:
        import tomllib
        data = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
        return (data.get("task") or {}).get("version")
    except Exception:
        return None


def _usage_from_events(events_path):
    # pi 在 turn_end.message.usage 上携带该回合 API 调用的用量——每回合恰
    # 好一条，是唯一不重复的口径（message_start/message_update/message_end
    # 会对同一次调用重复携带，流式 partial 还会出现多次）。这里聚合出过
    # 程指标（不参与计分）：输出/思考/输入 token、最终上下文规模、回合数
    # 与工具调用数。旧版 harness 的 manifest 无此字段，构建探索器时会用
    # 同一逻辑从 pi-events.jsonl 回填。
    usage = {
        "turns": 0, "tool_calls": 0,
        "tokens_output_total": 0, "tokens_reasoning_total": 0,
        "tokens_input_total": 0, "context_final": None,
    }
    try:
        handle = open(events_path, encoding="utf-8")
    except OSError:
        return usage
    with handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "tool_execution_start":
                usage["tool_calls"] += 1
                continue
            if kind != "turn_end":
                continue
            usage["turns"] += 1
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            u = message.get("usage") if isinstance(message.get("usage"), dict) else None
            if u is None:
                continue
            usage["tokens_output_total"] += int(u.get("output") or 0)
            usage["tokens_reasoning_total"] += int(u.get("reasoning") or 0)
            input_side = int(u.get("input") or 0) + int(u.get("cacheRead") or 0) + int(u.get("cacheWrite") or 0)
            usage["tokens_input_total"] += input_side
            usage["context_final"] = input_side
    return usage


def _process_from_actions(actions_path):
    counts = {
        "advance_count": 0,
        "write_count": 0,
        "finish_count": 0,
    }
    try:
        handle = open(actions_path, encoding="utf-8")
    except OSError:
        return counts
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            command = record.get("command")
            if not isinstance(command, dict):
                continue
            name = command.get("command")
            if name == "advance":
                counts["advance_count"] += 1
            elif name == "finish":
                counts["finish_count"] += 1
            elif name in {"dispatch", "reassign", "reposition", "transfer", "assign"}:
                counts["write_count"] += 1
    return counts


def _wall_clock_seconds(started, finished):
    try:
        from datetime import datetime
        start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        finish_dt = datetime.fromisoformat(finished.replace("Z", "+00:00"))
        return max(0.0, (finish_dt - start_dt).total_seconds())
    except (TypeError, ValueError):
        return None


details = _load(root / "verifier" / "details.json") or {}
contract = _load(task_dir / "environment" / "public_contract.json") or {}
state_files = sorted(p.name for p in (root / "state").glob("*")) if (root / "state").is_dir() else []
process = _process_from_actions(root / "state" / "actions.jsonl")
manifest = {
    "harness": "pi-host-bridge/v2",
    "task": task,
    "task_version": _task_version(),
    "contract_version": contract.get("contract_version"),
    "verifier_version": details.get("verifier_version"),
    "provider": provider,
    "model": model,
    "thinking": thinking,
    "tools": tools,
    "pi_version": pi_version,
    "compose_project": project,
    "started_at": started,
    "finished_at": finished,
    # Kept as explicit null/false fields so consumers can distinguish new
    # unlimited runs from old manifests that carried a numeric budget.
    "timeout_budget_sec": None,
    "timed_out": False,
    "pi_exit_code": int(pi_rc),
    "verdict": verdict,
    "diagnostic": diagnostic == "1",
    "reward": reward,
    "action_count": action_count,
    "usage": _usage_from_events(root / "pi-events.jsonl"),
    "process": process,
    "wall_clock_seconds": _wall_clock_seconds(started, finished),
    "state_files": state_files,
    "artifacts": {
        "pi_events": "pi-events.jsonl",
        "pi_stderr": "pi-stderr.log",
        "prompt": "prompt.md",
        "harness_log": "harness.log",
        "state_dir": "state/",
        "verifier_dir": "verifier/",
    },
}
(root / "run-manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY

log "完成：verdict=${verdict}（run 目录：${run_dir}）"
if [[ "$verdict" != "ok" ]]; then
  exit 1
fi
