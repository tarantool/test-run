import glob
import os
import re
import sys

from subprocess import PIPE
from subprocess import Popen
from threading import Timer

from lib.colorer import color_stdout
from lib.error import TestRunInitError
from lib.options import Options
from lib.sampler import sampler
from lib.server import Server
from lib.tarantool_server import Test
from lib.tarantool_server import TestExecutionError
from lib.tarantool_server import TarantoolServer
from lib.utils import bytes_to_str
from lib.utils import find_tags


def timeout_handler(process, test_timeout):
    color_stdout("Test timeout of %d secs reached\t" % test_timeout, schema='error')
    process.kill()


class LuatestTest(Test):
    """ Handle *_test.lua.

    Provide method for executing luatest <name>_test.lua test.
    """

    def __init__(self, *args, **kwargs):
        super(LuatestTest, self).__init__(*args, **kwargs)
        self.valgrind = kwargs.get('valgrind', False)

    def execute(self, server):
        """Execute test by luatest command

        Execute `luatest -c --no-clean --verbose <name>_test.lua --output tap --log <path>`
        command. Disable capture mode and deletion of the var directory. Provide a verbose
        output in the tap format. Extend the command by `--pattern <pattern>` if the
        corresponding option is provided.
        """
        server.current_test = self
        script = os.path.join(os.path.basename(server.testdir), self.name)

        # Disable stdout buffering.
        command = [server.binary, '-e', "io.stdout:setvbuf('no')"]
        # Add luatest as the script.
        command.extend([server.luatest])
        # Add luatest command-line options.
        command.extend(['-c', '--no-clean', '--verbose', script, '--output', 'tap'])
        # Add luatest logging option.
        command.extend(['--log', os.path.join(server.vardir, 'run.log')])
        if Options().args.pattern:
            for p in Options().args.pattern:
                command.extend(['--pattern', p])

        # Run a specific test case. See find_tests() for details.
        if 'test_case' in self.run_params:
            command.extend(['--run-test-case', self.run_params['test_case']])

        # We start luatest from the project source directory, it
        # is the usual way to use luatest.
        #
        # VARDIR (${BUILDDIR}/test/var/001_foo) will be used for
        # write ahead logs, snapshots, logs, unix domain sockets
        # and so on.
        os.environ['VARDIR'] = server.vardir
        project_dir = os.environ['SOURCEDIR']

        with open(server.logfile, 'ab') as f:
            proc = Popen(command, cwd=project_dir, stdout=sys.stdout, stderr=f)
        sampler.register_process(proc.pid, self.id, server.name)
        test_timeout = Options().args.test_timeout
        timer = Timer(test_timeout, timeout_handler, (proc, test_timeout))
        timer.start()
        proc.wait()
        timer.cancel()
        if proc.returncode != 0:
            raise TestExecutionError


