--- Utils provided by test-run.

local socket = require('socket')
local json = require('json')
local yaml = require('yaml')
local log = require('log')
local fiber = require('fiber')
local fio = require('fio')
local errno = require('errno')
local clock = require('clock')

local function cmd(self, msg)
    local sock = self:wait_cond(function()
        return socket.tcp_connect(self.host, self.port)
    end, 100)
    local data = msg .. '\n'
    sock:send(data)

    local result = sock:read('\n...\n')
    sock:close()
    result = yaml.decode(result)
    if type(result) == 'table' and result.error ~= nil then
        error(result.error, 0)
    end
    return result
end

local eval_cmd = 'eval %s "%s"'

local function eval(self, node, expr)
    return self:cmd(eval_cmd:format(node, expr))
end

local get_param_cmd = 'eval %s "return box.info%s"'

local function get_param(self, node, param)
    -- if param is passed then append dot, otherwise make it empty
    param = param and '.' .. param or ''
    return self:cmd(get_param_cmd:format(node, param))
end

local function get_lsn(self, node, sid)
    local nodes = self:get_param(node, 'vclock')
    return tonumber(nodes[1][tonumber(sid)])
end

local function get_server_id(self, node)
    local server = self:get_param(node, "server")[1]
    if server ~= nil then
        -- Tarantool < 1.7.4
        if server.id ~= nil and server.id <= 0 then
            return nil -- bootstrap in progress
        end
        return tonumber(server.id)
    end
    -- Tarantool 1.7.4+
    local server_id = self:get_param(node, "id")[1]
    if server_id == nil then
        return nil -- bootstrap in progress
    end
    return tonumber(server_id)
end

local function wait_lsn(self, waiter, master)
    local sid = self:get_server_id(master)
    local lsn = self:get_lsn(master, sid)

    while self:get_lsn(waiter, sid) < lsn do
        fiber.sleep(0.001)
    end
end

local function get_vclock(self, node)
    return self:get_param(node, 'vclock')[1]
end

local function wait_vclock(self, node, to_vclock)
    while true do
        local vclock = self:get_vclock(node)
        local ok = true
        for server_id, to_lsn in pairs(to_vclock) do
            local lsn = vclock[server_id]
            if lsn == nil or lsn < to_lsn then
                ok = false
                break
            end
        end
        if ok then
            return
        end
        log.info("wait vclock: %s to %s", yaml.encode(vclock),
                 yaml.encode(to_vclock))
        fiber.sleep(0.001)
    end
end


local create_cluster_cmd1 = 'create server %s with script="%s/%s.lua"'
local create_cluster_cmd1_return_listen_uri =
    'create server %s with script="%s/%s.lua", return_listen_uri=True'
local create_cluster_cmd2 = 'start server %s with wait_load=False, wait=False'
local create_cluster_cmd2_args = create_cluster_cmd2 .. ', args="%s"'

