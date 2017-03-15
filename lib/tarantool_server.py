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

from itertools import product
from lib.test import Test
from lib.server import Server
from lib.preprocessor import TestState
from lib.box_connection import BoxConnection
from lib.admin_connection import AdminConnection, AdminAsyncConnection
from lib.utils import find_port
from lib.utils import check_port

from greenlet import greenlet, GreenletExit
from test import TestRunGreenlet, TestExecutionError

from lib.colorer import Colorer

color_stdout = Colorer()


def find_in_path(name):
    path = os.curdir + os.pathsep + os.environ["PATH"]
    for _dir in path.split(os.pathsep):
        exe = os.path.join(_dir, name)
        if os.access(exe, os.X_OK):
            return exe
    return ''


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
        ts.cleanup()  # XXX: put under finally block?

    def execute(self, server):
        server.current_test = self
        cls_name = server.__class__.__name__.lower()
        if 'gdb' in cls_name or 'lldb' in cls_name:
            # don't propagate gdb/lldb mixin to non-default servers, it doesn't
            # work properly for now
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
        crash_occured = save_join(lua, timeout=self.TIMEOUT)

        # kill all servers and crash detectors before stream swap
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

class PythonTest(FuncTest):
    def execute(self, server):
        server.current_test = self
        execfile(self.name, dict(locals(), test_run_current_test=self,
                                 **server.__dict__))
        # crash was detected (possibly on non-default server)
        if server.current_test.is_crash_reported:
            raise TestExecutionError

