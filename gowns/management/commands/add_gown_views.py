"""python live.py add_gown_views --design "Coat Sequence Black, Green & Gray 1"

Gives the suits their side and back photos. For each suit design it takes the two pictures from the "Arabela Suits" folder, matches them to the
design's front photo (the same height on a white picture of the same size, gowns/view_fit.py), uploads them and sets them on every size of the
design. The front photo is never changed. A design that already has a view keeps it (--replace uploads it again).

    --design NAME     only this suit (can be repeated); without it, all 23 suits
    --dry-run         report what would happen: nothing is uploaded or saved
    --preview FOLDER  also write the matched pictures there, to look at them
    --folder FOLDER   where the pictures are (default: Downloads\\Arabela Suits\\views, one Suit_NN folder per suit)"""
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, default_storage
from django.core.management.base import BaseCommand, CommandError

from arabela_admin.views import _save_gown_photo
from gowns import design_details, photos, view_fit
from gowns.models import Gown

# The picture folders are numbered in the order of the names: Suit_01 is the first, Suit_23 the last.
SUITS = (
    "Chinese Collar Gray 1", "Chinese Collar Khaki 1", "Chinese Collar Light Blue 1", "Chinese Collar Light Gray 1", "Chinese Collar Light Gray 2",
    "Chinese Collar Navy Blue 1", "Chinese Collar Old Rose 1", "Chinese Collar Red 1", "Chinese Collar Red 2", "Chinese Collar Royal Blue 1",
    "Chinese Collar White 1", "Coat Sequence Black & Gold 1", "Coat Sequence Black 1", "Coat Sequence Black, Green & Gray 1",
    "Regular Collar Black 1", "Regular Collar Dusty Blue 1", "Regular Collar Gray 1", "Regular Collar Light Blue 1", "Regular Collar Light Gray 1",
    "Regular Collar Mint Green 1", "Regular Collar Purple 1", "Regular Collar Royal Blue 1", "Regular Collar White 1",
)
EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
DEFAULT_FOLDER = Path.home() / "Downloads" / "Arabela Suits" / "views"


def _shape(data):
    """How wide the garment is for its height (a side view is much narrower than a back view)."""
    left, top, right, bottom = view_fit.garment(view_fit.open_photo(data))[1]
    return (right - left) / (bottom - top)


class Command(BaseCommand):
    help = "Uploads the suits' side and back photos, matched to each front photo."

    def add_arguments(self, parser):
        parser.add_argument("--folder", default=str(DEFAULT_FOLDER))
        parser.add_argument("--design", action="append", default=[])
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--replace", action="store_true")
        parser.add_argument("--preview")

    def handle(self, *args, **options):
        folder = Path(options["folder"])
        if not folder.is_dir():
            raise CommandError(f"There is no folder {folder}. Point --folder at the folder holding Suit_01 ... Suit_23.")
        known = {name.casefold(): name for name in SUITS}
        unknown = [name for name in options["design"] if name.strip().casefold() not in known]
        if unknown:
            raise CommandError(f"Not one of the 23 suits: {', '.join(unknown)}. The names are:\n  " + "\n  ".join(SUITS))
        names = [known[name.strip().casefold()] for name in options["design"]] or list(SUITS)
        preview = Path(options["preview"]) if options["preview"] else None
        if preview:
            preview.mkdir(parents=True, exist_ok=True)
        dry = options["dry_run"]
        if not dry and "postgresql" in settings.DATABASES["default"]["ENGINE"] and isinstance(default_storage, FileSystemStorage):
            raise CommandError("This computer would save the pictures on its own disk (no CLOUDINARY_ settings in .env), so the shop's website "
                               "couldn't show them. Add the Cloudinary settings, then run it again.")
        done = skipped = 0
        for name in names:
            if self.one(name, SUITS.index(name) + 1, folder, preview, dry, options["replace"]):
                done += 1
            else:
                skipped += 1
        verb = "would get" if dry else "got"
        self.stdout.write(f"\n{done} suit design(s) {verb} side/back photos; {skipped} left as they were." + (" Nothing was saved (dry run)." if dry else ""))

    def one(self, name, number, folder, preview, dry, replace):
        units = list(Gown.objects.filter(category=Gown.Category.SUIT, name__iexact=name))
        if not units:
            self.stdout.write(f"{name}: no gown with this name in the Suit category, skipped")
            return False
        current = design_details.photos_of(units)
        wanted = [view for view in ("side", "back") if replace or not current[f"{view}_photo_url"]]
        if not wanted:
            self.stdout.write(f"{name}: already has its side and back, skipped")
            return False
        files = {}
        for view in ("side", "back"):
            candidates = sorted(p for p in (folder / f"Suit_{number:02d}").glob(f"Suit_{number:02d}_{view.title()}.*") if p.suffix.lower() in EXTENSIONS)
            if candidates:
                files[view] = candidates[0].read_bytes()
        try:
            if len(files) == 2 and _shape(files["side"]) > _shape(files["back"]):
                files = {"side": files["back"], "back": files["side"]}
                self.stdout.write(f"{name}: the Side and Back files are the wrong way round, so they are used swapped")
        except view_fit.CannotFit:
            pass        # reported below, for the picture that can't be used
        front = photos.read(current["photo_url"])
        if front is None:
            self.stdout.write(f"{name}: its front photo couldn't be read just now, skipped")
            return False
        fitted = {}
        for view in wanted:
            if view not in files:
                self.stdout.write(f"{name}: no {view.title()} picture in Suit_{number:02d}")
                continue
            try:
                fitted[view] = view_fit.fit_to_front(files[view], front)
            except view_fit.CannotFit as reason:
                self.stdout.write(f"{name}: the {view} picture isn't used: {reason}")
        if not fitted:
            return False
        if preview:
            for view, data in fitted.items():
                (preview / f"Suit_{number:02d}_{view.title()}.jpg").write_bytes(data)
        views = " and ".join(fitted)
        if dry:
            self.stdout.write(f"{name}: would upload the {views} to {len(units)} gown(s)")
            return True
        for view, data in fitted.items():
            url = _save_gown_photo(ContentFile(data, name=f"Suit_{number:02d}_{view.title()}.jpg"))
            design_details.share_photos(Gown.Category.SUIT, name, {f"{view}_photo_url": url})
        self.stdout.write(f"{name}: {views} uploaded to {len(units)} gown(s)")
        return True
