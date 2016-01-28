import gevent
import shlex
import json
import os

from gevent.server import StreamServer
from gevent.lock import Semaphore

class TarantoolInspector(StreamServer):
    """
    Tarantool inspector daemon. Usage:
    inspector = TarantoolInspector('localhost', 8080)
    inspector.start()
    # run some tests
    inspector.stop()
    """

    def __init__(self, host, port):
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
            except Exception, e:
                result = str(e)
            if not result:
                result = 'OK'
            elif type(result) == dict:
                result = json.dumps(result)
            else:
                result = str(result)
            socket.sendall('%s\n' % result)

        self.sem.release()

