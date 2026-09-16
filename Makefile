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
# `op whoami` en plus de la presence du binaire : sans lui, un coffre
# VERROUILLE faisait echouer la cible au lieu de la degrader. `op run` sort en
# erreur des qu'il n'est pas connecte, et la commande ne tournait pas du tout.
#
# Teste le 2026-09-06 : sans session, `op run -- echo` n'affiche que
# « You are not currently signed in » et n'execute rien.
# `op account list` et non `op whoami`.
#
# `op whoami` echoue avec « account is not signed in » des lors qu'on passe par
# l'integration avec l'app de bureau : celle-ci deverrouille a chaque appel
# (biometrie) et ne cree JAMAIS de jeton de session CLI. Le garde-fou refusait
# donc de demarrer un environnement parfaitement sain — pire que le probleme
# qu'il previent. Verifie sur la machine de Pierre le 2026-09-10 :
# `op whoami` -> ECHEC, `op account list` -> OK, et `op run` fonctionne.
#
# La sonde ne doit RIEN lire dans le coffre non plus : `op run -- true`
# resoudrait toutes les references du .env a chaque invocation de make, soit une
# rafale de demandes TouchID sur `make help` comme sur `make run`. Une sonde qui
# coute une authentification par appel est inutilisable.
#
# `op account list` repond depuis la config locale : aucune biometrie, et elle
# distingue exactement le cas vise — « le CLI ne voit aucun compte ». Elle ne
# prouve pas le deverrouillage, et c'est voulu : `op run` demandera la biometrie
# une seule fois, quand la cible en a reellement besoin.
OP := $(shell command -v op >/dev/null 2>&1 && op account list --format=json 2>/dev/null | grep -q '"url"' && echo "op run --env-file=.env --")

# Refuser de demarrer plutot que de demarrer FAUX.
#
# La garde ci-dessus se contentait de vider $(OP) : la cible tournait, sans
# dechiffrement, sans le dire. Les valeurs `op://` arrivaient alors dans
# l'application comme des chaines litterales, et le symptome n'apparaissait que
# bien plus loin — « Failed to decrypt openai_api_key », une liste de modeles
# vide — sans rien qui designe la vraie cause. Deux allers-retours de debogage
# pour ca le 2026-09-07, sur une cible qui affichait pourtant un demarrage
# normal.
#
# Le test ne se declenche QUE si le .env porte reellement des references : un
# .env entierement en clair continue de tourner sans op, donc rien ne se
# complique pour qui n'utilise pas le coffre.
define require_op
@if [ -z "$(OP)" ] && grep -qs '^[A-Za-z_][A-Za-z0-9_]*=op://' .env; then \
	echo ""; \
	echo "  X  1Password injoignable : les valeurs op:// du .env NE SERAIENT PAS resolues."; \
	echo ""; \
	echo "     Verifie d'abord :  op account list"; \
	echo "     Une liste VIDE veut dire que le CLI ne voit aucun compte — l'app de"; \
	echo "     bureau ne l'expose pas. 1Password > Reglages > Developpeur >"; \
	echo "     « Integrer avec le CLI 1Password ». C'est le seul reglage qui vaut"; \
	echo "     pour TOUS les terminaux ; un 'op signin' ne vaut que pour le shell"; \
	echo "     ou tu l'as lance, pas pour celui d'un agent ou d'un autre onglet."; \
	echo ""; \
	exit 1; \
fi
endef

PORT ?= 8000

help:
	@echo "Shazamer — DJ digging station"
	@echo ""
	@echo "  make install        Install Python + frontend dependencies"
	@echo "  make run            Run API (:8000) and Vite dev server (:5173)"
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
	$(require_op)
	@$(OP) $(VENV) -m arq src.jobs.worker.WorkerSettings

