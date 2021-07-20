import os
import sys
import shutil

from lib.options import Options
from lib.tarantool_server import TarantoolServer
from lib.unittest_server import UnittestServer
from lib.app_server import AppServer
from lib.luatest_server import LuatestServer
from lib.utils import warn_unix_sockets_at_start
from lib.worker import find_suites

__all__ = ['Options']


def setenv():
    """Find where is tarantool dir by check_file"""
    check_file = 'src/trivia/util.h'
    path = os.path.abspath('../')
    while path != '/':
        if os.path.isfile('%s/%s' % (path, check_file)):
            os.putenv('TARANTOOL_SRC_DIR', path)
            break
        path = os.path.abspath(os.path.join(path, '../'))


def module_init():
    """ Called at import """
    args = Options().args
    # Change the current working directory to where all test
    # collections are supposed to reside
    # If script executed with (python test-run.py) dirname is ''
    # so we need to make it .
    path = os.path.dirname(sys.argv[0])
    if not path:
        path = '.'
    os.chdir(path)
    setenv()

    # Keep the PWD environment variable in sync with a current
    # working directory. It does not strictly necessary, just to
    # avoid any confusion.
    os.environ['PWD'] = os.getcwd()

    warn_unix_sockets_at_start(args.vardir)

    # always run with clean (non-existent) 'var' directory
    try:
        shutil.rmtree(args.vardir)
    except OSError:
        pass

    args.builddir = os.path.abspath(os.path.expanduser(args.builddir))

    SOURCEDIR = os.path.dirname(os.getcwd())
    BUILDDIR = args.builddir
    os.environ["SOURCEDIR"] = SOURCEDIR
    os.environ["BUILDDIR"] = BUILDDIR
    soext = sys.platform == 'darwin' and 'dylib' or 'so'

    def find_dir(path, dir_name, level=2):
        """Check directory exists at the path or on levels above.

        For example,
            path = 'foo/bar',
            dir_name = 'baz',
            level = 2 (default)
            Return True if baz exists by foo/bar/baz or foo/baz path.
        """
        level -= 1
        if os.path.isdir(os.path.join(path, dir_name)):
            return os.path.join(path, dir_name)
        if level:
            return find_dir(os.path.split(path)[0], dir_name, level)

    def is_core_in_suite(core):
        """Check there is core in current tests."""
        return core in [suite.ini["core"] for suite in find_suites()]

    ROCKS_DIR = find_dir(SOURCEDIR, '.rocks') or find_dir(BUILDDIR, '.rocks')
    if not ROCKS_DIR and is_core_in_suite('luatest'):
        raise Exception(
            '.rocks was not found in source dir = %s and build dir = %s' %
            (SOURCEDIR, BUILDDIR))
    os.environ["PATH"] += ":" + ROCKS_DIR
    os.environ["LUA_PATH"] = (SOURCEDIR + "/test/?.lua;" +
                              SOURCEDIR + "/test/luatest_helpers/?.lua;"
                              + SOURCEDIR + "/?.lua;"
                              + SOURCEDIR + "/?/init.lua;;")
    os.environ['LUATEST_BIN'] = os.path.join(ROCKS_DIR, "bin/luatest")
    os.environ["LUA_CPATH"] = BUILDDIR + "/?." + soext + ";;"
    os.environ["REPLICATION_SYNC_TIMEOUT"] = str(args.replication_sync_timeout)
    os.environ['MEMTX_ALLOCATOR'] = args.memtx_allocator

    TarantoolServer.find_exe(args.builddir)
    UnittestServer.find_exe(args.builddir)
    AppServer.find_exe(args.builddir)
    LuatestServer.find_exe(args.builddir)

    Options().check_schema_upgrade_option(TarantoolServer.debug)


# Init
######


module_init()
