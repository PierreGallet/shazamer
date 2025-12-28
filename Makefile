.PHONY: help install clean analyze

# Default target
help:
	@echo "Shazamer - DJ Set & Playlist Analyzer"
	@echo ""
	@echo "Usage:"
	@echo "  make install              - Install dependencies in virtual environment"
	@echo "  make <audio_file>         - Analyze an audio file (e.g., make song.mp3)"
	@echo "  make analyze FILE=<path>  - Alternative way to analyze a file"
	@echo "  make clean                - Remove virtual environment and output files"
	@echo ""
	@echo "Examples:"
	@echo "  make install"
	@echo "  make ~/Music/dj_set.mp3"
	@echo "  make analyze FILE=\"/path/to/my mix.mp3\""

# Install dependencies
install:
	@echo "Checking system dependencies..."
	@# Check and install Homebrew on macOS
	@if [ "$$(uname)" = "Darwin" ]; then \
		if ! command -v brew >/dev/null 2>&1; then \
			echo "Homebrew not found. Installing Homebrew..."; \
			/bin/bash -c "$$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"; \
			echo "Homebrew installed. You may need to add it to your PATH:"; \
			echo "  echo 'eval \"$$($(brew --prefix)/bin/brew shellenv)\"' >> ~/.zprofile"; \
			echo "  eval \"$$($(brew --prefix)/bin/brew shellenv)\""; \
			eval "$$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"; \
		fi; \
	fi
	@echo "Checking for Python 3.12..."
	@if ! command -v python3.12 >/dev/null 2>&1; then \
		echo "Python 3.12 not found. Installing..."; \
		if [ "$$(uname)" = "Darwin" ]; then \
			echo "Installing Python 3.12 via Homebrew..."; \
			brew install python@3.12; \
		elif command -v apt-get >/dev/null 2>&1; then \
			echo "Installing Python 3.12 via apt..."; \
			sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv python3.12-pip; \
		elif command -v yum >/dev/null 2>&1; then \
			echo "Installing Python 3.12 via yum..."; \
			sudo yum install -y python3.12 python3.12-venv python3.12-pip; \
		else \
			echo "Error: Cannot automatically install Python 3.12. Please install manually."; \
			echo "Visit https://www.python.org/downloads/ for installation instructions."; \
			exit 1; \
		fi; \
	fi
	@echo "Checking for uv package manager..."
	@if ! command -v uv >/dev/null 2>&1; then \
		echo "uv not found. Installing..."; \
		if [ "$$(uname)" = "Darwin" ]; then \
			echo "Installing uv via Homebrew..."; \
			brew install uv; \
		else \
			echo "Installing uv via curl..."; \
			curl -LsSf https://astral.sh/uv/install.sh | sh; \
			export PATH="$$HOME/.cargo/bin:$$PATH"; \
		fi; \
	fi
	@echo "Creating virtual environment with Python 3.12..."
	@uv venv venv --python python3.12
	@echo "Installing dependencies with uv..."
	@uv pip sync requirements.txt --python venv/bin/python
	@echo "Installation complete!"

# Clean up
clean:
	@echo "Cleaning up..."
	@rm -rf venv
	@rm -rf outputs
	@rm -rf tmp
	@echo "Clean complete!"

# Analyze with FILE variable
analyze:
	@if [ -z "$(FILE)" ]; then \
		echo "Error: Please specify a file to analyze"; \
		echo "Usage: make analyze FILE=\"path/to/audio.mp3\""; \
		exit 1; \
	fi
	@if [ ! -f "venv/bin/python" ]; then \
		echo "Virtual environment not found. Running 'make install' first..."; \
		$(MAKE) install; \
	fi
	@echo "Analyzing: $(FILE)"
	@uv run python shazamer.py "$(FILE)"

# Force rebuild for audio files
.PHONY: %.mp3 %.wav %.flac %.m4a

# Pattern rule for direct file analysis
%.mp3 %.wav %.flac %.m4a:
	@if [ ! -f "$@" ]; then \
		echo "Error: File not found: $@"; \
		exit 1; \
	fi
	@if [ ! -f "venv/bin/python" ]; then \
		echo "Virtual environment not found. Running 'make install' first..."; \
		$(MAKE) install; \
	fi
	@echo "Analyzing: $@"
	@uv run python shazamer.py "$@"