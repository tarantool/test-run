default:
	false

.PHONY: lint flake8 luacheck
lint: flake8 luacheck

flake8:
	python2 -m flake8 *.py lib/*.py

luacheck:
	luacheck --config .luacheckrc .
