"""python manage.py backfill_payments [--write]

Gives the bookings made BEFORE the Payments page their payment rows, so the Payments page, the balances and the receipts list are the same for
old and new bookings. A booking's rows are worked out from what it already carries (reservations/ledger.py legacy_rows):

  * an approved online booking: its deposit, paid online by GCash (checked by staff when it was approved);
  * the rental of an online booking that has a shop receipt photo attached: paid at the shop;
  * a walk-in booking: the deposit and/or the rental, as its labelled shop receipt photos say.

These rows are marked as recorded before the Payments page and have NO receipt number (nothing was printed for them). Nothing else changes:
no amount, status, date or photo of any booking is touched, and the old receipt photos stay exactly where they are.

It does NOTHING until you add --write: without it the command only prints what it WOULD do, booking by booking, so you can read the list first.
Safe to run again: a booking that already has rows is skipped. A booking is done in its own transaction, so a problem with one leaves the rest alone.

To reach the live shop on purpose: `python live.py backfill_payments` (list), then `python live.py backfill_payments --write`.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from reservations import ledger, payments
from reservations.models import Reservation


class Command(BaseCommand):
    help = "Give bookings made before the Payments page their payment rows (a dry run unless --write is given)."

    def add_arguments(self, parser):
        parser.add_argument("--write", action="store_true", help="Really save the rows. Without it nothing is written.")

    def handle(self, *args, **options):
        write = options["write"]
        bookings = (
            Reservation.objects.filter(payments__isnull=True)
            .prefetch_related("items", "receipt_records")
            .order_by("created_at", "id")
        )
        seen = skipped = made = 0
        for reservation in bookings:
            seen += 1
            rows = ledger.legacy_rows(reservation)
            if not rows:
                skipped += 1
                continue
            text = ", ".join(f"{r['purpose']} {ledger.peso(r['amount'])} ({r['method']})" for r in rows)
            self.stdout.write(f"{reservation.reference_code}  {reservation.get_status_display():<10} {reservation.get_channel_display():<8} {text}")
            if write:
                with transaction.atomic():
                    reservation = Reservation.objects.select_for_update(of=("self",)).get(pk=reservation.pk)
                    made += payments.ensure_rows(reservation)
            else:
                made += len(rows)
        verb = "Saved" if write else "Would save"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {made} payment row(s) for {seen - skipped} booking(s); {skipped} booking(s) have nothing to record (not approved, or nothing paid yet)."
        ))
        if not write:
            self.stdout.write("This was a dry run. Nothing was written. Add --write to save these rows.")
