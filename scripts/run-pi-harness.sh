#!/usr/bin/env bash
# run-pi-harness.sh — 宿主机 pi harness + 容器桥接（abench）
#
# 架构：pi 在宿主机运行（有 LLM API 网络、有 ~/.pi/agent/models.json provider
# 目录），其 bash 工具只经逐字桥接命令 `podman compose … exec -T main emsctl …`
# 驱动任务容器；容器保持隔离。被评分的动作日志来自 simulator sidecar
# （/tmp/ems-state/actions.jsonl，评分权威），评分在宿主机跑 tests/harbor_verify.sh。
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
thinking="${PI_HARNESS_THINKING:-low}"
timeout_sec="${PI_HARNESS_TIMEOUT_SEC:-840}"
task_rel="${PI_HARNESS_TASK:-tasks/sffd-rolling-surge-01}"
project="${PI_HARNESS_PROJECT:-sffd-pi}"
pi_bin="${PI_BIN:-}"
bootstrap_only=0
skip_smoke=0
keep=0
no_verify=0
tools_list="bash,read,grep,find,ls,write,edit"

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
case "$thinking" in
  off|minimal|low|medium|high|xhigh|max) ;;
  *) echo "推理档 ${thinking} 不在 pi 支持的档位内（off|minimal|low|medium|high|xhigh|max）" >&2; exit 2 ;;
esac
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

bridge="podman ${COMPOSE[*]} exec -T main emsctl"
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

# ---------- 5. 组装 prompt ----------

prompt_file="$run_dir/prompt.md"
{
  cat "$task_dir/instruction.md"
  cat <<PROMPT

## 运行方式与桥接（重要）

你在宿主机上运行，任务环境在容器里。emsctl 是唯一的环境表面，且只能通过
下面这条逐字桥接命令调用（把 <子命令及参数> 换成实际参数）：

    ${bridge} <子命令及参数>

例如第一条观察：

    ${bridge} observe

规则：

- 每次调用在 stdout 恰好输出一个 JSON 对象；退出码 2 表示结构化拒绝
  （状态版本过期 / action_id 重复 / 未知对象 / 单位忙 / 能力不匹配）——
  读取返回的 JSON，改用最新的 state_version 与全新的唯一 action_id 重试；
  完全相同的 action_id 重试是幂等的。
- 先 observe，再按上面的任务契约行动，持续 advance 到 horizon，或在无可
  行动作时 finish。
- 你的总时间预算约 ${timeout_sec} 秒；接近预算时执行下面这条命令收尾
  （reason 自拟）：

    ${bridge} finish --reason "budget almost exhausted"
- 仓库中 tasks/ 目录（尤其 scenario.json、tests/、environment/、solution/）
  是评测方私有：不要读取、列出或修改。临时文件只写在当前工作目录内。
  除 LLM API（由 harness 提供）外不要使用网络。
PROMPT
} > "$prompt_file"
cp "$prompt_file" "$workspace/BRIDGE.md"

# ---------- 6. 启动 pi（手动 watchdog） ----------

events_file="$run_dir/pi-events.jsonl"
stderr_file="$run_dir/pi-stderr.log"
log "启动 pi：provider=${provider} model=${model} thinking=${thinking} timeout=${timeout_sec}s"

(
  cd "$workspace"
  export PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0
  exec "$pi_bin" -p "$(<"$prompt_file")" \
    --approve --no-session --mode json \
    --provider "$provider" --model "$model" \
    --thinking "$thinking" \
    --tools "$tools_list" \
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
python3 - "$run_dir" "$task_rel" "$provider" "$model" "$thinking" "$tools_list" \
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
    "harness": "pi-host-bridge/v1",
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
