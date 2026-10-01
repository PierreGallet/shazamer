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
# `deploy` NE POUSSE PLUS. Pousser et deployer sont deux gestes distincts, et
# l'ordre correct est `git push` puis `make deploy`. Les melanger avait un effet
# de bord concret : le push declenchait la CI, dont le job deploy partait en
# parallele de celui-ci — deux deploiements concurrents sur le meme service.
#
# Deux cibles, aucune duplication : `deploy-without-env` EST l'etape commune, et
# `deploy` se contente de resoudre le .env avant de l'appeler.
#
#   deploy / deploy-with-env   .env resolu depuis 1Password, puis code
#   deploy-without-env         code seul, n'exige pas 1Password
#
# Enchainees par `$(MAKE)` dans la recette et non en prerequis : sous `make -j`
# des prerequis peuvent partir en parallele, et poser le .env pendant que le code
# se deploie rejouerait la panne corrigee en septembre.
deploy: ## Deploiement manuel sur genius (.env resolu depuis 1Password)
	@$(MAKE) env-resolve
	@$(MAKE) deploy-without-env

# Ce commentaire est AU-DESSUS de la cible, pas entre deux lignes de recette, et
# c'est deliberé : un commentaire en colonne 0 n'interrompt PAS une recette
# commencee. Place au milieu, il laissait les lignes suivantes appartenir a la
# cible precedente — `env-resolve` resolvait le .env ET deployait le code, tandis
# que `deploy-without-env` restait une coquille vide qui ne disait rien. `make -n`
# repondait « Nothing to be done » en sortant en 0 : la cible mentait en silence.
#
# L'INSTALLATION DU .env SE FAIT APRES `git reset --hard`, jamais avant.
#
# `.env` est VERSIONNE — c'est la premisse meme de `op inject -i .env`. Le poser
# avant le reset revient a le faire ecraser deux commandes plus loin par la
# version du depot : celle du developpement, references `op://` comprises. Le
# piege est qu'il EST correct entre le scp et le reset, donc l'inspecter juste
# apres l'etape qui le pose ne montre rien.
#
# Le `chmod` et l'installation sont CONDITIONNES a la presence du fichier. Sans
# cela, `deploy-without-env` lance seul echouait sur sa premiere commande — le
# fichier n'existe pas, `chmod` sort en erreur, et le `&&` arretait tout avant
# meme le `git fetch`.
.PHONY: deploy-without-env
# Le .env du serveur est MIS DE COTE avant le `git reset --hard`, et remis apres
# si aucun .env resolu n'a ete depose.
#
# Sans cela, `deploy-without-env` — dont le contrat est justement de ne pas
# toucher au .env du serveur — le DETRUISAIT : `.env` est versionne (c'est la
# premisse de `op inject -i .env`), le reset restaure donc la version du depot,
# celle qui ne porte que des references `op://` non resolues. Et le message
# annoncait « .env du serveur inchange » juste apres l'avoir ecrase.
#
# Ce n'est pas une hypothese : la meme faute, sous une autre forme, a envoye
# tennis_cron en production le 27/09 avec ACCOUNT_1_PASSWORD et CARD_NUMBER
# valant litteralement `op://…`. La reservation du lendemain aurait echoue.
deploy-without-env: ## Deploie le code sans toucher au .env du serveur — n'exige pas 1Password
# MEME ORCHESTRATION QUE LA CI, et pour les memes raisons.
#
# Le verrou est pris AVANT le `git fetch`/`reset`, pas seulement autour de
# scripts/deploy.sh : les deploiements partagent un seul repertoire de travail sur
# le serveur, et le reset hors verrou a produit le 01/10 une image construite
# depuis un contexte qui n'etait plus celui de l'arbre.
#
# La cible est un SHA resolu une fois (`ls-remote`), pas une reference qui bouge
# sous nous. Ecrite avant l'attente, relue apres : si une fusion atterrit pendant
# qu'on patiente, c'est elle qu'on deploie — « le dernier gagne ».
#
# scripts/deploy.sh recoit DEPLOY_LOCK_HELD=1 pour ne pas attendre notre propre
# verrou, et decide lui-meme s'il y a quelque chose a faire.
	ssh $(DEPLOY_HOST) "set -e; \
	  SVC=shazamer_app; \
	  CIBLE=\$$(git -C $(DEPLOY_PATH) ls-remote origin -h refs/heads/main | cut -f1); \
	  printf '%s\\n' \"\$$CIBLE\" > /tmp/deploy-target-\$$SVC; \
	  exec 9>/tmp/deploy-\$$SVC.lock; \
	  if ! flock -n 9; then \
	    echo '>> Un autre deploiement est en cours — on attend son tour.'; \
	    flock -w 2700 9 || { echo '>> ECHEC : verrou non libere en 2700 s.' >&2; exit 75; }; \
	  fi; \
	  CIBLE=\$$(cat /tmp/deploy-target-\$$SVC); \
	  echo \">> Cible : \$${CIBLE:0:7}\"; \
	  cd $(DEPLOY_PATH); \
	  [ -f .env ] && cp -p .env /tmp/env.keep.shazamer || true; \
	  git fetch origin; git reset --hard \"\$$CIBLE\"; \
	  if [ -f /tmp/env.shazamer ]; then \
	    chmod 600 /tmp/env.shazamer; install -m 600 /tmp/env.shazamer .env; \
	    rm -f /tmp/env.shazamer /tmp/env.keep.shazamer; \
	    echo '>> .env resolu installe'; \
	  elif [ -f /tmp/env.keep.shazamer ]; then \
	    install -m 600 /tmp/env.keep.shazamer .env; rm -f /tmp/env.keep.shazamer; \
	    echo '>> .env du serveur preserve a travers le reset'; \
	  else \
	    echo '>> ATTENTION : aucun .env sur le serveur' >&2; exit 1; \
	  fi; \
	  echo \">> env: \$$(grep -c '^[A-Z_]*=' .env) variable(s)\"; \
	  chmod +x scripts/deploy.sh; \
	  DEPLOY_LOCK_HELD=1 bash scripts/deploy.sh"

.PHONY: deploy-with-env
deploy-with-env: deploy   ## Synonyme explicite de `deploy`, quand on veut le nommer

.PHONY: env-resolve
env-resolve:
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
