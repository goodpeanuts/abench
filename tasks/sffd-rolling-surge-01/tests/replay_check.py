#!/usr/bin/env python3
"""Replay and tamper verifier for the SFFD Harbor task."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from verifier_lib import (  # type: ignore
        audit_actions,
        canonical_json,
        first_trace_divergence,
        load_json,
        load_jsonl,
        replay_actions,
        sha256_json,
    )
else:
    from .verifier_lib import audit_actions, canonical_json, first_trace_divergence, load_json, load_jsonl, replay_actions, sha256_json


def _load_actions(path: Path) -> list[Any]:
    if path.suffix.lower() == ".jsonl":
        return load_jsonl(path)
    value = load_json(path)
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("actions", "commands", "trajectory"):
            if isinstance(value.get(key), list):
                return value[key]
        steps = value.get("steps")
        if isinstance(steps, list):
            records: list[dict[str, Any]] = []
            for step in steps:
                if not isinstance(step, dict):
                    continue
                response: Any = None
                observation = step.get("observation")
                if isinstance(observation, dict) and isinstance(observation.get("results"), list):
                    results = observation["results"]
                    if results and isinstance(results[0], dict):
                        content = results[0].get("content")
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
                    record = {
                        "action": name,
                        "args": call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
                        "action_id": call.get("tool_call_id"),
                    }
                    if response is not None:
                        record["response"] = response
                    records.append(record)
            return records
    raise ValueError(f"actions file must contain a JSON array or JSONL: {path}")


def tamper_probe(actions: list[Any], scenario_path: Path | None = None) -> dict[str, Any]:
    """Create a minimally modified log and report the first changed path.

    This is intentionally independent of the simulator.  It proves that the
    verifier's canonical action digest changes when an action is altered.
    """

    original_digest = sha256_json(actions)
    modified = json.loads(json.dumps(actions))
    if not modified:
        modified = [{"action": "observe", "action_id": "tamper-probe"}]
    elif isinstance(modified[0], dict):
        target = modified[0]
        if isinstance(target.get("args"), dict):
            target["args"]["tampered"] = True
        elif isinstance(target.get("command"), dict):
            # Sidecar action records keep the actual flat command under a
            # nested ``command`` envelope.  Editing only an outer artifact
            # field would change the file digest while replaying the exact
            # same transition, defeating the divergence check.
            nested_command = target["command"]
            if isinstance(nested_command.get("args"), dict):
                nested_command["args"]["tampered"] = True
            else:
                nested_command["tampered"] = True
        else:
            target["tampered"] = True
    else:
        modified[0] = {"action": str(modified[0]), "tampered": True}
    modified_digest = sha256_json(modified)
    result = {
        "original_action_hash": original_digest,
        "tampered_action_hash": modified_digest,
        "changed": original_digest != modified_digest,
        # Keep an explicit JSON path so a verifier report can identify the
        # first divergent command instead of returning only two opaque hashes.
        "first_divergence": first_trace_divergence(actions, modified),
    }
    if scenario_path is not None and scenario_path.is_file():
        # Replay both logs through the independent simulator as well.  This
        # catches a tamper that changes a transition outcome even when the
        # edited JSON remains syntactically valid; only hashes/paths are
        # returned, never the private traces themselves.
        original_replay = replay_actions(scenario_path, actions, include_trace=True)
        tampered_replay = replay_actions(scenario_path, modified, include_trace=True)
        original_trace = original_replay.get("trace")
        tampered_trace = tampered_replay.get("trace")
        if original_trace is not None and tampered_trace is not None:
            result["replay_first_divergence"] = first_trace_divergence(
                original_trace, tampered_trace
            )
        result["original_replay_ok"] = bool(original_replay.get("ok"))
        result["tampered_replay_ok"] = bool(tampered_replay.get("ok"))
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=root / "data" / "scenario.json")
    parser.add_argument("--actions", type=Path, default=Path("/logs/agent/actions.jsonl"))
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=Path("/logs/verifier/replay.json"))
    parser.add_argument("--tamper-check", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.scenario.is_file():
        result = {"ok": False, "reason": f"scenario not found: {args.scenario}"}
        _write(args.output, result)
        return 2
    actions = _load_actions(args.actions) if args.actions.is_file() else []
    scenario = load_json(args.scenario)
    structural = audit_actions(scenario, actions)
    result = replay_actions(args.scenario, actions, args.state_dir)
    result["tamper"] = tamper_probe(actions, args.scenario) if args.tamper_check else None
    result["action_count"] = len(actions)
    result["structural"] = structural
    _write(args.output, result)
    return 0 if result.get("ok") else 1


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
