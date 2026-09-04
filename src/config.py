"""Configuration that is not secret.

The split this module exists to enforce: a SECRET is a value whose disclosure
grants access — a token, a key, a password. Everything else is CONFIGURATION,
and configuration belongs in the repository where it can be read, reviewed and
diffed without opening a vault.

Blurring the two has a cost that shows up later. A .env where nothing can be
read becomes a file nobody reads, and a real secret slipped in among twenty
harmless values gets the same careless treatment as the rest.

Values here are still overridable by environment, so a deploy or a local
experiment can change them without editing code.
"""

import os


def _env() -> str:
    """The deployment environment.

    Defaults to Development, and that direction matters: an oversight yields a
    dev configuration, never a production stack quietly running with dev
    settings. Production is set explicitly by the deploy job.
    """
    return os.environ.get("PYTHON_ENV", "Development")


IS_PRODUCTION = _env().lower() in ("production", "prod")

# Where this instance lives, for links that leave the app — invitation emails
# in particular.
#
# Not derived from the request's Host header: anyone able to reach the API
# could then mint an invitation pointing at a site they control. A fixed value
# per environment is the whole defence.
PUBLIC_URL = os.environ.get(
    "PUBLIC_URL",
    "https://shazamer.pierregallet.com" if IS_PRODUCTION else "http://localhost:5173",
).rstrip("/")

# Where the deploy job checks the repository out on genius. Read by the CI
# workflow, not by the application — kept here so there is one place to look.
DEPLOY_PATH = os.environ.get("DEPLOY_PATH", "/home/sharon/shazamer")
