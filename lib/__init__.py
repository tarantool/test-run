import os
import sys
import shutil

# monkey patch tarantool and msgpack
from lib.utils import check_libs
check_libs()

from lib.options import Options                   # noqa: E402
from lib.tarantool_server import TarantoolServer  # noqa: E402
from lib.unittest_server import UnittestServer    # noqa: E402
from utils import warn_unix_sockets_at_start      # noqa: E402
from lib.colorer import color_log                 # noqa: E402
import xlog                                       # noqa: E402


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
    os.environ["LUA_PATH"] = SOURCEDIR+"/?.lua;"+SOURCEDIR+"/?/init.lua;;"
    os.environ["LUA_CPATH"] = BUILDDIR+"/?."+soext+";;"

    TarantoolServer.find_exe(args.builddir)
    UnittestServer.find_exe(args.builddir)

    # Initialize xlog module with found tarantool / tarantoolctl.
    # Set color_log() as the function for write debug logs.
    tarantool_cmd = [TarantoolServer.binary]
    tarantoolctl_cmd = tarantool_cmd + [TarantoolServer.ctl_path]
    xlog.init(tarantool=tarantool_cmd, tarantoolctl=tarantoolctl_cmd,
              debug=lambda x: color_log(' | ' + x + '\n'))

    Options().check_snapshot_option()
    Options().check_schema_upgrade_option(TarantoolServer.debug)


# Init
######


module_init()
