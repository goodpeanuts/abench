#!/usr/bin/env python3
"""Harbor verifier entry point for SFFD Rolling Surge 01.

Usage (inside Harbor or locally)::

    python tests/score.py --scenario data/scenario.json \
        --actions /logs/agent/actions.jsonl \
        --output /logs/verifier/reward.json \
        --details-output /logs/verifier/details.json

The command remains useful without Harbor: it can score a supplied trace or
perform a structural audit. ``reward.json`` contains only Harbor-compatible
numeric rewards; explanations and replay diagnostics are written separately.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from verifier_lib import (  # type: ignore
        VERIFIER_VERSION,
        compute_score,
        load_json,
        load_jsonl,
        replay_actions,
        _public_integrity,
    )
else:
    from .verifier_lib import (
        VERIFIER_VERSION,
        compute_score,
        load_json,
        load_jsonl,
        replay_actions,
        _public_integrity,
    )


# The official eight keys keep their values and order; the six reference keys
# (unsquared, ungated) are appended as reporting aids so no consumer ever has
# to reconstruct a linear scale from compressed numbers.
REWARD_KEYS = (
    "response",
    "coverage",
    "safety",
    "adaptation",
    "efficiency",
    "integrity",
    "overall",
    "strict_pass",
    "response_linear",
    "safety_ungated",
    "adaptation_ungated",
    "efficiency_ungated",
    "overall_reference",
    "completion_rate",
)


def _first_existing(candidates: list[Path]) -> Path | None:
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def default_scenario(task_root: Path) -> Path:
    found = _first_existing([
        task_root / "data" / "scenario.json",
        task_root / "scenario.json",
        task_root / "data" / "scenario.example.json",
    ])
    return found or task_root / "data" / "scenario.json"


def default_actions() -> Path | None:
    candidates = []
    for variable in ("HARBOR_ACTIONS", "ACTIONS_PATH", "AGENT_ACTIONS", "TRAJECTORY_ACTIONS"):
        value = os.environ.get(variable)
        if value:
            candidates.append(Path(value))
    candidates.extend([
        Path("/logs/agent/actions.jsonl"),
        Path("/logs/agent/actions.json"),
        Path("/logs/agent/trajectory.json"),
        Path("/logs/agent/trajectory.jsonl"),
        Path("actions.jsonl"),
    ])
    return _first_existing(candidates)


def load_actions(path: Path) -> list[Any]:
    if path.suffix.lower() == ".jsonl":
        return load_jsonl(path)
    value = load_json(path)
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("actions", "commands", "trajectory"):
            if isinstance(value.get(key), list):
                return value[key]
        # ATIF-v1.x trajectory documents store tool calls inside agent steps.
        steps = value.get("steps")
        if isinstance(steps, list):
            records: list[dict[str, Any]] = []
            for step in steps:
                if not isinstance(step, dict):
                    continue
                observation = step.get("observation")
                response: Any = None
                if isinstance(observation, dict) and isinstance(observation.get("results"), list):
                    result_items = observation["results"]
                    if result_items:
                        content = result_items[0].get("content") if isinstance(result_items[0], dict) else None
                        if isinstance(content, str):
                            try:
                                response = json.loads(content)
                            except json.JSONDecodeError:
                                response = content
                        else:
                            response = content
                for call in step.get("tool_calls", []):
                    if not isinstance(call, dict):
                        continue
                    name = str(call.get("function_name", "unknown"))
                    if name.startswith("emsctl."):
                        name = name.split(".", 1)[1]
                    record: dict[str, Any] = {
                        "action": name,
                        "args": call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
                        "action_id": call.get("tool_call_id"),
                    }
                    if response is not None:
                        record["response"] = response
                    records.append(record)
            return records
    raise ValueError(f"actions file must contain a JSON array or JSONL: {path}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    task_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=default_scenario(task_root))
    parser.add_argument("--actions", type=Path, default=default_actions())
    parser.add_argument("--trace", type=Path, default=None, help="optional simulator trace JSON")
    parser.add_argument("--output", type=Path, default=Path(os.environ.get("HARBOR_REWARD_PATH", "/logs/verifier/reward.json")))
    parser.add_argument(
        "--details-output",
        type=Path,
        default=None,
        help="diagnostic JSON path (default: details.json beside reward.json)",
    )
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--no-replay", action="store_true", help="skip simulator replay; useful for static fallback")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    details_output = args.details_output or args.output.with_name("details.json")
    if not args.scenario.is_file():
        reward = _zero_reward()
        details = {
            "verifier_version": VERIFIER_VERSION,
            "action_count": 0,
            "reward": reward,
            "errors": [f"scenario not found: {args.scenario}"],
        }
        _write_json(args.output, reward)
        _write_json(details_output, details)
        return 2

    scenario = load_json(args.scenario)
    action_records: list[Any] = []
    action_load_error: str | None = None
    if args.actions is not None and args.actions.is_file():
        try:
            action_records = load_actions(args.actions)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            # A malformed agent trajectory is a scored integrity failure, not
            # a verifier process crash.  Keep the reward schema available so
            # Harbor can distinguish a bad submission from infrastructure
            # failure.
            action_load_error = f"actions_load_error: {type(exc).__name__}: {exc}"

    trace: Any = None
    trace_load_error: str | None = None
    if args.trace is not None and args.trace.is_file():
        try:
            trace = load_json(args.trace)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            trace_load_error = f"trace_load_error: {type(exc).__name__}: {exc}"

    replay = None
    if action_load_error is not None or trace_load_error is not None:
        replay = {
            "ok": False,
            "simulator_available": False,
            "reason": action_load_error or trace_load_error,
        }
    elif not args.no_replay:
        # Ask the independent replayer for its authoritative trace so the
        # outcome metrics are derived from simulated events rather than from
        # an agent-supplied summary.  The private trace is removed before the
        # result is serialized below; it must never become part of
        # agent-visible reward metadata.
        replay = replay_actions(args.scenario, action_records, args.state_dir, include_trace=True)
        # A successful simulator replay supplies the authoritative trace.
        if replay.get("trace") is not None:
            trace = replay["trace"]

    result = compute_score(scenario, action_records, trace=trace, integrity=replay)
    reward = _numeric_reward(result)
    errors = [error for error in (action_load_error, trace_load_error) if error]
    details: dict[str, Any] = {
        "verifier_version": VERIFIER_VERSION,
        "action_count": len(action_records),
        "reward": reward,
        "evidence": _aggregate_evidence(result.get("evidence")),
        "errors": errors,
    }
    if replay is not None:
        details["replay"] = _replay_summary(replay)

    # Details are useful after grading, but must not turn into a second copy
    # of the hidden simulator trace.  Recursively redact latent state, RNG
    # material, source row IDs, raw actions, and private event logs.
    public_details = _public_integrity(details)
    if not isinstance(public_details, dict):
        public_details = {
            "verifier_version": VERIFIER_VERSION,
            "reward": reward,
            "errors": ["diagnostic redaction returned an invalid shape"],
        }

    _write_json(args.output, reward)
    _write_json(details_output, public_details)
    # Harbor consumes reward.json as the task result.  Ordinary agent errors
    # (and integrity failures) are represented in that file rather than being
    # conflated with a verifier process/infrastructure failure.
    return 0


def _zero_reward() -> dict[str, float | int]:
    return {
        "response": 0.0,
        "coverage": 0.0,
        "safety": 0.0,
        "adaptation": 0.0,
        "efficiency": 0.0,
        "integrity": 0.0,
        "overall": 0.0,
        "strict_pass": 0,
        "response_linear": 0.0,
        "safety_ungated": 0.0,
        "adaptation_ungated": 0.0,
        "efficiency_ungated": 0.0,
        "overall_reference": 0.0,
        "completion_rate": 0.0,
    }


def _numeric_reward(result: dict[str, Any]) -> dict[str, float | int]:
    """Project a rich score report onto Harbor's numeric reward schema."""

    reward = _zero_reward()
    for key in REWARD_KEYS:
        value = result.get(key)
        # bool is an int subclass, but emitting JSON booleans violates the
        # verifier contract and Harbor's numeric result model.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        reward[key] = int(value) if key == "strict_pass" else float(value)
    return reward


