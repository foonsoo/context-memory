import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.run_recall_live_replay import load_fixture, run


class RecallLiveReplayTests(unittest.TestCase):
    def test_live_replay_is_provenance_bearing_and_explicitly_retrospective(self):
        fixture = load_fixture()
        self.assertEqual(fixture["evaluation_class"], "retrospective-live-replay")
        self.assertIn("not prospective held-out", fixture["limitations"])
        self.assertTrue(
            all(case["prompt_source_event_ids"] for case in fixture["cases"])
        )

    def test_live_replay_recovers_expected_handoff_and_sources(self):
        result = run(repeats=2)
        self.assertEqual(result["metrics"]["expected_recovery"], 1.0)
        self.assertEqual(result["metrics"]["source_recovery"], 1.0)
        self.assertEqual(result["metrics"]["forbidden_leakage"], 0)
        self.assertTrue(
            all(
                case["selection_reason"] == "cwd_recent_events"
                for case in result["cases"]
            )
        )

    def test_live_replay_rejects_missing_prompt_provenance(self):
        fixture = load_fixture()
        fixture["cases"][0]["prompt_source_event_ids"] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source event IDs"):
                load_fixture(path)


if __name__ == "__main__":
    unittest.main()
