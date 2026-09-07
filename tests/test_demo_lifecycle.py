import copy
import unittest

from scripts.demo_lifecycle import validate_demo


class DemoLifecycleValidationTests(unittest.TestCase):
    def setUp(self):
        self.result = {
            "initial_recall": {
                "items": [
                    {
                        "memory_id": "sqlite-memory",
                        "source_event_ids": ["sqlite-event"],
                    }
                ]
            },
            "initial_source": "sqlite-event",
            "current_recall": {
                "items": [
                    {
                        "memory_id": "postgres-memory",
                        "source_event_ids": ["postgres-event"],
                    }
                ]
            },
            "current_source": "postgres-event",
            "sqlite_memory": {
                "id": "sqlite-memory",
                "status": "superseded",
            },
            "postgres_memory": {"id": "postgres-memory", "status": "active"},
        }

    def test_valid_result_passes(self):
        validate_demo(self.result)

    def test_wrong_recall_result_fails_ci_contract(self):
        broken = copy.deepcopy(self.result)
        broken["current_recall"]["items"][0]["memory_id"] = "wrong"
        with self.assertRaisesRegex(RuntimeError, "PostgreSQL memory"):
            validate_demo(broken)

    def test_stale_memory_in_final_recall_fails_ci_contract(self):
        broken = copy.deepcopy(self.result)
        broken["current_recall"]["items"].append(
            {"memory_id": "sqlite-memory", "source_event_ids": ["sqlite-event"]}
        )
        with self.assertRaisesRegex(RuntimeError, "superseded SQLite"):
            validate_demo(broken)


if __name__ == "__main__":
    unittest.main()
