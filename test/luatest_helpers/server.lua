local clock = require('clock')
local digest = require('digest')
local ffi = require('ffi')
local fiber = require('fiber')
local fio = require('fio')
local fun = require('fun')
local json = require('json')

local checks = require('checks')
local luatest = require('luatest')

ffi.cdef([[
    int kill(pid_t pid, int sig);
]])

local Server = luatest.Server:inherit({})

local WAIT_TIMEOUT = 60
local WAIT_DELAY = 0.1

-- Differences from luatest.Server:
--
-- * 'alias' is mandatory.
-- * 'command' is optional, assumed test/instances/default.lua by
--   default.
-- * 'workdir' is optional, determined by 'alias'.
-- * The new 'box_cfg' parameter.
-- * engine - provides engine for parameterized tests
Server.constructor_checks = fun.chain(Server.constructor_checks, {
    alias = 'string',
    command = '?string',
    workdir = '?string',
    box_cfg = '?table',
    engine = '?string',
}):tomap()

Server.socketdir = fio.abspath(os.getenv('VARDIR') or 'test/var')

function Server.build_instance_uri(alias)
    return ('%s/%s.iproto'):format(Server.socketdir, alias)
end

function Server:initialize()
    if self.id == nil then
        local random = digest.urandom(9)
        self.id = digest.base64_encode(random, {urlsafe = true})
    end
    if self.command == nil then
        self.command = 'test/instances/default.lua'
    end
    if self.workdir == nil then
        self.workdir = ('%s/%s-%s'):format(self.socketdir, self.alias, self.id)
        fio.rmtree(self.workdir)
        fio.mktree(self.workdir)
    end
    if self.net_box_port == nil and self.net_box_uri == nil then
        self.net_box_uri = self.build_instance_uri(self.alias)
        fio.mktree(self.socketdir)
    end

    -- AFAIU, the inner getmetatable() returns our helpers.Server
    -- class, the outer one returns luatest.Server class.
    getmetatable(getmetatable(self)).initialize(self)
end

--- Generates environment to run process with.
-- The result is merged into os.environ().
-- @return map
function Server:build_env()
    local res = getmetatable(getmetatable(self)).build_env(self)
    if self.box_cfg ~= nil then
        res.TARANTOOL_BOX_CFG = json.encode(self.box_cfg)
    end
    res.TARANTOOL_ENGINE = self.engine
    return res
end

function Server:wait_for_readiness()
    local alias = self.alias
    local id = self.id
    local pid = self.process.pid

    local deadline = clock.time() + WAIT_TIMEOUT
    while true do
        local ok, is_ready = pcall(function()
            self:connect_net_box()
            return self.net_box:eval('return _G.ready') == true
        end)
        if ok and is_ready then
            break
        end
        if clock.time() > deadline then
            error(('Starting of server %s-%s (PID %d) was timed out'):format(
                alias, id, pid))
        end
        fiber.sleep(WAIT_DELAY)
    end
end

-- Unlike the original luatest.Server function it waits for
-- starting the server.
function Server:start(opts)
    checks('table', {
        wait_for_readiness = '?boolean',
    })
    getmetatable(getmetatable(self)).start(self)

    -- The option is true by default.
    local wait_for_readiness = true
    if opts ~= nil and opts.wait_for_readiness ~= nil then
        wait_for_readiness = opts.wait_for_readiness
    end

    if wait_for_readiness then
        self:wait_for_readiness()
    end
end

-- TODO: Add the 'wait_for_readiness' parameter for the restart()
-- method.

-- Unlike the original luatest.Server function it waits until
-- the server will stop.
function Server:stop()
    local alias = self.alias
    local id = self.id
    if self.process then
        local pid = self.process.pid
        getmetatable(getmetatable(self)).stop(self)

        local deadline = clock.time() + WAIT_TIMEOUT
        while true do
            if ffi.C.kill(pid, 0) ~= 0 then
                break
            end
            if clock.time() > deadline then
                error(('Stopping of server %s-%s (PID %d) was timed out'):format(
                    alias, id, pid))
            end
            fiber.sleep(WAIT_DELAY)
        end
    end
end

return Server
