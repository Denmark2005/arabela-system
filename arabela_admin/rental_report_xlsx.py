"""Rental History's Reservation Report as an Excel file: Summary, Rentals, Top Gowns and Payments. Black headings and light grey rows; amounts are real
numbers and days real dates, so the owner can sort, filter and add them up. A name that starts with "=" is kept as text, never read as a formula. The
numbers come ready-made from arabela_admin/rental_report.py."""
import io

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from arabela_admin import rental_history, rental_report

HEAD_FILL = PatternFill("solid", fgColor="111111")
ZEBRA_FILL = PatternFill("solid", fgColor="F4F4F4")
HEAD_FONT = Font(bold=True, color="FFFFFF")
BOLD = Font(bold=True)
LINE = Side(style="thin", color="DDDDDD")
MONEY, DAY, NUMBER = "#,##0.00", "mmm d, yyyy", "#,##0"


def _put(sheet, row, column, value, fmt=None, *, font=None, fill=None, line=False, wrap=False):
    cell = sheet.cell(row=row, column=column)
    if isinstance(value, str):
        cell.value = ILLEGAL_CHARACTERS_RE.sub(" ", value)
        cell.data_type = "s"          # text stays text, even when it starts with "="
    else:
        cell.value = value
    if fmt:
        cell.number_format = fmt
    if font:
        cell.font = font
    if fill:
        cell.fill = fill
    if line:
        cell.border = Border(bottom=LINE)
    cell.alignment = Alignment(vertical="top", wrap_text=wrap)
    return cell


def _table(sheet, headings, rows, formats, widths, *, top=1, left=1, empty="", totals=None, filtered=False):
    """A heading row and its rows at (top, left). `totals` = {column index: value} written in bold under a blank row, outside the sort/filter range."""
    for i, heading in enumerate(headings):
        _put(sheet, top, left + i, heading, font=HEAD_FONT, fill=HEAD_FILL)
    for r, values in enumerate(rows, 1):
        for i, value in enumerate(values):
            _put(sheet, top + r, left + i, value, formats[i], fill=ZEBRA_FILL if r % 2 == 0 else None, line=True)
    for i, width in enumerate(widths):
        sheet.column_dimensions[get_column_letter(left + i)].width = width
    if not rows and empty:
        _put(sheet, top + 1, left, empty)
    if filtered and rows:
        sheet.auto_filter.ref = f"{get_column_letter(left)}{top}:{get_column_letter(left + len(headings) - 1)}{top + len(rows)}"
    if totals and rows:
        at = top + len(rows) + 2
        _put(sheet, at, left, "Total", font=BOLD)
        for column, value in totals.items():
            _put(sheet, at, left + column, value, formats[column], font=BOLD)


def _page(sheet):
    """Printing: landscape, one page wide."""
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0


