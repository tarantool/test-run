local fio = require('fio')

local t = require('luatest')
local g = t.group()

local Process = t.Process
local Server = t.Server

local root = fio.dirname(fio.dirname(fio.abspath(package.search('test.helper')))) -- luacheck: ignore
local datadir = fio.pathjoin(root, 'tmp', 'db_test')
local command = fio.pathjoin(root, 'test', 'server_instance.lua')

local server = Server:new({
    command = command,
    workdir = fio.pathjoin(datadir, 'common'),
    env = {custom_env = 'test_value'},
    http_port = 8182,
    net_box_port = 3133,
})

g.before_all = function()
    fio.rmtree(datadir)
    fio.mktree(server.workdir)
    server:start()
    -- wait until booted
    t.helpers.retrying({timeout = 2}, function() server:http_request('get', '/ping') end)
end

g.after_all = function()
    if server.process then
        server:stop()
    end
    fio.rmtree(datadir)
end

g.test_start_stop = function()
    local workdir = fio.pathjoin(datadir, 'start_stop')
    fio.mktree(workdir)
    local s = Server:new({command = command, workdir = workdir})
    local orig_args = table.copy(s.args)
    s:start()
    local pid = s.process.pid
    t.helpers.retrying({timeout = 0.5}, function()
        t.assert(Process.is_pid_alive(pid))
    end)
    s:stop()
    t.helpers.retrying({timeout = 0.5}, function()
        t.assert_not(Process.is_pid_alive(pid))
    end)
    t.assert_equals(s.args, orig_args)
end
