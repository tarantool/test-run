import os
import sys
import shutil
import atexit
import signal
import traceback
from ast import literal_eval

from options              import Options
from lib.tarantool_server import TarantoolServer
from lib.unittest_server  import UnittestServer

from lib.colorer import Colorer
color_stdout = Colorer()


def warn_unix_sockets():
    unix_socket_len_limit = 107
    max_unix_socket_rel = './var/??_replication/autobootstrap_guest3.control'
    max_unix_socket_abs = os.path.realpath(max_unix_socket_rel)
    if len(max_unix_socket_abs) > unix_socket_len_limit:
        color_stdout('WARGING: unix sockets can become longer than 107 symbols:\n',
                     schema='error')
        color_stdout('WARNING: for example: "%s" has length %d\n' % \
            (max_unix_socket_abs, len(max_unix_socket_abs)), schema='error')


def parse_tests_file(tests_file):
    reproduce = []
    try:
        if tests_file:
            with open(tests_file, 'r') as f:
                for line in f:
                    task_id = literal_eval(line)
                    reproduce.append(task_id)
    except IOError:
        color_stdout('Cannot read "%s" passed as --reproduce argument\n' %
            tests_file, schema='error')
        exit(1)
    return reproduce


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
    options = Options()
    oldcwd = os.getcwd()
    # Change the current working directory to where all test
    # collections are supposed to reside
    # If script executed with (python test-run.py) dirname is ''
    # so we need to make it .
    path = os.path.dirname(sys.argv[0])
    if not path:
        path = '.'
    os.chdir(path)
    setenv()

    warn_unix_sockets()
    reproduce = parse_tests_file(options.args.reproduce)

    # always run with clean (non-existent) 'var' directory
    try:
        shutil.rmtree(options.args.vardir)
    except OSError:
        pass

    options.args.builddir = os.path.abspath(os.path.expanduser(options.args.builddir))
    os.environ["SOURCEDIR"] = os.path.dirname(os.path.abspath(path))
    os.environ["BUILDDIR"] = os.path.abspath(options.args.builddir)

    TarantoolServer.find_exe(options.args.builddir) # XXX: can raise
    UnittestServer.find_exe(options.args.builddir)

    return (options, oldcwd, reproduce)

@atexit.register
def module_del():
    """ Called before the module exit """
    if 'oldcwd' in globals().keys() and oldcwd:
        os.chdir(oldcwd)


# Globals
#########


options, oldcwd, reproduce = module_init()
