import ConfigParser
import json
import os

from lib.colorer import Colorer
from lib.inspector import TarantoolInspector
from lib.server import Server
from lib.tarantool_server import TarantoolServer
from lib.utils import non_empty_valgrind_logs, print_tail_n

color_stdout = Colorer()
try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO


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

        if self.args.stress is None and self.ini['core'] == 'stress':
            return

        for i in ["script"]:
            self.ini[i] = os.path.join(suite_path, self.ini[i]) if i in self.ini else None
        for i in ["disabled", "valgrind_disabled", "release_disabled"]:
            self.ini[i] = dict.fromkeys(self.ini[i].split()) if i in self.ini else dict()
        for i in ["lua_libs"]:
            self.ini[i] = map(lambda x: os.path.join(suite_path, x),
                    dict.fromkeys(self.ini[i].split()) if i in self.ini else dict())
        try:
            if self.ini['core'] in ['tarantool', 'stress']:
                self.server = TarantoolServer(self.ini, test_suite=self)
            else:
                self.server = Server(self.ini)
            self.ini["server"] = self.server
        except Exception as e:
            print e
            raise RuntimeError("Unknown server: core = {0}".format(
                               self.ini["core"]))
        color_stdout("Collecting tests in ", schema='ts_text')
        color_stdout(repr(suite_path), schema='path')
        color_stdout(": ", self.ini["description"], ".\n", schema='ts_text')
        self.server.find_tests(self, suite_path)
        color_stdout("Found ", str(len(self.tests)), " tests.\n", schema='path')

    def is_test_enabled(self, test, conf):
        test_name = os.path.basename(test.name)
        tconf = '%s:%s' % (test_name, conf)
        checks = [
            (True, self.ini["disabled"]),
            (not self.server.debug, self.ini["release_disabled"]),
            (self.args.valgrind, self.ini["valgrind_disabled"]),
            (not self.args.long, self.ini["long_run"])]
        for check in checks:
            check_enabled, disabled_tests = check
            if check_enabled and (test_name in disabled_tests
                    or tconf in disabled_tests):
                return False
        return True

    def run_all(self):
        """For each file in the test suite, run client program
        assuming each file represents an individual test."""
        if not self.tests:
            # noting to test, exit
            return []
        # fixme: remove this string if we fix all legacy tests
        self.server.cls = self.tests[0].__class__
        # create inspectpor daemon for cluster tests
        inspector = TarantoolInspector(
            'localhost', self.server.inspector_port
        )
        inspector.start()
        self.server.deploy(silent=False)

        longsep = '='*80
        shortsep = '-'*75
        color_stdout(longsep, "\n", schema='separator')
        color_stdout("TEST".ljust(48), schema='t_name')
        color_stdout("PARAMS\t\t", schema='test_var')
        color_stdout("RESULT\n", schema='test_pass')
        color_stdout(shortsep, "\n", schema='separator')
        failed_tests = []
        try:
            for test in self.tests:
                test.inspector = inspector
                color_stdout(os.path.join(
                    self.ini['suite'], os.path.basename(test.name)).ljust(48),
                    schema='t_name'
                )
                # for better diagnostics in case of a long-running test

                conf = ''
                if test.run_params:
                    conf = test.conf_name
                color_stdout("%s" % conf.ljust(16), schema='test_var')
                if self.is_test_enabled(test, conf):
                    test.run(self.server)
                    if not test.passed():
                        failed_tests.append(test.name)
                else:
                    color_stdout("[ disabled ]\n", schema='t_name')
            color_stdout(shortsep, "\n", schema='separator')
            self.server.stop(silent=False)
            # don't delete core files or state of the data dir
            # in case of exception, which is raised when the
            # server crashes
            inspector.stop()
            self.server.cleanup()
        except KeyboardInterrupt:
            color_stdout("\n%s\n" % shortsep, schema='separator')
            self.server.stop(silent=False)
            raise

        if failed_tests:
            color_stdout("Failed {0} tests: {1}.\n".format(len(failed_tests),
                                                ", ".join(failed_tests)),
                                                schema='error')

        if self.args.valgrind:
            non_empty_logs = non_empty_valgrind_logs(
                self.server.current_valgrind_logs(for_suite=True))
            for log_file in non_empty_logs:
                color_stdout(shortsep, "\n", schema='separator')
                color_stdout("  Error! There were warnings/errors in valgrind log file [%s]:\n" % log_file, schema='error')
                print_tail_n(log_file, 20)
                color_stdout(shortsep, "\n", schema='separator')
            if bool(non_empty_logs):
                return ['valgrind error in ' + self.suite_path]
        return failed_tests
