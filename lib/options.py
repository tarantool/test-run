import os
import sys
import textwrap
import argparse
from itertools import product

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


def split_list(tags_str):
    # Accept both ', ' and ',' as a separator.
    #
    # It is consistent with parsing of tags within a test file.
    return [tag.strip() for tag in tags_str.split(',')]


def format_help(s):
    """
    Remove indentation and add an empty line at the end.
    """
    return textwrap.dedent(s.lstrip('\n')) + '\n'


class Options(object):
    """Handle options of test-runner"""

    _instance = None
    _initialized = False

    # Just some unique marker.
    _show_tags = {}

    def __new__(cls, *args, **kwargs):
        """Make the class singleton."""
        if cls._instance:
            return cls._instance
        cls._instance = super(Options, cls).__new__(cls, *args, **kwargs)
        return cls._instance

    def __init__(self):
        """Add all program options, with their defaults."""

        # The __init__() method is called always, even when we
        # return already initialized Options instance from
        # __new__().
        if Options._initialized:
            return

        parser = argparse.ArgumentParser(
            description="Tarantool regression test suite front-end.",
            formatter_class=argparse.RawTextHelpFormatter,
            add_help=False)

        parser.epilog = "For a complete description, use 'pydoc ./" + \
            os.path.basename(sys.argv[0]) + "'"

        parser.add_argument(
                "tests",
                metavar="test",
                nargs="*",
                default=env_list('TEST_RUN_TESTS', ['']),
                help=format_help(
                    """
                    Can be empty. List of test names, to look for in suites.
                    Each name is used as a substring to look for in the path to
                    test file, e.g. "show" will run all tests that have "show"
                    in their name in all suites, "box/show" will only enable
                    tests starting with "show" in "box" suite.

                    Default: run all tests in all specified suites.
                    """))

        # Add the --help argument explicitly to format its message
        # in accordance to other ones: start from a capital letter
        # and leave an empty line at the end.
        parser.add_argument(
                '-h', '--help',
                action='help',
                default=argparse.SUPPRESS,
                help=format_help(
                    """
                    Show this help message and exit.
                    """))

        parser.add_argument(
                "--exclude",
                action='append',
                default=env_list('TEST_RUN_EXCLUDE', []),
                help=format_help(
                    """
                    Set an exclusion pattern. When a full test name (say,
                    app-tap/string.test.lua) contains the pattern as a
                    substring, the test will be excluded from execution. The
                    option can be passed several times.
                    """))

        parser.add_argument(
                "--suite",
                dest='suites',
                metavar="suite",
                nargs="*",
                default=[],
                help=format_help(
                    """
                    List of test suites to look for tests in.

                    Default: "" (means find all available).
                    """))

        parser.add_argument(
                "--verbose",
                dest='is_verbose',
                action="store_true",
                default=False,
                help=format_help(
                    """
                    Print TAP13 test output to log.

                    Default: false.
                    """))

        parser.add_argument(
                '--debug',
                dest='debug',
                action='store_true',
                default=False,
                help=format_help(
                    """
                    Print test-run logs to the terminal.

                    Default: false.
                    """))

        parser.add_argument(
                "--force",
                dest="is_force",
                action="store_true",
                default=False,
                help=format_help(
                    """
                    Go on with other tests in case of an individual test
                    failure.

                    Default: false.
                    """))

        parser.add_argument(
                "--gdb",
                dest="gdb",
                action="store_true",
                default=False,
                help=format_help(
                    """
                    Start the server under 'gdb' debugger in detached Screen.
                    This option is mutually exclusive with --valgrind,
                    --gdbserver, --lldb and --strace.

                    Default: false.
                    """))

        parser.add_argument(
                "--gdbserver",
                dest="gdbserver",
                action="store_true",
                default=False,
                help=format_help(
                    """
                    Start the server under 'gdbserver'. This option is mutually
                    exclusive with --valgrind, --gdb, --lldb and --strace.

                    Default: false.
                    """))

        parser.add_argument(
                "--lldb",
                dest="lldb",
                action="store_true",
                default=False,
                help=format_help(
                    """
                    Start the server under 'lldb' debugger in detached Screen.
                    This option is mutually exclusive with --valgrind, --gdb,
                    --gdbserver and --strace.

                    Default: false.
                    """))

        parser.add_argument(
                "--valgrind",
                dest="valgrind",
                action="store_true",
                default=False,
                help=format_help(
                    """
                    Run the server under 'valgrind'. This option is mutually
                    exclusive with --gdb, --gdbserver, --lldb and --strace.

                    Default: false.
                    """))

        parser.add_argument(
                "--strace",
                dest="strace",
                action="store_true",
                default=False,
                help=format_help(
                    """
                    Run the server under 'strace'. This option is mutually
                    exclusive with --valgrind, --gdb, --gdbserver, --lldb and
                    --strace.

                    Default: false.
                    """))

        parser.add_argument(
                "--builddir",
                dest="builddir",
                default="..",
                help=format_help(
                    """
                    Path to project build directory.

                    Beware: A relative path is resolved against the directory,
                    where all tests reside. **NOT** a current directory of a
                    parent shell.

                    Default: .. (parent directory).
                    """))

        parser.add_argument(
                "--tarantool-port",
                dest="tarantool_port",
                default=None,
                help=format_help(
                    """
                    Listen port number to run tests against. Admin port number
                    must be listen + 1.
                    """))

        parser.add_argument(
                "--vardir",
                dest="vardir",
                default=os.environ.get('VARDIR') or '/tmp/t',
                help=format_help(
                    """
                    Path to data directory.

                    Beware: A relative path is resolved against the directory,
                    where all tests reside. **NOT** a current directory of a
                    parent shell.

                    Default: ${VARDIR} or /tmp/t.
                    """))

        parser.add_argument(
               "--long",
               dest="long",
               default=False,
               action='store_true',
               help=format_help(
                   """
                   Enable long run tests.
                   """))

        parser.add_argument(
                "--conf",
                dest="conf",
                default=None,
                help=format_help(
                    """
                    Force set test configuration mode.
                    """))

        parser.add_argument(
                "-j", "--jobs",
                dest="jobs",
                const=0,
                nargs='?',
                default=env_int('TEST_RUN_JOBS', 0),
                type=int,
                help=format_help(
                    """
                    Workers count.

                    0 means 2 x CPU count.
                    -1 means everything running consistently (single process).

                    Default: ${TEST_RUN_JOBS} or 0.
                    """))

        parser.add_argument(
                "-r", "--retries",
                dest='retries',
                default=env_int('TEST_RUN_RETRIES', 0),
                type=int,
                help=format_help(
                    """
                    The number of test run retries after a failure.

                    It is also the default value for 'fragile' tests unless the
                    `retries` option is set in the suite.ini config file.

                    Default: ${TEST_RUN_RETRIES} or 0.
                    """))

        parser.add_argument(
                "-p", "--pattern",
                dest='pattern',
                nargs='+',
                help=format_help(
                    """
                    Execute all luatest tests with names matching the given
                    Lua PATTERN.

                    Values may be repeated to include several patterns.
                    Use --verbose to control which tests were executed.
                    """))

        parser.add_argument(
                "--reproduce",
                dest="reproduce",
                default=None,
                help=format_help(
                    """
                    Run tests in the order given by the file.

                    Such files created by workers in the "var/reproduce"
                    directory.

                    Note: The option works now only with parallel testing.
                    """))

        parser.add_argument(
                "--server-start-timeout",
                dest="server_start_timeout",
                default=env_int('SERVER_START_TIMEOUT', 90),
                type=int,
                help=format_help(
                    """
                    Stop the server process if the server starts longer than
                    this amount of seconds.

                    Default: 90 [seconds].
                    """))

        parser.add_argument(
                "--test-timeout",
                dest="test_timeout",
                default=env_int('TEST_TIMEOUT', 110),
                type=int,
                help=format_help(
                    """
                    Break the test process with kill signal if the test runs
                    longer than this amount of seconds.

                    Default: 110 [seconds].
                    """))

        parser.add_argument(
                "--no-output-timeout",
                dest="no_output_timeout",
                default=env_int('NO_OUTPUT_TIMEOUT', 120),
                type=int,
                help=format_help(
                    """
                    Exit if there was no output from workers during this amount
                    of seconds. Set it to -1 to disable hang detection.

                    Default: 120 [seconds] (but disabled when one of --gdb,
                    --llgb, --valgrind, --long options is passed).

                    Note: The option works now only with parallel testing.
                    """))

        parser.add_argument(
                "--replication-sync-timeout",
                dest="replication_sync_timeout",
                default=env_int('REPLICATION_SYNC_TIMEOUT', 100),
                type=int,
                help=format_help(
                    """
                    The number of seconds that a replica will wait when trying
                    to sync with a master in a cluster, or a quorum of masters,
                    after connecting or during configuration update. This could
                    fail indefinitely if replication_sync_lag is smaller than
                    network latency, or if the replica cannot keep pace with
                    master updates. If replication_sync_timeout expires, the
                    replica enters orphan status.

                    Default: 100 [seconds].
                    """))

        parser.add_argument(
                "--luacov",
                dest="luacov",
                action="store_true",
                default=False,
                help=format_help(
                    """
                    Run the server under 'luacov'.

                    Default: false.
                    """))

        parser.add_argument(
                "--update-result",
                dest="update_result",
                action="store_true",
                default=False,
                help=format_help(
                    """
                    Update or create file with reference output (.result).

                    Default: false.
                    """))

        parser.add_argument(
                "--snapshot",
                dest='snapshot_path',
                default=None,
                type=os.path.abspath,
                help=format_help(
                    """
                    Path to snapshot that will be loaded before testing.
                    """))

        parser.add_argument(
                "--disable-schema-upgrade",
                dest='disable_schema_upgrade',
                action="store_true",
                default=False,
                help=format_help(
                    """
                    Disable schema upgrade on testing with snapshots.
                    """))

        parser.add_argument(
                "--memtx-allocator",
                dest="memtx_allocator",
                default=os.environ.get("MEMTX_ALLOCATOR", "small"),
                help=format_help(
                    """
                    Memtx allocator type for tests.
                    """))

        parser.add_argument(
                '--tags',
                dest='tags',
                const=self._show_tags,
                nargs='?',
                default=None,
                type=split_list,
                help=format_help(
                    """
                    Comma separated list of tags.

                    If tags are provided, test-run will run only those tests,
                    which are marked with ANY of the provided tags.

                    If the option is given without a parameter (at the last
                    position), test-run will show a list of tags and stop.
                    """))

        parser.add_argument(
                '--env',
                dest='show_env',
                action='store_true',
                default=False,
                help=format_help(
                    """
                    Print environment variables, which are set by test-run.

                    Useful for, say, running built-in luatest executable.

                    Usage: just source it into a current environment:

                    $ . <(./test/test-run.py --env)
                    """))

        parser.add_argument(
                '--executable',
                dest='executable',
                default=None,
                help=format_help(
                    """
                    Set a custom path to the Tarantool executable.

                    Useful when Tarantool binary is not in $BUILDDIR and $PATH.
                    """))

        # XXX: We can use parser.parse_intermixed_args() on
        # Python 3.7 to understand commands like
        # ./test-run.py foo --exclude bar baz
        self.args = parser.parse_args()

        # If `--tags foo,bar` is passed, just keep the list in
        # `args.tags`.
        #
        # If `--tags` is passed without a parameter, clean up
        # `args.tags` and toggle `args.show_tags`.
        if self.args.tags == self._show_tags:
            self.args.tags = None
            self.args.show_tags = True
        else:
            self.args.show_tags = False

        self.check()

        self.check_timeouts()

        Options._initialized = True

    def check_timeouts(self) -> None:
        default_time_offset = 10
        if (self.args.no_output_timeout - self.args.test_timeout) < default_time_offset or \
                (self.args.test_timeout - self.args.server_start_timeout) < default_time_offset:
            color_stdout("Some timeouts are set incorrectly.\n"
                         "Change the value(s) so that --no-output-timeout is at least 10 seconds "
                         "longer than --test-timeout\nand --test-timeout is at least 10 seconds "
                         "longer than --server-start-timeout\n", schema='error')
            exit(1)

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
