"""Optional Sentry integration.

Activated when `SENTRY_DSN` is set and `PYTHON_ENV` is not a local/dev value.
Keeps the app fully functional when Sentry is not configured.
"""
import logging
import os

logger = logging.getLogger(__name__)

_DEV_ENVS = {"development", "test", "local"}


def init_sentry() -> bool:
    """Initialize Sentry if DSN is configured and env is non-local.

    Returns True when Sentry is enabled, False otherwise.
    """
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False

    env = os.environ.get("PYTHON_ENV", "Production")
    if env.lower() in _DEV_ENVS:
        logger.info("Sentry disabled in %s environment", env)
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except ImportError as exc:
        logger.warning("sentry-sdk not installed, skipping Sentry init: %s", exc)
        return False

    traces_rate = float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1"))
    profiles_rate = float(os.environ.get("SENTRY_PROFILES_SAMPLE_RATE", "0.0"))
    release = os.environ.get("SENTRY_RELEASE") or _probe_release()

    sentry_sdk.init(
        dsn=dsn,
        environment=env,
        release=release,
        integrations=[
            FastApiIntegration(transaction_style="endpoint"),
            StarletteIntegration(transaction_style="endpoint"),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        traces_sample_rate=traces_rate,
        profiles_sample_rate=profiles_rate,
        send_default_pii=False,
    )
    logger.info(
        "Sentry enabled (env=%s release=%s traces=%s)", env, release, traces_rate
    )
    return True


def _probe_release() -> str | None:
    """Best-effort: read version from a VERSION file or pyproject, else None."""
    candidates = [
        ("VERSION", lambda c: c.strip()),
    ]
    for path, parse in candidates:
        try:
            with open(path) as f:
                version = parse(f.read())
                if version:
                    return f"shazamer@{version}"
        except OSError:
            continue
    return None
