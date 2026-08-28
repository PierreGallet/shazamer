#!/usr/bin/env python3
"""Hand the library that predates accounts to one address.

Accounts arrived after the library did, so every set, starred track, followed
channel and download made before then has no owner. With accounts switched off
that is fine — the app runs as the ownerless user and everything is visible.
The moment accounts are switched on, those rows belong to nobody and vanish
from every query.

Signing in for the first time adopts them automatically. This exists for the
case where you would rather not depend on that: it creates the account up
front, so the data is already yours before the login screen appears, and so
turning accounts on is a change of one environment variable rather than a
change plus a hope.

Safe to run more than once. Rows already owned are left alone, and an account
that exists is reused rather than duplicated.

    python scripts/claim_library.py pierre.gallet@hotmail.fr
    python scripts/claim_library.py pierre.gallet@hotmail.fr --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.store.accounts import (Accounts, _iso, _now, looks_like_email,  # noqa: E402
                                normalise_email)
from src.store.library import Library  # noqa: E402

OWNED_TABLES = ("sets", "crate", "watches", "downloads")


def count_unowned(path: Path) -> dict:
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        out = {}
        for table in OWNED_TABLES:
            columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if "user_id" not in columns:
                out[table] = 0     # migration has not run here yet
                continue
            out[table] = conn.execute(
                f"SELECT COUNT(*) FROM {table}"
                " WHERE user_id IS NULL OR user_id = ''").fetchone()[0]
        return out


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    parser.add_argument("--data-dir", default=None,
                        help="Where the databases live (default: ./data)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would move, change nothing")
    parser.add_argument("--i-know", dest="force", action="store_true",
                        help="Claim even with accounts switched off, which "
                             "hides the library until they are on")
    args = parser.parse_args()

    email = normalise_email(args.email)
    if not looks_like_email(email):
        print(f"Not an email address: {args.email}", file=sys.stderr)
        return 2

    root = Path(args.data_dir) if args.data_dir else Path("data")
    library_path = root / "library.db"
    if not library_path.exists():
        print(f"No library at {library_path}", file=sys.stderr)
        return 2

    # Constructing these runs their migrations, which is what puts the
    # user_id columns there in the first place.
    library = Library(library_path)
    accounts = Accounts(root / "accounts.db")

    before = count_unowned(library_path)
    total = sum(before.values())
    print(f"Library: {library_path}")
    for table, n in before.items():
        print(f"  {table:10} {n:5} row(s) with no owner")
    print(f"  {'total':10} {total:5}")
    print()

    if args.dry_run:
        print(f"Dry run — would give all of it to {email}.")
        return 0

    # Claiming while accounts are off makes the library disappear, and I did
    # exactly that to a live site: the rows become owned, the app is still
    # running as the ownerless user, and every query returns nothing. Nothing
    # is lost and the fix is one UPDATE, but the site looks empty until
    # somebody notices.
    #
    # So the two changes belong together. Order them the other way — claim,
    # then switch on — and there is a window with no library; switch on first
    # and there is a window where the first person to sign in adopts it, which
    # is fine but is not what this script is for.
    import os
    if os.environ.get("AUTH_ENABLED", "").lower() not in ("1", "true", "yes"):
        print("Refusing: AUTH_ENABLED is not set.\n"
              "\n"
              "With accounts off the app runs as the ownerless user, so\n"
              "claiming these rows would hide them from it — the library\n"
              "would read as empty until accounts are switched on.\n"
              "\n"
              "Set AUTH_ENABLED=1 (and SMTP, or nobody can sign in), deploy,\n"
              "then run this. Or pass --i-know to do it anyway.",
              file=sys.stderr)
        if not args.force:
            return 3
        print("--i-know given; claiming anyway.", file=sys.stderr)

    user = await find_or_create(accounts, email)
    print(f"Account {email} -> id {user['id']}")

    moved = await library.adopt_orphans(user["id"])
    after = sum(count_unowned(library_path).values())
    print(f"Moved {moved} row(s). {after} still unowned.")

    if after:
        print("  (rows added since this started, or a table without the "
              "column yet)", file=sys.stderr)
    return 0


async def find_or_create(accounts: Accounts, email: str) -> dict:
    """The account for `email`, created if it does not exist.

    Created directly rather than through the login flow: that flow exists to
    prove someone can read the inbox, and there is nobody here to prove it to.
    The account is empty until they sign in, and signing in is still the only
    way to get a session.
    """
    def _sync():
        with closing(accounts._connect()) as conn:
            row = conn.execute("SELECT id, email FROM users WHERE email = ?",
                               (email,)).fetchone()
            if row is not None:
                return {"id": row["id"], "email": row["email"]}
            import secrets
            user_id = secrets.token_hex(8)
            now = _iso(_now())
            conn.execute(
                "INSERT INTO users (id, email, created_at, last_seen)"
                " VALUES (?, ?, ?, NULL)", (user_id, email, now))
            conn.commit()
            return {"id": user_id, "email": email}

    return await accounts._run(_sync)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
