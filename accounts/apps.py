from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = 'accounts'

    def ready(self):
        # Emails to customers about their own booking. The hooks do nothing at all unless
        # settings.CUSTOMER_EMAILS_ENABLED is on -- see accounts/customer_emails.py.
        from accounts import customer_emails
        customer_emails.connect_signals()
