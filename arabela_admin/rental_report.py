"""The Reservation Report inside Rental History, and its PDF and Excel downloads. Everything is read from the bookings and the payments ledger; nothing is saved.

  read_period(params, today)   which rentals: All, Today, Last 7 days, This month or Custom dates, by their pick-up, return or event day
  read_filters(params)         what narrows the rentals LIST only (never the money boxes): status, search words, category, Online / Walk-in, one gown
  build(period, items, ...)    every number of the report: money, rentals by status, still to collect, top gowns, categories, payments and the note

The money comes from arabela_admin.dashboard_money.money_between (the Dashboard's own sums) and the deposits held now from the Security Deposits page's own
states (handed in by the view), so the report can never say something those pages don't. Amounts are Decimals; amount_text() writes them for people."""
from datetime import date, timedelta

from django.db.models import Q
from django.utils import timezone

from arabela_admin import dashboard_money, rental_history
from reservations import ledger
from reservations.models import Payment, Reservation, ReservationItem

ALL, TODAY, WEEK, MONTH, CUSTOM = "all", "today", "week", "month", "custom"
PERIODS = ((ALL, "All"), (TODAY, "Today"), (WEEK, "Last 7 days"), (MONTH, "This month"), (CUSTOM, "Custom dates"))
PICKUP, RETURN, EVENT = "pickup", "return", "event"
DATE_KINDS = ((PICKUP, "Pick-up"), (RETURN, "Return"), (EVENT, "Event"))
FILE_KINDS = {"pdf": "PDF", "excel": "Excel"}

# A gown counts in "Top rented gowns" and "Rentals by category" once it is a real rental: approved and waiting, out, or back. A pending request and a gown
# nobody picked up do not.
RENTED = (rental_history.WAITING, rental_history.OUT_NOW, rental_history.OVERDUE, rental_history.RETURNED)

TOP = 5
SEARCH_MAX, CATEGORY_MAX, GOWN_MAX = 100, 60, 200
FIRST_YEAR, LAST_YEAR = 2000, 2100            # a typing slip such as year 0202 is not a date anyone can mean
REMOVED_GOWN = "Removed gown"                 # the category of a gown that was later deleted from the catalog

MISSING_DATES = "Pick a From date and a To date."
WRONG_ORDER = "The To date can't be before the From date."
NOTHING_LISTED = "No rentals in this period."


def amount_text(value):
    """'35,500.00' (no currency sign: the page writes the peso sign, the downloads write PHP)."""
    return f"{ledger.money(value):,.2f}"


def short_day(day):
    """'Nov 26, 2026'."""
    return f"{day:%b} {day.day}, {day.year}"


def span_text(start, end):
    """'Nov 20 to Nov 26, 2026' / 'Jun 1, 2026 to Jun 30, 2027' / 'Nov 26, 2026'."""
    if start == end:
        return short_day(start)
    if start.year == end.year:
        return f"{start:%b} {start.day} to {short_day(end)}"
    return f"{short_day(start)} to {short_day(end)}"


# ---------------------------------------------------------------------------------------------------------------------------------
# which dates
# ---------------------------------------------------------------------------------------------------------------------------------
class Period:
    """The dates the report looks at. start / end are None for "All"; with an error (Custom dates that cannot be used) nothing is held."""

    def __init__(self, key, kind, start=None, end=None, error="", typed_from="", typed_to=""):
        self.key, self.kind, self.start, self.end, self.error = key, kind, start, end, error
        self.typed_from, self.typed_to = typed_from, typed_to          # what the From / To boxes show

    def holds(self, day):
        if self.error or day is None:
            return False
        return (self.start is None or day >= self.start) and (self.end is None or day <= self.end)

    @property
    def kind_label(self):
        return dict(DATE_KINDS)[self.kind]

    @property
    def label(self):
        """What the report is about, in words: 'November 2026', 'Jun 1, 2026 to Jun 30, 2027'."""
        if self.error:
            return "Custom dates"
        if self.key == ALL:
            return "All dates"
        if self.key == TODAY:
            return f"Today, {short_day(self.start)}"
        if self.key == WEEK:
            return f"Last 7 days, {span_text(self.start, self.end)}"
        if self.key == MONTH:
            return f"{self.start:%B %Y}"
        return span_text(self.start, self.end)

    @property
    def tag(self):
        """The small tag beside a box's title."""
        if self.key == TODAY:
            return "Today"
        if self.key == WEEK:
            return "Last 7 days"
        return self.label

    @property
    def file_part(self):
        """For the downloaded file's name: 'all dates', '2026-11-26' or '2026-06-01 to 2027-06-30'."""
        if self.error or self.start is None or self.end is None:
            return "all dates"
        if self.start == self.end:
            return self.start.isoformat()
        return f"{self.start.isoformat()} to {self.end.isoformat()}"

    def params(self):
        """The address parameters that give this period back."""
        out = {"period": self.key, "by": self.kind}
        if self.key == CUSTOM:
            out.update({"from": self.typed_from, "to": self.typed_to})
        return out


