"""How the live site's web server (gunicorn) runs. gunicorn reads this file by itself from the folder it is started in, so the Render start command
can stay the plain `gunicorn arabela_system.wsgi`. (A flag written in the start command, such as --workers, --threads or --timeout, wins over
this file for that one setting.)

Why it exists (2026-11-18). The site used gunicorn's defaults: ONE worker that handles ONE request at a time, and a request that takes more than
30 seconds is killed. Confirm Rental reads the receipt picture (up to about 20 seconds), uploads it and saves the booking inside one request, and
while it ran EVERYBODY else waited behind it (six page loads started together finished one after another, about a second apart). On the day of the
defense that made Confirm Rental answer with an error page, which the checkout shows as "Server busy".

Now: still ONE worker process (the free hosting plan has little memory, and a second process would double it), but with several threads, so a
request that is waiting on Google or the database does not stop the others; and a slow request has 90 seconds instead of 30 (the receipt check
alone is limited to about 20, and the hosting's own edge gives up at 100).

Every number can be changed from Render's environment settings without a new deploy: WEB_CONCURRENCY, GUNICORN_THREADS, GUNICORN_TIMEOUT.
"""
import os

worker_class = "gthread"
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "90"))
graceful_timeout = 30                 # a worker that is being replaced gets this long to finish what it is doing
keepalive = 5                         # the hosting's proxy reuses its connections to the app
