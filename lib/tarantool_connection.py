__author__ = "Konstantin Osipov <kostja.osipov@gmail.com>"

# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY AUTHOR AND CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.

import ctypes
import errno
import re
import socket
from contextlib import contextmanager

import gevent
from gevent import socket as gsocket

from connpool import ConnectionPool
from test import TestRunGreenlet
from utils import warn_unix_socket
from utils import set_fd_cloexec


class TarantoolPool(ConnectionPool):
    def __init__(self, host, port, *args, **kwargs):
        self.host = host
        self.port = port
        super(TarantoolPool, self).__init__(*args, **kwargs)

    def _new_connection(self):
        result = None
        # https://github.com/tarantool/tarantool/issues/3806
        # We should set FD_CLOEXEC before connect(), because connect() is
        # blocking operation and with gevent it can wakeup another greenlet,
        # including one in which we do Popen. When FD_CLOEXEC was set after
        # connect() we observed socket file descriptors leaking into tarantool
        # server in case of unix socket. It was not observed in case of tcp
        # sockets for unknown reason, so now we leave setting FD_CLOEXEC after
        # connect for tcp sockets and fix it only for unix sockets.
        if self.host == 'unix/' or re.search(r'^/', str(self.port)):
            warn_unix_socket(self.port)
            result = gsocket.socket(gsocket.AF_UNIX, gsocket.SOCK_STREAM)
            set_fd_cloexec(result.fileno())
            result.connect(self.port)
        else:
            result = gsocket.create_connection((self.host, self.port))
            result.setsockopt(gsocket.SOL_TCP, gsocket.TCP_NODELAY, 1)
            set_fd_cloexec(result.fileno())
        return result

    def _addOne(self):
        stime = 0.1
        while True:
            try:
                c = self._new_connection()
            except gsocket.error:
                c = None
            if c:
                break
            gevent.sleep(stime)
            if stime < 400:
                stime *= 2
        self.conn.append(c)
        self.lock.release()

    @contextmanager
    def get(self):
        self.lock.acquire()

        try:
            c = self.conn.pop()
            yield c
        except self.exc_classes:
            greenlet = TestRunGreenlet(self._addOne)
            greenlet.start_later(1)
            raise
        except:  # noqa: E722
            self.conn.append(c)
            self.lock.release()
            raise
        else:
            self.conn.append(c)
            self.lock.release()

    def close_all(self):
        self.conn.clear()


class TarantoolConnection(object):
    @property
    def uri(self):
        if self.host == 'unix/' or re.search(r'^/', str(self.port)):
            return self.port
        else:
            return self.host+':'+str(self.port)

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.is_connected = False
        if self.host == 'unix/' or re.search(r'^/', str(self.port)):
            warn_unix_socket(self.port)

    def connect(self):
        # See comment in TarantoolPool._new_connection().
        if self.host == 'unix/' or re.search(r'^/', str(self.port)):
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            set_fd_cloexec(self.socket.fileno())
            self.socket.connect(self.port)
        else:
            self.socket = socket.create_connection((self.host, self.port))
            self.socket.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
            set_fd_cloexec(self.socket.fileno())
        self.is_connected = True

    def disconnect(self):
        if self.is_connected:
            self.socket.close()
            self.is_connected = False

    def reconnect(self):
        self.disconnect()
        self.connect()

    def opt_reconnect(self):
        """ On a socket which was disconnected, recv of 0 bytes immediately
            returns with no data. On a socket which is alive, it returns
            EAGAIN. Make use of this property and detect whether or not the
            socket is dead. Reconnect a dead socket, do nothing if the socket
            is good.
        """
        try:
            if not self.is_connected or self.socket.recv(
                    1, socket.MSG_DONTWAIT | socket.MSG_PEEK) == '':
                self.reconnect()
        except socket.error as e:
            if e.errno == errno.EAGAIN:
                pass
            else:
                self.reconnect()

    def clone(self):
        return type(self)(self.host, self.port)

    def execute(self, command, silent=True):
        self.opt_reconnect()
        return self.execute_no_reconnect(command, silent)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, type, value, tb):
        self.disconnect()

    def __call__(self, command, silent=False, simple=False):
        return self.execute(command, silent)


class TarantoolAsyncConnection(TarantoolConnection):
    pool = TarantoolPool

    def __init__(self, host, port):
        super(TarantoolAsyncConnection, self).__init__(host, port)
        self.connections = None
        libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
        self._sys_recv = libc.recv

    @property
    def socket(self):
        with self.connections.get() as c:
            result = c
        return result

    def connect(self):
        self.connections = self.pool(self.host, self.port, 3)
        self.is_connected = True

    def disconnect(self):
        if self.is_connected:
            self.connections.close_all()
            self.is_connected = False

    def execute(self, command, silent=True):
        return self.execute_no_reconnect(command, silent)
