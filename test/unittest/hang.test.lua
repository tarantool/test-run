test_run = require('test_run').new()

-- This test should hang: we are unable to bootstrap the replica, because it is
-- unable to join the master because of lack of granting user permissions.
test_run:cmd('create server replica with rpl_master=default, script="unittest/replica-7f4d4895ff58.lua"')
test_run:cmd('start server replica')
