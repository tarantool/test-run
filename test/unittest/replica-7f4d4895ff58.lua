#!/usr/bin/env tarantool

box.cfg {
    listen = os.getenv('LISTEN'),
    replication = os.getenv('MASTER'),
}

require('console').listen(os.getenv('ADMIN'))
