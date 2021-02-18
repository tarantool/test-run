# Tarantool Functional testing framework

### Test Suite

Bunch of tests, that lay down in the subfolder (recursively) with `suite.ini`
file. `suite.ini` is basic ini-file, that consists of one section `default`,
and a number of fields:

* `core`
* `description` - Test Suite description
* `script` - shebang file to start tarantool with
* disables:
    * `disabled` - tests that must be skipped
    * `release_disabled` - tests that must be skipped when Tarantool has been
      builded with `Release`
    * `valgrind_disabled` - tests that must be skipped when Valgrind is enabled
* `lua_libs` - paths for lua files, that should be copied into the folder,
  where server is started (delimited with the space, e.g. `lua_libs=lua/1.lua
  lua/2.lua`)
* `long_run` - mark tests as long, enabled only with `--long` option (delimited
  with the space, e.g. `long_run=t1.test.lua t2.test.lua`)
* `config` - test configuration file name
* `pretest_clean` - see [pretest_clean](#pretest_clean)

Field `core` must be one of:

* `tarantool` - Test-Suite for Functional Testing
* `app` - Another functional Test-Suite
* `unittest` - Unit-Testing Test Suite

### Test

Each test consists of files `*.test(.lua|.sql|.py)?`, `*.result`, and may have
skip condition file `*.skipcond`.  On first run (without `.result`) `.result`
is generated from output.  Each run, in the beggining, `.skipcond` file is
executed. In the local env there's object `self`, that's `Test` object. If test
must be skipped - you must put `self.skip = 1` in this file. Next,
`.test(.lua|.py)?` is executed and file `.reject` is created, then `.reject` is
compared with `.result`. If something differs, then 15 last string of this diff
file are printed and `.reject` file is saving in the `<vardir>/rejects/<suite>`
subfolder given in options or set localy as `var/rejects/<suite>` by default.
If not, then `.reject` file is deleted.

### Test configuration

Test configuration file contains config for multiple run. For each test section
system runs separated test and compares result with common `.result` file. For
example we need to run one test for different db engines("*" means default
configuration):

```json
{
    "my.test.lua": {
        "first": {"a": 1, "b": 2},
        "second": {"a": 1, "b": 3}
    },
    "*": {
        "memtx": {"engine": "memtx"},
        "sophia": {"engine": "sophia"}
    }
}
```

In test case we can get configuration from inspector:

```lua
engine = test_run:get_cfg('engine')
-- first run engine is 'memtx'
-- second run engine is 'sophia'
```

"engine" value has a special meaning for *.test.sql files: if it is "memtx" or
"vinyl", then the corresponding default engine will be set before executing
commands from a test file. An engine is set with the following commands:

```sql
UPDATE "_session_settings" SET "value" = 'memtx|vinyl' WHERE "name" = 'sql_default_engine'
pragma sql_default_engine='memtx|vinyl'
```

If the first fails, then the second will be executed. When both fails, fail the test.

#### Python

Files: `<name>.test.py`, `<name>.result` and `<name>.skipcond`(optionaly).

Environment:

* `sql` - `BoxConnection` class. Convert our subclass of SQL into IProto query
  and then decode it. Print into `.result` in YAML. Examples:
    * `sql("select * from t<space> where k<key>=<string|number>[ limit <number>]")`
    * `sql("insert into t<space> values ([<string|number> [, <string|number>]*])")`
    * `sql("delete from t<space> where k<key>=<string|number>")`
    * `sql("call <proc_name>([string|number]*)")`
    * `sql("update t<space> set [k<field>=<string|number> [, k<field>=<string|number>]*] where k<key>=<string|number>"")`
    * `sql("ping")`
* `admin` - `AdminConnection` - simply send admin query on admin port (LUA),
  then, receive answer. Examples
    * `admin('box.info')`

**Example:**

```python
import os
import time

from lib.admin_connection import AdminConnection
from lib.tarantool_server import TarantoolServer

master = server
admin("box.info.lsn") # equivalent to master.admin("box.info.lsn") and server.admin(...)
sql("select * from t0 where k0=1")
replica = TarantoolServer()
replica.script = 'replication/replica.lua'
replica.vardir = os.path.join(server.vardir, "replica")
replica.deploy()
master.admin("box.insert(0, 1, 'hello')")
print('sleep_1')
time.sleep(0.1)
print('sleep_finished')
print('sleep_2')
admin("require('fiber').sleep(0.1)")
print('sleep_finished')
replica.admin("box.select(0, 0, 1)")
con2 = AdminConnection('localhost', server.admin.port)
con2("box.info.lsn")
replica.stop()
replica.cleanup()
con2.disconnect()
```

**Result:**

```yaml
box.info.lsn
---
- null
...
select * from t0 where k0=1
---
- error:
    errcode: ER_NO_SUCH_SPACE
    errmsg: Space '#0' does not exist
...
box.insert(0, 1, 'hello')
---
- error: '[string "return box.insert(0, 1, ''hello'')"]:1: attempt to call field ''insert''
    (a nil value)'
...
sleep_1
sleep_finished
sleep_2
require('fiber').sleep(0.1)
---
...
sleep_finished
box.select(0, 0, 1)
---
- error: '[string "return box.select(0, 0, 1)"]:1: attempt to call field ''select''
    (a nil value)'
...
box.info.lsn
---
- null
...
```

#### Lua

Files: `<name>.test.lua`, `<name>.result` and `<name>.skipcond`(optionaly).
Tests interact only with `AdminConnection`. Supports some preprocessor functions (eg `delimiter`)

**Delimiter example:**

```
env = require('test_run')
test_run = env.new()
box.schema.space.create('temp')
t1 = box.space.temp
t1:create_index('primary', { type = 'hash', parts = {1, 'num'}, unique = true})
t1:insert{0, 1, 'hello'}
test_run:cmd("setopt delimiter ';'")
function test()
    return {1,2,3}
end;
test(
);
test_run:cmd("setopt delimiter ''");
test(
);
test
```

**Delimiter result:**

```
env = require('test_run')
test_run = env.new()
box.schema.space.create('temp')
---
- index: []
  on_replace: 'function: 0x40e4fdf0'
  temporary: false
  id: 512
  engine: memtx
  enabled: false
  name: temp
  field_count: 0
- created
...
t1 = box.space.temp
---
...
t1:create_index('primary', { type = 'hash', parts = {1, 'num'}, unique = true})
---
...
t1:insert{0, 1, 'hello'}
---
- [0, 1, 'hello']
...
test_run:cmd("setopt delimiter ';'")
function test()
    return {1,2,3}
end;
---
...
test(
);
---
- - 1
  - 2
  - 3
...
test_run:cmd("setopt delimiter ''");
test(
---
- error: '[string "test( "]:1: unexpected symbol near ''<eof>'''
...
);
---
- error: '[string "); "]:1: unexpected symbol near '')'''
...
test
---
- 'function: 0x40e533b8'
...
```

It is possible to use backslash at and of a line to carry it.

```lua
function echo(...) \
    return ...     \
end
```

#### SQL

*.test.sql files are just SQL statements written line-by-line.

It is possible to mix SQL and Lua commands using `\set language lua` and `\set
language sql` commands.

##### Interaction with the test environment

In lua test you can use `test_run` module to interact with the test
environment.

```lua
env = require('test_run')
test_run = env.new()
test_run:cmd("<command>")
```

__Base directives:__

* `setopt delimiter '<delimiter>'` - Sets delimiter to `<delimiter>`\n

__Server directives:__

* `create server <name> with ...` - Create server with name `<name>`, where `...`
  may be:
    * `script = '<path>'` - script to start
    * `rpl_master = <server>` - replication master server name
* `start server <name>` - Run server `<name>`
* `stop server <name> [with signal=<signal>]` - Stop server `<name>`
    * `<signal>` is a signal name (with or without 'SIG' prefix, uppercased) or
      a signal number to use instead of default SIGTERM
* `cleanup server <name>` - Cleanup (basically after server has been stopped)
* `restart server <name>` - Restart server `<name>` (you can restart yourself
  from lua!)

__Connection switch:__

* `switch <name>` - Switch connection to server `<name>` and add test run into
  global scope

__Connection directives(low level):__

* `create connection <name-con> to <name-serv>` - create connection named
  `<name-con>` to `<name-serv>` server
* `drop connection <name>` - Turn connection `<name>` off and delete it
* `set connection <name>` - Set connection `<name>` to be main, for next commands

__Filter directives:__

* `push filter '<regexp_from>' to '<regexp_to>'` - e.g. `push filter 'listen: .*' to 'listen: <uri>'`

__Set variables:__

* `set variables '<variable_name>' to '<where>'` - execute
  `<variable_name> = *<where>` where *<where> is value of where. Where must be
    * `<server_name>.admin` - admin port of this server
    * `<server_name>.master` - listen port of master of this replica
    * `<server_name>.listen` - listen port of this server

__Dev ops features:__

You can power on any tarantool replicas in a loop.

```lua
test_run:cmd('setopt delimiter ";"')
function join(inspector, n)
    for i=1,n do
        local rid = tostring(i)
        os.execute('mkdir -p tmp')
        os.execute('cp ../replication/replica.lua ./tmp/replica'..rid..'.lua')
        os.execute('chmod +x ./tmp/replica'..rid..'.lua')
        inspector:cmd("create server replica"..rid.." with rpl_master=default, script='./var/tmp/replica"..rid..".lua'")
        inspector:cmd("start server replica"..rid)
    end
end;
test_run:cmd('setopt delimiter ""');

-- create 30 replicas for current tarantool
join(test_run, 30)
```

### pretest_clean

Add the following line to `suite.ini` to enable this option for a test suite:

```
pretest_clean = True
```

The behaviour of this option varies across test suite types (`core = tarantool /
app / unittest`).

For a `core = tarantool` test suite enabling of this option will lead to
execution of a special clean function before each Lua test. The
`pretest_clean.lua` file is copied into a test directory (`var/ddd-suite-name`)
and `require('pretest_clean').clean()` is invoked before each test.

This function performs the following steps:

* drop all non-system spaces;
* delete all users except 'guest' and 'admin';
* delete all roles except 'public', 'replicaiton' and 'super';
* delete all `box.space._func` records except `box.schema.user.info`;
* delete all `box.space._cluster` records except one for a current instance;
* remove all global variables except ones that tarantool had at start (it uses a
  predefined list, see the source);
* unload all packages except built-in ones (see the source for the list).

Set _G.protected_globals to list of names to protect custom globals.

Nothing will be done before a Python test.

For a `core = app` test suite enabling of this option will lead to removing
tarantool WAL and snapshot files before each test.

The following files will be removed:

* `*.snap`
* `*.xlog`
* `*.vylog`
* `*.inprogress`
* `[0-9]*/`

For a `core = unittest` test suite this option does not change any behaviour.

### Used By

- [Tarantool](https://github.com/tarantool/tarantool) - in-memory database and application server
- [vshard](https://github.com/tarantool/vshard) - sharding based on virtual buckets
- xsync (internal project)