def _parse_day(text):
    try:
        day = date.fromisoformat(str(text or "").strip()[:10])
    except ValueError:
        return None
    return day if FIRST_YEAR <= day.year <= LAST_YEAR else None


def read_period(params, today):
    """The Period asked for in the address (?period=&by=&from=&to=). Anything unknown falls back to All, by pick-up; Custom dates need both days, in order."""
    key = params.get("period") or ALL
    if key not in dict(PERIODS):
        key = ALL
    kind = params.get("by") or PICKUP
    if kind not in dict(DATE_KINDS):
        kind = PICKUP
    month_start = today.replace(day=1)
    if key == TODAY:
        return Period(key, kind, today, today)
    if key == WEEK:
        return Period(key, kind, today - timedelta(days=6), today)
    if key == MONTH:
        return Period(key, kind, month_start, (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1))
    if key == CUSTOM:
        typed_from, typed_to = (params.get("from") or "").strip()[:10], (params.get("to") or "").strip()[:10]
        start, end = _parse_day(typed_from), _parse_day(typed_to)
        if start is None or end is None:
            return Period(key, kind, error=MISSING_DATES, typed_from=typed_from if start else "", typed_to=typed_to if end else "")
        if end < start:
            return Period(key, kind, error=WRONG_ORDER, typed_from=start.isoformat(), typed_to=end.isoformat())
        return Period(key, kind, start, end, typed_from=start.isoformat(), typed_to=end.isoformat())
    return Period(ALL, kind)


def day_of(item, kind):
    """The day of one gown that "Dates by" means: the day it went out (or is booked to), came back (or is due), or the customer's event day."""
    if kind == RETURN:
        return item.returned_on if item.stage == ReservationItem.Stage.RETURNED and item.returned_on else item.return_date
    if kind == EVENT:
        return item.effective_event_date
    return item.picked_up_on or item.rental_date


# ---------------------------------------------------------------------------------------------------------------------------------
# what narrows the list
# ---------------------------------------------------------------------------------------------------------------------------------
class Filters:
    """The search box, the status buttons, Category, Online / Walk-in and "this gown". They narrow the LIST (on screen and in the downloads), never the boxes."""

    def __init__(self, status="All", q="", category="", channel="", gown=""):
        self.status, self.q, self.category, self.channel, self.gown = status, q, category, channel, gown

    @property
    def active(self):
        return bool(self.status != "All" or self.q or self.category or self.channel or self.gown)

    def matches(self, row):
        return (
            (self.status == "All" or row["status"] == self.status)
            and (not self.q or self.q.lower() in row["search"])
            and (not self.category or row["category"] == self.category)
            and (not self.channel or row["channel"] == self.channel)
            and (not self.gown or row["gown_key"] == self.gown)
        )

    def words(self, gown_name=""):
        """How the report says which filters were on."""
        out = []
        if self.status != "All":
            out.append(f"Status: {self.status}")
        if self.q:
            out.append(f'Search: "{self.q}"')
        if self.category:
            out.append(f"Category: {self.category}")
        if self.channel:
            out.append(f"{Reservation.Channel(self.channel).label} bookings only")
        if self.gown:
            out.append(f"Gown: {gown_name or self.gown.split('|', 1)[-1]}")
        return out


def read_filters(params):
    status = params.get("status") or "All"
    if status not in rental_history.LOOK:
        status = "All"
    channel = params.get("channel") or ""
    if channel not in Reservation.Channel.values:
        channel = ""
    return Filters(
        status=status, q=(params.get("q") or "").strip()[:SEARCH_MAX], category=(params.get("category") or "").strip()[:CATEGORY_MAX],
        channel=channel, gown=(params.get("gown") or "")[:GOWN_MAX],
    )


# ---------------------------------------------------------------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------------------------------------------------------------
def gown_key(category, name):
    """One DESIGN (every size and unit with the same name in a category): what "Top rented gowns" adds up and a tap on one lists."""
    return f"{category}|{(name or '').strip().casefold()}"[:GOWN_MAX]


