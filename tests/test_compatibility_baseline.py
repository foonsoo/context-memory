from __future__ import annotations

import json
import unittest
from pathlib import Path

from compatibility_snapshot import compatibility_snapshot


class CompatibilityBaselineTests(unittest.TestCase):
    def test_frozen_public_contracts(self) -> None:
        expected_path = (
            Path(__file__).parent / "fixtures/compatibility-baseline-v1.json"
        )
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        self.assertEqual(compatibility_snapshot(), expected)


if __name__ == "__main__":
    unittest.main()
