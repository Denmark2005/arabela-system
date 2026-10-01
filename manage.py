#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import socket
import sys


def _prefer_ipv4():
    """Try a host's IPv4 addresses before its IPv6 ones.

    On this network Windows lists the Supabase pooler's dead NAT64 IPv6 addresses
    (64:ff9b::...) first, and every database connection then waits for them to time out
    (tens of seconds per page) before falling back to the IPv4 address that answers in
    0.1s. Reordering the lookup results is harmless wherever IPv6 works, and it only
    affects commands run through this file (runserver, shell, test, migrate).
    """
    original = socket.getaddrinfo

    def ipv4_first(*args, **kwargs):
        results = original(*args, **kwargs)
        return sorted(results, key=lambda result: 0 if result[0] == socket.AF_INET else 1)

    socket.getaddrinfo = ipv4_first


def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'arabela_system.settings')
    _prefer_ipv4()
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
