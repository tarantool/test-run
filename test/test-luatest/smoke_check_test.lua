local t = require('luatest')
local g = t.group()

local Server = t.Server
local fio = require('fio')

local root = os.environ()['SOURCEDIR']
local datadir = fio.pathjoin(root, debug.getinfo(1).short_src)
local command = fio.pathjoin(root, 'test', 'test-luatest', 'server_instance.lua')

local server = Server:new({
    command = command,
    workdir = fio.pathjoin(datadir, 'common'),
    env = {custom_env = 'test_value'},
    net_box_port = 3133,
})

g.before_all = function()
    fio.rmtree(datadir)
    fio.mktree(server.workdir)
    server:start()
end

g.after_all = function()
    if server.process then
        server:stop()
    end
    fio.rmtree(datadir)
end

g.test_smoke = function()
    t.helpers.retrying({timeout = 5}, function()
        server:connect_net_box()
        t.assert_equals(server.net_box.state, 'active')
    end)
end
