import unittest

from context_memory.serialization import canonical, canonical_digest


class SerializationTests(unittest.TestCase):
    def test_canonical_json_is_compact_unicode_and_key_sorted(self):
        self.assertEqual(
            canonical({"z": "기억", "a": [2, 1]}),
            '{"a":[2,1],"z":"기억"}',
        )

    def test_canonical_digest_uses_canonical_json(self):
        self.assertEqual(
            canonical_digest({"z": "기억", "a": [2, 1]}),
            canonical_digest({"a": [2, 1], "z": "기억"}),
        )
