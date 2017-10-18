import errno
import gc
import glob
import os
import os.path
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

import gevent
import yaml
from gevent import socket

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

import inspect  # for caller_globals

from lib.test import Test
from lib.server import Server
from lib.preprocessor import TestState
from lib.box_connection import BoxConnection
from lib.admin_connection import AdminConnection, AdminAsyncConnection
from lib.utils import find_port
from lib.utils import signame
from lib.utils import warn_unix_socket
from lib.utils import format_process

from greenlet import greenlet, GreenletExit
from test import TestRunGreenlet, TestExecutionError

from lib.colorer import color_stdout, color_log


def save_join(green_obj, timeout=None):
    """
    Gevent join wrapper for
    test-run stop-on-crash feature

    :return True in case of crash and False otherwise
    """
    try:
        green_obj.join(timeout=timeout)
    except GreenletExit:
        return True
    return False


class FuncTest(Test):
    def execute(self, server):
        server.current_test = self
        execfile(self.name, dict(locals(), **server.__dict__))


class LuaTest(FuncTest):
    TIMEOUT = 60 * 10

    def exec_loop(self, ts):
        cmd = None

        def send_command(command):
            result = ts.curcon[0](command, silent=True)
            for conn in ts.curcon[1:]:
                conn(command, silent=True)
            # gh-24 fix
            if result is None:
                result = '[Lost current connection]\n'
            return result

        for line in open(self.name, 'r'):
            if not line.endswith('\n'):
                line += '\n'
            # context switch for inspector after each line
            if not cmd:
                cmd = StringIO()
            if line.find('--') == 0:
                sys.stdout.write(line)
            else:
                if line.strip() or cmd.getvalue():
                    cmd.write(line)
                delim_len = -len(ts.delimiter) if len(ts.delimiter) else None
                if line.endswith(ts.delimiter + '\n') and cmd.getvalue().strip()[:delim_len].strip():
                    sys.stdout.write(cmd.getvalue())
                    rescom = cmd.getvalue()[:delim_len].replace('\n\n', '\n')
                    result = send_command(rescom)
                    sys.stdout.write(result.replace("\r\n", "\n"))
                    cmd.close()
                    cmd = None
            # join inspector handler
            self.inspector.sem.wait()
        # stop any servers created by the test, except the default one
        ts.stop_nondefault()

    def killall_servers(self, server, ts, crash_occured):
        """ kill all servers and crash detectors before stream swap """
        check_list = ts.servers.values() + [server, ]

        # check that all servers stopped correctly
        for server in check_list:
            crash_occured = crash_occured or server.process.returncode not in (None, 0, -signal.SIGKILL, -signal.SIGTERM)

        for server in check_list:
            server.process.poll()

            if crash_occured:
                # kill all servers and crash detectors on crash
                if server.process.returncode is None:
                    server.process.kill()
                gevent.kill(server.crash_detector)
            elif server.process.returncode is not None:
                # join crash detectors of stopped servers
                save_join(server.crash_detector)

    def execute(self, server):
        server.current_test = self
        cls_name = server.__class__.__name__.lower()
        if 'gdb' in cls_name or 'lldb' in cls_name or 'strace' in cls_name:
            # don't propagate gdb/lldb/strace mixin to non-default servers, it doesn't
            # work properly for now
            # TODO: strace isn't interactive, so it's easy to make it works for
            #       non-default server
            create_server = TarantoolServer
        else:
            # propagate valgrind mixin to non-default servers
            create_server = server.__class__
        ts = TestState(
            self.suite_ini, server, create_server,
            self.run_params
        )
        self.inspector.set_parser(ts)
        lua = TestRunGreenlet(self.exec_loop, ts)
        self.current_test_greenlet = lua
        lua.start()
        crash_occured = True
        try:
            crash_occured = save_join(lua, timeout=self.TIMEOUT)
            self.killall_servers(server, ts, crash_occured)
        except KeyboardInterrupt:
            # prevent tests greenlet from writing to the real stdout
            lua.kill()

            ts.stop_nondefault()
            raise

class PythonTest(FuncTest):
    def execute(self, server):
        server.current_test = self
        execfile(self.name, dict(locals(), test_run_current_test=self,
                                 **server.__dict__))
        # crash was detected (possibly on non-default server)
        if server.current_test.is_crash_reported:
            raise TestExecutionError

