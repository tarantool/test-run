import errno
import glob
import os
import shutil
import sys

from gevent.subprocess import Popen, PIPE

from lib.colorer import color_log
from lib.preprocessor import TestState
from lib.server import Server
from lib.server import DEFAULT_SNAPSHOT_NAME
from lib.tarantool_server import Test
from lib.tarantool_server import TarantoolServer
from lib.tarantool_server import TarantoolStartError
from lib.utils import find_port
from lib.utils import format_process
from lib.utils import warn_unix_socket
from test import TestRunGreenlet, TestExecutionError
from xlog import prepare_bootstrap_snapshot


def run_server(execs, cwd, server, logfile, retval):
    os.putenv("LISTEN", server.iproto)
    server.process = Popen(execs, stdout=PIPE, stderr=PIPE, cwd=cwd)
    stdout, stderr = server.process.communicate()
    sys.stdout.write(stdout)
    with open(logfile, 'a') as f:
        f.write(stderr)
    retval['returncode'] = server.process.wait()
    server.process = None


class AppTest(Test):
    def execute(self, server):
        super(AppTest, self).execute(server)
        ts = TestState(self.suite_ini, None, TarantoolServer,
                       self.run_params,
                       default_server_no_connect=server)
        self.inspector.set_parser(ts)

        execs = server.prepare_args()
        retval = dict()
        tarantool = TestRunGreenlet(run_server, execs, server.vardir, server,
                                    server.logfile, retval)
        self.current_test_greenlet = tarantool

        # Copy the snapshot right before starting the server.
        # Otherwise pretest_clean() would remove it.
        if server.snapshot_path:
            snapshot_dest = os.path.join(server.vardir, DEFAULT_SNAPSHOT_NAME)
            color_log("Copying snapshot {} to {}\n".format(
                server.snapshot_path, snapshot_dest))
            shutil.copy(server.snapshot_path, snapshot_dest)
            if server.is_snapshot_for_bootstrap:
                # Temporary solution until gh-5540 will be
                # implemented.
                color_log('Preparing snapshot {} for local '
                          'recovery...\n'.format(snapshot_dest))
                prepare_bootstrap_snapshot(snapshot_dest)

        try:
            tarantool.start()
            tarantool.join()
        except TarantoolStartError:
            # A non-default server failed to start.
            raise TestExecutionError
        if retval['returncode'] != 0:
            raise TestExecutionError


class AppServer(Server):
    """A dummy server implementation for application server tests"""
    def __new__(cls, ini=None, *args, **kwargs):
        cls = Server.get_mixed_class(cls, ini)
        return object.__new__(cls)

    def __init__(self, _ini=None, test_suite=None):
        ini = dict(vardir=None)
        ini.update({} if _ini is None else _ini)
        Server.__init__(self, ini, test_suite)
        self.testdir = os.path.abspath(os.curdir)
        self.vardir = ini['vardir']
        self.builddir = ini['builddir']
        self.debug = False
        self.lua_libs = ini['lua_libs']
        self.name = 'app_server'
        self.process = None
        self.binary = TarantoolServer.binary
        self.use_unix_sockets_iproto = ini['use_unix_sockets_iproto']

    @property
    def logfile(self):
        # remove suite name using basename
        test_name = os.path.basename(self.current_test.name)
        # add :conf_name if any
        if self.current_test.conf_name is not None:
            test_name += ':' + self.current_test.conf_name
        # add '.tarantool.log'
        file_name = test_name + '.tarantool.log'
        # put into vardir
        return os.path.join(self.vardir, file_name)

    def prepare_args(self, args=[]):
        cli_args = [os.path.join(os.getcwd(), self.current_test.name)] + args
        if self.disable_schema_upgrade:
            cli_args = [self.binary, '-e',
                        self.DISABLE_AUTO_UPGRADE] + cli_args

        return cli_args

    def deploy(self, vardir=None, silent=True, need_init=True):
        self.vardir = vardir
        if not os.access(self.vardir, os.F_OK):
            os.makedirs(self.vardir)
        if self.lua_libs:
            for i in self.lua_libs:
                source = os.path.join(self.testdir, i)
                try:
                    if os.path.isdir(source):
                        shutil.copytree(source,
                                        os.path.join(self.vardir,
                                                     os.path.basename(source)))
                    else:
                        shutil.copy(source, self.vardir)
                except IOError as e:
                    if (e.errno == errno.ENOENT):
                        continue
                    raise
        if self.use_unix_sockets_iproto:
            path = os.path.join(self.vardir, self.name + ".socket-iproto")
            warn_unix_socket(path)
            self.iproto = path
        else:
            self.iproto = str(find_port())
        shutil.copy(os.path.join(self.TEST_RUN_DIR, 'test_run.lua'),
                    self.vardir)

        # Note: we don't know the instance name of the tarantool server, so
        # cannot check length of path of *.control unix socket created by it.
        # So for 'app' tests type we don't check *.control unix sockets paths.

    def stop(self, silent):
        if not self.process:
            return
        color_log('AppServer.stop(): stopping the %s\n' %
                  format_process(self.process.pid), schema='test_var')
        try:
            self.process.terminate()
        except OSError:
            pass

    @classmethod
    def find_exe(cls, builddir):
        cls.builddir = builddir

    @staticmethod
    def find_tests(test_suite, suite_path):
        def patterned(test_name, patterns):
            answer = []
            for i in patterns:
                if test_name.find(i) != -1:
                    answer.append(test_name)
            return answer

        def is_correct(run):
            return test_suite.args.conf is None or test_suite.args.conf == run

        test_suite.ini['suite'] = suite_path

        test_names = sorted(glob.glob(os.path.join(suite_path, "*.test.lua")))
        test_names = Server.exclude_tests(test_names, test_suite.args.exclude)
        test_names = sum(map((lambda x: patterned(x, test_suite.args.tests)),
                             test_names), [])
        tests = []

        for test_name in test_names:
            runs = test_suite.get_multirun_params(test_name)
            if runs:
                tests.extend([AppTest(
                    test_name,
                    test_suite.args,
                    test_suite.ini,
                    params=params,
                    conf_name=conf_name
                ) for conf_name, params in runs.iteritems()
                    if is_correct(conf_name)])
            else:
                tests.append(AppTest(test_name,
                                     test_suite.args,
                                     test_suite.ini))

        test_suite.tests = tests
