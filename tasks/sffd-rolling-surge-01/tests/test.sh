#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TASK_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# In a separate Harbor verifier image the hidden scenario is transferred to
# its original sidecar path and tests/ is baked into /tests.  Harbor always
# invokes /tests/test.sh, so dispatch to the grading entrypoint before the
# local developer test suite.
if [[ "${SFFD_HARBOR_VERIFIER:-0}" == "1" || "$SCRIPT_DIR" == "/tests" || \
      -f "/opt/sffd-sidecar/scenario.json" || -f "/opt/sffd-task/scenario.json" ]]; then
  exec bash "$SCRIPT_DIR/harbor_verify.sh"
fi

cd "$TASK_ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3}"

"$PYTHON_BIN" -m unittest discover -s tests -p 'test_*.py' -v

# Run a real scenario smoke path as well as the unit tests.  Harbor normally
# mounts these files into separate agent/verifier containers; locally we use a
# narrow temporary directory to exercise the same JSON interfaces.
if [[ -f scenario.json ]]; then
  smoke_dir="$(mktemp -d "${TMPDIR:-/tmp}/sffd-smoke.XXXXXX")"
  trap 'rm -rf "$smoke_dir"' EXIT
  tmp_actions="$smoke_dir/actions.jsonl"
  printf '%s\n' '{"action":"observe","action_id":"smoke-observe"}' > "$tmp_actions"
  printf '%s\n' '{"action":"advance","args":{"next_event":true},"action_id":"smoke-advance"}' >> "$tmp_actions"
  "$PYTHON_BIN" tests/replay_check.py --scenario scenario.json --actions "$tmp_actions" --tamper-check --output "$smoke_dir/replay.json"
  "$PYTHON_BIN" tests/atif.py --actions "$tmp_actions" --output "$smoke_dir/trajectory.json"
  "$PYTHON_BIN" tests/score.py --scenario scenario.json --actions "$tmp_actions" \
    --output "$smoke_dir/reward.json" --details-output "$smoke_dir/details.json" >/dev/null
  "$PYTHON_BIN" - <<'PY' "$smoke_dir/reward.json" "$smoke_dir/details.json" "$smoke_dir/trajectory.json"
import json, sys
reward = json.load(open(sys.argv[1], encoding="utf-8"))
details = json.load(open(sys.argv[2], encoding="utf-8"))
trajectory = json.load(open(sys.argv[3], encoding="utf-8"))
required = {"response", "coverage", "safety", "adaptation", "efficiency", "integrity", "overall", "strict_pass"}
if set(reward) != required:
    raise SystemExit(f"reward.json has unexpected keys: {sorted(set(reward) ^ required)}")
if any(type(value) not in {int, float} for value in reward.values()):
    raise SystemExit("reward.json contains a non-numeric value")
if details.get("reward") != reward:
    raise SystemExit("details.json does not identify the emitted reward")
if trajectory.get("schema_version") != "ATIF-v1.7":
    raise SystemExit("ATIF trajectory has unexpected schema version")
PY
fi
