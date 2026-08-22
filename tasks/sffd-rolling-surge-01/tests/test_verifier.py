from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from verifier_lib import (
    _public_integrity,
    audit_actions,
    canonical_json,
    compute_score,
    first_trace_divergence,
    load_jsonl,
    hidden_state_violations,
    normalize_action,
    replay_actions,
    sha256_json,
)
from score import main as score_main


REWARD_KEYS = {
    "response", "coverage", "safety", "adaptation", "efficiency",
    "integrity", "overall", "strict_pass",
}


def scenario_fixture() -> dict:
    return {
        "schema_version": "sffd-scenario/v1",
        "scenario_id": "unit-test",
        "seed": 17,
        "clock": {"start_s": 0, "horizon_s": 1200},
        "zones": ["A", "B"],
        "units": [
            {"unit_id": "M1", "unit_type": "MEDIC", "capability": "ALS", "zone": "A", "initial_status": "FREE"},
            {"unit_id": "B1", "unit_type": "BLS", "capability": "BLS", "zone": "B", "initial_status": "FREE"},
        ],
        "incidents": [
            {"incident_id": "I1", "arrival_s": 0, "priority": "1", "zone": "B", "dispatchable": True},
            {"incident_id": "I2", "arrival_s": 120, "priority": "3", "zone": "A", "dispatchable": True},
        ],
    }


