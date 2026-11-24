"""python manage.py demo_paymongo [--port 8801]

Runs the PRACTICE payment gateway (reservations/demo_gateway.py) in this window, for showing the online payments without a PayMongo account.
The shop needs, in .env (or typed in the terminal):

    PAYMONGO_SECRET_KEY=sk_test_demo
    PAYMONGO_API_BASE=http://127.0.0.1:8801

and it must be using a laptop copy of the database (.env.laptop-db) so a practice payment can never make a booking in the live shop; the shop
refuses the practice gateway otherwise. The easy way is `python demo.py`, which sets up a throwaway practice shop and this gateway together."""
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from reservations import demo_gateway, paymongo


class Command(BaseCommand):
    help = "Run the practice payment gateway (a stand-in for PayMongo, for demonstrations only)."

    def add_arguments(self, parser):
        parser.add_argument("--port", type=int, default=0, help="Port to listen on (default: the one in PAYMONGO_API_BASE, else 8801).")

    def handle(self, *args, **options):
        key = paymongo.secret_key()
        if not key.startswith("sk_test_"):
            raise CommandError("Set PAYMONGO_SECRET_KEY=sk_test_demo (any sk_test_ value) first. The practice gateway never takes a live key.")
        port = options["port"] or urlparse(settings.PAYMONGO_API_BASE).port or 8801
        if not paymongo.api_base_allowed():
            raise CommandError("This shop would not talk to a practice gateway (it may be using the live database). Use `python demo.py`, or a laptop copy "
                               "of the database (.env.laptop-db) with PAYMONGO_API_BASE=http://127.0.0.1:%d." % port)
        server = demo_gateway.make_server("127.0.0.1", port, key)
        self.stdout.write(self.style.SUCCESS(f"Practice payment gateway running at http://127.0.0.1:{port} (press Ctrl+C to stop)."))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            self.stdout.write("Stopped.")
        finally:
            server.server_close()
