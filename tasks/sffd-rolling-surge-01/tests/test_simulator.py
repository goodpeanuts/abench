from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "environment" / "simulator"))
from simulator import Simulator
from verifier_lib import compute_score, replay_actions, verify_state_dir


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = ROOT / "scenario.json"


class SimulatorTests(unittest.TestCase):
    def make_sim(self) -> Simulator:
        return Simulator(str(SCENARIO_PATH))

    def advance_to(self, simulator: Simulator, predicate, limit: int = 100) -> dict:
        for index in range(limit):
            observation = simulator.observe()
            if predicate(observation):
                return observation
            result = simulator.step({
                "command": "advance",
                "state_version": simulator.state_version,
                "action_id": f"advance-{simulator.state_version}-{index}",
            })
            self.assertTrue(result["ok"], result)
        self.fail("predicate was not reached before the event horizon")

    def test_initial_observation_redacts_future_and_latent_state(self) -> None:
        simulator = self.make_sim()
        observation = simulator.observe()
        self.assertEqual(observation["incidents"], [])
        encoded = json.dumps(observation, sort_keys=True).lower()
        for forbidden in ("future_events", "future_incidents", "release_time", "available_time", "seed", "latent"):
            self.assertNotIn(forbidden, encoded)
        self.assertTrue(all("busy_until_s" not in unit for unit in observation["units"]))
        self.assertIn("public_state_hash", observation)
        self.assertNotIn("state_hash", observation)

    def test_public_state_hash_cannot_fingerprint_hidden_world(self) -> None:
        original = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
        altered = copy.deepcopy(original)
        altered["seed"] = int(original["seed"]) + 1
        # At reset no incident is visible; changing a future event must alter
        # the private verifier commitment but not the public observation hash.
        altered["incidents"][-1]["arrival_s"] += 1
        left = Simulator(original)
        right = Simulator(altered)
        self.assertNotEqual(left.state_hash(), right.state_hash())
        self.assertEqual(left.observe()["public_state_hash"],
                         right.observe()["public_state_hash"])

    def test_hidden_incident_probe_does_not_reveal_future_ids(self) -> None:
        simulator = self.make_sim()
        # I002 is a real future incident in the frozen scenario; a made-up ID
        # must receive the same public error shape and message.
        future = simulator.step({
            "command": "dispatch", "incident_id": "I002", "unit_id": "M505",
            "state_version": 0, "action_id": "probe-future",
        })
        unknown = simulator.step({
            "command": "dispatch", "incident_id": "I999", "unit_id": "M505",
            "state_version": 0, "action_id": "probe-unknown",
        })
        self.assertFalse(future["ok"])
        self.assertFalse(unknown["ok"])
        self.assertEqual(future["code"], unknown["code"])
        self.assertEqual(future["error"], unknown["error"])

    def test_service_and_release_lookup_keeps_priority_strata(self) -> None:
        simulator = self.make_sim()
        unit = simulator.units["M505"]
        priority_one = simulator.incidents["I004"]
        priority_two = simulator.incidents["I005"]
        service_one = simulator._lookup_calibration(
            simulator.scenario["calibration"], "service", unit, priority_one
        )
        service_two = simulator._lookup_calibration(
            simulator.scenario["calibration"], "service", unit, priority_two
        )
        release_one = simulator._lookup_calibration(
            simulator.scenario["calibration"], "release", unit, priority_one
        )
        release_two = simulator._lookup_calibration(
            simulator.scenario["calibration"], "release", unit, priority_two
        )
        calibration = simulator.scenario["calibration"]
        self.assertEqual(service_one, calibration["service_duration_s"]["MEDIC|1"])
        self.assertEqual(service_two, calibration["service_duration_s"]["MEDIC|2"])
        self.assertEqual(release_one, calibration["release_delay_s"]["MEDIC|1"])
        self.assertEqual(release_two, calibration["release_delay_s"]["MEDIC|2"])

    def test_sffd_calibration_requires_usable_numeric_distribution(self) -> None:
        scenario = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
        scenario["calibration"]["travel_time_s"] = {"default": []}
        with self.assertRaisesRegex(ValueError, "no usable numeric distribution"):
            Simulator(scenario)

    def test_sffd_scenario_requires_seed_and_sample_counts(self) -> None:
        scenario = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
        scenario.pop("seed")
        with self.assertRaisesRegex(ValueError, "frozen seed"):
            Simulator(scenario)
        scenario = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
        scenario["calibration"].pop("sample_counts")
        with self.assertRaisesRegex(ValueError, "sample_counts"):
            Simulator(scenario)

    def test_reposition_zone_validation_does_not_probe_hidden_catalog(self) -> None:
        def issue(zone: str, action_id: str) -> dict:
            simulator = self.make_sim()
            return simulator.step({
                "command": "reposition", "unit_id": "M505", "zone": zone,
                "state_version": 0, "action_id": action_id,
            })

        # B:B10 is a zone used only by a future incident at reset, while
        # B:ZZ is not present in the candidate scenario at all.  Both are
        # syntactically valid coarse labels and therefore must not diverge.
        future_zone = issue("B:B10", "reposition-future-zone")
        arbitrary_zone = issue("B:ZZ", "reposition-arbitrary-zone")
        self.assertEqual(future_zone["ok"], arbitrary_zone["ok"])
        self.assertFalse(future_zone["ok"])

    def test_rejected_conflicting_action_preserves_public_observation(self) -> None:
        simulator = self.make_sim()
        first = simulator.step({"command": "advance", "state_version": 0, "action_id": "same"})
        self.assertTrue(first["ok"])
        before = simulator.observe()
        conflict = simulator.step({"command": "finish", "state_version": 1, "action_id": "same"})
        self.assertFalse(conflict["ok"])
        self.assertEqual(simulator.observe(), before)

    def test_state_version_and_action_idempotency(self) -> None:
        simulator = self.make_sim()
        first = simulator.step({"command": "advance", "state_version": 0, "action_id": "a1"})
        self.assertTrue(first["ok"], first)
        before = simulator.state_hash()
        repeated = simulator.step({"command": "advance", "state_version": 0, "action_id": "a1"})
        self.assertEqual(repeated, first)
        self.assertEqual(simulator.state_hash(), before)
        conflict = simulator.step({"command": "finish", "state_version": 1, "action_id": "a1"})
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["code"], "action_id_conflict")
        stale = simulator.step({"command": "advance", "state_version": 0, "action_id": "a2"})
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["code"], "action_rejected")
        self.assertEqual(simulator.state_version, 1)

    def test_conflicting_action_id_is_logged_without_replacing_cache(self) -> None:
        simulator = self.make_sim()
        first = simulator.step({"command": "advance", "state_version": 0, "action_id": "same"})
        self.assertTrue(first["ok"], first)
        before = simulator.state_hash()
        conflict = simulator.step({"command": "finish", "state_version": 1, "action_id": "same"})
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["code"], "action_id_conflict")
        self.assertEqual(simulator.state_hash(), before)
        self.assertEqual(len(simulator.export_trace()["actions"]), 2)
        # An identical retry must still return the original successful result,
        # even though the rejected conflict is present in the audit log.
        repeated = simulator.step({"command": "advance", "state_version": 0, "action_id": "same"})
        self.assertEqual(repeated, first)

    def test_unexpected_transition_input_is_structured_and_atomic(self) -> None:
        simulator = self.make_sim()
        before = simulator.state_hash()
        def explode(_command):
            raise RuntimeError("synthetic transition failure")
        simulator._advance = explode
        result = simulator.step({"command": "advance", "action_id": "unexpected"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "transition_error")
        self.assertEqual(simulator.state_hash(), before)
        self.assertEqual(simulator.state_version, 0)

    def test_dispatch_release_and_reuse(self) -> None:
        simulator = self.make_sim()
        observation = self.advance_to(
            simulator,
            lambda value: any(i["incident_id"] == "I002" and i["status"] == "QUEUED" for i in value["incidents"]),
        )
        incident = next(i for i in observation["incidents"] if i["incident_id"] == "I002")
        unit = next(u for u in observation["units"] if u["status"] == "AVAILABLE" and u["controllable"])
        dispatch = simulator.step({
            "command": "dispatch",
            "incident_id": incident["incident_id"],
            "unit_id": unit["unit_id"],
            "state_version": simulator.state_version,
            "action_id": "dispatch-1",
        })
        self.assertTrue(dispatch["ok"], dispatch)
        # Wait until this particular unit is available again and a later
        # incident is visible; a legal reuse must not be treated as booking.
        def ready(value: dict) -> bool:
            return (
                any(u["unit_id"] == unit["unit_id"] and u["status"] == "AVAILABLE" for u in value["units"])
                and any(i["dispatchable"] and i["status"] == "QUEUED" and i["incident_id"] != "I002" for i in value["incidents"])
            )
        observation = self.advance_to(simulator, ready, limit=200)
        later = next(i for i in observation["incidents"] if i["dispatchable"] and i["status"] == "QUEUED" and i["incident_id"] != "I002")
        reused = simulator.step({
            "command": "dispatch",
            "incident_id": later["incident_id"],
            "unit_id": unit["unit_id"],
            "state_version": simulator.state_version,
            "action_id": "dispatch-2",
        })
        self.assertTrue(reused["ok"], reused)

    def test_simulator_reward_has_no_strict_pass(self) -> None:
        # The simulator-level reward is diagnostics only; the strict pass/fail
        # authority lives exclusively in tests/verifier_lib.py.  Guard against
        # a second, drifting definition reappearing here.
        simulator = self.make_sim()
        result = simulator.run_actions([
            {"command": "observe"},
        ])
        self.assertNotIn("strict_pass", result["reward"])

    def test_replay_is_bitwise_deterministic_twenty_runs(self) -> None:
        actions = []
        for index in range(30):
            actions.append({"command": "advance", "action_id": f"a{index}"})
        hashes = []
        traces = []
        for _ in range(20):
            simulator = self.make_sim()
            result = simulator.run_actions(actions)
            hashes.append(simulator.state_hash())
            traces.append(json.dumps(result["trace"], sort_keys=True, separators=(",", ":")))
        self.assertEqual(len(set(hashes)), 1)
        self.assertEqual(len(set(traces)), 1)

    def test_checkpoint_is_bound_to_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            simulator = Simulator(str(SCENARIO_PATH), state_dir=state_dir)
            simulator.step({"command": "advance", "action_id": "a1"})
            altered = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
            altered["seed"] = int(altered["seed"]) + 1
            altered_path = state_dir / "altered.json"
            altered_path.write_text(json.dumps(altered), encoding="utf-8")
            with self.assertRaises(ValueError):
                Simulator(str(altered_path), state_dir=state_dir, load_existing=True)

            metadata_only = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
            metadata_only.setdefault("metadata", {})["tampered_marker"] = True
            metadata_path = state_dir / "metadata-altered.json"
            metadata_path.write_text(json.dumps(metadata_only), encoding="utf-8")
            with self.assertRaises(ValueError):
                Simulator(str(metadata_path), state_dir=state_dir, load_existing=True)

    def test_noop_state_is_replayable_with_empty_action_log(self) -> None:
        """A no-op trial still emits complete verifier evidence.

        Harbor collects sidecar files after the Agent stops.  Keeping an empty
        ``actions.jsonl`` is therefore important: a missing file would look
        like a failed artifact transfer instead of a valid no-op trajectory.
        """

        scenario = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            Simulator(str(SCENARIO_PATH), state_dir=state_dir)
            action_log = state_dir / "actions.jsonl"
            self.assertTrue(action_log.is_file())
            self.assertEqual(action_log.read_text(encoding="utf-8"), "")
            self.assertTrue(verify_state_dir(state_dir, scenario=scenario)["ok"])

            replay = replay_actions(SCENARIO_PATH, [], state_dir=state_dir,
                                    include_trace=True)
            self.assertTrue(replay["ok"], replay)
            self.assertTrue(replay["log_integrity"]["source"]["ok"])
            score = compute_score(scenario, [], replay["trace"], replay)
            self.assertEqual(score["integrity"], 1.0)

    def test_append_only_log_tamper_is_detected(self) -> None:
        scenario = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            simulator = Simulator(str(SCENARIO_PATH), state_dir=state_dir)
            simulator.step({"command": "advance", "action_id": "a1"})
            self.assertTrue(verify_state_dir(state_dir, scenario=scenario)["ok"])
            rng_manifest = state_dir / "rng_manifest.json"
            self.assertTrue(rng_manifest.is_file())
            rng_manifest.write_text("{}\n", encoding="utf-8")
            rng_audit = verify_state_dir(state_dir, scenario=scenario)
            self.assertFalse(rng_audit["ok"])
            self.assertIn("rng_manifest_mismatch", rng_audit["errors"])
            # Restore the file before exercising event-log tamper detection.
            simulator._write_manifest()
            event_log = state_dir / "event_log.jsonl"
            rows = event_log.read_text(encoding="utf-8").splitlines()
            first = json.loads(rows[0])
            first["time_s"] = float(first.get("time_s", 0)) + 1
            rows[0] = json.dumps(first, sort_keys=True)
            event_log.write_text("\n".join(rows) + "\n", encoding="utf-8")
            audit = verify_state_dir(state_dir, scenario=scenario)
            self.assertFalse(audit["ok"])
            self.assertIn("event_log_hash_mismatch", audit["errors"])
            self.assertEqual(audit["first_divergence"]["artifact"], "event_log")
            # Truncating the append-only file completely must be detected too;
            # an empty parsed list is still a tampered artifact.
            event_log.write_text("", encoding="utf-8")
            truncated_audit = verify_state_dir(state_dir, scenario=scenario)
            self.assertFalse(truncated_audit["ok"])
            self.assertIn("event_log_hash_mismatch", truncated_audit["errors"])

    def test_failed_transition_does_not_leave_event_log_junk(self) -> None:
        scenario = {
            "schema_version": "test/v1",
            "scenario_id": "rollback-fixture",
            "seed": 1,
            "clock": {"t0": 0, "horizon_seconds": 30},
            "zones": ["A"],
            "units": [{"unit_id": "U1", "unit_type": "MEDIC", "als": True,
                        "initial_zone": "A", "initial_status": "AVAILABLE"}],
            "incidents": [{"incident_id": "F1", "arrival_s": 0,
                            "call_type": "Fire", "priority": "1", "dispatchable": False,
                            "zone": "A"}],
            # The event reaches a roster unit but intentionally has no release
            # calibration.  It should fail atomically when advanced.
            "background_events": [{"event_id": "B1", "incident_id": "F1",
                                    "unit_id": "U1", "dispatch_s": 5,
                                    "arrival_s": 5, "kind": "external_response"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            simulator = Simulator(scenario, state_dir=state_dir)
            result = simulator.step({"command": "advance", "action_id": "bad-advance"})
            self.assertFalse(result["ok"])
            self.assertTrue(verify_state_dir(state_dir, scenario=scenario)["ok"])


if __name__ == "__main__":
    unittest.main()
