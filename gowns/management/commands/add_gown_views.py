"""python live.py add_gown_views --design "Coat Sequence Black, Green & Gray 1"
python live.py add_gown_views --wedding
python live.py add_gown_views --mother

Gives gowns their side and back photos: the suits, or with --wedding / --mother the wedding or mother gowns. For each design it takes the two
pictures from that set's folder, matches them to the design's front photo (the same height on a white picture of the same size,
gowns/view_fit.py), uploads them and sets them on every size of the design. The front photo is never changed. A design that already has a view
keeps it (--replace uploads it again).

    --wedding         the wedding gowns instead of the suits
    --mother          the mother gowns instead of the suits
    --design NAME     only this design (can be repeated); without it, the whole set
    --dry-run         report what would happen: nothing is uploaded or saved
    --preview FOLDER  also write the matched pictures there, to look at them
    --folder FOLDER   where the pictures are. Suits: Downloads\\Arabela Suits\\views, one Suit_NN folder per suit.
                      Wedding gowns: Downloads\\Arabela Wedding Gowns\\views, one Gown_NN folder per gown (Gown_07 is Wedding Gown 7).
                      Mother gowns: Downloads\\Arabela Mother Gowns\\views, one Mother_Gown_NN folder per gown, numbered in the order of
                      the names (01 is Beige Mother Gown 1, 30 is Silver Mother Gown 4)."""
from pathlib import Path
from typing import NamedTuple

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
WEDDING_GOWNS = tuple(f"Wedding Gown {number}" for number in range(1, 29))
MOTHER_GOWNS = tuple(f"{colour} Mother Gown {number}" for colour, count in (
    ("Beige", 3), ("Blue", 3), ("Champagne", 3), ("Emerald Green", 3), ("Gold", 6), ("Navy Blue", 1), ("Pink", 4), ("Purple", 1),
    ("Rose Gold", 2), ("Silver", 4)) for number in range(1, count + 1))
# These pictures show a mannequin head or a stand in one view and not in the front, so matching the whole figure would shrink the dress. They are
# matched by where the dress starts and ends instead (view_fit.fit_to_front, spans): ((front top, bottom), (view top, bottom)), as shares of
# each picture's height, read off these designs' present photos.
BY_THE_DRESS = {
    "Champagne Mother Gown 1": {"side": ((0.277, 0.877), (0.299, 0.872)), "back": ((0.277, 0.877), (0.264, 0.855))},
    "Champagne Mother Gown 3": {"side": ((0.195, 0.878), (0.108, 0.825)), "back": ((0.195, 0.878), (0.097, 0.856))},
    "Rose Gold Mother Gown 2": {"side": ((0.21, 0.985), (0.21, 0.985)), "back": ((0.21, 0.985), (0.21, 0.985))},
}
EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


class PictureSet(NamedTuple):
    category: str
    names: tuple            # the first name's pictures are number 01, the next 02, and so on
    prefix: str             # folder and file names: Suit_01\Suit_01_Side.png
    folder: Path            # where the pictures are, unless --folder says otherwise
    kind: str               # what one is called in messages
    tells_swapped: bool     # a suit's side is always narrower than its back, so swapped files show; a wedding gown's train can make its side wider


SUIT_SET = PictureSet(Gown.Category.SUIT, SUITS, "Suit", Path.home() / "Downloads" / "Arabela Suits" / "views", "suit", True)
WEDDING_SET = PictureSet(Gown.Category.WEDDING_GOWN, WEDDING_GOWNS, "Gown", Path.home() / "Downloads" / "Arabela Wedding Gowns" / "views",
                         "wedding gown", False)
MOTHER_SET = PictureSet(Gown.Category.MOTHER_GOWN, MOTHER_GOWNS, "Mother_Gown", Path.home() / "Downloads" / "Arabela Mother Gowns" / "views",
                        "mother gown", False)


def _shape(data):
    """How wide the garment is for its height (a side view is much narrower than a back view)."""
    left, top, right, bottom = view_fit.garment(view_fit.open_photo(data))[1]
    return (right - left) / (bottom - top)


