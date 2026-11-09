#!/usr/bin/env python3
"""Fabrique le compte loggable de dev/test de shazamer.

shazamer est **passwordless** : on ne se connecte pas avec un mot de passe mais
avec un code a six chiffres recu par email, echange contre un cookie de session
(`shazamer_session`). En local sans SMTP, aucun code n'arrive — donc on ne
*fabrique pas un mot de passe*, on **frappe un cookie de session directement**
contre le magasin de comptes, exactement comme le fait la suite de tests
(`tests/conftest.py::_seed_account` + `Accounts.create_session`) et comme le
docstring de `src/auth.py` l'autorise explicitement :

    « a developer without mail can mint a session directly against the
      accounts store. »

Il n'y a **aucun secret** ici : le magasin de comptes est un simple fichier
SQLite (`<data>/accounts.db`), ni `op` ni variable sensible ne sont requis.

Sortie : le nom et la valeur du cookie a poser dans le navigateur pour etre
connecte en tant que `pierre.gallet@hotmail.fr`, sans jamais taper de code.
"""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import closing
from pathlib import Path

# Rendre `src` importable quel que soit le CWD d'appel (make lance depuis la
# racine, mais restons robustes).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.store.accounts import Accounts  # noqa: E402

# Source de verite : src/auth.py::COOKIE_NAME. Copie ici a dessein pour que le
# seed ne depende que de la stdlib (src.store.accounts) et tourne AVANT meme que
# `make install` ait pose fastapi — importer src.auth tirerait fastapi.
COOKIE_NAME = "shazamer_session"

DATA_DIR = Path(os.environ.get("SHAZAMER_DATA_DIR") or "data")
EMAIL = os.environ.get("SEED_EMAIL", "pierre.gallet@hotmail.fr")
# Un id FIXE (pas un id aleatoire mint par `Accounts`) : rejouer `seed-dev`
# doit retomber sur le meme compte, pas en empiler un nouveau a chaque fois.
USER_ID = os.environ.get("SEED_USER_ID", "dev-pierre")
FIRST = os.environ.get("SEED_FIRST_NAME", "Pierre")
LAST = os.environ.get("SEED_LAST_NAME", "Gallet")


async def main() -> int:
    accounts = Accounts(DATA_DIR / "accounts.db")

    # Compte a un id choisi — comme conftest, droit dans la table : `Accounts`
    # n'expose pas « cree un user avec cet id exact » (rien dans l'app n'en a
    # besoin, seuls le seed et les tests).
    def write() -> None:
        with closing(accounts._connect()) as conn:
            conn.execute(
                "INSERT INTO users (id, email, first_name, last_name, created_at)"
                " VALUES (?, ?, ?, ?, datetime('now'))"
                " ON CONFLICT(id) DO UPDATE SET email = excluded.email,"
                " first_name = excluded.first_name, last_name = excluded.last_name",
                (USER_ID, EMAIL, FIRST, LAST))
            conn.commit()

    await accounts._run(write)
    token = await accounts.create_session(USER_ID, "seed-dev")

    # Verification immediate : le cookie frappe doit resoudre vers le compte.
    who = await accounts.user_for_session(token)
    assert who and who["email"] == EMAIL, "la session frappee ne se resout pas"

    public_url = os.environ.get("PUBLIC_URL", "http://localhost:5173")
    print("")
    print("  Compte loggable pret (passwordless → cookie de session)")
    print(f"    email        {EMAIL}")
    print(f"    data dir     {DATA_DIR}/accounts.db")
    print(f"    cookie       {COOKIE_NAME}={token}")
    print("")
    print("  Pour te connecter sans code email, pose ce cookie sur le domaine")
    print(f"  local (http, COOKIE_SECURE=0), puis ouvre {public_url} :")
    print("")
    print(f"    document.cookie = '{COOKIE_NAME}={token}; path=/'")
    print("")
    # Ligne machine-parsable pour Chrome headless / un agent (AC-3.1).
    print(f"SHAZAMER_SESSION_COOKIE={COOKIE_NAME}={token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
