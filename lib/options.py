import os
import sys
import argparse
from itertools import product

from lib.colorer import Colorer
color_stdout = Colorer()


class Options:
    """Handle options of test-runner"""
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
                default = [""],
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
                "--builddir",
                dest = "builddir",
                default = "..",
                help = """Path to project build directory. Default: " + "../.""")

        parser.add_argument(
                "--stress",
                dest = "stress",
                default = None,
                help = """Name of stress TestSuite to run""")

        parser.add_argument(
                "--tarantool-port",
                dest = "tarantool_port",
                default = None,
                help = """Listen port number to run tests against. Admin port number must be listen+1""")

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
                help="""Force set test configuration mode"""
        )

        self.args = parser.parse_args()
        self.check()

    def check(self):
        """Check the arguments for correctness."""
        check_error = False
        conflict_options = ('valgrind', 'gdb', 'lldb')
        for op1, op2 in product(conflict_options, repeat=2):
            if op1 != op2 and getattr(self, op1, '') and \
                    getattr(self, op2, ''):
                format_str = "Error: option --{} is not compatible \
                                with option --{}"
                color_stdout(format_str.format(op1, op2), schema='error')
                check_error = True


        if check_error:
            exit(-1)



