import unittest

from context_memory.audit_serialization import (
    build_audit_checkpoint,
    serialize_audit_chain,
    verify_audit_chain_bundle,
)


PROJECT_ID = "00000000-0000-4000-8000-000000000001"
DIGEST = "a" * 64


def bundle():
    checkpoints = [
        {
            "project_id": PROJECT_ID,
            "from_seq": 1,
            "through_seq": 2,
            "entry_count": 2,
            "previous_digest": None,
            "digest": DIGEST,
        }
    ]
    entries = [{"project_id": PROJECT_ID, "seq": 3}]
    return serialize_audit_chain(PROJECT_ID, checkpoints, entries)


class AuditSerializationTests(unittest.TestCase):
    def test_builds_checkpoint_from_loaded_rows(self):
        checkpoint = build_audit_checkpoint(
            PROJECT_ID,
            [{"seq": 2, "value": "first"}, {"seq": 3, "value": "second"}],
            DIGEST,
            checkpoint_id="checkpoint-id",
            created_at="2026-08-24T00:00:00+00:00",
        )

        self.assertEqual(checkpoint["from_seq"], 2)
        self.assertEqual(checkpoint["through_seq"], 3)
        self.assertEqual(checkpoint["entry_count"], 2)
        self.assertEqual(checkpoint["previous_digest"], DIGEST)
        self.assertRegex(checkpoint["digest"], r"^[0-9a-f]{64}$")

    def test_checkpoint_requires_rows(self):
        with self.assertRaisesRegex(ValueError, "at least one row"):
            build_audit_checkpoint(
                PROJECT_ID, [], None, "checkpoint-id", "created-at"
            )

    def test_serializes_and_verifies_audit_chain_without_database(self):
        result = verify_audit_chain_bundle(bundle(), DIGEST)

        self.assertEqual(
            result,
            {
                "ok": True,
                "project_id": PROJECT_ID,
                "head_digest": DIGEST,
                "checkpoint_count": 1,
                "audit_entry_count": 1,
                "anchored": True,
                "errors": [],
            },
        )

    def test_rejects_reordered_entries_and_replaced_anchor(self):
        value = bundle()
        value["audit_entries"] = [
            {"project_id": PROJECT_ID, "seq": 4},
            {"project_id": PROJECT_ID, "seq": 3},
        ]

        result = verify_audit_chain_bundle(value, "b" * 64)

        self.assertFalse(result["ok"])
        self.assertIn("expected head digest mismatch", result["errors"])
        self.assertTrue(
            any("not strictly ordered" in error for error in result["errors"])
        )

    def test_rejects_malformed_collection_contract(self):
        value = bundle()
        value["checkpoints"] = None

        self.assertEqual(
            verify_audit_chain_bundle(value),
            {
                "ok": False,
                "errors": ["checkpoints and audit_entries must be arrays"],
            },
        )
