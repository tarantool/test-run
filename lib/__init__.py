import os
import sys
import shutil

from lib.options import Options
from lib.tarantool_server import TarantoolServer
from lib.unittest_server import UnittestServer
from utils import warn_unix_sockets_at_start

from lib.colorer import color_stdout


__all__ = ['Options']


def setenv():
    """Find where is tarantool dir by check_file"""
    check_file = 'src/fiber.h'
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

    os.environ["SOURCEDIR"] = os.path.dirname(os.getcwd())
    os.environ["BUILDDIR"]  = args.builddir

    TarantoolServer.find_exe(args.builddir)
    UnittestServer.find_exe(args.builddir)


# Init
######


module_init()
