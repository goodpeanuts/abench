"""Deterministic, discrete-event EMS simulator for the SFFD Harbor task.

The simulator deliberately keeps the public observation separate from the
latent world.  Incident arrivals and background events are driven by a frozen
scenario, while travel/service/release durations are resolved from the
scenario's calibration section (or per-event latent values) using a stable
SHA-256 based sampler.  No third party packages are required.

The module is usable both as a library and through ``emsctl``::

    sim = Simulator("scenario.json")
    sim.reset()
    sim.step({"command": "dispatch", "unit_id": "M1",
              "incident_id": "I1", "action_id": "a1"})

``export_trace(include_private=True)`` is intended for the verifier.  The CLI
never returns the private trace, latent durations, or future event queue.
"""

from __future__ import annotations

import copy
import datetime as _datetime
import hashlib
import heapq
import json
import math
import os
import pathlib
import re
import stat
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple


ENGINE_VERSION = "sffd-ems-simulator/0.1.0"
RNG_ALGORITHM = "sha256-stable-v1"
CONTROLLED_UNIT_TYPES = {"MEDIC", "BLS", "PRIVATE"}

# Lower values are processed first when multiple events share a timestamp.
EVENT_PRIORITY = {
    "incident_arrival": 10,
    "background_arrival": 12,
    "background_assign": 14,
    "unit_release": 20,
    "travel_complete": 30,
    "service_complete": 40,
    "reposition_complete": 50,
    "horizon": 90,
}


class ScenarioError(ValueError):
    """Raised when a scenario is malformed or lacks required calibration."""


class ActionError(ValueError):
    """A rejected action.  The simulator converts this to a JSON error result."""


def _canonical(value: Any) -> Any:
    """Return a JSON-stable representation for hashes and action identity."""

    if isinstance(value, Mapping):
        return {str(k): _canonical(value[k]) for k in sorted(value, key=lambda x: str(x))}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite number in state")
        # Avoid platform-dependent negative zero in hashes.
        return 0.0 if value == 0 else value
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(_canonical(value), ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _first(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "als"}


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ScenarioError(f"expected numeric value, got {value!r}") from exc
    if not math.isfinite(result):
        raise ScenarioError(f"expected finite number, got {value!r}")
    return result


def _norm_type(value: Any) -> str:
    return str(value or "UNKNOWN").strip().upper().replace("-", "_")


def _valid_zone_label(value: str) -> bool:
    """Validate a coarse public zone label without consulting hidden zones.

    Membership checks against the scenario's candidate incidents would turn a
    reposition error into a future-location oracle.  The syntax check keeps
    malformed input out while allowing a legitimate coarse label to be used
    before an incident in that area is revealed.
    """

    return bool(re.fullmatch(r"[A-Za-z0-9:_-]{1,64}", value))


def _norm_priority(value: Any) -> str:
    # Keep historical codes intact.  Ordering is handled by priority_rank.
    return str(value if value is not None else "UNKNOWN").strip().upper()


def priority_rank(value: Any) -> int:
    """Map common SFFD/EMS priority labels to a lower-is-more-urgent rank."""

    label = _norm_priority(value)
    ranks = {
        "1": 1, "P1": 1, "E": 1, "A": 1,
        "2": 2, "P2": 2, "B": 2,
        "3": 3, "P3": 3, "C": 3,
        "4": 4, "P4": 4, "D": 4,
        "I": 3, "T": 3,
    }
    return ranks.get(label, 9)


def _stable_uniform(seed: Any, key: str) -> float:
    digest = hashlib.sha256(f"{seed}|{key}".encode("utf-8")).digest()
    # 53 bits gives a reproducible double in [0, 1).
    integer = int.from_bytes(digest[:8], "big") >> 11
    return integer / float(1 << 53)


def _stable_int(seed: Any, key: str, upper: int) -> int:
    if upper <= 1:
        return 0
    digest = hashlib.sha256(f"{seed}|{key}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % upper


@dataclass(order=True)
class Event:
    """Internal event queue item."""

    sort_key: Tuple[float, int, int] = field(init=False, repr=False)
    time_s: float
    priority: int
    seq: int
    kind: str
    payload: Dict[str, Any] = field(default_factory=dict, compare=False)
    token: Optional[str] = field(default=None, compare=False)

    def __post_init__(self) -> None:
        self.sort_key = (float(self.time_s), int(self.priority), int(self.seq))

    def public(self) -> Dict[str, Any]:
        return {
            "time_s": self.time_s,
            "priority": self.priority,
            "sequence": self.seq,
            "kind": self.kind,
            # Payload is deliberately not exposed to the agent by observe().
        }

    def private(self) -> Dict[str, Any]:
        return {
            "time_s": self.time_s,
            "priority": self.priority,
            "sequence": self.seq,
            "kind": self.kind,
            "payload": copy.deepcopy(self.payload),
            "token": self.token,
        }


@dataclass
class Unit:
    unit_id: str
    unit_type: str
    als: bool
    zone: str
    status: str = "AVAILABLE"
    incident_id: Optional[str] = None
    busy_until_s: Optional[float] = None
    assignment_token: int = 0
    initial_zone: Optional[str] = None
    background: bool = False

    @property
    def controllable(self) -> bool:
        return self.unit_type in CONTROLLED_UNIT_TYPES and not self.background

    def public(self) -> Dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "unit_type": self.unit_type,
            "als": self.als,
            "zone": self.zone,
            "status": self.status,
            "incident_id": self.incident_id,
            "controllable": self.controllable,
        }

    def private(self) -> Dict[str, Any]:
        data = self.public()
        data.update({
            "busy_until_s": self.busy_until_s,
            "assignment_token": self.assignment_token,
            "initial_zone": self.initial_zone,
            "background": self.background,
        })
        return data


@dataclass
class Incident:
    incident_id: str
    arrival_s: float
    call_type: str
    priority: str
    requires_als: bool
    zone: str
    dispatchable: bool
    source_rowids: List[Any] = field(default_factory=list)
    status: str = "HIDDEN"
    assigned_unit_id: Optional[str] = None
    discovered_s: Optional[float] = None
    dispatch_s: Optional[float] = None
    on_scene_s: Optional[float] = None
    completion_s: Optional[float] = None
    response_time_s: Optional[float] = None
    # Raw latent values are private and never copied into public observations.
    latent: Dict[str, Any] = field(default_factory=dict, repr=False)
    background: bool = False

    def public(self) -> Dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "arrival_s": self.arrival_s,
            "call_type": self.call_type,
            "priority": self.priority,
            "priority_rank": priority_rank(self.priority),
            "requires_als": self.requires_als,
            "zone": self.zone,
            "dispatchable": self.dispatchable,
            "status": self.status,
            "assigned_unit_id": self.assigned_unit_id,
            "discovered_s": self.discovered_s,
            "dispatch_s": self.dispatch_s,
            "on_scene_s": self.on_scene_s,
            "completion_s": self.completion_s,
            "response_time_s": self.response_time_s,
            "background": self.background,
        }

    def private(self) -> Dict[str, Any]:
        data = self.public()
        data.update({"source_rowids": copy.deepcopy(self.source_rowids),
                     "latent": copy.deepcopy(self.latent)})
        return data


