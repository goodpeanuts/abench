from __future__ import annotations

import unittest

from atif import build_atif


class AtifTests(unittest.TestCase):
    def test_v17_shape_and_redaction(self) -> None:
        document = build_atif([
            {
                "action": "observe",
                "action_id": "o1",
                "response": {
                    "observation": {"sim_time": 0, "future_events": [], "units": []},
                    "private": {"seed": 123},
                },
            }
        ])
        self.assertEqual(document["schema_version"], "ATIF-v1.7")
        self.assertEqual(document["steps"][0]["source"], "agent")
        self.assertEqual(document["steps"][0]["llm_call_count"], 0)
        content = document["steps"][0]["observation"]["results"][0]["content"]
        self.assertNotIn("future_events", content)
        self.assertNotIn("seed", content)

    def test_redacts_latent_timing_aliases(self) -> None:
        document = build_atif([{
            "action": "observe",
            "response": {"available_time_s": 12, "release_time_s": 8,
                          "busy_until": 20, "sim_time": 1},
        }])
        content = document["steps"][0]["observation"]["results"][0]["content"]
        self.assertNotIn("available_time_s", content)
        self.assertNotIn("release_time_s", content)
        self.assertNotIn("busy_until", content)

    def test_nested_command_arguments_are_preserved(self) -> None:
        document = build_atif([{
            "command": {"name": "dispatch", "args": {
                "incident_id": "I1", "unit_id": "M1", "action_id": "a1"
            }}
        }])
        call = document["steps"][0]["tool_calls"][0]
        self.assertEqual(call["function_name"], "emsctl.dispatch")
        self.assertEqual(call["arguments"]["incident_id"], "I1")

    def test_sidecar_action_record_exports_only_command_envelope(self) -> None:
        document = build_atif([{
            "record_type": "action",
            "action_id": "a1",
            "fingerprint": "private-fingerprint",
            "before_state_hash": "public-hash",
            "command": {
                "command": "advance",
                "next_event": True,
                "action_id": "a1",
            },
        }])
        call = document["steps"][0]["tool_calls"][0]
        self.assertEqual(call["function_name"], "emsctl.advance")
        self.assertEqual(call["arguments"]["next_event"], True)
        self.assertNotIn("fingerprint", call["arguments"])
        self.assertNotIn("before_state_hash", call["arguments"])


if __name__ == "__main__":
    unittest.main()
