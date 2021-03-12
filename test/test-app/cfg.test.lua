#!/usr/bin/env tarantool

-- Test is an example of TAP test

local tap = require('tap')
local test = tap.test('cfg')
test:plan(4)

box.cfg{listen = box.NULL}
test:is(nil, box.info.listen, 'no cfg.listen - no info.listen')

box.cfg{listen = '127.0.0.1:0'}
test:ok(box.info.listen:match('127.0.0.1'), 'real IP in info.listen')
test:ok(not box.info.listen:match(':0'), 'real port in info.listen')

box.cfg{listen = box.NULL}
test:is(nil, box.info.listen, 'cfg.listen reset drops info.listen')

os.exit(test:check() and 0 or 1)
