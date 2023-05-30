import glob
import os
import shutil
import subprocess
from itertools import product

from lib.server_mixins import ValgrindMixin
from lib.server_mixins import GdbMixin
from lib.server_mixins import GdbServerMixin
from lib.server_mixins import LLdbMixin
from lib.server_mixins import StraceMixin
from lib.server_mixins import LuacovMixin
from lib.colorer import color_stdout
from lib.options import Options
from lib.utils import print_tail_n
from lib.utils import bytes_to_str
from lib.utils import find_tags

DEFAULT_CHECKPOINT_PATTERNS = ["*.snap", "*.xlog", "*.vylog", "*.inprogress",
                               "[0-9]*/"]

DEFAULT_SNAPSHOT_NAME = "00000000000000000000.snap"


class Server(object):
    """Server represents a single server instance. Normally, the
    program operates with only one server, but in future we may add
    replication slaves. The server is started once at the beginning
    of each suite, and stopped at the end."""
    DEFAULT_INSPECTOR = 0
    TEST_RUN_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                ".."))
    # assert(false) hangs due to gh-4983, added fiber.sleep(0) to workaround it
    DISABLE_AUTO_UPGRADE = "require('fiber').sleep(0) \
        assert(box.error.injection.set('ERRINJ_AUTO_UPGRADE', true) == 'ok', \
        'no such errinj')"

    @property
    def vardir(self):
        if not hasattr(self, '_vardir'):
            raise ValueError("No vardir specified")
        return self._vardir

    @vardir.setter
    def vardir(self, path):
        if path is None:
            return
        self._vardir = os.path.abspath(path)

    @staticmethod
    def get_mixed_class(cls, ini):
        if ini is None:
            return cls

        conflict_options = ('valgrind', 'gdb', 'gdbserver', 'lldb', 'strace')
        for op1, op2 in product(conflict_options, repeat=2):
            if op1 != op2 and \
                    (op1 in ini and ini[op1]) and \
                    (op2 in ini and ini[op2]):
                format_str = 'Can\'t run under {} and {} simultaniously'
                raise OSError(format_str.format(op1, op2))

        lname = cls.__name__.lower()

        if ini.get('valgrind') and 'valgrind' not in lname:
            cls = type('Valgrind' + cls.__name__, (ValgrindMixin, cls), {})
        elif ini.get('gdbserver') and 'gdbserver' not in lname:
            cls = type('GdbServer' + cls.__name__, (GdbServerMixin, cls), {})
        elif ini.get('gdb') and 'gdb' not in lname:
            cls = type('Gdb' + cls.__name__, (GdbMixin, cls), {})
        elif ini.get('lldb') and 'lldb' not in lname:
            cls = type('LLdb' + cls.__name__, (LLdbMixin, cls), {})
        elif 'strace' in ini and ini['strace']:
            cls = type('Strace' + cls.__name__, (StraceMixin, cls), {})
        elif 'luacov' in ini and ini['luacov']:
            cls = type('Luacov' + cls.__name__, (LuacovMixin, cls), {})

        return cls

    def __new__(cls, ini=None, *args, **kwargs):
        if ini is None or 'core' not in ini or ini['core'] is None:
            return object.__new__(cls)
        core = ini['core'].lower().strip()
        cls.mdlname = "lib.{0}_server".format(core.replace(' ', '_'))
        cls.clsname = "{0}Server".format(core.title().replace(' ', ''))
        corecls = __import__(cls.mdlname,
                             fromlist=cls.clsname).__dict__[cls.clsname]
        return corecls.__new__(corecls, ini, *args, **kwargs)

    def __init__(self, ini, test_suite=None):
        self.core = ini['core']
        self.ini = ini
        self.vardir = ini['vardir']
        self.inspector_port = int(ini.get(
            'inspector_port', self.DEFAULT_INSPECTOR
        ))
        self.disable_schema_upgrade = Options().args.disable_schema_upgrade
        self.snapshot_path = Options().args.snapshot_path

        # filled in {Test,AppTest,LuaTest,PythonTest}.execute()
        # or passed through execfile() for PythonTest (see
        # TarantoolServer.__init__).
        self.current_test = None

        # Used in valgrind_log property. 'test_suite' is not None only for
        # default servers running in TestSuite.run_all()
        self.test_suite = test_suite

    @classmethod
    def version(cls):
        p = subprocess.Popen([cls.binary, "--version"], stdout=subprocess.PIPE)
        version = bytes_to_str(p.stdout.read()).rstrip()
        p.wait()
        return version

    def prepare_args(self, args=[]):
        return args

    def pretest_clean(self):
        self.cleanup()

    def cleanup(self, dirname='.'):
        waldir = os.path.join(self.vardir, dirname)
        for pattern in DEFAULT_CHECKPOINT_PATTERNS:
            for f in glob.glob(os.path.join(waldir, pattern)):
                if os.path.isdir(f):
                    shutil.rmtree(f)
                else:
                    os.remove(f)

    def install(self, binary=None, vardir=None, mem=None, silent=True):
        pass

    def init(self):
        pass

    def start(self, silent=True):
        pass

    def stop(self, silent=True):
        pass

    def restart(self):
        pass

    def print_log(self, num_lines=None):
        """ Show information from the given log file.
        """
        prefix = '\n[test-run server "{instance}"] '.format(instance=self.name)
        if not self.logfile:
            msg = 'No log file is set (internal test-run error)\n'
            color_stdout(prefix + msg, schema='error')
        elif not os.path.exists(self.logfile):
            fmt_str = 'The log file {logfile} does not exist\n'
            msg = fmt_str.format(logfile=self.logfile)
            color_stdout(prefix + msg, schema='error')
        elif os.path.getsize(self.logfile) == 0:
            fmt_str = 'The log file {logfile} has zero size\n'
            msg = fmt_str.format(logfile=self.logfile)
            color_stdout(prefix + msg, schema='error')
        elif num_lines:
            fmt_str = 'Last {num_lines} lines of the log file {logfile}:\n'
            msg = fmt_str.format(logfile=self.logfile, num_lines=num_lines)
            color_stdout(prefix + msg, schema='error')
            print_tail_n(self.logfile, num_lines)
        else:
            fmt_str = 'The log file {logfile}:\n'
            msg = fmt_str.format(logfile=self.logfile)
            color_stdout(msg, schema='error')
            print_tail_n(self.logfile, num_lines)

    @staticmethod
    def exclude_tests(test_names, exclude_patterns):
        """ Filter out test files by several criteria:

            exclude_patters: a list of strings. If a string is
            a substring of the test full name, exclude it.

            If --tags option is provided, exclude tests, which
            have no a tag from the provided list.
        """

        # TODO: Support filtering by another file (for unit
        # tests). Transform a test name into a test source name.
        # Don't forget about out of source build.
        # TODO: Support multiline comments (mainly for unit
        # tests).

        def match_any_pattern(test_name, patterns):
            for pattern in patterns:
                if pattern in test_name:
                    return True
            return False

        def match_any_tag(test_name, accepted_tags):
            tags = find_tags(test_name)
            for tag in tags:
                if tag in accepted_tags:
                    return True
            return False

        accepted_tags = Options().args.tags

        res = []
        for test_name in test_names:
            if match_any_pattern(test_name, exclude_patterns):
                continue
            if accepted_tags is None or match_any_tag(test_name, accepted_tags):
                res.append(test_name)
        return res
