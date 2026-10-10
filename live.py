#!/usr/bin/env python
"""Runs ONE manage.py command against the LIVE database, even when the laptop has its own (.env.laptop-db).

    python live.py migrate
    python live.py backup_database

Only this one command uses the live database; the next plain `python manage.py ...` is back on the laptop copy.
See arabela_system/laptop_db.py."""
import os
import sys

import manage

if __name__ == '__main__':
    os.environ['ARABELA_LIVE_DB'] = '1'
    print("Using the LIVE database for this command.", file=sys.stderr)
    manage.main()
