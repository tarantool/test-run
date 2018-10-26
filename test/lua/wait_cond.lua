local tap = require('tap')
local tap_wait_cond = tap.test('wait_cond')
local os = require('os')
os.setenv('INSPECTOR', '99')
local test_run = require('test_run').new()

tap_wait_cond:plan(20)

tap_wait_cond:is(true, test_run:wait_cond(function() return true end, 0.1), 'true')
tap_wait_cond:is(nil, test_run:wait_cond(function() return nil end, 0.1), 'nil')
tap_wait_cond:is(false, test_run:wait_cond(function() return false end, 0.1), 'false')
tap_wait_cond:is(0, test_run:wait_cond(function() return 0 end, 0.1), '0')
tap_wait_cond:is(42, test_run:wait_cond(function() return 42 end, 0.1), '42')

tap_wait_cond:is_deeply({true, true}, {test_run:wait_cond(function() local res = true return res ~= nil, res end, 0.1)}, 'res true')
tap_wait_cond:is_deeply({true, nil}, {test_run:wait_cond(function() local res = nil return res == nil, res end, 0.1)}, 'res nil')
tap_wait_cond:is_deeply({true, false}, {test_run:wait_cond(function() local res = false return res == false, res end, 0.1)}, 'res false')
tap_wait_cond:is_deeply({true, 0}, {test_run:wait_cond(function() local res = 0 return res < 1, res end, 0.1)}, 'res 0')
tap_wait_cond:is_deeply({true, 42}, {test_run:wait_cond(function() local res = 42 return res > 1, res end, 0.1)}, 'res 42')

tap_wait_cond:is_deeply({nil, true}, {test_run:wait_cond(function() local res = true return nil, res end, 0.1)}, 'res true')
tap_wait_cond:is_deeply({nil, nil}, {test_run:wait_cond(function() local res = nil return nil, res end, 0.1)}, 'res nil')
tap_wait_cond:is_deeply({nil, false}, {test_run:wait_cond(function() local res = false return nil, res end, 0.1)}, 'res false')
tap_wait_cond:is_deeply({nil, 0}, {test_run:wait_cond(function() local res = 0 return nil, res end, 0.1)}, 'res 0')
tap_wait_cond:is_deeply({nil, 42}, {test_run:wait_cond(function() local res = 42 return nil, res end, 0.1)}, 'res 42')

tap_wait_cond:is_deeply({false, true}, {test_run:wait_cond(function() local res = true return not res, res end, 0.1)}, 'timeout true')
tap_wait_cond:is_deeply({false, nil}, {test_run:wait_cond(function() local res = nil return res ~= nil, res end, 0.1)}, 'timeout nil')
tap_wait_cond:is_deeply({false, false}, {test_run:wait_cond(function() local res = false return res ~= false, res end, 0.1)}, 'timeout false')
tap_wait_cond:is_deeply({false, 0}, {test_run:wait_cond(function() local res = 0 return res ~= 0, res end, 0.1)}, 'timeout 0')
tap_wait_cond:is_deeply({false, 42}, {test_run:wait_cond(function() local res = 42 return res < 1, res end, 0.1)}, 'timeout 42')

tap_wait_cond:check()
