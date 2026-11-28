"""Rental History's Reservation Report as a PDF, landscape A4: a black band with the white Arabela logo, the dates and filters, the four figures, the top
rented gowns and categories, the rentals with their totals, a note about the two totals, and the payments of the dates. Black, white and light grey; every
status is written in words; amounts as "PHP" (the standard PDF fonts have no peso sign). The numbers come ready-made from arabela_admin/rental_report.py."""
import io
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import BaseDocTemplate, CondPageBreak, Frame, LongTable, PageTemplate, Paragraph, Spacer, Table, TableStyle

from arabela_admin import rental_history, rental_report

BLACK = colors.HexColor("#111111")
ZEBRA = colors.HexColor("#F4F4F4")
LINE = colors.HexColor("#DDDDDD")
GREY = colors.HexColor("#555555")
WHITE = colors.white

PAGE_W, PAGE_H = landscape(A4)
MARGIN = 32
WIDTH = PAGE_W - 2 * MARGIN
FIRST_BAND, LATER_BAND = 60, 26
FOOT = 34
TITLE = "Reservation Report"
LOGO = Path("static") / "images" / "email" / "arabela-logo-white.png"


def _style(name, size=8, bold=False, color=BLACK, right=False, leading=None):
    return ParagraphStyle(name, fontName="Helvetica-Bold" if bold else "Helvetica", fontSize=size, leading=leading or size * 1.3, textColor=color,
                          alignment=TA_RIGHT if right else 0)


CELL = _style("cell")
CELL_BOLD = _style("cell-bold", bold=True)
CELL_RIGHT = _style("cell-right", right=True)
CELL_BOLD_RIGHT = _style("cell-bold-right", bold=True, right=True)
SMALL = _style("small", 7.4, color=GREY)
HEAD = _style("head", bold=True, color=WHITE)
HEAD_RIGHT = _style("head-right", bold=True, color=WHITE, right=True)
LABEL = _style("label", 8.4, color=GREY)
VALUE = _style("value", 8.4)
BIG = _style("big", 17, bold=True, leading=21)
BOX = _style("box", 8, color=GREY, leading=11)
SECTION = _style("section", 12, bold=True, leading=15)
NOTE = _style("note", 8.4, color=GREY, leading=11.4)


def clean(value):
    """Plain text the built-in PDF fonts can write: no control characters, and a "?" for a letter outside their alphabet."""
    text = "" if value is None else str(value)
    text = "".join(ch if ch >= " " else " " for ch in text)
    return text.encode("cp1252", "replace").decode("cp1252")


def para(value, style=CELL):
    return Paragraph(escape(clean(value)), style)


def fit(value, width, style=CELL):
    """A cell of the long tables: plain text when it fits on one line (the table draws it in the same font and builds many times faster than a wrapping
    box for each cell), else a box that wraps it."""
    text = clean(value)
    return text if stringWidth(text, style.fontName, style.fontSize) <= width - 8.5 else para(text, style)


def lines(parts, style):
    """Several short lines in one box."""
    return Paragraph("<br/>".join(escape(clean(p)) for p in parts), style)


def php(value):
    return f"PHP {rental_report.amount_text(value)}"


def day(value):
    return rental_report.short_day(value) if value else "-"


def _plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def _logo():
    """The white logo, its empty margins cut off; (None, 0) when the file can't be read (the band then writes the name)."""
    try:
        picture = Image.open(Path(settings.BASE_DIR) / LOGO)
        picture.load()
    except (OSError, ValueError):
        return None, 0
    box = picture.getbbox()
    if box:
        picture = picture.crop(box)
    return ImageReader(picture), picture.width / picture.height


def _band(canvas, height, title_size, logo, ratio, subtitle):
    canvas.saveState()
    top = PAGE_H - height
    canvas.setFillColor(BLACK)
    canvas.rect(0, top, PAGE_W, height, stroke=0, fill=1)
    logo_h = height * (0.52 if height > 40 else 0.62)
    if logo is not None:
        canvas.drawImage(logo, MARGIN, top + (height - logo_h) / 2, width=logo_h * ratio, height=logo_h, mask="auto")
    else:
        canvas.setFillColor(WHITE)
        canvas.setFont("Helvetica-Bold", title_size)
        canvas.drawString(MARGIN, top + height / 2 - title_size / 3, "ARABELA")
    canvas.setFillColor(WHITE)
    if subtitle:
        canvas.setFont("Helvetica-Bold", title_size)
        canvas.drawRightString(PAGE_W - MARGIN, top + height / 2 + 2, TITLE)
        canvas.setFont("Helvetica", 9)
        canvas.drawRightString(PAGE_W - MARGIN, top + height / 2 - 13, clean(subtitle))
    else:
        canvas.setFont("Helvetica-Bold", title_size)
        canvas.drawRightString(PAGE_W - MARGIN, top + height / 2 - title_size / 3, TITLE)
    canvas.restoreState()


