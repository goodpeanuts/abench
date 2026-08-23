#!/usr/bin/env bash
# Run a fixed number of pi harness rolls sequentially, then aggregate only the
# run directories created by this batch.
#
# Usage:
#   bash scripts/run-pi-batch.sh --n 3 [--out FILE] -- <run-pi-harness args>
#
# PI_BATCH_HARNESS and PI_BATCH_AGGREGATOR may override the two scripts for
# isolated testing. Production runs should use the repository defaults.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
harness_script="${PI_BATCH_HARNESS:-$repo_root/scripts/run-pi-harness.sh}"
aggregate_script="${PI_BATCH_AGGREGATOR:-$repo_root/scripts/aggregate_pi_runs.py}"
n="3"
out=""
harness_args=()

usage() {
  cat <<'EOF'
Usage: bash scripts/run-pi-batch.sh [--n N] [--out FILE] -- <harness args>

Runs scripts/run-pi-harness.sh N times sequentially (N defaults to 3). A failed roll is recorded
and does not stop later rolls. The final aggregate is written to --out, or to
results/pi-runs/_batch/batch-<UTC stamp>.json by default.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --n)
      [[ $# -ge 2 ]] || { echo "run-pi-batch.sh: --n requires a value" >&2; exit 2; }
      n="$2"
      shift 2
      ;;
    --out|--output)
      [[ $# -ge 2 ]] || { echo "run-pi-batch.sh: $1 requires a value" >&2; exit 2; }
      out="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      harness_args=("$@")
      break
      ;;
    *)
      echo "run-pi-batch.sh: unknown batch option: $1 (harness args must follow --)" >&2
      exit 2
      ;;
  esac
done

[[ "$n" =~ ^[1-9][0-9]*$ ]] || {
  echo "run-pi-batch.sh: --n must be a positive integer" >&2
  exit 2
}
[[ "${#harness_args[@]}" -gt 0 ]] || {
  echo "run-pi-batch.sh: harness arguments are required after --" >&2
  exit 2
}
[[ -f "$harness_script" ]] || {
  echo "run-pi-batch.sh: harness script not found: $harness_script" >&2
  exit 2
}
[[ -f "$aggregate_script" ]] || {
  echo "run-pi-batch.sh: aggregator script not found: $aggregate_script" >&2
  exit 2
}

batch_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
if [[ -z "$out" ]]; then
  out="$repo_root/results/pi-runs/_batch/batch-${batch_stamp}.json"
elif [[ -d "$out" ]]; then
  out="${out%/}/batch-${batch_stamp}.json"
fi

temporary_dir="$(mktemp -d "${TMPDIR:-/tmp}/abench-pi-batch.XXXXXX")"
attempts_file="$temporary_dir/attempts.tsv"
cleanup() {
  if [[ -n "${temporary_dir:-}" && -d "$temporary_dir" \
        && "$(basename "$temporary_dir")" == abench-pi-batch.* ]]; then
    rm -rf -- "$temporary_dir"
  fi
}
trap cleanup EXIT

run_dirs=()
failures=0
for ((roll = 1; roll <= n; roll++)); do
  run_log="$temporary_dir/roll-${roll}.log"
  printf '[pi-batch] roll %d/%d starting\n' "$roll" "$n"

  set +e
  bash "$harness_script" "${harness_args[@]}" 2>&1 | tee "$run_log"
  harness_rc=${PIPESTATUS[0]}
  set -e

  # The harness log uses a Chinese label ("run 目录："). Parse it by its
  # stable prefix instead of relying on locale-specific sed escapes.
  run_dir="$(python3 - "$run_log" <<'PY'
import pathlib
import sys

prefix = "[pi-harness] run "
last = ""
for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").splitlines():
    if line.startswith(prefix) and "：" in line:
        last = line.rsplit("：", 1)[1].strip()
print(last)
PY
)"
  if [[ -n "$run_dir" ]]; then
    if [[ ! -f "$run_dir/run-manifest.json" ]]; then
      printf '[pi-batch] roll %d produced no run-manifest.json: %s\n' \
        "$roll" "$run_dir" >&2
    fi
    run_dirs+=("$run_dir")
  fi
  printf '%s\t%s\t%s\n' "$roll" "$harness_rc" "$run_dir" >> "$attempts_file"

  if [[ "$harness_rc" -ne 0 ]]; then
    failures=$((failures + 1))
    printf '[pi-batch] roll %d/%d failed (exit %d); continuing\n' \
      "$roll" "$n" "$harness_rc" >&2
  else
    printf '[pi-batch] roll %d/%d completed\n' "$roll" "$n"
  fi
done

include_diagnostic=0
for arg in "${harness_args[@]}"; do
  if [[ "$arg" == "--allow-any-thinking" ]]; then
    include_diagnostic=1
    break
  fi
done

aggregate_args=(--metric overall --output "$out")
if [[ "${#run_dirs[@]}" -gt 0 ]]; then
  aggregate_args+=(--run-dir "${run_dirs[@]}")
else
  # Avoid accidentally aggregating historical runs when every attempt failed
  # before the harness created a run directory.
  empty_scan="$temporary_dir/no-runs"
  mkdir -p "$empty_scan"
  aggregate_args+=(--scan "$empty_scan")
fi
if [[ "$include_diagnostic" -eq 1 ]]; then
  aggregate_args+=(--include-diagnostic)
fi

set +e
python3 "$aggregate_script" "${aggregate_args[@]}"
aggregate_rc=$?
set -e

# Preserve roll-level failures, including preflight failures that produced no
# run-manifest.json, alongside the aggregate statistics.
if [[ -f "$out" ]]; then
  python3 - "$out" "$batch_stamp" "$n" "$attempts_file" "$harness_script" \
    "$aggregate_rc" "${harness_args[@]}" <<'PY'
import json
import os
import pathlib
import sys
import tempfile

output = pathlib.Path(sys.argv[1])
stamp = sys.argv[2]
requested = int(sys.argv[3])
attempts_path = pathlib.Path(sys.argv[4])
harness = sys.argv[5]
aggregate_exit_code = int(sys.argv[6])
harness_args = sys.argv[7:]

attempts = []
for line in attempts_path.read_text(encoding="utf-8").splitlines():
    roll, exit_code, run_dir = line.split("\t", 2)
    attempts.append({
        "roll": int(roll),
        "exit_code": int(exit_code),
        "run_dir": run_dir or None,
        "manifest_exists": bool(run_dir)
        and (pathlib.Path(run_dir) / "run-manifest.json").is_file(),
    })

payload = json.loads(output.read_text(encoding="utf-8"))
payload["batch"] = {
    "stamp": stamp,
    "requested_rolls": requested,
    "successful_processes": sum(item["exit_code"] == 0 for item in attempts),
    "failed_processes": sum(item["exit_code"] != 0 for item in attempts),
    "harness": harness,
    "harness_args": harness_args,
    "aggregate_exit_code": aggregate_exit_code,
    "attempts": attempts,
}

fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    os.replace(temporary_name, output)
except BaseException:
    try:
        os.unlink(temporary_name)
    except FileNotFoundError:
        pass
    raise
PY
fi

printf '[pi-batch] complete: %d requested, %d process failures, aggregate=%s\n' \
  "$n" "$failures" "$out"

if [[ "$aggregate_rc" -ne 0 ]]; then
  exit "$aggregate_rc"
fi
if [[ "$failures" -ne 0 ]]; then
  exit 1
fi
