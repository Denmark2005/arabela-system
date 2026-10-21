import json
import logging
import re
import time
from urllib.parse import urlparse

import requests
from django.conf import settings
from accounts import rate_limit
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_POST

from gowns.context_processors import _build_search_catalog, all_categories, category_url
from gowns.models import SiteSettings

logger = logging.getLogger(__name__)

GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

MAX_MESSAGE_LENGTH = 500
MAX_HISTORY_TURNS = 8

# Gemini Flash is a "thinking" model: it spends output tokens reasoning
# internally BEFORE writing a single visible character, and that reasoning alone
# measures ~750-850 tokens for these questions. maxOutputTokens covers thinking
# + answer together, so anything near 1k gets truncated mid-thought, comes back
# with empty content, and the chat shows its error fallback. The budget below is
# deliberately generous; the visible answer is only ~70-90 tokens.
# (thinkingBudget / thinkingLevel are both rejected with HTTP 400 by this model,
# so capping the reasoning directly is not an option.)
MAX_OUTPUT_TOKENS = 3072

# This model reasons before answering, so replies routinely take 4-11 seconds.
# The old 20s ceiling was clipping legitimate answers into the error fallback.
REQUEST_TIMEOUT_SECONDS = 45

# Google answers "503 This model is currently experiencing high demand" in short bursts (measured: 4 of 8 identical requests in a row),
# and the old code gave up on the first one. A busy or failing model is retried after a short pause, then the backup model
# (settings.GEMINI_FALLBACK_MODEL) gets the same tries. No new attempt starts once RETRY_CUTOFF_SECONDS have passed, and every
# attempt is limited to what is left of REQUEST_TIMEOUT_SECONDS, so the longest a customer can wait is the same as before.
GEMINI_ATTEMPTS_PER_MODEL = 3
RETRY_PAUSES_SECONDS = (1.0, 2.0)       # before the 2nd and the 3rd attempt on the same model
RETRY_CUTOFF_SECONDS = 20
RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})

FALLBACK_REPLY = (
    "Sorry, I'm having trouble thinking right now. Please try again in a moment, "
    "or reach us directly through Facebook or by phone -- you'll find both in the site footer."
)

BUSY_REPLY = (
    "I'm getting a lot of questions right now, so I've hit my limit for the moment. "
    "Please try again in a minute -- or message us on Facebook and our team will help you straight away."
)

TIMEOUT_REPLY = (
    "Sorry, that one took me too long to think through. Please try asking again, "
    "or keep it a little shorter."
)

# Gemini itself said it is overloaded (HTTP 503) and every retry and the backup model were busy too.
OVERLOADED_REPLY = (
    "I'm very busy right now and couldn't answer that one. Please send it again in a few seconds -- "
    "or message us on Facebook and our team will help you straight away."
)

# Protects the Gemini quota/cost and the endpoint itself from a sudden flood of
# requests. Two windows guard two different risks: the per-minute cap stops a
# script hammering the endpoint in a burst, and the per-day cap stops the same
# visitor staying just under that burst limit for hours and still running up real
# API usage over a day.
AI_CHAT_RATE_LIMIT_PER_MINUTE = 10
AI_CHAT_RATE_LIMIT_WINDOW_SECONDS = 60
AI_CHAT_DAILY_LIMIT = 40
AI_CHAT_DAILY_WINDOW_SECONDS = 24 * 60 * 60

DAILY_LIMIT_REPLY = (
    "You've reached today's message limit for the AI stylist on this connection. "
    "Please try again tomorrow, or message us on Facebook and our team will help you directly."
)


def _client_ip(request) -> str:
    """The visitor's real IP address behind Render's proxy (the LAST X-Forwarded-For entry) -- see accounts.rate_limit.client_ip."""
    return rate_limit.client_ip(request)


def _hit_fixed_window(name: str, window_seconds: int) -> int:
    """Counts one message in a fixed time window and returns the count so far. Kept in the database (accounts.rate_limit), so a
    restart of the app no longer resets the limit; the window starts at the first message and is never extended by later ones."""
    return rate_limit.hit(name, window_seconds)


