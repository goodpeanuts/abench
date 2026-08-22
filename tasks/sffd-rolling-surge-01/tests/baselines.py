"""Public-observation-only baseline policies for SFFD Rolling Surge 01.

The baselines in this module are deliberately implemented against the narrow
Agent API (``observe`` and ``step``).  A policy never receives the scenario,
seed, event queue, calibration tables, latent release times, or a private
trace.  This makes the runner useful both for local calibration and for
checking that a policy cannot accidentally use privileged information.

The module is dependency-free and can be used as a library::

    from baselines import run_baselines
    matrix = run_baselines(lambda: Simulator("scenario.json"))

or as a small command-line utility::

    python tests/baselines.py --scenario scenario.json --output /tmp/baselines.json
"""

from __future__ import annotations

import argparse
import copy
import json
import random
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Protocol, Sequence


class PublicSimulator(Protocol):
    """The only simulator surface a baseline is allowed to use."""

    def observe(self) -> Mapping[str, Any]: ...

    def step(self, command: Mapping[str, Any]) -> Mapping[str, Any]: ...


SimulatorFactory = Callable[[], PublicSimulator]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _priority_rank(incident: Mapping[str, Any]) -> int:
    value = incident.get("priority_rank")
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    text = str(incident.get("priority", "9")).strip().upper().replace("-", "_")
    return {"1": 1, "P1": 1, "CODE_1": 1, "A": 1,
            "2": 2, "P2": 2, "CODE_2": 2, "B": 2,
            "3": 3, "P3": 3, "CODE_3": 3, "C": 3,
            "4": 4, "P4": 4, "D": 4}.get(text, 9)


def _zone_distance(origin: Any, destination: Any) -> float:
    """Approximate distance using only public categorical zones.

    SFFD's public observation intentionally exposes battalion/station zones,
    not coordinates or travel-time calibration.  Equal zones are therefore
    the only distance fact available.  For common labels such as ``B:B03`` we
    use the numeric suffix as a stable tie breaker, never as a hidden map.
    """

    left, right = str(origin or ""), str(destination or "")
    if left == right:
        return 0.0
    left_numbers = [int(v) for v in re.findall(r"\d+", left)]
    right_numbers = [int(v) for v in re.findall(r"\d+", right)]
    if left_numbers and right_numbers:
        return float(abs(left_numbers[-1] - right_numbers[-1]) + (0 if left.split(":", 1)[0] == right.split(":", 1)[0] else 2))
    # Lexical distance is deterministic and does not imply access to a route
    # matrix.  It only breaks otherwise indistinguishable categorical ties.
    common = 0
    for a, b in zip(left, right):
        if a != b:
            break
        common += 1
    return float(1 + max(len(left), len(right)) - common)


