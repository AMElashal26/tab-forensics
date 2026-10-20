PYTHON ?= python3
SCRIPTS := scripts
STAMP   := $(shell date +%Y-%m-%d)
OUTDIR  ?= $(HOME)/Documents

.PHONY: help test audit archive demo board clean lint

help:
	@echo "tab-forensics"
	@echo ""
	@echo "  make test      run the test suite"
	@echo "  make audit     read-only report on the live Chrome session"
	@echo "  make archive   write markdown + URL list + HTML board to $(OUTDIR)"
	@echo "  make board     write only the HTML restore board"
	@echo "  make demo      build a board from the synthetic sample fixture"
	@echo "  make clean     remove generated files and caches"
	@echo ""
	@echo "Destructive operations are NOT exposed as make targets by design."
	@echo "Run them explicitly, and read the script first:"
	@echo "  $(PYTHON) $(SCRIPTS)/close_duplicates.py            # dry run"
	@echo "  $(PYTHON) $(SCRIPTS)/close_duplicates.py --apply    # closes duplicates"
	@echo "  $(PYTHON) $(SCRIPTS)/open_batch.py --file ... --domain ... --limit 25 --apply"

test:
	$(PYTHON) -m unittest discover -s tests -v

audit:
	cd $(SCRIPTS) && $(PYTHON) session_audit.py

archive:
	cd $(SCRIPTS) && $(PYTHON) session_audit.py \
		--archive $(OUTDIR)/tabs-$(STAMP).md \
		--board   $(OUTDIR)/tab-restore-board-$(STAMP).html

board:
	cd $(SCRIPTS) && $(PYTHON) session_audit.py \
		--board $(OUTDIR)/tab-restore-board-$(STAMP).html

demo:
	cd $(SCRIPTS) && $(PYTHON) build_board.py \
		../data/sample/sample_session.json \
		-o /tmp/demo-board.html \
		--title "Demo Board"

lint:
	$(PYTHON) -m py_compile $(SCRIPTS)/*.py && echo "compile OK"
	@command -v ruff >/dev/null 2>&1 && ruff check $(SCRIPTS) tests || echo "ruff not installed, skipped"

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache build dist *.egg-info
	rm -f /tmp/demo-board.html /tmp/smoke*.md /tmp/smoke*.html
	@echo "cleaned"
