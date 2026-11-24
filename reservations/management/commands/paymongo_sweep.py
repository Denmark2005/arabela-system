"""python manage.py paymongo_sweep

Closes the checkouts nobody paid within their 20 minutes (PayMongo never closes a checkout by itself) and books any that turned out to be paid.
It is only housekeeping: a checkout left open can never take money by mistake (a payment that still arrives is booked, or sent back when the gown is
gone), and the shop's own pages already do this whenever a customer pays or a PayMongo message arrives. Run it from a scheduled job if you want it
done on a clock; it is safe to run at any time, any number of times."""
from django.core.management.base import BaseCommand

from gowns import online_checkout
from reservations import paymongo


class Command(BaseCommand):
    help = "Close old unpaid PayMongo checkouts and book the ones that were paid."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50, help="How many old checkouts to look at (default 50).")

    def handle(self, *args, **options):
        if not paymongo.enabled():
            self.stdout.write("PayMongo is not set up (no key): nothing to do.")
            return
        looked = online_checkout.expire_stale(limit=max(1, options["limit"]))
        self.stdout.write(self.style.SUCCESS(f"Looked at {looked} old checkout(s)."))
