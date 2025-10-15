.PHONY: help install install-dev install-gdl90 update-gdl90 run run-monitor clean test venv sync

# UV binary (install with: curl -LsSf https://astral.sh/uv/install.sh | sh)
UV := uv

# Python interpreter (UV manages this)
PYTHON := $(UV) run python

# GDL90 library directory
GDL90_DIR := gdl90

help:
	@echo "SkyEcho GDL90 Receiver - Makefile Commands (UV)"
	@echo "================================================"
	@echo "make install       - Install dependencies with UV and clone gdl90"
	@echo "make sync          - Sync dependencies from pyproject.toml"
	@echo "make install-dev   - Install with dev dependencies"
	@echo "make install-gdl90 - Clone gdl90 library from GitHub"
	@echo "make update-gdl90  - Update gdl90 library to latest version"
	@echo "make run           - Run the simple GDL90 receiver (text output)"
	@echo "make run-monitor   - Run the Textual TUI monitor (default)"
	@echo "make run-textual   - Run the Textual TUI monitor (same as run-monitor)"
	@echo "make run-monitor-rich - Run the Rich TUI monitor (legacy)"
	@echo "make clean         - Remove UV cache and Python artifacts"
	@echo "make clean-all     - Remove everything including gdl90 directory"
	@echo "make test          - Run tests (if available)"
	@echo ""
	@echo "First time setup:"
	@echo "  1. Install UV: curl -LsSf https://astral.sh/uv/install.sh | sh"
	@echo "  2. Run: make install"

install: install-gdl90
	@echo "Installing dependencies with UV..."
	$(UV) sync
	@echo ""
	@echo "Installation complete!"
	@echo "Run receiver with: make run"
	@echo "Run monitor with: make run-monitor"

sync:
	@echo "Syncing dependencies from pyproject.toml..."
	$(UV) sync

install-dev: install-gdl90
	@echo "Installing with dev dependencies..."
	$(UV) sync --dev
	@echo "Development environment ready!"

install-gdl90:
	@if [ ! -d "$(GDL90_DIR)" ]; then \
		echo "Cloning gdl90 library from GitHub..."; \
		git clone https://github.com/etdey/gdl90.git $(GDL90_DIR); \
		echo "gdl90 library cloned to $(GDL90_DIR)/"; \
	else \
		echo "gdl90 library already exists at $(GDL90_DIR)/"; \
	fi

update-gdl90:
	@if [ -d "$(GDL90_DIR)" ]; then \
		echo "Updating gdl90 library..."; \
		cd $(GDL90_DIR) && git pull; \
		echo "gdl90 library updated"; \
	else \
		echo "gdl90 library not found. Run 'make install-gdl90' first."; \
		exit 1; \
	fi

run:
	@echo "Running simple receiver with UV..."
	$(PYTHON) skyechogdl.py

run-monitor:
	@echo "Starting Textual TUI monitor with UV..."
	$(PYTHON) skyecho_textual.py

run-monitor-rich:
	@echo "Starting Rich TUI monitor (legacy) with UV..."
	$(PYTHON) skyecho_rich_monitor.py --split

run-monitor-rich-full:
	@echo "Starting Rich TUI monitor (traffic only, legacy) with UV..."
	$(PYTHON) skyecho_rich_monitor.py

run-textual:
	@echo "Starting Textual TUI monitor with UV..."
	$(PYTHON) skyecho_textual.py

clean:
	@echo "Cleaning up UV cache and Python artifacts..."
	rm -rf .uv
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@echo "Cleanup complete!"

clean-all: clean
	@echo "Removing gdl90 library..."
	rm -rf $(GDL90_DIR)
	@echo "Removing old venv..."
	rm -rf .venv
	@echo "Full cleanup complete!"

test:
	@echo "Running tests with UV..."
	$(UV) run pytest

# Development commands
format:
	@echo "Formatting code with black..."
	$(UV) run black .

lint:
	@echo "Linting code with ruff..."
	$(UV) run ruff check .

# UV-specific commands
uv-upgrade:
	@echo "Upgrading all dependencies to latest versions..."
	$(UV) sync --upgrade

uv-lock:
	@echo "Updating uv.lock file..."
	$(UV) lock

uv-info:
	@echo "Python version and location:"
	@$(UV) run python --version
	@$(UV) run which python
	@echo ""
	@echo "Installed packages:"
	@$(UV) pip list