def row(item, today):
    """Everything the list and the downloads say about one gown. `item` is a Rental History line (it carries its booking's money statement and its view)."""
    reservation = item.reservation
    category = item.gown.category if item.gown is not None else REMOVED_GOWN
    status = item.view["status"]
    rate = int(reservation.late_fee_per_day or 0)
    # The same fees the opened row names, added up. A gown nobody picked up is charged only a free-days booking's booked extra days (the gown was kept for them).
    charged = rate > 0 and (item.uses_free_days or status != rental_history.MISSED)
    fees = ledger.money(ledger.planned_late_days(item, today) * rate) if charged else ledger.ZERO
    name = reservation.display_customer_name
    return {
        "item": item, "id": item.id, "booking_id": reservation.pk, "code": reservation.reference_code, "customer": name,
        "gown": item.gown_name, "gown_key": gown_key(category, item.gown_name), "category": category, "size": item.size,
        "channel": reservation.channel, "channel_label": reservation.get_channel_display(),
        "booked": timezone.localtime(reservation.created_at).date(),
        "pickup": day_of(item, PICKUP), "return": day_of(item, RETURN), "event": day_of(item, EVENT),
        "status": status, "note": item.view["note"], "price": ledger.money(item.rental_price), "fees": fees,
        "balance": item.statement.balance_due if item.statement.tracked else None,
        "search": f"{name} {item.gown_name} {reservation.reference_code}".lower(),
    }


def _ranked(rows, key_of, start):
    """[{..., "times", "amount", "share"}] of the rented rows grouped by key_of(row), most rented first, then the most money, then the name."""
    groups = {}
    for line in rows:
        if line["status"] not in RENTED:
            continue
        key = key_of(line)
        entry = groups.get(key)
        if entry is None:
            entry = groups[key] = dict(start(line), times=0, amount=ledger.ZERO)
        entry["times"] += 1
        entry["amount"] += line["price"]
    ranked = sorted(groups.values(), key=lambda e: (-e["times"], -e["amount"], e["name"].casefold(), e.get("category", "")))
    most = ranked[0]["times"] if ranked else 0
    for entry in ranked:
        entry["share"] = round(100 * entry["times"] / most) if most else 0
        entry["amount_text"] = amount_text(entry["amount"])
    return ranked


def _cancelled_gowns(period):
    """How many cancelled gowns (not listed on this page) belong to the chosen dates. A rejected request never was a rental, so it is not counted."""
    if period.error:
        return 0
    gowns = (
        ReservationItem.objects.exclude(reservation__status=Reservation.Status.REJECTED)
        .filter(Q(stage=ReservationItem.Stage.CANCELLED) | Q(reservation__status=Reservation.Status.CANCELLED))
        .only("id", "stage", "rental_date", "return_date", "event_date", "picked_up_on", "returned_on")
    )
    return sum(1 for gown in gowns if period.holds(day_of(gown, period.kind)))


def _no_money():
    zero = ledger.ZERO
    return {"income": zero, "by_booking": {}, "kept": zero, "collected": zero, "returned": zero, "payments": []}


def _payment_line(entry):
    """One payment of the chosen days, as the downloads list it."""
    reservation, paid = entry["reservation"], entry["row"]
    payment = paid["payment"]
    purpose, method = paid["purpose"], paid["method"]
    if payment is not None:
        purpose_words, method_words = payment.get_purpose_display(), payment.get_method_display()
    else:                                                                     # an old booking's payment, worked out from what it carries
        purpose_words = "Extra charge" if purpose == Payment.Purpose.EXTRA else dict(Payment.Purpose.choices).get(purpose, purpose)
        method_words = dict(Payment.Method.choices).get(method, method)
    is_in = paid["direction"] == Payment.Direction.IN
    test = payment is not None and "TEST PAYMENT" in (payment.note or "")
    return {
        "day": entry["day"], "receipt": (payment.receipt_number if payment is not None else "") or "", "code": reservation.reference_code,
        "customer": reservation.display_customer_name, "purpose": purpose_words + (" (test)" if test else ""), "method": method_words,
        "received": paid["amount"] if is_in else None, "handed_back": None if is_in else paid["amount"],
        "rental": entry["rental"], "deposit": entry["deposit"], "extra": entry["extra"], "other": entry["other"], "test": test,
    }


def _reconcile(listed, income_by_booking):
    """Why Rental Income (money by the day it was PAID) can differ from what the listed rentals add up to (by their pick-up / return / event day):
    income = listed value + rental money paid for rentals not listed - listed rentals not paid in these days."""
    price_by_booking = {}
    for line in listed:
        price_by_booking[line["booking_id"]] = price_by_booking.get(line["booking_id"], ledger.ZERO) + line["price"]
    other_paid = not_paid = ledger.ZERO
    for booking in set(price_by_booking) | set(income_by_booking):
        difference = income_by_booking.get(booking, ledger.ZERO) - price_by_booking.get(booking, ledger.ZERO)
        if difference > 0:
            other_paid += difference
        else:
            not_paid -= difference
    return sum(price_by_booking.values(), ledger.ZERO), other_paid, not_paid


