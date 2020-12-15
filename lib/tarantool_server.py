import errno
import gevent
import glob
import inspect  # for caller_globals
import os
import os.path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import yaml

from gevent import socket
from gevent import Timeout
from greenlet import GreenletExit
from threading import Timer

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

from lib.admin_connection import AdminConnection, AdminAsyncConnection
from lib.box_connection import BoxConnection
from lib.colorer import color_stdout
from lib.colorer import color_log
from lib.colorer import qa_notice
from lib.options import Options
from lib.preprocessor import TestState
from lib.server import Server
from lib.server import DEFAULT_SNAPSHOT_NAME
from lib.test import Test
from lib.utils import find_port
from lib.utils import extract_schema_from_snapshot
from lib.utils import format_process
from lib.utils import safe_makedirs
from lib.utils import signame
from lib.utils import warn_unix_socket
from lib.utils import prefix_each_line
from test import TestRunGreenlet, TestExecutionError


def save_join(green_obj, timeout=None):
    """
    Gevent join wrapper for
    test-run stop-on-crash/stop-on-timeout feature
    """
    try:
        green_obj.get(timeout=timeout)
    except Timeout:
        color_stdout("Test timeout of %d secs reached\t" % timeout, schema='error')
        # We should kill the greenlet that writes to a temporary
        # result file. If the same test is run several times (e.g.
        # on different configurations), this greenlet may wake up
        # and write to the temporary result file of the new run of
        # the test.
        green_obj.kill()
    except GreenletExit:
        pass
    # We don't catch TarantoolStartError here to propagate it to a parent
    # greenlet to report a (default or non-default) tarantool server fail.


