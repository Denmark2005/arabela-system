"""gunicorn.conf.py (the live site's web server settings): one process with several threads and a timeout that fits the receipt check.

gunicorn itself cannot run on Windows, so these tests read the settings file the way gunicorn does (it is a plain Python file) and check the values
against what the live site needs."""
import os
import runpy
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase

CONF = Path(settings.BASE_DIR) / "gunicorn.conf.py"


def load(**env):
    with patch.dict(os.environ, env, clear=False):
        for name in ("WEB_CONCURRENCY", "GUNICORN_THREADS", "GUNICORN_TIMEOUT"):
            if name not in env:
                os.environ.pop(name, None)
        return runpy.run_path(str(CONF))


class GunicornConfTests(SimpleTestCase):
    def test_gunicorn_finds_the_file_by_itself_in_the_project_folder(self):
        self.assertTrue(CONF.is_file())
        self.assertEqual(CONF.name, "gunicorn.conf.py")                         # the name gunicorn looks for in the folder it starts in
        self.assertEqual(CONF.parent, Path(settings.BASE_DIR))

    def test_the_defaults_are_one_process_with_several_threads(self):
        values = load()
        self.assertEqual(values["worker_class"], "gthread")
        self.assertEqual(values["workers"], 1)                                   # the free plan's memory only holds one process
        self.assertGreaterEqual(values["threads"], 2)                            # so a slow request never stops the others
        self.assertLessEqual(values["threads"], 8)                               # but a handful of 5 MB pictures at once must fit in memory

    def test_a_slow_booking_has_time_to_finish_and_the_hosting_edge_is_not_beaten(self):
        timeout = load()["timeout"]
        self.assertGreater(timeout, 30)                                          # the old default killed Confirm Rental on a slow moment
        self.assertGreaterEqual(timeout, 60)                                     # the receipt check (about 20 s) + the picture's upload + the saving
        self.assertLess(timeout, 100)                                            # the hosting's edge answers with its own error page at 100 s

    def test_the_numbers_can_be_changed_from_the_environment(self):
        values = load(WEB_CONCURRENCY="2", GUNICORN_THREADS="6", GUNICORN_TIMEOUT="75")
        self.assertEqual((values["workers"], values["threads"], values["timeout"]), (2, 6, 75))

    def test_every_value_is_a_whole_number_gunicorn_accepts(self):
        values = load()
        for name in ("workers", "threads", "timeout", "graceful_timeout", "keepalive"):
            with self.subTest(setting=name):
                self.assertIsInstance(values[name], int)
                self.assertGreater(values[name], 0)

    def test_only_settings_gunicorn_knows_are_set(self):
        known = {"worker_class", "workers", "threads", "timeout", "graceful_timeout", "keepalive"}
        values = {k for k in load() if not k.startswith("_") and k not in ("os",)}
        self.assertEqual(values - {"__doc__"}, known)
