import errno
import os
import sys
import collections
import signal
import fcntl
import difflib
import time
import json
import subprocess
import multiprocessing
from lib.colorer import color_stdout

try:
    # Python3.5 or above
    from signal import Signals
except ImportError:
    # Python2
    Signals = None

try:
    # Python 3.3+.
    from shlex import quote as _shlex_quote
except ImportError:
    # Python 2.7.
    from pipes import quote as _shlex_quote

try:
    # Python 3.3+.
    from shutil import get_terminal_size
except ImportError:
    # Python 2.7.
    get_terminal_size = None

try:
    # Python 3.3+
    from os import sched_getaffinity
except ImportError:
    sched_getaffinity = None

UNIX_SOCKET_LEN_LIMIT = 107

# Useful for very coarse version differentiation.
PY3 = sys.version_info[0] == 3
PY2 = sys.version_info[0] == 2

if PY2:
    FileNotFoundError = IOError

if PY3:
    string_types = str,
    integer_types = int,
else:
    string_types = basestring,      # noqa: F821
    integer_types = (int, long)     # noqa: F821


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
    with open(filename, "r", encoding="utf-8", errors="replace") as logfile:
        tail_n = collections.deque(logfile, num_lines)
        for line in tail_n:
            color_stdout(line, schema='tail')


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
    if isinstance(signal, integer_types):
        return SIGNAMES[signal]
    if Signals and isinstance(signal, Signals):
        return SIGNAMES[int(signal)]
    if isinstance(signal, string_types):
        return signal
    raise TypeError('signame(): signal argument of unexpected type: {}'.format(
                    str(type(signal))))


def signum(signal):
    if isinstance(signal, integer_types):
        return signal
    if Signals and isinstance(signal, Signals):
        return int(signal)
    if isinstance(signal, string_types):
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


def proc_stat_rss_supported():
    return os.path.isfile('/proc/%d/status' % os.getpid())


def get_proc_stat_rss(pid):
    rss = 0
    try:
        with open('/proc/%d/status' % pid, 'r') as f:
            for line in f:
                if ':' not in line:
                    continue
                key, value = line.split(':', 1)
                if key == 'VmRSS':
                    rss = int(value.strip().split()[0])
    except (OSError, IOError):
        pass
    return rss


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
    if not os.path.exists(xlog_path):
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), xlog_path)
    cmd = ['tarantoolctl', 'cat', xlog_path, '--format=json', '--show-system']
    with open(os.devnull, 'w') as devnull:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=devnull)
    for line in process.stdout.readlines():
        yield json.loads(bytes_to_str(line))


def extract_schema_from_snapshot(snapshot_path):
    """
    Extract schema version from snapshot.

    Assume tarantool and tarantoolctl is in PATH.

    Example of record:

     {
       "HEADER": {"lsn":2, "type": "INSERT", "timestamp": 1584694286.0031},
       "BODY": {"space_id": 272, "tuple": ["version", 2, 3, 1]}
     }

    :returns: (2, 3, 1)
    """
    BOX_SCHEMA_ID = 272
    for row in xlog_rows(snapshot_path):
        if row['HEADER']['type'] == 'INSERT' and \
           row['BODY']['space_id'] == BOX_SCHEMA_ID:
            res = row['BODY']['tuple']
            if res[0] == 'version':
                return tuple(res[1:])
    return None


def assert_bytes(b):
    """ Ensure given value is <bytes>.
    """
    if type(b) is not bytes:
        raise ValueError('Internal error: expected {}, got {}: {}'.format(
            str(bytes), str(type(b)), repr(b)))


def assert_str(s):
    """ Ensure given value is <str>.
    """
    if type(s) is not str:
        raise ValueError('Internal error: expected {}, got {}: {}'.format(
            str(str), str(type(s)), repr(s)))


def bytes_to_str(b):
    """ Convert <bytes> to <str>.

        No-op on Python 2.
    """
    assert_bytes(b)
    if PY2:
        return b
    return b.decode('utf-8')


def str_to_bytes(s):
    """ Convert <str> to <bytes>.

        No-op on Python 2.
    """
    assert_str(s)
    if PY2:
        return s
    return s.encode('utf-8')


def parse_tag_line(line):
    tags_str = line.split(':', 1)[1].strip()
    return [tag.strip() for tag in tags_str.split(',')]


def find_tags(filename):
    """ Extract tags from a first comment in the file.
    """
    # TODO: Support multiline comments. See exclude_tests() in
    # lib/server.py.
    if filename.endswith('.lua') or filename.endswith('.sql'):
        singleline_comment = '--'
    elif filename.endswith('.py'):
        singleline_comment = '#'
    else:
        return []

    tags = []
    with open(filename, 'r') as f:
        for line in f:
            line = line.rstrip('\n')
            if line.startswith('#!'):
                pass
            elif line == '':
                pass
            elif line.startswith(singleline_comment + ' tags:'):
                tags.extend(parse_tag_line(line))
            elif line.startswith(singleline_comment):
                pass
            else:
                break
    return tags


def prepend_path(p):
    """ Add an absolute path into PATH (at start) if it is not already there.
    """
    p = os.path.abspath(p)
    if p in os.environ['PATH'].split(os.pathsep):
        return
    os.environ['PATH'] = os.pathsep.join((p, os.environ['PATH']))


def shlex_quote(s):
    return _shlex_quote(s)


def terminal_columns():
    if get_terminal_size:
        return get_terminal_size().columns
    return 80


def cpu_count():
    """
    Return available CPU count available for the current process.

    The result is the same as one from the `nproc` command.

    It may be smaller than all the online CPUs count. For example,
    an LXD container may have limited available CPUs or it may be
    reduced by `taskset` or `numactl` commands.

    If it is impossible to determine the available CPUs count (for
    example on Python < 3.3), fallback to the all online CPUs
    count.
    """
    if sched_getaffinity:
        return len(sched_getaffinity(0))
    return multiprocessing.cpu_count()
