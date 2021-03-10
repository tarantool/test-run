-- Copy of cleanup_cluster() from test_run.lua.
local function cleanup_cluster()
    local cluster = box.space._cluster:select()
    for _, tuple in pairs(cluster) do
        if tuple[1] ~= box.info.id then
            box.space._cluster:delete(tuple[1])
        end
    end
end

local function cleanup_list(list, allowed)
    for k, _ in pairs(list) do
        if not allowed[k] then
            list[k] = nil
        end
    end
end

local function clean()
    local _SPACE_NAME = 3

    box.space._space:pairs():map(function(tuple)
        local name = tuple[_SPACE_NAME]
        return name
    end):filter(function(name)
        -- skip internal spaces
        local first_char = string.sub(name, 1, 1)
        return first_char ~= '_'
    end):each(function(name)
        box.space[name]:drop()
    end)

    local _USER_TYPE = 4
    local _USER_NAME = 3

    local allowed_users = {
        guest = true,
        admin = true,
    }
    box.space._user:pairs():filter(function(tuple)
        local tuple_type = tuple[_USER_TYPE]
        return tuple_type == 'user'
    end):map(function(tuple)
        local name = tuple[_USER_NAME]
        return name
    end):filter(function(name)
        return not allowed_users[name]
    end):each(function(name)
        box.schema.user.drop(name)
    end)

    local allowed_roles = {
        public = true,
        replication = true,
        super = true,
    }
    box.space._user:pairs():filter(function(tuple)
        local tuple_type = tuple[_USER_TYPE]
        return tuple_type == 'role'
    end):map(function(tuple)
        local name = tuple[_USER_NAME]
        return name
    end):filter(function(name)
        return not allowed_roles[name]
    end):each(function(name)
        box.schema.role.drop(name)
    end)

    local _FUNC_NAME = 3
    local allowed_funcs = {
        ['box.schema.user.info'] = true,
        ['TRIM'] = true,
        ['TYPEOF'] = true,
        ['PRINTF'] = true,
        ['UNICODE'] = true,
        ['CHAR'] = true,
        ['HEX'] = true,
        ['VERSION'] = true,
        ['QUOTE'] = true,
        ['REPLACE'] = true,
        ['SUBSTR'] = true,
        ['GROUP_CONCAT'] = true,
        ['JULIANDAY'] = true,
        ['DATE'] = true,
        ['TIME'] = true,
        ['DATETIME'] = true,
        ['STRFTIME'] = true,
        ['CURRENT_TIME'] = true,
        ['CURRENT_TIMESTAMP'] = true,
        ['CURRENT_DATE'] = true,
        ['LENGTH'] = true,
        ['POSITION'] = true,
        ['ROUND'] = true,
        ['UPPER'] = true,
        ['LOWER'] = true,
        ['IFNULL'] = true,
        ['RANDOM'] = true,
        ['CEIL'] = true,
        ['CEILING'] = true,
        ['CHARACTER_LENGTH'] = true,
        ['CHAR_LENGTH'] = true,
        ['FLOOR'] = true,
        ['MOD'] = true,
        ['OCTET_LENGTH'] = true,
        ['ROW_COUNT'] = true,
        ['COUNT'] = true,
        ['LIKE'] = true,
        ['ABS'] = true,
        ['EXP'] = true,
        ['LN'] = true,
        ['POWER'] = true,
        ['SQRT'] = true,
        ['SUM'] = true,
        ['TOTAL'] = true,
        ['AVG'] = true,
        ['RANDOMBLOB'] = true,
        ['NULLIF'] = true,
        ['ZEROBLOB'] = true,
        ['MIN'] = true,
        ['MAX'] = true,
        ['COALESCE'] = true,
        ['EVERY'] = true,
        ['EXISTS'] = true,
        ['EXTRACT'] = true,
        ['SOME'] = true,
        ['GREATER'] = true,
        ['LESSER'] = true,
        ['SOUNDEX'] = true,
        ['LIKELIHOOD'] = true,
        ['LIKELY'] = true,
        ['UNLIKELY'] = true,
        ['GREATEST'] = true,
        ['LEAST'] = true,
        ['_sql_stat_get'] = true,
        ['_sql_stat_push'] = true,
        ['_sql_stat_init'] = true,
        ['LUA'] = true,
    }
    box.space._func:pairs():map(function(tuple)
        local name = tuple[_FUNC_NAME]
        return name
    end):filter(function(name)
        return not allowed_funcs[name]
    end):each(function(name)
        box.schema.func.drop(name)
    end)

    cleanup_cluster()

    local allowed_globals = {
        -- modules
        bit = true,
        coroutine = true,
        debug = true,
        io = true,
        jit = true,
        math = true,
        misc = true,
        os = true,
        package = true,
        string = true,
        table = true,
        utf8 = true,
        -- variables
        _G = true,
        _VERSION = true,
        arg = true,
        -- functions
        assert = true,
        collectgarbage = true,
        dofile = true,
        error = true,
        gcinfo = true,
        getfenv = true,
        getmetatable = true,
        ipairs = true,
        load = true,
        loadfile = true,
        loadstring = true,
        module = true,
        next = true,
        pairs = true,
        pcall = true,
        print = true,
        rawequal = true,
        rawget = true,
        rawset = true,
        require = true,
        select = true,
        setfenv = true,
        setmetatable = true,
        tonumber = true,
        tonumber64 = true,
        tostring = true,
        type = true,
        unpack = true,
        xpcall = true,
        -- tarantool
        _TARANTOOL = true,
        box = true,
        dostring = true,
        help = true,
        newproxy = true,
        role_check_grant_revoke_of_sys_priv = true,
        tutorial = true,
        update_format = true,
        protected_globals = true,
    }
    for _, name in ipairs(rawget(_G, 'protected_globals') or {}) do
        allowed_globals[name] = true
    end
    cleanup_list(_G, allowed_globals)

    -- Strict module tracks declared global variables when the
    -- strict mode is enabled, so we need to flush its internal
    -- state.
    local mt = getmetatable(_G)
    if mt ~= nil and type(mt.__declared) == 'table' then
        cleanup_list(mt.__declared, allowed_globals)
    end

    local allowed_packages = {
        ['_G'] = true,
        bit = true,
        box = true,
        ['box.backup'] = true,
        ['box.internal'] = true,
        ['box.internal.sequence'] = true,
        ['box.internal.session'] = true,
        ['box.internal.space'] = true,
        buffer = true,
        clock = true,
        console = true,
        coroutine = true,
        crypto = true,
        csv = true,
        debug = true,
        decimal = true,
        digest = true,
        errno = true,
        ffi = true,
        fiber = true,
        fio = true,
        fun = true,
        help = true,
        ['help.en_US'] = true,
        ['http.client'] = true,
        iconv = true,
        ['internal.argparse'] = true,
        ['internal.trigger'] = true,
        io = true,
        jit = true,
        ['jit.bc'] = true,
        ['jit.bcsave'] = true,
        ['jit.dis_x64'] = true,
        ['jit.dis_x86'] = true,
        ['jit.dump'] = true,
        ['jit.opt'] = true,
        ['jit.p'] = true,
        ['jit.profile'] = true,
        ['jit.util'] = true,
        ['jit.v'] = true,
        ['jit.vmdef'] = true,
        ['jit.zone'] = true,
        json = true,
        log = true,
        math = true,
        misc = true,
        msgpack = true,
        msgpackffi = true,
        ['net.box'] = true,
        ['net.box.lib'] = true,
        os = true,
        package = true,
        pickle = true,
        popen = true,
        pwd = true,
        socket = true,
        strict = true,
        string = true,
        table = true,
        ['table.clear'] = true,
        ['table.new'] = true,
        tap = true,
        tarantool = true,
        title = true,
        uri = true,
        utf8 = true,
        uuid = true,
        xlog = true,
        yaml = true,
    }
    cleanup_list(package.loaded, allowed_packages)

    local user_count = box.space._user:count()
    assert(user_count == 4 or user_count == 5,
        'box.space._user:count() should be 4 (1.10) or 5 (2.0)')
    assert(box.space._cluster:count() == 1,
        'box.space._cluster:count() should be only one')

    -- Ensure all traces of a previous test are gone: open
    -- iterators and so on. They can affect statistics counters
    -- that may be important for a test.
    collectgarbage()
end

return {
    clean = clean;
}