def _aggregate_evidence(value: Any) -> dict[str, Any]:
    """Keep score explanations useful without publishing hidden incidents.

    The rich verifier report contains one row per scenario incident.  A
    hidden variant may include arrivals the Agent never observed before it
    stopped, so even a post-run diagnostic artifact must not serialize that
    table.  Counts, weights, and violation reason categories are sufficient
    to explain the numeric reward without revealing IDs, timing, priority, or
    counterfactual outcomes.
    """

    if not isinstance(value, dict):
        return {}
    summary: dict[str, Any] = {}
    scalar_keys = (
        "incident_count",
        "covered_weight",
        "timely_coverage_weight",
        "completed_weight",
        "total_priority_weight",
        "completion_rate",
        "service_gate",
        "response_quality_power",
        "late_count",
        "high_priority_missed",
        "hard_failure",
    )
    for key in scalar_keys:
        child = value.get(key)
        if isinstance(child, (bool, int, float)) and not isinstance(child, str):
            summary[key] = child

    for source_key, count_key, reasons_key in (
        ("invalid_actions", "invalid_action_count", "invalid_action_reasons"),
        ("simulator_rejected_actions", "simulator_rejected_action_count", "simulator_rejected_action_reasons"),
        ("catastrophic_violations", "catastrophic_violation_count", "catastrophic_violation_reasons"),
    ):
        rows = value.get(source_key)
        if not isinstance(rows, list):
            continue
        summary[count_key] = len(rows)
        reasons = sorted({
            str(item.get("reason"))
            for item in rows
            if isinstance(item, dict) and item.get("reason") is not None
        })
        if reasons:
            summary[reasons_key] = reasons

    hidden = value.get("hidden_state_violations")
    if isinstance(hidden, list):
        summary["hidden_state_violation_count"] = len(hidden)

    integrity = value.get("integrity")
    if isinstance(integrity, dict):
        summary["integrity"] = _replay_summary(integrity)
    return summary


