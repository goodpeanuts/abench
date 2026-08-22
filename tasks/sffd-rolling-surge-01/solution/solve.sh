#!/usr/bin/env bash
set -euo pipefail

# Conservative local baseline.  It intentionally never claims hidden
# information: observe, repeatedly advance visible events, and finish.  The
# Harbor evaluator may replace this with a stronger agent solution.
if command -v emsctl >/dev/null 2>&1; then
  emsctl observe >/dev/null || true
  for _ in $(seq 1 256); do
    if ! emsctl advance --next-event >/dev/null 2>&1; then
      break
    fi
  done
  emsctl finish --reason "baseline horizon reached" >/dev/null 2>&1 || true
fi