CON_SWITCH = {
    'lua': AdminAsyncConnection,
    'python': AdminConnection
}


class TarantoolStartError(OSError):
    pass


class TarantoolLog(object):
    def __init__(self, path):
        self.path = path
        self.log_begin = 0

    def positioning(self):
        if os.path.exists(self.path):
            with open(self.path, 'r') as f:
                f.seek(0, os.SEEK_END)
                self.log_begin = f.tell()
        return self

    def seek_once(self, msg):
        if not os.path.exists(self.path):
            return -1
        with open(self.path, 'r') as f:
            f.seek(self.log_begin, os.SEEK_SET)
            while True:
                log_str = f.readline()

                if not log_str:
                    return -1
                pos = log_str.find(msg)
                if pos != -1:
                    return pos

    def seek_wait(self, msg, proc=None):
        while True:
            if os.path.exists(self.path):
                break
            time.sleep(0.001)

        with open(self.path, 'r') as f:
            f.seek(self.log_begin, os.SEEK_SET)
            cur_pos = self.log_begin
            while True:
                if not (proc is None):
                    if not (proc.poll() is None):
                        raise TarantoolStartError
                log_str = f.readline()
                if not log_str:
                    time.sleep(0.001)
                    f.seek(cur_pos, os.SEEK_SET)
                    continue
                if re.findall(msg, log_str):
                    return
                cur_pos = f.tell()


