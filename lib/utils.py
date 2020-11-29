import os
import sys
import six
import collections
import signal
import random
import fcntl
import difflib
import time
import json
import subprocess
from gevent import socket
from lib.colorer import color_stdout
try:
    # Python3.5 or above
    from signal import Signals
except ImportError:
    # Python2
    Signals = None


UNIX_SOCKET_LEN_LIMIT = 107


def check_libs():
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
            color_stdout("\n\nNo %s library found\n" % mod_name,
                         schema='error')
            print(e)
            sys.exit(1)


def non_empty_valgrind_logs(paths_to_log):
    """ Check that there were no warnings in the log."""
    non_empty_logs = []
    for path_to_log in paths_to_log:
        if os.path.exists(path_to_log) and os.path.getsize(path_to_log) != 0:
            non_empty_logs.append(path_to_log)
    return non_empty_logs


def print_tail_n(filename, num_lines=None):
    """ Print N last lines of a file. If num_lines is not set,
    prints the whole file.
    """
    with open(filename, "r") as logfile:
        tail_n = collections.deque(logfile, num_lines)
        for line in tail_n:
            color_stdout(line, schema='tail')


def check_port(port, rais=True, ipv4=True, ipv6=True):
    """ True -- it's possible to listen on this port for TCP/IPv4 or TCP/IPv6
    connections (UNIX Sockets in case of file path). False -- otherwise.
    """
    try:
        if isinstance(port, (int, long)):
            if ipv4:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind(('127.0.0.1', port))
                sock.listen(5)
                sock.close()
            if ipv6:
                sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                sock.bind(('::1', port))
                sock.listen(5)
                sock.close()
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(port)
    except socket.error:
        if rais:
            raise RuntimeError(
                "The server is already running on port {0}".format(port))
        return False
    return True


# A list of ports used so far. Avoid reusing ports
# to reduce race conditions between starting and stopping servers.
# We're using tarantoolctl for instance control, and it reports
# a successful stop of the server before it really closes its
# network sockets
ports = {}


is_ipv6_supported = check_port(port=0, rais=False, ipv4=False, ipv6=True)


def find_port():
    global ports
    start_port = int(os.environ.get('TEST_RUN_TCP_PORT_START', '3000'))
    end_port = int(os.environ.get('TEST_RUN_TCP_PORT_END', '65535'))
    port = random.randrange(start_port, end_port + 1)

    while port <= end_port:
        is_free = check_port(port, False, ipv4=True, ipv6=is_ipv6_supported)
        if port not in ports and is_free:
            ports[port] = True
            return port
        port += 1

    # We've made a full circle, clear the list of used ports and start
    # from scratch
    ports = {}
    return find_port()


def find_in_path(name):
    path = os.curdir + os.pathsep + os.environ["PATH"]
    for _dir in path.split(os.pathsep):
        exe = os.path.join(_dir, name)
        if os.access(exe, os.X_OK):
            return exe
    return ''


# http://stackoverflow.com/a/2549950
SIGNAMES = dict((int(v), k) for k, v in reversed(sorted(
    signal.__dict__.items())) if k.startswith('SIG') and
    not k.startswith('SIG_'))
SIGNUMS = dict((k, int(v)) for k, v in reversed(sorted(
    signal.__dict__.items())) if k.startswith('SIG') and
    not k.startswith('SIG_'))


def signame(signal):
    if isinstance(signal, six.integer_types):
        return SIGNAMES[signal]
    if Signals and isinstance(signal, Signals):
        return SIGNAMES[int(signal)]
    if isinstance(signal, six.string_types):
        return signal
    raise TypeError('signame(): signal argument of unexpected type: {}'.format(
                    str(type(signal))))


def signum(signal):
    if isinstance(signal, six.integer_types):
        return signal
    if Signals and isinstance(signal, Signals):
        return int(signal)
    if isinstance(signal, six.string_types):
        if not signal.startswith('SIG'):
            signal = 'SIG' + signal
        return SIGNUMS[signal]
    raise TypeError('signum(): signal argument of unexpected type: {}'.format(
                    str(type(signal))))


def warn_unix_sockets_at_start(vardir):
    max_unix_socket_rel = '???_replication/autobootstrap_guest3.control'
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


def safe_makedirs(directory):
    if os.path.isdir(directory):
        return
    # try-except to prevent races btw processes
    try:
        os.makedirs(directory)
    except OSError:
        pass


def format_process(pid):
    cmdline = 'unknown'
    try:
        with open('/proc/%d/cmdline' % pid, 'r') as f:
            cmdline = ' '.join(f.read().split('\0')).strip() or cmdline
    except (OSError, IOError):
        pass
    status = 'unknown'
    try:
        with open('/proc/%d/status' % pid, 'r') as f:
            for line in f:
                if ':' not in line:
                    continue
                key, value = line.split(':', 1)
                if key == 'State':
                    status = value.strip()
    except (OSError, IOError):
        pass
    return 'process %d [%s; %s]' % (pid, status, cmdline)


def set_fd_cloexec(socket):
    flags = fcntl.fcntl(socket, fcntl.F_GETFD)
    fcntl.fcntl(socket, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)


def print_unidiff(filepath_a, filepath_b):
    def process_file(filepath):
        fh = None
        try:
            fh = open(filepath, 'r')
            lines = fh.readlines()
            ctime = time.ctime(os.stat(filepath).st_mtime)
        except Exception:
            if not os.path.exists(filepath):
                color_stdout('[File does not exist: {}]\n'.format(filepath),
                             schema='error')
            lines = []
            ctime = time.ctime()
        if fh:
            fh.close()
        return lines, ctime

    lines_a, time_a = process_file(filepath_a)
    lines_b, time_b = process_file(filepath_b)
    diff = difflib.unified_diff(lines_a,
                                lines_b,
                                filepath_a,
                                filepath_b,
                                time_a,
                                time_b)
    color_stdout.writeout_unidiff(diff)


def prefix_each_line(prefix, data):
    data = data.rstrip('\n')
    lines = [(line + '\n') for line in data.split('\n')]
    return prefix + prefix.join(lines)


def just_and_trim(src, width):
    if len(src) > width:
        return src[:width - 1] + '>'
    return src.ljust(width)


def xlog_rows(xlog_path):
    """ Parse xlog / snapshot file.

        Assume tarantool and tarantoolctl is in PATH.
    """
    cmd = ['tarantoolctl', 'cat', xlog_path, '--format=json', '--show-system']
    with open(os.devnull, 'w') as devnull:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=devnull)
    for line in process.stdout.readlines():
        yield json.loads(line)


def extract_schema_from_snapshot(snapshot_path):
    """
    Extract schema version from snapshot.

    Assume tarantool and tarantoolctl is in PATH.

    Example of record:

     {
       "HEADER": {"lsn":2, "type": "INSERT", "timestamp": 1584694286.0031},
       "BODY": {"space_id": 272, "tuple": ["version", 2, 3, 1]}
     }

    :returns: [u'version', 2, 3, 1]
    """
    BOX_SCHEMA_ID = 272
    for row in xlog_rows(snapshot_path):
        if row['HEADER']['type'] == 'INSERT' and \
           row['BODY']['space_id'] == BOX_SCHEMA_ID:
            res = row['BODY']['tuple']
            if res[0] == 'version':
                return res
    return None
