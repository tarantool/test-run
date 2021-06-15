#!/usr/bin/env tarantool

local workdir = os.getenv('TARANTOOL_WORKDIR')
local listen = os.getenv('TARANTOOL_LISTEN')


box.cfg({
    work_dir = workdir,
    listen = listen
})

box.schema.user.grant('guest', 'read,write,execute', 'universe')
