"""Independent scoring and replay helpers for the SFFD Harbor task.

The verifier deliberately does not trust a score produced by the simulator or
by the agent.  It consumes the frozen scenario, the agent action log, and (if
available) a simulator trace, then derives the reward and integrity checks
again.  The module only uses the Python standard library so it can run in the
Harbor verifier image and in a checkout without Harbor installed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence


WEIGHTS = {
    "response": 0.40,
    "coverage": 0.20,
    "safety": 0.15,
    "adaptation": 0.10,
    "efficiency": 0.10,
    "integrity": 0.05,
}

# A response that consumes most of its priority SLA should contribute much
# less than a response that arrives comfortably inside the target.  Squaring
# the bounded SLA quality makes the first task deliberately risk-sensitive:
# a policy cannot compensate for repeatedly missing urgent calls by completing
# a large number of low-priority calls late.
RESPONSE_QUALITY_POWER = 2

# SFFD has used several historical priority alphabets.  Keep the mapping
# intentionally conservative: unknown values are treated as medium priority,
# never silently promoted to the highest class.
PRIORITY_WEIGHT = {
    "1": 1.00,
    "P1": 1.00,
    "CODE_1": 1.00,
    "A": 1.00,
    "2": 0.75,
    "P2": 0.75,
    "CODE_2": 0.75,
    "B": 0.75,
    "3": 0.50,
    "P3": 0.50,
    "CODE_3": 0.50,
    "C": 0.50,
    "E": 0.50,
    "I": 0.50,
    "T": 0.50,
}

PRIORITY_SLA_SECONDS = {
    "1": 480.0,
    "P1": 480.0,
    "CODE_1": 480.0,
    "A": 480.0,
    "2": 900.0,
    "P2": 900.0,
    "CODE_2": 900.0,
    "B": 900.0,
    "3": 1200.0,
    "P3": 1200.0,
    "CODE_3": 1200.0,
    "C": 1200.0,
    "E": 1200.0,
    "I": 1200.0,
    "T": 1200.0,
}

FORBIDDEN_OBSERVATION_KEYS = {
    "seed",
    "rng",
    "random_seed",
    "oracle",
    "oracle_score",
    "reward_anchor",
    "hidden_events",
    "future_events",
    "future_incidents",
    "release_time",
    "release_time_s",
    "release_at_s",
    "available_time",
    "available_time_s",
    "available_at_s",
    "verifier",
    "ground_truth",
    "busy_until",
    "busy_until_s",
    "latent",
    "hidden",
    "private_state",
    "event_queue",
    "queue",
    "scenario",
    "raw_scenario",
    "calibration",
    "final_state",
    "event_log",
    "state_hashes",
    "replay_manifest",
}

EVENT_NAME_KEYS = ("event", "event_type", "type", "kind", "name", "status")
INCIDENT_KEYS = ("incident_id", "incident", "call_id", "id")
UNIT_KEYS = ("unit_id", "unit", "vehicle_id", "resource_id")
TIME_KEYS = ("sim_time", "time", "timestamp", "at", "time_s", "t")


def canonical_json(value: Any) -> str:
    """Return a stable JSON representation suitable for hashing."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_json(path: os.PathLike[str] | str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_jsonl(path: os.PathLike[str] | str) -> list[Any]:
    rows: list[Any] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            text = line.strip()
            if not text:
                continue
            try:
                rows.append(json.loads(text))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}: {exc}") from exc
    return rows


