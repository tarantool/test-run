import glob
import os
import re
import sys

from subprocess import Popen, PIPE
from threading import Timer

from lib.colorer import color_stdout
from lib.error import TestRunInitError
from lib.options import Options
from lib.sampler import sampler
from lib.server import Server
from lib.tarantool_server import Test
from lib.tarantool_server import TestExecutionError
from lib.tarantool_server import TarantoolServer


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

        Execute `luatest -c --verbose <name>_test.lua --output tap` command.
        Disable capture mode. Provide a verbose output in the tap format.
        Extend the command by `--pattern <pattern>` if the corresponding
        option is provided.
        """
        server.current_test = self
        script = os.path.join(os.path.basename(server.testdir), self.name)
        command = ['luatest', '-c', '--verbose', script, '--output', 'tap']
        if Options().args.pattern:
            for p in Options().args.pattern:
                command.extend(['--pattern', p])

        # Tarantool's build directory is added to PATH in
        # TarantoolServer.find_exe().
        #
        # We start luatest from the project source directory, it
        # is the usual way to use luatest.
        #
        # VARDIR (${BUILDDIR}/test/var/001_foo) will be used for
        # write ahead logs, snapshots, logs, unix domain sockets
        # and so on.
        os.environ['VARDIR'] = server.vardir
        project_dir = os.environ['SOURCEDIR']

        with open(server.logfile, 'ab') as f:
            proc = Popen(command, cwd=project_dir, stdout=PIPE, stderr=f)
        sampler.register_process(proc.pid, self.id, server.name)
        test_timeout = Options().args.test_timeout
        timer = Timer(test_timeout, timeout_handler, (proc, test_timeout))
        timer.start()
        sys.stdout.write_bytes(proc.communicate()[0])
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

    @property
    def binary(self):
        return LuatestServer.prepare_args(self)[0]

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

    @classmethod
    def verify_luatest_exe(cls):
        """Verify that luatest executable is available."""
        try:
            # Just check that the command returns zero exit code.
            with open(os.devnull, 'w') as devnull:
                returncode = Popen(['luatest', '--version'],
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

    @staticmethod
    def find_tests(test_suite, suite_path):
        """Looking for *_test.lua, which are can be executed by luatest."""

        def patterned(test, patterns):
            answer = []
            for i in patterns:
                if test.name.find(i) != -1:
                    answer.append(test)
            return answer

        test_suite.ini['suite'] = suite_path
        tests = glob.glob(os.path.join(suite_path, '*_test.lua'))

        tests = Server.exclude_tests(tests, test_suite.args.exclude)
        test_suite.tests = [LuatestTest(k, test_suite.args, test_suite.ini)
                            for k in sorted(tests)]
        test_suite.tests = sum([patterned(x, test_suite.args.tests)
                                for x in test_suite.tests], [])
