import unittest

import lib.utils as utils


class TestUtils(unittest.TestCase):
    def test_extract_schema_from_snapshot(self):
        snapshot_path = 'test/unittest/00000000000000000003.snap'
        v = utils.extract_schema_from_snapshot(snapshot_path)
        self.assertEqual(v, (2, 3, 1))


if __name__ == "__main__":
    unittest.main()
