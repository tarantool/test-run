"""
Original Tarantool's test box-py/iproto.test.py had a problem when output with
running under Python 2 was not the same as with running under Python 3.

Fixed in commit 697b79781cc63e2d87d86d43713998261d602334
"test: make output of box-py/iproto.test.py deterministic".
"""

from __future__ import print_function

import msgpack
from tarantool.const import *
from tarantool import Connection
from tarantool.response import Response
from lib.tarantool_connection import TarantoolConnection

# Note re IPROTO_SQL_INFO_* keys: they cannot appear in the
# response map at the top level, but have the same codes as other
# IPROTO_* constants. Exclude those names so.
key_names = {}
for (k,v) in list(globals().items()):
    if type(k) == str and k.startswith("IPROTO_") and \
            not k.startswith("IPROTO_SQL_INFO_") and type(v) == int:
        key_names[v] = k

def repr_dict(todump):
    d = {}
    for (k, v) in todump.items():
        k_name = key_names.get(k, k)
        d[k_name] = v
    return repr(sorted(d.items()))


def test(header, body):
    # Connect and authenticate
    c = Connection("localhost", server.iproto.port)
    c.connect()
    print("query", repr_dict(header), repr_dict(body))
    header = msgpack.dumps(header)
    body = msgpack.dumps(body)
    query = msgpack.dumps(len(header) + len(body)) + header + body
    # Send raw request using connected socket
    s = c._socket
    try:
        s.send(query)
    except OSError as e:
        print("   => ", "Failed to send request")
    c.close()
    print(iproto.py_con.ping() > 0)

print("IPROTO_UPDATE")
test({ IPROTO_CODE : REQUEST_TYPE_UPDATE }, { IPROTO_SPACE_ID: 280 })
test({ IPROTO_CODE : REQUEST_TYPE_UPDATE },
     { IPROTO_SPACE_ID: 280, IPROTO_KEY: (1, )})
print("\n")