class LuaTest(Test):
    """ Handle *.test.lua and *.test.sql test files. """

    RESULT_FILE_VERSION_INITIAL = 1
    RESULT_FILE_VERSION_DEFAULT = 2
    RESULT_FILE_VERSION_LINE_RE = re.compile(
        r'^-- test-run result file version (?P<version>\d+)$')
    RESULT_FILE_VERSION_TEMPLATE = '-- test-run result file version {}'

    def __init__(self, *args, **kwargs):
        super(LuaTest, self).__init__(*args, **kwargs)
        if self.name.endswith('.test.lua'):
            self.default_language = 'lua'
        else:
            assert self.name.endswith('.test.sql')
            self.default_language = 'sql'
        self.result_file_version = self.result_file_version()

    def result_file_version(self):
        """ If a result file is not exists, return a default
            version (last known by test-run).
            If it exists, but does not contain a valid result file
            header, return 1.
            If it contains a version, return the version.
        """
        if not os.path.isfile(self.result):
            return self.RESULT_FILE_VERSION_DEFAULT

        with open(self.result, 'r') as f:
            line = f.readline().rstrip('\n')

            # An empty line or EOF.
            if not line:
                return self.RESULT_FILE_VERSION_INITIAL

            # No result file header.
            m = self.RESULT_FILE_VERSION_LINE_RE.match(line)
            if not m:
                return self.RESULT_FILE_VERSION_INITIAL

            # A version should be integer.
            try:
                return int(m.group('version'))
            except ValueError:
                return self.RESULT_FILE_VERSION_INITIAL

    def write_result_file_version_line(self):
        # The initial version of a result file does not have a
        # version line.
        if self.result_file_version < 2:
            return
        sys.stdout.write(self.RESULT_FILE_VERSION_TEMPLATE.format(
                         self.result_file_version) + '\n')

    def execute_pretest_clean(self, ts):
        """ Clean globals, loaded packages, spaces, users, roles
            and so on before each test if the option is set.

            Return True as success (or if this feature is disabled
            in suite.ini) and False in case of an error.
        """
        if not self.suite_ini['pretest_clean']:
            return True

        command = "require('pretest_clean').clean()"
        result = self.send_command(command, ts, 'lua')
        result = result.replace('\r\n', '\n')
        if result != '---\n...\n':
            sys.stdout.write(result)
            return False

        return True

    def execute_pragma_sql_default_engine(self, ts):
        """ Set default engine for an SQL test if it is provided
            in a configuration.

            Return True if the command is successful or when it is
            not performed, otherwise (when got an unexpected
            result for the command) return False.
        """
        # Pass the command only for *.test.sql test files, because
        # hence we sure tarantool supports SQL.
        if self.default_language != 'sql':
            return True

        # Skip if no 'memtx' or 'vinyl' engine is provided.
        ok = self.run_params and 'engine' in self.run_params and \
            self.run_params['engine'] in ('memtx', 'vinyl')
        if not ok:
            return True

        engine = self.run_params['engine']

        # Probe the new way. Pass through on any error.
        command_new = ("UPDATE \"_session_settings\" SET \"value\" = '{}' " +
                       "WHERE \"name\" = 'sql_default_engine'").format(engine)
        result_new = self.send_command(command_new, ts, 'sql')
        result_new = result_new.replace('\r\n', '\n')
        if result_new == '---\n- row_count: 1\n...\n':
            return True

        # Probe the old way. Fail the test on an error.
        command_old = "pragma sql_default_engine='{}'".format(engine)
        result_old = self.send_command(command_old, ts, 'sql')
        result_old = result_old.replace('\r\n', '\n')
        if result_old == '---\n- row_count: 0\n...\n':
            return True

        sys.stdout.write(command_new)
        sys.stdout.write(result_new)
        sys.stdout.write(command_old)
        sys.stdout.write(result_old)
        return False

    def send_command_raw(self, command, ts):
        """ Send a command to tarantool and read a response. """
        color_log('DEBUG: sending command: {}\n'.format(command.rstrip()),
                  schema='tarantool command')
        # Evaluate the request on the first connection, save the
        # response.
        result = ts.curcon[0](command, silent=True)
        # Evaluate on other connections, ignore responses.
        for conn in ts.curcon[1:]:
            conn(command, silent=True)
        # gh-24 fix
        if result is None:
            result = '[Lost current connection]\n'
        color_log("DEBUG: tarantool's response for [{}]\n{}\n".format(
            command.rstrip(), prefix_each_line(' | ', result)),
            schema='tarantool command')
        return result

    def set_language(self, ts, language):
        command = r'\set language ' + language
        self.send_command_raw(command, ts)

    def send_command(self, command, ts, language=None):
        if language:
            self.set_language(ts, language)
        return self.send_command_raw(command, ts)

    def flush(self, ts, command_log, command_exe):
        # Write a command to a result file.
        command = command_log.getvalue()
        sys.stdout.write(command)

        # Drop a previous command.
        command_log.seek(0)
        command_log.truncate()

        if not command_exe:
            return

        # Send a command to tarantool console.
        result = self.send_command(command_exe.getvalue(), ts)

        # Convert and prettify a command result.
        result = result.replace('\r\n', '\n')
        if self.result_file_version >= 2:
            result = prefix_each_line(' | ', result)

        # Write a result of the command to a result file.
        sys.stdout.write(result)

        # Drop a previous command.
        command_exe.seek(0)
        command_exe.truncate()

    def exec_loop(self, ts):
        self.write_result_file_version_line()
        if not self.execute_pretest_clean(ts):
            return
        if not self.execute_pragma_sql_default_engine(ts):
            return

        # Set default language for the test.
        self.set_language(ts, self.default_language)

        # Use two buffers: one to commands that are logged in a
        # result file and another that contains commands that
        # actually executed on a tarantool console.
        command_log = StringIO()
        command_exe = StringIO()

        # A newline from a source that is not end of a command is
        # replaced with the following symbols.
        newline_log = '\n'
        newline_exe = ' '

        # A backslash from a source is replaced with the following
        # symbols.
        backslash_log = '\\'
        backslash_exe = ''

        # A newline that marks end of a command is replaced with
        # the following symbols.
        eoc_log = '\n'
        eoc_exe = '\n'

        for line in open(self.name, 'r'):
            # Normalize a line.
            line = line.rstrip('\n')

            # Show empty lines / comments in a result file, but
            # don't send them to tarantool.
            line_is_empty = line.strip() == ''
            if line_is_empty or line.find('--') == 0:
                if self.result_file_version >= 2:
                    command_log.write(line + eoc_log)
                    self.flush(ts, command_log, None)
                elif line_is_empty:
                    # Compatibility mode: don't add empty lines to
                    # a result file in except when a delimiter is
                    # set.
                    if command_log.getvalue():
                        command_log.write(eoc_log)
                else:
                    # Compatibility mode: write a comment and only
                    # then a command before it when a delimiter is
                    # set.
                    sys.stdout.write(line + eoc_log)
                self.inspector.sem.wait()
                continue

            # A delimiter is set and found at end of the line:
            # send the command.
            if ts.delimiter and line.endswith(ts.delimiter):
                delimiter_len = len(ts.delimiter)
                command_log.write(line + eoc_log)
                command_exe.write(line[:-delimiter_len] + eoc_exe)
                self.flush(ts, command_log, command_exe)
                self.inspector.sem.wait()
                continue

            # A backslash found at end of the line: continue
            # collecting input. Send / log a backslash as is when
            # it is inside a block with set delimiter.
            if line.endswith('\\') and not ts.delimiter:
                command_log.write(line[:-1] + backslash_log + newline_log)
                command_exe.write(line[:-1] + backslash_exe + newline_exe)
                self.inspector.sem.wait()
                continue

            # A delimiter is set, but not found at the end of the
            # line: continue collecting input.
            if ts.delimiter:
                command_log.write(line + newline_log)
                command_exe.write(line + newline_exe)
                self.inspector.sem.wait()
                continue

            # A delimiter is not set, backslash is not found at
            # end of the line: send the command.
            command_log.write(line + eoc_log)
            command_exe.write(line + eoc_exe)
            self.flush(ts, command_log, command_exe)
            self.inspector.sem.wait()

        # Free StringIO() buffers.
        command_log.close()
        command_exe.close()

    def execute(self, server):
        super(LuaTest, self).execute(server)
        cls_name = server.__class__.__name__.lower()
        if 'gdb' in cls_name or 'lldb' in cls_name or 'strace' in cls_name:
            # don't propagate gdb/lldb/strace mixin to non-default servers,
            # it doesn't work properly for now
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
        try:
            save_join(lua, timeout=Options().args.test_timeout)
        except KeyboardInterrupt:
            # prevent tests greenlet from writing to the real stdout
            lua.kill()
            raise
        except TarantoolStartError as e:
            color_stdout('\n[Instance "{0}"] Failed to start tarantool '
                         'instance "{1}"\n'.format(server.name, e.name),
                         schema='error')
            server.kill_current_test()
        finally:
            # Stop any servers created by the test, except the
            # default one.
            #
            # The stop_nondefault() method calls
            # TarantoolServer.stop() under the hood. It sends
            # SIGTERM (if another signal is not passed), waits
            # for 5 seconds for a process termination and, if
            # nothing occurs, sends SIGKILL and continue waiting
            # for the termination.
            #
            # Look, 5 seconds (plus some delay for waiting) for
            # each instance if it does not follow SIGTERM[^1].
            # It is unacceptable, because the difference between
            # --test-timeout (110 seconds by default) and
            # --no-output-timeout (120 seconds by default) may
            # be lower than (5 seconds + delay) * (non-default
            # instance count).
            #
            # That's why we send SIGKILL for residual instances
            # right away.
            #
            # Hitting --no-output-timeout is undesirable, because
            # in the current implementation it is the show-stopper
            # for a testing: test-run doesn't restart fragile
            # tests, doesn't continue processing of other tests,
            # doesn't save artifacts at the end of the testing.
            #
            # [^1]: See gh-4127 and gh-5573 for problems of this
            #       kind.
            ts.stop_nondefault(signal=signal.SIGKILL)