class LuatestServer(Server):
    """A dummy server implementation for luatest server tests"""

    def __new__(cls, ini=None, *args, **kwargs):
        cls = Server.get_mixed_class(cls, ini)
        return object.__new__(cls)

    def __init__(self, _ini=None, test_suite=None):
        if _ini is None:
            _ini = {}
        ini = {'vardir': None}
        ini.update(_ini)
        super(LuatestServer, self).__init__(ini, test_suite)
        self.testdir = os.path.abspath(os.curdir)
        self.vardir = ini['vardir']
        self.builddir = ini['builddir']
        self.name = 'luatest_server'

    @property
    def logfile(self):
        # Remove the suite name using basename().
        test_name = os.path.basename(self.current_test.name)
        # Strip '.lua' from the end.
        #
        # The '_test' postfix is kept to ease distinguish this
        # log file from luatest.server instance logs.
        test_name = test_name[:-len('.lua')]
        # Add '.log'.
        file_name = test_name + '.log'
        # Put into vardir.
        return os.path.join(self.vardir, file_name)

    def deploy(self, vardir=None, silent=True, wait=True):
        self.vardir = vardir
        if not os.access(self.vardir, os.F_OK):
            os.makedirs(self.vardir)

    @classmethod
    def find_exe(cls, builddir):
        cls.builddir = builddir
        cls.binary = TarantoolServer.binary
        cls.debug = bool(re.findall(r'^Target:.*-Debug$', str(cls.version()),
                                    re.M))
        cls.luatest = os.environ['TEST_RUN_DIR'] + '/lib/luatest/bin/luatest'

    @classmethod
    def verify_luatest_exe(cls):
        """Verify that luatest executable is available."""
        try:
            # Just check that the command returns zero exit code.
            with open(os.devnull, 'w') as devnull:
                returncode = Popen([cls.luatest, '--version'],
                                   stdout=devnull,
                                   stderr=devnull).wait()
            if returncode != 0:
                raise TestRunInitError('Unable to run `luatest --version`',
                                       {'returncode': returncode})
        except OSError as e:
            # Python 2 raises OSError if the executable is not
            # found or if it has no executable bit. Python 3
            # raises FileNotFoundError and PermissionError in
            # those cases, which are childs of OSError anyway.
            raise TestRunInitError('Unable to find luatest executable', e)

    @classmethod
    def test_cases(cls, test_name):
        p = Popen([cls.luatest, test_name, '--list-test-cases'], stdout=PIPE)
        output = bytes_to_str(p.stdout.read()).rstrip()
        p.wait()

        # Exclude the first line if it is a tarantool version
        # report.
        res = output.split('\n')
        if len(res) > 0 and res[0].startswith('Tarantool version is'):
            return res[1:]

        return res

    @staticmethod
    def find_tests(test_suite, suite_path):
        """Looking for *_test.lua, which are can be executed by luatest."""

        # TODO: Investigate why this old hack is needed and drop
        # it if possible (move the assignment to test_suite.py).
        #
        # cdc70f94701f suggests that it is related to the out of
        # source build.
        test_suite.ini['suite'] = suite_path

        # A pattern here means just a substring to find in a test
        # name.
        include_patterns = Options().args.tests
        exclude_patterns = Options().args.exclude

        accepted_tags = Options().args.tags

        tests = []
        for test_name in glob.glob(os.path.join(suite_path, '*_test.lua')):
            # Several include patterns may match the given
            # test[^1].
            #
            # The primary usage of this behavior is to run a test
            # many times in parallel to verify its stability or
            # to debug an unstable behavior.
            #
            # Execute the test once for each of the matching
            # patterns.
            #
            # [^1]: A pattern matches a test if the pattern is a
            #       substring of the test name.
            repeat = sum(1 for p in include_patterns if p in test_name)
            # If neither of the include patterns matches the given
            # test, skip the test.
            if repeat == 0:
                continue

            # If at least one of the exclude patterns matches the
            # given test, skip the test.
            if any(p in test_name for p in exclude_patterns):
                continue

            tags = find_tags(test_name)

            # If --tags <...> CLI option is provided...
            if accepted_tags:
                # ...and the test has neither of the given tags,
                # skip the test.
                if not any(t in accepted_tags for t in tags):
                    continue

            # Add the test to the execution list otherwise.
            if 'parallel' in tags:
                # If the test has the 'parallel' tag, split the
                # test to test cases to run in separate tasks in
                # parallel.
                test_cases = LuatestServer.test_cases(test_name)

                # Display shorter test case names on the screen:
                # strip the common prefix.
                prefix_len = len(os.path.commonprefix(test_cases))

                for test_case in test_cases:
                    test_obj = LuatestTest(test_name, test_suite.args, test_suite.ini,
                                           params={"test_case": test_case},
                                           conf_name=test_case[prefix_len:])
                    tests.extend([test_obj] * repeat)
            else:
                # If the test has no 'parallel' tag, run all the
                # test cases as one task.
                test_obj = LuatestTest(test_name, test_suite.args, test_suite.ini)
                tests.extend([test_obj] * repeat)

        tests.sort(key=lambda t: t.name)

        # TODO: Don't modify a test suite object's field from
        # another object directly. It is much better to just
        # return a list of tests from this method.
        test_suite.tests = tests
