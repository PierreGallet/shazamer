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
	@echo "Creating virtual environment..."
	@python3.12 -m venv venv
	@echo "Installing dependencies..."
	@./venv/bin/pip install --upgrade pip
	@./venv/bin/pip install -r requirements.txt
	@echo "Installation complete!"

# Clean up
clean:
	@echo "Cleaning up..."
	@rm -rf venv
	@rm -rf outputs
	@rm -f temp_segment_*.wav
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
	@./venv/bin/python shazamer.py "$(FILE)"

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
	@./venv/bin/python shazamer.py "$@"