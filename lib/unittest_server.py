import os
import sys
import glob
from subprocess import Popen, PIPE, STDOUT

from lib.server import Server
from lib.tarantool_server import Test


class UnitTest(Test):
    def __init__(self, *args, **kwargs):
        Test.__init__(self, *args, **kwargs)
        self.valgrind = kwargs.get('valgrind', False)

    def execute(self, server):
        server.current_test = self
        execs = server.prepare_args()
        proc = Popen(execs, cwd=server.vardir, stdout=PIPE, stderr=STDOUT)
        sys.stdout.write(proc.communicate()[0])


class UnittestServer(Server):
    """A dummy server implementation for unit test suite"""
    def __new__(cls, ini=None, *args, **kwargs):
        cls = Server.get_mixed_class(cls, ini)
        return object.__new__(cls)

    def __init__(self, _ini=None, test_suite=None):
        if _ini is None:
            _ini = {}
        ini = {'vardir': None}
        ini.update(_ini)
        Server.__init__(self, ini, test_suite)
        self.testdir = os.path.abspath(os.curdir)
        self.vardir = ini['vardir']
        self.builddir = ini['builddir']
        self.debug = False
        self.name = 'unittest_server'

    @property
    def logfile(self):
        return self.current_test.tmp_result

    @property
    def binary(self):
        return UnittestServer.prepare_args(self)[0]

    def prepare_args(self, args=[]):
        executable_path = os.path.join(self.builddir, "test",
                                       self.current_test.name)
        return [executable_path] + args

    def deploy(self, vardir=None, silent=True, wait=True):
        self.vardir = vardir
        if not os.access(self.vardir, os.F_OK):
            os.makedirs(self.vardir)

    @classmethod
    def find_exe(cls, builddir):
        cls.builddir = builddir

    @staticmethod
    def find_tests(test_suite, suite_path):
        def patterned(test, patterns):
            answer = []
            for i in patterns:
                if test.name.find(i) != -1:
                    answer.append(test)
            return answer

        test_suite.ini['suite'] = suite_path
        tests = glob.glob(os.path.join(suite_path, "*.test"))

        if not tests:
            executable_path_glob = os.path.join(test_suite.args.builddir,
                                                'test', suite_path, '*.test')
            tests = glob.glob(executable_path_glob)

        tests = Server.exclude_tests(tests, test_suite.args.exclude)
        test_suite.tests = [UnitTest(k, test_suite.args, test_suite.ini)
                            for k in sorted(tests)]
        test_suite.tests = sum([patterned(x, test_suite.args.tests)
                                for x in test_suite.tests], [])

    def print_log(self, lines):
        pass
