import errno
import glob
import os
import re
import shutil
import signal
import sys

from gevent.subprocess import Popen

from lib.colorer import color_stdout
from lib.colorer import color_log
from lib.colorer import qa_notice
from lib.options import Options
from lib.preprocessor import TestState
from lib.sampler import sampler
from lib.server import Server
from lib.server import DEFAULT_SNAPSHOT_NAME
from lib.tarantool_server import Test
from lib.tarantool_server import TarantoolServer
from lib.tarantool_server import TarantoolStartError
from lib.utils import format_process
from lib.utils import signame
from lib.utils import warn_unix_socket
from threading import Timer
from lib.test import TestRunGreenlet, TestExecutionError


def timeout_handler(server_process, test_timeout):
    color_stdout("Test timeout of %d secs reached\t" % test_timeout, schema='error')
    server_process.kill()


def run_server(execs, cwd, server, logfile, retval, test_id):
    os.putenv("LISTEN", server.listen_uri)
    with open(logfile, 'ab') as f:
        server.process = Popen(execs, stdout=sys.stdout, stderr=f, cwd=cwd)
    sampler.register_process(server.process.pid, test_id, server.name)
    test_timeout = Options().args.test_timeout
    timer = Timer(test_timeout, timeout_handler, (server.process, test_timeout))
    timer.start()
    retval['returncode'] = server.process.wait()
    timer.cancel()
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
                                    server.logfile, retval, self.id)
        self.current_test_greenlet = tarantool

        # Copy the snapshot right before starting the server.
        # Otherwise pretest_clean() would remove it.
        if server.snapshot_path:
            snapshot_dest = os.path.join(server.vardir, DEFAULT_SNAPSHOT_NAME)
            color_log("Copying snapshot {} to {}\n".format(
                server.snapshot_path, snapshot_dest))
            shutil.copy(server.snapshot_path, snapshot_dest)

        try:
            tarantool.start()
            tarantool.join()
        except TarantoolStartError:
            # A non-default server failed to start.
            raise TestExecutionError
        finally:
            self.teardown(server, ts)
        if retval.get('returncode', None) != 0:
            raise TestExecutionError

    def teardown(self, server, ts):
        # Stop any servers created by the test, except the
        # default one.
        #
        # See a comment in LuaTest.execute() for motivation of
        # SIGKILL usage.
        ts.stop_nondefault(signal=signal.SIGKILL)

        # When a supplementary (non-default) server fails, we
        # should not leave the process that executes an app test.
        # Let's kill it.
        #
        # Reuse AppServer.stop() code for convenience.
        server.stop(signal=signal.SIGKILL)


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
        self.lua_libs = ini['lua_libs']
        self.name = 'app_server'
        self.process = None
        self.localhost = '127.0.0.1'
        self.use_unix_sockets_iproto = ini['use_unix_sockets_iproto']

    @property
    def logfile(self):
        # remove suite name using basename
        test_name = os.path.basename(self.current_test.name)
        # add .conf_name if any
        if self.current_test.conf_name is not None:
            test_name += '.' + self.current_test.conf_name
        # add '.tarantool.log'
        file_name = test_name + '.tarantool.log'
        # put into vardir
        return os.path.join(self.vardir, file_name)

    def prepare_args(self, args=[]):
        # Disable stdout bufferization.
        cli_args = [self.binary, '-e', "io.stdout:setvbuf('no')"]

        # Disable schema upgrade if requested.
        if self.disable_schema_upgrade:
            cli_args.extend(['-e', self.DISABLE_AUTO_UPGRADE])

        # Add path to the script (the test).
        cli_args.extend([os.path.join(os.getcwd(), self.current_test.name)])

        # Add extra args if provided.
        cli_args.extend(args)

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
            path = os.path.join(self.vardir, self.name + ".i")
            warn_unix_socket(path)
            self.listen_uri = path
        else:
            self.listen_uri = self.localhost + ':0'
        shutil.copy(os.path.join(self.TEST_RUN_DIR, 'test_run.lua'),
                    self.vardir)

        # Note: we don't know the instance name of the tarantool server, so
        # cannot check length of path of *.control unix socket created by it.
        # So for 'app' tests type we don't check *.control unix sockets paths.

    def stop(self, silent=True, signal=signal.SIGTERM):
        # FIXME: Extract common parts of AppServer.stop() and
        # TarantoolServer.stop() to an utility function.

        color_log('DEBUG: [app server] Stopping the server...\n',
                  schema='info')

        if not self.process:
            color_log(' | Nothing to do: the process does not exist\n',
                      schema='info')
            return

        if self.process.returncode:
            if self.process.returncode < 0:
                signaled_by = -self.process.returncode
                color_log(' | Nothing to do: the process was terminated by '
                          'signal {} ({})\n'.format(signaled_by,
                                                    signame(signaled_by)),
                          schema='info')
            else:
                color_log(' | Nothing to do: the process was exited with code '
                          '{}\n'.format(self.process.returncode),
                          schema='info')
            return

        color_log(' | Sending signal {0} ({1}) to {2}\n'.format(
                  signal, signame(signal),
                  format_process(self.process.pid)))
        try:
            self.process.send_signal(signal)
        except OSError:
            pass

        # Waiting for stopping the server. If the timeout
        # reached, send SIGKILL.
        timeout = 5

        def kill():
            qa_notice('The app server does not stop during {} '
                      'seconds after the {} ({}) signal.\n'
                      'Info: {}\n'
                      'Sending SIGKILL...'.format(
                          timeout, signal, signame(signal),
                          format_process(self.process.pid)))
            try:
                self.process.kill()
            except OSError:
                pass

        timer = Timer(timeout, kill)
        timer.start()
        self.process.wait()
        timer.cancel()

    @classmethod
    def find_exe(cls, builddir):
        cls.builddir = builddir
        cls.binary = TarantoolServer.binary
        cls.debug = bool(re.findall(r'^Target:.*-Debug$', str(cls.version()),
                                    re.M))

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
                ) for conf_name, params in runs.items()
                    if is_correct(conf_name)])
            else:
                tests.append(AppTest(test_name,
                                     test_suite.args,
                                     test_suite.ini))

        test_suite.tests = tests
