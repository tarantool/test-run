default:
	false

.PHONY: lint
lint:
	python2 -m flake8 *.py lib/*.py
