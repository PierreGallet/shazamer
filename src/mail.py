"""Sending the one-time login code.

SMTP rather than a provider SDK. Every transactional service worth using —
Resend, Postmark, Mailgun, SES — offers an SMTP endpoint, as does Gmail with
an app password, so one implementation covers whichever you pick and switching
is a change of environment variables rather than of code.

With nothing configured the code is written to the log instead. That is the
right behaviour for local work, and it is *not* a quiet fallback in
production: `configured()` is false, and the API refuses to hand out codes
rather than pretending to have sent one.
"""
from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

logger = logging.getLogger(__name__)

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER)
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "Shazamer")
# Implicit TLS on connect (port 465) versus STARTTLS after (587). Providers
# offer both; getting it wrong hangs rather than failing, so it is explicit.
SMTP_SSL = os.environ.get("SMTP_SSL", "").lower() in ("1", "true", "yes")
# STARTTLS on port 587, which is what every hosted provider wants and what the
# default should be — a login code crossing the internet in clear is a login
# code anyone on the path can use. Set to 0 only for a relay on this same
# machine, where there is no network to eavesdrop on.
SMTP_STARTTLS = os.environ.get("SMTP_STARTTLS", "1").lower() not in ("0", "false", "no")
SMTP_TIMEOUT = float(os.environ.get("SMTP_TIMEOUT", "20"))


def configured() -> bool:
    return bool(SMTP_HOST and MAIL_FROM)


class MailError(RuntimeError):
    """Delivery failed. Surfaced to the caller rather than swallowed."""


def _build(to: str, code: str, minutes: int) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"{code} is your Shazamer sign-in code"
    message["From"] = formataddr((MAIL_FROM_NAME, MAIL_FROM))
    message["To"] = to
    # Tells well-behaved clients not to file this under a previous thread, and
    # keeps a sign-in code out of conversation view.
    message["X-Entity-Ref-ID"] = code

    message.set_content(
        f"Your sign-in code is {code}\n\n"
        f"It works once and expires in {minutes} minutes.\n\n"
        "If you did not ask to sign in to Shazamer, you can ignore this — "
        "someone typed your address by mistake, and nothing has happened to "
        "your account.\n"
    )
    # A plain-text part is sent as well, above, so this degrades cleanly.
    message.add_alternative(
        "<div style=\"font-family:-apple-system,BlinkMacSystemFont,"
        "'Segoe UI',sans-serif;max-width:420px;color:#1a1714\">"
        "<p style=\"font-size:15px;margin:0 0 20px\">Your sign-in code:</p>"
        f"<p style=\"font-family:ui-monospace,SFMono-Regular,Menlo,monospace;"
        f"font-size:34px;font-weight:700;letter-spacing:0.16em;"
        f"margin:0 0 20px;color:#ff5500\">{code}</p>"
        f"<p style=\"font-size:14px;color:#675f58;margin:0 0 16px\">"
        f"It works once and expires in {minutes} minutes.</p>"
        "<p style=\"font-size:13px;color:#675f58;margin:0\">"
        "If you did not ask to sign in to Shazamer, you can ignore this — "
        "someone typed your address by mistake, and nothing has happened to "
        "your account.</p></div>",
        subtype="html")
    return message


def _build_share(to: str, from_name: str, title: str, link: str,
                 tracks: int) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"{from_name} shared a tracklist with you"
    message["From"] = formataddr((MAIL_FROM_NAME, MAIL_FROM))
    message["To"] = to

    message.set_content(
        f"{from_name} shared a tracklist with you on Shazamer:\n\n"
        f"  {title}\n"
        f"  {tracks} tracks\n\n"
        f"{link}\n\n"
        "Opening it puts a copy in your own library — yours to keep, star and "
        "delete, whatever they do with theirs.\n"
    )
    message.add_alternative(
        "<div style=\"font-family:-apple-system,BlinkMacSystemFont,"
        "'Segoe UI',sans-serif;max-width:460px;color:#1a1714\">"
        f"<p style=\"font-size:15px;margin:0 0 6px\">"
        f"<strong>{from_name}</strong> shared a tracklist with you.</p>"
        f"<p style=\"font-size:19px;font-weight:700;margin:0 0 2px\">{title}</p>"
        f"<p style=\"font-size:13px;color:#675f58;margin:0 0 22px\">"
        f"{tracks} tracks</p>"
        f"<p style=\"margin:0 0 22px\"><a href=\"{link}\" "
        "style=\"display:inline-block;background:#ff5500;color:#fff;"
        "text-decoration:none;padding:11px 18px;border-radius:8px;"
        "font-weight:600;font-size:14px\">Open the tracklist</a></p>"
        "<p style=\"font-size:13px;color:#675f58;margin:0\">"
        "Opening it puts a copy in your own library — yours to keep, star and "
        "delete, whatever they do with theirs.</p></div>",
        subtype="html")
    return message


async def send_share(to: str, from_name: str, title: str, link: str,
                     tracks: int) -> None:
    """Tell someone a tracklist is waiting for them."""
    if not configured():
        logger.warning("SMTP is not configured — share link for %s is %s",
                       to, link)
        return
    message = _build_share(to, from_name, title, link, tracks)
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, _send_blocking, message)
    except Exception as exc:                   # noqa: BLE001 - reported below
        logger.error("Could not send a share to %s: %s", to, exc)
        raise MailError(str(exc)) from exc


def _send_blocking(message: EmailMessage) -> None:
    context = ssl.create_default_context()
    if SMTP_SSL:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT,
                                  timeout=SMTP_TIMEOUT, context=context)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT)
    try:
        if not SMTP_SSL and SMTP_STARTTLS:
            server.starttls(context=context)
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:                      # noqa: BLE001 - already sent
            server.close()


async def send_login_code(to: str, code: str, minutes: int = 10) -> None:
    """Deliver a code, or raise MailError.

    Run in a thread: smtplib is blocking, and a slow relay would otherwise
    stall every other request in the process — including the analysis
    reporting its progress.
    """
    if not configured():
        # Never at INFO in production, because this prints a live credential.
        # It is here so local development works with no configuration at all.
        logger.warning("SMTP is not configured — login code for %s is %s",
                       to, code)
        return

    message = _build(to, code, minutes)
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, _send_blocking, message)
    except Exception as exc:                   # noqa: BLE001 - reported below
        # The address is logged, the code never is.
        logger.error("Could not send a login code to %s: %s", to, exc)
        raise MailError(str(exc)) from exc