class VerifierTests(unittest.TestCase):
    def _one_action_sidecar(self, root: Path) -> tuple[Path, Path, dict]:
        from verifier_lib import load_simulator_class

        scenario_path = Path(__file__).resolve().parents[1] / "scenario.json"
        state_dir = root / "ems-state"
        simulator_class = load_simulator_class()
        self.assertIsNotNone(simulator_class)
        simulator = simulator_class(str(scenario_path), state_dir=state_dir)
        command = {
            "command": "advance",
            "state_version": simulator.state_version,
            "action_id": "binding-advance",
        }
        response = simulator.step(command)
        self.assertTrue(response["ok"], response)
        return scenario_path, state_dir, command

    def test_sidecar_flat_command_log_preserves_dispatch_arguments(self) -> None:
        """A sidecar action record must be replayable by the verifier.

        The simulator's append-only log wraps each command as a flat mapping
        under ``command``.  This is the format Harbor transfers from the
        private sidecar, so exercise that exact path instead of only testing
        hand-authored ``{"action": ..., "args": ...}`` records.
        """

        from pathlib import Path

        task_root = Path(__file__).resolve().parents[1]
        scenario_path = task_root / "scenario.json"
        # Import through the verifier's normal loader so this test follows
        # the same module discovery path as a separate Harbor verifier image.
        from verifier_lib import load_simulator_class

        simulator_class = load_simulator_class()
        self.assertIsNotNone(simulator_class)

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "ems-state"
            simulator = simulator_class(str(scenario_path), state_dir=state_dir)

            # Reveal the first dispatchable incident through ordinary logical
            # time advancement, then issue one legal dispatch.  The exact
            # unit/incident IDs are discovered from the public observation.
            for index in range(20):
                observation = simulator.observe()
                queued = [
                    item for item in observation["incidents"]
                    if item.get("dispatchable") and item.get("status") == "QUEUED"
                ]
                available = [
                    item for item in observation["units"]
                    if item.get("controllable") and item.get("status") == "AVAILABLE"
                ]
                if queued and available:
                    incident_id = queued[0]["incident_id"]
                    unit_id = available[0]["unit_id"]
                    response = simulator.step({
                        "command": "dispatch",
                        "incident_id": incident_id,
                        "unit_id": unit_id,
                        "state_version": simulator.state_version,
                        "action_id": "sidecar-dispatch",
                    })
                    self.assertTrue(response["ok"], response)
                    break
                response = simulator.step({
                    "command": "advance",
                    "state_version": simulator.state_version,
                    "action_id": f"sidecar-advance-{index}",
                })
                self.assertTrue(response["ok"], response)
            else:
                self.fail("scenario did not expose a dispatchable incident")

            # Let the selected call reach service completion so the score
            # assertion below verifies an actual outcome, not just a parsed
            # command.  Advancing is deterministic and does not reveal any
            # private release time to the action log.
            # The frozen surge includes background calls and long empirical
            # service/release tails.  Bound the loop by the public horizon,
            # rather than an arbitrary event count, so this regression test
            # remains valid when the event mix changes.
            horizon_s = int(float(simulator.observe().get("horizon_seconds", 7200)))
            completed = False
            for index in range(max(100, horizon_s + 20)):
                current = simulator.observe()
                current_incident = next(
                    item for item in current["incidents"]
                    if item.get("incident_id") == incident_id
                )
                if current_incident.get("status") in {"COMPLETED", "AVAILABLE", "DONE"}:
                    completed = True
                    break
                if current.get("done") or current.get("finished"):
                    break
                response = simulator.step({
                    "command": "advance",
                    "state_version": simulator.state_version,
                    "action_id": f"sidecar-post-dispatch-{index}",
                })
                self.assertTrue(response["ok"], response)
            self.assertTrue(completed, current_incident)

            sidecar_rows = load_jsonl(state_dir / "actions.jsonl")
            dispatch_row = next(row for row in sidecar_rows if row.get("record_type") == "action"
                                and row.get("command", {}).get("command") == "dispatch")
            normalized = normalize_action(dispatch_row)
            self.assertEqual(normalized["action"], "dispatch")
            self.assertEqual(normalized["args"]["incident_id"], incident_id)
            self.assertEqual(normalized["args"]["unit_id"], unit_id)

            replay = replay_actions(scenario_path, sidecar_rows, state_dir, include_trace=True)
            self.assertTrue(replay["ok"], replay)
            self.assertTrue(any(
                event.get("kind") == "dispatch"
                and event.get("public", {}).get("incident_id") == incident_id
                and event.get("public", {}).get("unit_id") == unit_id
                for event in replay["trace"].get("events", [])
            ))

            # Run the same sidecar JSONL through the public score entry point;
            # this guards the Harbor path that prefers /tmp/ems-state logs.
            actions_path = state_dir / "sidecar-actions.jsonl"
            actions_path.write_text(
                "\n".join(json.dumps(row, sort_keys=True) for row in sidecar_rows) + "\n",
                encoding="utf-8",
            )
            reward_path = state_dir / "reward.json"
            self.assertEqual(score_main([
                "--scenario", str(scenario_path),
                "--actions", str(actions_path),
                "--state-dir", str(state_dir),
                "--output", str(reward_path),
            ]), 0)
            reward = json.loads(reward_path.read_text(encoding="utf-8"))
            self.assertEqual(reward["integrity"], 1.0)
            self.assertGreater(reward["coverage"], 0.0)

    def test_replay_rejects_scored_actions_missing_from_sidecar_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario_path, state_dir, _ = self._one_action_sidecar(Path(directory))

            replay = replay_actions(scenario_path, [], state_dir)

            self.assertFalse(replay["ok"], replay)
            binding = replay["log_integrity"]["action_binding"]
            self.assertFalse(binding["ok"])
            self.assertEqual(binding["scored_action_count"], 0)
            self.assertEqual(binding["source_action_count"], 1)

    def test_replay_accepts_semantically_equivalent_command_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario_path, state_dir, command = self._one_action_sidecar(Path(directory))
            source_files_before = {
                path.name: path.read_bytes()
                for path in state_dir.iterdir()
                if path.is_file()
            }
            equivalent = [{
                "action": command["command"],
                "args": {
                    key: value
                    for key, value in command.items()
                    if key != "command"
                },
            }]

            replay = replay_actions(scenario_path, equivalent, state_dir)

            self.assertTrue(replay["ok"], replay)
            self.assertTrue(replay["log_integrity"]["action_binding"]["ok"])
            self.assertTrue(replay["log_integrity"]["final_state_binding"]["ok"])
            self.assertFalse((state_dir / "replay-a").exists())
            self.assertFalse((state_dir / "replay-b").exists())
            self.assertEqual(
                source_files_before,
                {
                    path.name: path.read_bytes()
                    for path in state_dir.iterdir()
                    if path.is_file()
                },
                "independent replay must not mutate authoritative sidecar artifacts",
            )

    def test_sidecar_auto_version_command_replays_from_nested_envelope(self) -> None:
        from verifier_lib import load_simulator_class

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scenario_path = Path(__file__).resolve().parents[1] / "scenario.json"
            state_dir = root / "ems-state"
            simulator_class = load_simulator_class()
            self.assertIsNotNone(simulator_class)
            simulator = simulator_class(str(scenario_path), state_dir=state_dir)

            response = simulator.step({"command": "advance"})
            self.assertTrue(response["ok"], response)
            sidecar_rows = load_jsonl(state_dir / "actions.jsonl")
            self.assertNotIn("state_version", sidecar_rows[0]["command"])

            replay = replay_actions(scenario_path, sidecar_rows, state_dir)

            self.assertTrue(replay["ok"], replay)
            self.assertTrue(replay["log_integrity"]["final_state_binding"]["ok"])

    def test_replay_rejects_source_final_state_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario_path, state_dir, command = self._one_action_sidecar(Path(directory))
            manifest_path = state_dir / "replay_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["final_state_hash"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            replay = replay_actions(scenario_path, [command], state_dir)

            self.assertFalse(replay["ok"], replay)
            self.assertTrue(replay["log_integrity"]["action_binding"]["ok"])
            self.assertFalse(replay["log_integrity"]["final_state_binding"]["ok"])

    def test_authoritative_replay_trace_drives_score_without_leaking_private_trace(self) -> None:
        scenario = {
            "schema_version": "test/v1",
            "scenario_id": "replay-score-fixture",
            "seed": 17,
            "clock": {"t0": 0, "horizon_seconds": 100},
            "zones": ["A"],
            "units": [{"unit_id": "M1", "unit_type": "MEDIC", "als": True,
                       "initial_zone": "A"}],
            "incidents": [{"incident_id": "I1", "arrival_s": 0,
                           "priority": "1", "zone": "A", "dispatchable": True,
                           "requires_als": True,
                           "latent": {"travel_time_s": 10,
                                      "service_duration_s": 20,
                                      "release_delay_s": 0}}],
        }
        actions = [
            {"action": "dispatch", "args": {"incident_id": "I1", "unit_id": "M1",
                                               "state_version": 0, "action_id": "d1"}},
            {"action": "advance", "args": {"state_version": 1, "action_id": "a1"}},
            {"action": "advance", "args": {"state_version": 2, "action_id": "a2"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scenario_path = root / "scenario.json"
            actions_path = root / "actions.json"
            reward_path = root / "reward.json"
            scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
            actions_path.write_text(json.dumps(actions), encoding="utf-8")

            redacted = replay_actions(scenario_path, actions)
            self.assertNotIn("trace", redacted)
            replay = replay_actions(scenario_path, actions, include_trace=True)
            self.assertIn("trace", replay)
            result = compute_score(scenario, actions, replay["trace"], replay)
            self.assertGreater(result["response"], 0.0)
            self.assertGreater(result["coverage"], 0.0)

            self.assertEqual(score_main([
                "--scenario", str(scenario_path), "--actions", str(actions_path),
                "--output", str(reward_path),
            ]), 0)
            reward = json.loads(reward_path.read_text(encoding="utf-8"))
            self.assertGreater(reward["response"], 0.0)
            self.assertGreater(reward["coverage"], 0.0)
            self.assertEqual(set(reward), REWARD_KEYS)
            self.assertTrue(all(type(value) in {int, float} for value in reward.values()))

            details = json.loads((root / "details.json").read_text(encoding="utf-8"))
            encoded_details = json.dumps(details, sort_keys=True).lower()
            self.assertNotIn('"scenario_id":', encoded_details)
            self.assertNotIn('"trace":', encoded_details)
            self.assertNotIn('"latent":', encoded_details)
            self.assertNotIn('"event_log":', encoded_details)
            self.assertNotIn('"normalized_actions":', encoded_details)
            self.assertNotIn('"calibration":', encoded_details)
            self.assertNotIn('"source_rowid":', encoded_details)
            self.assertNotIn('"response_by_incident":', encoded_details)
            self.assertNotIn('"incident_id":', encoded_details)
            self.assertNotIn('"arrival_s":', encoded_details)
            self.assertNotIn('"priority":', encoded_details)

    def test_malformed_action_file_returns_structured_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scenario_path = root / "scenario.json"
            actions_path = root / "actions.json"
            reward_path = root / "reward.json"
            scenario_path.write_text(json.dumps(scenario_fixture()), encoding="utf-8")
            actions_path.write_text("{not-json\n", encoding="utf-8")
            self.assertEqual(score_main([
                "--scenario", str(scenario_path), "--actions", str(actions_path),
                "--output", str(reward_path),
            ]), 0)
            reward = json.loads(reward_path.read_text(encoding="utf-8"))
            self.assertEqual(reward["overall"], 0.0)
            self.assertEqual(reward["integrity"], 0.0)
            self.assertEqual(set(reward), REWARD_KEYS)
            details = json.loads((root / "details.json").read_text(encoding="utf-8"))
            self.assertTrue(any("actions_load_error" in item for item in details.get("errors", [])))

    def test_missing_scenario_writes_zero_reward_and_separate_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reward_path = root / "reward.json"
            details_path = root / "diagnostics.json"
            self.assertEqual(score_main([
                "--scenario", str(root / "missing.json"),
                "--output", str(reward_path),
                "--details-output", str(details_path),
            ]), 2)
            reward = json.loads(reward_path.read_text(encoding="utf-8"))
            details = json.loads(details_path.read_text(encoding="utf-8"))
            self.assertEqual(set(reward), REWARD_KEYS)
            self.assertTrue(all(type(value) in {int, float} for value in reward.values()))
            self.assertTrue(all(value == 0 for value in reward.values()))
            self.assertTrue(any("scenario not found" in item for item in details.get("errors", [])))

    def test_replay_rejections_are_scored_when_tool_responses_are_omitted(self) -> None:
        scenario = scenario_fixture()
        actions = [{"action": "dispatch", "args": {
            "incident_id": "I1", "unit_id": "M1", "action_id": "bad"
        }}]
        trace = {"actions": [{"action_id": "bad", "ok": False,
                               "command": actions[0]}], "events": []}
        result = compute_score(scenario, actions, trace, {"ok": True})
        self.assertEqual(len(result["evidence"]["simulator_rejected_actions"]), 1)
        self.assertLess(result["safety"], 1.0)

    def test_score_shape_and_weighted_overall(self) -> None:
        scenario = scenario_fixture()
        actions = [
            {"action": "dispatch", "args": {"incident_id": "I1", "unit_id": "M1", "state_version": 0, "action_id": "a1"}},
            {"action": "advance", "args": {"action_id": "a2"}},
            {"action": "dispatch", "args": {"incident_id": "I2", "unit_id": "B1", "state_version": 1, "action_id": "a3"}},
        ]
        trace = {"events": [
            {"event": "incident_arrival", "incident_id": "I1", "time": 0},
            {"event": "unit_on_scene", "incident_id": "I1", "unit_id": "M1", "time": 300},
            {"event": "incident_complete", "incident_id": "I1", "time": 600},
            {"event": "incident_arrival", "incident_id": "I2", "time": 120},
            {"event": "unit_on_scene", "incident_id": "I2", "unit_id": "B1", "time": 300},
            {"event": "incident_complete", "incident_id": "I2", "time": 480},
        ]}
        replay = {"ok": True, "simulator_available": False, "state_hash": "x"}
        result = compute_score(scenario, actions, trace, replay)
        for key in ("response", "coverage", "safety", "adaptation", "efficiency", "integrity", "overall"):
            self.assertIn(key, result)
            self.assertGreaterEqual(result[key], 0.0)
            self.assertLessEqual(result[key], 1.0)
        expected = sum(result[key] * weight for key, weight in {
            "response": 0.40,
            "coverage": 0.20,
            "safety": 0.15,
            "adaptation": 0.10,
            "efficiency": 0.10,
            "integrity": 0.05,
        }.items())
        self.assertAlmostEqual(result["overall"], round(expected, 6))

    def test_illegal_actions_and_duplicate_action_id(self) -> None:
        scenario = scenario_fixture()
        actions = [
            {"action": "dispatch", "args": {"incident_id": "I1", "unit_id": "M1", "action_id": "same"}},
            {"action": "dispatch", "args": {"incident_id": "I2", "unit_id": "M1", "action_id": "same"}},
            {"action": "dispatch", "args": {"incident_id": "missing", "unit_id": "M9", "action_id": "bad"}},
        ]
        audit = audit_actions(scenario, actions)
        reasons = {item["reason"] for item in audit["invalid_actions"]}
        self.assertIn("duplicate_action_id", reasons)
        self.assertIn("unit_double_booked", reasons)
        self.assertIn("unknown_incident", reasons)
        self.assertIn("unknown_unit", reasons)
        # A command-only trajectory proves an invalid attempt, not that the
        # simulator committed an unrecoverable state transition.
        self.assertEqual(audit["catastrophic_violations"], [])

    def test_rejected_conflicting_write_is_not_a_hard_failure(self) -> None:
        scenario = scenario_fixture()
        actions = [
            {"action": "dispatch", "args": {
                "incident_id": "I1", "unit_id": "M1", "action_id": "same",
            }, "response": {"ok": True}},
            {"action": "dispatch", "args": {
                "incident_id": "I2", "unit_id": "M1", "action_id": "same",
            }, "response": {
                "ok": False,
                "code": "action_id_conflict",
                "error": "action id already used",
            }},
        ]
        trace = {"events": [
            {"event": "unit_on_scene", "incident_id": "I1", "unit_id": "M1", "time": 180},
            {"event": "incident_complete", "incident_id": "I1", "unit_id": "M1", "time": 300},
        ]}
        audit = audit_actions(scenario, actions)
        self.assertTrue(audit["invalid_actions"])
        self.assertEqual(audit["catastrophic_violations"], [])

        result = compute_score(scenario, actions, trace, {"ok": True})
        self.assertFalse(result["evidence"]["hard_failure"])
        self.assertGreater(result["overall"], 0.0)
        self.assertGreater(result["integrity"], 0.0)

    def test_identical_action_retry_is_idempotent_in_audit(self) -> None:
        scenario = scenario_fixture()
        action = {"action": "dispatch", "args": {
            "incident_id": "I1", "unit_id": "M1", "action_id": "retry"
        }}
        audit = audit_actions(scenario, [action, dict(action)])
        self.assertEqual(audit["catastrophic_violations"], [])
        self.assertEqual(audit["invalid_actions"], [])

    def test_hidden_state_is_only_checked_in_agent_outputs(self) -> None:
        clean = [{"action": "observe", "observation": {"sim_time": 2, "units": []}}]
        leaked = [{"action": "observe", "observation": {"future_events": [], "units": []}}]
        self.assertEqual(hidden_state_violations(clean), [])
        self.assertTrue(hidden_state_violations(leaked))
        self.assertTrue(hidden_state_violations([{
            "action": "observe",
            "observation": {"release_time_s": 10, "units": []},
        }]))
        # A raw verifier event may contain a release time; it is not an agent
        # observation and must not be misclassified as a leak.
        self.assertEqual(hidden_state_violations([{ "event": "unit_release", "release_time": 10 }]), [])

    def test_canonical_hash_and_first_divergence(self) -> None:
        left = {"events": [{"time": 1, "status": "FREE"}]}
        right = {"events": [{"time": 2, "status": "FREE"}]}
        self.assertNotEqual(sha256_json(left), sha256_json(right))
        divergence = first_trace_divergence(left, right)
        self.assertEqual(divergence["path"], "$.events[0].time")
        self.assertEqual(canonical_json({"b": 1, "a": 2}), '{"a":2,"b":1}')

    def test_tamper_probe_reports_first_action_path(self) -> None:
        from replay_check import tamper_probe

        result = tamper_probe([{"action": "advance", "action_id": "a1"}])
        self.assertTrue(result["changed"])
        self.assertEqual(result["first_divergence"]["path"], "$[0].tampered")

    def test_tamper_probe_changes_nested_sidecar_command(self) -> None:
        from replay_check import tamper_probe

        with tempfile.TemporaryDirectory() as directory:
            scenario_path = Path(directory) / "scenario.json"
            scenario_path.write_text(json.dumps(scenario_fixture()), encoding="utf-8")
            records = [{
                "record_type": "action",
                "action_id": "a1",
                "command": {"command": "advance", "action_id": "a1"},
            }]
            result = tamper_probe(records, scenario_path)
            self.assertTrue(result["changed"])
            self.assertIsNotNone(result.get("replay_first_divergence"))

    def test_tamper_probe_replays_and_reports_transition_divergence(self) -> None:
        from replay_check import tamper_probe

        with tempfile.TemporaryDirectory() as directory:
            scenario_path = Path(directory) / "scenario.json"
            scenario_path.write_text(json.dumps(scenario_fixture()), encoding="utf-8")
            result = tamper_probe([{"action": "observe", "action_id": "o1"}], scenario_path)
            self.assertTrue(result["original_replay_ok"])
            self.assertIsNotNone(result["replay_first_divergence"])

    def test_fallback_replay_is_deterministic_and_tamper_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scenario_path = root / "scenario.json"
            scenario_path.write_text(json.dumps(scenario_fixture()), encoding="utf-8")
            actions = [{"action": "observe", "action_id": "o1"}, {"action": "advance", "action_id": "a1"}]
            first = replay_actions(scenario_path, actions)
            second = replay_actions(scenario_path, actions)
            self.assertEqual(first["action_hash"], second["action_hash"])
            self.assertEqual(first["state_hash"], second["state_hash"])
            tampered = replay_actions(scenario_path, actions + [{"action": "finish", "action_id": "f1"}])
            self.assertNotEqual(first["action_hash"], tampered["action_hash"])

    def test_structural_fallback_is_not_authoritative_integrity(self) -> None:
        # Force the diagnostic path to model a verifier image without the
        # simulator runtime.  Hashes remain available for debugging, but the
        # result must not claim that the trajectory was replayed.
        import verifier_lib

        with tempfile.TemporaryDirectory() as directory:
            scenario_path = Path(directory) / "scenario.json"
            scenario_path.write_text(json.dumps(scenario_fixture()), encoding="utf-8")
            original_loader = verifier_lib.load_simulator_class
            verifier_lib.load_simulator_class = lambda *args, **kwargs: None
            try:
                result = replay_actions(scenario_path, [{"action": "observe", "action_id": "o1"}])
            finally:
                verifier_lib.load_simulator_class = original_loader
            self.assertFalse(result["ok"])
            self.assertFalse(result["simulator_available"])
            self.assertIn("fallback", result)

    def test_non_dispatchable_events_do_not_count_as_work(self) -> None:
        scenario = scenario_fixture()
        scenario["incidents"].append({"incident_id": "F1", "arrival_s": 20, "priority": "1", "call_type": "Fire", "dispatchable": False})
        result = compute_score(scenario, [], {"events": []}, {"ok": True})
        self.assertEqual(result["evidence"]["incident_count"], 2)

    def test_noop_only_receives_integrity_residual(self) -> None:
        scenario = scenario_fixture()
        trace = {"events": [
            {"event": "incident_arrival", "incident_id": "I1", "time": 0},
            {"event": "incident_arrival", "incident_id": "I2", "time": 120},
        ]}
        result = compute_score(scenario, [{"action": "advance"}], trace, {"ok": True})
        self.assertLessEqual(result["overall"], 0.05)
        self.assertEqual(result["response"], 0.0)
        self.assertEqual(result["coverage"], 0.0)
        self.assertEqual(result["integrity"], 1.0)

    def test_public_integrity_retry_cannot_oracle_hidden_future(self) -> None:
        def retry_diagnostic(suffix: str, hidden_time: int) -> dict:
            return {
                "scenario_id": f"private-variant-{suffix}",
                "response_by_incident": {
                    f"future-incident-{suffix}": {
                        "incident_id": f"future-incident-{suffix}",
                        "arrival_s": hidden_time,
                        "response_s": hidden_time + 10,
                    },
                },
                "retry_diagnostic": {
                    "action_id": f"retry-{suffix}",
                    "incident_id": f"future-incident-{suffix}",
                    "unit_id": f"hidden-unit-{suffix}",
                    "sim_time": hidden_time,
                    "state_version": hidden_time,
                    "observation": {
                        "future_incidents": [{
                            "incident_id": f"future-incident-{suffix}",
                            "arrival_s": hidden_time,
                        }],
                    },
                    "reason": "simulator_rejected_action",
                    "code": "unknown_incident",
                    "state_hash": "safe-state-hash",
                },
                "state_hash": "safe-state-hash",
                "trace_hash": "safe-trace-hash",
                "first_divergence": {
                    "path": "$.events[0].time_s",
                    "first": {"incident_id": f"future-incident-{suffix}"},
                    "second": {"time_s": hidden_time},
                    "first_length": 1,
                    "second_length": 2,
                },
            }

        first = _public_integrity(retry_diagnostic("real", 900))
        second = _public_integrity(retry_diagnostic("decoy", 1800))

        # A retry using a real future ID and one using a decoy must expose the
        # same diagnostics; otherwise details.json becomes an enumeration
        # oracle even though the simulator's public error is identical.
        self.assertEqual(first, second)
        self.assertEqual(first["state_hash"], "safe-state-hash")
        self.assertEqual(first["trace_hash"], "safe-trace-hash")
        self.assertEqual(first["first_divergence"], {
            "path": "$.events[0].time_s",
            "first_length": 1,
            "second_length": 2,
        })
        self.assertEqual(first["retry_diagnostic"], {
            "reason": "simulator_rejected_action",
            "code": "unknown_incident",
            "state_hash": "safe-state-hash",
        })
        encoded = json.dumps(first, sort_keys=True).lower()
        for forbidden in (
            "scenario_id", "response_by_incident", "action_id",
            "incident_id", "unit_id", "sim_time", "state_version",
            "future_incidents", "arrival_s", "response_s",
        ):
            self.assertNotIn(f'"{forbidden}"', encoded)

    def test_catastrophic_violation_is_hard_failure(self) -> None:
        scenario = scenario_fixture()
        actions = [
            {"action": "dispatch", "args": {"incident_id": "I1", "unit_id": "M1", "action_id": "a1"}},
            {"action": "dispatch", "args": {"incident_id": "I2", "unit_id": "M1", "action_id": "a2"}},
        ]
        # The original command list alone is not sufficient for a hard fail.
        # The authoritative replay trace below explicitly claims both writes
        # were accepted, which proves a committed double-booking invariant
        # violation even if the supplied Agent trajectory omitted responses.
        trace = {
            "actions": [
                {**actions[0], "ok": True},
                {**actions[1], "ok": True},
            ],
            "events": [],
        }
        result = compute_score(scenario, actions, trace, {"ok": True})
        self.assertEqual(result["integrity"], 0.0)
        self.assertEqual(result["overall"], 0.0)
        self.assertTrue(result["evidence"]["hard_failure"])
        self.assertIn(
            "unit_double_booked",
            {item["reason"] for item in result["evidence"]["catastrophic_violations"]},
        )


if __name__ == "__main__":
    unittest.main()
