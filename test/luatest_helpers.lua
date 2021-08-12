local fun = require('fun')
local json = require('json')

local luatest_helpers = {}

luatest_helpers.Server = require('test.luatest_helpers.server')

local function default_cfg()
    return {
        work_dir = os.getenv('TARANTOOL_WORKDIR'),
        listen = os.getenv('TARANTOOL_LISTEN'),
    }
end

local function env_cfg()
    local src = os.getenv('TARANTOOL_BOX_CFG')
    if src == nil then
        return {}
    end
    local res = json.decode(src)
    assert(type(res) == 'table')
    return res
end

-- Collect box.cfg table from values passed through
-- luatest_helpers.Server({<...>}) and from the given argument.
--
-- Use it from inside an instance script.
function luatest_helpers.box_cfg(cfg)
    return fun.chain(default_cfg(), env_cfg(), cfg or {}):tomap()
end

return luatest_helpers
