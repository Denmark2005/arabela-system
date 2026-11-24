#!/usr/bin/env python
"""The PRACTICE SHOP: a throwaway copy of Arabela on this computer, with a practice payment gateway, for showing the online payments (GCash, Maya,
QR Ph) without a PayMongo account.

    python demo.py              start it (the first run takes a minute to set up its own small database)
    python demo.py --reset      start from a fresh practice database
    python demo.py --no-browser do not open the browser by itself

What it is: its own SQLite file (demo.sqlite3, never the real database), a practice payment page that says so on every screen, and two sign-in links
(a demo customer and a demo owner). It cannot touch the live shop: it never reads the database settings in .env, and it refuses to start on Render.
Stop it with Ctrl+C. Nothing it does is saved anywhere else.
"""
import argparse
import os
import sys
import threading
import webbrowser

GATEWAY_KEY = "sk_test_demo"


def main():
    parser = argparse.ArgumentParser(description="Run the practice shop with a practice payment gateway.")
    parser.add_argument("--port", type=int, default=8000, help="Port of the practice shop (default 8000).")
    parser.add_argument("--gateway-port", type=int, default=8801, help="Port of the practice payment page (default 8801).")
    parser.add_argument("--reset", action="store_true", help="Delete the practice database and start fresh.")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser.")
    args = parser.parse_args()

    # Everything the practice shop needs, set BEFORE Django reads its settings (a value typed here always wins over .env).
    os.environ.update({
        "ARABELA_DEMO": "1",                                           # settings.py: its own SQLite file, no emails, refuses Render
        "DEBUG": "True",
        "ALLOWED_HOSTS": "127.0.0.1,localhost",
        "PAYMONGO_SECRET_KEY": GATEWAY_KEY,
        "PAYMONGO_WEBHOOK_SECRET": "whsk_demo",
        "PAYMONGO_API_BASE": f"http://127.0.0.1:{args.gateway_port}",
        "SITE_BASE_URL": "",
        "CLOUDINARY_CLOUD_NAME": "",                                    # pictures stay on this computer
        "CUSTOMER_EMAILS_ENABLED": "false",
    })
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "arabela_system.settings")

    from django.conf import settings
    base = settings.BASE_DIR
    database = base / "demo.sqlite3"
    if args.reset and database.exists():
        database.unlink()
        print("Deleted the old practice database.")

    import django
    from django.core.management import call_command

    django.setup()
    if not settings.DEMO_MODE:
        sys.exit("The practice shop did not switch on (ARABELA_DEMO). Nothing was started.")
    first_time = not database.exists() or database.stat().st_size == 0
    print("Setting up the practice database..." if first_time else "Opening the practice database...")
    call_command("migrate", interactive=False, verbosity=0)

    from arabela_system import demo as practice
    practice.seed()

    from reservations import demo_gateway
    try:
        demo_gateway.start_in_background("127.0.0.1", args.gateway_port, GATEWAY_KEY)
    except OSError:
        sys.exit(f"Port {args.gateway_port} is already in use. Close what is using it, or run: python demo.py --gateway-port 8802")

    url = f"http://127.0.0.1:{args.port}/demo/"
    print()
    print("  PRACTICE SHOP is starting. Nothing here touches the real shop.")
    print(f"  Start page:          {url}")
    print(f"  Practice payment page: http://127.0.0.1:{args.gateway_port}  (a stand-in for PayMongo)")
    print("  Stop with Ctrl+C.")
    print()
    if not args.no_browser:
        threading.Timer(2.5, lambda: webbrowser.open(url)).start()
    call_command("runserver", f"127.0.0.1:{args.port}", use_reloader=False)


if __name__ == "__main__":
    main()