class TarantoolServer(Server):
    default_tarantool = {
        "bin": "tarantool",
        "logfile": "tarantool.log",
        "pidfile": "tarantool.pid",
        "name": "default",
        "ctl": "tarantoolctl",
    }

    # ----------------------------------PROPERTIES----------------------------------#
    @property
    def debug(self):
        return self.test_debug()

    @property
    def name(self):
        if not hasattr(self, '_name') or not self._name:
            return self.default_tarantool["name"]
        return self._name

    @name.setter
    def name(self, val):
        self._name = val

    @property
    def logfile(self):
        if not hasattr(self, '_logfile') or not self._logfile:
            return os.path.join(self.vardir, self.default_tarantool["logfile"])
        return self._logfile

    @logfile.setter
    def logfile(self, val):
        self._logfile = os.path.join(self.vardir, val)

    @property
    def pidfile(self):
        if not hasattr(self, '_pidfile') or not self._pidfile:
            return os.path.join(self.vardir, self.default_tarantool["pidfile"])
        return self._pidfile

    @pidfile.setter
    def pidfile(self, val):
        self._pidfile = os.path.join(self.vardir, val)

    @property
    def builddir(self):
        if not hasattr(self, '_builddir'):
            raise ValueError("No build-dir is specified")
        return self._builddir

    @builddir.setter
    def builddir(self, val):
        if val is None:
            return
        self._builddir = os.path.abspath(val)

    @property
    def script_dst(self):
        return os.path.join(self.vardir, os.path.basename(self.script))

    @property
    def logfile_pos(self):
        if not hasattr(self, '_logfile_pos'): self._logfile_pos = None
        return self._logfile_pos

    @logfile_pos.setter
    def logfile_pos(self, val):
        self._logfile_pos = TarantoolLog(val).positioning()

    @property
    def script(self):
        if not hasattr(self, '_script'): self._script = None
        return self._script

    @script.setter
    def script(self, val):
        if val is None:
            if hasattr(self, '_script'):
                delattr(self, '_script')
            return
        self._script = os.path.abspath(val)
        self.name = os.path.basename(self._script).split('.')[0]

    @property
    def _admin(self):
        if not hasattr(self, 'admin'): self.admin = None
        return self.admin

    @_admin.setter
    def _admin(self, port):
        if hasattr(self, 'admin'):
            del self.admin
        if not hasattr(self, 'tests_type'):
            self.tests_type = 'lua'
        self.admin = CON_SWITCH[self.tests_type]('localhost', port)

    @property
    def _iproto(self):
        if not hasattr(self, 'iproto'): self.iproto = None
        return self.iproto

    @_iproto.setter
    def _iproto(self, port):
        try:
            port = int(port)
        except ValueError as e:
            raise ValueError("Bad port number: '%s'" % port)
        if hasattr(self, 'iproto'):
            del self.iproto
        self.iproto = BoxConnection('localhost', port)

    @property
    def log_des(self):
        if not hasattr(self, '_log_des'): self._log_des = open(self.logfile, 'a')
        return self._log_des

    @log_des.deleter
    def log_des(self):
        if not hasattr(self, '_log_des'): return
        if not self._log_des.closed: self._log_des.closed()
        delattr(self, _log_des)

    @property
    def rpl_master(self):
        if not hasattr(self, '_rpl_master'): self._rpl_master = None
        return self._rpl_master

    @rpl_master.setter
    def rpl_master(self, val):
        if not isinstance(self, (TarantoolServer, None)):
            raise ValueError('Replication master must be Tarantool'
                             ' Server class, his derivation or None')
        self._rpl_master = val

    # ------------------------------------------------------------------------------#

    def __new__(cls, ini=None, *args, **kwargs):
        cls = Server.get_mixed_class(cls, ini)
        return object.__new__(cls)

    def __init__(self, _ini=None, test_suite=None):
        if _ini is None:
            _ini = {}
        ini = {
            'core': 'tarantool',
            'gdb': False,
            'lldb': False,
            'script': None,
            'lua_libs': [],
            'valgrind': False,
            'vardir': None,
            'use_unix_sockets': False,
            'tarantool_port': None,
            'strace': False
        }
        ini.update(_ini)
        Server.__init__(self, ini, test_suite)
        self.testdir = os.path.abspath(os.curdir)
        self.sourcedir = os.path.abspath(os.path.join(os.path.basename(
            sys.argv[0]), "..", ".."))
        self.re_vardir_cleanup += [
            "*.snap", "*.xlog", "*.vylog", "*.inprogress",
            "*.sup", "*.lua", "*.pid", "[0-9]*/"]
        self.name = "default"
        self.conf = {}
        self.status = None
        self.environ = None
        # -----InitBasicVars-----#
        self.core = ini['core']

        self.gdb = ini['gdb']
        self.lldb = ini['lldb']
        self.script = ini['script']
        self.lua_libs = ini['lua_libs']
        self.valgrind = ini['valgrind']
        self.strace = ini['strace']
        self.use_unix_sockets = ini['use_unix_sockets']
        self._start_against_running = ini['tarantool_port']
        self.crash_detector = None
        # use this option with inspector
        # to enable crashes in test
        self.crash_enabled = False

        # set in from a test let test-run ignore server's crashes
        self.crash_expected = False

        # filled in {Test,FuncTest,LuaTest,PythonTest}.execute()
        # or passed through execfile() for PythonTest
        self.current_test = None
        caller_globals = inspect.stack()[1][0].f_globals
        if 'test_run_current_test' in caller_globals.keys():
            self.current_test = caller_globals['test_run_current_test']

    def __del__(self):
        self.stop()

    @classmethod
    def version(cls):
        p = subprocess.Popen([cls.binary, "--version"], stdout=subprocess.PIPE)
        version = p.stdout.read().rstrip()
        p.wait()
        return version

    @classmethod
    def find_exe(cls, builddir, silent=True):
        cls.builddir = os.path.abspath(builddir)
        builddir = os.path.join(builddir, "src")
        path = builddir + os.pathsep + os.environ["PATH"]
        color_log("Looking for server binary in ", schema='serv_text')
        color_log(path + ' ...\n', schema='path')
        for _dir in path.split(os.pathsep):
            exe = os.path.join(_dir, cls.default_tarantool["bin"])
            ctl_dir = _dir
            # check local tarantoolctl source
            if _dir == builddir:
                ctl_dir = os.path.join(_dir, '../extra/dist')

            ctl = os.path.join(ctl_dir, cls.default_tarantool['ctl'])
            need_lua_path = False
            if os.path.isdir(ctl) or not os.access(ctl, os.X_OK):
                ctl_dir = os.path.join(_dir, '../extra/dist')
                ctl = os.path.join(ctl_dir, cls.default_tarantool['ctl'])
                need_lua_path = True
            if os.access(exe, os.X_OK) and os.access(ctl, os.X_OK):
                cls.binary      = os.path.abspath(exe)
                cls.ctl_path    = os.path.abspath(ctl)
                cls.ctl_plugins = os.path.abspath(
                    os.path.join(ctl_dir, '..')
                )
                os.environ["PATH"] = os.pathsep.join([
                        os.path.abspath(ctl_dir),
                        os.path.abspath(_dir),
                        os.environ["PATH"]
                ])
                os.environ["TARANTOOLCTL"] = ctl
                if need_lua_path:
                    os.environ["LUA_PATH"] = ctl_dir + '/?.lua;' + \
                                             ctl_dir + '/?/init.lua;' + \
                                             os.environ.get("LUA_PATH", ";;")
                return exe
        raise RuntimeError("Can't find server executable in " + path)

    @classmethod
    def print_exe(cls):
        color_stdout('Installing the server ...\n', schema='serv_text')

        if cls.binary:
            color_stdout('    Found executable   at ', schema='serv_text')
            color_stdout(cls.binary + '\n', schema='path')

        if cls.ctl_path:
            color_stdout('    Found tarantoolctl at ', schema='serv_text')
            color_stdout(cls.ctl_path + '\n', schema='path')

        color_stdout("\n", cls.version(), "\n", schema='version')

    def install(self, silent=True):
        if self._start_against_running:
            self._iproto = self._start_against_running
            self._admin = int(self._start_against_running) + 1
            return
        color_log('Installing the server ...\n', schema='serv_text')
        color_log('    Found executable at ', schema='serv_text')
        color_log(self.binary + '\n', schema='path')
        color_log('    Found tarantoolctl at  ', schema='serv_text')
        color_log(self.ctl_path + '\n', schema='path')
        color_log('    Creating and populating working directory in ', schema='serv_text')
        color_log(self.vardir + ' ...\n', schema='path')
        if not os.path.exists(self.vardir):
            os.makedirs(self.vardir)
        else:
            color_log('    Found old vardir, deleting ...\n', schema='serv_text')
            self.kill_old_server()
            self.cleanup()
        self.copy_files()

        if self.use_unix_sockets:
            self._admin = os.path.join(self.vardir, "socket-admin")
        else:
            self._admin = find_port()

        self._iproto = find_port()

        # these sockets will be created by tarantool itself
        path = os.path.join(self.vardir, self.name + '.control')
        warn_unix_socket(path)

    def deploy(self, silent=True, **kwargs):
        self.install(silent)
        self.start(silent=silent, **kwargs)

    def copy_files(self):
        if self.script:
            shutil.copy(self.script, self.script_dst)
            os.chmod(self.script_dst, 0777)
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
        shutil.copy('.tarantoolctl', self.vardir)
        shutil.copy(os.path.join(self.TEST_RUN_DIR, 'test_run.lua'),
                    self.vardir)

    def prepare_args(self):
        return [self.ctl_path, 'start', os.path.basename(self.script)]

    def start(self, silent=True, wait=True, wait_load=True, rais=True,
              **kwargs):
        if self._start_against_running:
            return
        if self.status == 'started':
            if not silent:
                color_stdout('The server is already started.\n', schema='lerror')
            return

        args = self.prepare_args()
        self.pidfile = '%s.pid' % self.name
        self.logfile = '%s.log' % self.name

        path = self.script_dst if self.script else \
            os.path.basename(self.binary)
        color_log("Starting the server ...\n", schema='serv_text')
        color_log("Starting ", schema='serv_text')
        color_log(path + " \n", schema='path')
        color_log(self.version() + "\n", schema='version')

        # prepare test environment
        env = os.environ.copy()
        if self.environ is not None:
            for key in self.environ:
                env[key.upper()] = self.environ[key]
        env["LISTEN"] = self.iproto.uri
        env["ADMIN"] = self.admin.uri
        if self.rpl_master:
            env["MASTER"] = self.rpl_master.iproto.uri
        self.logfile_pos = self.logfile

        # redirect stdout from tarantoolctl and tarantool
        env["TEST_WORKDIR"] = self.vardir
        self.process = subprocess.Popen(args,
                                        cwd=self.vardir,
                                        stdout=self.log_des,
                                        stderr=self.log_des,
                                        env=env)

        # gh-19 crash detection
        self.crash_detector = TestRunGreenlet(self.crash_detect)
        self.crash_detector.info = "Crash detector: %s" % self.process
        self.crash_detector.start()
        wait = wait
        wait_load = wait_load
        if wait:
            try:
                self.wait_until_started(wait_load)
            except TarantoolStartError:
                # Raise exception when caller ask for it (e.g. in case of
                # non-default servers)
                if rais:
                    raise
                # Python tests expect we raise an exception when non-default
                # server fails
                if self.crash_expected:
                    raise
                if not self.current_test or not self.current_test.is_crash_reported:
                    if self.current_test:
                        self.current_test.is_crash_reported = True
                    color_stdout('\n[Instance "{}"] Tarantool server failed to start\n'.format(
                        self.name), schema='error')
                    self.print_log(15)
                # if the server fails before any test started, we should inform
                # a caller by the exception
                if not self.current_test:
                    raise
                self.kill_current_test()

        port = self.admin.port
        self.admin.disconnect()
        self.admin = CON_SWITCH[self.tests_type]('localhost', port)
        self.status = 'started'

    def crash_detect(self):
        if self.crash_expected:
            return

        while self.process.returncode is None:
            self.process.poll()
            if self.process.returncode is None:
                gevent.sleep(0.1)

        if self.process.returncode in [0, -signal.SIGKILL, -signal.SIGTERM]:
           return

        self.kill_current_test()

        if not os.path.exists(self.logfile):
            return

        if not self.current_test.is_crash_reported:
            self.current_test.is_crash_reported = True
            self.crash_grep()

    def crash_grep(self):
        print_log_lines = 15
        assert_fail_re = re.compile(r'^.*: Assertion .* failed\.$')

        # find and save backtrace or assertion fail
        assert_lines = list()
        bt = list()
        with open(self.logfile, 'r') as log:
            lines = log.readlines()
            for rpos, line in enumerate(reversed(lines)):
                if line.startswith('Segmentation fault'):
                    bt = lines[-rpos - 1:]
                    break
                if assert_fail_re.match(line):
                    pos = len(lines) - rpos
                    assert_lines = lines[max(0, pos - print_log_lines):pos]
                    break
            else:
                bt = list()

        # print insident meat
        if self.process.returncode < 0:
            color_stdout('\n\n[Instance "%s" killed by signal: %d (%s)]\n' % (
                self.name, -self.process.returncode,
                signame(-self.process.returncode)), schema='error')
        else:
            color_stdout('\n\n[Instance "%s" returns with non-zero exit code: %d]\n' % (
                self.name, self.process.returncode), schema='error')

        # print assert line if any and return
        if assert_lines:
            color_stdout('Found assertion fail in the results file [%s]:\n' % self.logfile, schema='error')
            sys.stderr.flush()
            for line in assert_lines:
                sys.stderr.write(line)
            sys.stderr.flush()
            return

        # print backtrace if any
        sys.stderr.flush()
        for trace in bt:
            sys.stderr.write(trace)

        # print log otherwise (if backtrace was not found)
        if not bt:
            self.print_log(print_log_lines)
        sys.stderr.flush()

    def kill_current_test(self):
        """ Unblock save_join() call inside LuaTest.execute(), which doing
            necessary servers/greenlets clean up.
        """
        # current_test_greenlet is None for PythonTest
        if self.current_test.current_test_greenlet:
            gevent.kill(self.current_test.current_test_greenlet)

    def wait_stop(self):
        self.process.wait()

    def cleanup(self, full=False):
        try:
            shutil.rmtree(os.path.join(self.vardir, self.name))
        except OSError:
            pass

    def stop(self, silent=True):
        if self._start_against_running:
            return
        if self.status != 'started':
            if not silent:
                raise Exception('Server is not started')
            return
        if not silent:
            color_stdout('Stopping the server ...\n', schema='serv_text')
        # kill only if process is alive
        if self.process is not None and self.process.returncode is None:
            color_log('TarantoolServer.stop(): stopping the %s\n'
                % format_process(self.process.pid), schema='test_var')
            try:
                self.process.terminate()
            except OSError:
                pass
            if self.crash_detector is not None:
                save_join(self.crash_detector)
            self.wait_stop()

        self.status = None
        if re.search(r'^/', str(self._admin.port)):
            if os.path.exists(self._admin.port):
                os.unlink(self._admin.port)

    def restart(self):
        self.stop()
        self.start()

    def kill_old_server(self, silent=True):
        pid = self.read_pidfile()
        if pid == -1:
            return False
        if not silent:
            color_stdout('    Found old server, pid {0}, killing ...'.format(pid), schema='info')
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        self.wait_until_stopped(pid)
        return True

    def wait_until_started(self, wait_load=True):
        """ Wait until server is started.

        Server consists of two parts:
        1) wait until server is listening on sockets
        2) wait until server tells us his status

        """
        if wait_load:
            msg = 'entering the event loop|will retry binding|hot standby mode'
            self.logfile_pos.seek_wait(
                msg, self.process if not self.gdb and not self.lldb else None)
        while True:
            try:
                temp = AdminConnection('localhost', self.admin.port)
                if not wait_load:
                    ans = yaml.load(temp.execute("2 + 2"))
                    return True
                ans = yaml.load(temp.execute('box.info.status'))[0]
                if ans in ('running', 'hot_standby', 'orphan'):
                    return True
                elif ans in ('loading'):
                    continue
                else:
                    raise Exception("Strange output for `box.info.status`: %s" % (ans))
            except socket.error as e:
                if e.errno == errno.ECONNREFUSED:
                    time.sleep(0.1)
                    continue
                raise

    def wait_until_stopped(self, pid):
        while True:
            try:
                time.sleep(0.01)
                os.kill(pid, 0)
                continue
            except OSError as err:
                break

    def read_pidfile(self):
        pid = -1
        if os.path.exists(self.pidfile):
            try:
                with open(self.pidfile) as f:
                    pid = int(f.read())
            except:
                pass
        return pid

    def print_log(self, lines):
        color_stdout('\nLast {0} lines of Tarantool Log file [Instance "{1}"][{2}]:\n'.format(
            lines, self.name, self.logfile or 'null'), schema='error')
        if os.path.exists(self.logfile):
            with open(self.logfile, 'r') as log:
                color_stdout(''.join(log.readlines()[-lines:]))
        else:
            color_stdout("    Can't find log:\n", schema='error')

    def test_option_get(self, option_list_str, silent=False):
        args = [self.binary] + shlex.split(option_list_str)
        if not silent:
            print " ".join([os.path.basename(self.binary)] + args[1:])
        output = subprocess.Popen(args, cwd=self.vardir, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT).stdout.read()
        return output

    def test_option(self, option_list_str):
        print self.test_option_get(option_list_str)

    def test_debug(self):
        if re.findall(r"-Debug", self.test_option_get("-V", True), re.I):
            return True
        return False

    @staticmethod
    def find_tests(test_suite, suite_path):
        test_suite.ini['suite'] = suite_path
        get_tests = lambda x: sorted(glob.glob(os.path.join(suite_path, x)))
        tests = [PythonTest(k, test_suite.args, test_suite.ini)
                 for k in get_tests("*.test.py")
                 ]
        for k in get_tests("*.test.lua"):
            runs = test_suite.get_multirun_params(k)
            is_correct = lambda x: test_suite.args.conf is None or \
                                   test_suite.args.conf == x
            if runs:
                tests.extend([LuaTest(
                    k, test_suite.args,
                    test_suite.ini, runs[r], r
                ) for r in runs.keys() if is_correct(r)])
            else:
                tests.append(LuaTest(k, test_suite.args, test_suite.ini))

        test_suite.tests = []
        # don't sort, command line arguments must be run in
        # the specified order
        for name in test_suite.args.tests:
            for test in tests:
                if test.name.find(name) != -1:
                    test_suite.tests.append(test)

    def get_param(self, param=None):
        if not param is None:
            return yaml.load(self.admin("box.info." + param, silent=True))[0]
        return yaml.load(self.admin("box.info", silent=True))

    def get_lsn(self, node_id):
        nodes = self.get_param("vclock")
        if type(nodes) == dict and node_id in nodes:
            return int(nodes[node_id])
        elif type(nodes) == list and node_id <= len(nodes):
            return int(nodes[node_id - 1])
        else:
            return -1

    def wait_lsn(self, node_id, lsn):
        while (self.get_lsn(node_id) < lsn):
            # print("wait_lsn", node_id, lsn, self.get_param("vclock"))
            time.sleep(0.01)

    def get_log(self):
        return TarantoolLog(self.logfile).positioning()
