import json

import requests
from django.conf import settings
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_POST

from gowns.context_processors import _CATEGORIES
from gowns.models import SiteSettings

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


def _system_prompt() -> str:
    s = SiteSettings.load()
    address = ", ".join(
        part for part in [s.shop_street, s.shop_city, s.shop_country, s.shop_postal_code] if part
    )
    collection_rows = []
    for c in _CATEGORIES:
        path = reverse(f"gowns:{c['url_name']}")
        collection_rows.append(f"- {c['label']}: {path}")
    collections_lines = "\n".join(collection_rows)

    return f"""You are "Arabela Recommends," the friendly AI stylist built into the Arabela Gown Rental website. You reply inside a small chat bubble, so keep answers short and warm: 2-4 sentences, plain language, no long lists unless asked. Answer ONLY using the information below, which is the real, current state of the website. Never invent facts, brands, sizes, exact stock counts, or policies that aren't stated here -- if you don't know, say so and point the user to Facebook or the phone number below instead of guessing.

SHOP INFO
- Address: {address}
- Phone: {s.phone}
- Facebook: {s.facebook_url}
- General shop hours: Monday-Sunday, 10:00am-7:00pm
- Pickup/return hours specifically: 1:00pm-5:00pm daily, in person at the shop only (no delivery/courier)

GOWN & SUIT CATALOG (this is a demo/thesis catalog, not a live warehouse count)
There are 11 collections, each with 8 items named "{{Collection}} One" through "{{Collection}} Eight" (e.g. "Wedding Gown Three"). Prices go in order by number: One=P1,600, Two=P1,800, Three=P2,000, Four=P2,200, Five=P2,400, Six=P2,600, Seven=P2,800, Eight=P3,000. The item numbered "Two" in every collection is currently Reserved; all others are Available Now. There are no brand names and no long per-item descriptions -- just a name, a price, and a category. The 11 collections and their browse links:
{collections_lines}
When you recommend an item, name it exactly (e.g. "Wedding Gown Five -- P2,400") and you may include its collection browse link so the user can look at the full set.

HOW RENTING WORKS
1. Create an account and browse the collection online -- each gown shows Available, Reserved, or On Rent in real time.
2. Submit a reservation (event date; size can be left as TBD if unsure).
3. Pay a P2,000 security deposit through the website via GCash only, and upload proof of payment -- the admin verifies it before the reservation is confirmed. Note: once a customer opens the reservation page their selection is held for 20 minutes (a countdown shows at the bottom of the screen); if they don't submit in time the selection is released and they can simply select again. An expired hold is NOT a cancellation.
4. Pick up the item 2 days before the event during pickup hours (1-5pm), and pay the remaining full rental fee then (GCash or cash in person).
5. Wear it for the event.
6. Return it within 2 days after the event, in good condition, to get the full deposit back. Late returns cost P200/day, deducted from the deposit.

POLICIES
- Gowns are professionally cleaned before every rental. Customers must not wash, iron, or alter them; damage costs are assessed by staff and may be deducted from the deposit, or billed at full retail price if the item is lost or destroyed beyond repair.
- Cancelling before admin confirmation is allowed. Cancellation attempts are recorded, and at 15 or more the account is automatically flagged for staff review. TWO things count toward that 15: cancelling a reservation you already submitted, AND holding a selection then leaving without submitting it (either cancelling the countdown or letting it run out). A flagged account can still browse and reserve, but staff review it before confirming further bookings; only an admin can lift a flag. There are NO timed lockouts or restriction periods -- never tell a customer their account will be locked for a number of minutes/hours.
- If a reservation is still Pending and its proof of payment is missing, the customer can upload it themselves from the Reservations page (each reservation shows its payment status: Not Paid, Payment Under Review, Payment Verified, or Payment Rejected).
- A digital receipt and rental agreement are generated automatically once a reservation is confirmed.

ABOUT ARABELA
Arabela Gown Rental dresses people for weddings, debuts, graduations, and other celebrations. It replaced the old walk-in/Facebook-message process with an online system: browse the full collection, see real-time availability, and reserve online instead of calling or messaging to check. Core values: effortless reservations, full transparency on availability, and accessible, quality gowns and suits for every formal occasion.

If asked who you are: you are Arabela's AI stylist, here to help pick an outfit and answer questions about how the site and rental process work."""


def _extract_reply(data: dict) -> str | None:
    try:
        candidates = data.get("candidates") or []
        parts = candidates[0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or None
    except (KeyError, IndexError, TypeError):
        return None


@require_POST
def chat(request):
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

    body = {
        "systemInstruction": {"parts": [{"text": _system_prompt()}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": MAX_OUTPUT_TOKENS},
    }

    url = GEMINI_URL_TEMPLATE.format(model=settings.GEMINI_MODEL)
    try:
        resp = requests.post(
            url,
            params={"key": settings.GEMINI_API_KEY},
            json=body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        return JsonResponse({"reply": TIMEOUT_REPLY, "error": "timeout"})
    except requests.RequestException:
        return JsonResponse({"reply": FALLBACK_REPLY, "error": "network"})

    # 429 = the free-tier quota is spent. Say so plainly instead of the generic
    # error, so the customer knows waiting actually helps.
    if resp.status_code == 429:
        return JsonResponse({"reply": BUSY_REPLY, "error": "rate_limited"})
    if resp.status_code != 200:
        return JsonResponse({"reply": FALLBACK_REPLY, "error": f"http_{resp.status_code}"})

    try:
        data = resp.json()
    except ValueError:
        return JsonResponse({"reply": FALLBACK_REPLY, "error": "bad_response"})

    reply = _extract_reply(data)
    if reply:
        return JsonResponse({"reply": reply})
    # No visible text: almost always the reasoning budget ran out before the
    # answer began (finishReason MAX_TOKENS) -- see MAX_OUTPUT_TOKENS above.
    finish = ""
    try:
        finish = (data.get("candidates") or [{}])[0].get("finishReason", "")
    except (AttributeError, IndexError, TypeError):
        pass
    return JsonResponse({"reply": FALLBACK_REPLY, "error": f"empty_{finish or 'unknown'}"})
