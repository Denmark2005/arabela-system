"""The fabric, description and photos customers see on a gown's page.

They belong to the DESIGN: every physical gown (size or extra unit) with the same name in a category is one product on the customer site
(gowns.models.group_gowns_by_name), so all of them always carry the same text and the same front, side and back photos. Add Gown, Edit Gown and
the photo window go through here, so changing them on one size changes them on all of them. Staff's private "notes" are a different thing and
never shown to customers."""
import re

from django.utils import timezone

from gowns.models import Gown

FABRIC_MAX = 200
DESCRIPTION_MAX = 1000
PHOTO_FIELDS = ("photo_url", "side_photo_url", "back_photo_url")      # front, side, back


def clean_fabric(raw):
    """One line: extra spaces and line breaks become single spaces."""
    return " ".join(str(raw or "").split())


def clean_description(raw):
    """The text as typed, with tidy spacing: Windows line endings made plain, spaces squeezed, never more than one empty line in a row."""
    lines = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    text = "\n".join(" ".join(line.split()) for line in lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def problem(fabric, description):
    """'' when both fit, else the sentence staff can act on."""
    if len(fabric) > FABRIC_MAX:
        return f"Fabric must be {FABRIC_MAX} characters or fewer."
    if len(description) > DESCRIPTION_MAX:
        return f"Description must be {DESCRIPTION_MAX:,} characters or fewer."
    return ""


def units_of(category, name, *, exclude_pk=None):
    """Every gown of one design."""
    units = Gown.objects.filter(category=category, name__iexact=str(name or "").strip())
    return units.exclude(pk=exclude_pk) if exclude_pk is not None else units


def text_of(units):
    """(fabric, description) of a design: the first of each that any unit has, oldest unit first."""
    units = sorted(units, key=lambda g: g.pk or 0)
    return (next((u.fabric for u in units if u.fabric), ""), next((u.description for u in units if u.description), ""))


def share(category, name, fabric, description):
    """Gives every gown of the design this text. Returns how many gowns the design has."""
    units = units_of(category, name)
    units.exclude(fabric=fabric, description=description).update(fabric=fabric, description=description, updated_at=timezone.now())
    return units.count()


def photos_of(units):
    """The design's front, side and back photo ({field: address}): the first of each that any unit has, oldest unit first."""
    units = sorted(units, key=lambda g: g.pk or 0)
    return {field: next((getattr(u, field) for u in units if getattr(u, field)), "") for field in PHOTO_FIELDS}


def share_photos(category, name, photos):
    """Gives every gown of the design these photos ({field: address}; "" takes one away). Returns how many gowns the design has."""
    units = units_of(category, name)
    if photos:
        units.exclude(**photos).update(**photos, updated_at=timezone.now())
    return units.count()