def _ai_chat_rate_limit_reply(request):
    """None if this visitor is clear to send a message; otherwise (reply_text,
    error_code) to send back instead of ever calling Gemini, so a blocked message
    never costs any API quota.
    """
    ip = _client_ip(request)

    minute_count = _hit_fixed_window(f"ai_chat_rl_min:{ip}", AI_CHAT_RATE_LIMIT_WINDOW_SECONDS)
    if minute_count > AI_CHAT_RATE_LIMIT_PER_MINUTE:
        return BUSY_REPLY, "rate_limited_burst"

    day_count = _hit_fixed_window(f"ai_chat_rl_day:{ip}", AI_CHAT_DAILY_WINDOW_SECONDS)
    if day_count > AI_CHAT_DAILY_LIMIT:
        return DAILY_LIMIT_REPLY, "rate_limited_daily"

    return None


# Gowns listed to the AI per category before the rest is left to the category's browse link -- only a safety cap, so the prompt can
# never grow without limit. It was 25, which hid gowns from the AI the moment a category passed 25 (Wedding 28, Filipiniana 30 and
# Kids 33 live products: 16 gowns it could not recommend by name). A listed gown costs about 25 tokens, so 100 per category keeps even
# a full shop (hundreds of products) far inside what the model reads, while every real product stays visible to it.
MAX_GOWNS_PER_CATEGORY = 100

_ALL_COLLECTIONS_PATH = "/collections/all/"


def _link_label(text: str) -> str:
    """A gown's name made safe to use as a markdown link label."""
    return re.sub(r"[\[\]()]", " ", text).strip()


def _live_catalog() -> tuple[str, set[str]]:
    """The AI's catalog, read from the database right now (so it is never out of date): every
    category that currently exists, marked Women's or Men's, and under it every real, bookable gown
    with its real price and its own product-page link. Also returns the set of site paths that are
    real, which `_sanitize_reply` uses to throw away any link the AI makes up."""
    categories = all_categories()
    by_collection: dict[str, list[dict]] = {}
    for item in _build_search_catalog(categories):
        by_collection.setdefault(item["collection_key"], []).append(item)

    allowed = {"/collections/", _ALL_COLLECTIONS_PATH}
    lines = []
    for category in categories:
        path = category_url(category)
        allowed.add(path)
        audience = "men's" if category.get("audience") == "men" else "women's"
        label = _link_label(category["label"])
        header = f"- {category['label']} ({audience} collection) -- browse all: [{label}]({path})"
        items = by_collection.get(category["key"], [])
        if not items:
            lines.append(f"{header} -- NO GOWNS LISTED YET: never recommend anything from this collection")
            continue
        lines.append(header)
        for item in items[:MAX_GOWNS_PER_CATEGORY]:
            allowed.add(item["url"])
            lines.append(f"    - {item['title']} | {item['price_label']} | [{_link_label(item['title'])}]({item['url']})")
        if len(items) > MAX_GOWNS_PER_CATEGORY:
            lines.append(f"    - ...and {len(items) - MAX_GOWNS_PER_CATEGORY} more (use the browse-all link above)")
    if not lines:
        lines.append("- (There are no collections on the website right now.)")
    return "\n".join(lines), allowed


