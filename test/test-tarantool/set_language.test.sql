-- Simple SQL test that uses '\set language' command.
-- Command introduced in commit 6e38b88eb6bbe543a1e3ba0a6a0be2f6f58abc86
-- ('Implement SQL driver')

-- Create table for tests
CREATE TABLE t (a BOOLEAN PRIMARY KEY);
INSERT INTO t VALUES (true), (false);

-- Create user-defined function.
\set language lua
test_run = require('test_run').new()
\set language sql

SELECT a FROM t WHERE a;
SELECT a FROM t WHERE a != true;

-- Cleaning.
DROP TABLE t;
