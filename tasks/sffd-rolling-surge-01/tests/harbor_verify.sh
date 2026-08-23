#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

scenario=""
for candidate in \
  "${HARBOR_SCENARIO:-}" \
  "/opt/sffd-sidecar/scenario.json" \
  "/opt/sffd-task/scenario.json" \
  "/tests/scenario.json" \
  "$SCRIPT_DIR/../scenario.json" \
  "$SCRIPT_DIR/../data/scenario.json"; do
  if [[ -n "$candidate" && -f "$candidate" ]]; then
    scenario="$candidate"
    break
  fi
done

state_dir="${HARBOR_STATE_DIR:-/tmp/ems-state}"

# The sidecar's action log is authoritative: it is written by the simulator
# process, not by the Agent.  Harbor materializes sidecar artifacts at their
# original absolute paths in a separate verifier environment.  Always prefer
# that log, even when an Agent/ATIF path was supplied through the environment;
# those paths remain fallbacks for local providers without a compose sidecar.
if [[ -f "$state_dir/actions.jsonl" ]]; then
  actions="$state_dir/actions.jsonl"
elif [[ -f "/tmp/ems-state/actions.jsonl" ]]; then
  actions="/tmp/ems-state/actions.jsonl"
elif [[ -n "${HARBOR_ACTIONS:-}" ]]; then
  actions="$HARBOR_ACTIONS"
elif [[ -n "${ACTIONS_PATH:-}" ]]; then
  actions="$ACTIONS_PATH"
elif [[ -n "${AGENT_ACTIONS:-}" ]]; then
  actions="$AGENT_ACTIONS"
else
  actions="/logs/agent/actions.jsonl"
  for candidate in /logs/agent/actions.json /logs/agent/trajectory.json /logs/agent/trajectory.jsonl \
                   "$SCRIPT_DIR/actions.jsonl"; do
    if [[ -f "$candidate" ]]; then actions="$candidate"; break; fi
  done
fi
reward="${HARBOR_REWARD_PATH:-/logs/verifier/reward.json}"
details="${HARBOR_DETAILS_PATH:-/logs/verifier/details.json}"
replay="${HARBOR_REPLAY_PATH:-/logs/verifier/replay.json}"

mkdir -p "$(dirname "$reward")" "$(dirname "$details")" "$(dirname "$replay")"

if [[ -z "$scenario" ]]; then
  # Keep infrastructure/configuration failures machine-readable.  Harbor can
  # still collect reward.json even when a provider failed to transfer an
  # artifact, and a missing scenario must never receive a positive score.
  "$PYTHON_BIN" - "$reward" "$details" "$SCRIPT_DIR" <<'PY'
import json
import sys
reward_path, details_path, script_dir = sys.argv[1:4]
sys.path.insert(0, script_dir)
from score import VERIFIER_VERSION, _zero_reward

reward = _zero_reward()
details = {
    "verifier_version": VERIFIER_VERSION,
    "action_count": 0,
    "reward": reward,
    "errors": ["scenario not found in verifier environment"],
}
with open(reward_path, "w", encoding="utf-8") as handle:
    json.dump(reward, handle, indent=2, sort_keys=True)
    handle.write("\n")
with open(details_path, "w", encoding="utf-8") as handle:
    json.dump(details, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
  exit 0
fi

# Missing actions are treated as an empty trajectory.  The independent
# verifier still replays that trajectory, which makes a no-op score explicit
# and preserves the deterministic integrity check.
score_args=(--scenario "$scenario" --output "$reward" --details-output "$details")
replay_args=(--scenario "$scenario" --actions "$actions" --output "$replay" --tamper-check)
if [[ -f "$actions" ]]; then
  score_args+=(--actions "$actions")
fi
if [[ -d "$state_dir" ]]; then
  replay_args+=(--state-dir "$state_dir")
  score_args+=(--state-dir "$state_dir")
fi

"$PYTHON_BIN" "$SCRIPT_DIR/replay_check.py" "${replay_args[@]}" || true
"$PYTHON_BIN" "$SCRIPT_DIR/score.py" "${score_args[@]}" || true

# The verifier process itself must remain successful after writing reward.json;
# task quality is represented by the reward/integrity fields, not an exit code.
exit 0
