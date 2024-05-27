try:
    # Python 2
    import ConfigParser as configparser
except ImportError:
    # Python 3
    import configparser

import json
import os
import re
import sys
import time

from lib import Options
from lib.app_server import AppServer
from lib.luatest_server import LuatestServer
from lib.colorer import color_stdout
from lib.colorer import test_line
from lib.inspector import TarantoolInspector
from lib.server import Server
from lib.tarantool_server import TarantoolServer
from lib.unittest_server import UnittestServer


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

    RETRIES_COUNT = Options().args.retries

    def get_multirun_conf(self, suite_path):
        conf_name = self.ini.get('config', None)
        if conf_name is None:
            return None

        path = os.path.join(suite_path, conf_name)
        result = None
        with open(path) as cfg:
            try:
                content = cfg.read()
                content = re.sub(r'^\s*//.*$', '', content, flags=re.M)
                result = json.loads(content)
            except ValueError:
                raise RuntimeError('Invalid multirun json')
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

    def parse_bool_opt(self, name, default):
        val = self.ini.get(name)
        if val is None:
            self.ini[name] = default
        elif isinstance(val, bool):
            pass
        elif isinstance(val, str) and val.lower() in ('true', 'false'):
            # If value is not boolean it come from ini file, need to convert
            # string 'True' or 'False' into boolean representation.
            self.ini[name] = val.lower() == 'true'
        else:
            raise ConfigurationError(name, val, "'True' or 'False'")

    def __init__(self, suite_path, args):
        """Initialize a test suite: check that it exists and contains
        a syntactically correct configuration file. Then create
        a test instance for each found test."""
        self.args = args
        self.tests = []
        self.ini = {}
        self.fragile = {'retries': self.RETRIES_COUNT, 'tests': {}}
        self.suite_path = suite_path
        self.ini["core"] = "tarantool"

        if not os.access(suite_path, os.F_OK):
            raise RuntimeError("Suite %s doesn't exist" % repr(suite_path))

        # read the suite config
        parser_kwargs = dict()
        if sys.version_info[0] == 3:
            parser_kwargs['inline_comment_prefixes'] = (';',)
            parser_kwargs['strict'] = True
        config = configparser.ConfigParser(**parser_kwargs)
        config.read(os.path.join(suite_path, "suite.ini"))
        self.ini.update(dict(config.items("default")))
        self.ini.update(self.args.__dict__)
        self.multi_run = self.get_multirun_conf(suite_path)

        # list of long running tests
        if 'long_run' not in self.ini:
            self.ini['long_run'] = []

        for i in ["script"]:
            self.ini[i] = os.path.join(suite_path, self.ini[i]) \
                if i in self.ini else None
        for i in ["disabled", "valgrind_disabled", "release_disabled",
                  "fragile"]:
            self.ini[i] = dict.fromkeys(self.ini[i].split()) \
                if i in self.ini else dict()
        for i in ["lua_libs"]:
            self.ini[i] = map(
                lambda x: os.path.join(suite_path, x),
                dict.fromkeys(self.ini[i].split())
                if i in self.ini else dict())
        if config.has_option("default", "fragile"):
            fragiles = config.get("default", "fragile")
            try:
                self.fragile.update(json.loads(fragiles))
                if 'tests' not in self.fragile:
                    raise RuntimeError(
                        "Key 'tests' absent in 'fragile' json: {}"
                        . format(self.fragile))
            except ValueError:
                # use old format dictionary
                self.fragile['tests'] = self.ini['fragile']

        self.parse_bool_opt('use_unix_sockets_iproto', False)
        self.parse_bool_opt('is_parallel', False)
        self.parse_bool_opt('show_reproduce_content', True)

        # XXX: Refactor *Server.find_tests() to return a value
        # instead of direct changing of test_suite.tests and get
        # rid of all other side effects.
        self.tests_are_collected = False

        if self.ini['core'] == 'luatest':
            LuatestServer.verify_luatest_exe()

    def collect_tests(self):
        if self.tests_are_collected:
            return self.tests

        if self.ini['core'] == 'tarantool':
            TarantoolServer.find_tests(self, self.suite_path)
        elif self.ini['core'] == 'luatest':
            LuatestServer.find_tests(self, self.suite_path)
        elif self.ini['core'] == 'app':
            AppServer.find_tests(self, self.suite_path)
        elif self.ini['core'] == 'unittest':
            UnittestServer.find_tests(self, self.suite_path)
        elif self.ini['core'] == 'stress':
            # parallel tests are not supported and disabled for now
            self.tests = []
            self.tests_are_collected = True
            return self.tests
        else:
            raise ValueError(
                'Cannot collect tests of unknown type: %s' % self.ini['core'])

        # In given cases, this large output looks redundant.
        if not Options().args.reproduce and not Options().args.show_tags:
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
        self.tests_are_collected = True
        return self.tests

    def get_fragile_list(self):
        return self.fragile['tests'].keys()

    def stable_tests(self):
        self.collect_tests()
        res = []
        for test in self.tests:
            if os.path.basename(test.name) not in self.get_fragile_list():
                res.append(test)
        return res

    def fragile_tests(self):
        self.collect_tests()
        res = []
        for test in self.tests:
            if os.path.basename(test.name) in self.get_fragile_list():
                res.append(test)
        return res

    def gen_server(self):
        try:
            return Server(self.ini, test_suite=self)
        except Exception as e:
            print(e)
            raise RuntimeError("Unknown server: core = {0}".format(
                               self.ini["core"]))

    def is_test_enabled(self, test, conf, server):
        test_name = os.path.basename(test.name)
        tconf = '%s:%s' % (test_name, conf or '')
        checks = [
            (True, self.ini["disabled"]),
            (not server.debug, self.ini["release_disabled"]),
            (self.args.valgrind, self.ini["valgrind_disabled"]),
            (not self.args.long, self.ini["long_run"])
        ]
        for check in checks:
            check_enabled, disabled_tests = check
            if check_enabled and (test_name in disabled_tests or
                                  tconf in disabled_tests):
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
        # Set 'lua' type for *.test.lua and *.test.sql test files.
        server.tests_type = 'python' if suite_name.endswith('-py') else 'lua'
        server.deploy(silent=False)
        return inspector

    def stop_server(self, server, inspector, silent=False, cleanup=True):
        if server:
            server.stop(silent=silent)
        if inspector:
            inspector.stop()
        # don't delete core files or state of the data dir
        # in case of exception, which is raised when the
        # server crashes
        if cleanup and inspector:
            inspector.cleanup_nondefault()
        if cleanup and server:
            server.cleanup()

    def run_test(self, test, server, inspector):
        """ Returns short status of the test as a string: 'skip', 'pass',
            'new', 'fail', or 'disabled'.
        """
        test.inspector = inspector
        test_name = os.path.basename(test.name)
        full_test_name = os.path.join(self.ini['suite'], test_name)
        test_line(full_test_name, test.conf_name)

        start_time = time.time()
        if self.is_test_enabled(test, test.conf_name, server):
            short_status = test.run(server)
        else:
            color_stdout("[ disabled ]\n", schema='t_name')
            short_status = 'disabled'
        duration = time.time() - start_time

        # cleanup only if test passed or if --force mode enabled
        if Options().args.is_force or short_status == 'pass':
            inspector.cleanup_nondefault()

        return short_status, duration

    def is_parallel(self):
        return self.ini['is_parallel']

    def fragile_retries(self):
        return self.fragile['retries']

    def show_reproduce_content(self):
        return self.ini['show_reproduce_content']

    def test_is_long(self, task_id):
        return os.path.basename(task_id[0]) in self.ini['long_run']
