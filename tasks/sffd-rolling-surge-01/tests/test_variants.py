from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_sffd_variants import (  # noqa: E402
    derive_seed,
    is_well_separated,
    opaque_scenario_id,
    rank_candidates,
)


class HiddenVariantTests(unittest.TestCase):
    def test_hmac_seed_and_opaque_id_are_unique_and_deterministic(self) -> None:
        key = b"private-evaluator-key-material"
        source_hash = "a" * 64
        times = [datetime(2025, 3, day, 10) for day in range(1, 11)]
        first = [derive_seed(key, source_hash, value, index)
                 for index, value in enumerate(times, 1)]
        second = [derive_seed(key, source_hash, value, index)
                  for index, value in enumerate(times, 1)]
        self.assertEqual(first, second)
        self.assertEqual(len(set(first)), 10)
        identifiers = [opaque_scenario_id(key, source_hash, value) for value in times]
        self.assertEqual(len(set(identifiers)), 10)
        self.assertTrue(all("2025" not in value for value in identifiers))

    def test_candidate_ranking_is_keyed_and_gap_check_is_strict(self) -> None:
        candidates = [
            {"t0": datetime(2025, 3, day, 10), "dispatchable_count": 12}
            for day in range(1, 6)
        ]
        ranked_a = rank_candidates(candidates, key=b"a" * 32, source_hash="b" * 64)
        ranked_b = rank_candidates(candidates, key=b"a" * 32, source_hash="b" * 64)
        ranked_c = rank_candidates(candidates, key=b"c" * 32, source_hash="b" * 64)
        self.assertEqual(ranked_a, ranked_b)
        self.assertNotEqual([item["t0"] for item in ranked_a],
                            [item["t0"] for item in ranked_c])
        selected = [datetime(2025, 3, 1, 10)]
        self.assertFalse(is_well_separated(datetime(2025, 3, 1, 21), selected,
                                           timedelta(hours=12)))
        self.assertTrue(is_well_separated(datetime(2025, 3, 1, 22), selected,
                                          timedelta(hours=12)))


if __name__ == "__main__":
    unittest.main()
