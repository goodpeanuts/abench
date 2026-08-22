from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from fetch_sffd_slice import canonical_csv, validate_rows  # noqa: E402


class FetchValidationTests(unittest.TestCase):
    def rows(self):
        return [
            {"rowid": "r1", "incident_number": "i1", "received_dttm": "2025-01-01T00:00:01"},
            {"rowid": "r2", "incident_number": "i1", "received_dttm": "2025-01-01T00:00:02"},
            {"rowid": "r3", "incident_number": "i2", "received_dttm": "2025-01-01T00:00:03"},
        ]

    def test_quality_reports_multi_row_events_without_deduplicating_responses(self) -> None:
        rows = self.rows()
        quality = validate_rows(rows, ["rowid", "incident_number", "received_dttm"])
        self.assertTrue(quality["rowid_unique"])
        self.assertTrue(quality["received_time_monotonic"])
        self.assertEqual(quality["unique_incident_numbers"], 2)
        self.assertEqual(quality["multi_row_incident_numbers"], 1)

    def test_quality_rejects_duplicate_rows_and_unsorted_time(self) -> None:
        duplicate = self.rows()
        duplicate[2]["rowid"] = "r1"
        with self.assertRaises(ValueError):
            validate_rows(duplicate, ["rowid", "incident_number", "received_dttm"])
        unsorted = self.rows()
        unsorted[2]["received_dttm"] = "2024-01-01T00:00:00"
        with self.assertRaises(ValueError):
            validate_rows(unsorted, ["rowid", "incident_number", "received_dttm"])

    def test_paginated_csv_is_canonicalized(self) -> None:
        page_a = b"rowid,incident_number\nr1,i1\n"
        page_b = b"rowid,incident_number\nr2,i1\n"
        data, header, count = canonical_csv([page_a, page_b])
        self.assertEqual(header, ["rowid", "incident_number"])
        self.assertEqual(count, 2)
        self.assertEqual(data, b"rowid,incident_number\nr1,i1\nr2,i1\n")


if __name__ == "__main__":
    unittest.main()
