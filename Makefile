default:
	false

.PHONY: lint
lint:
	python -m flake8 *.py lib/*.py
