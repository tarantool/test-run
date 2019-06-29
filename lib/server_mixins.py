import os
import glob
import shlex
from lib.utils import find_in_path
from lib.utils import print_tail_n
from lib.utils import non_empty_valgrind_logs
from lib.colorer import color_stdout, color_log
from six.moves import shlex_quote


def shlex_join(strings):
    return ' '.join(shlex_quote(s) for s in strings)


class Mixin(object):
    pass


class ValgrindMixin(Mixin):
    default_valgr = {
        "suppress_path": "share/",
        "suppress_name": "tarantool.sup"
    }

    def format_valgrind_log_path(self, suite_name, test_name, conf,
                                 server_name, num):
        basename = '{}.{}.{}.{}.{}.valgrind.log'.format(
            suite_name, test_name, conf, server_name, str(num))
        return os.path.join(self.vardir, basename)

    @property
    def valgrind_log(self):
        # suite.{test/default}.{conf/none}.instance.num.valgrind.log
        # Why 'TarantoolServer' is special case here? Consider:
        # * TarantoolServer runs once, then execute tests in the one process
        #   (we run the server itself under valgrind).
        # * AppServer / UnittestServer just create separate processes for each
        #   tests (we run these processes under valgrind).
        if 'TarantoolServer' in self.__class__.__name__ and self.test_suite:
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
            default_tmpl = self.format_valgrind_log_path(
                suite_name, 'default', '*', '*', '*')
            non_default_tmpl = self.format_valgrind_log_path(
                suite_name, test_name, '*', '*', '*')
            return sorted(glob.glob(default_tmpl) +
                          glob.glob(non_default_tmpl))
        else:
            suite_tmpl = self.format_valgrind_log_path(
                suite_name, '*', '*', '*', '*')
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

    @property
    def valgrind_cmd_args(self):
        return shlex.split("valgrind --log-file={log} --suppressions={sup} \
            --gen-suppressions=all --trace-children=yes --leak-check=full \
            --read-var-info=yes --quiet".format(
                log=self.valgrind_log,
                sup=self.valgrind_sup))

    def prepare_args(self, args=[]):
        if not find_in_path('valgrind'):
            raise OSError('`valgrind` executables not found in PATH')
        orig_args = super(ValgrindMixin, self).prepare_args(args)
        res_args = self.valgrind_cmd_args + orig_args
        color_log('\nRUN: ' + shlex_join(res_args) + '\n', schema='test_var')
        return res_args

    def wait_stop(self):
        return self.process.wait()

    def crash_grep(self):
        if self.process.returncode < 0 or \
                not non_empty_valgrind_logs([self.valgrind_log]):
            super(ValgrindMixin, self).crash_grep()
            return

        lines_cnt = 50
        color_stdout(('\n\nValgrind for [Instance "%s"] returns non-zero ' +
                     'exit code: %d\n') % (self.name, self.process.returncode),
                     schema='error')
        color_stdout("It's known that it can be valgrind's " +
                     "\"the 'impossible' happened\" error\n", schema='error')
        color_stdout('Last %d lines of valgring log file [%s]:\n' % (
            lines_cnt, self.valgrind_log), schema='error')
        print_tail_n(self.valgrind_log, lines_cnt)


class StraceMixin(Mixin):
    @property
    def strace_log(self):
        # TODO: don't overwrite log, like in the 'valgrind_log' property above
        return os.path.join(self.vardir, 'strace.log')

    def prepare_args(self, args=[]):
        if not find_in_path('strace'):
            raise OSError('`strace` executables not found in PATH')
        orig_args = super(StraceMixin, self).prepare_args(args)
        res_args = shlex.split("strace -o {log} -f -tt -T -x -I1 {bin}".format(
            bin=' '.join(orig_args),
            log=self.strace_log
        ))
        color_log('\nRUN: ' + shlex_join(res_args) + '\n', schema='test_var')
        return res_args

    def wait_stop(self):
        self.kill_old_server()
        return self.process.wait()


class DebugMixin(Mixin):
    debugger_args = {
        "screen_name": None,
        "debugger": None,
        "sh_string": None
    }

    def prepare_args(self, args=[]):
        screen_name = self.debugger_args['screen_name']
        debugger = self.debugger_args['debugger']
        gdbserver_port = self.debugger_args['gdbserver_port']
        gdbserver_opts = self.debugger_args['gdbserver_opts']
        sh_string = self.debugger_args['sh_string']

        is_under_gdbserver = 'GdbServer' in self.__class__.__name__

        if not is_under_gdbserver and not find_in_path('screen'):
            raise OSError('`screen` executables not found in PATH')
        if not find_in_path(debugger):
            raise OSError('`%s` executables not found in PATH' % debugger)

        is_tarantoolserver = 'TarantoolServer' in self.__class__.__name__

        if is_tarantoolserver or is_under_gdbserver:
            color_stdout('\nYou started the server in %s mode.\n' % debugger,
                         schema='info')
            if is_under_gdbserver:
                color_stdout("To attach, use `gdb -ex 'target remote :%s'`\n" %
                             gdbserver_port, schema='info')
            else:
                color_stdout('To attach, use `screen -r %s`\n' % screen_name,
                             schema='info')

        # detach only for TarantoolServer
        screen_opts = '-d' if is_tarantoolserver else ''

        orig_args = super(DebugMixin, self).prepare_args(args)
        res_args = shlex.split(sh_string.format(
            screen_name=screen_name,
            screen_opts=screen_opts,
            binary=self.binary,
            args=' '.join(orig_args),
            logfile=self.logfile,
            debugger=debugger,
            gdbserver_port=gdbserver_port,
            gdbserver_opts=gdbserver_opts))
        color_log('\nRUN: ' + shlex_join(res_args) + '\n', schema='test_var')
        return res_args

    def wait_stop(self):
        self.kill_old_server()
        self.process.wait()


class GdbMixin(DebugMixin):
    debugger_args = {
        "screen_name": "tarantool",
        "debugger": "gdb",
        "gdbserver_port": None,
        "gdbserver_opts": None,
        "sh_string":
            """screen {screen_opts} -mS {screen_name} {debugger} {binary}
               -ex 'b main' -ex 'run {args} >> {logfile} 2>> {logfile}'
            """
    }


# this would be good for running unit tests:
# https://cygwin.com/ml/gdb-patches/2015-03/msg01051.html
class GdbServerMixin(DebugMixin):
    debugger_args = {
        "screen_name": None,
        "debugger": "gdbserver",
        "gdbserver_port": "8888",
        "gdbserver_opts": "",
        "sh_string":
            """gdbserver :{gdbserver_port} {binary} {args} -- {gdbserver_opts}
            """
    }


class LLdbMixin(DebugMixin):
    debugger_args = {
        "screen_name": "tarantool",
        "debugger": "lldb",
        "gdbserver_port": None,
        "gdbserver_opts": None,
        "sh_string":
            """screen {screen_opts} -mS {screen_name} {debugger} -f {binary}
               -o 'b main'
               -o 'settings set target.run-args {args}'
               -o 'process launch -o {logfile} -e {logfile}'
            """
    }


class LuacovMixin(Mixin):
    def prepare_args(self, args=[]):
        orig_args = super(LuacovMixin, self).prepare_args(args)
        return ['tarantool',
                '-e', '_G.TEST_RUN_LUACOV=true',
                '-e', 'jit.off()',
                '-l', 'luacov'] + orig_args