class Command(BaseCommand):
    help = "Uploads the suits' (or the wedding or mother gowns') side and back photos, matched to each front photo."

    def add_arguments(self, parser):
        which = parser.add_mutually_exclusive_group()
        which.add_argument("--wedding", action="store_true")
        which.add_argument("--mother", action="store_true")
        parser.add_argument("--folder")
        parser.add_argument("--design", action="append", default=[])
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--replace", action="store_true")
        parser.add_argument("--preview")

    def handle(self, *args, **options):
        pictures = WEDDING_SET if options["wedding"] else MOTHER_SET if options["mother"] else SUIT_SET
        folder = Path(options["folder"] or pictures.folder)
        if not folder.is_dir():
            raise CommandError(f"There is no folder {folder}. Point --folder at the folder holding {pictures.prefix}_01 ... "
                               f"{pictures.prefix}_{len(pictures.names):02d}.")
        known = {name.casefold(): name for name in pictures.names}
        unknown = [name for name in options["design"] if name.strip().casefold() not in known]
        if unknown:
            raise CommandError(f"Not one of the {len(pictures.names)} {pictures.kind}s: {', '.join(unknown)}. The names are:\n  "
                               + "\n  ".join(pictures.names))
        names = [known[name.strip().casefold()] for name in options["design"]] or list(pictures.names)
        preview = Path(options["preview"]) if options["preview"] else None
        if preview:
            preview.mkdir(parents=True, exist_ok=True)
        dry = options["dry_run"]
        if not dry and "postgresql" in settings.DATABASES["default"]["ENGINE"] and isinstance(default_storage, FileSystemStorage):
            raise CommandError("This computer would save the pictures on its own disk (no CLOUDINARY_ settings in .env), so the shop's website "
                               "couldn't show them. Add the Cloudinary settings, then run it again.")
        done = skipped = 0
        for name in names:
            if self.one(pictures, name, folder, preview, dry, options["replace"]):
                done += 1
            else:
                skipped += 1
        verb = "would get" if dry else "got"
        self.stdout.write(f"\n{done} {pictures.kind} design(s) {verb} side/back photos; {skipped} left as they were."
                          + (" Nothing was saved (dry run)." if dry else ""))

    def one(self, pictures, name, folder, preview, dry, replace):
        stem = f"{pictures.prefix}_{pictures.names.index(name) + 1:02d}"
        units = list(Gown.objects.filter(category=pictures.category, name__iexact=name))
        if not units:
            self.stdout.write(f"{name}: no gown with this name in the {pictures.category.label} category, skipped")
            return False
        current = design_details.photos_of(units)
        wanted = [view for view in ("side", "back") if replace or not current[f"{view}_photo_url"]]
        if not wanted:
            self.stdout.write(f"{name}: already has its side and back, skipped")
            return False
        files = {}
        for view in ("side", "back"):
            candidates = sorted(p for p in (folder / stem).glob(f"{stem}_{view.title()}.*") if p.suffix.lower() in EXTENSIONS)
            if candidates:
                files[view] = candidates[0].read_bytes()
        try:
            if pictures.tells_swapped and len(files) == 2 and _shape(files["side"]) > _shape(files["back"]):
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
                self.stdout.write(f"{name}: no {view.title()} picture in {stem}")
                continue
            try:
                fitted[view] = view_fit.fit_to_front(files[view], front, BY_THE_DRESS.get(name, {}).get(view))
            except view_fit.CannotFit as reason:
                self.stdout.write(f"{name}: the {view} picture isn't used: {reason}")
        if not fitted:
            return False
        if preview:
            for view, data in fitted.items():
                (preview / f"{stem}_{view.title()}.jpg").write_bytes(data)
        views = " and ".join(fitted)
        if dry:
            self.stdout.write(f"{name}: would upload the {views} to {len(units)} gown(s)")
            return True
        for view, data in fitted.items():
            url = _save_gown_photo(ContentFile(data, name=f"{stem}_{view.title()}.jpg"))
            design_details.share_photos(pictures.category, name, {f"{view}_photo_url": url})
        self.stdout.write(f"{name}: {views} uploaded to {len(units)} gown(s)")
        return True
