globals = {"box", "_TARANTOOL", "tonumber64", "os", "test_run"}
ignore = {
    -- Accessing an undefined field of a global variable <debug>.
    "143/debug",
    -- Accessing an undefined field of a global variable <string>.
    "143/string",
    -- Accessing an undefined field of a global variable <table>.
    "143/table",
    -- Unused argument <self>.
    "212/self",
    -- Redefining a local variable.
    "411",
    -- Redefining an argument.
    "412",
    -- Shadowing a local variable.
    "421",
    -- Shadowing an upvalue.
    "431",
    -- Shadowing an upvalue argument.
    "432",
}

include_files = {
    "**/*.lua",
}

exclude_files = {
    "lib/tarantool-python",
    "test/test-tarantool/*.test.lua",
    "lib/luatest/**",
    "lib/checks/**",
}
