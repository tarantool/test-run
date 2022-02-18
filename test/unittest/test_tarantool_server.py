import os
import subprocess
import sys
import unittest


class TestTarantoolServer(unittest.TestCase):
    def test_tarantool_server_not_hanging(self):
        env = os.environ.copy()
        env['SERVER_START_TIMEOUT'] = '5'

        cmd = [sys.executable, 'test/test-run.py', 'unittest/hang.test.lua']

        # File names intentionally have hashes to find exactly these processes
        # using 'ps' command.
        box_instance = 'box-cc0544b6afd1'
        replica_instance = 'replica-7f4d4895ff58'

        err_msg_1 = ('[Instance "%s"] Failed to start tarantool '
                     'instance "%s"' % (box_instance, replica_instance))
        err_msg_2 = ('[Instance "%s"] Failed to start within %s seconds'
                     % (replica_instance, env['SERVER_START_TIMEOUT']))

        try:
            subprocess.check_output(cmd, env=env, universal_newlines=True)
            self.fail("Command `%s` did not return non-zero exit code"
                      % ' '.join(cmd))
        except subprocess.CalledProcessError as exc:
            err_obj = exc

        self.assertIn(err_msg_1, err_obj.output)
        self.assertIn(err_msg_2, err_obj.output)

        ps_lines = subprocess.check_output(
            ['ps', '-o', 'command'], universal_newlines=True
        ).splitlines()
        proc_lines = [line.strip() for line in ps_lines
                      if 'tarantool %s.lua' % box_instance in line or
                      'tarantool %s.lua' % replica_instance in line]

        self.assertFalse(
            proc_lines, 'There are some hanging tarantool processes!'
        )


if __name__ == '__main__':
    unittest.main()
