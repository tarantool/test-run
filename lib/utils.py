import os
import sys
import collections
import signal
from gevent import socket


UNIX_SOCKET_LEN_LIMIT = 107


class Singleton(type):
    _instances = {}
    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
        return cls._instances[cls]


def check_libs():
    from lib.colorer import Colorer
    color_stdout = Colorer()

    deps = [
        ('msgpack', 'msgpack-python'),
        ('tarantool', 'tarantool-python')
    ]
    base_path = os.path.dirname(os.path.abspath(__file__))

    for (mod_name, mod_dir) in deps:
        mod_path = os.path.join(base_path, mod_dir)
        if mod_path not in sys.path:
            sys.path = [mod_path] + sys.path

    for (mod_name, _mod_dir) in deps:
        try:
            __import__(mod_name)
        except ImportError as e:
            color_stdout("\n\nNo %s library found\n" % mod_name, schema='error')
            print(e)
            sys.exit(1)

def non_empty_valgrind_logs(paths_to_log):
    """ Check that there were no warnings in the log."""
    non_empty_logs = []
    for path_to_log in paths_to_log:
        if os.path.exists(path_to_log) and os.path.getsize(path_to_log) != 0:
            non_empty_logs.append(path_to_log)
    return non_empty_logs

def print_tail_n(filename, num_lines):
    """Print N last lines of a file."""

    from lib.colorer import Colorer
    color_stdout = Colorer()

    with open(filename, "r+") as logfile:
        tail_n = collections.deque(logfile, num_lines)
        for line in tail_n:
            color_stdout(line, schema='tail')


def check_port(port, rais=True):
    try:
        if isinstance(port, (int, long)):
            sock = socket.create_connection(("localhost", port))
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(port)

    except socket.error:
        return True
    if rais:
        raise RuntimeError("The server is already running on port {0}".format(port))
    return False

# A list of ports used so far. Avoid reusing ports
# to reduce race conditions between starting and stopping servers.
# We're using tarantoolctl for instance control, and it reports
# a successful stop of the server before it really closes its
# network sockets
ports = {}

def find_port(port):
    global ports
    while port < 65536:
        if port not in ports and check_port(port, False):
            ports[port] = True
            return port
        port += 1
# We've made a full circle, clear the list of used ports and start
# from scratch
    ports = {}
    return find_port(34000)


def find_in_path(name):
    path = os.curdir + os.pathsep + os.environ["PATH"]
    for _dir in path.split(os.pathsep):
        exe = os.path.join(_dir, name)
        if os.access(exe, os.X_OK):
            return exe
    return ''

# http://stackoverflow.com/a/2549950
SIGNAMES = dict((v, k) for k, v in reversed(sorted(signal.__dict__.items()))
    if k.startswith('SIG') and not k.startswith('SIG_'))
def signame(signum):
    return SIGNAMES[signum]


def warn_unix_sockets_at_start(vardir):
    from colorer import Colorer
    color_stdout = Colorer()

    max_unix_socket_rel = '??_replication/autobootstrap_guest3.control'
    real_vardir = os.path.realpath(vardir)
    max_unix_socket_abs = os.path.join(real_vardir, max_unix_socket_rel)
    max_unix_socket_real = os.path.realpath(max_unix_socket_abs)
    if len(max_unix_socket_real) > UNIX_SOCKET_LEN_LIMIT:
        color_stdout(
            'WARGING: unix sockets can become longer than %d symbols:\n'
            % UNIX_SOCKET_LEN_LIMIT,
            schema='error')
        color_stdout('WARNING: for example: "%s" has length %d\n' %
                     (max_unix_socket_real, len(max_unix_socket_real)),
                     schema='error')


def warn_unix_socket(path):
    from colorer import Colorer
    color_stdout = Colorer()

    real_path = os.path.realpath(path)
    if len(real_path) <= UNIX_SOCKET_LEN_LIMIT or \
            real_path in warn_unix_socket.warned:
        return
    color_stdout(
        '\nWARGING: unix socket\'s "%s" path has length %d symbols that is '
        'longer than %d. That likely will cause failing of tests.\n' %
        (real_path, len(real_path), UNIX_SOCKET_LEN_LIMIT), schema='error')
    warn_unix_socket.warned.add(real_path)


warn_unix_socket.warned = set()
