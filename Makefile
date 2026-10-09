.PHONY: install run dev test clean clean-data

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
HOST ?= 127.0.0.1
PORT ?= 5024

install:
	python3 -m venv $(VENV)
	$(PIP) install -e ".[dev]"
	@echo "✅ Installed. Run 'make run' to start."

run:
	$(PYTHON) -m uvicorn lumenfeed.api:app --host $(HOST) --port $(PORT)

dev:
	$(PYTHON) -m uvicorn lumenfeed.api:app --host $(HOST) --port $(PORT) --reload

test:
	$(PYTHON) -m pytest tests/ -v

# Leaves data/ alone: it holds your reading history, saves and preferences.
clean:
	rm -rf $(VENV) build/ dist/ *.egg-info/ .pytest_cache/ .ruff_cache/
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	@echo "🧹 Cleaned (data/ kept; 'make clean-data' deletes it)."

clean-data:
	rm -rf data/
	@echo "🗑️  Deleted data/ (articles, reads, saves)."
