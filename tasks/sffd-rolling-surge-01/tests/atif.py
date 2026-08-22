#!/usr/bin/env python3
"""Convert an EMS action/tool log into a minimal Harbor ATIF v1.7 document.

Harbor normally owns trajectory capture.  This dependency-free adapter is a
local smoke-path and a useful compatibility bridge for runners that expose
JSONL tool calls but do not yet emit ATIF themselves.  It deliberately keeps
only the public command arguments and redacted tool observations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence


FORBIDDEN_KEYS = {
    "seed", "rng", "random_seed", "oracle", "oracle_score", "reward_anchor",
    "hidden_events", "future_events", "future_incidents", "release_time",
    "release_time_s", "release_at_s", "available_time", "available_time_s",
    "available_at_s", "busy_until", "busy_until_s", "verifier", "ground_truth",
    "latent", "hidden", "private_state", "event_queue", "queue", "source_rowids",
    "source_rowid", "hidden_reference_responses", "private", "queue",
    "busy_until_s", "scenario", "raw_scenario", "calibration", "final_state",
    "event_log", "state_hashes", "replay_manifest",
}


def load_records(path: Path) -> list[Any]:
    if path.suffix.lower() == ".jsonl":
        records: list[Any] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}: {exc}") from exc
        return records
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        for key in ("actions", "commands", "trajectory", "responses"):
            if isinstance(value.get(key), list):
                return list(value[key])
    raise ValueError("log must contain a JSON array or JSONL records")


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                continue
            result[str(key)] = _redact(child)
        return result
    if isinstance(value, list):
        return [_redact(child) for child in value]
    return value


def _action_parts(record: Any) -> tuple[str, dict[str, Any], str, Any]:
    if not isinstance(record, Mapping):
        return str(record), {}, hashlib.sha256(str(record).encode()).hexdigest()[:16], None
    raw = dict(record)
    nested = raw.get("args") or raw.get("arguments")
    command_value = raw.get("command")
    if not nested and isinstance(command_value, Mapping):
        nested = command_value.get("args") or command_value.get("arguments")
        if not nested:
            # The simulator's append-only action record stores the normalized
            # command directly under ``command``.  Export only that public
            # command envelope; never copy verifier hashes/fingerprints from
            # the surrounding private record into an ATIF trajectory.
            nested = {
                key: value for key, value in command_value.items()
                if key not in {"command", "action", "op", "type"}
            }
    args = dict(nested) if isinstance(nested, Mapping) else {
        key: value for key, value in raw.items()
        if key not in {"action", "command", "name", "tool", "args", "arguments",
                       "response", "observation", "result", "output",
                       "record_type", "fingerprint", "before_state_hash",
                       "after_state_hash", "state_version", "sim_time"}
    }
    name = raw.get("action") or raw.get("name") or raw.get("tool")
    command = command_value
    if not name and isinstance(command, str):
        name = command
    if isinstance(command, Mapping):
        name = command.get("name") or command.get("action") or command.get("command") or name
    name = str(name or "unknown").strip().lower().replace("_", "-")
    action_id = args.get("action_id") or raw.get("action_id")
    if action_id is None:
        action_id = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:16]
    response = raw.get("response", raw.get("result", raw.get("output", raw.get("observation"))))
    return name, args, str(action_id), response


def build_atif(records: Sequence[Any], *, scenario_id: str = "sffd-rolling-surge-01") -> dict[str, Any]:
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, default=str).encode()).hexdigest()
    steps: list[dict[str, Any]] = []
    for index, record in enumerate(records, 1):
        name, args, action_id, response = _action_parts(record)
        tool_call = {
            "tool_call_id": action_id,
            "function_name": f"emsctl.{name}",
            "arguments": _redact(args),
        }
        step: dict[str, Any] = {
            "step_id": index,
            "source": "agent",
            "message": "",
            "tool_calls": [tool_call],
            # A command emitted by a harness is deterministic dispatch, not an
            # extra LLM inference.  This is explicitly allowed by ATIF v1.7.
            "llm_call_count": 0,
        }
        if response is not None:
            step["observation"] = {
                "results": [{
                    "source_call_id": action_id,
                    "content": json.dumps(_redact(response), sort_keys=True, ensure_ascii=True),
                }]
            }
        steps.append(step)
    return {
        "schema_version": "ATIF-v1.7",
        "trajectory_id": f"{scenario_id}-{digest[:16]}",
        "session_id": digest,
        "agent": {"name": "harbor-ems-agent", "version": "0.1.0"},
        "steps": steps,
        "final_metrics": {"total_steps": len(steps), "extra": {"scenario_id": scenario_id}},
        "extra": {"format_adapter": "sffd-rolling-surge-01", "action_log_sha256": digest},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario-id", default="sffd-rolling-surge-01")
    args = parser.parse_args(argv)
    document = build_atif(load_records(args.actions), scenario_id=args.scenario_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "steps": len(document["steps"]),
                      "schema_version": document["schema_version"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
