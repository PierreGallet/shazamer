"""Session handling for the HTTP layer.

The cookie is the credential. Everything here exists to make sure it is hard
to steal, useless if stolen from the database, and long-lived enough that
nobody is asked to sign in again during ordinary use.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request, Response

from src.store.accounts import Accounts, SESSION_TTL

logger = logging.getLogger(__name__)

COOKIE_NAME = "shazamer_session"

# Off only for local HTTP development. Production is behind Traefik on HTTPS,
# and a session cookie sent in clear is a session handed to whoever is on the
# same network.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1").lower() not in ("0", "false", "no")
# Lax, not Strict: Strict drops the cookie when you arrive from a link in the
# email we just sent, so the sign-in appears not to have worked. Lax still
# blocks the cross-site POSTs that matter.
COOKIE_SAMESITE = os.environ.get("COOKIE_SAMESITE", "lax")
COOKIE_DOMAIN = os.environ.get("COOKIE_DOMAIN", "") or None

# There is no switch. Accounts are how one person's library stays theirs, and
# a setting that turns that off is a setting that can be left off — in an
# environment file, by a copied deploy, by anyone who wanted to get past a
# login once and forgot.
#
# It existed as a development convenience: running locally with no SMTP means
# no way to receive a code. That is a real problem and this is not where it
# should be solved — the tests sign in through the same path everything else
# uses, and a developer without mail can mint a session directly against the
# accounts store.
#
# Rows written before accounts existed have an empty owner, and they are still
# reachable: `Library.adopt_orphans` hands them to the first account created.


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,                 # JavaScript must never read this
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        domain=COOKIE_DOMAIN,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", domain=COOKIE_DOMAIN)


class RateLimiter:
    """A fixed window per key, in memory.

    In memory because there is one API container and this guards a mailbox,
    not a bank. If it ever runs on two, the limit doubles — which is a worse
    limit, not a broken one.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: Dict[str, list] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self._hits.get(key, ()) if now - t < self.window]
        # Pruned on write rather than swept: the dict only grows with distinct
        # keys seen inside one window, and this is not a public signup form.
        if len(recent) >= self.limit:
            self._hits[key] = recent
            return False
        recent.append(now)
        self._hits[key] = recent
        return True


# Five codes an hour to one address, and twenty an hour from one caller. The
# first stops a mailbox being used as a nuisance, the second stops someone
# walking a list of addresses to see which ones exist — though the endpoint
# answers identically either way, so there is nothing to learn from doing it.
email_limiter = RateLimiter(limit=5, window_seconds=3600)
ip_limiter = RateLimiter(limit=20, window_seconds=3600)
# Verification is limited too. The code itself allows five wrong guesses, but
# without this a caller could ask for a fresh code and guess five more, for
# ever.
verify_limiter = RateLimiter(limit=20, window_seconds=3600)


def client_key(request: Request) -> str:
    """Best available identifier for the caller.

    Behind Traefik the socket address is the proxy, so the forwarded header is
    used when present. It is client-controlled and therefore not trustworthy
    for anything but rate limiting, where the worst case is that a determined
    caller gets a fresh bucket.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def make_dependencies(accounts: Accounts):
    """Build the request dependencies, bound to one Accounts store.

    A factory rather than module-level globals so the tests can point it at a
    temporary database without reaching into module state.
    """

    async def current_user_optional(request: Request) -> Optional[Dict[str, Any]]:
        token = request.cookies.get(COOKIE_NAME, "")
        return await accounts.user_for_session(token)

    async def current_user(
        user: Optional[Dict[str, Any]] = Depends(current_user_optional),
    ) -> Dict[str, Any]:
        if user is None:
            raise HTTPException(status_code=401, detail="Sign in to continue")
        return user

    return current_user_optional, current_user
