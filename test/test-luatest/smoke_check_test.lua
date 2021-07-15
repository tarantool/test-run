local t = require('luatest')
local g = t.group()

g.test_smoke = function()
    t.assert(true)
    t.assert_not(false)
end
