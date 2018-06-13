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
import os
import sys
import errno
import ctypes
import socket
import struct
import warnings

from tarantool_connection import TarantoolConnection

# monkey patch tarantool and msgpack
from lib.utils import check_libs

check_libs()

from tarantool import Connection as tnt_connection
from tarantool import Schema

SEPARATOR = '\n'


class BoxConnection(TarantoolConnection):
    def __init__(self, host, port):
        super(BoxConnection, self).__init__(host, port)
        self.py_con = tnt_connection(host, port, connect_now=False, socket_timeout=100)
        self.py_con.error = False
        self.sort = False

    def connect(self):
        self.py_con.connect()

    def authenticate(self, user, password):
        self.py_con.authenticate(user, password)

    def disconnect(self):
        self.py_con.close()

    def reconnect(self):
        if self.py_con.connected:
            self.disconnect()
        self.connect()

    def set_schema(self, schemadict):
        self.py_con.schema = Schema(schemadict)

    def check_connection(self):
        rc = self.py_con._sys_recv(
            self.py_con._socket.fileno(), '  ', 1,
            socket.MSG_DONTWAIT | socket.MSG_PEEK
        )
        if ctypes.get_errno() == errno.EAGAIN:
            ctypes.set_errno(0)
            return True
        return False

    def execute_no_reconnect(self, command, silent=True):
        if not command:
            return
        if not silent:
            print
            command
        cmd = command.replace(SEPARATOR, ' ') + SEPARATOR
        response = self.py_con.call(cmd)
        result = str(response)
        if not silent:
            print
            response
        return response

    def execute(self, command, silent=True):
        return self.execute_no_reconnect(command, silent)

    def call(self, command, *args):
        if not command:
            return
        print
        'call ', command, args
        response = self.py_con.call(command, *args)
        result = str(response)
        print
        result
        return result
