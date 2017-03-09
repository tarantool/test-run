local socket = require('socket')
local yaml = require('yaml')
local log = require('log')
local fiber = require('fiber')
local fio = require('fio')
local errno = require('errno')

local function request(self, msg)
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

local function tnt_eval(self, node, expr)
    return request(self, 'eval ' .. node .. ' "' .. expr .. '"')
end

local function get_param(self, node, param)
    local cmd = 'eval ' .. node .. ' "return box.info'
    if param ~= nil then
        cmd = cmd .. '.' .. param
    end
    cmd = cmd .. '"'
    return request(self, cmd)
end

local function get_lsn(self, node, sid)
    local nodes = get_param(self, node, 'vclock')
    return tonumber(nodes[1][tonumber(sid)])
end

local function wait_lsn(self, waiter, master)
    local sid = self:get_param(master, 'server')[1].id
    local lsn = self:get_lsn(master, sid)

    while self:get_lsn(waiter, sid) < lsn do
        fiber.sleep(0.001)
    end
end

local function get_server_id(self, node)
    return tonumber(self:get_param(node, "server")[1].id)
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

local function create_cluster(self, servers)
    -- TODO: use the name of test suite instead of 'replication/'
    for _, name in ipairs(servers) do
        self:cmd("create server "..name..
                 "  with script='replication/"..name..".lua', "..
                 "       wait_load=False, wait=False")
        self:cmd("start server "..name)
    end
end

local function drop_cluster(self, servers)
    for _, name in ipairs(self) do
        self:cmd("stop server "..name)
        self:cmd("cleanup server "..name)
    end
end

local function wait_fullmesh(self, servers)
    log.info("starting full mesh")
    for _, server in ipairs(servers) do
        -- wait bootstrap to finish
        log.info("%s: waiting bootstrap", server)
        local server_id
        while true do
            server_id = self:get_server_id(server)
            if server_id > 0 then
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
                    local info = self:eval(server2,
                        "box.info.replication["..server_id.."]")[1]
                    if info ~= nil and info.status == 'follow' then
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

local function switch(self, node)
    -- switch to other node and enable test_run
    self:eval(node, "env=require('test_run')")
    self:eval(node, "test_run=env.new('"..self.host.."', "..tostring(self.port)..")")
    return self:cmd('set connection ' .. node)
end

local function get_cfg(self, name)
    if self.run_conf == nil then
        self.run_conf = self:cmd('config ' .. name)
    end
    return self.run_conf[name]
end

local function grep_log(self, node, what, bytes)
    local filename = self:eval(node, "box.cfg.log")[1]
    local file -- forward declaration for fail() to capture
    local function fail(msg)
        local err = errno.strerror()
        if file ~= nil then
            file:close()
        end
        error(string.format("%s: %s: %s", msg, filename, err))
    end
    file = fio.open(filename, {'O_RDONLY', 'O_NONBLOCK'})
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

local function new(host, port)
    local inspector = {}

    if host == nil then
        inspector.host = 'localhost'
    else
        inspector.host = host
    end

    if port == nil then
        inspector.port = tonumber(os.getenv('INSPECTOR'))
	if inspector.port == nil then
	    error('Inspector not started')
	end
    else
        inspector.port = port
    end


    inspector.cmd = request
    inspector.eval = tnt_eval
    inspector.get_param = get_param
    inspector.get_server_id = get_server_id
    inspector.get_lsn = get_lsn
    inspector.wait_lsn = wait_lsn
    inspector.get_vclock = get_vclock
    inspector.wait_vclock = wait_vclock
    inspector.switch = switch
    inspector.create_cluster = create_cluster
    inspector.drop_cluster = drop_cluster
    inspector.wait_fullmesh = wait_fullmesh
    inspector.get_cluster_vclock = get_cluster_vclock
    inspector.wait_cluster_vclock = wait_cluster_vclock
    inspector.get_cfg = get_cfg
    inspector.grep_log = grep_log
    return inspector
end

return {
    new=new;
}
