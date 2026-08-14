"""Cross-portal deduplication.

The same flat is routinely listed on Homegate, ImmoScout24 and Comparis at once
(they syndicate from the same agency feeds), and Comparis in particular is an
aggregator. Without merging, the digest would be three quarters duplicates.

Strategy: bucket by a coarse key (postcode + rounded price + rooms + rounded
size), then within a bucket confirm with fuzzy street matching. Both halves are
needed - the bucket alone would merge two different flats in the same building,
and street matching alone would be O(n^2) across thousands of listings.

The merged record keeps every source URL, so the digest can say "also on
Homegate" and link out to each.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict

from rapidfuzz import fuzz

from .models import Listing
from .normalize import dedup_key, normalize_street

STREET_MATCH_THRESHOLD = 82
#: Portals round rent and floor area differently, and agencies update one site
#: before the other. Compare with tolerance rather than for equality.
PRICE_TOLERANCE = 0.05
SPACE_TOLERANCE = 0.10

#: Preferred source when merging conflicting field values. Flatfox first because
#: its payload is the richest and has real coordinates.
SOURCE_PRIORITY = ["flatfox", "homegate", "immoscout", "newhome", "comparis"]


class MergedListing:
    """One physical flat, plus every platform it was found on."""

    def __init__(self, listings: list[Listing]) -> None:
        self.listings = sorted(listings, key=_priority)
        self.primary = self.listings[0]
        self.id = listing_id(self.primary)

    @property
    def sources(self) -> list[Listing]:
        return self.listings

    def merged(self) -> Listing:
        """Primary record, with gaps filled in from the other platforms."""
        base = self.primary.model_copy(deep=True)
        for other in self.listings[1:]:
            for field in (
                "price_chf", "price_net_chf", "charges_chf", "rooms", "living_space_m2",
                "floor", "street", "zipcode", "city", "lat", "lon", "available_from",
                "year_built", "year_renovated", "published", "title", "description",
            ):
                if _is_empty(getattr(base, field)):
                    setattr(base, field, getattr(other, field))
            # Amenities and photos are additive: one portal often has the
            # structured flags while another has the better gallery.
            base.amenities = sorted(set(base.amenities) | set(other.amenities))
            if len(other.images) > len(base.images):
                base.images = other.images
            base.available_immediately = base.available_immediately or other.available_immediately
        return base


def _is_empty(value: object) -> bool:
    return value is None or value == "" or value == 0


def _priority(listing: Listing) -> tuple[int, str]:
    try:
        rank = SOURCE_PRIORITY.index(listing.source)
    except ValueError:
        rank = len(SOURCE_PRIORITY)
    return rank, listing.source_id


def listing_id(listing: Listing) -> str:
    """Stable id derived from the physical flat, not from any portal's id."""
    seed = f"{dedup_key(listing)}|{normalize_street(listing.street)}"
    return hashlib.sha1(seed.encode()).hexdigest()[:16]


def _within(a: float | None, b: float | None, tolerance: float) -> bool:
    """True if both are absent, or present and within `tolerance` of each other.

    A value missing on one side is not evidence of a different flat - portals
    routinely omit floor area - so it is not treated as a mismatch.
    """
    if a is None or b is None:
        return True
    if a == b:
        return True
    return abs(a - b) <= tolerance * max(a, b)


def same_flat(a: Listing, b: Listing) -> bool:
    if a.zipcode != b.zipcode:
        return False
    if a.rooms is not None and b.rooms is not None and a.rooms != b.rooms:
        return False
    if not _within(a.price_chf, b.price_chf, PRICE_TOLERANCE):
        return False
    if not _within(a.living_space_m2, b.living_space_m2, SPACE_TOLERANCE):
        return False

    street_a, street_b = normalize_street(a.street), normalize_street(b.street)
    if street_a and street_b:
        return fuzz.ratio(street_a, street_b) >= STREET_MATCH_THRESHOLD
    # No usable street on at least one side. Same postcode, same room count and
    # a matching rent is already a tight coincidence, so accept it - a false
    # merge costs one duplicate hidden, a false split costs a duplicate shown.
    return a.price_chf is not None and b.price_chf is not None


def merge(listings: list[Listing]) -> list[MergedListing]:
    buckets: dict[str, list[Listing]] = defaultdict(list)
    for listing in listings:
        buckets[dedup_key(listing)].append(listing)

    merged: list[MergedListing] = []
    for bucket in buckets.values():
        groups: list[list[Listing]] = []
        for listing in bucket:
            for group in groups:
                if same_flat(group[0], listing):
                    group.append(listing)
                    break
            else:
                groups.append([listing])
        merged.extend(MergedListing(group) for group in groups)
    return merged
