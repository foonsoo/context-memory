import unittest

from benchmarks.run_embedding_evaluation import run


class EmbeddingEvaluationTests(unittest.TestCase):
    def test_dependency_free_baselines_are_reproducible(self):
        result = run(repeats=1)
        self.assertEqual(set(result["results"]), {"fts", "local_hash"})
        self.assertGreaterEqual(result["results"]["local_hash"]["recall_at_5"], result["results"]["fts"]["recall_at_5"])
        self.assertLessEqual(result["results"]["fts"]["negative_query_result_rate"], 0.25)


if __name__ == "__main__":
    unittest.main()
