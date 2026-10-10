"""Static files with a fingerprint in their name (style.3f9a1c.css), compressed, served by WhiteNoise -- browsers keep them for good,
because a changed file gets a new name.

One change from WhiteNoise's own storage: if a page names a static file that is not in the build (a typo, a file that was removed),
the page gets the plain name -- one missing picture or style, exactly what happens today -- instead of the whole page crashing.
`collectstatic` itself still stops loudly on a stylesheet that points at a missing file, so a broken build never goes live."""
from whitenoise.storage import CompressedManifestStaticFilesStorage


class ForgivingManifestStaticFilesStorage(CompressedManifestStaticFilesStorage):
    manifest_strict = False

    def stored_name(self, name):
        if not self.hashed_files:
            # No build index (staticfiles.json) -- local development and the tests: plain names, exactly as before, rather than
            # fingerprints worked out from whatever old files happen to sit in the local staticfiles folder. Render's build
            # always writes the index, so the live site always gets fingerprinted names.
            return name
        try:
            return super().stored_name(name)
        except ValueError:
            return name