def _replay_summary(value: Any) -> dict[str, Any]:
    """Return replay diagnostics that cannot disclose simulator state."""

    if not isinstance(value, dict):
        return {}
    public: dict[str, Any] = {}
    scalar_keys = (
        "ok",
        "simulator_available",
        "action_hash",
        "state_hash",
        "state_hash_repeat",
        "trace_hash",
        "trace_hash_repeat",
        "replay_state_invariant_ok",
    )
    for key in scalar_keys:
        child = value.get(key)
        if isinstance(child, (bool, int, float, str)):
            public[key] = child

    divergence = value.get("first_divergence")
    if isinstance(divergence, dict):
        public["first_divergence"] = {
            key: divergence[key]
            for key in ("path", "first_length", "second_length")
            if isinstance(divergence.get(key), (int, float, str))
        }

    structural = value.get("structural")
    if isinstance(structural, dict):
        structural_summary: dict[str, Any] = {}
        for key in ("action_count", "unique_action_ids"):
            if isinstance(structural.get(key), (int, float)):
                structural_summary[key] = structural[key]
        for key in ("invalid_actions", "catastrophic_violations", "hidden_state_violations"):
            if isinstance(structural.get(key), list):
                structural_summary[f"{key[:-1]}_count"] = len(structural[key])
        public["structural"] = structural_summary
    return public


def _write_json(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