# Two processes: the API, and Vite with hot reload proxying /api to it.
#
# `run` and not `dev`, to match triton, noctambule and lecrapaud — every other
# repo here starts the same way. `dev` stays as an alias: it is what the README,
# the docs site and muscle memory have said for months, and breaking it would
# cost more than the line it takes to keep.
run:
	@echo "API on http://localhost:$(PORT) · UI on http://localhost:5173"
	$(require_op)
	@$(OP) $(VENV) -m uvicorn src.web:app --reload --port $(PORT) & \
	 cd web && npm run dev; \
	 kill %1 2>/dev/null || true

# Alias historique.
dev: run

web: build
	$(require_op)
	@$(OP) $(VENV) -m uvicorn src.web:app --host 0.0.0.0 --port $(PORT)

analyze:
	@test -n "$(FILE)" || { echo 'Usage: make analyze FILE="path/to/mix.mp3"'; exit 1; }
	$(require_op)
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


DEPLOY_HOST ?= genius
DEPLOY_PATH ?= /home/sharon/shazamer

.PHONY: deploy
deploy: ## Deploiement manuel sur genius — memes etapes que la CI
# Sert quand le quota GitHub Actions est epuise : la CI ne tourne alors pas et
# cette cible fait le meme travail qu'elle, .env compris.
#
# ATTENTION quand le quota est disponible : le `git push` ci-dessous DECLENCHE
# aussi la CI, dont le job deploy s'execute sur push main. Deux deploiements
# concurrents sur le meme service.
	git push origin main
	@command -v op >/dev/null || { echo "1Password CLI absent — impossible de resoudre le .env."; exit 1; }
# `op inject` resout les references `op://` du .env VERSIONNE, qui reste ainsi
# l'unique endroit ou une variable d'execution est declaree.
#
# `sed` et non un ajout en fin de fichier : la CI fait la meme substitution en
# place, ce qui evite un PYTHON_ENV en double dont seule la derniere occurrence
# compterait — un fichier de production doit pouvoir se lire.
#
# Le garde-fou `tail -c1` couvre un .env qui ne finirait pas par un saut de
# ligne : la substitution n'en depend pas, mais tout ajout ulterieur si, et le
# cas s'est deja produit ailleurs (une cle collee a la valeur precedente, donc
# perdue en silence).
#
# TOUT tient dans UN SEUL shell (les `\` en fin de ligne) : chaque ligne d'une
# recette tourne sinon dans son propre shell, et `$$$$` y rendrait un PID
# different a chaque ligne — le fichier ecrit ne porterait deja plus le meme nom
# a l'etape `scp`. Le `trap` garantit qu'il disparait meme en cas d'echec.
	@set -e; \
	  TMP=$$(mktemp /tmp/env.shazamer.XXXXXX); \
	  trap 'rm -f "$$TMP"' EXIT INT TERM; \
	  echo ">> Resolution du .env depuis 1Password"; \
	  op inject -i .env -f -o "$$TMP"; \
	  if [ -n "$$(tail -c1 "$$TMP")" ]; then printf '\n' >> "$$TMP"; fi; \
	  sed -i.bak 's/^PYTHON_ENV=.*/PYTHON_ENV=Production/' "$$TMP"; rm -f "$$TMP.bak"; \
	  chmod 600 "$$TMP"; \
	  scp -q "$$TMP" $(DEPLOY_HOST):/tmp/env.shazamer; \
	  echo ">> .env resolu depose sur le serveur (PYTHON_ENV=Production)"
# Le .env est installe APRES `git reset --hard`, dans le MEME ssh, parce que
# `.env` est VERSIONNE — c'est la premisse meme de `op inject -i .env`. Pose
# avant, le reset le remplacerait par la version du depot : celle du
# developpement, references `op://` non resolues comprises. La CI ne s'y trompe
# pas non plus, son `install` vient apres ses operations git.
	ssh $(DEPLOY_HOST) "chmod 600 /tmp/env.shazamer \
	  && cd $(DEPLOY_PATH) \
	  && git fetch origin && git reset --hard origin/main \
	  && install -m 600 /tmp/env.shazamer .env && rm -f /tmp/env.shazamer \
	  && echo \">> env: \$$(grep -c '^[A-Z_]*=' .env) variable(s)\" \
	  && bash scripts/deploy.sh"
