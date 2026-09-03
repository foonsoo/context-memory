import unittest

from benchmarks.call_inventory import build_inventory


class CallInventoryTests(unittest.TestCase):
    def test_every_private_store_method_has_a_static_call_site(self):
        inventory = build_inventory()
        self.assertEqual(inventory["unused_private"], [])

    def test_inventory_covers_the_frozen_store_surface(self):
        inventory = build_inventory()
        self.assertEqual(inventory["method_count"], 91)
        self.assertEqual(inventory["public_count"], 63)
        self.assertEqual(inventory["private_count"], 28)


if __name__ == "__main__":
    unittest.main()