class _Numbered(pdf_canvas.Canvas):
    """A canvas that writes "Page X of Y" at the foot of every page (the total is only known at the end)."""

    def __init__(self, *args, footer="", **kwargs):
        super().__init__(*args, **kwargs)
        self._footer, self._states = footer, []

    def showPage(self):
        self._states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._states)
        for state in self._states:
            self.__dict__.update(state)
            self.setStrokeColor(LINE)
            self.setLineWidth(0.6)
            self.line(MARGIN, FOOT - 8, PAGE_W - MARGIN, FOOT - 8)
            self.setFillColor(GREY)
            self.setFont("Helvetica", 7.6)
            self.drawString(MARGIN, FOOT - 19, self._footer)
            self.drawRightString(PAGE_W - MARGIN, FOOT - 19, f"Page {self._pageNumber} of {total}")
            super().showPage()
        super().save()


def _grid(data, widths, *, right=(), total=False, empty=False, long=False):
    """A table with a black heading row, grey every other line and thin lines between. `right` = the number columns; `total` = the last row is the total;
    `empty` = the one row under the heading just says there is nothing."""
    table = (LongTable if long else Table)(data, colWidths=widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), BLACK),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, LINE),
        ("FONTNAME", (0, 1), (-1, -1), CELL.fontName), ("FONTSIZE", (0, 1), (-1, -1), CELL.fontSize), ("LEADING", (0, 1), (-1, -1), CELL.leading),
        ("TEXTCOLOR", (0, 1), (-1, -1), BLACK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2 if total else -1), [WHITE, ZEBRA]),
    ]
    for column in right:
        style.append(("ALIGN", (column, 0), (column, -1), "RIGHT"))
    if empty:
        style.append(("SPAN", (0, 1), (-1, 1)))
    if total:
        # (no SPAN across the total's label: a spanned cell makes every page break of a long table re-measure all of it)
        style += [("LINEABOVE", (0, -1), (-1, -1), 1, BLACK), ("BACKGROUND", (0, -1), (-1, -1), WHITE), ("FONTNAME", (0, -1), (-1, -1), CELL_BOLD.fontName)]
    table.setStyle(TableStyle(style))
    return table


def _head(names, right=()):
    return [para(name, HEAD_RIGHT if i in right else HEAD) for i, name in enumerate(names)]


def _info(report, filter_words, printed):
    period = report["period"]
    when = printed["at"]
    at = f"{rental_report.short_day(when.date())}, {when.strftime('%I:%M %p').lstrip('0')}"
    rows = [
        [para("Dates", LABEL), para(period.label, VALUE), para("Printed", LABEL), para(at, VALUE)],
        [para("Dates by", LABEL), para(f"{period.kind_label} date", VALUE), para("Printed by", LABEL), para(printed["by"], VALUE)],
        [para("List filters", LABEL), para("; ".join(filter_words) if filter_words else "None", VALUE), "", ""],
    ]
    table = Table(rows, colWidths=[62, WIDTH - 62 - 62 - 190, 62, 190])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5), ("SPAN", (1, 2), (3, 2)),
    ]))
    return table