CON_SWITCH = {
    LuaTest: AdminAsyncConnection,
    PythonTest: AdminConnection
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


class Mixin(object):
    pass


class ValgrindMixin(Mixin):
    default_valgr = {
        "suppress_path": "share/",
        "suppress_name": "tarantool.sup"
    }

    def format_valgrind_log_path(self, suite_name, test_name, conf, server_name, num):
        basename = '{}.{}.{}.{}.{}.valgrind.log'.format(
            suite_name, test_name, conf, server_name, str(num))
        return os.path.join(self.vardir, basename)

    @property
    def valgrind_log(self):
        # suite.{test/default}.{conf/none}.instance.num.valgrind.log
        if self.test_suite:
            suite_name = os.path.basename(self.test_suite.suite_path)
            path = self.format_valgrind_log_path(
                suite_name, 'default', 'none', self.name, 1)
        else:
            suite_name = os.path.basename(self.current_test.suite_ini['suite'])
            test_name = os.path.basename(self.current_test.name)
            conf_name = self.current_test.conf_name or 'none'
            num = 1
            while True:
                path = self.format_valgrind_log_path(
                    suite_name, test_name, conf_name, self.name, num)
                if not os.path.isfile(path):
                    break
                num += 1
        return path

    def current_valgrind_logs(self, for_suite=False, for_test=False):
        if not self.test_suite or not self.current_test:
            raise ValueError(
                "The method should be called on a default suite's server.")
        if for_suite == for_test:
            raise ValueError('Set for_suite OR for_test to True')
        suite_name = os.path.basename(self.test_suite.suite_path)
        if for_test:
            test_name = os.path.basename(self.current_test.name)
            default_tmpl = self.format_valgrind_log_path(suite_name, 'default', '*', '*', '*')
            non_default_tmpl = self.format_valgrind_log_path(suite_name, test_name, '*', '*', '*')
            return sorted(glob.glob(default_tmpl) + glob.glob(non_default_tmpl))
        else:
            suite_tmpl = self.format_valgrind_log_path(suite_name, '*', '*', '*', '*')
            return sorted(glob.glob(suite_tmpl))

    @property
    def valgrind_sup(self):
        if not hasattr(self, '_valgrind_sup') or not self._valgrind_sup:
            return os.path.join(self.testdir,
                                self.default_valgr['suppress_path'],
                                self.default_valgr['suppress_name'])
        return self._valgrind_sup

    @valgrind_sup.setter
    def valgrind_sup(self, val):
        self._valgrind_sup = os.path.abspath(val)

    @property
    def valgrind_sup_output(self):
        return os.path.join(self.vardir, self.default_valgr['suppress_name'])

    def prepare_args(self):
        if not find_in_path('valgrind'):
            raise OSError('`valgrind` executables not found in PATH')
        return shlex.split("valgrind --log-file={log} --suppressions={sup} \
                --gen-suppressions=all --trace-children=yes --leak-check=full \
                --read-var-info=yes --quiet {bin}".format(
            log=self.valgrind_log,
            sup=self.valgrind_sup,
            bin=' '.join([self.ctl_path, 'start', os.path.basename(self.script)])
        ))

    def wait_stop(self):
        return self.process.wait()


class DebugMixin(Mixin):
    debugger_args = {
        "name": None,
        "debugger": None,
        "sh_string": None
    }

    def prepare_args(self):
        debugger = self.debugger_args['debugger']
        screen_name = self.debugger_args['name']
        sh_string = self.debugger_args['sh_string']

        if not find_in_path('screen'):
            raise OSError('`screen` executables not found in PATH')
        if not find_in_path(debugger):
            raise OSError('`%s` executables not found in PATH' % debugger)
        color_stdout('You started the server in %s mode.\n' % debugger,
                     schema='info')
        color_stdout('To attach, use `screen -r %s `\n' % screen_name,
                     schema='info')
        return shlex.split(sh_string.format(
            self.debugger_args['name'], self.binary,
            ' '.join([self.ctl_path, 'start', os.path.basename(self.script)]),
            self.logfile, debugger)
        )

    def wait_stop(self):
        self.kill_old_server()
        self.process.wait()


class GdbMixin(DebugMixin):
    debugger_args = {
        "name": "tarantool",
        "debugger": "gdb",
        "sh_string": """screen -dmS {0} {4} {1}
                        -ex 'b main' -ex 'run {2} >> {3} 2>> {3}' """
    }


class LLdbMixin(DebugMixin):
    debugger_args = {
        "name": "tarantool",
        "debugger": "lldb",
        "sh_string": """screen -dmS {0} {4} -f {1}
                        -o 'b main'
                        -o 'settings set target.run-args {2}'
                        -o 'process launch -o {3} -e {3}' """
    }


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
        if not hasattr(self, 'cls'):
            self.cls = LuaTest
        self.admin = CON_SWITCH[self.cls]('localhost', port)

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
        if ini is None:
            ini = {'core': 'tarantool'}

        conflict_options = ('valgrind', 'gdb', 'lldb')
        for op1, op2 in product(conflict_options, repeat=2):
            if op1 != op2 and \
                    (op1 in ini and ini[op1]) and \
                    (op2 in ini and ini[op2]):
                format_str = 'Can\'t run under {} and {} simultaniously'
                raise OSError(format_str.format(op1, op2))

        if 'valgrind' in ini and ini['valgrind']:
            cls = type('ValgrindTarantoolServer', (ValgrindMixin, TarantoolServer), {})
        elif 'gdb' in ini and ini['gdb']:
            cls = type('GdbTarantoolServer', (GdbMixin, TarantoolServer), {})
        elif 'lldb' in ini and ini['lldb']:
            cls = type('LLdbTarantoolServer', (LLdbMixin, TarantoolServer), {})

        return super(TarantoolServer, cls).__new__(cls)

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
            'tarantool_port': None
        }
        ini.update(_ini)
        Server.__init__(self, ini)
        self.testdir = os.path.abspath(os.curdir)
        self.sourcedir = os.path.abspath(os.path.join(os.path.basename(
            sys.argv[0]), "..", ".."))
        self.re_vardir_cleanup += [
            "*.snap", "*.xlog", "*.inprogress",
            "*.sup", "*.lua", "*.pid", "[0-9]*/"]
        self.name = "default"
        self.conf = {}
        self.status = None
        # -----InitBasicVars-----#
        self.core = ini['core']

        self.gdb = ini['gdb']
        self.lldb = ini['lldb']
        self.script = ini['script']
        self.lua_libs = ini['lua_libs']
        self.valgrind = ini['valgrind']
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

        # Used in valgrind_log property. 'test_suite' is not None only for
        # default servers running in TestSuite.run_all()
        self.test_suite = test_suite

    def __del__(self):
        self.stop()

    @classmethod
    def find_exe(cls, builddir, silent=True):
        cls.builddir = os.path.abspath(builddir)
        builddir = os.path.join(builddir, "src")
        path = builddir + os.pathsep + os.environ["PATH"]
        if not silent:
            color_stdout("Looking for server binary in ", schema='serv_text')
            color_stdout(path + ' ...\n', schema='path')
        for _dir in path.split(os.pathsep):
            exe = os.path.join(_dir, cls.default_tarantool["bin"])
            ctl_dir = _dir
            # check local tarantoolctl source
            if _dir == builddir:
                ctl_dir = os.path.join(_dir, '../extra/dist')

            ctl = os.path.join(ctl_dir, cls.default_tarantool['ctl'])
            if not os.access(ctl, os.X_OK):
                ctl_dir = os.path.join(_dir, '../extra/dist')
                ctl = os.path.join(ctl_dir, cls.default_tarantool['ctl'])
            if os.access(exe, os.X_OK) and os.access(ctl, os.X_OK):
                cls.binary = os.path.abspath(exe)
                os.environ["PATH"] = (os.path.abspath(ctl_dir) + os.pathsep +
                                      os.path.abspath(_dir) + os.pathsep +
                                      os.environ["PATH"])
                os.environ["TARANTOOLCTL_PLUGIN_PATH"] = os.path.abspath(
                        os.path.join(ctl_dir, 'plugins')
                )
                cls.ctl_path = os.path.abspath(ctl)
                return exe
        raise RuntimeError("Can't find server executable in " + path)

    def install(self, silent=True):
        if self._start_against_running:
            self._iproto = self._start_against_running
            self._admin = int(self._start_against_running) + 1
            return
        if not silent:
            color_stdout('Installing the server ...\n', schema='serv_text')
            color_stdout('    Found executable at ', schema='serv_text')
            color_stdout(self.binary + '\n', schema='path')
            color_stdout('    Found tarantoolctl at  ', schema='serv_text')
            color_stdout(self.ctl_path + '\n', schema='path')
            color_stdout('    Creating and populating working directory in ', schema='serv_text')
            color_stdout(self.vardir + ' ...\n', schema='path')
        if not os.path.exists(self.vardir):
            os.makedirs(self.vardir)
        else:
            if not silent:
                color_stdout('    Found old vardir, deleting ...\n', schema='serv_text')
            self.kill_old_server()
            self.cleanup()
        self.copy_files()
        port = random.randrange(3300, 9999)

        if self.use_unix_sockets:
            self._admin = os.path.join(self.vardir, "socket-admin")
        else:
            self._admin = find_port(port)
            port = self._admin.port + 1

        self._iproto = find_port(port)

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

    def start(self, silent=True, wait=True, wait_load=True, **kwargs):
        if self._start_against_running:
            return
        if self.status == 'started':
            if not silent:
                color_stdout('The server is already started.\n', schema='lerror')
            return

        args = self.prepare_args()
        self.pidfile = '%s.pid' % self.name
        self.logfile = '%s.log' % self.name

        if not silent:
            color_stdout("Starting the server ...\n", schema='serv_text')
            color_stdout("Starting ", schema='serv_text')
            color_stdout((os.path.basename(self.binary) if not self.script else self.script_dst) + " \n", schema='path')
            color_stdout(self.version() + "\n", schema='version')

        check_port(self.admin.port)
        os.putenv("LISTEN", self.iproto.uri)
        os.putenv("ADMIN", self.admin.uri)
        if self.rpl_master:
            os.putenv("MASTER", self.rpl_master.iproto.uri)
        self.logfile_pos = self.logfile

        # redirect strout from tarantoolctl and tarantool
        os.putenv("TEST_WORKDIR", self.vardir)
        self.process = subprocess.Popen(args,
                                        cwd=self.vardir,
                                        stdout=self.log_des,
                                        stderr=self.log_des)

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
        self.admin = CON_SWITCH[self.cls]('localhost', port)
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

        if not os.path.exists(self.logfile):
            return

        if not self.current_test.is_crash_reported:
            self.current_test.is_crash_reported = True
            self.crash_grep()
        self.kill_current_test()

    def crash_grep(self):
        bt = list()
        with open(self.logfile, 'r') as log:
            lines = log.readlines()
            for pos, line in enumerate(reversed(lines)):
                if line.startswith('Segmentation fault'):
                    bt = lines[-pos - 1:]
                    break
            else:
                bt = list()

        color_stdout('\n\n[Instance "%s" crash detected]\n' % self.name,
                     schema='error')
        color_stdout('[ReturnCode=%s]\n' % repr(self.process.returncode),
                     schema='error')
        sys.stderr.flush()
        for trace in bt:
            sys.stderr.write(trace)
        if not bt:
            color_stdout(
                'Silent crash: no "Segmentation fault" found in the logfile [{}]\n'.format(
                    self.logfile), schema='error')
            self.print_log(15)
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
        if self.process.returncode is None:
            self.process.terminate()
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

    def find_tests(self, test_suite, suite_path):
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

    def version(self):
        p = subprocess.Popen([self.binary, "--version"],
                             cwd=self.vardir,
                             stdout=subprocess.PIPE)
        version = p.stdout.read().rstrip()
        p.wait()
        return version

    def get_log(self):
        return TarantoolLog(self.logfile).positioning()
