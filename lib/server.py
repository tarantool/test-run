import glob
import os
import shutil
from itertools import product
from lib.server_mixins import ValgrindMixin
from lib.server_mixins import GdbMixin
from lib.server_mixins import GdbServerMixin
from lib.server_mixins import LLdbMixin
from lib.server_mixins import StraceMixin


class Server(object):
    """Server represents a single server instance. Normally, the
    program operates with only one server, but in future we may add
    replication slaves. The server is started once at the beginning
    of each suite, and stopped at the end."""
    DEFAULT_INSPECTOR = 0
    TEST_RUN_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    @property
    def vardir(self):
        if not hasattr(self, '_vardir'):
            raise ValueError("No vardir specified")
        return self._vardir
    @vardir.setter
    def vardir(self, path):
        if path == None:
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

        if ini.get('valgrind') and not 'valgrind' in lname:
            cls = type('Valgrind' + cls.__name__, (ValgrindMixin, cls), {})
        elif ini.get('gdbserver') and not 'gdbserver' in lname:
            cls = type('GdbServer' + cls.__name__, (GdbServerMixin, cls), {})
        elif ini.get('gdb') and not 'gdb' in lname:
            cls = type('Gdb' + cls.__name__, (GdbMixin, cls), {})
        elif ini.get('lldb') and not 'lldb' in lname:
            cls = type('LLdb' + cls.__name__, (LLdbMixin, cls), {})
        elif 'strace' in ini and ini['strace']:
            cls = type('Strace' + cls.__name__, (StraceMixin, cls), {})

        return cls

    def __new__(cls, ini=None, *args, **kwargs):
        if ini == None or 'core' not in ini or ini['core'] is None:
            return object.__new__(cls)
        core = ini['core'].lower().strip()
        cls.mdlname = "lib.{0}_server".format(core.replace(' ', '_'))
        cls.clsname = "{0}Server".format(core.title().replace(' ', ''))
        corecls = __import__(cls.mdlname, fromlist=cls.clsname).__dict__[cls.clsname]
        return corecls.__new__(corecls, ini, *args, **kwargs)

    def __init__(self, ini, test_suite=None):
        self.core = ini['core']
        self.ini = ini
        self.re_vardir_cleanup = ['*.core.*', 'core']
        self.vardir = ini['vardir']
        self.inspector_port = int(ini.get(
            'inspector_port', self.DEFAULT_INSPECTOR
        ))

        # filled in {Test,FuncTest,LuaTest,PythonTest}.execute()
        # or passed through execfile() for PythonTest (see
        # TarantoolServer.__init__).
        self.current_test = None

        # Used in valgrind_log property. 'test_suite' is not None only for
        # default servers running in TestSuite.run_all()
        self.test_suite = test_suite

    def prepare_args(self, args=[]):
        return args

    def cleanup(self, full=False):
        if full:
            shutil.rmtree(self.vardir)
            return
        for re in self.re_vardir_cleanup:
            for f in glob.glob(os.path.join(self.vardir, re)):
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
