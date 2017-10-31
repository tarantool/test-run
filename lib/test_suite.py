import ConfigParser
import json
import os

import lib
from lib.colorer import color_stdout
from lib.inspector import TarantoolInspector
from lib.server import Server
from lib.tarantool_server import TarantoolServer
from lib.app_server import AppServer
from lib.unittest_server import UnittestServer
from lib.utils import non_empty_valgrind_logs, print_tail_n

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

class ConfigurationError(RuntimeError):
    def __init__(self, name, value, expected):
        self.name = name
        self.value = value
        self.expected = expected

    def __str__(self):
        return "Bad value for %s: expected %s, got %s" % (
            repr(self.name), self.expected, repr(self.value)
        )


class TestSuite:
    """Each test suite contains a number of related tests files,
    located in the same directory on disk. Each test file has
    extention .test and contains a listing of server commands,
    followed by their output. The commands are executed, and
    obtained results are compared with pre-recorded output. In case
    of a comparision difference, an exception is raised. A test suite
    must also contain suite.ini, which describes how to start the
    server for this suite, the client program to execute individual
    tests and other suite properties. The server is started once per
    suite."""
    def get_multirun_conf(self, suite_path):
        conf_name = self.ini.get('config', None)
        if conf_name is None:
            return None

        path = os.path.join(suite_path, conf_name)
        result = None
        with open(path) as cfg:
            try:
                result = json.load(cfg)
            except ValueError:
                raise RuntimeError('Ivalid multirun json')
        return result

    def get_multirun_params(self, test_path):
        test = test_path.split('/')[-1]
        if self.multi_run is None:
            return
        result = self.multi_run.get(test, None)
        if result is not None:
            return result
        result = self.multi_run.get('*', None)
        return result


    def __init__(self, suite_path, args):
        """Initialize a test suite: check that it exists and contains
        a syntactically correct configuration file. Then create
        a test instance for each found test."""
        self.args = args
        self.tests = []
        self.ini = {}
        self.suite_path = suite_path
        self.ini["core"] = "tarantool"

        if os.access(suite_path, os.F_OK) == False:
            raise RuntimeError("Suite %s doesn't exist" % repr(suite_path))

        # read the suite config
        config = ConfigParser.ConfigParser()
        config.read(os.path.join(suite_path, "suite.ini"))
        self.ini.update(dict(config.items("default")))
        self.ini.update(self.args.__dict__)
        self.multi_run = self.get_multirun_conf(suite_path)

        # list of long running tests
        if 'long_run' not in self.ini:
            self.ini['long_run'] = []

        for i in ["script"]:
            self.ini[i] = os.path.join(suite_path, self.ini[i]) if i in self.ini else None
        for i in ["disabled", "valgrind_disabled", "release_disabled"]:
            self.ini[i] = dict.fromkeys(self.ini[i].split()) if i in self.ini else dict()
        for i in ["lua_libs"]:
            self.ini[i] = map(lambda x: os.path.join(suite_path, x),
                    dict.fromkeys(self.ini[i].split()) if i in self.ini else dict())
        for i in ["requirements"]:
            self.ini[i] = map(lambda x: os.path.join(suite_path, x),
                    dict.fromkeys(self.ini[i].split()) if i in self.ini else dict())

    def find_tests(self):
        if self.ini['core'] == 'tarantool':
            TarantoolServer.find_tests(self, self.suite_path)
        elif self.ini['core'] == 'app':
            AppServer.find_tests(self, self.suite_path)
        elif self.ini['core'] == 'unittest':
            UnittestServer.find_tests(self, self.suite_path)
        elif self.ini['core'] == 'stress':
            # parallel tests are not supported and disabled for now
            return []
        else:
            raise ValueError('Cannot collect tests of unknown type')

        if not lib.Options().args.reproduce:
            color_stdout("Collecting tests in ", schema='ts_text')
            color_stdout(
                '%s (Found %s tests)' % (
                    repr(self.suite_path).ljust(16),
                    str(len(self.tests)).ljust(3)
                ),
                schema='path'
            )
            color_stdout(": ", self.ini["description"], ".\n",
                         schema='ts_text')
        return self.tests

    def gen_server(self):
        try:
            return Server(self.ini, test_suite=self)
        except Exception as e:
            print e
            raise RuntimeError("Unknown server: core = {0}".format(
                               self.ini["core"]))

    def is_test_enabled(self, test, conf, server):
        test_name = os.path.basename(test.name)
        tconf = '%s:%s' % (test_name, conf)
        checks = [
            (True, self.ini["disabled"]),
            (not server.debug, self.ini["release_disabled"]),
            (self.args.valgrind, self.ini["valgrind_disabled"]),
            (not self.args.long, self.ini["long_run"])
        ]
        for check in checks:
            check_enabled, disabled_tests = check
            if check_enabled and (test_name in disabled_tests
                    or tconf in disabled_tests):
                return False
        return True

    def start_server(self, server):
        # create inspector daemon for cluster tests
        inspector = TarantoolInspector(
            'localhost', server.inspector_port
        )
        inspector.start()
        # fixme: remove this string if we fix all legacy tests
        suite_name = os.path.basename(self.suite_path)
        server.tests_type = 'python' if suite_name.endswith('-py') else 'lua'
        server.deploy(silent=False)
        return inspector

    def stop_server(self, server, inspector, silent=False, cleanup=True):
        server.stop(silent=silent)
        # don't delete core files or state of the data dir
        # in case of exception, which is raised when the
        # server crashes
        if inspector:
            inspector.stop()
        if cleanup:
            inspector.cleanup_nondefault()
            server.cleanup()

    def run_test(self, test, server, inspector):
        """ Returns short status of the test as a string: 'skip', 'pass',
            'new', 'fail', or 'disabled'.
        """
        test.inspector = inspector
        color_stdout(
            os.path.join(
                self.ini['suite'],
                os.path.basename(test.name)
            ).ljust(48),
            schema='t_name'
        )
        # for better diagnostics in case of a long-running test

        conf = ''
        if test.run_params:
            conf = test.conf_name
        color_stdout(conf.ljust(16), schema='test_var')
        test_name = os.path.basename(test.name)

        if self.is_test_enabled(test, conf, server):
            short_status = test.run(server)
        else:
            color_stdout("[ disabled ]\n", schema='t_name')
            short_status = 'disabled'

        # cleanup only if test passed or if --force mode enabled
        if lib.Options().args.is_force or short_status == 'pass':
            inspector.cleanup_nondefault()

        return short_status

    def is_parallel(self):
        val = self.ini.get('is_parallel', 'False').lower()
        if val == 'true':
            val = True
        elif val == 'false':
            val = False
        else:
            raise ConfigurationError()
            pass
        return val