def _figures(report):
    income = [f"from {_plural(report['income_bookings'], 'booking')}" if report["income_bookings"] else "No rental paid in these dates",
              f"Fees and deductions kept: {php(report['kept'])}", f"Kept from cancellations: {php(report['kept_cancelled'])}",
              "Security deposits are not income"]
    held = report["held"]
    deposits = [f"held now, for {_plural(held['held_bookings'], 'booking')}" if held["held_bookings"] else "No deposit is held now",
                f"Collected in these dates: {php(report['collected'])}", f"Deposit returned in these dates: {php(report['returned_deposit'])}"]
    if report["returned_extra"]:
        deposits.append(f"Extra money returned in these dates: {php(report['returned_extra'])}")
    gowns = ["gown" + ("" if report["rentals"] == 1 else "s") + " rented or booked"]
    gowns += [f"{entry['status']}: {entry['count']}" for entry in report["by_status"]]
    gowns.append(f"Cancelled (not listed): {report['cancelled']}")
    owing = [f"from {_plural(report['owing_bookings'], 'booking')} in these dates" if report["owing_bookings"] else "Nothing left to collect"]
    data = [
        [para(t, HEAD) for t in ("Rental income", "Deposits held", "Rentals", "Still to collect")],
        [para(php(report["income"]), BIG), para(php(held["held"]), BIG), para(report["rentals"], BIG), para(php(report["owing"]), BIG)],
        [lines(income, BOX), lines(deposits, BOX), lines(gowns, BOX), lines(owing, BOX)],
    ]
    table = Table(data, colWidths=[WIDTH / 4] * 4)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BLACK), ("BACKGROUND", (0, 1), (-1, -1), ZEBRA), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("LINEAFTER", (0, 0), (2, -1), 5, WHITE),
    ]))
    return table


def _rankings(report):
    """Top rented gowns beside Rentals by category."""
    left_w, gap = 430, 24
    right_w = WIDTH - left_w - gap
    top = [_head(["#", "Gown", "Category", "Times", "Amount"], right=(3, 4))]
    for number, entry in enumerate(report["top"], 1):
        top.append([para(number), para(entry["name"]), para(entry["category"]), para(entry["times"], CELL_RIGHT), para(php(entry["amount"]), CELL_RIGHT)])
    if not report["top"]:
        top.append([para("No gown was rented in these dates.", SMALL), "", "", "", ""])
    left = _grid(top, [20, left_w - 20 - 112 - 40 - 82, 112, 40, 82], right=(3, 4), empty=not report["top"])

    cats = [_head(["Category", "Times", "Amount"], right=(1, 2))]
    for entry in report["categories"]:
        cats.append([para(entry["name"]), para(entry["times"], CELL_RIGHT), para(php(entry["amount"]), CELL_RIGHT)])
    if not report["categories"]:
        cats.append([para("No gown was rented in these dates.", SMALL), "", ""])
    right = _grid(cats, [right_w - 44 - 96, 44, 96], right=(1, 2), empty=not report["categories"])

    titles = [para("Top rented gowns", SECTION), "", para("Rentals by category", SECTION)]
    table = Table([titles, [left, "", right]], colWidths=[left_w, gap, right_w])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, 0), 5), ("BOTTOMPADDING", (0, 1), (-1, -1), 0),
    ]))
    return table


RENTAL_COLUMNS = [("Booking no.", 68), ("Customer", 114), ("Gown", 154), ("Pick-up", 62), ("Return", 62), ("Event", 62), ("Status", 108), ("Price", 74), ("Balance", 74)]


def _rentals(rows, filtered):
    right = (7, 8)
    data = [_head([name for name, _ in RENTAL_COLUMNS], right=right)]
    widths = [w for _, w in RENTAL_COLUMNS]
    for line in rows:
        status = [line["status"]]
        if line["status"] in (rental_history.OVERDUE, rental_history.RETURNED) and line["note"] and line["note"] != "On time":
            status.append(line["note"])
        if line["balance_shown"] is not None:
            balance = php(line["balance_shown"])
        else:
            balance = "-" if line["balance"] is None else ""
        values = [line["code"], line["customer"], line["gown"], day(line["pickup"]), day(line["return"]), day(line["event"])]
        row = [fit(value, width) for value, width in zip(values, widths)]
        status = [clean(text) for text in status]
        plain = all(stringWidth(text, CELL.fontName, CELL.fontSize) <= widths[6] - 8.5 for text in status)
        row.append("\n".join(status) if plain else lines(status, CELL))
        row += [fit(php(line["price"]), widths[7], CELL_RIGHT), fit(balance, widths[8], CELL_RIGHT)]
        data.append(row)
    if not rows:
        data.append([para("No rentals match the list filters." if filtered else rental_report.NOTHING_LISTED, SMALL)] + [""] * (len(RENTAL_COLUMNS) - 1))
        return _grid(data, widths, right=right, empty=True, long=True)
    price = sum((line["price"] for line in rows), 0)
    owing = sum((line["balance_shown"] for line in rows if line["balance_shown"] is not None), 0)
    data.append([f"Total ({_plural(len(rows), 'gown')})", "", "", "", "", "", "", para(php(price), CELL_BOLD_RIGHT), para(php(owing), CELL_BOLD_RIGHT)])
    return _grid(data, widths, right=right, total=True, long=True)


