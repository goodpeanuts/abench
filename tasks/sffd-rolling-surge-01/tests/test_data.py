from __future__ import annotations

import csv
import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DataTests(unittest.TestCase):
    def test_release_scenario_and_manifest_are_consistent(self) -> None:
        scenario = json.loads((ROOT / "scenario.json").read_text(encoding="utf-8"))
        manifest = json.loads((ROOT / "data_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(scenario["metadata"]["source_csv_sha256"], manifest["calibration_freeze"]["source_csv_sha256"])
        self.assertTrue(scenario["metadata"]["roster"]["history_complete"])
        self.assertFalse(scenario["metadata"]["roster"]["short_history_allowed"])
        self.assertEqual(scenario["metadata"]["dispatchable_incident_count"], 12)
        self.assertTrue(scenario["metadata"]["resource_competition"]["detected"])
        self.assertEqual(scenario["calibration"]["sample_counts"]["travel_observations"], 5617)
        self.assertEqual(scenario["calibration"]["sample_counts"]["service_observations"], 5595)
        self.assertEqual(scenario["calibration"]["sample_counts"]["release_observations"], 5595)
        self.assertEqual(scenario["metadata"]["scenario_schema_version"], "sffd-ems-scenario.v1")
        self.assertEqual(scenario["metadata"]["simulator_version"], "sffd-ems-simulator/0.1.0")
        self.assertEqual(scenario["metadata"]["seed"], manifest["scenario"]["seed"])
        markers = scenario["metadata"]["field_provenance"]
        self.assertEqual(set(markers["marker_vocabulary"]), {"observed", "derived", "counterfactual"})
        self.assertEqual(len(markers["source_fields"]), 36)
        self.assertEqual(markers["source_fields"]["address"]["exposed"], False)
        self.assertEqual(markers["scenario_fields"]["agent_actions"]["source"], "counterfactual")
        manifest_markers = manifest["field_provenance"]
        self.assertEqual(len(manifest_markers["source_fields"]), 36)
        quality = scenario["metadata"]["data_quality"]
        self.assertEqual(quality["row_count"], 7183)
        self.assertTrue(quality["rowid_unique"])
        self.assertTrue(quality["received_time_monotonic"])
        self.assertEqual(quality["unique_incident_numbers"], 3489)
        self.assertEqual(manifest["scenario"]["data_quality"]["missing_rates"],
                         quality["missing_rates"])
        scenario_hash = hashlib.sha256((ROOT / "scenario.json").read_bytes()).hexdigest()
        self.assertEqual(manifest["scenario"]["scenario_sha256"], scenario_hash)

    def test_no_exact_location_is_emitted(self) -> None:
        scenario = json.loads((ROOT / "scenario.json").read_text(encoding="utf-8"))
        # Field names may appear in the provenance snapshot, but no raw value
        # from a precise location column may be emitted into the scenario.
        for key in ("address", "case_location", "zipcode_of_incident"):
            values = []
            def collect(value):
                if isinstance(value, dict):
                    for child_key, child in value.items():
                        if child_key == key and isinstance(child, str):
                            values.append(child)
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)
            collect(scenario)
            self.assertEqual(values, [], key)

    def test_public_contract_does_not_contain_scenario_material(self) -> None:
        contract = json.loads((ROOT / "environment" / "public_contract.json").read_text(encoding="utf-8"))
        encoded = json.dumps(contract, sort_keys=True).lower()
        # A contract may name forbidden keys as policy; it must not carry any
        # scenario values or a hidden material object.
        self.assertNotIn('"hidden_reference_responses":', encoded)
        self.assertNotIn('"source_rowids":', encoded)
        self.assertNotIn('"latent":', encoded)

    def test_agent_image_excludes_private_scenario(self) -> None:
        dockerfile = (ROOT / "environment" / "Dockerfile").read_text(encoding="utf-8")
        ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        compose = (ROOT / "environment" / "docker-compose.yaml").read_text(encoding="utf-8")
        # Harbor builds the Agent image with environment/ as its context.  The
        # security invariant is that only the client/contract are copied.
        self.assertIn("COPY emsctl /opt/sffd-task/environment/emsctl", dockerfile)
        self.assertIn("USER agent", dockerfile)
        self.assertNotIn("COPY . /opt/sffd-task", dockerfile)
        sidecar_dockerfile = (ROOT / "environment" / "simulator.Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY simulator /opt/sffd-sidecar/simulator", sidecar_dockerfile)
        self.assertIn('CMD ["python3", "simulator_server.py"', sidecar_dockerfile)
        self.assertIn('"simulator_server.py", "--scenario"', compose)
        self.assertIn("scenario.json", ignore)
        self.assertIn("../scenario.json:/opt/sffd-sidecar/scenario.json:ro", compose)
        # The hidden mount belongs to the simulator service, while the main
        # service receives only the socket volume.
        self.assertIn("\n  main:", compose)
        self.assertIn("\n  simulator:", compose)
        main_block = compose.split("\n  simulator:", 1)[0]
        simulator_block = compose.split("\n  simulator:", 1)[1].split("\nvolumes:", 1)[0]
        self.assertIn("scenario.json:/opt/sffd-sidecar/scenario.json:ro", simulator_block)
        self.assertNotIn("scenario.json:", main_block)
        # The shared endpoint is a capability, not an Agent-owned workspace.
        # A read-only mount prevents an Agent from unlinking/replacing the
        # sidecar socket while retaining permission to connect to it.
        self.assertIn("ems-socket:/tmp/ems:ro", main_block)

    def test_separate_verifier_contract_is_present(self) -> None:
        task_toml = (ROOT / "task.toml").read_text(encoding="utf-8")
        verifier_dockerfile = (ROOT / "tests" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('environment_mode = "separate"', task_toml)
        self.assertNotIn("command =", task_toml)
        self.assertIn('source = "/tmp/ems-state/actions.jsonl"', task_toml)
        self.assertIn("COPY . /tests/", verifier_dockerfile)
        self.assertIn("SFFD_HARBOR_VERIFIER=1", verifier_dockerfile)
        # The Dockerfile copies the complete tests context; Harbor invokes the
        # resulting script at /tests/test.sh.
        self.assertIn("chmod 0555 /tests/*.sh", verifier_dockerfile)


if __name__ == "__main__":
    unittest.main()
