import os
import sys
import collections
from gevent import socket


from lib.colorer import Colorer
color_stdout = Colorer()

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
            color_stdout("\n\nNo %s library found\n" % mod_name, schema='error')
            print(e)
            sys.exit(1)

def check_valgrind_log(path_to_log):
    """ Check that there were no warnings in the log."""
    return os.path.exists(path_to_log) and os.path.getsize(path_to_log) != 0

def print_tail_n(filename, num_lines):
    """Print N last lines of a file."""
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

def find_port(port):
    while port < 65536:
        if check_port(port, False):
            return port
        port += 1
    return find_port(34000)