def _summary(sheet, report, filter_words, printed):
    period = report["period"]
    when = printed["at"]
    _put(sheet, 1, 1, "Arabela Reservation Report", font=Font(bold=True, size=15))
    facts = [
        ("Dates", period.label), ("Dates by", f"{period.kind_label} date"),
        ("List filters", "; ".join(filter_words) if filter_words else "None"),
        ("Printed", f"{rental_report.short_day(when.date())}, {when.strftime('%I:%M %p').lstrip('0')}"), ("Printed by", printed["by"]),
    ]
    row = 3
    for label, value in facts:
        _put(sheet, row, 1, label, font=BOLD)
        _put(sheet, row, 2, value, wrap=True)
        row += 1
    held = report["held"]
    figures = [
        ("Rental income (PHP)", report["income"], MONEY),
        ("Bookings the rental income came from", report["income_bookings"], NUMBER),
        ("Fees and deductions kept (PHP)", report["kept"], MONEY),
        ("Kept from cancellations (PHP)", report["kept_cancelled"], MONEY),
        ("Security deposits held now (PHP)", held["held"], MONEY),
        ("Bookings with a deposit held now", held["held_bookings"], NUMBER),
        ("Security deposits collected in these dates (PHP)", report["collected"], MONEY),
        ("Security deposits returned in these dates (PHP)", report["returned_deposit"], MONEY),
        ("Extra money returned in these dates (PHP)", report["returned_extra"], MONEY),
        ("Rentals (gowns)", report["rentals"], NUMBER),
    ]
    figures += [(f"    {status}", report["status_counts"][status], NUMBER) for status in rental_history.LOOK]
    figures += [
        ("Cancelled (not listed)", report["cancelled"], NUMBER),
        ("Still to collect (PHP)", report["owing"], MONEY),
        ("Bookings with a balance", report["owing_bookings"], NUMBER),
    ]
    row += 1
    _table(sheet, ["Figure", "Value"], [(name, value) for name, value, _ in figures], [None, None], [48, 30], top=row)
    for offset, (_, _, fmt) in enumerate(figures, 1):
        sheet.cell(row=row + offset, column=2).number_format = fmt
    row += len(figures) + 2
    for text in rental_report.explain(report, bool(filter_words)) + ["Security deposits are the customers' money: they are never counted as income."]:
        _put(sheet, row, 1, text, wrap=True)
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
        sheet.row_dimensions[row].height = 15 * (len(text) // 72 + 1)
        row += 1
    _page(sheet)


def _rentals(sheet, rows, filtered_list):
    headings = ["Booking no.", "Customer", "Gown", "Category", "Size", "Online / Walk-in", "Booked", "Pick-up", "Return", "Event", "Status",
                "Rental price", "Fees", "Balance"]
    formats = [None, None, None, None, None, None, DAY, DAY, DAY, DAY, None, MONEY, MONEY, MONEY]
    widths = [16, 26, 30, 18, 16, 16, 14, 14, 14, 14, 20, 14, 12, 14]
    values = [
        (r["code"], r["customer"], r["gown"], r["category"], r["size"], r["channel_label"], r["booked"], r["pickup"], r["return"], r["event"], r["status"],
         r["price"], r["fees"], r["balance_shown"])
        for r in rows
    ]
    totals = {
        11: sum((r["price"] for r in rows), 0), 12: sum((r["fees"] for r in rows), 0),
        13: sum((r["balance_shown"] for r in rows if r["balance_shown"] is not None), 0),
    }
    _table(sheet, headings, values, formats, widths, totals=totals, filtered=True,
           empty="No rentals match the list filters." if filtered_list else rental_report.NOTHING_LISTED)
    sheet.freeze_panes = "C2"
    if rows:
        _put(sheet, len(rows) + 4, 1, "A booking's balance is written once, on its first line.")
    _page(sheet)


def _top_gowns(sheet, report):
    gowns = [(n, g["name"], g["category"], g["times"], g["amount"]) for n, g in enumerate(report["gowns"], 1)]
    _table(sheet, ["Rank", "Gown", "Category", "Times rented", "Amount (PHP)"], gowns, [NUMBER, None, None, NUMBER, MONEY], [8, 30, 20, 14, 16],
           empty="No gown was rented in these dates.", filtered=True)
    categories = [(c["name"], c["times"], c["amount"]) for c in report["categories"]]
    _table(sheet, ["Category", "Times rented", "Amount (PHP)"], categories, [None, NUMBER, MONEY], [20, 14, 16], left=7,
           empty="No gown was rented in these dates.")
    sheet.column_dimensions["F"].width = 3
    sheet.freeze_panes = "A2"
    _page(sheet)


def _payments(sheet, report):
    show_other = bool(report["payment_totals"]["other"])
    headings = ["Date", "Receipt no.", "Booking no.", "Customer", "For", "Paid by", "Received", "Handed back", "Rental part", "Deposit part",
                "Extra charge"] + (["Other money"] if show_other else [])
    formats = [DAY, None, None, None, None, None] + [MONEY] * (len(headings) - 6)
    widths = [14, 20, 16, 26, 20, 26, 14, 14, 14, 14, 14] + ([14] if show_other else [])
    values = []
    for p in report["payments"]:
        parts = [p["rental"], p["deposit"], p["extra"]] + ([p["other"]] if show_other else [])
        if p["received"] is None:                       # money handed back has no parts
            parts = [None] * len(parts)
        values.append((p["day"], p["receipt"], p["code"], p["customer"], p["purpose"], p["method"], p["received"], p["handed_back"], *parts))
    totals = report["payment_totals"]
    names = ["received", "handed_back", "rental", "deposit", "extra"] + (["other"] if show_other else [])
    _table(sheet, headings, values, formats, widths, totals={6 + i: totals[name] for i, name in enumerate(names)}, filtered=True,
           empty="No payments in these dates.")
    sheet.freeze_panes = "A2"
    _page(sheet)


def build_workbook(report, rows, filter_words, printed):
    """The workbook as bytes. `rows` = rental_report.listed_for(...); `printed` = {"at": aware datetime in the shop's time, "by": name}."""
    book = Workbook()
    summary = book.active
    summary.title = "Summary"
    _summary(summary, report, filter_words, printed)
    _rentals(book.create_sheet("Rentals"), rows, bool(filter_words))
    _top_gowns(book.create_sheet("Top Gowns"), report)
    _payments(book.create_sheet("Payments"), report)
    book.properties.title = "Arabela Reservation Report"
    book.properties.creator = "Arabela"
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
