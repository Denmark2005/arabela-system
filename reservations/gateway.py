"""Which online payment service the shop talks to, and ONE set of functions for it.

The shop was built on PayMongo (reservations/paymongo.py); it moved to Xendit (reservations/xendit.py) when PayMongo's sign-ups were closed. The two
modules have the same functions, and everything that takes an online payment (the checkout, the webhook, the deposit refunds, the admin pages) calls THESE
instead, so it does not care which one is behind them.

Which one: settings.PAYMENT_PROVIDER = "xendit" or "paymongo" decides when it is set; when it is empty, Xendit is used as soon as a Xendit key is set,
else PayMongo. With neither key set, online payment is OFF and the older GCash-screenshot checkout keeps working (`enabled()` is False).

Everything here just forwards to the chosen module at the moment of the call (never earlier), so a test that patches `reservations.paymongo._request`
or `reservations.xendit._request` still works."""
from django.conf import settings

from reservations import paymongo, xendit

PAYMONGO, XENDIT = "paymongo", "xendit"
PROVIDERS = (PAYMONGO, XENDIT)
LABELS = {PAYMONGO: "PayMongo", XENDIT: "Xendit"}

# What an `except` clause catches: an answer either service could not give.
GatewayError = (paymongo.PaymongoError, xendit.XenditError)
# The names of the ways to pay, the same for both services (GCash, Maya, QR Ph).
CHANNEL_LABELS = {"gcash": "GCash", "paymaya": "Maya", "qrph": "QR Ph"}
# How long after paying a refund can still be sent back, kept well inside each service's own limit; a way of paying that is not listed cannot be refunded
# through the service at all. Xendit: GCash 180 days, Maya 365 days, QR Ph never. PayMongo: GCash and Maya as its docs say, QR Ph a short window.
REFUND_DAYS = {
    PAYMONGO: {"PayMongo GCash": 170, "PayMongo Maya": 350, "PayMongo QR Ph": 28},
    XENDIT: {"PayMongo GCash": 170, "PayMongo Maya": 350},
}


# The largest single online payment: the lowest limit among the ways to pay the page offers (PayMongo: 100,000; Xendit: QR Ph 50,000, GCash and Maya 100,000).
MAX_AMOUNT = {PAYMONGO: 100000, XENDIT: 50000}


def provider():
    """"xendit" or "paymongo"."""
    chosen = (getattr(settings, "PAYMENT_PROVIDER", "") or "").strip().lower()
    if chosen in PROVIDERS:
        return chosen
    return XENDIT if xendit.secret_key() else PAYMONGO


def _impl():
    return xendit if provider() == XENDIT else paymongo


def name():
    """The service's own name, as it is written in the database (Payment.provider) and in the logs."""
    return provider()


def label():
    """The name staff and customers read: "Xendit" or "PayMongo"."""
    return LABELS[provider()]


def refund_days():
    return REFUND_DAYS[provider()]


def max_amount():
    return MAX_AMOUNT[provider()]


# ---------------------------------------------------------------- switches
def enabled():
    return _impl().enabled()


def mode():
    return _impl().mode()


def is_test():
    return _impl().is_test()


def is_demo():
    return _impl().is_demo()


def auto_refund():
    """Hand a returned security deposit back to the customer's own account automatically (settings.PAYMONGO_AUTO_REFUND: the one switch for both)."""
    return bool(getattr(settings, "PAYMONGO_AUTO_REFUND", False))


# ---------------------------------------------------------------- sessions and refunds
def create_checkout_session(**kwargs):
    if provider() == PAYMONGO:
        kwargs.pop("expires_minutes", None)                 # a PayMongo session never expires by itself; the shop closes it
    return _impl().create_checkout_session(**kwargs)


def retrieve_checkout_session(session_id):
    return _impl().retrieve_checkout_session(session_id)


def expire_checkout_session(session_id):
    return _impl().expire_checkout_session(session_id)


def create_refund(**kwargs):
    return _impl().create_refund(**kwargs)


def parse_refund(payload):
    return _impl().parse_refund(payload)


# ---------------------------------------------------------------- webhooks
def verify_webhook(request):
    """The event (a dict) when the webhook request is genuine for the chosen service, else None. PayMongo: the signature header with the webhook secret.
    Xendit: the callback token header with the shop's callback token."""
    if provider() == XENDIT:
        return xendit.verify_webhook(request.body, request.headers.get(xendit.WEBHOOK_TOKEN_HEADER, ""), getattr(settings, "XENDIT_WEBHOOK_TOKEN", ""))
    return paymongo.verify_webhook(request.body, request.headers.get(paymongo.SIGNATURE_HEADER, ""), getattr(settings, "PAYMONGO_WEBHOOK_SECRET", ""))


def event_type(event):
    return _impl().event_type(event)


def event_resource(event):
    return _impl().event_resource(event)
