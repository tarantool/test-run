local t = require('luatest')
local g = t.group()

local luatest_helpers = require('test.luatest_helpers')

g.before_all = function()
    g.server = luatest_helpers.Server:new({
        alias = 'my_server',
        env = {MY_ENV_VAR = 'test_value'},
        box_cfg = {memtx_memory = 100 * 1024 ^ 2},
    })
    g.server:start()
end

g.after_all = function()
    g.server:stop()
end

g.test_smoke = function()
    -- The server is started and operable.
    local res = g.server.net_box:eval('return 42')
    t.assert_equals(res, 42)

    -- The database is bootstrapped and accessible.
    local res = g.server.net_box:eval('return box.info.status')
    t.assert_equals(res, 'running')

    -- The environment variable is passed.
    local res = g.server.net_box:eval('return os.getenv("MY_ENV_VAR")')
    t.assert_equals(res, 'test_value')

    -- The box.cfg() values are passed as well.
    local res = g.server.net_box:eval('return box.cfg.memtx_memory')
    t.assert_equals(res, 100 * 1024 ^ 2)
end