local function create_cluster(self, servers, test_suite, opts)
    local opts = opts or {}
    test_suite = test_suite or 'replication'

    local uris = {}

    for _, name in ipairs(servers) do
        if opts.return_listen_uri then
            local cmd1 = create_cluster_cmd1_return_listen_uri
            uris[#uris + 1] = self:cmd(cmd1:format(name, test_suite, name))
        else
            self:cmd(create_cluster_cmd1:format(name, test_suite, name))
        end
    end
    for _, name in ipairs(servers) do
        if opts.args then
            self:cmd(create_cluster_cmd2_args:format(name, opts.args))
        else
            self:cmd(create_cluster_cmd2:format(name))
        end
    end

    if opts.return_listen_uri then
        return uris
    end
end

local drop_cluster_cmd1 = 'stop server %s'
local drop_cluster_cmd2 = 'cleanup server %s'
local drop_cluster_cmd3 = 'delete server %s'

local function drop_cluster(self, servers)
    for _, name in ipairs(servers) do
        -- Don't fail on already stopped servers.
        pcall(self.cmd, self, drop_cluster_cmd1:format(name))
        self:cmd(drop_cluster_cmd2:format(name))
        self:cmd(drop_cluster_cmd3:format(name))
    end
end

local function cleanup_cluster(self)
    local cluster = box.space._cluster:select()
    for _, tuple in pairs(cluster) do
        if tuple[1] ~= box.info.id then
            box.space._cluster:delete(tuple[1])
        end
    end
end

local wait_fullmesh_cmd = 'box.info.replication[%s]'

local function wait_fullmesh(self, servers)
    log.info("starting full mesh")
    for _, server in ipairs(servers) do
        -- wait bootstrap to finish
        log.info("%s: waiting bootstrap", server)
        local server_id
        while true do
            server_id = self:get_server_id(server)
            if server_id ~= nil then
                log.info("%s: bootstrapped", server)
                break
            end
            fiber.sleep(0.01)
        end
        -- wait all for full mesh
        for _, server2 in ipairs(servers) do
            if server ~= server2 then
                log.info("%s -> %s: waiting for connection", server2, server)
                while true do
                    local cmd = wait_fullmesh_cmd:format(server_id)
                    local info = self:eval(server2, cmd)[1]
                    if info ~= nil and info.upstream ~= nil then
                        local status_and_message = json.encode({
                            status = info.upstream.status,
                            message = info.upstream.message,
                        })
                        if info.upstream.status == 'stopped' then
                            log.info("fullmesh failed to connect %s -> %s: %s",
                                     server2, server, status_and_message)
                            return false
                        end
                        log.info("connecting %s -> %s: %s",
                                 server2, server, status_and_message)
                    end
                    if info ~= nil and (info.status == 'follow' or
                                        (info.upstream ~= nil and
                                         info.upstream.status == 'follow')) then
                        log.info("%s -> %s: connected", server2, server)
                        break
                    end
                    fiber.sleep(0.01)
                end
            end
        end
    end
    log.info("full mesh connected")
end

local function get_cluster_vclock(self, servers)
    local vclock = {}
    for _, name in pairs(servers) do
        for server_id, lsn in pairs(self:get_vclock(name)) do
            local prev_lsn = vclock[server_id]
            if prev_lsn == nil or prev_lsn < lsn then
                vclock[server_id] = lsn
            end
        end
    end
    return setmetatable(vclock, { __serialize = 'map' })
end

local function wait_cluster_vclock(self, servers, vclock)
    for _, name in pairs(servers) do
        self:wait_vclock(name, vclock)
    end
    return vclock
end

local switch_cmd1 = "env = require('test_run')"
local switch_cmd2 = "test_run = env.new('%s', '%s')"
local switch_cmd3 = "set connection %s"

local function switch(self, node)
    -- switch to other node and enable test_run
    self:eval(node, switch_cmd1)
    self:eval(node, switch_cmd2:format(self.host, self.port))
    return self:cmd(switch_cmd3:format(node))
end

local function log_box_info_replication_cond(id, field, ok, info, opts)
    local exp = json.encode({
        status = opts.status,
        message_re = opts.message_re,
    })
    local got = json.encode(info ~= nil and {
        status = info.status,
        message = info.message,
    } or nil)
    log.info('wait_%s(%d, ...); exp = %s; got = %s; result = %s', field, id,
        exp, got, tostring(ok))
end

local function box_info_replication_instance(id)
    if type(box.cfg) ~= 'table' then
        return nil
    end
    local box_info = box.info
    local replication = box_info ~= nil and box_info.replication or nil
    return replication ~= nil and replication[id] or nil
end

local function gen_box_info_replication_cond(id, field, opts)
    return function()
        local instance = box_info_replication_instance(id)
        local info = instance ~= nil and instance[field] or nil
        local ok = info ~= nil
        if opts.status ~= nil then
            ok = ok and info.status == opts.status
        end
        if opts.message_re ~= nil then
            -- regex match
            ok = ok and info.message ~= nil and info.message:match(
                opts.message_re)
        elseif type(opts.message_re) ~= 'nil' then
            -- expect nil or box.NULL if opts.message_re is box.NULL
            ok = ok and info.message == nil
        end
        log_box_info_replication_cond(id, field, not not ok, info, opts)
        if not ok then return false, instance end
        return true
    end
end

--- Wait for upstream status.
---
--- The function waits until information about an upstream with
--- provided id will appear (regardless of passed options) and
--- then waits for a certain state of the upstream if requested.
---
--- If `opts.status` is `nil` or `box.NULL` an upstream status is
--- not checked.
---
--- If `opts.message_re` is `nil` an upstream message is not
--- checked.
---
--- If `opts.message_re` is `box.NULL` an upstream message is
--- expected to be `nil` or `box.NULL`.
---
--- @tparam table self test-run instance
--- @tparam number id box.info.replication key
--- @tparam[opt] table opts values to wait for
--- @tparam[opt] string opts.status upstream status
--- @tparam[opt] string opts.message_re upstream message (regex)
---
--- @return `true` at success, `false` at error
--- @return `nil` at success, `box.info.replication[id]` at error
local function wait_upstream(self, id, opts)
    local opts = opts or {}
    assert(type(opts) == 'table')
    local cond = gen_box_info_replication_cond(id, 'upstream', opts)
    return self:wait_cond(cond)
end

--- Wait for downstream status.
---
--- See @{wait_upstream} for parameters and return values.
---
--- @tparam table self
--- @tparam number id
--- @tparam[opt] table opts
--- @tparam[opt] string opts.status
--- @tparam[opt] string opts.message_re
---
--- @return
--- @return
local function wait_downstream(self, id, opts)
    local opts = opts or {}
    assert(type(opts) == 'table')
    local cond = gen_box_info_replication_cond(id, 'downstream', opts)
    return self:wait_cond(cond)
end

local get_cfg_cmd = 'config %s'

local function get_cfg(self, name)
    if self.run_conf == nil then
        self.run_conf = self:cmd(get_cfg_cmd:format(name))
    end
    return self.run_conf[name]
end

local function grep_log(self, node, what, bytes, opts)
    local opts = opts or {}
    local noreset = opts.noreset or false
    -- if instance has crashed provide filename to use grep_log
    local filename = opts.filename or self:eval(node, "box.cfg.log")[1]
    local file = fio.open(filename, {'O_RDONLY', 'O_NONBLOCK'})

    local function fail(msg)
        local err = errno.strerror()
        if file ~= nil then
            file:close()
        end
        error(string.format("%s: %s: %s", msg, filename, err))
    end

    if file == nil then
        fail("Failed to open log file")
    end
    io.flush() -- attempt to flush stdout == log fd
    local filesize = file:seek(0, 'SEEK_END')
    if filesize == nil then
        fail("Failed to get log file size")
    end
    local bytes = bytes or 65536 -- don't read whole log - it can be huge
    bytes = bytes > filesize and filesize or bytes
    if file:seek(-bytes, 'SEEK_END') == nil then
        fail("Failed to seek log file")
    end
    local found, buf
    repeat -- read file in chunks
        local s = file:read(2048)
        if s == nil then
            fail("Failed to read log file")
        end
        local pos = 1
        repeat -- split read string in lines
            local endpos = string.find(s, '\n', pos)
            endpos = endpos and endpos - 1 -- strip terminating \n
            local line = string.sub(s, pos, endpos)
            if endpos == nil and s ~= '' then
                -- line doesn't end with \n or eof, append it to buffer
                -- to be checked on next iteration
                buf = buf or {}
                table.insert(buf, line)
            else
                if buf ~= nil then -- prepend line with buffered data
                    table.insert(buf, line)
                    line = table.concat(buf)
                    buf = nil
                end
                if string.match(line, "Starting instance") and not noreset then
                    found = nil -- server was restarted, reset search
                else
                    found = string.match(line, what) or found
                end
            end
            pos = endpos and endpos + 2 -- jump to char after \n
        until pos == nil
    until s == ''
    file:close()
    return found
end

-- Block until the condition function returns a positive value
-- (anything except `nil` and `false`) or until the timeout
-- exceeds. Return the result of the last invocation of the
-- condition function (it is `false` or `nil` in case of exiting
-- by the timeout).
local function wait_cond(self, cond, timeout, delay)
    assert(type(cond) == 'function')

    local timeout = timeout or 60
    local delay = delay or 0.001

    local start_time = clock.monotonic()
    local res = {cond()}

    while not res[1] do
        local work_time = clock.monotonic() - start_time
        if work_time > timeout then
            return unpack(res)
        end
        fiber.sleep(delay)
        res = {cond()}
    end

    return unpack(res)
end

-- Wrapper for grep_log, wait until expected log entry is appear
-- in a server log file.
local function wait_log(self, node, what, bytes, timeout, opts)
    assert(timeout ~= nil)

    local opts = opts or {}
    local delay = opts.delay

    local cond = function()
        return grep_log(self, node, what, bytes, opts)
    end

    return wait_cond(self, cond, timeout, delay)
end

local inspector_methods = {
    cmd = cmd,
    eval = eval,
    -- get wrappers
    get_param = get_param,
    get_server_id = get_server_id,
    get_cfg = get_cfg,
    -- lsn
    get_lsn = get_lsn,
    wait_lsn = wait_lsn,
    -- vclock
    get_vclock = get_vclock,
    wait_vclock = wait_vclock,
    switch = switch,
    -- replication
    create_cluster = create_cluster,
    drop_cluster = drop_cluster,
    cleanup_cluster = cleanup_cluster,
    wait_fullmesh = wait_fullmesh,
    get_cluster_vclock = get_cluster_vclock,
    wait_cluster_vclock = wait_cluster_vclock,
    wait_upstream = wait_upstream,
    wait_downstream = wait_downstream,
    --
    grep_log = grep_log,
    wait_cond = wait_cond,
    wait_log = wait_log,
}

local function inspector_new(host, port)
    local inspector = {}

    inspector.host = host or os.getenv('INSPECTOR_HOST') or 'localhost'
    inspector.port = port or tonumber(os.getenv('INSPECTOR_PORT'))
    if inspector.port == nil then
        error('Inspector not started')
    end

    return setmetatable(inspector, { __index = inspector_methods })
end

return {
    new = inspector_new;
}
