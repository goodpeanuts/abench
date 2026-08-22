from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "environment" / "simulator"))

from baselines import (  # noqa: E402
    POLICY_TYPES,
    RollingHorizonPolicy,
    _feasible_pairs,
    acceptance_report,
    authoritative_scores,
    run_baselines,
)
from simulator import Simulator  # noqa: E402


def fixture() -> dict:
    # Every duration is explicit latent data so this test remains independent
    # of the large SFFD calibration artifact.
    return {
        "schema_version": "test/v1",
        "scenario_id": "baseline-fixture",
        "seed": 999,
        "clock": {"t0": 0, "horizon_seconds": 300},
        "zones": ["A", "B"],
        "units": [
            {"unit_id": "M1", "unit_type": "MEDIC", "als": True, "initial_zone": "A"},
            {"unit_id": "B1", "unit_type": "BLS", "als": False, "initial_zone": "B"},
        ],
        "incidents": [
            {"incident_id": "I1", "arrival_s": 0, "call_type": "Medical Incident", "priority": "1",
             "requires_als": True, "zone": "B", "dispatchable": True,
             "latent": {"travel_time_s": 10, "service_duration_s": 30, "release_delay_s": 0}},
            {"incident_id": "I2", "arrival_s": 0, "call_type": "Medical Incident", "priority": "3",
             "requires_als": False, "zone": "A", "dispatchable": True,
             "latent": {"travel_time_s": 5, "service_duration_s": 20, "release_delay_s": 0}},
            {"incident_id": "F1", "arrival_s": 0, "call_type": "Structure Fire", "priority": "1",
             "requires_als": False, "zone": "A", "dispatchable": False},
        ],
        "calibration": {"reposition_time_s": 5},
    }


class PublicOnlyWrapper:
    """Reject privileged attribute reads to enforce the baseline contract."""

    def __init__(self, inner: Simulator) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        if name in {"scenario", "raw_scenario", "hidden", "_queue", "_action_cache", "_metrics"}:
            raise AssertionError(f"baseline attempted privileged read: {name}")
        return getattr(self._inner, name)

    def observe(self):
        value = self._inner.observe()
        # The wrapper returns exactly the redacted observation object.
        return copy.deepcopy(value)

    def step(self, command):
        return self._inner.step(command)


class BaselineTests(unittest.TestCase):
    def factory(self):
        scenario = fixture()
        return PublicOnlyWrapper(Simulator(scenario))

    def test_all_policies_use_only_public_observation(self) -> None:
        matrix = run_baselines(self.factory, max_steps=200)
        self.assertEqual(set(matrix["policies"]), set(POLICY_TYPES))
        for name, run in matrix["runs"].items():
            self.assertIsNone(run["error"], (name, run["error"]))
            self.assertTrue(run["terminated"], name)
            encoded = json.dumps(run["actions"] + run["observations"], sort_keys=True).lower()
            for forbidden in ("future_events", "future_incidents", "latent", "release_time", "seed", "oracle"):
                self.assertNotIn(forbidden, encoded, name)
            self.assertIn("overall", matrix["policies"][name])

    def test_random_policy_is_deterministic_without_simulator_seed(self) -> None:
        first = run_baselines(self.factory, names=["random-feasible"], random_seed=123, max_steps=200)
        second = run_baselines(self.factory, names=["random-feasible"], random_seed=123, max_steps=200)
        self.assertEqual(first["runs"]["random-feasible"]["actions"],
                         second["runs"]["random-feasible"]["actions"])
        self.assertEqual(first["policies"], second["policies"])

    def test_rolling_horizon_respects_als_constraint(self) -> None:
        sim = Simulator(fixture())
        observation = sim.observe()
        # At t=0 the fixture's arrivals are visible, so the public feasible
        # set must never pair the BLS unit with the ALS-only incident.
        pairs = _feasible_pairs(observation)
        self.assertTrue(pairs)
        self.assertFalse(any(i["incident_id"] == "I1" and not u.get("als") for i, u in pairs))
        policy = RollingHorizonPolicy()
        choice = policy.choose(observation)
        self.assertIsNotNone(choice)
        command = choice[1]
        if command.get("incident_id") == "I1":
            self.assertEqual(command.get("unit_id"), "M1")

    def test_public_action_log_replays(self) -> None:
        matrix = run_baselines(self.factory, names=["priority-nearest"], max_steps=200)
        actions = matrix["runs"]["priority-nearest"]["actions"]
        replay = Simulator(fixture())
        replay_result = replay.run_actions(actions)
        self.assertEqual(replay_result["responses"][-1].get("ok"), True)
        self.assertEqual(replay.observe()["public_state_hash"],
                         matrix["runs"]["priority-nearest"]["final_observation"]["public_state_hash"])

    def test_authoritative_score_pass_is_explicit(self) -> None:
        # Use the small fixture so this test exercises the same independent
        # replay path as Harbor without depending on the release-sized SFFD
        # artifact or a network fetch.
        matrix = run_baselines(self.factory, names=["priority-nearest"], max_steps=200)
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            scenario_path = Path(directory) / "scenario.json"
            scenario_path.write_text(json.dumps(fixture()), encoding="utf-8")
            scores = authoritative_scores(scenario_path, matrix)
        self.assertIn("priority-nearest", scores)
        self.assertIn("overall", scores["priority-nearest"])
        report = acceptance_report(scores)
        self.assertIn("all_numeric_targets_met", report)
        self.assertIsInstance(report["all_numeric_targets_met"], bool)


if __name__ == "__main__":
    unittest.main()
