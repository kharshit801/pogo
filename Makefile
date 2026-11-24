.PHONY: help test lint docs screenshots install uninstall

help:
	@echo "make test         run the test suite"
	@echo "make lint         byte-compile everything (catches syntax errors)"
	@echo "make docs         regenerate docs/commands.md from the CLI's help"
	@echo "make screenshots  regenerate the terminal images in docs/assets"
	@echo "make install      link ./bin/pogo into ~/.local/bin"
	@echo "make uninstall    remove that link"

test:
	python3 -m unittest discover -s tests

lint:
	python3 -m compileall -q pogo tests scripts

docs:
	python3 scripts/gen_commands_doc.py

screenshots:
	./scripts/screenshots.sh

install:
	mkdir -p "$(HOME)/.local/bin"
	ln -sf "$(CURDIR)/bin/pogo" "$(HOME)/.local/bin/pogo"
	@echo "installed $(HOME)/.local/bin/pogo"

uninstall:
	rm -f "$(HOME)/.local/bin/pogo"
