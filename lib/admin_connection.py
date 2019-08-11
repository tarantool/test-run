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

import re
import sys

from tarantool_connection import TarantoolConnection
from tarantool_connection import TarantoolPool
from tarantool_connection import TarantoolAsyncConnection


ADMIN_SEPARATOR = '\n'


def get_handshake(sock, length=128, max_try=100):
    """
    Correct way to get tarantool handshake
    """
    result = ""
    i = 0
    while len(result) != length and i < max_try:
        result = "%s%s" % (result, sock.recv(length-len(result)))
        # max_try counter for tarantool/gh-1362
        i += 1
    return result


class AdminPool(TarantoolPool):
    def _new_connection(self):
        s = super(AdminPool, self)._new_connection()
        handshake = get_handshake(s)
        if handshake and not re.search(r'^Tarantool.*console.*',
                                       str(handshake)):
            # tarantool/gh-1163
            # 1. raise only if handshake is not full
            # 2. be silent on crashes or if it's server.stop() operation
            print 'Handshake error {\n', handshake, '\n}'
            raise RuntimeError('Broken tarantool console handshake')
        return s


class ExecMixIn(object):
    def cmd(self, socket, cmd, silent):
        socket.sendall(cmd)

        bufsiz = 4096
        res = ""
        while True:
            buf = socket.recv(bufsiz)
            if not buf:
                break
            res = res + buf
            if (res.rfind("\n...\n") >= 0 or res.rfind("\r\n...\r\n") >= 0):
                break

        if not silent:
            sys.stdout.write(res.replace("\r\n", "\n"))
        return res


class AdminConnection(TarantoolConnection, ExecMixIn):
    def execute_no_reconnect(self, command, silent):
        if not command:
            return
        if not silent:
            sys.stdout.write(command + ADMIN_SEPARATOR)
        cmd = command.replace('\n', ' ') + ADMIN_SEPARATOR
        return self.cmd(self.socket, cmd, silent)

    def connect(self):
        super(AdminConnection, self).connect()
        handshake = get_handshake(self.socket)
        if not re.search(r'^Tarantool.*console.*', str(handshake)):
            raise RuntimeError('Broken tarantool console handshake')


class AdminAsyncConnection(TarantoolAsyncConnection, ExecMixIn):
    pool = AdminPool

    def execute_no_reconnect(self, command, silent):
        if not command:
            return
        if not silent:
            sys.stdout.write(command + ADMIN_SEPARATOR)
        cmd = command.replace('\n', ' ') + ADMIN_SEPARATOR

        result = None
        with self.connections.get() as sock:
            result = self.cmd(sock, cmd, silent)
        return result

    def execute(self, command, silent=True):
        if not self.is_connected:
            self.connect()
        try:
            return self.execute_no_reconnect(command, silent)
        except Exception:
            return None
