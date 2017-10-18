import os
import sys
import glob
import errno
import shutil

from gevent.subprocess import Popen, PIPE

from lib.server import Server
from lib.tarantool_server import Test, TarantoolServer
from lib.preprocessor import TestState
from lib.utils import find_port
from test import TestRunGreenlet

def run_server(execs, cwd, server):
    env = os.environ.copy()
    env["TEST_SUITE"] = server.test_suite.suite_path
    env["LISTEN"] = server.listen

    server.process = Popen(execs, stdout=PIPE, stderr=PIPE, cwd=cwd, env=env)
    stdout, stderr = server.process.communicate()
    sys.stdout.write(stdout)
    if server.process.wait() != 0:
        sys.stdout.write(stderr)
    server.process = None

class AppTest(Test):
    def execute(self, server):
        server.current_test = self
        ts = TestState(self.suite_ini, None, TarantoolServer,
                       default_server_no_connect=server)
        self.inspector.set_parser(ts)

        execs = server.prepare_args()
        tarantool = TestRunGreenlet(run_server, execs, server.vardir, server)
        self.current_test_greenlet = tarantool
        tarantool.start()

        tarantool.join()

class AppServer(Server):
    """A dummy server implementation for application server tests"""
    def __new__(cls, ini=None, *args, **kwargs):
        cls = Server.get_mixed_class(cls, ini)
        return object.__new__(cls)

    def __init__(self, _ini=None, test_suite=None):
        if _ini is None:
            _ini = {}
        ini = {
            'vardir': None
        }; ini.update(_ini)
        Server.__init__(self, ini, test_suite)
        self.testdir = os.path.abspath(os.curdir)
        self.vardir = ini['vardir']
        self.re_vardir_cleanup += [
            "*.snap", "*.xlog", "*.vylog", "*.inprogress", "*.sup", "*.lua", "*.pid"
        ]
        self.cleanup()
        self.builddir = ini['builddir']
        self.debug = False
        self.lua_libs = ini['lua_libs']
        self.requirements = ini['requirements']
        self.name = 'app_server'
        self.process = None
        self.listen = None

    def prepare_args(self):
        return [os.path.join(os.getcwd(), self.current_test.name)]

    def deploy(self, vardir=None, silent=True, need_init=True):
        self.vardir = vardir
        if not os.access(self.vardir, os.F_OK):
            os.makedirs(self.vardir)
        if self.lua_libs:
            for i in self.lua_libs:
                source = os.path.join(self.testdir, i)
                if os.path.basename(source):
                    dst = os.path.join(self.vardir, os.path.basename(source))
                else:
                    basedirname = source.split('/')[-1]
                    if not basedirname:
                        basedirname = source.split('/')[-2]
                    dst = os.path.join(self.vardir, basedirname)
                try:
                    if os.path.isdir(source):
                        shutil.copytree(source, dst)
                    else:
                        shutil.copy(source, self.vardir)
                except IOError as e:
                    print e
                    if (e.errno == errno.ENOENT):
                        continue
                    raise
        if self.requirements:
            for i in self.requirements:
                source = os.path.join(self.testdir, i)
                if os.path.basename(source):
                    dst = os.path.join(self.vardir, os.path.basename(source))
                else:
                    basedirname = source.split('/')[-1]
                    if not basedirname:
                        basedirname = source.split('/')[-2]
                    dst = os.path.join(self.vardir, basedirname)
                try:
                    if os.path.isdir(source):
                        shutil.copytree(source, dst)
                    else:
                        shutil.copy(source, self.vardir)
                except IOError as e:
                    print e
                    if (e.errno == errno.ENOENT):
                        continue
                    raise
        self.listen = str(find_port())
        os.putenv("LISTEN", self.listen)
        shutil.copy(os.path.join(self.TEST_RUN_DIR, 'test_run.lua'),
                    self.vardir)

        # Note: we don't know the instance name of the tarantool server, so
        # cannot check length of path of *.control unix socket created by it.
        # So for 'app' tests type we don't check *.control unix sockets paths.

    def stop(self, silent):
        if not self.process:
            return
        color_log('AppServer.stop(): stopping the %s\n'
            % format_process(self.process.pid), schema='test_var')
        try:
            self.process.terminate()
        except OSError:
            pass

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
        test_suite.tests = [AppTest(k, test_suite.args, test_suite.ini) for k in sorted(glob.glob(os.path.join(suite_path, "*.test.lua" )))]
        test_suite.tests = sum(map((lambda x: patterned(x, test_suite.args.tests)), test_suite.tests), [])

    def print_log(self, lines):
        pass
