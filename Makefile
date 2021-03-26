TEST_RUN_EXTRA_PARAMS?=
PYTHON?=python

default:
	false

lint: flake8 luacheck

flake8:
	$(PYTHON) -m flake8 *.py lib/*.py

luacheck:
	luacheck --config .luacheckrc .

test_integration:
	$(PYTHON) test/test-run.py --force $(TEST_RUN_EXTRA_PARAMS)

test_unittest:
	$(PYTHON) -m unittest discover test/unittest/

test: test_unittest test_integration

.PHONY: lint flake8 luacheck test test_integration test_unittest