PAYMENT_COLUMNS = [("Date", 62), ("Receipt no.", 92), ("Booking no.", 70), ("Customer", 154), ("For", 104), ("Paid by", 148), ("Received", 74), ("Handed back", 74)]


def _payments(report):
    right = (6, 7)
    data = [_head([name for name, _ in PAYMENT_COLUMNS], right=right)]
    widths = [w for _, w in PAYMENT_COLUMNS]
    for payment in report["payments"]:
        values = [day(payment["day"]), payment["receipt"] or "-", payment["code"], payment["customer"], payment["purpose"], payment["method"]]
        row = [fit(value, width) for value, width in zip(values, widths)]
        row += [fit(php(amount) if amount is not None else "", width, CELL_RIGHT)
                for amount, width in zip((payment["received"], payment["handed_back"]), widths[6:])]
        data.append(row)
    if not report["payments"]:
        data.append([para("No payments in these dates.", SMALL)] + [""] * (len(PAYMENT_COLUMNS) - 1))
        return _grid(data, widths, right=right, empty=True, long=True)
    totals = report["payment_totals"]
    data.append([f"Total ({_plural(len(report['payments']), 'payment')})", "", "", "", "", "",
                 para(php(totals["received"]), CELL_BOLD_RIGHT), para(php(totals["handed_back"]), CELL_BOLD_RIGHT)])
    return _grid(data, widths, right=right, total=True, long=True)


def _received_split(report):
    totals = report["payment_totals"]
    text = f"Received {php(totals['received'])} = rental income {php(totals['rental'])} + security deposits {php(totals['deposit'])}"
    if totals["extra"]:
        text += f" + extra charges {php(totals['extra'])}"
    if totals["other"]:
        text += f" + other money {php(totals['other'])}"
    return text + f". Handed back to customers: {php(totals['handed_back'])}."


def build_pdf(report, rows, filter_words, printed):
    """The PDF as bytes. `rows` = rental_report.listed_for(...); `printed` = {"at": aware datetime in the shop's time, "by": name}."""
    period = report["period"]
    logo, ratio = _logo()
    buffer = io.BytesIO()
    first = Frame(MARGIN, FOOT, WIDTH, PAGE_H - FIRST_BAND - 14 - FOOT, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id="first")
    later = Frame(MARGIN, FOOT, WIDTH, PAGE_H - LATER_BAND - 14 - FOOT, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id="later")
    document = BaseDocTemplate(buffer, pagesize=(PAGE_W, PAGE_H), title=f"Arabela {TITLE}", author="Arabela", subject=clean(period.label))
    document.addPageTemplates([
        PageTemplate(id="first", frames=[first], autoNextPageTemplate="later",
                     onPage=lambda c, d: _band(c, FIRST_BAND, 17, logo, ratio, period.label)),
        PageTemplate(id="later", frames=[later], onPage=lambda c, d: _band(c, LATER_BAND, 10, logo, ratio, "")),
    ])
    story = [
        _info(report, filter_words, printed), Spacer(1, 10),
        _figures(report), Spacer(1, 14),
        CondPageBreak(120), _rankings(report), Spacer(1, 14),
        CondPageBreak(70), para(f"Rentals ({len(rows)})", SECTION),
    ]
    if filter_words:
        story.append(para("This list is narrowed by the list filters above; the figures at the top are for all rentals in these dates.", NOTE))
    story += [Spacer(1, 4), _rentals(rows, bool(filter_words)), Spacer(1, 6)]
    story += [para(text, NOTE) for text in rental_report.explain(report, bool(filter_words))]
    story.append(para("A booking's balance is written once, on its first line.", NOTE))
    story += [Spacer(1, 14), CondPageBreak(70), para(f"Payments in these dates ({len(report['payments'])})", SECTION),
              para("By the day the money was paid or handed back.", NOTE), Spacer(1, 4), _payments(report)]
    if report["payments"]:
        story += [Spacer(1, 6), para(_received_split(report), NOTE)]
    footer = clean(f"Arabela {TITLE} | {period.label} | by {period.kind_label.lower()} date")
    document.build(story, canvasmaker=lambda *args, **kwargs: _Numbered(*args, footer=footer, **kwargs))
    return buffer.getvalue()
