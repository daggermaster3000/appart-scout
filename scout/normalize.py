"""Helpers for turning vendor payloads into `Listing` fields.

Every portal spells the same facts differently ("3.5 Zimmer", "3,5 Zi.", rooms
as a string, price as "CHF 2'350.–"), and only some of them expose amenities as
structured flags - the rest bury them in the description. These helpers are
shared by all adapters so the messiness lives in one place.
"""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import date, datetime

from .models import AMENITIES, Listing, ObjectCategory

# --------------------------------------------------------------------------
# scalars
# --------------------------------------------------------------------------

_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def to_float(value: object) -> float | None:
    """Parse a number out of anything a portal might hand us."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("'", "").replace("’", "").replace("\xa0", " ")
    match = _NUM_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group().replace(",", "."))
    except ValueError:
        return None


def to_int(value: object) -> int | None:
    f = to_float(value)
    return int(round(f)) if f is not None else None


def parse_date(value: object) -> tuple[date | None, bool]:
    """Return (date, available_immediately).

    Swiss listings very often say "sofort" / "per sofort" / "nach Vereinbarung"
    instead of a date.
    """
    if value is None:
        return None, False
    if isinstance(value, date) and not isinstance(value, datetime):
        return value, False
    if isinstance(value, datetime):
        return value.date(), False
    text = str(value).strip().lower()
    if not text:
        return None, False
    if any(w in text for w in ("sofort", "immediately", "de suite", "subito", "now")):
        return None, True
    if any(w in text for w in ("vereinbarung", "agreement", "convenir", "accordo")):
        return None, True
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date(), False
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("z", "+00:00")).date(), False
    except ValueError:
        return None, False


def parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text[: len(fmt) + 4], fmt)
        except ValueError:
            continue
    return None


def parse_zipcode(value: object) -> int | None:
    z = to_int(value)
    # Swiss postcodes are 1000-9999. Anything else is a parsing accident.
    return z if z and 1000 <= z <= 9999 else None


def clean_text(value: object) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = re.sub(r"<[^>]+>", " ", text)  # some portals return HTML descriptions
    text = html.unescape(text)  # ...with entities still in them
    return re.sub(r"[ \t]*\n[ \t]*", "\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


# --------------------------------------------------------------------------
# categories
# --------------------------------------------------------------------------

_CATEGORY_MAP = {
    "APARTMENT": "APARTMENT",
    "FLAT": "APARTMENT",
    "ATTIC": "APARTMENT",
    "LOFT": "APARTMENT",
    "STUDIO": "APARTMENT",
    "DUPLEX": "APARTMENT",
    "ROOF_FLAT": "APARTMENT",
    "HOUSE": "HOUSE",
    "SINGLE_HOUSE": "HOUSE",
    "ROW_HOUSE": "HOUSE",
    "TERRACE_HOUSE": "HOUSE",
    "VILLA": "HOUSE",
    "SHARED": "SHARED",
    "SHARED_FLAT": "SHARED",
    "ROOM": "SHARED",
}


def map_category(value: object) -> ObjectCategory:
    key = str(value or "").upper().replace("-", "_").replace(" ", "_")
    return _CATEGORY_MAP.get(key, "OTHER")  # type: ignore[return-value]


# --------------------------------------------------------------------------
# amenities
# --------------------------------------------------------------------------

# Matched against the joined lowercase text of structured attribute flags plus
# the description. German first (the corridor is German-speaking), then French
# and English for the portals that localize.
_AMENITY_PATTERNS: dict[str, tuple[str, ...]] = {
    "BALCONY": ("balkon", "balcon", "balcony"),
    "TERRACE": ("terrasse", "terrace", "sitzplatz"),
    "GARDEN": ("garten", "jardin", "garden"),
    "LIFT": ("lift", "aufzug", "ascenseur", "elevator"),
    "DISHWASHER": ("geschirrspüler", "geschirrspueler", "spülmaschine", "lave-vaisselle", "dishwasher", "gs "),
    "WASHING_MACHINE": (
        "waschmaschine", "eigene waschmaschine", "waschturm",
        "machine à laver", "washing machine", "wm/tumbler",
    ),
    "PARKING": ("parkplatz", "aussenparkplatz", "place de parc", "parking"),
    "GARAGE": ("garage", "einstellhalle", "einstellplatz"),
    "PETS_ALLOWED": ("haustiere erlaubt", "tiere erlaubt", "animaux admis", "pets allowed"),
    "WHEELCHAIR": ("rollstuhl", "behindertengerecht", "hindernisfrei", "wheelchair"),
    "FIREPLACE": ("cheminée", "cheminee", "kamin", "fireplace"),
    "CELLAR": ("keller", "kellerabteil", "cave", "cellar"),
    "QUIET": ("ruhige lage", "ruhig gelegen", "calme", "quiet location"),
    "NEW_BUILD": ("neubau", "erstvermietung", "nouvelle construction", "new build"),
    "MINERGIE": ("minergie",),
}

# Words that mean the amenity is explicitly absent; cheap guard against
# "keine Haustiere erlaubt" being read as pets-allowed.
_NEGATIONS = ("kein", "keine", "nicht", "ohne", "no ", "pas de", "sans")


def detect_amenities(*texts: str) -> list[str]:
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return []
    found: list[str] = []
    for amenity, patterns in _AMENITY_PATTERNS.items():
        for pattern in patterns:
            idx = blob.find(pattern)
            if idx < 0:
                continue
            window = blob[max(0, idx - 30) : idx]
            if any(neg in window for neg in _NEGATIONS):
                continue
            found.append(amenity)
            break
    return [a for a in AMENITIES if a in found]  # stable order


# --------------------------------------------------------------------------
# dedup key
# --------------------------------------------------------------------------

_STREET_NOISE = re.compile(r"[^a-zäöüß0-9]+")
_STREET_SUFFIX = re.compile(r"(strasse|str\.?|gasse|weg|platz|allee|rue|chemin|via)\b")


def normalize_street(street: str) -> str:
    """Collapse "Bahnhofstrasse 12a" and "Bahnhofstr. 12 A" onto one token."""
    text = unicodedata.normalize("NFKC", street or "").lower()
    text = _STREET_SUFFIX.sub("str", text)
    return _STREET_NOISE.sub("", text)


def dedup_key(listing: Listing) -> str:
    """Coarse bucket key. Listings sharing a bucket are compared pairwise.

    Only fields that portals agree on go in here. Price and size deliberately do
    not: the same flat is routinely advertised at 2199 on one portal and 2201 on
    another, or with the size given on one and omitted on the other, and any
    bucketing of those puts the two copies in different buckets where they can
    never be compared. `dedup.same_flat` compares them with tolerance instead.
    """
    rooms = listing.rooms or 0
    return f"{listing.zipcode or 0}|{rooms:g}"