def build(period, items, *, today, held):
    """The whole report for `period`, from `items` (every Rental History line, newest first, each with .statement and .view) and `held` (the deposit money
    held now: Decimals "held" / "due" and their booking counts)."""
    listed = [row(item, today) for item in items if period.holds(day_of(item, period.kind))]
    listed.sort(key=lambda line: (day_of(line["item"], period.kind), line["id"]), reverse=True)
    money = _no_money() if period.error else dashboard_money.money_between(period.start, period.end, today)

    counts = {status: 0 for status in rental_history.LOOK}
    for line in listed:
        counts[line["status"]] += 1
    owing = {}
    for line in listed:
        if line["balance"] is not None and line["balance"] > 0:
            owing[line["booking_id"]] = line["balance"]

    gowns = _ranked(listed, lambda r: r["gown_key"], lambda r: {"key": r["gown_key"], "name": r["gown"], "category": r["category"]})
    categories = _ranked(listed, lambda r: r["category"], lambda r: {"name": r["category"]})

    listed_value, other_paid, not_paid = _reconcile(listed, money["by_booking"])
    payments = [_payment_line(entry) for entry in money["payments"]]
    totals = {
        name: sum((p[name] for p in payments if p[name] is not None), ledger.ZERO)
        for name in ("received", "handed_back", "rental", "deposit", "extra", "other")
    }
    owing_total = sum(owing.values(), ledger.ZERO)
    return {
        "period": period, "listed": listed,
        "income": money["income"], "income_bookings": len(money["by_booking"]), "kept": money["kept"],
        "held": held, "collected": money["collected"], "returned": money["returned"],
        "rentals": len(listed), "by_status": [{"status": s, "count": n, "dot": rental_history.DOTS[rental_history.LOOK[s][0]]} for s, n in counts.items() if n],
        "status_counts": counts, "cancelled": _cancelled_gowns(period),
        "owing": owing_total, "owing_bookings": len(owing),
        "gowns": gowns, "top": gowns[:TOP], "categories": categories,
        "listed_value": listed_value, "other_paid": other_paid, "not_paid": not_paid,
        "payments": payments, "payment_totals": totals,
        # the same figures written for people (the page and the PDF)
        "text": {
            "income": amount_text(money["income"]), "kept": amount_text(money["kept"]), "collected": amount_text(money["collected"]),
            "returned": amount_text(money["returned"]), "held": amount_text(held["held"]), "due": amount_text(held["due"]),
            "owing": amount_text(owing_total),
        },
    }


def listed_for(report, filters):
    """The rows a download lists: the chosen dates, narrowed by the list filters, oldest first, each booking's balance written once (on its first line)."""
    lines = [dict(line) for line in reversed(report["listed"]) if filters.matches(line)]
    seen = set()
    for line in lines:
        first = line["booking_id"] not in seen
        seen.add(line["booking_id"])
        line["balance_shown"] = line["balance"] if first else None
    return lines


def gown_label(report, filters):
    """The name of the gown the list is narrowed to ("" when none): from the report's own rows."""
    for line in report["listed"]:
        if filters.gown and line["gown_key"] == filters.gown:
            return line["gown"]
    return ""


def explain(report, filtered=False):
    """The note under the totals: why Rental Income (money paid in these dates) and the price of these dates' rentals can differ (the sentence, then the
    numbers). `filtered` = the list under it is narrowed, so say that these numbers are for ALL the rentals of the dates."""
    kind = report["period"].kind_label.lower()
    first = (f"Rental income counts the money on the day it was paid; the rentals are chosen by their {kind} day, so the two can differ. "
             "Security deposits are never income.")
    rentals = "all rentals in these dates" + (" (not only the filtered list)" if filtered else "")
    income = amount_text(report["income"])
    if report["income"] == report["listed_value"]:
        return [first, f"In these dates the rental income and the price of {rentals} are both PHP {income}."]
    text = f"Rental income PHP {income} = the price of {rentals} PHP {amount_text(report['listed_value'])}"
    if report["other_paid"]:
        text += f", plus rental money paid in these dates for other rentals PHP {amount_text(report['other_paid'])}"
    if report["not_paid"]:
        text += f", minus the part of these rentals not paid in these dates PHP {amount_text(report['not_paid'])}"
    return [first, text + "."]
