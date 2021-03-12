-- Simple test that uses 'setopt delimiter' command.
-- Command introduced in commit 6e38b88eb6bbe543a1e3ba0a6a0be2f6f58abc86
-- ('Implement SQL driver')

test_run = require('test_run').new()

-- Using delimiter
_ = test_run:cmd("setopt delimiter ';'")
function test_a()
    local a = 1
end;
_ = test_run:cmd("setopt delimiter ''");

box.cfg{}

-- Using multiline
box.cfg{                     \
    coredump = false,        \
    log_format = 'plain',    \
    log_level = 5,           \
    strip_core = true        \
}