def _system_prompt(catalog: tuple[str, set[str]] | None = None) -> str:
    s = SiteSettings.load()
    address = ", ".join(
        part for part in [s.shop_street, s.shop_city, s.shop_country, s.shop_postal_code] if part
    )
    catalog_lines, _allowed = catalog or _live_catalog()

    return f"""You are "Arabela Recommends," the friendly AI stylist built into the Arabela Gown Rental website. You reply inside a small chat bubble, so keep answers short and warm: 2-4 sentences, plain language, no long lists unless asked. Answer ONLY using the information below, which is the real, current state of the website. Never invent facts, brands, sizes, exact stock counts, or policies that aren't stated here -- if you don't know, say so and point the user to Facebook or the phone number below instead of guessing.

SHOP INFO
- Address: {address}
- Phone: {s.phone}
- Facebook (only for contacting the shop -- never use it as a link to a gown or collection): {s.facebook_url}
- General shop hours: Monday-Sunday, 10:00am-7:00pm
- Pickup/return hours specifically: 1:00pm-5:00pm daily, in person at the shop only (no delivery/courier)

LIVE CATALOG -- THIS IS THE ONLY LIST OF GOWNS AND COLLECTIONS THAT EXIST
This list was read from the shop's database at the moment the customer sent this message, so it is always current. Anything that is not on it does not exist: never recommend, name, or quote a price for any gown or collection that is not on this list, even if an earlier message in this chat mentioned it (earlier messages may be out of date). Each collection is marked as the women's or the men's collection. Each gown line is: name | rental price | link to that gown's own page.
{catalog_lines}
Whole catalog: [All collections]({_ALL_COLLECTIONS_PATH})

HOW TO RECOMMEND
- Recommend at most 3 gowns, by their exact name and exact price as listed, and link each one with ITS OWN link from the list, written exactly as shown, like [Name](/collections/.../products/.../). Use a collection's browse-all link only when the customer wants to look through a whole collection.
- Copy every link exactly as it is written in the list. Links start with "/collections/". NEVER put a website address in front of them, never combine them with the Facebook address, and never make up a link.
- Before you recommend anything you need to know whether the customer wants a women's gown or a men's outfit. If they have not said so and it is not obvious from the chat, ask ONE short question first (for example: "Are you looking for a women's gown or a men's outfit?") and do not recommend yet. Once you know, remember it for the rest of the chat and only recommend from the collections marked for that audience (men's collections for men, women's collections for women).
- Match the customer's occasion and words to the collections and gown names on the list. The list has no colours, fabrics, sizes or style descriptions, so never make any up.
- If the best matching collection has no gowns listed, or nothing on the list fits, say so honestly and suggest the closest collection that does have gowns, or the shop's phone or Facebook. Never invent a gown.
- Prices are the rental prices shown on the list. Whether a gown is free on a certain date depends on the calendar, so never say a gown is available or booked on a given day -- tell the customer to open the gown's page and check the date calendar.

HOW RENTING WORKS
1. Create an account and browse the collection online -- a gown with no label can be booked now; otherwise its card shows the date it is next available (for example "Available from Oct 26") or "Fully booked for now".
2. Submit a reservation (event date; size can be left as TBD if unsure).
3. Pay a P2,000 security deposit through the website via GCash only, and upload proof of payment -- the admin verifies it before the reservation is confirmed. Note: once a customer opens the reservation page their selection is held for 20 minutes (a countdown shows at the bottom of the screen); letting it run out, or cancelling it, releases the selection AND counts as one cancellation attempt on the account -- the same as cancelling a submitted reservation.
4. Pick up the item up to 2 days before the event (or on the event day itself when the gown is still being cleaned after an earlier rental) during pickup hours (1-5pm; today's pick-up closes at 5pm), and pay the remaining full rental fee then (GCash or cash in person).
5. Wear it for the event.
6. Return it within 2 days after the event, in good condition, to get the full deposit back. Late returns cost P200/day, deducted from the deposit.

POLICIES
- Gowns are professionally cleaned before every rental. Customers must not wash, iron, or alter them; damage costs are assessed by staff and may be deducted from the deposit, or billed at full retail price if the item is lost or destroyed beyond repair.
- After a booking, that same gown is off the market for 7 days after the event date, even once it's returned -- time for the shop to check, clean, and repair it if needed before it goes out again. If a customer asks why a date they want is greyed out, this is one likely reason (the other is it's simply already booked by someone else).
- Cancelling is allowed at any stage before pick-up -- including a reservation that is already confirmed and paid for -- as long as no item in it has been picked up yet. Once a reservation is confirmed, its security deposit is NOT refunded if it is cancelled -- whether the customer cancels it or Arabela cancels it because the gown was not picked up; the customer should contact Arabela to book again. Cancellation attempts are recorded. TWO things count toward the total, every time: cancelling a reservation you already submitted (at any stage up to pick-up), AND holding a selection then leaving without submitting it (either cancelling the countdown or letting it run out). Escalating consequences: at 5 attempts, a temporary 30-minute lockout on starting a new reservation; at 10 attempts, a 2-hour lockout; at 15 or more, the account is automatically flagged for staff review. A flagged account can still browse and reserve, but staff review it before confirming further bookings; only an admin can lift a flag. If you tell a customer about a lockout, only state the exact numbers above (5 -> 30 minutes, 10 -> 2 hours, 15 -> flagged) -- never invent a different threshold or duration.
- If a reservation is still Pending and its proof of payment is missing, the customer can upload it themselves from the Reservations page (each reservation shows its payment status: Not Paid, Payment Under Review, Payment Verified, or Payment Rejected).
- A digital receipt and rental agreement are generated automatically once a reservation is confirmed.

ABOUT ARABELA
Arabela Gown Rental dresses people for weddings, debuts, graduations, and other celebrations. It replaced the old walk-in/Facebook-message process with an online system: browse the full collection, see real-time availability, and reserve online instead of calling or messaging to check. Core values: effortless reservations, full transparency on availability, and accessible, quality gowns and suits for every formal occasion.

If asked who you are: you are Arabela's AI stylist, here to help pick an outfit and answer questions about how the site and rental process work."""


