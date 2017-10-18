local socket = require('socket')
local json = require('json')
local yaml = require('yaml')
local log = require('log')
local fiber = require('fiber')
local fio = require('fio')
local errno = require('errno')

local function cmd(self, msg)
    local sock = socket.tcp_connect(self.host, self.port)
    local data = msg .. '\n'
    sock:send(data)

    local result = sock:read('\n...\n')
    sock:close()
    result = yaml.decode(result)
    if type(result) == 'table' and result.error ~= nil then
        error(result.error)
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
            if lsn < to_lsn then
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


local create_cluster_cmd1 = 'create server %s with script="%s/%s.lua",' ..
                            ' wait_load=False, wait=False'
local create_cluster_cmd2 = 'start server %s'

local function create_cluster(self, servers, test_suite)
    test_suite = test_suite or 'replication'
    for _, name in ipairs(servers) do
        self:cmd(create_cluster_cmd1:format(name, test_suite, name))
        self:cmd(create_cluster_cmd2:format(name))
    end
end

local drop_cluster_cmd1 = 'stop server %s'
local drop_cluster_cmd2 = 'cleanup server %s'

local function drop_cluster(self, servers)
    for _, name in ipairs(servers) do
        self:cmd(drop_cluster_cmd1:format(name))
        self:cmd(drop_cluster_cmd2:format(name))
    end
end

local set_env_variable_cmd = [[env %s="%s"]]

local function set_cluster_environment(self, env_dict)
    if type(env_dict) ~= 'table' then
        log.error('environment must be a Lua table')
        return nil
    end
    for name, val in pairs(env_dict) do
        self:cmd(set_env_variable_cmd:format(name, val))
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
            local info = self:eval(server, "box.info")
            fiber.sleep(0.01)
        end
        -- wait all for full mesh
        for _, server2 in ipairs(servers) do
            if server ~= server2 then
                log.info("%s -> %s: waiting for connection", server2, server)
                while true do
                    local cmd = wait_fullmesh_cmd:format(server_id)
                    local info = self:eval(server2, cmd)[1]
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

local get_cfg_cmd = 'config %s'

local function get_cfg(self, name)
    if self.run_conf == nil then
        self.run_conf = self:cmd(get_cfg_cmd:format(name))
    end
    return self.run_conf[name]
end

local function grep_log(self, node, what, bytes)
    local filename = self:eval(node, "box.cfg.log")[1]
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
                if string.match(line, "Starting instance") then
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
    set_cluster_environment = set_cluster_environment,
    --
    grep_log = grep_log,
}

local function inspector_new(host, port)
    local inspector = {}

    inspector.host = host or 'localhost'
    inspector.port = port or tonumber(os.getenv('INSPECTOR'))
    if inspector.port == nil then
        error('Inspector not started')
    end

    return setmetatable(inspector, { __index = inspector_methods })
end

return {
    new = inspector_new;
}
