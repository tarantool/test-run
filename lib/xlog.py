"""Xlog and snapshot utility functions."""

import os
import msgpack
import subprocess
import json
from uuid import uuid4


__all__ = ['init', 'snapshot_is_for_bootstrap', 'prepare_bootstrap_snapshot',
           'extract_schema_from_snapshot']


# {{{ Constants

# Xlog binary format constants.
ROW_MARKER = b'\xd5\xba\x0b\xab'
EOF_MARKER = b'\xd5\x10\xad\xed'
XLOG_FIXHEADER_SIZE = 19
VCLOCK_MAX = 32
VCLOCK_STR_LEN_MAX = 1 + VCLOCK_MAX * (2 + 2 + 20 + 2) + 1
XLOG_META_LEN_MAX = 1024 + VCLOCK_STR_LEN_MAX


# The binary protocol (iproto) constants.
#
# Widely reused for xlog / snapshot files.
IPROTO_REQUEST_TYPE = 0x00
IPROTO_LSN = 0x03
IPROTO_TIMESTAMP = 0x04
IPROTO_INSERT = 2
IPROTO_SPACE_ID = 0x10
IPROTO_TUPLE = 0x21


# System space IDs.
BOX_SCHEMA_ID = 272
BOX_CLUSTER_ID = 320

# }}} Constants


tarantool_cmd = 'tarantool'
tarantoolctl_cmd = 'tarantoolctl'
debug_f = lambda x: None  # noqa: E731


def init(tarantool=None, tarantoolctl=None, debug=None):
    """ Redefine module level globals.

        If the function is not called, tarantool and tarantoolctl
        will be called according to the PATH environment variable.

        Beware: tarantool and tarantoolctl are lists. Example:

        tarantool_cmd = ['/path/to/tarantool']
        tarantoolctl_cmd = tarantool_cmd + ['/path/to/tarantoolctl']
        xlog.init(tarantool=tarantool_cmd, tarantoolctl=tarantoolctl_cmd)
    """
    global tarantool_cmd
    global tarantoolctl_cmd
    global debug_f

    if tarantool:
        assert isinstance(tarantool, list)
        tarantool_cmd = tarantool
    if tarantool_cmd:
        assert isinstance(tarantoolctl, list)
        tarantoolctl_cmd = tarantoolctl
    if debug:
        debug_f = debug


# {{{ General purpose helpers

def crc32c(data):
    """ Use tarantool's implementation of CRC32C algorithm.

        Python has no built-in implementation of CRC32C.
    """
    lua = "print(require('digest').crc32_update(0, io.stdin:read({})))".format(
        len(data))
    with open(os.devnull, 'w') as devnull:
        process = subprocess.Popen(tarantool_cmd + ['-e', lua],
                                   stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE,
                                   stderr=devnull)
    process.stdin.write(data)
    res, _ = process.communicate()
    return int(res)

# }}} General purpose helpers


# {{{ parse xlog / snapshot

def xlog_rows(xlog_path):
    cmd = tarantoolctl_cmd + ['cat', xlog_path, '--format=json',
                              '--show-system']
    with open(os.devnull, 'w') as devnull:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=devnull)
    for line in process.stdout.readlines():
        yield json.loads(line)

# }}} parse xlog / snapshot


# {{{ xlog encode data helpers

def encode_xrow_header(xrow):
    packer = msgpack.Packer(use_bin_type=False)
    xrow_header = ROW_MARKER
    # xrow size
    xrow_header += packer.pack(len(xrow))
    # previous xrow crc32
    xrow_header += packer.pack(0)
    # current xrow crc32
    xrow_header += packer.pack(crc32c(xrow))
    # padding
    padding_size = XLOG_FIXHEADER_SIZE - len(xrow_header)
    xrow_header += packer.pack(b'\x00' * (padding_size - 1))
    assert(len(xrow_header) == XLOG_FIXHEADER_SIZE)
    return xrow_header


def encode_xrow(header, body):
    packer = msgpack.Packer(use_bin_type=False)
    header = packer.pack(header)
    body = packer.pack(body)
    xrow_data = header + body
    return encode_xrow_header(xrow_data) + xrow_data

# }}} xlog encode data helpers


# {{{ xlog write data helpers

