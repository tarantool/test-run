import ConfigParser
import json
import os

from lib.colorer import Colorer
from lib.inspector import TarantoolInspector
from lib.server import Server
from lib.tarantool_server import TarantoolServer
from lib.app_server import AppServer
from lib.unittest_server import UnittestServer
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

        for i in ["script"]:
            self.ini[i] = os.path.join(suite_path, self.ini[i]) if i in self.ini else None
        for i in ["disabled", "valgrind_disabled", "release_disabled"]:
            self.ini[i] = dict.fromkeys(self.ini[i].split()) if i in self.ini else dict()
        for i in ["lua_libs"]:
            self.ini[i] = map(lambda x: os.path.join(suite_path, x),
                    dict.fromkeys(self.ini[i].split()) if i in self.ini else dict())

    def find_tests(self):
        color_stdout("Collecting tests in ", schema='ts_text')
        color_stdout(repr(self.suite_path), schema='path')
        color_stdout(": ", self.ini["description"], ".\n", schema='ts_text')

        if self.ini['core'] == 'tarantool':
            TarantoolServer.find_tests(self, self.suite_path)
        elif self.ini['core'] == 'app':
            AppServer.find_tests(self, self.suite_path)
        elif self.ini['core'] == 'unittest':
            UnittestServer.find_tests(self, self.suite_path)
        elif self.ini['core'] == 'stress':
            return []  # parallel tests are broken and disabled for now
        else:
            raise ValueError('Cannot collect tests of unknown type')

        color_stdout("Found ", str(len(self.tests)), " tests.\n", schema='path')
        return self.tests

    def gen_server(self):
        try:
            if self.ini['core'] in ['tarantool', 'stress']:
                server = TarantoolServer(self.ini, test_suite=self)
            else:
                server = Server(self.ini, test_suite=self)
        except Exception as e:
            print e
            #raise e # XXX: remove it
            raise RuntimeError("Unknown server: core = {0}".format(
                               self.ini["core"]))
        return server

    def start_server(self, server):
        # create inspectpor daemon for cluster tests
        inspector = TarantoolInspector(
            'localhost', server.inspector_port
        )
        inspector.start()
        server.deploy(silent=False)
        return inspector

    def stop_server(self, server, inspector, silent=False):
        #color_stdout(shortsep, "\n", schema='separator') # XXX
        server.stop(silent=silent)
        # don't delete core files or state of the data dir
        # in case of exception, which is raised when the
        # server crashes
        inspector.stop()
        server.cleanup()

    def run_test(self, test, server, inspector):
        # fixme: remove this string if we fix all legacy tests
        server.cls = test.__class__

# TODO: move it to somewhere
#        longsep = '='*80
#        shortsep = '-'*75
#        color_stdout(longsep, "\n", schema='separator')
#        color_stdout("TEST".ljust(48), schema='t_name')
#        color_stdout("PARAMS\t\t", schema='test_var')
#        color_stdout("RESULT\n", schema='test_pass')
#        color_stdout(shortsep, "\n", schema='separator')

        try:
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
            test_name = os.path.basename(test.name)
            if (test_name in self.ini["disabled"]
                or not server.debug and test_name in self.ini["release_disabled"]
                or self.args.valgrind and test_name in self.ini["valgrind_disabled"]
                or not self.args.long and test_name in self.ini.get("long_run", [])):
                color_stdout("[ disabled ]\n", schema='t_name')
            else:
                test.run(server)
                return test.passed()
        except KeyboardInterrupt:
            # XXX: move it above by a function call stack
            #color_stdout("\n%s\n" % shortsep, schema='separator')
            #server.stop(silent=False)
            raise

# TODO: return TaskStatus(...)
#        if failed_tests:
#            color_stdout("Failed {0} tests: {1}.\n".format(len(failed_tests),
#                                                ", ".join(failed_tests)),
#                                                schema='error')

# XXX: maybe we already handle all valgrind cases in test.py?
#        if self.args.valgrind:
#            non_empty_logs = non_empty_valgrind_logs(
#                self.server.current_valgrind_logs(for_suite=True))
#            for log_file in non_empty_logs:
#                color_stdout(shortsep, "\n", schema='separator')
#                color_stdout("  Error! There were warnings/errors in valgrind log file [%s]:\n" % log_file, schema='error')
#                print_tail_n(log_file, 20)
#                color_stdout(shortsep, "\n", schema='separator')
#            if bool(non_empty_logs):
#                return ['valgrind error in ' + self.suite_path]
#        return failed_tests
