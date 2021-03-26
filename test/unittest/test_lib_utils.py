import socket
import unittest

from hypothesis import given, settings
from hypothesis.strategies import integers

import lib.utils as utils

class TestUtils(unittest.TestCase):
    def test_extract_schema_from_snapshot(self):
        snapshot_path = 'test/unittest/00000000000000000003.snap'
        v = utils.extract_schema_from_snapshot(snapshot_path)
        self.assertEqual(v, (2, 3, 1))

    @settings(max_examples=5)
    @given(port=integers(65100, 65535))
    def test_check_port(self, port):
        def open_socket(p):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(('localhost', p))
            s.listen(0)
            return s
        status = utils.check_port(port, rais=False, ipv4=True, ipv6=False)
        self.assertEqual(status, True)
        s = open_socket(port)
        status = utils.check_port(port, rais=False, ipv4=True, ipv6=False)
        s.close()
        self.assertEqual(status, False)


if __name__ == "__main__":
    unittest.main()
