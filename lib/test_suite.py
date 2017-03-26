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
            raise RuntimeError("Unknown server: core = {0}".format(
                               self.ini["core"]))
        return server

    def is_test_enabled(self, test, conf, server):
        test_name = os.path.basename(test.name)
        tconf = '%s:%s' % (test_name, conf)
        checks = [
            (True, self.ini["disabled"]),
            (not server.debug, self.ini["release_disabled"]),
            (self.args.valgrind, self.ini["valgrind_disabled"]),
            (not self.args.long, self.ini["long_run"])]
        for check in checks:
            check_enabled, disabled_tests = check
            if check_enabled and (test_name in disabled_tests
                    or tconf in disabled_tests):
                return False
        return True

    def start_server(self, server):
        # create inspectpor daemon for cluster tests
        inspector = TarantoolInspector(
            'localhost', server.inspector_port
        )
        inspector.start()
        server.deploy(silent=False)
        return inspector

    def stop_server(self, server, inspector, silent=False):
        server.stop(silent=silent)
        # don't delete core files or state of the data dir
        # in case of exception, which is raised when the
        # server crashes
        inspector.stop()
        server.cleanup()

    def run_test(self, test, server, inspector):
        # fixme: remove this string if we fix all legacy tests
        server.cls = test.__class__

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

        if self.is_test_enabled(test, conf, server):
            return test.run(server)
        else:
            color_stdout("[ disabled ]\n", schema='t_name')
            return 'disabled'
