"""python manage.py send_test_alert

Sends ONE test error to the alert service, so you can see it reach your email. Run it with the same SENTRY_DSN the live site will use:

    PowerShell:   $env:SENTRY_DSN = "paste-the-address-here"; python manage.py send_test_alert"""
from django.core.management.base import BaseCommand, CommandError

from arabela_system import monitoring


class Command(BaseCommand):
    help = "Send one test error to Sentry to prove error alerts work."

    def handle(self, *args, **options):
        if not monitoring.is_on():
            raise CommandError("Error alerts are OFF here: SENTRY_DSN is not set (or sentry-sdk is not installed). See README section 14.")
        import sentry_sdk

        event_id = sentry_sdk.capture_message("Arabela test alert: if you can read this, error alerts work.", level="error")
        sentry_sdk.flush(timeout=10)
        self.stdout.write(self.style.SUCCESS(f"Test alert sent (id {event_id}). It should reach your Sentry inbox and email within a minute."))
