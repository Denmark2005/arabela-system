from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = 'accounts'

    def ready(self):
        # Emails to customers about their own booking. The hooks do nothing at all unless
        # settings.CUSTOMER_EMAILS_ENABLED is on -- see accounts/customer_emails.py.
        from accounts import customer_emails
        customer_emails.connect_signals()

        # After every `migrate`, lock any table that is still open to Supabase's public web API
        # (see arabela_system/table_lock.py). sender=self so it runs once per migrate, not once per app.
        from django.db.models.signals import post_migrate

        from arabela_system.table_lock import lock_public_tables
        post_migrate.connect(lock_public_tables, sender=self, dispatch_uid="arabela_lock_public_tables")