def _visible_incidents(observation: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values = observation.get("incidents", [])
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    return [item for item in values if isinstance(item, Mapping)
            and bool(item.get("dispatchable", False))
            and str(item.get("status", "")).upper() in {"QUEUED", "UNASSIGNED"}]


def _available_units(observation: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values = observation.get("units", [])
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    return [item for item in values if isinstance(item, Mapping)
            and bool(item.get("controllable", item.get("unit_type") in {"MEDIC", "BLS", "PRIVATE"}))
            and str(item.get("status", "")).upper() == "AVAILABLE"]


def _feasible_pairs(observation: Mapping[str, Any]) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for incident in _visible_incidents(observation):
        for unit in _available_units(observation):
            requires_als = bool(incident.get("requires_als", False))
            if requires_als and not bool(unit.get("als", str(unit.get("unit_type", "")).upper() == "MEDIC")):
                continue
            pairs.append((incident, unit))
    return pairs


def _action(command: str, observation: Mapping[str, Any], sequence: int, **kwargs: Any) -> dict[str, Any]:
    result = {"command": command, "action_id": f"baseline-{sequence}", **kwargs}
    if observation.get("state_version") is not None:
        result["state_version"] = observation["state_version"]
    return result


class Policy:
    """Base class for policies; subclasses only inspect an observation."""

    name = "policy"

    def __init__(self, *, random_seed: int = 17) -> None:
        # This is policy-local randomness, not the simulator's hidden seed.
        self._rng = random.Random(random_seed)

    def choose(self, observation: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
        raise NotImplementedError

    def _choose_pair(self, observation: Mapping[str, Any], pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
                     *, sequence: int) -> tuple[str, dict[str, Any]] | None:
        if not pairs:
            return None
        incident, unit = pairs[0]
        return "dispatch", _action("dispatch", observation, sequence,
                                    incident_id=str(incident.get("incident_id")),
                                    unit_id=str(unit.get("unit_id")))


class NoOpPolicy(Policy):
    name = "no-op"

    def choose(self, observation: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
        return None


class RandomFeasiblePolicy(Policy):
    name = "random-feasible"

    def choose(self, observation: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
        pairs = _feasible_pairs(observation)
        if not pairs:
            return None
        incident, unit = self._rng.choice(list(pairs))
        return "dispatch", _action("dispatch", observation, 0,
                                    incident_id=str(incident.get("incident_id")),
                                    unit_id=str(unit.get("unit_id")))


class NearestUnitPolicy(Policy):
    name = "nearest-unit"

    def choose(self, observation: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
        pairs = _feasible_pairs(observation)
        if not pairs:
            return None
        # Preserve arrival/order as represented in the public observation and
        # use zone distance plus IDs solely as deterministic tie breakers.
        pair = min(pairs, key=lambda p: (_number(p[0].get("arrival_s")),
                                         _zone_distance(p[1].get("zone"), p[0].get("zone")),
                                         str(p[0].get("incident_id")), str(p[1].get("unit_id"))))
        return self._choose_pair(observation, [pair], sequence=0)


class PriorityNearestPolicy(Policy):
    name = "priority-nearest"

    def choose(self, observation: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
        pairs = _feasible_pairs(observation)
        if not pairs:
            return None
        pair = min(pairs, key=lambda p: (_priority_rank(p[0]), _number(p[0].get("arrival_s")),
                                         _zone_distance(p[1].get("zone"), p[0].get("zone")),
                                         str(p[0].get("incident_id")), str(p[1].get("unit_id"))))
        return self._choose_pair(observation, [pair], sequence=0)


class RollingHorizonPolicy(Policy):
    """Public-state adaptive heuristic, without privileged lookahead.

    It prioritizes urgent visible calls, avoids spending an ALS unit on a
    lower-priority call when a BLS unit is available, and retains one ALS unit
    when multiple urgent calls are currently queued.  It is intentionally a
    feasible online heuristic rather than an oracle or an optimizer.
    """

    name = "rolling-horizon"

    def choose(self, observation: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
        pairs = _feasible_pairs(observation)
        if not pairs:
            return None
        incidents = _visible_incidents(observation)
        urgent = [i for i in incidents if _priority_rank(i) <= 2]
        available_als = [u for u in _available_units(observation) if bool(u.get("als", False))]
        reserve_als = len(urgent) > 1 and len(available_als) > 1

        def key(pair: tuple[Mapping[str, Any], Mapping[str, Any]]) -> tuple[Any, ...]:
            incident, unit = pair
            rank = _priority_rank(incident)
            als = bool(unit.get("als", False))
            # For non-ALS calls, BLS is preferred when an ALS reserve is useful.
            reserve_penalty = 1 if reserve_als and rank > 1 and als else 0
            capability_penalty = 0 if (bool(incident.get("requires_als", False)) == als
                                       or (not incident.get("requires_als", False) and not als)) else 1
            return (rank, reserve_penalty, capability_penalty,
                    _zone_distance(unit.get("zone"), incident.get("zone")),
                    _number(incident.get("arrival_s")), str(incident.get("incident_id")),
                    str(unit.get("unit_id")))

        pair = min(pairs, key=key)
        return self._choose_pair(observation, [pair], sequence=0)


POLICY_TYPES: dict[str, type[Policy]] = {
    "no-op": NoOpPolicy,
    "random-feasible": RandomFeasiblePolicy,
    "nearest-unit": NearestUnitPolicy,
    "priority-nearest": PriorityNearestPolicy,
    "rolling-horizon": RollingHorizonPolicy,
}


@dataclass
class BaselineRun:
    name: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    responses: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    final_observation: dict[str, Any] = field(default_factory=dict)
    score: dict[str, Any] = field(default_factory=dict)
    terminated: bool = False
    error: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "actions": copy.deepcopy(self.actions),
            "responses": copy.deepcopy(self.responses),
            "observations": copy.deepcopy(self.observations),
            "final_observation": copy.deepcopy(self.final_observation),
            "score": copy.deepcopy(self.score),
            "terminated": self.terminated,
            "error": self.error,
        }


def _public_score(run: BaselineRun) -> dict[str, Any]:
    """Compute a transparent proxy score from public observations only.

    The Harbor verifier remains authoritative for leaderboard scores.  This
    local score is intentionally useful for baseline calibration and exposes
    its evidence so callers can compare it with ``tests/score.py``.
    """

    incidents = [i for i in run.final_observation.get("incidents", [])
                 if isinstance(i, Mapping) and bool(i.get("dispatchable", False))]
    completed = [i for i in incidents if str(i.get("status", "")).upper() == "COMPLETED"]
    waits = [(float(i.get("response_time_s")), _priority_rank(i))
             for i in completed if i.get("response_time_s") is not None]
    if waits:
        weighted_wait = sum(wait / (1.0 if rank <= 1 else 0.75 if rank == 2 else 0.5)
                            for wait, rank in waits) / sum(1.0 / (1.0 if rank <= 1 else 0.75 if rank == 2 else 0.5)
                                                         for _, rank in waits)
        response = max(0.0, min(1.0, 1.0 - weighted_wait / 1800.0))
    else:
        response = 0.0
    coverage = len(completed) / len(incidents) if incidents else 1.0
    invalid = sum(1 for response_item in run.responses if not bool(response_item.get("ok", False)))
    safety = max(0.0, 1.0 - invalid / max(1.0, len(run.actions)))
    # No baseline uses reassign; keeping this explicit makes the dimensions
    # easy to replace with the authoritative verifier later.
    adaptation = 1.0
    reposition_count = sum(1 for action in run.actions if action.get("command") == "reposition")
    dispatch_count = sum(1 for action in run.actions if action.get("command") == "dispatch")
    efficiency = max(0.0, 1.0 - reposition_count / max(20.0, dispatch_count + 1.0))
    # Operational dimensions are only earned by actually serving visible
    # work.  This keeps a no-op trace from receiving a large score merely for
    # being syntactically safe/replayable, while preserving the separate
    # integrity component for auditability.
    safety *= coverage
    adaptation *= coverage
    efficiency *= coverage
    integrity = 1.0 if run.terminated and not run.error else 0.0
    overall = (0.40 * response + 0.20 * coverage + 0.15 * safety +
               0.10 * adaptation + 0.10 * efficiency + 0.05 * integrity)
    strict_pass = int(bool(integrity and invalid == 0 and coverage >= 1.0))
    return {
        "response": round(response, 6), "coverage": round(coverage, 6),
        "safety": round(safety, 6), "adaptation": adaptation,
        "efficiency": round(efficiency, 6), "integrity": integrity,
        "overall": round(overall, 6), "strict_pass": strict_pass,
        "evidence": {"visible_dispatchable": len(incidents), "completed": len(completed),
                      "invalid_actions": invalid, "action_count": len(run.actions)},
    }


def run_policy(factory: SimulatorFactory, policy: Policy, *, max_steps: int = 2000) -> BaselineRun:
    """Run one policy using only public observations and commands."""

    run = BaselineRun(name=policy.name)
    simulator = factory()
    sequence = 0
    try:
        observation = dict(simulator.observe())
        run.observations.append(copy.deepcopy(observation))
        while sequence < max_steps:
            if bool(observation.get("done")) or bool(observation.get("finished")):
                run.terminated = True
                break
            choice = policy.choose(observation)
            if choice is None:
                command = _action("advance", observation, sequence, next_event=True)
            else:
                _, command = choice
                # Policies use sequence-independent construction internally;
                # the runner owns unique action IDs for replay/audit.
                command["action_id"] = f"{policy.name}-{sequence}"
            run.actions.append(copy.deepcopy(command))
            response = simulator.step(command)
            if not isinstance(response, Mapping):
                response = {"ok": False, "error": "simulator returned non-object"}
            response_dict = dict(response)
            run.responses.append(copy.deepcopy(response_dict))
            observation = dict(simulator.observe())
            run.observations.append(copy.deepcopy(observation))
            sequence += 1
        else:
            run.error = f"max_steps_exceeded:{max_steps}"
        if not run.terminated:
            # A bounded run that did not reach the horizon is explicitly
            # marked incomplete rather than silently assigning a good score.
            run.final_observation = copy.deepcopy(observation)
        else:
            run.final_observation = copy.deepcopy(observation)
    except Exception as exc:  # pragma: no cover - defensive adapter boundary
        run.error = f"{type(exc).__name__}: {exc}"
        try:
            run.final_observation = copy.deepcopy(dict(simulator.observe()))
        except Exception:
            run.final_observation = {}
    run.score = _public_score(run)
    return run


def run_baselines(factory: SimulatorFactory, *, names: Iterable[str] | None = None,
                  max_steps: int = 2000, random_seed: int = 17) -> dict[str, Any]:
    """Run the five calibration baselines and return a score matrix."""

    selected = list(names) if names is not None else list(POLICY_TYPES)
    runs: dict[str, BaselineRun] = {}
    for name in selected:
        if name not in POLICY_TYPES:
            raise ValueError(f"unknown baseline policy: {name}")
        policy = POLICY_TYPES[name](random_seed=random_seed)
        runs[name] = run_policy(factory, policy, max_steps=max_steps)
    matrix = {
        "baseline_version": "sffd-public-baselines/0.1",
        "score_semantics": {
            "policies": "public-observation proxy; not a Harbor leaderboard score",
            "authoritative_policies": "independent verifier replay; populated by the CLI",
        },
        "policies": {name: run.score for name, run in runs.items()},
        "runs": {name: run.public() for name, run in runs.items()},
    }
    return matrix


def authoritative_scores(scenario_path: Path, matrix: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Score baseline action logs through the same independent replay as Harbor.

    The public proxy is useful while a policy runs, but it intentionally lacks
    the hidden dynamics needed for an authoritative score.  This second pass
    replays only the recorded public actions against the frozen scenario and
    never serializes the verifier's private trace.
    """

    try:
        from verifier_lib import compute_score, replay_actions  # type: ignore
    except ImportError:  # pragma: no cover - package-style execution
        from .verifier_lib import compute_score, replay_actions  # type: ignore

    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    scores: dict[str, dict[str, Any]] = {}
    runs = matrix.get("runs", {})
    for name, run in runs.items() if isinstance(runs, Mapping) else []:
        if not isinstance(run, Mapping):
            continue
        actions = run.get("actions", [])
        if not isinstance(actions, list):
            actions = []
        replay = replay_actions(scenario_path, actions, include_trace=True)
        scores[str(name)] = compute_score(
            scenario, actions, trace=replay.get("trace"), integrity=replay
        )
    return scores


def acceptance_report(scores: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Evaluate the provisional single-scenario gates without hiding misses."""

    overall = {
        name: _number(score.get("overall"), -1.0)
        for name, score in scores.items()
    }
    checks = {
        "no_op_at_most_0_05": overall.get("no-op", 1.0) <= 0.05 + 1e-9,
        "random_feasible_at_most_0_20": overall.get("random-feasible", 1.0) <= 0.20 + 1e-9,
        "rolling_horizon_at_least_0_90": overall.get("rolling-horizon", -1.0) >= 0.90 - 1e-9,
        "nearest_unit_in_simple_rule_band": 0.25 <= overall.get("nearest-unit", -1.0) <= 0.55,
        "priority_nearest_in_simple_rule_band": 0.25 <= overall.get("priority-nearest", -1.0) <= 0.55,
    }
    return {
        "provisional_targets": {
            "no-op": {"max": 0.05},
            "random-feasible": {"max": 0.20},
            "rolling-horizon": {"min": 0.90},
            "simple-rule-band": {"min": 0.25, "max": 0.55},
        },
        "observed_overall": overall,
        "checks": checks,
        "all_numeric_targets_met": all(checks.values()),
        "not_evaluated_here": [
            "frontier-agent instability above 0.85",
            "ten hidden perturbation variants",
        ],
    }


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-steps", type=int, default=2000)
    parser.add_argument("--random-seed", type=int, default=17,
                        help="seed for random-feasible policy only; not simulator seed")
    args = parser.parse_args(argv)
    if not args.scenario.is_file():
        raise SystemExit(f"scenario not found: {args.scenario}")
    environment = args.scenario.parent / "environment"
    sys.path.insert(0, str(environment))
    from simulator import Simulator  # type: ignore

    matrix = run_baselines(lambda: Simulator(str(args.scenario)),
                           max_steps=args.max_steps, random_seed=args.random_seed)
    matrix["authoritative_policies"] = authoritative_scores(args.scenario, matrix)
    matrix["acceptance"] = acceptance_report(matrix["authoritative_policies"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(matrix, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    print(json.dumps({
        "authoritative_policies": matrix["authoritative_policies"],
        "acceptance": matrix["acceptance"],
    }, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())


__all__ = [
    "BaselineRun", "NoOpPolicy", "NearestUnitPolicy", "PriorityNearestPolicy",
    "RandomFeasiblePolicy", "RollingHorizonPolicy", "POLICY_TYPES", "run_policy",
    "acceptance_report", "authoritative_scores", "run_baselines",
]
