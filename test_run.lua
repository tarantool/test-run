socket = require('socket')
json = require('json')
log = require('log')
fiber = require('fiber')

local function request(self, msg)
    local sock = socket.tcp_connect(self.host, self.port)
    local data = msg .. '\n'
    sock:send(data)

    local result = sock:read('\n')
    result = string.gsub(result, '\n', '')
    sock:close()
    if result == 'OK' then
        return true
    end
    return tostring(result)
end

local function tnt_eval(self, node, expr)
    return json.decode(
        request(self, 'eval ' .. node .. ' "' .. expr .. '"')
    )
end

local function get_param(self, node, param)
    local cmd = 'eval ' .. node .. ' "return box.info'
    if param ~= nil then
        cmd = cmd .. '.' .. param
    end
    cmd = cmd .. '"'
    log.info(node ..' ' .. request(self, cmd))
    return json.decode(request(self, cmd))['result']
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

local function switch(self, node)
    -- switch to other node and enable test_run
    self:eval(node, "env=require('test_run')")
    self:eval(node, "test_run=env.new('"..self.host.."', "..tostring(self.port)..")")
    return self:cmd('set connection ' .. node)
end

local function get_cfg(self, name)
    if self.run_conf == nil then
        self.run_conf = json.decode(
            self:cmd('config ' .. name)
        )
    end
    return self.run_conf[name]
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
    else
        inspector.port = port
    end


    inspector.cmd = request
    inspector.eval = tnt_eval
    inspector.get_param = get_param
    inspector.get_lsn = get_lsn
    inspector.wait_lsn = wait_lsn
    inspector.switch = switch
    inspector.get_cfg = get_cfg
    return inspector
end

return {
    new=new;
}
