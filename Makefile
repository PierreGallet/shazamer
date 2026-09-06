.PHONY: help install install-web install-docs dev web web-build build docs docs-dev test test-integration clean lint

VENV := venv/bin/python

# Résout les références 1Password du .env et lance la commande avec les vraies
# valeurs dans son environnement — sans jamais les écrire sur le disque.
#
# Nécessaire parce que rien ne charge le .env autrement : le code lit
# os.environ et pas un fichier, et en production c'est docker-stack.yml qui
# fournit les variables. En local, sans ce préfixe, SMTP et Sentry sont
# simplement absents — ou pire, valent la chaîne « op://… » si le fichier est
# chargé à la main, ce qui ressemble à une valeur sans en être une.
#
# Dégradation volontaire si `op` n'est pas installé ou pas déverrouillé : la
# commande tourne quand même, sans les secrets. Contribuer au frontend ou
# lancer les tests ne doit pas exiger un coffre.
OP := $(shell command -v op >/dev/null 2>&1 && echo "op run --env-file=.env --")
PORT ?= 8000

help:
	@echo "Shazamer — DJ digging station"
	@echo ""
	@echo "  make install        Install Python + frontend dependencies"
	@echo "  make dev            Run API (:8000) and Vite dev server (:5173)"
	@echo "  make web            Run the production server (built frontend)"
	@echo "  make worker         Run the analysis worker (needs REDIS_URL)"
	@echo "  make build          Build the frontend into web/dist"
	@echo "  make docs           Build the documentation into docs-site/build"
	@echo "  make docs-dev       Run the documentation with hot reload (:3000)"
	@echo "  make test           Run the test suite"
	@echo "  make analyze FILE=… Analyse a file from the command line"
	@echo "  make clean          Remove venv, build output and caches"

install: install-py install-web install-docs

install-py:
	@command -v ffmpeg >/dev/null 2>&1 || { \
		echo "ffmpeg is required. Install it:"; \
		echo "  macOS:  brew install ffmpeg"; \
		echo "  Debian: sudo apt-get install ffmpeg"; \
		exit 1; }
	@command -v uv >/dev/null 2>&1 || pip install uv
	@uv venv venv --python python3.12
	@uv pip install -r requirements.txt --python venv/bin/python
	@echo "Python environment ready."

install-web:
	@cd web && npm install --no-audit --no-fund
	@echo "Frontend dependencies ready."

install-docs:
	@cd docs-site && npm install --no-audit --no-fund
	@echo "Docs dependencies ready."

build:
	@cd web && npm run build

# The app serves this at /docs, so `make web` shows the real thing only after
# this has run at least once. The Docker image builds it in its own stage.
docs:
	@cd docs-site && npm run build

docs-dev:
	@cd docs-site && npm start

# The analysis worker. Needs REDIS_URL; without one the API runs analyses
# itself and this is unnecessary.
worker:
	@$(OP) $(VENV) -m arq src.jobs.worker.WorkerSettings

# Two processes: the API, and Vite with hot reload proxying /api to it.
dev:
	@echo "API on http://localhost:$(PORT) · UI on http://localhost:5173"
	@$(OP) $(VENV) -m uvicorn src.web:app --reload --port $(PORT) & \
	 cd web && npm run dev; \
	 kill %1 2>/dev/null || true

web: build
	@$(OP) $(VENV) -m uvicorn src.web:app --host 0.0.0.0 --port $(PORT)

analyze:
	@test -n "$(FILE)" || { echo 'Usage: make analyze FILE="path/to/mix.mp3"'; exit 1; }
	@$(OP) $(VENV) -m src.shazamer "$(FILE)"

test:
	@$(VENV) -m pytest -q -m "not integration"

test-integration:
	@$(VENV) -m pytest -q -m integration

lint:
	@cd web && npm run typecheck

clean:
	@rm -rf venv web/node_modules web/dist tmp .pytest_cache \
	        docs-site/node_modules docs-site/build docs-site/.docusaurus
	@find . -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "Cleaned."
