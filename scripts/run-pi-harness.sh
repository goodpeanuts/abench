#!/usr/bin/env bash
# run-pi-harness.sh — 宿主机 pi harness + 容器桥接（abench）
#
# 架构：pi 在宿主机运行（有 LLM API 网络、有 ~/.pi/agent/models.json provider
# 目录），但模型**没有任何文件/shell/网络工具**：经 scripts/pi-emsctl-tool.mjs
# 注册的唯一工具 emsctl，把每次调用以固定 argv（不经 shell）桥接到
# `podman compose … exec -T main emsctl <子命令>`。容器保持隔离；宿主机上的
# 隐藏 scenario / 评测代码对模型不可达（硬封锁，非提示约束）。
# 被评分的动作日志来自 simulator sidecar（/tmp/ems-state/actions.jsonl，评分
# 权威），评分在宿主机跑 tests/harbor_verify.sh。
#
# 用法：
#   bash scripts/run-pi-harness.sh --provider dashscope --model qwen3.8-max
#   常用选项：--thinking low|--task <dir>|--timeout 840|--bootstrap-only|
#             --skip-smoke|--keep|--no-verify
# 环境变量：PI_HARNESS_PROVIDER/MODEL/THINKING/TIMEOUT_SEC/TASK/PROJECT、PI_BIN。
# 密钥变量只按名检查并透传给 pi，绝不写入任何文件或日志。
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
installed_models="$HOME/.pi/agent/models.json"

provider="${PI_HARNESS_PROVIDER:-}"
model="${PI_HARNESS_MODEL:-}"
thinking="${PI_HARNESS_THINKING:-}"
timeout_sec="${PI_HARNESS_TIMEOUT_SEC:-840}"
task_rel="${PI_HARNESS_TASK:-tasks/sffd-rolling-surge-01}"
project="${PI_HARNESS_PROJECT:-sffd-pi}"
pi_bin="${PI_BIN:-}"
bootstrap_only=0
skip_smoke=0
keep=0
no_verify=0
pi_ext="$repo_root/scripts/pi-emsctl-tool.mjs"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --provider) [[ $# -ge 2 ]] || { echo "--provider 需要一个值" >&2; exit 2; }; provider="$2"; shift 2 ;;
    --model) [[ $# -ge 2 ]] || { echo "--model 需要一个值" >&2; exit 2; }; model="$2"; shift 2 ;;
    --thinking) [[ $# -ge 2 ]] || { echo "--thinking 需要一个值" >&2; exit 2; }; thinking="$2"; shift 2 ;;
    --task) [[ $# -ge 2 ]] || { echo "--task 需要一个值" >&2; exit 2; }; task_rel="$2"; shift 2 ;;
    --timeout) [[ $# -ge 2 ]] || { echo "--timeout 需要一个值（秒）" >&2; exit 2; }; timeout_sec="$2"; shift 2 ;;
    --project) [[ $# -ge 2 ]] || { echo "--project 需要一个值" >&2; exit 2; }; project="$2"; shift 2 ;;
    --bootstrap-only) bootstrap_only=1; shift ;;
    --skip-smoke) skip_smoke=1; shift ;;
    --keep) keep=1; shift ;;
    --no-verify) no_verify=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

[[ -n "$provider" && -n "$model" ]] || {
  echo "必须指定 --provider 与 --model（或 PI_HARNESS_PROVIDER / PI_HARNESS_MODEL）" >&2
  exit 2
}

# 仓库约束（AGENTS.md「测试运行约定」）：所有评测运行必须使用模型相应的
# 最高思考档；降档一律拒绝，不允许静默降级。
resolve_max_thinking() {
  case "$1/$2" in
    wodex/claude-opus-5|moonshot/kimi-k3|dashscope/qwen3.8-max)
      printf '%s\n' "max" ;;
    wodex-gpt/gpt-5-sol)
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
elif [[ "$thinking" != "$max_thinking" ]]; then
  echo "仓库约束：${provider}/${model} 的评测必须使用最高思考档 ${max_thinking}（收到 ${thinking}），不接受降档" >&2
  exit 2
fi

[[ "$timeout_sec" =~ ^[0-9]+$ ]] || { echo "--timeout 必须是正整数秒" >&2; exit 2; }

task_dir="$repo_root/$task_rel"
log() { printf '[pi-harness] %s\n' "$*"; }

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

if [[ "$skip_smoke" -ne 1 ]]; then
  log "静态冒烟：bash scripts/pi-smoke.sh --no-call"
  bash "$repo_root/scripts/pi-smoke.sh" --no-call
fi

# 密钥变量名从已安装目录解析（只查存在性，不读值）
key_var="$(node -e '
  const fs = require("fs");
  const cfg = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
  const p = (cfg.providers || {})[process.argv[2]];
  if (!p) { console.error("未知 provider"); process.exit(1); }
  const m = String(p.apiKey || "").match(/^\$([A-Za-z_][A-Za-z0-9_]*)$/);
  if (!m) { console.error("apiKey 不是 $ENV_VAR 形式"); process.exit(1); }
  console.log(m[1]);
' "$installed_models" "$provider")" || { log "无法从 ${installed_models} 解析 ${provider} 的密钥变量"; exit 1; }
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
teardown() {
  if [[ "${keep:-0}" != "1" ]]; then
    podman "${COMPOSE[@]:-}" down -v --remove-orphans >/dev/null 2>&1 || true
  fi
}
trap teardown EXIT

# ---------- 2. run dir ----------

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
run_dir="$repo_root/results/pi-runs/${stamp}-${provider}-${model}"
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
  agent_smoke_out="$(
    cd "$workspace" && PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0 \
    "$pi_bin" -p "只回复：OK" --approve --no-session --mode json \
      --provider "$provider" --model "$model" --thinking "$max_thinking" \
      --tools emsctl --extension "$pi_ext" \
      --no-extensions --no-skills --no-prompt-templates --no-themes
  )" || { log "agent 烟测失败：扩展加载或工具注册异常"; exit 1; }
  grep -q '"type":"agent_end"' <<<"$agent_smoke_out" || {
    log "agent 烟测未产生 agent_end"; exit 1
  }
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
- 收尾：args = ["finish","--reason","all feasible work complete"]

规则：

- 工具报错表示结构化拒绝（状态版本过期 / action_id 重复 / 未知对象 /
  单位忙 / 能力不匹配）——读取错误中的 JSON 说明，改用最新的
  state_version 与全新的唯一 action_id 重试；完全相同的 action_id 重试是
  幂等的。
- 先 observe，再按上面的任务契约行动，持续 advance 到 horizon，或在无可
  行动作时 finish。
- 你没有文件读写、shell 或网络工具；请直接在对话中推理并记住状态。
- 你的总时间预算约 ${timeout_sec} 秒；接近预算时用 finish 收尾（reason
  自拟）。
PROMPT
} > "$prompt_file"
cp "$prompt_file" "$workspace/BRIDGE.md"