def xlog_seek_end(xlog):
    """Set the file position right before the end marker."""
    WHENCE_END = 2
    xlog.seek(-4, WHENCE_END)
    eof_marker = xlog.read(4)
    if eof_marker != EOF_MARKER:
        raise RuntimeError('Invalid eof marker: {}'.format(eof_marker))
    xlog.seek(-4, WHENCE_END)


def xlog_write_eof(xlog):
    xlog.write(EOF_MARKER)

# }}} xlog write data helpers


# {{{ xlog write meta helpers

def xlog_meta_write_instance_uuid(xlog, instance_uuid):
    xlog.seek(0)
    xlog.seek(xlog.read(XLOG_META_LEN_MAX).find(b'Instance: '))
    # Trick: old and new UUID have the same size.
    xlog.write(b'Instance: ' + instance_uuid)

# }}} xlog write meta helpers


def snapshot_is_for_bootstrap(snapshot_path):
    """ A bootstrap snapshot (src/box/bootstrap.snap) has no
        <cluster_uuid> and <instance_uuid> in _schema and
        _cluster system spaces.
    """
    cluster_uuid_exists = False
    instance_uuid_exists = False

    for row in xlog_rows(snapshot_path):
        if row['HEADER']['type'] == 'INSERT' and         \
           row['BODY']['space_id'] == BOX_SCHEMA_ID and \
           row['BODY']['tuple'][0] == 'cluster':
            cluster_uuid_exists = True

        if row['HEADER']['type'] == 'INSERT' and          \
           row['BODY']['space_id'] == BOX_CLUSTER_ID and \
           row['BODY']['tuple'][0] == 1:
            instance_uuid_exists = True

        if cluster_uuid_exists and instance_uuid_exists:
            break

    if cluster_uuid_exists != instance_uuid_exists:
        raise RuntimeError('A cluster UUID and an instance UUID should exist '
                           'or not exist both')

    return not cluster_uuid_exists


def prepare_bootstrap_snapshot(snapshot_path):
    """ Prepare a bootstrap snapshot to use with local recovery."""
    cluster_uuid = str(uuid4()).encode('ascii')
    debug_f('Cluster UUID: {}'.format(cluster_uuid))
    instance_uuid = str(uuid4()).encode('ascii')
    instance_id = 1
    debug_f('Instance ID: {}'.format(instance_id))
    debug_f('Instance UUID: {}'.format(instance_uuid))

    last_row = list(xlog_rows(snapshot_path))[-1]
    lsn = int(last_row['HEADER']['lsn'])
    timestamp = float(last_row['HEADER']['timestamp'])

    with open(snapshot_path, 'rb+') as xlog:
        xlog_meta_write_instance_uuid(xlog, instance_uuid)
        xlog_seek_end(xlog)

        # Write cluster UUID to _schema.
        lsn += 1
        xlog.write(encode_xrow({
            IPROTO_REQUEST_TYPE: IPROTO_INSERT,
            IPROTO_LSN: lsn,
            IPROTO_TIMESTAMP: timestamp,
        }, {
            IPROTO_SPACE_ID: BOX_SCHEMA_ID,
            IPROTO_TUPLE: ['cluster', cluster_uuid],
        }))

        # Write replica ID and replica UUID to _cluster.
        lsn += 1
        xlog.write(encode_xrow({
            IPROTO_REQUEST_TYPE: IPROTO_INSERT,
            IPROTO_LSN: lsn,
            IPROTO_TIMESTAMP: timestamp,
        }, {
            IPROTO_SPACE_ID: BOX_CLUSTER_ID,
            IPROTO_TUPLE: [1, instance_uuid],
        }))

        xlog_write_eof(xlog)


def extract_schema_from_snapshot(snapshot_path):
    """
    Extract schema version from snapshot.

    Example of record:

     {
       "HEADER": {"lsn":2, "type": "INSERT", "timestamp": 1584694286.0031},
       "BODY": {"space_id": 272, "tuple": ["version", 2, 3, 1]}
     }

    :returns: [u'version', 2, 3, 1]
    """
    for row in xlog_rows(snapshot_path):
        if row['HEADER']['type'] == 'INSERT' and \
           row['BODY']['space_id'] == BOX_SCHEMA_ID:
            res = row['BODY']['tuple']
            if res[0] == 'version':
                return res
    return None
