from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "environment" / "simulator"))

from simulator import Simulator
from verifier_lib import compute_score


def terminal_scenario() -> dict:
    return {
        "schema_version": "test/v1",
        "scenario_id": "terminal-padding-private-id",
        "seed": 7,
        "clock": {"t0": 0, "horizon_seconds": 10},
        "zones": ["A"],
        "units": [{
            "unit_id": "M1",
            "unit_type": "MEDIC",
            "als": True,
            "initial_zone": "A",
            "initial_status": "AVAILABLE",
        }],
        "incidents": [],
    }


def score_scenario() -> dict:
    return {
        "schema_version": "test/v1",
        "scenario_id": "score-padding-private-id",
        "seed": 9,
        "clock": {"t0": 0, "horizon_seconds": 1200},
        "zones": ["A"],
        "units": [{
            "unit_id": "M1",
            "unit_type": "MEDIC",
            "als": True,
            "initial_zone": "A",
            "initial_status": "AVAILABLE",
        }],
        "incidents": [{
            "incident_id": "I1",
            "arrival_s": 0,
            "call_type": "Medical Incident",
            "priority": "1",
            "zone": "A",
            "dispatchable": True,
            "requires_als": True,
        }],
    }


class TerminalPaddingTests(unittest.TestCase):
    def test_terminal_control_calls_are_loggable_but_state_idempotent(self) -> None:
        horizon = Simulator(terminal_scenario())
        first_advance = horizon.step({
            "command": "advance", "state_version": 0, "action_id": "reach-horizon",
        })
        self.assertTrue(first_advance["ok"], first_advance)
        self.assertTrue(horizon.done)
        terminal_version = horizon.state_version
        terminal_hash = horizon.state_hash()
        terminal_public = horizon.observe()
        self.assertNotIn("scenario_id", terminal_public)

        for index in range(3):
            padded = horizon.step({
                "command": "advance",
                "state_version": terminal_version,
                "action_id": f"padding-advance-{index}",
            })
            self.assertTrue(padded["ok"], padded)
            self.assertEqual(padded["state_version"], terminal_version)
            self.assertEqual(horizon.state_hash(), terminal_hash)
            self.assertEqual(horizon.observe(), terminal_public)

        finished = Simulator(terminal_scenario())
        first_finish = finished.step({
            "command": "finish", "reason": "done", "state_version": 0,
            "action_id": "first-finish",
        })
        self.assertTrue(first_finish["ok"], first_finish)
        finished_version = finished.state_version
        finished_hash = finished.state_hash()
        for index in range(3):
            padded = finished.step({
                "command": "finish", "reason": "ignored",
                "state_version": finished_version,
                "action_id": f"padding-finish-{index}",
            })
            self.assertTrue(padded["ok"], padded)
            self.assertEqual(padded["state_version"], finished_version)
            self.assertEqual(finished.state_hash(), finished_hash)

        # A different terminal command may be rejected, but must still leave
        # the logical world unchanged.
        rejected = finished.step({
            "command": "advance", "state_version": finished_version,
            "action_id": "advance-after-finish",
        })
        self.assertFalse(rejected["ok"])
        self.assertEqual(finished.state_version, finished_version)
        self.assertEqual(finished.state_hash(), finished_hash)

    def test_padding_zero_ten_or_hundred_cannot_raise_reward(self) -> None:
        scenario = score_scenario()
        trace = {"events": [
            {"event": "incident_arrival", "incident_id": "I1", "time": 0},
            {"event": "unit_on_scene", "incident_id": "I1", "unit_id": "M1", "time": 100},
            {"event": "incident_complete", "incident_id": "I1", "unit_id": "M1", "time": 200},
        ]}
        base_actions = [
            {"action": "dispatch", "args": {
                "incident_id": "I1", "unit_id": "M1", "action_id": "valid-dispatch",
            }},
            {"action": "dispatch", "args": {
                "incident_id": "missing", "unit_id": "missing", "action_id": "invalid-dispatch",
            }},
        ]

        scores = []
        for padding_count in (0, 10, 100):
            padding = []
            for index in range(padding_count):
                command = "advance" if index % 2 == 0 else "finish"
                padding.append({
                    "action": command,
                    "args": {"action_id": f"padding-{padding_count}-{index}"},
                })
            scores.append(compute_score(
                scenario, base_actions + padding, trace, {"ok": True},
            ))

        baseline = scores[0]
        for padded in scores[1:]:
            self.assertLessEqual(padded["overall"], baseline["overall"])
            self.assertEqual(padded["safety"], baseline["safety"])
            self.assertEqual(padded["efficiency"], baseline["efficiency"])
            self.assertEqual(
                padded["evidence"]["scoring_attempt_denominator"],
                baseline["evidence"]["scoring_attempt_denominator"],
            )


if __name__ == "__main__":
    unittest.main()