# ---------- 6. 启动 pi（手动 watchdog） ----------

events_file="$run_dir/pi-events.jsonl"
stderr_file="$run_dir/pi-stderr.log"
log "启动 pi（单工具硬封锁）：provider=${provider} model=${model} thinking=${thinking} timeout=${timeout_sec}s"

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

timed_out=0
pi_rc=0
elapsed=0
while :; do
  if ! kill -0 "$pi_pid" 2>/dev/null; then
    wait "$pi_pid" || pi_rc=$?
    break
  fi
  if [[ "$elapsed" -ge "$timeout_sec" ]]; then
    log "超过 ${timeout_sec}s，终止 pi（TERM）"
    kill "$pi_pid" 2>/dev/null || true
    for _ in $(seq 1 5); do
      kill -0 "$pi_pid" 2>/dev/null || break
      sleep 1
    done
    kill -9 "$pi_pid" 2>/dev/null || true
    wait "$pi_pid" 2>/dev/null || pi_rc=$?
    timed_out=1
    break
  fi
  sleep 1
  elapsed=$((elapsed + 1))
  if (( elapsed % 30 == 0 )); then
    log "仍在运行（${elapsed}s/${timeout_sec}s，事件 $(wc -l < "$events_file" 2>/dev/null || echo 0) 行）"
  fi
done
log "pi 结束：退出码 ${pi_rc}，超时标志 ${timed_out}"

# ---------- 7. 事件流判定 ----------
# pi 的 CLI 即使最后一轮以 API 错误收场也以 0 退出；判定失败不阻断评分
# （回合状态真实存在），只记入 manifest。

verdict="$(python3 - "$events_file" <<'PY'
import json, sys

saw_agent_end = False
final_stop = None
try:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "agent_end":
                saw_agent_end = True
            if event.get("type") == "message_end":
                message = event.get("message") or {}
                if message.get("role") == "assistant":
                    final_stop = message.get("stopReason")
except OSError:
    print("no_agent_end")
else:
    if not saw_agent_end:
        print("no_agent_end")
    elif final_stop == "error":
        print("error_stop")
    else:
        print("ok")
PY
)" || verdict="no_agent_end"

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
if [[ "$no_verify" = "1" ]]; then
  log "--no-verify：跳过评分"
else
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
fi

# ---------- 10. run-manifest ----------

finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 - "$run_dir" "$task_rel" "$provider" "$model" "$thinking" "emsctl" \
  "$pi_version" "$project" "$started_at" "$finished_at" "$timeout_sec" \
  "$timed_out" "$pi_rc" "$verdict" "$reward_summary" <<'PY'
import json, pathlib, sys

(run_dir, task, provider, model, thinking, tools, pi_version, project,
 started, finished, timeout_sec, timed_out, pi_rc, verdict, reward_summary) = sys.argv[1:16]
root = pathlib.Path(run_dir)
reward = None
action_count = None
if reward_summary != "null":
    try:
        payload = json.loads(reward_summary)
        reward = payload.get("reward")
        action_count = payload.get("action_count")
    except json.JSONDecodeError:
        pass
state_files = sorted(p.name for p in (root / "state").glob("*")) if (root / "state").is_dir() else []
manifest = {
    "harness": "pi-host-bridge/v2",
    "task": task,
    "provider": provider,
    "model": model,
    "thinking": thinking,
    "tools": tools,
    "pi_version": pi_version,
    "compose_project": project,
    "started_at": started,
    "finished_at": finished,
    "timeout_budget_sec": int(timeout_sec),
    "timed_out": timed_out == "1",
    "pi_exit_code": int(pi_rc),
    "verdict": verdict,
    "reward": reward,
    "action_count": action_count,
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
