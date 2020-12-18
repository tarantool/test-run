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
            description="Tarantool regression test suite front-end.")

        parser.epilog = "For a complete description, use 'pydoc ./" + \
            os.path.basename(sys.argv[0]) + "'"

        parser.add_argument(
                "tests",
                metavar="test",
                nargs="*",
                default=env_list('TEST_RUN_TESTS', ['']),
                help="""Can be empty. List of test names, to look for in
                suites. Each name is used as a substring to look for in the
                path to test file, e.g. "show" will run all tests that have
                "show" in their name in all suites, "box/show" will only enable
                tests starting with "show" in "box" suite. Default: run all
                tests in all specified suites.""")

        parser.add_argument(
                "--exclude",
                action='append',
                default=env_list('TEST_RUN_EXCLUDE', []),
                help="""Set an exclusion pattern. When a full test name (say,
                app-tap/string.test.lua) contains the pattern as a substring,
                the test will be excluded from execution. The option can be
                passed several times.""")

        parser.add_argument(
                "--suite",
                dest='suites',
                metavar="suite",
                nargs="*",
                default=[],
                help="""List of test suites to look for tests in. Default: "" -
                means find all available.""")

        parser.add_argument(
                "--verbose",
                dest='is_verbose',
                action="store_true",
                default=False,
                help="""Print TAP13 test output to log.
                Default: false.""")

        parser.add_argument(
                '--debug',
                dest='debug',
                action='store_true',
                default=False,
                help="""Print test-run logs to the terminal.
                Default: false.""")

        parser.add_argument(
                "--force",
                dest="is_force",
                action="store_true",
                default=False,
                help="""Go on with other tests in case of an individual test failure.
                Default: false.""")

        parser.add_argument(
                "--gdb",
                dest="gdb",
                action="store_true",
                default=False,
                help="""Start the server under 'gdb' debugger in detached
                Screen. This option is mutually exclusive with --valgrind,
                --gdbserver, --lldb and --strace.
                Default: false.""")

        parser.add_argument(
                "--gdbserver",
                dest="gdbserver",
                action="store_true",
                default=False,
                help="""Start the server under 'gdbserver'. This option is
                mutually exclusive with --valgrind, --gdb, --lldb and --strace.
                Default: false.""")

        parser.add_argument(
                "--lldb",
                dest="lldb",
                action="store_true",
                default=False,
                help="""Start the server under 'lldb' debugger in detached
                Screen. This option is mutually exclusive with --valgrind,
                --gdb, --gdbserver and --strace.
                Default: false.""")

        parser.add_argument(
                "--valgrind",
                dest="valgrind",
                action="store_true",
                default=False,
                help="""Run the server under 'valgrind'. This option is
                mutually exclusive with --gdb, --gdbserver, --lldb and
                --strace.
                Default: false.""")

        parser.add_argument(
                "--strace",
                dest="strace",
                action="store_true",
                default=False,
                help="""Run the server under 'strace'. This option is mutually
                exclusive with --valgrind, --gdb, --gdbserver, --lldb and
                --strace.
                Default: false.""")

        parser.add_argument(
                "--builddir",
                dest="builddir",
                default="..",
                help="""Path to project build directory. Default: ".." """)

        parser.add_argument(
                "--tarantool-port",
                dest="tarantool_port",
                default=None,
                help="""Listen port number to run tests against. Admin port
                number must be listen + 1""")

        parser.add_argument(
                "--vardir",
                dest="vardir",
                default="var",
                help="""Path to data directory. Default: var.""")
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
                "--test-timeout",
                dest="test_timeout",
                default=env_int('TEST_TIMEOUT', 110),
                type=int,
                help="""Break the test process with kill signal if the test runs
                longer than this amount of seconds. Default: 110 [seconds].""")

        parser.add_argument(
                "--no-output-timeout",
                dest="no_output_timeout",
                default=env_int('NO_OUTPUT_TIMEOUT', 120),
                type=int,
                help="""Exit if there was no output from workers during this
                amount of seconds. Set it to -1 to disable hang detection.
                Default: 120 [seconds] (but disabled when one of --gdb, --llgb,
                --valgrind, --long options is passed).
                Note: The option works now only with parallel testing.""")

        parser.add_argument(
                "--replication-sync-timeout",
                dest="replication_sync_timeout",
                default=env_int('REPLICATION_SYNC_TIMEOUT', 100),
                type=int,
                help="""The number of seconds that a replica will wait when
                trying to sync with a master in a cluster, or a quorum of
                masters, after connecting or during configuration update.
                This could fail indefinitely if replication_sync_lag is smaller
                than network latency, or if the replica cannot keep pace with
                master updates. If replication_sync_timeout expires, the replica
                enters orphan status.
                Default: 100 [seconds].""")

        parser.add_argument(
                "--luacov",
                dest="luacov",
                action="store_true",
                default=False,
                help="""Run the server under 'luacov'.
                Default: false.""")

        parser.add_argument(
                "--update-result",
                dest="update_result",
                action="store_true",
                default=False,
                help="""Update or create file with reference output (.result).
                Default: false.""")

        parser.add_argument(
                "--snapshot",
                dest='snapshot_path',
                default=None,
                type=os.path.abspath,
                help="""Path to snapshot that will be loaded before testing.""")

        parser.add_argument(
                "--disable-schema-upgrade",
                dest='disable_schema_upgrade',
                action="store_true",
                default=False,
                help="""Disable schema upgrade on testing with snapshots.""")

        # XXX: We can use parser.parse_intermixed_args() on
        # Python 3.7 to understand commands like
        # ./test-run.py foo --exclude bar baz
        self.args = parser.parse_args()
        self.check()

    def check(self):
        """Check the arguments for correctness."""
        check_error = False
        conflict_options = ('valgrind', 'gdb', 'lldb', 'strace')
        for op1, op2 in product(conflict_options, repeat=2):
            if op1 != op2 and getattr(self.args, op1, '') and \
                    getattr(self.args, op2, ''):
                format_str = "\nError: option --{} is not compatible with option --{}\n"
                color_stdout(format_str.format(op1, op2), schema='error')
                check_error = True
                break

        snapshot_path = self.args.snapshot_path
        if self.args.disable_schema_upgrade and not snapshot_path:
            color_stdout("\nOption --disable-schema-upgrade requires --snapshot\n",
                         schema='error')
            check_error = True

        if snapshot_path and not os.path.exists(snapshot_path):
            color_stdout("\nPath {} does not exist\n".format(snapshot_path), schema='error')
            check_error = True

        if check_error:
            exit(-1)

    def check_schema_upgrade_option(self, is_debug):
        if self.args.disable_schema_upgrade and not is_debug:
            color_stdout("Can't disable schema upgrade on release build\n", schema='error')
            exit(1)