class PythonTest(Test):
    """ Handle *.test.py test files. """

    def execute(self, server):
        super(PythonTest, self).execute(server)
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
    def __init__(self, name=None):
        self.name = name


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

    def seek_wait(self, msg, proc=None, name=None):
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
                        raise TarantoolStartError(name)
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

    # ----------------------------PROPERTIES--------------------------------- #
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
        if not hasattr(self, '_logfile_pos'):
            self._logfile_pos = None
        return self._logfile_pos

    @logfile_pos.setter
    def logfile_pos(self, val):
        self._logfile_pos = TarantoolLog(val).positioning()

    @property
    def script(self):
        if not hasattr(self, '_script'):
            self._script = None
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
        if not hasattr(self, 'admin'):
            self.admin = None
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
        if not hasattr(self, 'iproto'):
            self.iproto = None
        return self.iproto

    @_iproto.setter
    def _iproto(self, port):
        if hasattr(self, 'iproto'):
            del self.iproto
        self.iproto = BoxConnection('localhost', port)

    @property
    def log_des(self):
        if not hasattr(self, '_log_des'):
            self._log_des = open(self.logfile, 'a')
        return self._log_des

    @log_des.deleter
    def log_des(self):
        if not hasattr(self, '_log_des'):
            return
        if not self._log_des.closed:
            self._log_des.close()
        delattr(self, '_log_des')

    @property
    def rpl_master(self):
        if not hasattr(self, '_rpl_master'):
            self._rpl_master = None
        return self._rpl_master

    @rpl_master.setter
    def rpl_master(self, val):
        if not isinstance(self, (TarantoolServer, None)):
            raise ValueError('Replication master must be Tarantool'
                             ' Server class, his derivation or None')
        self._rpl_master = val

    # ----------------------------------------------------------------------- #

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
            'use_unix_sockets_iproto': False,
            'tarantool_port': None,
            'strace': False
        }
        ini.update(_ini)
        Server.__init__(self, ini, test_suite)
        self.testdir = os.path.abspath(os.curdir)
        self.sourcedir = os.path.abspath(os.path.join(os.path.basename(
            sys.argv[0]), "..", ".."))
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
        self.strace = ini['strace']
        self.use_unix_sockets = ini['use_unix_sockets']
        self.use_unix_sockets_iproto = ini['use_unix_sockets_iproto']
        self._start_against_running = ini['tarantool_port']
        self.crash_detector = None
        # use this option with inspector to enable crashes in test
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
                cls.binary = os.path.abspath(exe)
                cls.ctl_path = os.path.abspath(ctl)
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
                    os.environ["LUA_PATH"] = \
                        ctl_dir + '/?.lua;' + \
                        ctl_dir + '/?/init.lua;' + \
                        os.environ.get("LUA_PATH", ";;")
                cls.debug = bool(re.findall(r'-Debug', str(cls.version()),
                                 re.I))
                return exe
        raise RuntimeError("Can't find server executable in " + path)

    @classmethod
    def print_exe(cls):
        color_stdout('Tarantool server information\n', schema='info')
        if cls.binary:
            color_stdout(' | Found executable at {}\n'.format(cls.binary))
        if cls.ctl_path:
            color_stdout(' | Found tarantoolctl at {}\n'.format(cls.ctl_path))
        color_stdout('\n' + prefix_each_line(' | ', cls.version()) + '\n',
                     schema='version')

    def install(self, silent=True):
        if self._start_against_running:
            self._iproto = self._start_against_running
            self._admin = int(self._start_against_running) + 1
            return
        color_log('DEBUG: [Instance {}] Installing the server...\n'.format(
            self.name), schema='info')
        color_log(' | Found executable at {}\n'.format(self.binary))
        color_log(' | Found tarantoolctl at {}\n'.format(self.ctl_path))
        color_log(' | Creating and populating working directory in '
                  '{}...\n'.format(self.vardir))
        if not os.path.exists(self.vardir):
            os.makedirs(self.vardir)
        else:
            color_log(' | Found old workdir, deleting...\n')
            self.kill_old_server()
            self.cleanup()
        self.copy_files()

        if self.use_unix_sockets:
            path = os.path.join(self.vardir, self.name + ".socket-admin")
            warn_unix_socket(path)
            self._admin = path
        else:
            self._admin = find_port()

        if self.use_unix_sockets_iproto:
            path = os.path.join(self.vardir, self.name + ".socket-iproto")
            warn_unix_socket(path)
            self._iproto = path
        else:
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
        # Previously tarantoolctl configuration file located in tarantool
        # repository at test/ directory. Currently it is located in root
        # path of test-run/ submodule repository. For backward compatibility
        # this file should be checked at the old place and only after at
        # the current.
        tntctl_file = '.tarantoolctl'
        if not os.path.exists(tntctl_file):
            tntctl_file = os.path.join(self.TEST_RUN_DIR, '.tarantoolctl')
        shutil.copy(tntctl_file, self.vardir)
        shutil.copy(os.path.join(self.TEST_RUN_DIR, 'test_run.lua'),
                    self.vardir)
        # Need to use get here because of nondefault servers doesn't have ini.
        if self.ini.get('pretest_clean', False):
            shutil.copy(os.path.join(self.TEST_RUN_DIR, 'pretest_clean.lua'),
                        self.vardir)

        if self.snapshot_path:
            # Copy snapshot to the workdir.
            # Usually Tarantool looking for snapshots on start in a current directory
            # or in a directories that specified in memtx_dir or vinyl_dir box settings.
            # Before running test current directory (workdir) passed to a new instance in
            # an environment variable TEST_WORKDIR and then tarantoolctl
            # adds to it instance_name and set to memtx_dir and vinyl_dir.
            (instance_name, _) = os.path.splitext(os.path.basename(self.script))
            instance_dir = os.path.join(self.vardir, instance_name)
            safe_makedirs(instance_dir)
            snapshot_dest = os.path.join(instance_dir, DEFAULT_SNAPSHOT_NAME)
            color_log("Copying snapshot {} to {}\n".format(
                self.snapshot_path, snapshot_dest))
            shutil.copy(self.snapshot_path, snapshot_dest)

    def prepare_args(self, args=[]):
        cli_args = [self.ctl_path, 'start',
                    os.path.basename(self.script)] + args
        if self.disable_schema_upgrade:
            cli_args = [self.binary, '-e',
                        self.DISABLE_AUTO_UPGRADE] + cli_args

        return cli_args

    def pretest_clean(self):
        # Don't delete snap and logs for 'default' tarantool server
        # because it works during worker lifetime.
        pass

    def cleanup(self, *args, **kwargs):
        # For `core = tarantool` tests default worker runs on
        # subdirectory created by tarantoolctl by using script name
        # from suite.ini file.
        super(TarantoolServer, self).cleanup(dirname=self.name,
                                             *args, **kwargs)

    def start(self, silent=True, wait=True, wait_load=True, rais=True, args=[],
              **kwargs):
        if self._start_against_running:
            return
        if self.status == 'started':
            if not silent:
                color_stdout('The server is already started.\n',
                             schema='lerror')
            return

        args = self.prepare_args(args)
        self.pidfile = '%s.pid' % self.name
        self.logfile = '%s.log' % self.name

        path = self.script_dst if self.script else \
            os.path.basename(self.binary)
        color_log('DEBUG: [Instance {}] Starting the server...\n'.format(
            self.name), schema='info')
        color_log(' | ' + path + '\n', schema='path')
        color_log(prefix_each_line(' | ', self.version()) + '\n',
                  schema='version')

        os.putenv("LISTEN", self.iproto.uri)
        os.putenv("ADMIN", self.admin.uri)
        if self.rpl_master:
            os.putenv("MASTER", self.rpl_master.iproto.uri)
        self.logfile_pos = self.logfile

        # This is strange, but tarantooctl leans on the PWD
        # environment variable, not a real current working
        # directory, when it performs search for the
        # .tarantoolctl configuration file.
        os.environ['PWD'] = self.vardir

        # redirect stdout from tarantoolctl and tarantool
        os.putenv("TEST_WORKDIR", self.vardir)
        self.process = subprocess.Popen(args,
                                        cwd=self.vardir,
                                        stdout=self.log_des,
                                        stderr=self.log_des)
        del(self.log_des)

        # Restore the actual PWD value.
        os.environ['PWD'] = os.getcwd()

        # gh-19 crash detection
        self.crash_detector = TestRunGreenlet(self.crash_detect)
        self.crash_detector.info = "Crash detector: %s" % self.process
        self.crash_detector.start()

        if wait:
            try:
                self.wait_until_started(wait_load)
            except TarantoolStartError:
                # Python tests expect we raise an exception when non-default
                # server fails
                if self.crash_expected:
                    raise
                if not (self.current_test and
                        self.current_test.is_crash_reported):
                    if self.current_test:
                        self.current_test.is_crash_reported = True
                    color_stdout('\n[Instance "{0.name}"] Tarantool server '
                                 'failed to start\n'.format(self),
                                 schema='error')
                    self.print_log(15)
                # Raise exception when caller ask for it (e.g. in case of
                # non-default servers)
                if rais:
                    raise
                # if the server fails before any test started, we should inform
                # a caller by the exception
                if not self.current_test:
                    raise
                self.kill_current_test()

        port = self.admin.port
        self.admin.disconnect()
        self.admin = CON_SWITCH[self.tests_type]('localhost', port)
        self.status = 'started'

        # Verify that the schema actually was not upgraded.
        if self.disable_schema_upgrade:
            expected_version = extract_schema_from_snapshot(self.snapshot_path)
            actual_version = yaml.safe_load(self.admin.execute(
                'box.space._schema:get{"version"}'))[0]
            if expected_version != actual_version:
                color_stdout('Schema version check fails: expected '
                             '{}, got {}\n'.format(expected_version,
                                                   actual_version),
                             schema='error')
                raise TarantoolStartError(self.name)

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
            color_stdout('\n\n[Instance "%s" returns with non-zero exit code: '
                         '%d]\n' % (self.name, self.process.returncode),
                         schema='error')

        # print assert line if any and return
        if assert_lines:
            color_stdout('Found assertion fail in the results file '
                         '[%s]:\n' % self.logfile,
                         schema='error')
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

    def stop(self, silent=True, signal=signal.SIGTERM):
        """ Kill tarantool server using specified signal (SIGTERM by default)

            signal - a number of a signal
        """
        if self._start_against_running:
            color_log('Server [%s] start against running ...\n',
                      schema='test_var')
            return
        if self.status != 'started':
            if not silent:
                raise Exception('Server is not started')
            else:
                color_log(
                    'Server [{0.name}] is not started '
                    '(status:{0.status}) ...\n'.format(self),
                    schema='test_var'
                )
            return
        if not silent:
            color_stdout('[Instance {}] Stopping the server...\n'.format(
                self.name), schema='info')
        else:
            color_log('DEBUG: [Instance {}] Stopping the server...\n'.format(
                self.name), schema='info')
        # kill only if process is alive
        if self.process is not None and self.process.returncode is None:
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
                qa_notice('The server \'{}\' does not stop during {} '
                          'seconds after the {} ({}) signal.\n'
                          'Info: {}\n'
                          'Sending SIGKILL...'.format(
                              self.name, timeout, signal, signame(signal),
                              format_process(self.process.pid)))
                try:
                    self.process.kill()
                except OSError:
                    pass

            timer = Timer(timeout, kill)
            timer.start()
            if self.crash_detector is not None:
                save_join(self.crash_detector)
            self.wait_stop()
            timer.cancel()

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
            color_stdout(
                '    Found old server, pid {0}, killing ...'.format(pid),
                schema='info'
            )
        else:
            color_log('    Found old server, pid {0}, killing ...'.format(pid),
                      schema='info')
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
        color_log('DEBUG: [Instance {}] Waiting until started '
                  '(wait_load={})\n'.format(self.name, str(wait_load)),
                  schema='info')

        if wait_load:
            msg = 'entering the event loop|will retry binding|hot standby mode'
            p = self.process if not self.gdb and not self.lldb else None
            self.logfile_pos.seek_wait(msg, p, self.name)
        while True:
            try:
                temp = AdminConnection('localhost', self.admin.port)
                if not wait_load:
                    ans = yaml.safe_load(temp.execute("2 + 2"))
                    color_log(" | Successful connection check; don't wait for "
                              "loading")
                    return True
                ans = yaml.safe_load(temp.execute('box.info.status'))[0]
                if ans in ('running', 'hot_standby', 'orphan'):
                    color_log(" | Started {} (box.info.status: '{}')\n".format(
                        format_process(self.process.pid), ans))
                    return True
                elif ans in ('loading'):
                    continue
                else:
                    raise Exception(
                        "Strange output for `box.info.status`: %s" % (ans)
                    )
            except socket.error as e:
                if e.errno == errno.ECONNREFUSED:
                    color_log(' | Connection refused; will retry every 0.1 '
                              'seconds...')
                    time.sleep(0.1)
                    continue
                raise

    def wait_until_stopped(self, pid):
        while True:
            try:
                time.sleep(0.01)
                os.kill(pid, 0)
                continue
            except OSError:
                break

    def read_pidfile(self):
        pid = -1
        if os.path.exists(self.pidfile):
            try:
                with open(self.pidfile) as f:
                    pid = int(f.read())
            except Exception:
                pass
        return pid

    def test_option_get(self, option_list_str, silent=False):
        args = [self.binary] + shlex.split(option_list_str)
        if not silent:
            print " ".join([os.path.basename(self.binary)] + args[1:])
        output = subprocess.Popen(args,
                                  cwd=self.vardir,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT).stdout.read()
        return output

    def test_option(self, option_list_str):
        print self.test_option_get(option_list_str)

    @staticmethod
    def find_tests(test_suite, suite_path):
        test_suite.ini['suite'] = suite_path

        def get_tests(*patterns):
            res = []
            for pattern in patterns:
                path_pattern = os.path.join(suite_path, pattern)
                res.extend(sorted(glob.glob(path_pattern)))
            return Server.exclude_tests(res, test_suite.args.exclude)

        # Add Python tests.
        tests = [PythonTest(k, test_suite.args, test_suite.ini)
                 for k in get_tests("*.test.py")]

        # Add Lua and SQL tests. One test can appear several times
        # with different configuration names (as configured in a
        # file set by 'config' suite.ini option, usually *.cfg).
        for k in get_tests("*.test.lua", "*.test.sql"):
            runs = test_suite.get_multirun_params(k)

            def is_correct(run_name):
                return test_suite.args.conf is None or \
                    test_suite.args.conf == run_name

            if runs:
                tests.extend([LuaTest(
                    k,
                    test_suite.args,
                    test_suite.ini,
                    runs[r],
                    r
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
        if param is not None:
            return yaml.safe_load(self.admin("box.info." + param,
                                  silent=True))[0]
        return yaml.safe_load(self.admin("box.info", silent=True))

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