def verify_state_dir(state_dir: Path, *, scenario: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate append-only simulator artifacts without trusting their summary.

    The sidecar writes one JSON object per line and a checkpoint/manifest.  A
    verifier can compare the parsed logs and hashes independently; malformed
    or edited lines are reported with the first offending file/line.
    """

    result: dict[str, Any] = {"ok": True, "state_dir": str(state_dir), "errors": []}
    if not state_dir.is_dir():
        result.update({"ok": False, "errors": ["state_dir_missing"]})
        return result
    parsed: dict[str, list[Any]] = {}
    for name in ("actions.jsonl", "event_log.jsonl", "state_hashes.jsonl"):
        path = state_dir / name
        if not path.is_file():
            result["errors"].append(f"missing:{name}")
            continue
        rows: list[Any] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                result["errors"].append(f"malformed:{name}:{line_number}")
                break
        parsed[name] = rows
    manifest_path = state_dir / "replay_manifest.json"
    rng_manifest_path = state_dir / "rng_manifest.json"
    checkpoint_path = state_dir / "checkpoint.json"
    def _load_optional(path: Path, label: str) -> Any:
        if not path.is_file():
            return None
        try:
            return load_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            result["errors"].append(f"malformed:{label}")
            return None

    manifest = _load_optional(manifest_path, "replay_manifest.json")
    rng_manifest = _load_optional(rng_manifest_path, "rng_manifest.json")
    checkpoint = _load_optional(checkpoint_path, "checkpoint.json")
    if manifest is None:
        result["errors"].append("missing:replay_manifest.json")
    if rng_manifest is None:
        result["errors"].append("missing:rng_manifest.json")
    if checkpoint is None:
        result["errors"].append("missing:checkpoint.json")
    if scenario is not None and isinstance(checkpoint, Mapping):
        expected_hashes = {sha256_json(scenario)}
        # Simulator hashes use its normalized scenario representation, while
        # callers naturally pass the raw JSON object.  Accept both canonical
        # forms, but never accept an unrelated hash.
        try:
            simulator_class = load_simulator_class()
            if simulator_class is not None:
                normalized_simulator = simulator_class(scenario, hidden=True)
                expected_hashes.add(sha256_json(normalized_simulator.scenario))
        except Exception:
            pass
        if checkpoint.get("scenario_hash") not in expected_hashes:
            result["errors"].append("checkpoint_scenario_hash_mismatch")
        if checkpoint.get("source_scenario_hash") not in {None, sha256_json(scenario)}:
            result["errors"].append("checkpoint_source_scenario_hash_mismatch")
        if isinstance(manifest, Mapping) and manifest.get("scenario_hash") not in expected_hashes:
            result["errors"].append("manifest_scenario_hash_mismatch")
        if isinstance(manifest, Mapping) and manifest.get("source_scenario_hash") not in {None, sha256_json(scenario)}:
            result["errors"].append("manifest_source_scenario_hash_mismatch")
    if isinstance(manifest, Mapping):
        action_rows = [row for row in parsed.get("actions.jsonl", []) if isinstance(row, Mapping)]
        event_rows = parsed.get("event_log.jsonl", [])
        hash_rows = [row for row in parsed.get("state_hashes.jsonl", []) if isinstance(row, Mapping)]
        if manifest.get("action_log_sha256") != sha256_json(action_rows):
            result["errors"].append("action_log_hash_mismatch")
        # Compare even an empty event log.  A malicious truncation to zero
        # lines must not bypass the digest check merely because the parsed
        # list is false-y; the file is still an append-only artifact.
        if "event_log.jsonl" in parsed and manifest.get("event_log_sha256") != sha256_json(event_rows):
            result["errors"].append("event_log_hash_mismatch")
        if manifest.get("state_hash_log_sha256") != sha256_json(hash_rows):
            result["errors"].append("state_hash_log_hash_mismatch")
        if isinstance(rng_manifest, Mapping):
            embedded_rng = manifest.get("rng_manifest")
            if embedded_rng != dict(rng_manifest):
                result["errors"].append("rng_manifest_mismatch")
    # The checkpoint is the simulator's private causal snapshot.  Comparing
    # its stored append-only arrays with the independently parsed files gives
    # a useful first JSON path for an event/action tamper, rather than only an
    # opaque digest mismatch.
    if isinstance(checkpoint, Mapping):
        for label, file_name, checkpoint_key in (
            ("event_log", "event_log.jsonl", "event_log"),
            ("actions", "actions.jsonl", "actions"),
            ("state_hashes", "state_hashes.jsonl", "state_hashes"),
        ):
            expected_rows = checkpoint.get(checkpoint_key)
            actual_rows = parsed.get(file_name)
            if isinstance(expected_rows, list) and isinstance(actual_rows, list):
                divergence = first_trace_divergence(expected_rows, actual_rows)
                if divergence is not None:
                    result.setdefault("first_divergence", {
                        "artifact": label,
                        **divergence,
                    })
                    result["errors"].append(f"{label}_first_divergence")
    result["ok"] = not result["errors"]
    return result


def first_value(mapping: Mapping[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def normalize_priority(value: Any) -> str:
    if value is None:
        return "UNKNOWN"
    text = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "P_1": "P1",
        "P_2": "P2",
        "P_3": "P3",
        "PRIORITY_1": "1",
        "PRIORITY_2": "2",
        "PRIORITY_3": "3",
        "CODE1": "CODE_1",
        "CODE2": "CODE_2",
        "CODE3": "CODE_3",
    }
    return aliases.get(text, text)


def priority_weight(value: Any) -> float:
    return PRIORITY_WEIGHT.get(normalize_priority(value), 0.5)


def priority_sla(value: Any, default: float = 1200.0) -> float:
    return PRIORITY_SLA_SECONDS.get(normalize_priority(value), default)


def as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_action(record: Any) -> dict[str, Any]:
    """Normalize common CLI, Harbor, and JSONL action shapes.

    The returned object always has ``action`` and ``args`` keys while retaining
    the original payload under ``raw`` for audit evidence.
    """

    if not isinstance(record, Mapping):
        return {"action": str(record), "args": {}, "raw": record}
    raw = dict(record)
    nested = raw.get("command")
    sidecar_record = raw.get("record_type") == "action" and isinstance(nested, Mapping)
    if isinstance(nested, Mapping):
        name = nested.get("name") or nested.get("action") or nested.get("command")
        args = nested.get("args") or nested.get("arguments")
        # The sidecar action log stores the command name and its arguments in
        # one flat mapping (for example ``{"command": "dispatch",
        # "incident_id": "I1", "unit_id": "M1"}``).  Preserve those fields
        # when no explicit args envelope is present; otherwise an independent
        # verifier would replay every dispatch as a missing-argument action.
        if not isinstance(args, Mapping) or not args:
            args = {
                key: value
                for key, value in nested.items()
                if key not in {"action", "command", "name", "op", "type", "args", "arguments"}
            }
    else:
        name = raw.get("action") or raw.get("name") or raw.get("command") or raw.get("tool")
        args = raw.get("args") or raw.get("arguments") or {}
    if not isinstance(args, Mapping):
        args = {}
    # Some command logs put arguments beside the action name.
    if not args:
        args = {
            key: value
            for key, value in raw.items()
            if key not in {"action", "name", "command", "tool", "args", "arguments", "raw", "observation", "response"}
        }
    result = {"action": str(name or "unknown").strip().lower(), "args": dict(args), "raw": raw}
    for key in ("action_id", "state_version", "sim_time"):
        # In a sidecar record these top-level values describe the outcome
        # *after* the nested command.  Replaying them as submitted arguments
        # turns an otherwise valid auto-versioned command into a stale write.
        # The original command envelope is the sole authority here.
        if sidecar_record:
            continue
        if key in raw and key not in result["args"]:
            result["args"][key] = raw[key]
    return result


def normalize_actions(records: Iterable[Any]) -> list[dict[str, Any]]:
    return [normalize_action(record) for record in records]


def _semantic_action_commands(records: Iterable[Any]) -> list[dict[str, Any]]:
    """Project action records onto the commands that affect replay state.

    The authoritative sidecar wraps each submitted command with outcome and
    hash metadata.  Agent/ATIF logs commonly contain the same command as an
    ``action``/``args`` envelope.  Comparing raw records would reject those
    equivalent representations, while comparing only final state hashes
    would miss omitted rejected/no-op commands.  Normalize the command
    payload itself so the complete ordered command sequence can be bound
    without trusting or requiring a particular log envelope.
    """

    commands: list[dict[str, Any]] = []
    for record in records:
        payload = record
        if isinstance(record, Mapping) and isinstance(record.get("command"), Mapping):
            # Sidecar-only fields such as ``ok``, ``sim_time`` and state
            # hashes describe the outcome.  They are not command arguments.
            payload = record["command"]
        normalized = normalize_action(payload)
        commands.append({
            "action": normalized["action"],
            "args": normalized["args"],
        })
    return commands


def _task_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _simulator_module_candidates(task_root: Path | None = None) -> list[Path]:
    root = task_root or _task_root()
    return [
        root / "environment" / "simulator" / "simulator.py",
        root / "environment" / "simulator.py",
        root.parent.parent / "environment" / "simulator" / "simulator.py",
        # In Harbor's separate verifier, sidecar artifacts are copied back to
        # their original absolute paths.  Keep these candidates explicit so
        # the verifier can replay the private simulator without importing the
        # Agent image or relying on the task checkout layout.
        Path("/opt/sffd-sidecar/simulator/simulator.py"),
        Path("/opt/sffd-task/environment/simulator/simulator.py"),
        Path("/tests/simulator.py"),
    ]


def load_simulator_class(task_root: Path | None = None) -> Any | None:
    """Load Simulator without requiring the repository to be installed.

    Harbor mounts task directories differently across versions, so this uses
    explicit file candidates and then a normal import as a final fallback.
    """

    for path in _simulator_module_candidates(task_root):
        if not path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("sffd_task_simulator", path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            simulator = getattr(module, "Simulator", None)
            if simulator is not None:
                return simulator
        except Exception:
            # A verifier must still be able to report a useful structural
            # failure when optional simulator dependencies are absent.
            continue
    try:
        from environment.simulator.simulator import Simulator  # type: ignore

        return Simulator
    except Exception:
        return None


def instantiate_simulator(scenario_path: Path, state_dir: Path | None = None) -> Any | None:
    simulator_class = load_simulator_class(scenario_path.parent.parent if scenario_path.parent.name == "data" else None)
    if simulator_class is None:
        return None
    scenario = load_json(scenario_path)
    attempts = [
        lambda: simulator_class(scenario_path=str(scenario_path), state_dir=str(state_dir) if state_dir else None, hidden=True),
        lambda: simulator_class(scenario_path=str(scenario_path), hidden=True),
        lambda: simulator_class(scenario, state_dir=str(state_dir) if state_dir else None, hidden=True),
        lambda: simulator_class(scenario, hidden=True),
        lambda: simulator_class(scenario),
    ]
    for attempt in attempts:
        try:
            simulator = attempt()
            if hasattr(simulator, "reset"):
                simulator.reset()
            return simulator
        except (TypeError, ValueError, OSError):
            continue
    return None


def run_simulator_actions(simulator: Any, actions: Sequence[Mapping[str, Any]]) -> Any:
    """Run normalized actions against the simulator's narrow public API."""

    normalized = normalize_actions(actions)
    if hasattr(simulator, "run_actions"):
        try:
            return simulator.run_actions(normalized)
        except (TypeError, ValueError):
            try:
                return simulator.run_actions([a["raw"] for a in normalized])
            except Exception:
                pass
    outputs: list[Any] = []
    for action in normalized:
        name = action["action"]
        args = dict(action["args"])
        if name in {"observe", "get_state"}:
            method = getattr(simulator, "observe", None)
            result = method() if method else None
        elif name in {"advance", "advance_next", "tick"}:
            method = getattr(simulator, "step", None) or getattr(simulator, "advance", None)
            result = method({"action": "advance", **args}) if method else None
        else:
            method = getattr(simulator, name, None)
            if method is not None:
                try:
                    result = method(**args)
                except TypeError:
                    result = method(args)
            else:
                method = getattr(simulator, "step", None)
                result = method({"action": name, **args}) if method else None
        outputs.append(result)
    return {"outputs": outputs}


def get_simulator_trace(simulator: Any, run_result: Any = None) -> Any:
    if hasattr(simulator, "export_trace"):
        try:
            return simulator.export_trace()
        except Exception:
            pass
    return run_result if run_result is not None else {}


def get_simulator_hash(simulator: Any, trace: Any = None) -> str:
    if hasattr(simulator, "state_hash"):
        try:
            value = simulator.state_hash()
            if isinstance(value, str):
                return value
            return sha256_json(value)
        except Exception:
            pass
    return sha256_json(trace if trace is not None else {})


def _as_event_list(trace: Any) -> list[Mapping[str, Any]]:
    if isinstance(trace, list):
        return [event for event in trace if isinstance(event, Mapping)]
    if not isinstance(trace, Mapping):
        return []
    for key in ("events", "event_log", "transitions", "trace", "history"):
        value = trace.get(key)
        if isinstance(value, list):
            return [event for event in value if isinstance(event, Mapping)]
    return []


def _semantic_event(event: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the agent-safe/public part of a simulator transition.

    The simulator keeps private payloads beside a redacted ``public`` object.
    A verifier may receive either form (or a plain public event from a Harbor
    adapter), so normalize the shape without ever requiring private fields.
    """

    public = event.get("public")
    if isinstance(public, Mapping):
        merged = dict(event)
        merged.update(public)
        return merged
    return event


def _event_candidates(event: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Yield top-level, public, and private views for identity extraction."""

    candidates: list[Mapping[str, Any]] = [event]
    for key in ("public", "private"):
        value = event.get(key)
        if isinstance(value, Mapping):
            candidates.append(value)
    return candidates


def event_name(event: Mapping[str, Any]) -> str:
    value = first_value(_semantic_event(event), EVENT_NAME_KEYS, "")
    if not value:
        for candidate in _event_candidates(event):
            value = first_value(candidate, EVENT_NAME_KEYS, "")
            if value:
                break
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def event_incident(event: Mapping[str, Any]) -> str | None:
    for candidate in _event_candidates(event):
        value = first_value(candidate, INCIDENT_KEYS)
        if isinstance(value, Mapping):
            value = first_value(value, ("incident_id", "id"))
        if value is not None:
            return str(value)
    return None


def event_unit(event: Mapping[str, Any]) -> str | None:
    for candidate in _event_candidates(event):
        value = first_value(candidate, UNIT_KEYS)
        if isinstance(value, Mapping):
            value = first_value(value, ("unit_id", "id"))
        if value is not None:
            return str(value)
    return None


def event_time(event: Mapping[str, Any]) -> float | None:
    value = first_value(event, TIME_KEYS)
    if value is None:
        for candidate in _event_candidates(event)[1:]:
            value = first_value(candidate, TIME_KEYS)
            if value is not None:
                break
    if isinstance(value, str):
        # ISO timestamps are accepted for traces produced by data scripts.
        try:
            from datetime import datetime

            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return as_float(value)


def extract_incidents(scenario: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = scenario.get("incidents", [])
    if isinstance(values, Mapping):
        values = list(values.values())
    result: list[dict[str, Any]] = []
    for index, incident in enumerate(values if isinstance(values, list) else []):
        if not isinstance(incident, Mapping):
            continue
        item = dict(incident)
        item["incident_id"] = str(first_value(item, INCIDENT_KEYS, f"incident-{index}"))
        item["arrival_s"] = as_float(first_value(item, ("arrival_s", "arrival_time", "received_s", "t")), 0.0) or 0.0
        item["priority"] = normalize_priority(first_value(item, ("priority", "final_priority", "original_priority"), "UNKNOWN"))
        call_group = str(item.get("call_type_group") or "").lower()
        call_type = str(item.get("call_type") or "").lower()
        item["dispatchable"] = bool(item.get("dispatchable", call_group == "medical" or call_type.startswith("medical")))
        result.append(item)
    return result


def extract_units(scenario: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    values = scenario.get("units", [])
    if isinstance(values, Mapping):
        values = list(values.values())
    result: dict[str, dict[str, Any]] = {}
    for index, unit in enumerate(values if isinstance(values, list) else []):
        if not isinstance(unit, Mapping):
            continue
        item = dict(unit)
        item["unit_id"] = str(first_value(item, UNIT_KEYS, f"unit-{index}"))
        item["status"] = str(item.get("initial_status", item.get("status", "FREE"))).upper()
        result[item["unit_id"]] = item
    return result


def _walk_forbidden(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower()
            if key_text in FORBIDDEN_OBSERVATION_KEYS:
                found.append(f"{path}.{key}")
            found.extend(_walk_forbidden(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_walk_forbidden(child, f"{path}[{index}]"))
    return found


def hidden_state_violations(records: Iterable[Any]) -> list[str]:
    violations: list[str] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            continue
        # Inspect only observations/responses exposed to the agent. Raw event
        # logs may legitimately contain hidden fields for the verifier.
        for key in ("observation", "response", "result", "output"):
            if key in record:
                violations.extend(f"line {index + 1}: {path}" for path in _walk_forbidden(record[key], f"$.{key}"))
    return sorted(set(violations))


def _action_outcome(record: Any) -> bool | None:
    """Return an explicitly recorded action outcome, if one exists.

    Agent/ATIF trajectories commonly put the tool result below ``response``
    or ``result``, while the authoritative sidecar action log stores ``ok``
    at the top level.  Command-only records deliberately return ``None``:
    structural inference may flag such a command as invalid, but it is not
    evidence that the simulator actually committed an illegal transition.
    """

    def find(value: Any, *, depth: int = 0) -> bool | None:
        if not isinstance(value, Mapping) or depth > 3:
            return None
        for key in ("ok", "success"):
            outcome = value.get(key)
            if isinstance(outcome, bool):
                return outcome
        for key in ("response", "result", "output"):
            outcome = find(value.get(key), depth=depth + 1)
            if outcome is not None:
                return outcome
        return None

    return find(record)


def audit_actions(scenario: Mapping[str, Any], actions: Sequence[Any]) -> dict[str, Any]:
    """Perform independent structural/safety checks over an action log."""

    normalized = normalize_actions(actions)
    units = extract_units(scenario)
    incidents = {item["incident_id"]: item for item in extract_incidents(scenario)}
    active_assignments: dict[str, str] = {}
    uncertain_assignments: set[str] = set()
    seen_action_ids: set[str] = set()
    seen_action_fingerprints: dict[str, str] = {}
    invalid: list[dict[str, Any]] = []
    catastrophic: list[dict[str, Any]] = []
    previous_version: int | None = None

    def add_invalid(index: int, reason: str, action: Mapping[str, Any], catastrophic_flag: bool = False) -> None:
        item = {"index": index, "reason": reason, "action": action.get("raw", action)}
        invalid.append(item)
        if catastrophic_flag:
            catastrophic.append(item)

    def update_assignments_from_evidence(raw: Any) -> None:
        """Drop assignments proven to have been released by a tool result.

        A command-only log cannot reveal when a hidden release occurred, so we
        retain an assignment across ``advance`` unless the recorded response
        or observation explicitly shows the release.  Harbor/ATIF trajectories
        normally include the tool response, while this conservative behavior
        still catches an immediate double-booking in sparse logs.
        """

        if not isinstance(raw, Mapping):
            return
        # Walk response/event containers, but do not treat the raw command's
        # arguments as evidence of a state transition.
        containers: list[Any] = []
        for key in ("response", "result", "output", "observation", "events"):
            if key in raw:
                containers.append(raw[key])
        def walk(value: Any) -> None:
            if isinstance(value, Mapping):
                name = str(first_value(value, EVENT_NAME_KEYS, "")).lower().replace("-", "_").replace(" ", "_")
                uid = first_value(value, UNIT_KEYS)
                iid = first_value(value, INCIDENT_KEYS)
                if isinstance(uid, Mapping):
                    uid = first_value(uid, ("unit_id", "id"))
                if isinstance(iid, Mapping):
                    iid = first_value(iid, ("incident_id", "id"))
                if name in {"unit_available", "unit_released", "unit_release", "unit_repositioned", "reposition_complete"} and uid is not None:
                    active_assignments.pop(str(uid), None)
                if name in {"incident_completed", "incident_complete", "service_complete", "resolved", "closed", "close"}:
                    if uid is not None:
                        active_assignments.pop(str(uid), None)
                    if iid is not None:
                        for candidate, assigned in list(active_assignments.items()):
                            if assigned == str(iid):
                                active_assignments.pop(candidate, None)
                units_value = value.get("units")
                if isinstance(units_value, list):
                    for unit_value in units_value:
                        if not isinstance(unit_value, Mapping):
                            continue
                        unit_id = first_value(unit_value, UNIT_KEYS)
                        if unit_id is None:
                            continue
                        status = str(unit_value.get("status", "")).upper()
                        incident_value = first_value(unit_value, INCIDENT_KEYS)
                        if status in {"AVAILABLE", "FREE", "IDLE", "READY"}:
                            active_assignments.pop(str(unit_id), None)
                        elif incident_value is not None and status not in {"UNKNOWN", "HIDDEN"}:
                            active_assignments[str(unit_id)] = str(incident_value)
                for child in value.values():
                    if isinstance(child, (Mapping, list)):
                        walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        for container in containers:
            walk(container)

    for index, item in enumerate(normalized):
        action = item["action"]
        args = item["args"]
        raw = item["raw"]
        action_outcome = _action_outcome(raw)
        action_rejected = action_outcome is False
        action_accepted = action_outcome is True
        command_fingerprint = sha256_json({"action": action, "args": args})
        action_id = args.get("action_id") or raw.get("action_id") if isinstance(raw, Mapping) else None
        idempotent_replay = False
        if action_id is not None:
            action_id = str(action_id)
            if action_id in seen_action_ids:
                if seen_action_fingerprints.get(action_id) == command_fingerprint:
                    # The simulator deliberately returns the cached response
                    # for an identical retry.  It is not a double-booking and
                    # must not be scored as a catastrophic violation.
                    idempotent_replay = True
                else:
                    # Reusing an action ID for a different command is always
                    # invalid.  It is catastrophic only when authoritative
                    # outcome evidence says the conflicting transition was
                    # actually accepted.  A rejected attempt (or a sparse
                    # command-only record) leaves simulator state intact.
                    add_invalid(index, "duplicate_action_id", item, action_accepted)
            seen_action_ids.add(action_id)
            seen_action_fingerprints.setdefault(action_id, command_fingerprint)
        version = as_float(args.get("state_version"))
        if version is not None:
            version_int = int(version)
            if previous_version is not None and version_int < previous_version:
                add_invalid(index, "stale_state_version", item)
            previous_version = max(previous_version or version_int, version_int)

        if action_rejected:
            add_invalid(
                index,
                str(raw.get("error") or raw.get("error_code") or raw.get("code")
                    or "simulator_rejected_action") if isinstance(raw, Mapping)
                else "simulator_rejected_action",
                item,
            )

        if idempotent_replay:
            # Do not apply a cached dispatch/reassign twice to the independent
            # structural assignment model.
            update_assignments_from_evidence(raw)
            continue
        if action in {"dispatch", "assign"}:
            incident_id = str(first_value(args, ("incident_id", "incident", "call_id"), ""))
            unit_id = str(first_value(args, ("unit_id", "unit", "vehicle_id"), ""))
            if incident_id not in incidents:
                add_invalid(index, "unknown_incident", item)
            elif not incidents[incident_id].get("dispatchable", True):
                add_invalid(index, "incident_not_dispatchable", item)
            if unit_id not in units:
                add_invalid(index, "unknown_unit", item)
            elif unit_id in active_assignments:
                add_invalid(index, "unit_double_booked", item, action_accepted)
            if incident_id in active_assignments.values():
                add_invalid(index, "incident_already_assigned", item)
            if (not action_rejected and unit_id in units and incident_id in incidents
                    and unit_id not in active_assignments):
                uncertain_assignments.discard(unit_id)
                active_assignments[unit_id] = incident_id
        elif action in {"reassign", "transfer"}:
            from_unit = str(first_value(args, ("from_unit", "from", "source_unit", "unit_id"), ""))
            to_unit = str(first_value(args, ("to_unit", "to", "target_unit"), ""))
            incident_id = str(first_value(args, ("incident_id", "incident"), ""))
            if from_unit not in units or to_unit not in units:
                add_invalid(index, "unknown_reassign_unit", item)
            if from_unit not in active_assignments:
                add_invalid(index, "source_unit_not_assigned", item)
            elif incident_id and active_assignments[from_unit] != incident_id:
                add_invalid(index, "source_incident_mismatch", item)
            if to_unit in active_assignments:
                add_invalid(index, "target_unit_busy", item, action_accepted)
            if (not action_rejected and from_unit in active_assignments and to_unit in units
                    and to_unit not in active_assignments):
                assigned_incident = active_assignments.pop(from_unit)
                uncertain_assignments.discard(to_unit)
                active_assignments[to_unit] = incident_id or assigned_incident
        elif action in {"advance", "advance_next", "tick", "observe", "get_state", "finish", "reposition", "noop", "wait"}:
            has_evidence = isinstance(raw, Mapping) and any(
                key in raw for key in ("response", "result", "output", "observation", "events")
            )
            if action in {"advance", "advance_next", "tick"} and not has_evidence:
                # With a command-only log the hidden event queue is unknown;
                # after an advance an assignment may have released.  Preserve
                # immediate double-booking detection, but do not manufacture a
                # catastrophic finding for a later reuse that the simulator
                # will independently validate during replay.
                uncertain_assignments.update(active_assignments)
                active_assignments.clear()
        elif action in {"unknown", ""}:
            add_invalid(index, "unknown_action", item)
        else:
            # Keep forward compatibility with simulator extensions, but record
            # unsupported commands so they cannot improve the safety score.
            add_invalid(index, "unsupported_action", item)

        update_assignments_from_evidence(raw)

    hidden = hidden_state_violations(actions)
    return {
        "normalized_actions": normalized,
        "invalid_actions": invalid,
        "catastrophic_violations": catastrophic,
        "hidden_state_violations": hidden,
        "action_count": len(normalized),
        "unique_action_ids": len(seen_action_ids),
    }


def _event_outcomes(scenario: Mapping[str, Any], trace: Any) -> dict[str, dict[str, Any]]:
    incidents = {item["incident_id"]: {"incident": item} for item in extract_incidents(scenario)}
    # ``incident_arrival`` is an observation boundary, not a response.  Only
    # on-scene/arrival-after-dispatch events count toward response quality.
    response_names = {
        "on_scene", "onscene", "unit_on_scene", "arrived", "arrival_at_scene",
        "response_complete", "travel_complete",
    }
    completion_names = {
        "incident_completed", "incident_complete", "service_complete", "completed",
        "resolved", "closed", "close",
    }
    for event in _as_event_list(trace):
        incident_id = event_incident(event)
        if incident_id is None:
            continue
        incident_id = str(incident_id)
        row = incidents.setdefault(incident_id, {"incident": {"incident_id": incident_id, "arrival_s": 0.0, "priority": "UNKNOWN", "dispatchable": True}})
        name = event_name(event)
        time_value = event_time(event)
        if time_value is None:
            continue
        if name in response_names or name.endswith("_on_scene"):
            row.setdefault("arrival_times", []).append(time_value)
        if name in completion_names or name.endswith("_completed"):
            row.setdefault("completion_times", []).append(time_value)
    # Simulator implementations may expose a compact incident result map.
    if isinstance(trace, Mapping):
        compact = trace.get("incident_results") or trace.get("results")
        if isinstance(compact, Mapping):
            for incident_id, result in compact.items():
                if not isinstance(result, Mapping):
                    continue
                row = incidents.setdefault(str(incident_id), {"incident": {"incident_id": str(incident_id), "arrival_s": 0.0, "priority": "UNKNOWN", "dispatchable": True}})
                for key, target in (("response_time_s", "arrival_times"), ("on_scene_s", "arrival_times"), ("completion_time_s", "completion_times"), ("available_time_s", "completion_times")):
                    value = as_float(result.get(key))
                    if value is not None:
                        row.setdefault(target, []).append(value)
                if result.get("completed") is True:
                    row["completed"] = True
    return incidents


# ``compute_score`` receives the private replay object so that it can score
# authoritative outcomes.  The same result is later serialized as
# ``reward.json``; copying that object wholesale would accidentally put the
# simulator's private trace (latent durations, queue contents, and hidden
# incidents) in an agent-visible artifact.  Keep integrity diagnostics useful
# while recursively removing fields that are only for verifier internals.
_PRIVATE_INTEGRITY_KEYS = {
    "trace",
    "normalized_actions",
    "actions",
    "events",
    "transitions",
    "history",
    "raw",
    "action",
    "command",
    "args",
    "arguments",
    "observation",
    "notifications",
    "retries",
    "retry",
    "final_state",
    "private_state",
    "latent",
    "queue",
    "event_queue",
    "event_log",
    "state_hashes",
    "replay_manifest",
    "rng_manifest",
    "seed",
    "rng",
    "scenario",
    "scenario_id",
    "raw_scenario",
    "calibration",
    "hidden_reference_responses",
    "response_by_incident",
    # Violation rows contain the original command and retry identifiers.
    # Public diagnostics derive counts and stable reason categories before
    # this final recursive redaction pass, so the raw rows are never needed.
    "invalid_actions",
    "simulator_rejected_actions",
    "catastrophic_violations",
    "hidden_state_violations",
    # Per-action/event coordinates can become a future-event oracle when an
    # Agent probes the verifier repeatedly.  Keep aggregate counts and hashes,
    # but never serialize identifiers, logical times, or sequence positions.
    "id",
    "action_id",
    "event_id",
    "incident_id",
    "call_id",
    "unit_id",
    "vehicle_id",
    "resource_id",
    "tool_call_id",
    "trajectory_id",
    "incident",
    "unit",
    "vehicle",
    "resource",
    "sim_time",
    "time",
    "time_s",
    "timestamp",
    "at",
    "t",
    "arrival_s",
    "arrival_time",
    "dispatch_s",
    "dispatch_time",
    "response_s",
    "response_time_s",
    "on_scene_s",
    "completion_s",
    "completion_time_s",
    "sla_s",
    "state_version",
    "sequence",
    "index",
    "state_dir",
    "source_rowids",
    "source_rowid",
    # Divergence reports are useful for a path/line diagnosis, but the raw
    # values on either side may be private checkpoint/event payloads (release
    # times, latent samples, future incidents).  Keep only the JSON path and
    # length metadata in reward.json.
    "first",
    "second",
    # Simulator/import exceptions can contain absolute private paths or
    # scenario values.  The stable reason/type remains sufficient for public
    # diagnostics.
    "error",
} | FORBIDDEN_OBSERVATION_KEYS


def _public_integrity(value: Any) -> Any:
    """Return integrity metadata safe to include in reward.json.

    This is intentionally recursive: future replay implementations may nest
    a private trace below a diagnostic object rather than at the top level.
    Hashes, divergence paths, and structural error summaries remain intact.
    """

    if isinstance(value, Mapping):
        return {
            str(key): _public_integrity(child)
            for key, child in value.items()
            if str(key).lower() not in _PRIVATE_INTEGRITY_KEYS
        }
    if isinstance(value, list):
        return [_public_integrity(child) for child in value]
    if isinstance(value, tuple):
        return [_public_integrity(child) for child in value]
    return value


def compute_score(scenario: Mapping[str, Any], actions: Sequence[Any], trace: Any = None, integrity: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Compute the public reward vector from independently observed outcomes."""

    incident_rows = _event_outcomes(scenario, trace)
    dispatchable = [item for item in extract_incidents(scenario) if item.get("dispatchable", True)]
    if not dispatchable:
        dispatchable = [row["incident"] for row in incident_rows.values() if row.get("incident", {}).get("dispatchable", True)]
    total_weight = sum(priority_weight(item.get("priority")) for item in dispatchable) or 1.0
    response_sum = 0.0
    covered_weight = 0.0
    # ``timely_coverage`` is quality-weighted rather than a binary completion
    # count.  It gives zero credit to an unserved/late incident and partial
    # credit to a completed response that used part of its SLA.
    timely_coverage_sum = 0.0
    completed_weight = 0.0
    high_priority_missed = 0
    late_count = 0
    response_evidence: list[dict[str, Any]] = []

    for incident in dispatchable:
        incident_id = str(incident["incident_id"])
        row = incident_rows.get(incident_id, {})
        arrival_values = row.get("arrival_times", [])
        completion_values = row.get("completion_times", [])
        arrival_s = as_float(incident.get("arrival_s"), 0.0) or 0.0
        response_s = min(arrival_values) if arrival_values else None
        completion_s = min(completion_values) if completion_values else None
        weight = priority_weight(incident.get("priority"))
        sla = priority_sla(incident.get("priority"))
        if response_s is None:
            response_component = 0.0
            high_priority_missed += int(weight >= 0.75)
        else:
            delay = max(0.0, response_s - arrival_s)
            response_component = max(0.0, min(1.0, 1.0 - delay / sla))
            if delay > sla:
                late_count += 1
            covered_weight += weight
        if completion_s is not None or row.get("completed") is True:
            completed_weight += weight
            # A response that never reaches the scene cannot count as
            # coverage.  Completion without a response is retained in
            # ``completed_weight`` for strict-pass diagnostics, but contributes
            # no continuous coverage reward.
            timely_coverage_sum += weight * response_component
        response_sum += weight * (response_component ** RESPONSE_QUALITY_POWER)
        response_evidence.append({
            "incident_id": incident_id,
            "priority": incident.get("priority"),
            "arrival_s": arrival_s,
            "response_s": response_s,
            "completion_s": completion_s,
            "sla_s": sla,
            "response_component": round(response_component, 6),
            "response_quality": round(response_component ** RESPONSE_QUALITY_POWER, 6),
        })

    audit = audit_actions(scenario, actions)
    # A command-only trajectory may omit tool responses.  The independent
    # replay still knows which transitions the simulator rejected, so include
    # those failures in safety/strict-pass accounting instead of allowing a
    # sparse log to hide ordinary invalid writes.
    replay_invalid: list[dict[str, Any]] = []
    trace_audit: dict[str, Any] | None = None
    if isinstance(trace, Mapping):
        replay_actions_value = trace.get("actions")
        if isinstance(replay_actions_value, list):
            # The replay trace is authoritative evidence of whether a write
            # was accepted.  Audit it independently so an implementation bug
            # that actually commits a double-booking remains a hard failure,
            # while ordinary rejected attempts only reduce safety.
            trace_audit = audit_actions(scenario, replay_actions_value)
            for index, item in enumerate(replay_actions_value):
                if not isinstance(item, Mapping) or item.get("ok", True) is not False:
                    continue
                action_id = str(item.get("action_id", f"replay-{index}"))
                replay_invalid.append({
                    "index": index,
                    "reason": "simulator_rejected_action",
                    "action_id": action_id,
                    "action": item.get("command", item),
                })
    audited_invalid_ids = {
        str(item.get("action", {}).get("action_id"))
        for item in audit["invalid_actions"]
        if isinstance(item, Mapping) and isinstance(item.get("action"), Mapping)
        and item.get("action", {}).get("action_id") is not None
    }
    replay_only_invalid = [item for item in replay_invalid
                           if str(item.get("action_id")) not in audited_invalid_ids]
    invalid_evidence = list(audit["invalid_actions"]) + replay_only_invalid

    # Score safety over distinct operational attempts, not over every tool
    # call.  Otherwise an Agent can dilute one rejected write by appending an
    # arbitrary number of observe/advance/finish calls after the horizon.  An
    # explicit action_id identifies an idempotent attempt; records without one
    # remain distinct by log index.
    normalized_for_score = audit["normalized_actions"]

    def attempt_key(index: int, item: Mapping[str, Any]) -> tuple[str, str]:
        args = item.get("args")
        action_id = args.get("action_id") if isinstance(args, Mapping) else None
        raw = item.get("raw")
        if action_id is None and isinstance(raw, Mapping):
            action_id = raw.get("action_id")
        if action_id is not None and str(action_id):
            return ("action_id", str(action_id))
        return ("action_index", str(index))

    operational_names = {"dispatch", "assign", "reassign", "transfer", "reposition"}
    operational_attempts = {
        attempt_key(index, item)
        for index, item in enumerate(normalized_for_score)
        if item["action"] in operational_names
    }
    invalid_attempts: set[tuple[str, str]] = set()
    for item in audit["invalid_actions"]:
        index = item.get("index") if isinstance(item, Mapping) else None
        if isinstance(index, int) and 0 <= index < len(normalized_for_score):
            invalid_attempts.add(attempt_key(index, normalized_for_score[index]))
        else:
            invalid_attempts.add(("audit_evidence", sha256_json(item)))
    for index, item in enumerate(replay_only_invalid):
        action_id = item.get("action_id") if isinstance(item, Mapping) else None
        if action_id is not None and str(action_id):
            invalid_attempts.add(("action_id", str(action_id)))
        else:
            invalid_attempts.add(("replay_invalid", str(index)))

    invalid_attempt_count = len(invalid_attempts)
    safety_attempt_count = max(1, len(operational_attempts | invalid_attempts))
    catastrophic_evidence = list(audit["catastrophic_violations"])
    if trace_audit is not None:
        known_catastrophic = {
            sha256_json(item) for item in catastrophic_evidence
            if isinstance(item, Mapping)
        }
        for item in trace_audit["catastrophic_violations"]:
            fingerprint = sha256_json(item)
            if fingerprint not in known_catastrophic:
                catastrophic_evidence.append(item)
                known_catastrophic.add(fingerprint)
    catastrophic = bool(catastrophic_evidence)
    response = response_sum / total_weight
    coverage = timely_coverage_sum / total_weight
    # Safety, adaptation, and efficiency describe operating a live fleet.  A
    # no-op trace must not receive those points merely for being well-formed;
    # use the product of response quality and meaningful coverage as the gate.
    service_gate = max(0.0, min(1.0, response * coverage))
    base_safety = max(0.0, 1.0 - invalid_attempt_count / safety_attempt_count)
    safety = base_safety * service_gate
    if catastrophic:
        safety = 0.0

    dynamic_events = sum(1 for event in _as_event_list(trace) if any(token in event_name(event) for token in ("arrival", "new_incident", "priority_update", "failure", "road")))
    replan_attempts = {
        attempt_key(index, item)
        for index, item in enumerate(normalized_for_score)
        if item["action"] in {"reassign", "reposition"}
    }
    replans = len(replan_attempts)
    base_adaptation = 1.0 if dynamic_events == 0 else min(1.0, (replans + max(0, dynamic_events - high_priority_missed)) / dynamic_events)
    # Penalize unnecessary churn and excessive invalid writes while preserving
    # a usable score for agents that simply observe/advance.
    churn = len(replan_attempts)
    # Normalize churn by workload size and rejection cost by the distinct
    # operational-attempt set.  Neither denominator admits terminal control
    # calls, so padding cannot improve efficiency.
    churn_denominator = max(1, len(dispatchable))
    base_efficiency = max(
        0.0,
        1.0
        - max(0, churn - len(dispatchable)) / churn_denominator
        - invalid_attempt_count / (2.0 * safety_attempt_count),
    )
    adaptation = base_adaptation * service_gate
    efficiency = base_efficiency * service_gate

    integrity_score = 1.0
    integrity_evidence: dict[str, Any] = {}
    if integrity is not None:
        integrity_score = 1.0 if integrity.get("ok", False) else 0.0
        # The verifier needs the full object above, but only the redacted
        # diagnostics may cross the verifier/agent boundary.
        redacted_integrity = _public_integrity(integrity)
        integrity_evidence = redacted_integrity if isinstance(redacted_integrity, dict) else {}
    if audit["hidden_state_violations"]:
        integrity_score = 0.0
    if not trace:
        integrity_score = 0.0 if integrity is None else integrity_score

    # Integrity failures and catastrophic state violations are hard failures;
    # ordinary rejected actions remain continuous deductions above.
    hard_failure = bool(catastrophic or audit["hidden_state_violations"] or
                        (integrity is not None and not integrity.get("ok", False)))
    if hard_failure:
        integrity_score = 0.0

    components = {
        "response": round(max(0.0, min(1.0, response)), 6),
        "coverage": round(max(0.0, min(1.0, coverage)), 6),
        "safety": round(max(0.0, min(1.0, safety)), 6),
        "adaptation": round(max(0.0, min(1.0, adaptation)), 6),
        "efficiency": round(max(0.0, min(1.0, efficiency)), 6),
        "integrity": round(max(0.0, min(1.0, integrity_score)), 6),
    }
    overall = sum(WEIGHTS[key] * components[key] for key in WEIGHTS)
    if hard_failure:
        # A tampered/hidden-state trajectory is not a low-quality operational
        # policy; it is an invalid submission and must not retain partial
        # response or coverage credit.
        overall = 0.0
    strict_pass = int(
        bool(dispatchable)
        and not catastrophic
        and not invalid_evidence
        and high_priority_missed == 0
        and late_count == 0
        and completed_weight >= total_weight
        and components["integrity"] >= 1.0
    )
    return {
        **components,
        "overall": round(max(0.0, min(1.0, overall)), 6),
        "strict_pass": strict_pass,
        "evidence": {
            "incident_count": len(dispatchable),
            "covered_weight": round(covered_weight, 6),
            "timely_coverage_weight": round(timely_coverage_sum, 6),
            "completed_weight": round(completed_weight, 6),
            "total_priority_weight": round(total_weight, 6),
            "service_gate": round(service_gate, 6),
            "response_quality_power": RESPONSE_QUALITY_POWER,
            "scored_operational_attempts": len(operational_attempts),
            "scored_invalid_attempts": invalid_attempt_count,
            "scoring_attempt_denominator": safety_attempt_count,
            "late_count": late_count,
            "high_priority_missed": high_priority_missed,
            "response_by_incident": response_evidence,
            "invalid_actions": invalid_evidence,
            "simulator_rejected_actions": replay_only_invalid,
            "catastrophic_violations": catastrophic_evidence,
            "hidden_state_violations": audit["hidden_state_violations"],
            "integrity": integrity_evidence,
            "hard_failure": hard_failure,
        },
    }


def replay_actions(
    scenario_path: Path,
    actions: Sequence[Any],
    state_dir: Path | None = None,
    *,
    include_trace: bool = False,
) -> dict[str, Any]:
    """Replay actions twice and return hashes plus first divergence evidence.

    The verifier needs the authoritative private trace while calculating a
    reward, but that trace must not be copied into the agent-facing
    ``reward.json``/replay report.  ``include_trace`` is therefore explicit
    and opt-in; callers that only need integrity metadata keep the default
    redacted result.
    """

    normalized = normalize_actions(actions)
    # Audit the original records, not their normalized envelopes.  The raw
    # top-level/nested tool response contains the accepted/rejected outcome
    # and any observation leak evidence; normalizing twice obscures both.
    structural = audit_actions(load_json(scenario_path), actions)
    if structural["hidden_state_violations"]:
        return {
            "ok": False,
            "simulator_available": False,
            "reason": "hidden_state_leak",
            "structural": structural,
        }
    simulator_class = load_simulator_class(scenario_path.parent.parent if scenario_path.parent.name == "data" else None)
    if simulator_class is None:
        trace = {"actions": [item["raw"] for item in normalized]}
        digest = sha256_json(trace)
        result = {
            # A structural hash is useful for local diagnostics, but it is not
            # an authoritative replay.  Treating it as ``ok`` would award the
            # integrity component when the simulator/Harbor sidecar is absent,
            # contrary to the task's hard-failure rule for non-replayable logs.
            "ok": False,
            "simulator_available": False,
            "state_hash": digest,
            "trace_hash": digest,
            "action_hash": sha256_json([item["raw"] for item in normalized]),
            "structural": structural,
            "fallback": "structural deterministic hash; install simulator/Harbor for full replay",
        }
        if include_trace:
            result["trace"] = trace
        return result

    def run_once(suffix: str, replay_root: Path | None = None) -> tuple[str, Any]:
        local_state = replay_root / suffix if replay_root else None
        if local_state:
            local_state.mkdir(parents=True, exist_ok=True)
        sim = instantiate_simulator(scenario_path, local_state)
        if sim is None:
            raise RuntimeError("Simulator could not be instantiated")
        result = run_simulator_actions(sim, normalized)
        trace = get_simulator_trace(sim, result)
        return get_simulator_hash(sim, trace), trace

    replay_checks: dict[str, Any] | None = None
    try:
        if state_dir:
            # Sidecar artifacts are authoritative input and may be mounted
            # read-only by Harbor.  Independent replay state belongs in a
            # verifier-owned temporary workspace, never below that source
            # directory.
            with tempfile.TemporaryDirectory(prefix="sffd-verifier-replay-") as directory:
                replay_root = Path(directory)
                hash_a, trace_a = run_once("replay-a", replay_root)
                hash_b, trace_b = run_once("replay-b", replay_root)
                scenario_value = load_json(scenario_path)
                replay_checks = {
                    "replay_a": verify_state_dir(replay_root / "replay-a", scenario=scenario_value),
                    "replay_b": verify_state_dir(replay_root / "replay-b", scenario=scenario_value),
                }
        else:
            hash_a, trace_a = run_once("replay-a")
            hash_b, trace_b = run_once("replay-b")
    except Exception as exc:
        return {
            "ok": False,
            "simulator_available": True,
            "reason": f"simulator_error:{type(exc).__name__}",
            "error": str(exc),
            "structural": structural,
        }
    trace_a_canonical = canonical_json(trace_a)
    trace_b_canonical = canonical_json(trace_b)
    divergence = None if trace_a_canonical == trace_b_canonical else first_trace_divergence(trace_a, trace_b)
    replay_state_invariant_ok = True
    if isinstance(trace_a, Mapping) and isinstance(trace_a.get("actions"), list):
        replay_trace_audit = audit_actions(load_json(scenario_path), trace_a["actions"])
        replay_state_invariant_ok = not replay_trace_audit["catastrophic_violations"]
    log_integrity: dict[str, Any] | None = None
    logs_ok = True
    if state_dir:
        scenario_value = load_json(scenario_path)
        # When Harbor transfers the simulator sidecar's append-only directory,
        # audit that evidence before considering the independently replayed
        # trajectory valid.  The replay-a/replay-b directories below are fresh
        # verifier-owned outputs; the source check is what binds the score to
        # the sidecar's recorded causal history and detects truncation or
        # edits made during artifact collection.
        source_check = verify_state_dir(state_dir, scenario=scenario_value)
        checks = replay_checks or {}
        scored_commands = _semantic_action_commands(actions)
        source_commands: list[dict[str, Any]] = []
        source_actions_error: str | None = None
        try:
            source_commands = _semantic_action_commands(load_jsonl(state_dir / "actions.jsonl"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            source_actions_error = f"{type(exc).__name__}:{exc}"
        action_divergence = first_trace_divergence(scored_commands, source_commands)
        action_binding = {
            "ok": source_actions_error is None and action_divergence is None,
            "scored_action_count": len(scored_commands),
            "source_action_count": len(source_commands),
            "scored_command_hash": sha256_json(scored_commands),
            "source_command_hash": sha256_json(source_commands),
            "first_divergence": action_divergence,
        }
        if source_actions_error is not None:
            action_binding["error"] = source_actions_error

        source_final_state_hash: Any = None
        source_manifest_error: str | None = None
        try:
            source_manifest = load_json(state_dir / "replay_manifest.json")
            if isinstance(source_manifest, Mapping):
                source_final_state_hash = source_manifest.get("final_state_hash")
            else:
                source_manifest_error = "replay_manifest_not_object"
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            source_manifest_error = f"{type(exc).__name__}:{exc}"
        final_state_binding = {
            "ok": (
                source_manifest_error is None
                and isinstance(source_final_state_hash, str)
                and bool(source_final_state_hash)
                and hash_a == source_final_state_hash
                and hash_b == source_final_state_hash
            ),
            "source_final_state_hash": source_final_state_hash,
            "replay_final_state_hash": hash_a,
            "replay_repeat_final_state_hash": hash_b,
        }
        if source_manifest_error is not None:
            final_state_binding["error"] = source_manifest_error

        logs_ok = (
            source_check.get("ok", False)
            and all(item.get("ok", False) for item in checks.values())
            and action_binding["ok"]
            and final_state_binding["ok"]
        )
        log_integrity = {
            "source": source_check,
            "action_binding": action_binding,
            "final_state_binding": final_state_binding,
            **checks,
        }
    result = {
        "ok": (hash_a == hash_b and divergence is None and logs_ok
               and not structural["catastrophic_violations"]
               and replay_state_invariant_ok),
        "simulator_available": True,
        "replay_state_invariant_ok": replay_state_invariant_ok,
        "state_hash": hash_a,
        "state_hash_repeat": hash_b,
        "trace_hash": hashlib.sha256(trace_a_canonical.encode()).hexdigest(),
        "trace_hash_repeat": hashlib.sha256(trace_b_canonical.encode()).hexdigest(),
        "action_hash": sha256_json([item["raw"] for item in normalized]),
        "first_divergence": divergence,
        "log_integrity": log_integrity,
        "structural": structural,
    }
    if include_trace:
        # Keep the first replay as the authoritative outcome trace.  The
        # second replay is compared above and is represented by its hash only.
        result["trace"] = trace_a
    return result


def first_trace_divergence(first: Any, second: Any) -> dict[str, Any] | None:
    """Return the first JSON path where two traces differ."""

    def walk(a: Any, b: Any, path: str) -> dict[str, Any] | None:
        if type(a) is not type(b):
            return {"path": path, "first": a, "second": b}
        if isinstance(a, Mapping):
            keys = sorted(set(a) | set(b), key=str)
            for key in keys:
                if key not in a or key not in b:
                    return {"path": f"{path}.{key}", "first": a.get(key), "second": b.get(key)}
                result = walk(a[key], b[key], f"{path}.{key}")
                if result:
                    return result
            return None
        if isinstance(a, list):
            if len(a) != len(b):
                return {"path": path, "first_length": len(a), "second_length": len(b)}
            for index, (left, right) in enumerate(zip(a, b)):
                result = walk(left, right, f"{path}[{index}]")
                if result:
                    return result
            return None
        return None if a == b else {"path": path, "first": a, "second": b}

    return walk(first, second, "$")
