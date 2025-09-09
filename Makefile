
.PHONY: mypy pylint bandit

SRC = board_annotate.py

all:
	- $(MAKE) mypy
	- $(MAKE) pylint
	- $(MAKE) bandit

mypy:
	mypy $(SRC)

pylint:
	pylint $(SRC)

bandit:
	bandit $(SRC)
