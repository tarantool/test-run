import os
import sys
import shutil

from lib.options import Options
from lib.tarantool_server import TarantoolServer
from lib.unittest_server import UnittestServer
from lib.app_server import AppServer
from lib.luatest_server import LuatestServer
from lib.utils import warn_unix_sockets_at_start
from lib.utils import prepend_path


__all__ = ['Options', 'saved_env']


def setenv():
    """Find where is tarantool dir by check_file"""
    check_file = 'src/trivia/util.h'
    path = os.path.abspath('../')
    while path != '/':
        if os.path.isfile('%s/%s' % (path, check_file)):
            os.environ['TARANTOOL_SRC_DIR'] = path
            break
        path = os.path.abspath(os.path.join(path, '../'))


_saved_env = None


def saved_env():
    return _saved_env


def module_init():
    """ Called at import """
    global _saved_env
    _saved_env = dict(os.environ)

    args = Options().args
    # Change the current working directory to where all test
    # collections are supposed to reside
    # If script executed with (python test-run.py) dirname is ''
    # so we need to make it .
    path = os.path.dirname(sys.argv[0])
    os.environ['TEST_RUN_DIR'] = os.path.dirname(os.path.realpath(sys.argv[0]))
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

    os.environ['LUA_PATH'] = (
            SOURCEDIR + '/?.lua;' + SOURCEDIR + '/?/init.lua;'
            + os.environ['TEST_RUN_DIR'] + '/lib/checks/?.lua;'
            + os.environ['TEST_RUN_DIR'] + '/lib/luatest/?/init.lua;'
            + os.environ['TEST_RUN_DIR'] + '/lib/luatest/?.lua;;'
    )

    os.environ["LUA_CPATH"] = BUILDDIR+"/?."+soext+";;"
    os.environ["REPLICATION_SYNC_TIMEOUT"] = str(args.replication_sync_timeout)
    os.environ['MEMTX_ALLOCATOR'] = args.memtx_allocator

    prepend_path(os.path.join(os.environ['TEST_RUN_DIR'], 'lib/luatest/bin'))

    TarantoolServer.find_exe(args.builddir, executable=args.executable)
    UnittestServer.find_exe(args.builddir)
    AppServer.find_exe(args.builddir)
    LuatestServer.find_exe(args.builddir)

    Options().check_schema_upgrade_option(TarantoolServer.debug)


# Init
######


module_init()
