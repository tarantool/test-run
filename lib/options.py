import os
import sys
import argparse
from itertools import product
from lib.singleton import Singleton

from lib.colorer import color_stdout


def env_int(name, default):
    try:
        value = os.environ.get(name)
        return default if value is None else int(value)
    except ValueError:
        return default


def env_list(name, default):
    value_str = os.environ.get(name)
    if value_str is None:
        return default
    value_list = value_str.split()
    return value_list or default


class Options:
    """Handle options of test-runner"""

    __metaclass__ = Singleton

    def __init__(self):
        """Add all program options, with their defaults."""

        parser = argparse.ArgumentParser(
                description = "Tarantool regression test suite front-end.")

        parser.epilog = "For a complete description, use 'pydoc ./" +\
                os.path.basename(sys.argv[0]) + "'"

        parser.add_argument(
                "tests",
                metavar="test",
                nargs="*",
                default = env_list('TEST_RUN_TESTS', ['']),
                help="""Can be empty. List of test names, to look for in suites. Each
                name is used as a substring to look for in the path to test file,
                e.g. "show" will run all tests that have "show" in their name in all
                suites, "box/show" will only enable tests starting with "show" in
                "box" suite. Default: run all tests in all specified suites.""")

        parser.add_argument(
                "--suite",
                dest = 'suites',
                metavar = "suite",
                nargs="*",
                default = [],
                help = """List of test suites to look for tests in. Default: "" -
                means find all available.""")

        parser.add_argument(
                "--force",
                dest = "is_force",
                action = "store_true",
                default = False,
                help = """Go on with other tests in case of an individual test failure.
                Default: false.""")

        parser.add_argument(
                "--gdb",
                dest = "gdb",
                action = "store_true",
                default = False,
                help = """Start the server under 'gdb' debugger in detached
                Screen. This option is mutually exclusive with --valgrind and
                --lldb.
                Default: false.""")

        parser.add_argument(
                "--lldb",
                dest = "lldb",
                action = "store_true",
                default = False,
                help = """Start the server under 'lldb' debugger in detached
                Screen. This option is mutually exclusive with --valgrind
                and --gdb.
                Default: false.""")


        parser.add_argument(
                "--valgrind",
                dest = "valgrind",
                action = "store_true",
                default = False,
                help = "Run the server under 'valgrind'. Default: false.")

        parser.add_argument(
                "--strace",
                dest = "strace",
                action = "store_true",
                default = False,
                help = "Run the server under 'strace'. Default: false.")

        parser.add_argument(
                "--builddir",
                dest = "builddir",
                default = "..",
                help = """Path to project build directory. Default: ".." """)

        parser.add_argument(
                "--tarantool-port",
                dest = "tarantool_port",
                default = None,
                help = """Listen port number to run tests against. Admin port
                number must be listen + 1""")

        parser.add_argument(
                "--vardir",
                dest = "vardir",
                default = "var",
                help = """Path to data directory. Default: var.""")
        parser.add_argument(
               "--long",
               dest="long",
               default=False,
               action='store_true',
               help="""Enable long run tests""")

        parser.add_argument(
                "--conf",
                dest="conf",
                default=None,
                help="""Force set test configuration mode""")

        parser.add_argument(
                "-j", "--jobs",
                dest="jobs",
                const=0,
                nargs='?',
                default=env_int('TEST_RUN_JOBS', 0),
                type=int,
                help="""Workers count. Default: ${TEST_RUN_JOBS} or 0 (0 means
                2 x CPU count). -1 means everything running consistently
                (single process). """)

        parser.add_argument(
                "--reproduce",
                dest="reproduce",
                default=None,
                help="""Run tests in the order given by the file.
                Such files created by workers in the "var/reproduce" directory.
                Note: The option works now only with parallel testing.""")

        parser.add_argument(
                "--no-output-timeout",
                dest="no_output_timeout",
                default=0,
                type=int,
                help="""Exit if there was no output from workers during this
                amount of seconds. Set it to -1 to disable hang detection.
                Default: 120 [seconds] (but disabled when one of --gdb, --llgb,
                --valgrind, --long options is passed).
                Note: The option works now only with parallel testing.""")

        self.args = parser.parse_args()
        self.check()

    def check(self):
        """Check the arguments for correctness."""
        check_error = False
        conflict_options = ('valgrind', 'gdb', 'lldb', 'strace')
        for op1, op2 in product(conflict_options, repeat=2):
            if op1 != op2 and getattr(self, op1, '') and \
                    getattr(self, op2, ''):
                format_str = "Error: option --{} is not compatible \
                                with option --{}"
                color_stdout(format_str.format(op1, op2), schema='error')
                check_error = True

        if check_error:
            exit(-1)
