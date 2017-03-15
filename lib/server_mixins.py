import os
import glob
import shlex
from lib.utils import find_in_path
from lib.colorer import Colorer

color_stdout = Colorer()


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

    @property
    def valgrind_cmd_args(self):
        return shlex.split("valgrind --log-file={log} --suppressions={sup} \
            --gen-suppressions=all --trace-children=yes --leak-check=full \
            --read-var-info=yes --quiet".format(
                log=self.valgrind_log,
                sup=self.valgrind_sup))

    def prepare_args(self):
        if not find_in_path('valgrind'):
            raise OSError('`valgrind` executables not found in PATH')
        return self.valgrind_cmd_args + super(ValgrindMixin, self).prepare_args()

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
