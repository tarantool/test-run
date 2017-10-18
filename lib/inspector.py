import os
import yaml
import traceback

import gevent
from gevent.lock import Semaphore
from gevent.server import StreamServer

from lib.utils import find_port
from lib.colorer import color_stdout

from lib.tarantool_server import TarantoolStartError


# Module initialization
#######################


def gevent_propagate_exc():
    """Don't print backtraces and propagate the exception to the parent
    greenlet when Ctrl+C or startup fail hit the process when the active
    greenlet is one of the StreamServer owned.
    """
    ghub = gevent.get_hub()
    for exc_t in [KeyboardInterrupt, TarantoolStartError]:
        if exc_t not in ghub.NOT_ERROR:
            ghub.NOT_ERROR = ghub.NOT_ERROR + (exc_t,)
        if exc_t not in ghub.SYSTEM_ERROR:
            ghub.SYSTEM_ERROR = ghub.SYSTEM_ERROR + (exc_t,)


gevent_propagate_exc()


# TarantoolInspector
####################


class TarantoolInspector(StreamServer):
    """
    Tarantool inspector daemon. Usage:
    inspector = TarantoolInspector('localhost', 8080)
    inspector.start()
    # run some tests
    inspector.stop()
    """

    def __init__(self, host, port):
        # When specific port range was acquired for current worker, don't allow
        # OS set port for us that isn't from specified range.
        if port == 0:
            port = find_port()
        super(TarantoolInspector, self).__init__((host, port))
        self.parser = None

    def start(self):
        super(TarantoolInspector, self).start()
        os.environ['INSPECTOR'] = str(self.server_port)

    def stop(self):
        del os.environ['INSPECTOR']

    def set_parser(self, parser):
        self.parser = parser
        self.sem = Semaphore()

    @staticmethod
    def readline(socket, delimiter='\n', size=4096):
        result = ''
        data = True

        while data:
            try:
                data = socket.recv(size)
            except IOError:
                # catch instance halt connection refused errors
                data = ''
            result += data

            while result.find(delimiter) != -1:
                line, result = result.split(delimiter, 1)
                yield line
        return

    def handle(self, socket, addr):
        if self.parser is None:
            raise AttributeError('Parser is not defined')
        self.sem.acquire()

        for line in self.readline(socket):
            try:
                result = self.parser.parse_preprocessor(line)
            except (KeyboardInterrupt, TarantoolStartError):
                # propagate to the main greenlet
                raise
            except Exception as e:
                color_stdout('\nTarantoolInpector.handle() received the following error:\n' +
                    traceback.format_exc() + '\n', schema='error')
                result = { "error": repr(e) }
                self.parser.kill_current_test()
            if result == None:
                result = True
            result = yaml.dump(result)
            if not result.endswith('...\n'):
                result = result + '...\n'
            socket.sendall(result)

        self.sem.release()

    def cleanup_nondefault(self):
        if self.parser:
            self.parser.cleanup_nondefault()