class Simulator:
    """A deterministic EMS environment.

    Parameters
    ----------
    scenario:
        A path to ``scenario.json`` or an already-loaded mapping.
    state_dir:
        Optional directory for checkpoint, append-only logs and replay
        manifest.  The CLI uses this to persist state across invocations.
    load_existing:
        Restore a checkpoint from ``state_dir`` when present.
    """

    def __init__(self, scenario: Any, state_dir: Optional[os.PathLike[str] | str] = None,
                 *, load_existing: bool = False, hidden: bool = True) -> None:
        self.scenario_path: Optional[str] = None
        if isinstance(scenario, (str, os.PathLike)):
            self.scenario_path = os.fspath(scenario)
            with open(self.scenario_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        elif isinstance(scenario, Mapping):
            raw = copy.deepcopy(dict(scenario))
        else:
            raise TypeError("scenario must be a path or mapping")
        if not isinstance(raw, Mapping):
            raise ScenarioError("scenario root must be a JSON object")
        self.raw_scenario: Dict[str, Any] = copy.deepcopy(dict(raw))
        self.scenario = self._normalise_scenario(self.raw_scenario)
        self._validate_scenario()
        self.hidden = bool(hidden)
        self.state_dir = pathlib.Path(state_dir) if state_dir else None
        self._reset_runtime()
        if load_existing and self.state_dir and (self.state_dir / "checkpoint.json").exists():
            self._load_checkpoint()
        else:
            self.reset()

    # ------------------------------------------------------------------
    # Scenario parsing
    # ------------------------------------------------------------------
    def _normalise_scenario(self, raw: Mapping[str, Any]) -> Dict[str, Any]:
        clock = raw.get("clock") if isinstance(raw.get("clock"), Mapping) else {}
        t0 = _first(clock, "t0", "start", "start_time", default=raw.get("t0", 0))
        horizon = _as_float(_first(clock, "horizon_seconds", "horizon_s", "horizon",
                                    default=raw.get("horizon_seconds", 7200)), 7200.0)
        if horizon is None or horizon <= 0:
            raise ScenarioError("clock.horizon_seconds must be positive")

        zones_raw = raw.get("zones", [])
        if isinstance(zones_raw, Mapping):
            zones = [str(k) for k in zones_raw.keys()]
        else:
            zones = []
            for zone in zones_raw or []:
                if isinstance(zone, Mapping):
                    zones.append(str(_first(zone, "zone_id", "id", "name", default="UNKNOWN")))
                else:
                    zones.append(str(zone))

        units_raw = raw.get("units", raw.get("roster", []))
        if isinstance(units_raw, Mapping):
            units_raw = [dict(v, unit_id=k) if isinstance(v, Mapping) else {"unit_id": k, "unit_type": v}
                         for k, v in units_raw.items()]
        units: List[Dict[str, Any]] = []
        for index, value in enumerate(units_raw or []):
            if not isinstance(value, Mapping):
                raise ScenarioError(f"unit {index} must be an object")
            uid = str(_first(value, "unit_id", "id", "unit", default=f"U{index + 1}"))
            unit_type = _norm_type(_first(value, "unit_type", "type", "kind", default="UNKNOWN"))
            zone = str(_first(value, "initial_zone", "zone", "station_area", "battalion", default="UNKNOWN"))
            status = _norm_type(_first(value, "initial_status", "status", default="AVAILABLE"))
            if status in {"FREE", "IDLE", "READY"}:
                status = "AVAILABLE"
            busy_until = _as_float(_first(value, "busy_until_s", "initial_busy_until_s", "release_s"))
            units.append({
                "unit_id": uid,
                "unit_type": unit_type,
                "als": _as_bool(_first(value, "als", "is_als"), unit_type == "MEDIC"),
                "initial_zone": zone,
                "initial_status": status,
                "busy_until_s": busy_until,
                "background": _as_bool(value.get("background"), False),
            })
        if not units:
            raise ScenarioError("scenario must contain at least one unit")

        incidents_raw = raw.get("incidents", raw.get("events", []))
        if isinstance(incidents_raw, Mapping):
            incidents_raw = [dict(v, incident_id=k) if isinstance(v, Mapping) else {"incident_id": k}
                             for k, v in incidents_raw.items()]
        incidents: List[Dict[str, Any]] = []
        for index, value in enumerate(incidents_raw or []):
            if not isinstance(value, Mapping):
                raise ScenarioError(f"incident {index} must be an object")
            iid = str(_first(value, "incident_id", "id", "call_id", default=f"I{index + 1}"))
            arrival = _as_float(_first(value, "arrival_s", "arrival", "offset_s", "time_s", default=0), 0.0)
            if arrival is None or arrival < 0:
                raise ScenarioError(f"incident {iid} has invalid arrival_s")
            call_type = str(_first(value, "call_type", "type", "incident_type", default="UNKNOWN"))
            priority = _norm_priority(_first(value, "priority", "initial_priority", "original_priority", default="3"))
            dispatchable_default = "MEDICAL" in call_type.upper() or "EMS" in call_type.upper()
            dispatchable = _as_bool(value.get("dispatchable"), dispatchable_default)
            zone = str(_first(value, "zone", "battalion", "station_area", "location_zone", default="UNKNOWN"))
            latent = _first(value, "latent", "dynamics", "hidden", default={})
            if not isinstance(latent, Mapping):
                latent = {}
            incidents.append({
                "incident_id": iid,
                "arrival_s": arrival,
                "call_type": call_type,
                "priority": priority,
                "requires_als": _as_bool(_first(value, "requires_als", "als_required", "als"),
                                          priority_rank(priority) == 1),
                "zone": zone,
                "dispatchable": dispatchable,
                "source_rowids": list(_first(value, "source_rowids", "rowids", default=[] ) or []),
                "latent": copy.deepcopy(dict(latent)),
                "background": _as_bool(value.get("background"), not dispatchable),
            })

        background_raw = raw.get("background_events", raw.get("background", []))
        if isinstance(background_raw, Mapping):
            background_raw = list(background_raw.values())
        background: List[Dict[str, Any]] = []
        for index, value in enumerate(background_raw or []):
            if not isinstance(value, Mapping):
                raise ScenarioError(f"background event {index} must be an object")
            item = copy.deepcopy(dict(value))
            item["event_id"] = str(_first(item, "event_id", "id", default=f"B{index + 1}"))
            item["arrival_s"] = _as_float(_first(item, "arrival_s", "arrival", "time_s", default=0), 0.0)
            item["kind"] = str(_first(item, "kind", "type", "event_type", default="background_arrival"))
            background.append(item)

        calibration = raw.get("calibration", raw.get("dynamics", {}))
        if not isinstance(calibration, Mapping):
            calibration = {}
        return {
            "schema_version": str(raw.get("schema_version", "1")),
            "scenario_id": str(raw.get("scenario_id", raw.get("id", "sffd-scenario"))),
            "seed": raw.get("seed", 0),
            "clock": {"t0": t0, "horizon_seconds": horizon},
            "zones": zones,
            "units": units,
            "incidents": incidents,
            "background_events": background,
            "calibration": copy.deepcopy(dict(calibration)),
            "metadata": copy.deepcopy(raw.get("metadata", {})) if isinstance(raw.get("metadata", {}), Mapping) else {},
        }

    def _validate_scenario(self) -> None:
        """Reject malformed/under-calibrated SFFD scenarios early.

        Generic unit-test fixtures are intentionally allowed to omit empirical
        calibration.  A generated SFFD scenario, however, must carry explicit
        distributions so missing data can never silently become a guessed
        constant.
        """

        unit_ids = [unit["unit_id"] for unit in self.scenario["units"]]
        if len(set(unit_ids)) != len(unit_ids):
            raise ScenarioError("scenario contains duplicate unit_id values")
        incident_ids = [incident["incident_id"] for incident in self.scenario["incidents"]]
        if len(set(incident_ids)) != len(incident_ids):
            raise ScenarioError("scenario contains duplicate incident_id values")
        if len(set(self.scenario["zones"])) != len(self.scenario["zones"]):
            raise ScenarioError("scenario contains duplicate zones")

        schema = str(self.scenario.get("schema_version", ""))
        if not schema.startswith("sffd-ems-scenario"):
            return
        if "seed" not in self.raw_scenario:
            raise ScenarioError("SFFD scenario is missing its frozen seed")
        seed = self.raw_scenario.get("seed")
        if isinstance(seed, (Mapping, list, tuple, bool)) or seed is None:
            raise ScenarioError("SFFD scenario seed must be a scalar value")
        calibration = self.scenario.get("calibration")
        if not isinstance(calibration, Mapping):
            raise ScenarioError("SFFD scenario is missing calibration")
        required = ("travel_time_s", "service_duration_s", "release_delay_s")
        for name in required:
            value = calibration.get(name)
            if not isinstance(value, Mapping) or not value:
                raise ScenarioError(f"SFFD scenario calibration {name!r} is empty")
            # A non-empty mapping can still be unusable, for example
            # ``{"default": []}`` or ``{"MEDIC": {}}``.  Reject it at
            # construction time instead of allowing the first dispatch to
            # fail halfway through a transition.
            if not self._contains_numeric_distribution(value):
                raise ScenarioError(
                    f"SFFD scenario calibration {name!r} has no usable numeric distribution"
                )
        counts = calibration.get("sample_counts")
        if not isinstance(counts, Mapping):
            raise ScenarioError("SFFD scenario calibration is missing sample_counts")
        for phase, key in (("travel", "travel_observations"),
                           ("service", "service_observations"),
                           ("release", "release_observations")):
            try:
                count = int(counts.get(key, 0))
            except (TypeError, ValueError):
                count = 0
            if count <= 0:
                raise ScenarioError(f"SFFD calibration has no {phase} observations")
        for item in self.scenario["background_events"]:
            if not isinstance(item, Mapping):
                raise ScenarioError("background event must be an object")
            if _as_float(item.get("arrival_s")) is None:
                raise ScenarioError("background event is missing arrival_s")
            if (_first(item, "unit_id", "unit") is not None
                    and _as_float(_first(item, "release_s", "release_at_s")) is None
                    and "background_duration_s" not in calibration):
                raise ScenarioError(
                    f"background event {item.get('event_id', '')!r} has no release calibration"
                )

    @classmethod
    def _contains_numeric_distribution(cls, value: Any) -> bool:
        """Return whether a nested calibration tree contains finite data."""

        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return math.isfinite(float(value)) and float(value) >= 0
        if isinstance(value, str):
            try:
                number = float(value)
            except ValueError:
                return False
            return math.isfinite(number) and number >= 0
        if isinstance(value, Mapping):
            return any(cls._contains_numeric_distribution(child) for child in value.values())
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return any(cls._contains_numeric_distribution(child) for child in value)
        return False

    # ------------------------------------------------------------------
    # Runtime and persistence
    # ------------------------------------------------------------------
    def _reset_runtime(self) -> None:
        self.sim_time = 0.0
        self.state_version = 0
        self.done = False
        self.finished = False
        self.finish_reason: Optional[str] = None
        self.units: Dict[str, Unit] = {}
        self.incidents: Dict[str, Incident] = {}
        self._queue: List[Event] = []
        self._next_sequence = 0
        self._action_cache: Dict[str, Dict[str, Any]] = {}
        self._actions: List[Dict[str, Any]] = []
        # ``_transition_events`` is the per-action notification buffer.  Keep
        # a separate append-only log for replay and independent scoring; the
        # public adapter must not lose earlier transitions when the next
        # command arrives.
        self._transition_events: List[Dict[str, Any]] = []
        self._event_log: List[Dict[str, Any]] = []
        self._state_hashes: List[Dict[str, Any]] = []
        self._initial_hash = ""
        self._last_response: Optional[Dict[str, Any]] = None
        self._metrics: Dict[str, Any] = {
            "invalid_actions": 0,
            "dispatch_actions": 0,
            "reassign_actions": 0,
            "reposition_actions": 0,
            "churn_count": 0,
            "events_processed": 0,
            "response_times_s": {},
            "completion_times_s": {},
            "action_count": 0,
        }

    def _ensure_state_dir(self) -> None:
        if not self.state_dir:
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, stat.S_IRWXU)
        except OSError:
            pass

    def reset(self) -> Dict[str, Any]:
        """Reset to the scenario initial state and schedule deterministic events."""
        self._reset_runtime()
        for item in self.scenario["units"]:
            unit = Unit(
                unit_id=item["unit_id"], unit_type=item["unit_type"], als=item["als"],
                zone=item["initial_zone"], initial_zone=item["initial_zone"],
                status=item["initial_status"], busy_until_s=item["busy_until_s"],
                background=item["background"],
            )
            if unit.status not in {"AVAILABLE", "BUSY", "EN_ROUTE", "ON_SCENE", "SERVICING", "REPOSITIONING"}:
                unit.status = "AVAILABLE"
            self.units[unit.unit_id] = unit

        for item in self.scenario["incidents"]:
            incident = Incident(
                incident_id=item["incident_id"], arrival_s=item["arrival_s"],
                call_type=item["call_type"], priority=item["priority"],
                requires_als=item["requires_als"], zone=item["zone"],
                dispatchable=item["dispatchable"], source_rowids=item["source_rowids"],
                latent=item["latent"], background=item["background"],
            )
            self.incidents[incident.incident_id] = incident
            self._schedule(incident.arrival_s, "incident_arrival", {"incident_id": incident.incident_id})

        for item in self.scenario["background_events"]:
            arrival = float(item.get("arrival_s", 0.0))
            self._schedule(arrival, self._background_kind(item), {"event": item})

        # Initial busy units become available at their hidden release time.
        for unit in self.units.values():
            if unit.status != "AVAILABLE":
                release = unit.busy_until_s
                if release is None:
                    # An initial non-available unit without a release is a
                    # malformed latent state; leave it busy but make the
                    # missing calibration explicit in the private trace.
                    continue
                self._schedule(release, "unit_release", {
                    "unit_id": unit.unit_id, "token": unit.assignment_token,
                    "initial": True,
                })

        horizon = float(self.scenario["clock"]["horizon_seconds"])
        self._schedule(horizon, "horizon", {})
        self._process_due_at_current_time()
        self._initial_hash = self.state_hash()
        self._record_hash("reset")
        self._write_init_logs()
        return self.observe()

    def _background_kind(self, item: Mapping[str, Any]) -> str:
        kind = str(item.get("kind", "background_arrival")).lower()
        if kind in {"arrival", "incident", "background", "background_arrival"}:
            return "background_arrival"
        if "release" in kind:
            return "unit_release"
        if "assign" in kind or "occup" in kind or "dispatch" in kind:
            return "background_assign"
        return "background_arrival"

    def _schedule(self, time_s: float, kind: str, payload: Optional[Mapping[str, Any]] = None,
                  token: Optional[str] = None) -> Event:
        if not math.isfinite(float(time_s)):
            raise ScenarioError(f"invalid event time {time_s!r}")
        if float(time_s) < self.sim_time - 1e-9:
            raise ScenarioError(f"cannot schedule event in the past: {time_s}")
        self._next_sequence += 1
        event = Event(float(time_s), EVENT_PRIORITY.get(kind, 60), self._next_sequence,
                      kind, dict(payload or {}), token)
        heapq.heappush(self._queue, event)
        return event

    def _write_line(self, name: str, value: Mapping[str, Any]) -> None:
        if not self.state_dir:
            return
        self._ensure_state_dir()
        path = self.state_dir / name
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(canonical_json(value) + "\n")

    def _event_log_file_offset(self) -> Optional[int]:
        """Return the current event-log byte offset for action rollback."""
        if not self.state_dir:
            return None
        path = self.state_dir / "event_log.jsonl"
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0

    def _rollback_event_log_file(self, offset: Optional[int]) -> None:
        """Remove transition lines written by a failed action, if any."""
        if offset is None or not self.state_dir:
            return
        path = self.state_dir / "event_log.jsonl"
        try:
            with open(path, "r+b") as handle:
                handle.truncate(offset)
        except FileNotFoundError:
            return

    def _write_init_logs(self) -> None:
        if not self.state_dir:
            return
        # Start fresh only on reset; subsequent commands append.
        self._ensure_state_dir()
        for name in ("event_log.jsonl", "state_hashes.jsonl", "actions.jsonl", "rng_manifest.json"):
            try:
                (self.state_dir / name).unlink()
            except FileNotFoundError:
                pass
        # Keep explicit empty append-only artifacts even when the Agent makes
        # no writes.  Harbor sidecar artifact collection treats a missing
        # source as an infrastructure error, while an empty action log is a
        # valid no-op trajectory that the verifier can score.
        (self.state_dir / "event_log.jsonl").touch()
        (self.state_dir / "actions.jsonl").touch()
        self._write_line("state_hashes.jsonl", {
            "state_version": self.state_version, "sim_time": self.sim_time,
            "reason": "reset", "state_hash": self._initial_hash,
        })
        # Events processed at logical time zero (for example an immediate
        # background arrival) occurred before the state directory was opened;
        # append them now so the on-disk authoritative log matches the private
        # checkpoint and replay manifest.
        for item in self._event_log:
            self._write_line("event_log.jsonl", item)
        self._checkpoint()

    def _checkpoint(self) -> None:
        if not self.state_dir:
            return
        self._ensure_state_dir()
        data = self._private_snapshot(include_queue=True)
        path = self.state_dir / "checkpoint.json"
        temporary = path.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        os.replace(temporary, path)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        self._write_manifest()

    def _load_checkpoint(self) -> None:
        with open(self.state_dir / "checkpoint.json", "r", encoding="utf-8") as handle:
            snapshot = json.load(handle)
        expected_hash = sha256_json(self.scenario)
        if snapshot.get("scenario_hash") not in {None, expected_hash}:
            raise ScenarioError("checkpoint scenario_hash does not match the loaded scenario")
        source_hash = sha256_json(self.raw_scenario)
        if snapshot.get("source_scenario_hash") not in {None, source_hash}:
            raise ScenarioError("checkpoint source_scenario_hash does not match the loaded scenario")
        self._restore_private_snapshot(snapshot)

    # ------------------------------------------------------------------
    # Public observation and state snapshots
    # ------------------------------------------------------------------
    def _public_snapshot(self) -> Dict[str, Any]:
        snapshot = {
            "sim_time": self.sim_time,
            "state_version": self.state_version,
            "horizon_seconds": self.scenario["clock"]["horizon_seconds"],
            "done": self.done,
            "finished": self.finished,
            "finish_reason": self.finish_reason,
            "units": [self.units[key].public() for key in sorted(self.units)],
            "incidents": [self.incidents[key].public() for key in sorted(self.incidents)
                           if self.incidents[key].status != "HIDDEN"],
            "notifications": self._notifications(),
        }
        # Never expose ``state_hash()`` here: the verifier hash commits to the
        # hidden queue, latent durations, RNG-conditioned state, and private
        # event history.  Returning it to the Agent would create a fingerprint
        # oracle even though the underlying fields remain redacted.
        snapshot["public_state_hash"] = sha256_json(snapshot)
        return snapshot

    def _notifications(self) -> List[Dict[str, Any]]:
        # Only public event summaries since the last action are returned.
        result = []
        for item in self._transition_events[-50:]:
            if item.get("public"):
                result.append(copy.deepcopy(item["public"]))
        return result

    def observe(self) -> Dict[str, Any]:
        """Return only information available to the Agent at current time."""
        return copy.deepcopy(self._public_snapshot())

    def _private_snapshot(self, *, include_queue: bool = True) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "engine_version": ENGINE_VERSION,
            "scenario_id": self.scenario["scenario_id"],
            "scenario_hash": sha256_json(self.scenario),
            "source_scenario_hash": sha256_json(self.raw_scenario),
            "sim_time": self.sim_time,
            "state_version": self.state_version,
            "done": self.done,
            "finished": self.finished,
            "finish_reason": self.finish_reason,
            "units": [self.units[key].private() for key in sorted(self.units)],
            "incidents": [self.incidents[key].private() for key in sorted(self.incidents)],
            "metrics": copy.deepcopy(self._metrics),
            "actions": copy.deepcopy(self._actions),
            "transition_events": copy.deepcopy(self._transition_events),
            "event_log": copy.deepcopy(self._event_log),
            "state_hashes": copy.deepcopy(self._state_hashes),
            "initial_hash": self._initial_hash,
            "next_sequence": self._next_sequence,
            "action_cache": copy.deepcopy(self._action_cache),
        }
        if include_queue:
            data["queue"] = [event.private() for event in sorted(self._queue)]
        return data

    def _restore_private_snapshot(self, data: Mapping[str, Any]) -> None:
        self._reset_runtime()
        self.sim_time = float(data.get("sim_time", 0.0))
        self.state_version = int(data.get("state_version", 0))
        self.done = bool(data.get("done", False))
        self.finished = bool(data.get("finished", False))
        self.finish_reason = data.get("finish_reason")
        for item in data.get("units", []):
            unit = Unit(
                unit_id=str(item["unit_id"]), unit_type=str(item["unit_type"]),
                als=bool(item.get("als", False)), zone=str(item.get("zone", "UNKNOWN")),
                status=str(item.get("status", "AVAILABLE")),
                incident_id=item.get("incident_id"), busy_until_s=item.get("busy_until_s"),
                assignment_token=int(item.get("assignment_token", 0)),
                initial_zone=item.get("initial_zone"), background=bool(item.get("background", False)),
            )
            self.units[unit.unit_id] = unit
        for item in data.get("incidents", []):
            incident = Incident(
                incident_id=str(item["incident_id"]), arrival_s=float(item.get("arrival_s", 0)),
                call_type=str(item.get("call_type", "UNKNOWN")), priority=str(item.get("priority", "UNKNOWN")),
                requires_als=bool(item.get("requires_als", False)), zone=str(item.get("zone", "UNKNOWN")),
                dispatchable=bool(item.get("dispatchable", False)), source_rowids=list(item.get("source_rowids", [])),
                status=str(item.get("status", "HIDDEN")), assigned_unit_id=item.get("assigned_unit_id"),
                discovered_s=item.get("discovered_s"), dispatch_s=item.get("dispatch_s"),
                on_scene_s=item.get("on_scene_s"), completion_s=item.get("completion_s"),
                response_time_s=item.get("response_time_s"), latent=copy.deepcopy(item.get("latent", {})),
                background=bool(item.get("background", False)),
            )
            self.incidents[incident.incident_id] = incident
        for item in data.get("queue", []):
            event = Event(float(item["time_s"]), int(item["priority"]), int(item["sequence"]),
                          str(item["kind"]), dict(item.get("payload", {})), item.get("token"))
            heapq.heappush(self._queue, event)
        self._next_sequence = int(data.get("next_sequence", 0))
        self._metrics = copy.deepcopy(data.get("metrics", self._metrics))
        self._actions = copy.deepcopy(data.get("actions", []))
        self._transition_events = copy.deepcopy(data.get("transition_events", []))
        self._event_log = copy.deepcopy(data.get("event_log", self._transition_events))
        self._state_hashes = copy.deepcopy(data.get("state_hashes", []))
        self._initial_hash = str(data.get("initial_hash", ""))
        self._action_cache = copy.deepcopy(data.get("action_cache", {}))

    def _hash_material(self) -> Dict[str, Any]:
        # Include latent state, queue contents, and the append-only event
        # sequence.  The latter binds a state hash to its complete causal
        # history, which lets the verifier detect log tampering.
        return {
            "scenario_id": self.scenario["scenario_id"],
            "sim_time": self.sim_time,
            "state_version": self.state_version,
            "done": self.done,
            "finished": self.finished,
            "finish_reason": self.finish_reason,
            "units": [self.units[key].private() for key in sorted(self.units)],
            "incidents": [self.incidents[key].private() for key in sorted(self.incidents)],
            "queue": [event.private() for event in sorted(self._queue)],
            "event_log": copy.deepcopy(self._event_log),
        }

    def state_hash(self) -> str:
        """Return the verifier-only hash of the complete private world."""
        return sha256_json(self._hash_material())

    def public_state_hash(self) -> str:
        """Return a hash derived exclusively from the current observation."""
        return str(self._public_snapshot()["public_state_hash"])

    def _record_hash(self, reason: str) -> str:
        digest = self.state_hash()
        entry = {"state_version": self.state_version, "sim_time": self.sim_time,
                 "reason": reason, "state_hash": digest}
        self._state_hashes.append(entry)
        self._write_line("state_hashes.jsonl", entry)
        return digest

    # ------------------------------------------------------------------
    # Action processing
    # ------------------------------------------------------------------
    def _normalise_command(self, command: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(command, Mapping):
            raise ActionError("command must be a JSON object")
        result = {str(k): copy.deepcopy(v) for k, v in command.items()}
        # Harbor/ATIF trajectories commonly encode tool arguments under an
        # ``args`` or ``arguments`` object.  Flatten that envelope while
        # retaining top-level command/action identity.
        nested_args = result.get("args", result.get("arguments"))
        if isinstance(nested_args, Mapping):
            flattened = {str(k): copy.deepcopy(v) for k, v in nested_args.items()}
            for key, value in result.items():
                if key not in {"args", "arguments"}:
                    flattened.setdefault(key, value)
            result = flattened
        name = _first(result, "command", "action", "op", "type")
        if not name:
            raise ActionError("missing command")
        result["command"] = str(name).lower().replace("_", "-")
        if "state_version" not in result and "expected_state_version" in result:
            result["state_version"] = result["expected_state_version"]
        return result

    def _action_id(self, command: MutableMapping[str, Any]) -> str:
        supplied = _first(command, "action_id", "id")
        if supplied is None or str(supplied) == "":
            # Deterministic fallback keeps simple interactive use convenient;
            # callers seeking idempotence should supply an explicit ID.
            supplied = f"auto-{len(self._actions) + 1}"
            command["action_id"] = supplied
        return str(supplied)

    def _check_state_version(self, command: Mapping[str, Any]) -> None:
        expected = command.get("state_version")
        if expected is None:
            return
        try:
            expected_int = int(expected)
        except (TypeError, ValueError) as exc:
            raise ActionError("state_version must be an integer") from exc
        if expected_int != self.state_version:
            raise ActionError(f"stale state_version: expected {self.state_version}, got {expected_int}")

    def _ok(self, command: Mapping[str, Any], *, events: Optional[List[Dict[str, Any]]] = None,
            message: Optional[str] = None) -> Dict[str, Any]:
        result = {
            "ok": True,
            "action_id": command.get("action_id"),
            "command": command.get("command"),
            "state_version": self.state_version,
            "sim_time": self.sim_time,
            "public_state_hash": self.public_state_hash(),
            "events": copy.deepcopy(events or []),
            "observation": self.observe(),
        }
        if message:
            result["message"] = message
        return result

    def _error(self, command: Optional[Mapping[str, Any]], error: str,
               *, code: str = "action_rejected") -> Dict[str, Any]:
        self._metrics["invalid_actions"] += 1
        result = {
            "ok": False,
            "action_id": command.get("action_id") if command else None,
            "command": command.get("command") if command else None,
            "error": error,
            "code": code,
            "state_version": self.state_version,
            "sim_time": self.sim_time,
            "public_state_hash": self.public_state_hash(),
            "observation": self.observe(),
        }
        return result

    def _persist_action_record(
        self,
        command: Mapping[str, Any],
        action_id: str,
        fingerprint: str,
        before_hash: str,
        response: Mapping[str, Any],
        *,
        cache_response: bool = True,
    ) -> Dict[str, Any]:
        """Persist one attempted write in the authoritative sidecar log.

        Conflict attempts are deliberately persisted too.  They must not
        replace the original idempotency-cache entry, but omitting them from
        the append-only log would let a client hide an illegal action from the
        independent verifier.
        """

        after_hash = self.state_hash()
        record = {
            "record_type": "action",
            "action_id": action_id,
            "fingerprint": fingerprint,
            "command": copy.deepcopy(dict(command)),
            "before_state_hash": before_hash,
            "after_state_hash": after_hash,
            "ok": bool(response.get("ok")),
            "state_version": self.state_version,
            "sim_time": self.sim_time,
        }
        if not bool(response.get("ok")):
            # Keep rejection diagnostics in the authoritative log so the
            # verifier can distinguish a stale write from a capability error
            # without trusting an Agent-side mirror.
            record["code"] = response.get("code")
            record["error"] = response.get("error")
        self._actions.append(record)
        if cache_response:
            self._action_cache[action_id] = {
                "fingerprint": fingerprint,
                "response": copy.deepcopy(dict(response)),
            }
        self._last_response = dict(response)
        self._write_line("actions.jsonl", record)
        self._record_hash(str(command.get("command", "unknown")))
        self._checkpoint()
        return dict(response)

    def step(self, command: Mapping[str, Any]) -> Dict[str, Any]:
        """Apply one command and return a JSON-serialisable result.

        Explicit action IDs are cached.  Repeating an identical action returns
        its original response without changing state; reusing an ID for a
        different command is rejected.
        """
        try:
            cmd = self._normalise_command(command)
            aid = self._action_id(cmd)
        except ActionError as exc:
            return self._error(None, str(exc), code="invalid_command")

        fingerprint = sha256_json(cmd)
        if aid in self._action_cache:
            cached = self._action_cache[aid]
            if cached.get("fingerprint") == fingerprint:
                return copy.deepcopy(cached["response"])
            # Keep the original cache entry intact so a later identical retry
            # remains idempotent, while still recording this conflicting write
            # for safety scoring and replay diagnostics.
            before_hash = self.state_hash()
            response = self._error(
                cmd,
                f"action_id {aid!r} was already used for a different command",
                code="action_id_conflict",
            )
            return self._persist_action_record(
                cmd, aid, fingerprint, before_hash, response, cache_response=False
            )

        before_hash = self.state_hash()
        # A failed action can fail after partially validating/mutating a
        # resource (for example when a latent duration is missing).  Restore
        # the complete private snapshot so rejection leaves the world intact.
        before_snapshot = self._private_snapshot(include_queue=True)
        event_log_offset = self._event_log_file_offset()
        self._transition_events = []
        mutating = cmd["command"] not in {"observe", "status"}
        try:
            if mutating:
                self._check_state_version(cmd)
            name = cmd["command"]
            if name in {"observe", "status"}:
                response = {"ok": True, "action_id": aid, "command": name,
                            "state_version": self.state_version, "sim_time": self.sim_time,
                            "public_state_hash": self.public_state_hash(),
                            "observation": self.observe()}
            elif name == "dispatch":
                self._dispatch(cmd)
                self.state_version += 1
                self._metrics["action_count"] += 1
                response = self._ok(cmd, events=self._public_events())
            elif name == "reassign":
                self._reassign(cmd)
                self.state_version += 1
                self._metrics["action_count"] += 1
                response = self._ok(cmd, events=self._public_events())
            elif name == "reposition":
                self._reposition(cmd)
                self.state_version += 1
                self._metrics["action_count"] += 1
                response = self._ok(cmd, events=self._public_events())
            elif name in {"advance", "next-event", "next"}:
                # Advancing an already completed horizon is an idempotent
                # control-plane operation.  It may be useful to a caller
                # recovering from a lost response, but it must not manufacture
                # new state versions or meaningful actions that can later be
                # used as score-padding.
                if self._advance(cmd):
                    self.state_version += 1
                    self._metrics["action_count"] += 1
                else:
                    self._transition_events = copy.deepcopy(
                        before_snapshot.get("transition_events", [])
                    )
                response = self._ok(cmd, events=self._public_events())
            elif name == "finish":
                # ``finish`` is likewise idempotent after the first accepted
                # request.  Distinct action IDs are still recorded for audit,
                # while the logical world and version remain unchanged.
                if self._finish(cmd):
                    self.state_version += 1
                    self._metrics["action_count"] += 1
                else:
                    self._transition_events = copy.deepcopy(
                        before_snapshot.get("transition_events", [])
                    )
                response = self._ok(cmd, events=self._public_events())
            else:
                raise ActionError(f"unknown command {name!r}")
        except ActionError as exc:
            self._restore_private_snapshot(before_snapshot)
            self._rollback_event_log_file(event_log_offset)
            response = self._error(cmd, str(exc), code="action_rejected")
        except ScenarioError as exc:
            self._restore_private_snapshot(before_snapshot)
            self._rollback_event_log_file(event_log_offset)
            response = self._error(cmd, str(exc), code="scenario_error")
        except Exception as exc:
            # Keep malformed or unexpected transition inputs inside the same
            # atomic error boundary as ordinary validation failures.  The
            # sidecar contract promises a structured response and an
            # unchanged world for every rejected write; leaking an arbitrary
            # ``KeyError``/``TypeError`` here would otherwise terminate a CLI
            # request halfway through a transition.
            self._restore_private_snapshot(before_snapshot)
            self._rollback_event_log_file(event_log_offset)
            response = self._error(cmd, f"transition failed: {type(exc).__name__}: {exc}",
                                   code="transition_error")

        return self._persist_action_record(cmd, aid, fingerprint, before_hash, response)

    def _public_events(self) -> List[Dict[str, Any]]:
        return [copy.deepcopy(item["public"]) for item in self._transition_events if item.get("public")]

    def _dispatch(self, cmd: Mapping[str, Any]) -> None:
        if self.done or self.finished:
            raise ActionError("simulation is finished")
        uid = str(_first(cmd, "unit_id", "unit", default=""))
        iid = str(_first(cmd, "incident_id", "incident", "call_id", default=""))
        if not uid or not iid:
            raise ActionError("dispatch requires unit_id and incident_id")
        unit = self.units.get(uid)
        incident = self.incidents.get(iid)
        if unit is None:
            raise ActionError(f"unknown unit {uid!r}")
        # Do not distinguish a guessed future ID from an unknown ID.  The
        # scenario contains the complete hidden arrival schedule, so a
        # probe-friendly error here would let an Agent enumerate future work
        # without advancing logical time.
        if incident is None or incident.status == "HIDDEN":
            # Keep unknown and future IDs indistinguishable.  Including the
            # submitted ID in the message would turn this write endpoint into
            # a future-incident enumeration oracle.
            raise ActionError("incident is unavailable")
        if not unit.controllable:
            raise ActionError(f"unit {uid!r} is not controllable")
        if unit.status != "AVAILABLE":
            raise ActionError(f"unit {uid!r} is not available ({unit.status})")
        if incident.status not in {"QUEUED", "UNASSIGNED"}:
            raise ActionError(f"incident {iid!r} cannot be dispatched from {incident.status}")
        if not incident.dispatchable:
            raise ActionError(f"incident {iid!r} is background/non-dispatchable")
        if incident.requires_als and not unit.als:
            raise ActionError(f"incident {iid!r} requires ALS")

        token = f"{uid}:{unit.assignment_token + 1}:{iid}"
        unit.assignment_token += 1
        unit.status = "EN_ROUTE"
        unit.incident_id = iid
        incident.status = "EN_ROUTE"
        incident.assigned_unit_id = uid
        incident.dispatch_s = self.sim_time
        travel = self._duration("travel", unit, incident, token)
        self._schedule(self.sim_time + travel, "travel_complete",
                       {"unit_id": uid, "incident_id": iid, "token": token}, token=token)
        self._metrics["dispatch_actions"] += 1
        self._add_transition("dispatch", {"unit_id": uid, "incident_id": iid,
                                            "travel_time_s": travel},
                             public={"kind": "dispatch", "unit_id": uid, "incident_id": iid})

    def _reassign(self, cmd: Mapping[str, Any]) -> None:
        if self.done or self.finished:
            raise ActionError("simulation is finished")
        iid = str(_first(cmd, "incident_id", "incident", default=""))
        from_uid = str(_first(cmd, "from_unit", "from", "unit_id", default=""))
        to_uid = str(_first(cmd, "to_unit", "to", "new_unit", default=""))
        if not iid or not from_uid or not to_uid:
            raise ActionError("reassign requires incident_id, from_unit and to_unit")
        old = self.units.get(from_uid)
        new = self.units.get(to_uid)
        incident = self.incidents.get(iid)
        # Hidden incidents and unknown IDs intentionally share one response so
        # the write API cannot be used as a future-event oracle.
        if old is None or new is None:
            raise ActionError("unknown unit")
        if incident is None or incident.status == "HIDDEN":
            raise ActionError("incident is unavailable")
        if not new.controllable or not old.controllable:
            raise ActionError("both units must be controllable")
        if incident.assigned_unit_id != from_uid or old.incident_id != iid:
            raise ActionError("from_unit is not assigned to incident")
        if incident.status not in {"EN_ROUTE", "ASSIGNED"}:
            raise ActionError(f"cannot reassign incident from {incident.status}")
        if new.status != "AVAILABLE":
            raise ActionError(f"to_unit {to_uid!r} is not available")
        if incident.requires_als and not new.als:
            raise ActionError(f"incident {iid!r} requires ALS")

        # Invalidate old queue events by incrementing its assignment token.
        old.assignment_token += 1
        old.status = "AVAILABLE"
        old.incident_id = None
        old.busy_until_s = None
        new.assignment_token += 1
        token = f"{to_uid}:{new.assignment_token}:{iid}"
        new.status = "EN_ROUTE"
        new.incident_id = iid
        incident.assigned_unit_id = to_uid
        incident.dispatch_s = self.sim_time
        travel = self._duration("travel", new, incident, token)
        self._schedule(self.sim_time + travel, "travel_complete",
                       {"unit_id": to_uid, "incident_id": iid, "token": token}, token=token)
        self._metrics["reassign_actions"] += 1
        self._metrics["churn_count"] += 1
        self._add_transition("reassign", {"incident_id": iid, "from_unit": from_uid,
                                            "to_unit": to_uid, "travel_time_s": travel},
                             public={"kind": "reassign", "incident_id": iid,
                                    "from_unit": from_uid, "to_unit": to_uid})

    def _reposition(self, cmd: Mapping[str, Any]) -> None:
        if self.done or self.finished:
            raise ActionError("simulation is finished")
        uid = str(_first(cmd, "unit_id", "unit", default=""))
        zone = str(_first(cmd, "zone_id", "zone", "to_zone", default=""))
        if not uid or not zone:
            raise ActionError("reposition requires unit_id and zone_id")
        unit = self.units.get(uid)
        if unit is None:
            raise ActionError(f"unknown unit {uid!r}")
        if not unit.controllable:
            raise ActionError(f"unit {uid!r} is not controllable")
        if unit.status != "AVAILABLE":
            raise ActionError(f"unit {uid!r} is not available ({unit.status})")
        if not _valid_zone_label(zone):
            raise ActionError("invalid zone label")
        # Only zones already present in the public observation are legal.
        # Consulting the full scenario catalog would reveal future incident
        # locations, while accepting arbitrary labels lets an Agent force a
        # calibration fallback and potentially bypass a costly OD stratum.
        public_zones = {candidate.zone for candidate in self.units.values()}
        public_zones.update(
            incident.zone for incident in self.incidents.values()
            if incident.status != "HIDDEN"
        )
        if zone not in public_zones:
            raise ActionError("zone is unavailable")
        origin = unit.zone
        unit.assignment_token += 1
        token = f"reposition:{uid}:{unit.assignment_token}:{zone}"
        unit.status = "REPOSITIONING"
        unit.incident_id = None
        travel = self._reposition_duration(unit, origin, zone, token)
        self._schedule(self.sim_time + travel, "reposition_complete",
                       {"unit_id": uid, "zone": zone, "origin": origin, "token": token}, token=token)
        self._metrics["reposition_actions"] += 1
        self._add_transition("reposition", {"unit_id": uid, "origin": origin,
                                              "zone": zone, "travel_time_s": travel},
                             public={"kind": "reposition", "unit_id": uid, "origin": origin,
                                    "zone": zone})

    def _advance(self, cmd: Mapping[str, Any]) -> bool:
        if self.finished:
            raise ActionError("simulation is finished")
        if self.done:
            return False
        # ``advance`` moves to the next distinct timestamp and processes all
        # same-time events in queue order, avoiding partial same-time views.
        if not self._queue:
            self.sim_time = min(float(self.scenario["clock"]["horizon_seconds"]), self.sim_time)
            self.done = True
            return True
        next_time = self._queue[0].time_s
        self.sim_time = max(self.sim_time, next_time)
        self._process_due_at_current_time()
        if self.sim_time >= float(self.scenario["clock"]["horizon_seconds"]):
            self.done = True
        return True

    def _finish(self, cmd: Mapping[str, Any]) -> bool:
        if self.finished:
            return False
        self.finished = True
        self.done = True
        self.finish_reason = str(_first(cmd, "reason", default="agent_finished"))
        # Mark visible unresolved incidents as unserved for metric purposes;
        # do not mutate hidden future incidents.
        for incident in self.incidents.values():
            if incident.status in {"QUEUED", "UNASSIGNED", "EN_ROUTE", "ON_SCENE", "SERVICING"}:
                if incident.status in {"QUEUED", "UNASSIGNED"}:
                    incident.status = "UNSERVED"
        self._add_transition("finish", {"reason": self.finish_reason},
                             public={"kind": "finish", "reason": self.finish_reason})
        return True

    # ------------------------------------------------------------------
    # Event dynamics
    # ------------------------------------------------------------------
    def _process_due_at_current_time(self) -> None:
        while self._queue and self._queue[0].time_s <= self.sim_time + 1e-9:
            event = heapq.heappop(self._queue)
            self._process_event(event)
            self._metrics["events_processed"] += 1

    def _process_event(self, event: Event) -> None:
        kind = event.kind
        payload = event.payload
        if kind == "horizon":
            self.done = True
            self._add_transition("horizon", {"time_s": self.sim_time},
                                 public={"kind": "horizon", "time_s": self.sim_time})
            return
        if kind == "incident_arrival":
            iid = str(payload["incident_id"])
            incident = self.incidents.get(iid)
            if incident is None or incident.status != "HIDDEN":
                return
            incident.status = "QUEUED" if incident.dispatchable else "BACKGROUND"
            incident.discovered_s = self.sim_time
            self._add_transition("incident_arrival", {"incident_id": iid},
                                 public={"kind": "incident_arrival", "incident_id": iid,
                                        "priority": incident.priority, "zone": incident.zone,
                                        "dispatchable": incident.dispatchable})
            return
        if kind == "background_arrival":
            item = payload.get("event", payload)
            self._handle_background_arrival(item)
            return
        if kind == "background_assign":
            item = payload.get("event", payload)
            self._handle_background_assign(item)
            return
        if kind == "unit_release":
            uid = str(payload.get("unit_id", ""))
            unit = self.units.get(uid)
            if unit is None:
                return
            token = payload.get("token")
            if token is not None and str(token) != str(unit.assignment_token) and not payload.get("initial"):
                self._add_transition("stale_event", {"kind": kind, "unit_id": uid}, public=None)
                return
            unit.status = "AVAILABLE"
            unit.incident_id = None
            unit.busy_until_s = None
            self._add_transition("unit_release", {"unit_id": uid},
                                 public={"kind": "unit_available", "unit_id": uid})
            return
        if kind == "travel_complete":
            uid = str(payload.get("unit_id", "")); iid = str(payload.get("incident_id", ""))
            unit = self.units.get(uid); incident = self.incidents.get(iid)
            token = str(payload.get("token", ""))
            if unit is None or incident is None or unit.incident_id != iid or token != self._unit_token(uid, iid):
                self._add_transition("stale_event", {"kind": kind, "unit_id": uid,
                                                      "incident_id": iid}, public=None)
                return
            unit.status = "ON_SCENE"
            incident.status = "ON_SCENE"
            incident.on_scene_s = self.sim_time
            incident.response_time_s = max(0.0, self.sim_time - incident.arrival_s)
            service = self._duration("service", unit, incident, token)
            release_delay = self._duration("release", unit, incident, token)
            unit.busy_until_s = self.sim_time + service + release_delay
            self._schedule(self.sim_time + service, "service_complete",
                           {"unit_id": uid, "incident_id": iid, "token": token}, token=token)
            self._add_transition("travel_complete", {"unit_id": uid, "incident_id": iid,
                                                       "service_duration_s": service,
                                                       "release_delay_s": release_delay},
                                 public={"kind": "on_scene", "unit_id": uid,
                                        "incident_id": iid})
            return
        if kind == "service_complete":
            uid = str(payload.get("unit_id", "")); iid = str(payload.get("incident_id", ""))
            unit = self.units.get(uid); incident = self.incidents.get(iid)
            token = str(payload.get("token", ""))
            if unit is None or incident is None or unit.incident_id != iid or token != self._unit_token(uid, iid):
                self._add_transition("stale_event", {"kind": kind, "unit_id": uid,
                                                      "incident_id": iid}, public=None)
                return
            incident.status = "COMPLETED"
            incident.completion_s = self.sim_time
            release_at = unit.busy_until_s if unit.busy_until_s is not None else self.sim_time
            unit.status = "CLEARING"
            self._schedule(release_at, "unit_release", {"unit_id": uid, "token": unit.assignment_token},
                           token=token)
            self._add_transition("service_complete", {"unit_id": uid, "incident_id": iid},
                                 public={"kind": "incident_completed", "unit_id": uid,
                                        "incident_id": iid})
            return
        if kind == "reposition_complete":
            uid = str(payload.get("unit_id", "")); unit = self.units.get(uid)
            token = str(payload.get("token", ""))
            reposition_prefix = f"reposition:{uid}:{unit.assignment_token}:" if unit else ""
            if unit is None or unit.status != "REPOSITIONING" or not token.startswith(reposition_prefix):
                self._add_transition("stale_event", {"kind": kind, "unit_id": uid}, public=None)
                return
            unit.zone = str(payload.get("zone", unit.zone))
            unit.status = "AVAILABLE"
            unit.busy_until_s = None
            self._add_transition("reposition_complete", {"unit_id": uid, "zone": unit.zone},
                                 public={"kind": "unit_repositioned", "unit_id": uid,
                                        "zone": unit.zone})
            return

    def _handle_background_arrival(self, item: Mapping[str, Any]) -> None:
        # A background event can be represented without a corresponding
        # incident row; expose only its public ID/type and optionally occupy a
        # listed unit.
        iid = str(_first(item, "incident_id", "call_id", "event_id", default=""))
        if iid and iid in self.incidents:
            incident = self.incidents[iid]
            # A fire/support response can accompany a dispatchable medical
            # incident.  It must not hide or close that incident from the
            # Agent; only genuinely non-dispatchable calls become background.
            if not incident.dispatchable:
                incident.status = "BACKGROUND"
            incident.discovered_s = self.sim_time
        uid = _first(item, "unit_id", "unit", default=None)
        dispatch_s = _as_float(_first(item, "dispatch_s", "dispatch_at_s"))
        if dispatch_s is not None and dispatch_s > self.sim_time + 1e-9:
            self._schedule(dispatch_s, "background_assign", {"event": dict(item)})
        elif uid is not None:
            self._handle_background_assign(dict(item, unit_id=uid))
        self._add_transition("background_arrival", {"event_id": item.get("event_id"),
                                                      "incident_id": iid or None},
                             public={"kind": "background_arrival", "event_id": item.get("event_id"),
                                    "incident_id": iid or None})

    def _handle_background_assign(self, item: Mapping[str, Any]) -> None:
        uid = str(_first(item, "unit_id", "unit", default=""))
        unit = self.units.get(uid)
        if unit is None:
            return
        if unit.status != "AVAILABLE":
            return
        unit.assignment_token += 1
        unit.status = "BUSY"
        unit.incident_id = str(_first(item, "incident_id", "call_id", "event_id", default="BACKGROUND"))
        release = _as_float(_first(item, "release_s", "release_at_s"))
        if release is None:
            duration = _as_float(_first(item, "duration_s", "busy_duration_s"))
            if duration is None:
                duration = self._background_duration(item)
            if duration is None:
                raise ScenarioError(
                    f"background event {item.get('event_id', '')!r} has no calibrated release"
                )
            release = self.sim_time + duration
        if release < self.sim_time:
            release = self.sim_time
        unit.busy_until_s = release
        self._schedule(release, "unit_release", {"unit_id": uid, "token": unit.assignment_token})
        self._add_transition("background_assign", {"unit_id": uid, "release_s": release}, public=None)

    def _background_duration(self, item: Mapping[str, Any]) -> float:
        value = self.scenario["calibration"].get("background_duration_s")
        return self._distribution_value(value, f"background:{item.get('event_id', '')}", default=None)

    def _unit_token(self, uid: str, iid: Optional[str], prefix: str = "") -> str:
        unit = self.units[uid]
        if prefix == "reposition":
            # Reposition token includes destination, so compare by prefix.
            return f"reposition:{uid}:{unit.assignment_token}:"
        return f"{uid}:{unit.assignment_token}:{iid}"

    # ------------------------------------------------------------------
    # Duration/calibration helpers
    # ------------------------------------------------------------------
    def _duration(self, kind: str, unit: Unit, incident: Incident, token: str) -> float:
        latent = incident.latent
        aliases = {
            "travel": ("travel_time_s", "travel_duration_s", "travel_s", "travel_time"),
            "service": ("service_duration_s", "service_time_s", "service_s", "service_time"),
            "release": ("release_delay_s", "release_duration_s", "release_time_s",
                         "release_s", "clearance_s", "release"),
        }
        candidates = aliases.get(kind, (f"{kind}_duration_s", f"{kind}_time_s", f"{kind}_s", kind))
        value = None
        for key in candidates:
            if key in latent:
                value = latent[key]
                break
        # A latent mapping may be keyed by unit ID or type.
        if isinstance(value, Mapping):
            value = _first(value, unit.unit_id, unit.unit_type, "default")
        if value is None:
            calibration = self.scenario["calibration"]
            value = self._lookup_calibration(calibration, kind, unit, incident)
        result = self._distribution_value(value, f"{kind}:{unit.unit_id}:{incident.incident_id}:{token}")
        if result is None or result < 0:
            raise ScenarioError(
                f"missing calibrated {kind} duration for unit={unit.unit_id}, incident={incident.incident_id}; "
                "provide scenario calibration or incident.latent values"
            )
        return max(0.0, float(result))

    def _lookup_calibration(self, calibration: Mapping[str, Any], kind: str,
                            unit: Unit, incident: Incident) -> Any:
        aliases = {
            "travel": ("travel_time_s", "travel_s", "travel_time"),
            "service": ("service_duration_s", "service_s", "service_time_s", "service_time"),
            "release": ("release_delay_s", "release_s", "clearance_s"),
        }
        value: Any = None
        for key in aliases.get(kind, (kind,)):
            if key in calibration:
                value = calibration[key]
                break
        if value is None:
            return None
        if not isinstance(value, Mapping):
            return value
        # Try increasingly general keys.  Both flat and nested maps are
        # supported to keep generated scenario manifests readable.
        keys = [
            f"{unit.unit_type}|{unit.zone}|{incident.zone}|{self._logical_time_bucket()}",
            f"{unit.unit_type}|{unit.zone}|{incident.zone}",
            f"{unit.unit_type}:{unit.zone}:{incident.zone}",
            f"{unit.zone}|{incident.zone}",
            f"{unit.zone}:{incident.zone}",
            # Service and release calibration is stratified by
            # unit_type x priority.  Keep this key ahead of the unit-wide
            # fallback; otherwise every priority silently collapses to the
            # aggregate type distribution.
            f"{unit.unit_type}|{incident.priority}",
            f"{unit.unit_type}:{incident.priority}",
            unit.unit_type,
            incident.priority,
            "default",
        ]
        for key in keys:
            if key in value:
                candidate = value[key]
                if isinstance(candidate, Mapping):
                    # A candidate may itself be a distribution object
                    # (min/max/median/samples), not a second level of unit or
                    # priority strata.  Preserve it for _distribution_value.
                    nested = _first(candidate, unit.unit_type, incident.priority, "default")
                    if nested is not None:
                        candidate = nested
                if candidate is not None:
                    return candidate
        # Nested origin -> destination -> value.
        origin_map = value.get(unit.zone)
        if isinstance(origin_map, Mapping):
            candidate = origin_map.get(incident.zone, origin_map.get("default"))
            if isinstance(candidate, Mapping):
                nested = _first(candidate, unit.unit_type, incident.priority, "default")
                if nested is not None:
                    candidate = nested
            if candidate is not None:
                return candidate
        return None

    def _logical_time_bucket(self) -> str:
        """Return the empirical weekday/hour bucket for the logical clock."""

        t0 = self.scenario.get("clock", {}).get("t0")
        if isinstance(t0, str):
            try:
                parsed = _datetime.datetime.fromisoformat(t0.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone(_datetime.timezone.utc).replace(tzinfo=None)
                current = parsed + _datetime.timedelta(seconds=self.sim_time)
                return f"weekday_{current.weekday()}_hour_{current.hour:02d}"
            except ValueError:
                pass
        return "unknown"

    def _reposition_duration(self, unit: Unit, origin: str, destination: str, token: str) -> float:
        calibration = self.scenario["calibration"]
        value = calibration.get("reposition_time_s", calibration.get("travel_time_s"))
        if isinstance(value, Mapping):
            # Reuse the same flat/nested lookup convention.
            value = self._lookup_calibration(calibration, "travel", unit,
                                              Incident("__reposition__", 0, "", "3", False,
                                                       destination, True))
        result = self._distribution_value(value, f"reposition:{unit.unit_id}:{origin}:{destination}:{token}")
        if result is None:
            raise ScenarioError("missing calibrated reposition/travel duration")
        return max(0.0, float(result))

    def _distribution_value(self, value: Any, key: str, default: Optional[float] = None) -> Optional[float]:
        if value is None:
            return default
        if isinstance(value, (int, float, str)):
            return _as_float(value)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            values = [_as_float(v) for v in value]
            values = [v for v in values if v is not None]
            if not values:
                return default
            return values[_stable_int(self.scenario["seed"], key, len(values))]
        if isinstance(value, Mapping):
            # Explicit sample arrays take precedence.
            samples = value.get("samples")
            if isinstance(samples, Sequence) and not isinstance(samples, (str, bytes, bytearray)):
                return self._distribution_value(samples, key, default)
            # Common calibration exports use either a mean or a median as the
            # deterministic representative.  Accept both while preserving
            # explicit sample/min/max precedence.
            exact = _first(value, "value", "seconds", "duration_s", "mean_s", "mean",
                           "median_seconds", "median_s", "p50_s", "default_seconds", default=None)
            minimum = _as_float(_first(value, "min_s", "minimum_s", "min"))
            maximum = _as_float(_first(value, "max_s", "maximum_s", "max"))
            if exact is not None and minimum is None and maximum is None:
                return _as_float(exact)
            if minimum is not None or maximum is not None:
                if minimum is None:
                    minimum = float(exact if exact is not None else 0)
                if maximum is None:
                    maximum = float(exact if exact is not None else minimum)
                if maximum < minimum:
                    raise ScenarioError("calibration max is less than min")
                return minimum + (maximum - minimum) * _stable_uniform(self.scenario["seed"], key)
            if exact is not None:
                return _as_float(exact)
        return default

    # ------------------------------------------------------------------
    # Trace, replay and metrics
    # ------------------------------------------------------------------
    def _add_transition(self, kind: str, private: Mapping[str, Any],
                        public: Optional[Mapping[str, Any]]) -> None:
        item = {"kind": kind, "time_s": self.sim_time,
                "private": copy.deepcopy(dict(private)),
                "public": copy.deepcopy(dict(public)) if public is not None else None}
        self._transition_events.append(item)
        self._event_log.append(copy.deepcopy(item))
        self._write_line("event_log.jsonl", item)

    def _write_manifest(self) -> None:
        if not self.state_dir:
            return
        manifest = self.replay_manifest()
        path = self.state_dir / "replay_manifest.json"
        temporary = path.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=True, indent=2, sort_keys=True)
        os.replace(temporary, path)
        rng_path = self.state_dir / "rng_manifest.json"
        rng_temporary = rng_path.with_suffix(".tmp")
        with open(rng_temporary, "w", encoding="utf-8") as handle:
            json.dump(self.rng_manifest(), handle, ensure_ascii=True, indent=2, sort_keys=True)
        os.replace(rng_temporary, rng_path)

    def rng_manifest(self) -> Dict[str, Any]:
        """Describe deterministic sampling without exposing sampled values."""
        return {
            "manifest_version": "1",
            "algorithm": RNG_ALGORITHM,
            "seed": self.scenario["seed"],
            "uniform_key_format": "seed|phase:unit_id:incident_id:assignment_token",
            "choice_key_format": "seed|phase:unit_id:incident_id:assignment_token",
            "phases": ["travel", "service", "release", "reposition", "background"],
        }

    def replay_manifest(self) -> Dict[str, Any]:
        return {
            "manifest_version": "1",
            "engine_version": ENGINE_VERSION,
            "rng_manifest": self.rng_manifest(),
            "scenario_id": self.scenario["scenario_id"],
            "scenario_hash": sha256_json(self.scenario),
            "source_scenario_hash": sha256_json(self.raw_scenario),
            "seed": self.scenario["seed"],
            "initial_state_hash": self._initial_hash,
            "final_state_hash": self.state_hash(),
            "state_version": self.state_version,
            "sim_time": self.sim_time,
            "action_count": len(self._actions),
            "event_count": self._metrics.get("events_processed", 0),
            "action_log_sha256": sha256_json(self._actions),
            "event_log_sha256": sha256_json(self._event_log),
            "state_hash_log_sha256": sha256_json(self._state_hashes),
            "done": self.done,
            "finished": self.finished,
        }

    def export_trace(self, include_private: bool = True) -> Dict[str, Any]:
        """Export a complete replay trace, or a redacted public trace."""
        if include_private:
            return {
                "engine_version": ENGINE_VERSION,
                "scenario_id": self.scenario["scenario_id"],
                "scenario_hash": sha256_json(self.scenario),
                "source_scenario_hash": sha256_json(self.raw_scenario),
                "initial_state_hash": self._initial_hash,
                "final_state_hash": self.state_hash(),
                "actions": copy.deepcopy(self._actions),
                "events": copy.deepcopy(self._event_log),
                "state_hashes": copy.deepcopy(self._state_hashes),
                "final_state": self._private_snapshot(include_queue=True),
                "replay_manifest": self.replay_manifest(),
            }
        return {
            "engine_version": ENGINE_VERSION,
            "scenario_id": self.scenario["scenario_id"],
            "initial_state_hash": self._initial_hash,
            "final_state_hash": self.state_hash(),
            "actions": [{"action_id": item["action_id"], "command": item["command"],
                          "ok": item["ok"], "state_version": item["state_version"],
                          "sim_time": item["sim_time"],
                          "before_state_hash": item["before_state_hash"],
                          "after_state_hash": item["after_state_hash"]}
                         for item in self._actions],
            "events": [item["public"] for item in self._event_log if item.get("public")],
        }

    def metrics(self) -> Dict[str, Any]:
        """Return verifier-friendly deterministic outcome metrics."""
        completed = [i for i in self.incidents.values() if i.dispatchable and i.status == "COMPLETED"]
        dispatchable = [i for i in self.incidents.values() if i.dispatchable and i.status != "HIDDEN"]
        response_times = [i.response_time_s for i in completed if i.response_time_s is not None]
        completed_count = len(completed)
        total = len(dispatchable)
        return {
            **copy.deepcopy(self._metrics),
            "dispatchable_visible": total,
            "completed": completed_count,
            "completion_rate": (completed_count / total) if total else 1.0,
            "mean_response_s": (sum(response_times) / len(response_times)) if response_times else None,
            "max_response_s": max(response_times) if response_times else None,
            "response_times_s": {i.incident_id: i.response_time_s for i in self.incidents.values()
                                 if i.response_time_s is not None},
            "incident_status": {i.incident_id: i.status for i in self.incidents.values()
                                 if i.status != "HIDDEN"},
        }

    def run_actions(self, actions: Iterable[Mapping[str, Any]], *, reset: bool = True) -> Dict[str, Any]:
        """Run a sequence and return responses, trace and metrics."""
        if reset:
            self.reset()
        commands: list[Mapping[str, Any]] = []
        for action in actions:
            if isinstance(action, Mapping) and isinstance(action.get("args"), Mapping):
                command = {str(k): copy.deepcopy(v) for k, v in action["args"].items()}
                command.setdefault("action", action.get("action", action.get("command", "")))
                commands.append(command)
            else:
                commands.append(action)
        responses = [self.step(action) for action in commands]
        return {"responses": responses, "trace": self.export_trace(include_private=True),
                "metrics": self.metrics(), "reward": self.reward()}

    def replay(self, actions: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
        """Alias used by verifiers for a fresh deterministic action replay."""
        return self.run_actions(actions, reset=True)

    def reward(self) -> Dict[str, Any]:
        """Compute transparent simulator-level metrics (not a clinical score).

        Diagnostics only.  The pass/fail authority is
        ``tests/verifier_lib.py::compute_score``; this method deliberately emits
        no strict-pass bit so the two definitions cannot drift apart again.
        """
        metrics = self.metrics()
        response = 0.0
        if metrics["mean_response_s"] is not None:
            # A generic bounded response component; scenario-specific verifier
            # may replace it with an oracle-normalized score.
            response = max(0.0, min(1.0, 1.0 - metrics["mean_response_s"] / 1800.0))
        coverage = float(metrics["completion_rate"])
        safety = max(0.0, 1.0 - min(1.0, metrics["invalid_actions"] / 10.0))
        adaptation = max(0.0, 1.0 - min(1.0, metrics["churn_count"] / 20.0))
        efficiency = max(0.0, 1.0 - min(1.0, metrics["reposition_actions"] / 20.0))
        integrity = 1.0 if self._replay_integrity_ok() else 0.0
        overall = (0.40 * response + 0.20 * coverage + 0.15 * safety +
                   0.10 * adaptation + 0.10 * efficiency + 0.05 * integrity)
        return {"response": response, "coverage": coverage, "safety": safety,
                "adaptation": adaptation, "efficiency": efficiency,
                "integrity": integrity, "overall": overall}

    def _replay_integrity_ok(self) -> bool:
        if not self._state_hashes:
            return True
        return self._state_hashes[-1].get("state_hash") == self.state_hash()


def load_simulator(scenario_path: str, state_dir: Optional[str] = None,
                   *, load_existing: bool = True) -> Simulator:
    """Convenience factory used by the command-line entrypoint."""
    return Simulator(scenario_path, state_dir=state_dir, load_existing=load_existing)


__all__ = [
    "ActionError", "ENGINE_VERSION", "ScenarioError", "Simulator",
    "CONTROLLED_UNIT_TYPES", "canonical_json", "load_simulator", "priority_rank",
    "sha256_json",
]
