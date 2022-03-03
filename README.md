# Tarantool Functional testing framework

[![Coverage Status](https://coveralls.io/repos/github/tarantool/test-run/badge.svg)](https://coveralls.io/github/tarantool/test-run)


<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
## Table of Contents

- [Test suite configuration file](#test-suite-configuration-file)
  - [General configuration values](#general-configuration-values)
  - [Disabling and skipping tests](#disabling-and-skipping-tests)
  - [Other parameters](#other-parameters)
  - [Fragile tests](#fragile-tests)
- [Test composition](#test-composition)
- [Test execution](#test-execution)
- [Test configuration](#test-configuration)
- [Writing various types of tests](#writing-various-types-of-tests)
  - [Python tests](#python-tests)
  - [Lua](#lua)
  - [SQL](#sql)
- [Interaction with the test environment](#interaction-with-the-test-environment)
- [pretest_clean()](#pretest_clean)
- [Tags](#tags)
- [Projects that use test-run](#projects-that-use-test-run)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

## Test suite configuration file

Test suite is a bunch of tests located in a directory with a `suite.ini`
file. The `suite.ini` is a basic ini-file with one section `[default]`.

```ini
[default]
core = luatest
description = Example test suite using luatest
...
```

There's a syntax for single- and multiline lists. (TODO see ./sql-tap/suite.ini)
For example:

```ini
single_line = first.test.lua second.test.lua third.test.lua
multiline = first.test.lua ;
            second.test.lua ;
            third.test.lua ;
            ...
```

Below is a list of configuration values (fields) in the `suite.ini` file.

### General configuration values

* `core` — major type of tests in this suite.
  Should have one of the following values:

  * `tarantool` — a test that reads a file with commands
    feeds it line by line to the tarantool console,
    writes requests and responses to an output file,
    and then compares that file with a reference file.
    Often called a "diff-test".
  * `app` — a Lua script test.
    Most of such tests produce
    [TAP13 output](http://testanything.org/tap-version-13-specification.html)
    using the built-in `tap` module.
    Test-run validates such output without a reference file.
    Some tests produce non-TAP13 output, which is compared to a reference file.
  * `luatest` — test suite using the [luatest](https://github.com/tarantool/luatest/) library.
  * `unittest` — an executable test file.

* `description`

* `script` — A file with Tarantool commands.
  It is used to start the default server using `tarantoolctl`.
  The value should be a file in the same directory as `suite.ini`, like `box.lua` or `master.lua`.
  This setting is mandatory for test suites with `core = tarantool`
  and ignored with other types of tests.
  
* `config` — name of a test configuration file, for example, `engine.cfg`.
  For details, see the [test configuration](#test-configuration) section below.

* `lua_libs` — paths to Lua files that should be copied to the directory,
  where a server is started. For example:

  ```ini
  lua_libs = lua/require_mod.lua lua/serializer_test.lua lua/process_timeout.lua
  ```

### Disabling and skipping tests

A number of fields are used to disable certain tests:

* `disabled` — list of tests that should be skipped.

* `release_disabled` — list of tests that should only run when Tarantool is
   built in the debug mode (with `-DCMAKE_BUILD_TYPE=Debug` and not `=RelWithDebInfo`).
   Tests that use error injections, which are only available in debug builds,
   should always be `release_disabled`.

* `valgrind_disabled` — list of tests that should be skipped when Valgrind is enabled

* `long_run` — mark tests as long. Such tests will run only with `--long` option.

  ```ini
  long_run = t1.test.lua t2.test.lua
  ```

### Other parameters

* `show_reproduce_content` (optional, `True` by default) — when set to `True`,
  show the contents of the reproduce file for each failed test.
  Reproduce files are not required for investigating results of
  tests that run each case in a separate Tarantool instance.
  For such tests it makes sense to set `show_reproduce_content = False`.
  (Implemented in [#113](https://github.com/tarantool/test-run/issues/113))

* `is_parallel` (optional, `False` by default) — whether the tests in the suite can run in parallel

* `use_unix_sockets` (optional, `False` by default) — use hard-coded UNIX sockets
* `use_unix_sockets_iproto` (optional, `False` by default) — use hard-coded UNIX sockets for IProto.

### Fragile tests

Tests which fail sometimes due to external reasons can be marked as fragile (flaky).
Test-run will retry each test if it fails.
It's recommended to provide a list of related
[tarantool/tarantool](https://github.com/tarantool/tarantool) issues
next to each fragile test.

```ini
fragile = {
    "retries": 10,
    "tests": {
        "tarantoolctl.test.lua": {
            "issues": [ "gh-5059", "gh-5346" ],
        },
        {...},
        {...},
    }
}
```

## Test composition

Each test consists of the following files:

* Test file: `<name>.test.lua`, `<name>_test.lua`, `<name>.test.py`, or `<name>.test.sql`.
* Reference file (optional): `<name>.result` .
* Skip condition file (optional): `<name>.skipcond`.

Reference file contains saved test output.
It is required for tests with non-TAP13 output.
Running `test-run.py` with `--update-result` option will update
the `.result` files with new test output.

The optional skip condition file is a Python script.
It is used to skip a test on some conditions, typically on a certain OS.
In the local Python environment of a test run there's a `self` object,
which is an instance of the [`Test` class](./lib/test.py).
Set `self.skip = 1` to skip this test.
For example, to skip `sometest` on OpenBSD,
add the following `sometest.skipcond` file:

```python 
import platform

# Disabled on OpenBSD due to fail #XXXX.
if platform.system() == 'OpenBSD':
    self.skip = 1
```


## Test execution

Running a test begins with executing the `.skipcond` file.
If `self.skip` is set to `1`, test-run skips this test
and reports TODO.

Next, the test file is executed and the output is written to a `.reject` file.
If the output is TAP13-compatible, test-run validates it.
Otherwise, test-run compares it with the `.result` file.
If there's a difference between `.reject` and `.result`, the test fails and
the last 15 lines of diff are printed to output.

Whenever a test fails, the `.reject` file is saved
and the path to this file is printed to output.

## Test configuration

Test configuration file contains configuration for multiple runs.
For each test section, system runs a separate test and compares the result to the common `.result` file.

For example, `my.test.lua` will run with two different sets of parameters:

```json
{
    "my.test.lua": {
        "first": {"a": 1, "b": 2},
        "second": {"a": 1, "b": 3}
    }
}
```

A common case is to run a test with different DB engines.
In the example below:

* `first.test.lua` will run only on the memtx engine;
* `second.test.lua` will run as is, without parameterizing;
* all other tests in the suite (`*`) will be parameterized to run on both memtx and vinyl engines.
* 
```json
{
    "first.test.lua": {
      "memtx": {"engine": "memtx"}
    },
    "second.test.lua": {},
    "*": {
        "memtx": {"engine": "memtx"},
        "vinyl": {"engine": "vinyl"}
    }
}
```

In the test case we can get configuration from the inspector:

```lua
engine = test_run:get_cfg('engine')
-- first run engine is 'memtx'
-- second run engine is 'vinyl'
```

"engine" value has a special meaning for `*.test.sql` files: if it is "memtx" or
"vinyl", then the corresponding default engine will be set before executing
commands from a test file. An engine is set with the following commands:

```sql
UPDATE "_session_settings" SET "value" = 'memtx|vinyl' WHERE "name" = 'sql_default_engine'
pragma sql_default_engine='memtx|vinyl'
```

If the first fails, then the second will be executed. When both fail, the test fails.

## Writing various types of tests

### Python tests

Files: `<name>.test.py`, `<name>.result` and `<name>.skipcond` (optionally).

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

### Lua

Files: `<name>.test.lua`, `<name>.result` and `<name>.skipcond`(optionaly).
Tests interact only with `AdminConnection`. Supports some preprocessor functions (eg `delimiter`)

**Delimiter example:**

```lua
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

```yaml
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

### SQL

*.test.sql files are just SQL statements written line-by-line.

It is possible to mix SQL and Lua commands using `\set language lua` and `\set
language sql` commands.

## Interaction with the test environment

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

## pretest_clean()

Nothing will be done before a Python test and for `core = unittest`
test suites.

For a `core = [app|tarantool]` test suites this function removes tarantool WAL
and snapshot files before each test.

The following files will be removed:

* `*.snap`
* `*.xlog`
* `*.vylog`
* `*.inprogress`
* `[0-9]*/`

## Tags

Usage:

```sh
./test-run.py --tags foo
./test-run.py --tags foo,bar app/ app-tap/
```

test-run will run only those tests, which have at least one of the
provided tags.

Show a list of tags:

```sh
./test-run.py --tags
./test-run.py app-tap/ --tags
```

The tags metainfo should be placed within a first comment of a test
file.

Examples:

* .lua file:

  ```lua
  #!/usr/bin/tarantool

  -- tags: foo, bar
  -- tags: one, more

  <...>
  ```

* .sql file:

  ```sql
  -- tags: foo
  -- tags: bar
  <...>
  ```

* .py file:

  ```python
  # tags: foo

  <...>
  ```

Unsupported features:

* Marking unit tests with tags.
* Multiline comments (use singleline ones for now).

## Projects that use test-run

- [Tarantool](https://github.com/tarantool/tarantool) - in-memory database and application server
- [memcached](https://github.com/tarantool/memcached) - Memcached protocol 'wrapper' for Tarantool
- [vshard](https://github.com/tarantool/vshard) - sharding based on virtual buckets
- xsync (internal project)
