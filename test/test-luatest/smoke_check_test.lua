local t = require('luatest')

local server = require('test.luatest_helpers.server')

local g = t.group()

g.before_all = function()
    g.server = server:new({
        alias = 'my_server',
        env = {MY_ENV_VAR = 'test_value'},
        box_cfg = {memtx_memory = 100 * 1024 ^ 2},
    })
    g.server:start()
end

g.after_all = function()
    g.server:stop()
end

g.test_server_is_started_and_operable = function()
    local res = g.server:eval('return 42')
    t.assert_equals(res, 42)
end

g.test_database_is_bootstrapped_and_accessible = function()
    local res = g.server:exec(function() return box.info.status end)
    t.assert_equals(res, 'running')
end

g.test_environment_variable_is_passed = function()
    local res = g.server:exec(function() return os.getenv('MY_ENV_VAR') end)
    t.assert_equals(res, 'test_value')
end

g.test_box_cfg_values_are_passed = function()
    local res = g.server:exec(function() return box.cfg.memtx_memory end)
    t.assert_equals(res, 100 * 1024 ^ 2)
end
