"""python manage.py paymongo_check [--methods gcash,paymaya,qrph] [--keep]

Asks PayMongo, with the key in .env, the three things the checkout relies on -- and says plainly what it found, so nothing is assumed:

  1. open a checkout session for PHP 100 with the configured payment methods (does PayMongo accept GCash, Maya and QR Ph there?);
  2. read it back (which read address answers, and does the answer have the fields the shop uses?);
  3. close it again (--keep leaves it open and prints its page, so you can pay it on PayMongo's test page by hand).

It works ONLY with a TEST key (sk_test_...): it refuses a live key, so it can never create anything real. It writes nothing to the database and never
prints a key. Run it once after you put the test key in .env (and on Render's shell after you add it there).
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from reservations import paymongo


class Command(BaseCommand):
    help = "Check the PayMongo test key and what PayMongo accepts (test mode only; creates nothing in the shop's database)."

    def add_arguments(self, parser):
        parser.add_argument("--methods", default="", help="Payment methods to try, comma separated (default: the PAYMONGO_METHODS setting).")
        parser.add_argument("--keep", action="store_true", help="Leave the test session open and print its page address.")

    def handle(self, *args, **options):
        if not paymongo.secret_key():
            raise CommandError("No PAYMONGO_SECRET_KEY is set. Put your TEST secret key (sk_test_...) in .env first.")
        if paymongo.mode() != "test":
            raise CommandError("This check only works with a TEST key (sk_test_...). It will not run against a live key.")
        wanted = [m.strip() for m in options["methods"].split(",") if m.strip()]
        if wanted:
            settings.PAYMONGO_METHODS = wanted
        self.stdout.write(f"Mode: test.  Methods asked for: {', '.join(paymongo.methods())}")

        problems = 0
        try:
            session = paymongo.create_checkout_session(
                amount=100, name="Arabela connection check", description="Connection check. Nothing is charged.",
                reference_number="CHECK-0001", success_url="https://example.com/ok", cancel_url="https://example.com/cancel",
                metadata={"purpose": "paymongo_check"},
            )
        except paymongo.PaymongoError as exc:
            raise CommandError(f"PayMongo would not open a checkout with these methods: {exc}")
        self.stdout.write(self.style.SUCCESS(f"OK   opened a checkout session {session.id} with {', '.join(paymongo.methods())}"))
        self.stdout.write(f"     its page: {session.checkout_url}")

        try:
            state = paymongo.retrieve_checkout_session(session.id)
        except paymongo.PaymongoError as exc:
            problems += 1
            self.stdout.write(self.style.ERROR(f"FAIL reading the session back: {exc}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"OK   read it back: status {state.status or '(none)'}, paid {state.paid}"))
            if not state.status:
                problems += 1
                self.stdout.write(self.style.WARNING("WARN the answer had no status field"))

        if options["keep"]:
            self.stdout.write("     left open on purpose (--keep). Pay it on that page with Authorize, or close it later.")
        else:
            outcome = paymongo.expire_checkout_session(session.id)
            if outcome in ("expired", "already_expired"):
                self.stdout.write(self.style.SUCCESS(f"OK   closed the session ({outcome})"))
            else:
                problems += 1
                self.stdout.write(self.style.ERROR(f"FAIL closing the session: {outcome}"))

        secret_note = "set" if settings.PAYMONGO_WEBHOOK_SECRET else "NOT set yet (add it after you create the webhook in PayMongo's dashboard)"
        self.stdout.write(f"Webhook secret: {secret_note}")
        if problems:
            raise CommandError(f"{problems} problem(s) found. See the lines above.")
        self.stdout.write(self.style.SUCCESS("Everything the checkout needs from PayMongo answered."))