_MD_LINK_RE = re.compile(r"\[([^\[\]]+)\]\(([^()\s]+)\)")
_BARE_URL_RE = re.compile(r"(?:https?://[^\s<>()\[\]]+|/collections/[^\s<>()\[\]]*)")
_TRAILING_PUNCTUATION = ".,;:!?*_'\""


def _classify_link(url: str, allowed: set[str], host: str):
    """("other", url) for a link that isn't about our collections (left alone), ("keep", path) for
    a real collection/product page of this site, ("bad", None) for a collection-looking link that
    isn't real -- an invented page, or the shop's Facebook address glued in front of a path."""
    parsed = urlparse(url)
    if "/collections" not in parsed.path.lower():
        return "other", url
    own_site = not parsed.netloc or parsed.netloc.lower() == host.lower()
    path = parsed.path if parsed.path.endswith("/") else parsed.path + "/"
    if own_site and path in allowed:
        return "keep", path
    return "bad", None


def _sanitize_reply(reply: str, allowed: set[str], host: str) -> str:
    """The AI is told to copy links exactly, but it is still a language model. So every link in its
    reply that points at a collection or gown page is checked against the real pages: a real one is
    kept (made a clean site path), an invented one is removed, so a customer is never sent to a
    page that doesn't exist or to the wrong website. Contact links (Facebook) are left alone."""
    kept: list[str] = []

    def markdown_link(match):
        label, url = match.group(1), match.group(2)
        verdict, value = _classify_link(url, allowed, host)
        if verdict == "bad":
            return label  # keep the words, drop the broken link
        token = f"@@ARABELALINK{len(kept)}@@"
        kept.append(f"[{label}]({value})")
        return token

    def bare_link(match):
        raw = match.group(0)
        url = raw.rstrip(_TRAILING_PUNCTUATION)
        tail = raw[len(url):]
        verdict, value = _classify_link(url, allowed, host)
        if verdict == "other":
            return raw
        return (value if verdict == "keep" else _ALL_COLLECTIONS_PATH) + tail

    text = _MD_LINK_RE.sub(markdown_link, reply)
    text = _BARE_URL_RE.sub(bare_link, text)
    for i, link in enumerate(kept):
        text = text.replace(f"@@ARABELALINK{i}@@", link)
    return text


