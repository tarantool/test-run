MAKEFILE_PATH := $(abspath $(lastword $(MAKEFILE_LIST)))
PROJECT_DIR := $(patsubst %/,%,$(dir $(MAKEFILE_PATH)))
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
	PYTHONPATH=$(PROJECT_DIR) $(PYTHON) test/test-run.py --force --exclude unittest $(TEST_RUN_EXTRA_PARAMS)

test_unittest:
	$(PYTHON) -m unittest discover test/unittest/

test: test_unittest test_integration

coverage:
	PYTHON="coverage run" make -f $(MAKEFILE_PATH) test
	coverage combine $(PROJECT_DIR) $(PROJECT_DIR)/test
	coverage report

clean:
	coverage erase

.PHONY: lint flake8 luacheck test test_integration test_unittest
