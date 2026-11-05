PY ?= python3

.PHONY: test
test:
	$(PY) -m unittest discover -s tests -t . -v