def _extract_reply(data: dict) -> str | None:
    try:
        candidates = data.get("candidates") or []
        parts = candidates[0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or None
    except (KeyError, IndexError, TypeError):
        return None


def _pause(seconds: float) -> None:
    """The short wait between two attempts. Its own function so a test can skip the waiting."""
    time.sleep(seconds)


def _models_to_try() -> list[str]:
    """The main model first, then the backup (when one is set and it is a different model)."""
    models = [settings.GEMINI_MODEL]
    backup = (getattr(settings, "GEMINI_FALLBACK_MODEL", "") or "").strip()
    if backup and backup != settings.GEMINI_MODEL:
        models.append(backup)
    return models


def _ask_gemini(body: dict):
    """Sends the conversation to Gemini. Returns (response, None) for the first HTTP 200, or (None, error_code) when nothing worked.

    * A busy or failing model (HTTP 500/502/503/504, or a connection error) is tried up to GEMINI_ATTEMPTS_PER_MODEL times with a
      short pause, because Google's "high demand" 503 usually clears within seconds.
    * A refusal that waiting will not fix (429 quota, 404 retired model, 400/401/403) moves straight on to the backup model.
    * A timeout ends everything at once: the time is already spent.
    * The error code reported is the main model's first failure ("http_503", "network", "rate_limited", ...), which is what the
      customer actually ran into; every failed attempt is also written to the log.
    """
    started = time.monotonic()
    first_failure = None
    for model in _models_to_try():
        url = GEMINI_URL_TEMPLATE.format(model=model)
        for attempt in range(GEMINI_ATTEMPTS_PER_MODEL):
            elapsed = time.monotonic() - started
            if first_failure is not None and elapsed > RETRY_CUTOFF_SECONDS:
                return None, first_failure
            try:
                resp = requests.post(
                    url,
                    params={"key": settings.GEMINI_API_KEY},
                    json=body,
                    timeout=max(REQUEST_TIMEOUT_SECONDS - elapsed, 1),
                )
            except requests.Timeout:
                logger.warning("Gemini %s: timed out (attempt %d)", model, attempt + 1)
                return None, "timeout"
            except requests.RequestException as exc:
                failure, retryable = "network", True
                logger.warning("Gemini %s: connection problem %s (attempt %d)", model, type(exc).__name__, attempt + 1)
            else:
                if resp.status_code == 200:
                    return resp, None
                if resp.status_code == 429:
                    failure, retryable = "rate_limited", False
                else:
                    failure, retryable = f"http_{resp.status_code}", resp.status_code in RETRYABLE_STATUSES
                logger.warning("Gemini %s: HTTP %s (attempt %d)", model, resp.status_code, attempt + 1)
            if first_failure is None:
                first_failure = failure
            if not retryable or attempt == GEMINI_ATTEMPTS_PER_MODEL - 1:
                break
            _pause(RETRY_PAUSES_SECONDS[min(attempt, len(RETRY_PAUSES_SECONDS) - 1)])
    return None, first_failure


@require_POST
def chat(request):
    limited = _ai_chat_rate_limit_reply(request)
    if limited:
        reply, error_code = limited
        return JsonResponse({"reply": reply, "error": error_code}, status=429)

    if not settings.GEMINI_API_KEY:
        return JsonResponse({"reply": FALLBACK_REPLY, "error": "not_configured"})

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "bad_request"}, status=400)

    message = (payload.get("message") or "").strip()[:MAX_MESSAGE_LENGTH]
    if not message:
        return JsonResponse({"error": "empty_message"}, status=400)

    contents = []
    for turn in (payload.get("history") or [])[-MAX_HISTORY_TURNS:]:
        role = turn.get("role")
        text = (turn.get("text") or "").strip()[:MAX_MESSAGE_LENGTH]
        if role in ("user", "model") and text:
            contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": message}]})

    # Rebuilt from the database on every message, so what staff add, change or delete is what the
    # AI knows on the very next message.
    catalog = _live_catalog()
    body = {
        "systemInstruction": {"parts": [{"text": _system_prompt(catalog)}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": MAX_OUTPUT_TOKENS},
    }

    resp, failure = _ask_gemini(body)
    if resp is None:
        # 429 = the quota is spent: say so plainly, so the customer knows waiting helps. 503 = Google is overloaded even after the
        # retries and the backup model. Everything else keeps the general apology.
        reply = {"timeout": TIMEOUT_REPLY, "rate_limited": BUSY_REPLY, "http_503": OVERLOADED_REPLY}.get(failure, FALLBACK_REPLY)
        return JsonResponse({"reply": reply, "error": failure})

    try:
        data = resp.json()
    except ValueError:
        return JsonResponse({"reply": FALLBACK_REPLY, "error": "bad_response"})

    reply = _extract_reply(data)
    if reply:
        return JsonResponse({"reply": _sanitize_reply(reply, catalog[1], request.get_host())})
    # No visible text: almost always the reasoning budget ran out before the
    # answer began (finishReason MAX_TOKENS) -- see MAX_OUTPUT_TOKENS above.
    finish = ""
    try:
        finish = (data.get("candidates") or [{}])[0].get("finishReason", "")
    except (AttributeError, IndexError, TypeError):
        pass
    return JsonResponse({"reply": FALLBACK_REPLY, "error": f"empty_{finish or 'unknown'}"})
