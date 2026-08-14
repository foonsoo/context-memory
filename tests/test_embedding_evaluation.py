import unittest
import json
import tempfile
from pathlib import Path

from benchmarks.run_embedding_evaluation import load_fixture, run


class EmbeddingEvaluationTests(unittest.TestCase):
    def test_dependency_free_baselines_are_reproducible(self):
        result = run(repeats=1)
        self.assertEqual(result["schema_version"], 3)
        self.assertEqual(result["fixture"]["source"], "built-in-synthetic")
        self.assertEqual(set(result["results"]), {"fts", "local_hash"})
        self.assertGreaterEqual(result["results"]["local_hash"]["recall_at_5"], result["results"]["fts"]["recall_at_5"])
        self.assertLessEqual(result["results"]["fts"]["negative_query_result_rate"], 0.25)
        self.assertEqual(result["results"]["local_hash"]["negative_queries"]["count"], 4)
        self.assertGreater(result["results"]["local_hash"]["recall_at_5"], result["results"]["fts"]["recall_at_5"])
        self.assertEqual(result["results"]["local_hash"]["categories"]["korean-spacing"]["recall_at_5"], 1.0)

    def test_external_judgment_fixture_is_validated_and_not_copied_to_metadata(self):
        fixture = {"schema_version": 1,
                   "memories": [{"key": "private-1", "title": "Private title", "content": "secret-shaped fixture text"}],
                   "queries": [{"query": "private lookup", "relevant": ["private-1"], "category": "personal"},
                               {"query": "unrelated topic", "relevant": [], "category": "negative"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "judgments.json"
            path.write_text(json.dumps(fixture), encoding="utf-8")
            self.assertEqual(load_fixture(path)[0][0][0], "private-1")
            result = run(repeats=1, fixture=path)
        self.assertEqual(result["fixture"], {"source": "external", "memories": 1, "queries": 2, "repeats": 1})
        self.assertNotIn("secret-shaped", json.dumps(result))
        self.assertNotIn("private lookup", json.dumps(result))
        self.assertNotIn("outcomes", result["results"]["fts"])

    def test_external_fixture_rejects_unknown_relevance_key(self):
        fixture = {"schema_version": 1,
                   "memories": [{"key": "known", "title": "Title", "content": "Content"}],
                   "queries": [{"query": "lookup", "relevant": ["missing"], "category": "personal"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown memories"):
                load_fixture(path)


if __name__ == "__main__":
    unittest.main()
