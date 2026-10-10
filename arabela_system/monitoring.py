"""Error alerts: when the live site breaks for a visitor, you get an email with the exact page and line (Sentry, free plan).

Off by default: nothing happens unless the SENTRY_DSN setting (an address Sentry gives you) is present, so local development and the
tests never send anything. Switch it on by adding SENTRY_DSN to Render's Environment page -- see README section 14.

What is sent is kept small on purpose: the error, where it happened, and which version of the code was running. NOT the people --
no names, emails or IP addresses, and never what was typed into a form."""
import logging

logger = logging.getLogger("arabela.monitoring")

# Errors that are only noise: strangers' bots sending a made-up website name, and visitors who closed the page mid-request.
_NOISE = {"DisallowedHost", "BrokenPipeError", "ConnectionResetError", "ConnectionAbortedError"}


def _before_send(event, hint):
    exc_info = hint.get("exc_info")
    if exc_info and exc_info[0] is not None and exc_info[0].__name__ in _NOISE:
        return None
    return event


def init_from_environment(environ) -> bool:
    """Turns error alerts on if SENTRY_DSN is set. Returns whether they are on. Never raises: alerts must not be able to stop the site."""
    dsn = (environ.get("SENTRY_DSN") or "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration
        from sentry_sdk.integrations.logging import ignore_logger
    except ImportError:
        logger.warning("SENTRY_DSN is set but the sentry-sdk package is not installed, so error alerts are OFF.")
        return False
    ignore_logger("django.security.DisallowedHost")
    sentry_sdk.init(
        dsn=dsn,
        integrations=[DjangoIntegration()],
        environment=(environ.get("SENTRY_ENVIRONMENT") or "production").strip(),
        release=(environ.get("RENDER_GIT_COMMIT") or "").strip() or None,   # Render sets this: which version of the code broke
        send_default_pii=False,                                              # no names, emails or IP addresses
        max_request_body_size="never",                                       # never what was typed into a form
        traces_sample_rate=0.0,                                              # errors only, to stay inside the free allowance
        before_send=_before_send,
    )
    return True


def is_on() -> bool:
    try:
        import sentry_sdk
    except ImportError:
        return False
    return sentry_sdk.is_initialized()
