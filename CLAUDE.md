# shazamer — notes pour les agents

Station de digging DJ : API FastAPI + worker arq, frontend Vite. Déployé en Swarm
derrière Traefik (infra genius).

## Base de dev/test — API uniforme (RFC genius #18)

**Golden rule.** Toute base de dev/test se monte/détruit avec `make db-dev-up` /
`db-test-up` / `db-test-down` ; seed via `seed-dev` / `seed-test` ; env complet
avec `dev-up` / `dev-down`. Garde-fou hôte local + nom sur les cibles
destructrices.

**Cas particulier shazamer : PAS de serveur SQL.** L'état applicatif vit dans des
**fichiers SQLite** sous `SHAZAMER_DATA_DIR` (`accounts.db`, `library.db`,
`soulseek_quota.db` — schéma auto-créé à l'ouverture, rien à migrer). Redis 7
n'est que le **broker arq** du worker (optionnel : sans `REDIS_URL` l'API analyse
en direct), pas un magasin de données applicatif. Donc :

- `db-dev-up` / `db-test-up` / `db-test-down` **n'ont aucun sens relationnel**
  (pas de moteur en loopback, pas de port, pas de `CREATE DATABASE`). On les
  garde pour l'uniformité de l'API, ré-interprétés pour un magasin de fichiers :
  ici **une « base » = un répertoire de données**.
  - `db-dev-up` ≡ `seed-dev` (assure le répertoire de dev + compte loggable).
  - `db-test-up [SESSION=…]` crée un **répertoire SQLite jetable** isolé
    `tmp/dbtest-<session>/`, le seed, et imprime `SHAZAMER_DATA_DIR=…`.
  - `db-test-down [SESSION=…]` supprime **ce** répertoire (refus si hors `./tmp`
    ou nom sans `test`).
- **Pas d'éphémère Redis par session** : Redis ne porte pas l'état applicatif
  (les tests tournent en direct, sans Redis). L'isolation par session passe par
  le répertoire SQLite jetable ci-dessus.

**Compte loggable — passwordless.** Pas de mot de passe : l'auth est un code à 6
chiffres → cookie de session (`shazamer_session`). `seed-dev`
(`scripts/seed_dev.py`) ne pose donc pas de mot de passe, il **frappe un cookie
de session** directement contre `accounts.db` (comme `tests/conftest.py` et comme
`src/auth.py` l'autorise) pour `pierre.gallet@hotmail.fr`, et imprime le cookie à
poser dans le navigateur. Aucun secret `op` requis : le magasin de comptes est du
SQLite pur.

**`dev-up` / `dev-down`.** `dev-up` lance API + Vite en tâche de fond **sans
secrets `op`** (SMTP/Sentry restent simplement absents) et seed le compte
loggable ; `dev-down` arrête **par pidfile, jamais par motif**.
