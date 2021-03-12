-- regression test for the problem fixed in [1] that was a part
-- of more common issue [2]. It's worth to mention that original
-- problem has more chances to reproduce with applied patch to
-- multiprocessing source code (multiprocessing/connection.py).
--
-- 1. https://github.com/tarantool/test-run/pull/275
-- 2. https://github.com/tarantool/tarantool-qa/issues/96

-- Setup
box.schema.user.grant('guest', 'replication')

-- Setup and teardown cluster, manage separate instances.
test_run = require('test_run').new()
test_run:cmd('create server replica with rpl_master=default, script="test-tarantool/replica.lua"')
test_run:cmd('start server replica')
test_run:cmd('stop server replica')
test_run:cmd('cleanup server replica')
test_run:cmd('delete server replica')

-- Teardown
box.schema.user.revoke('guest', 'replication')
