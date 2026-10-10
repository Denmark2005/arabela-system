"""Faster repeat visits: static files are saved with a fingerprint in their name so browsers can keep them for good -- and a page can
never crash because a template names a file that does not exist."""
import json
import tempfile
from pathlib import Path

from django.conf import settings
from django.templatetags.static import static
from django.test import SimpleTestCase, override_settings


class FingerprintedStaticFilesTests(SimpleTestCase):
    def test_static_files_are_saved_with_a_fingerprint_and_compressed(self):
        self.assertEqual(settings.STORAGES["staticfiles"]["BACKEND"], "arabela_system.storage.ForgivingManifestStaticFilesStorage")
        from whitenoise.storage import CompressedManifestStaticFilesStorage
        from arabela_system.storage import ForgivingManifestStaticFilesStorage
        self.assertTrue(issubclass(ForgivingManifestStaticFilesStorage, CompressedManifestStaticFilesStorage))

    def test_a_missing_file_never_crashes_a_page(self):
        self.assertIs(settings.WHITENOISE_MANIFEST_STRICT, False)

    def test_without_a_build_index_pages_get_plain_names_whatever_old_files_are_lying_around(self):
        root = Path(tempfile.mkdtemp(prefix="arabela-static-test-"))
        (root / "js").mkdir()
        (root / "js" / "collection-sort.js").write_text("old copy", encoding="utf-8")   # a leftover file, but no staticfiles.json
        with override_settings(STATIC_ROOT=str(root)):
            self.assertEqual(static("js/collection-sort.js"), "/static/js/collection-sort.js")
        (root / "js" / "collection-sort.js").unlink()
        (root / "js").rmdir()
        root.rmdir()

    def test_with_a_build_index_pages_get_the_fingerprinted_name_and_a_missing_file_falls_back_to_its_plain_name(self):
        root = Path(tempfile.mkdtemp(prefix="arabela-static-test-"))
        (root / "staticfiles.json").write_text(json.dumps({
            "version": "1.1", "hash": "x", "paths": {"js/collection-sort.js": "js/collection-sort.0123456789ab.js"},
        }), encoding="utf-8")
        with override_settings(STATIC_ROOT=str(root)):
            self.assertEqual(static("js/collection-sort.js"), "/static/js/collection-sort.0123456789ab.js")
            self.assertEqual(static("arabela_admin/css/not-there.css"), "/static/arabela_admin/css/not-there.css")
        (root / "staticfiles.json").unlink()
        root.rmdir()
