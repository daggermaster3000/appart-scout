"""Flatfox adapter.

Flatfox exposes a genuinely public JSON endpoint, `/api/v1/public-listing/`, but
it has three quirks that shape this adapter:

1. It ignores every filter parameter. `object_category=APARTMENT`, `zipcode=...`,
   `ordering=...` all come back with the identical unfiltered result set, so all
   filtering has to happen client-side.
2. `limit` is silently capped at 100.
3. The result set is ordered by primary key *ascending*, i.e. the ~35k rows are
   oldest-first and **the newest listings are at the very end**.

So instead of paging forward through 35k rows every run, we read `count`, then
page backwards from the tail until we reach a `pk` we already imported. After
the first run that is typically one or two requests.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..models import Criteria, Image, Listing, Settings
from ..normalize import (
    clean_text,
    detect_amenities,
    map_category,
    parse_date,
    parse_datetime,
    parse_zipcode,
    to_float,
    to_int,
)
from .base import Source

log = logging.getLogger(__name__)

BASE = "https://flatfox.ch"
ENDPOINT = f"{BASE}/api/v1/public-listing/"
PAGE_SIZE = 100  # hard cap enforced by the API

# Flatfox's structured attribute flags -> our vocabulary.
ATTRIBUTE_MAP = {
    "balconygarden": "BALCONY",
    "lift": "LIFT",
    "dishwasher": "DISHWASHER",
    "washingmachine": "WASHING_MACHINE",
    "parkingspace": "PARKING",
    "garage": "GARAGE",
    "petsallowed": "PETS_ALLOWED",
    "accessiblewithwheelchair": "WHEELCHAIR",
    "fireplace": "FIREPLACE",
    "minergie": "MINERGIE",
}


class FlatfoxSource(Source):
    name = "flatfox"
    label = "Flatfox"
    rate_limit = 0.7

    #: how far back to page on a cold database (pages, not listings)
    seed_pages = 40
    #: safety valve so a cursor mishap can never turn into a full 35k crawl
    max_pages = 60

    async def fetch(
        self,
        client: httpx.AsyncClient,
        criteria: Criteria,
        settings: Settings,
        state: dict[str, Any],
    ) -> tuple[list[Listing], dict[str, Any]]:
        head = await self.get_json(client, ENDPOINT, params={"limit": 1, "offset": 0})
        total = int(head.get("count") or 0)
        if not total:
            return [], state

        last_pk = int(state.get("max_pk") or 0)
        page_budget = self.max_pages if last_pk else self.seed_pages

        listings: list[Listing] = []
        seen_pks: set[int] = set()
        highest_pk = last_pk
        offset = max(0, total - PAGE_SIZE)
        pages = 0

        while pages < page_budget and offset >= 0:
            payload = await self.get_json(
                client,
                ENDPOINT,
                params={"limit": PAGE_SIZE, "offset": offset, "expand": "images"},
            )
            results = payload.get("results") or []
            if not results:
                break

            reached_known = False
            for raw in results:
                pk = to_int(raw.get("pk"))
                if pk is None or pk in seen_pks:
                    continue
                seen_pks.add(pk)
                highest_pk = max(highest_pk, pk)
                if last_pk and pk <= last_pk:
                    # Everything below here was imported on a previous run.
                    reached_known = True
                    continue
                listing = parse_listing(raw)
                if listing is not None:
                    listings.append(listing)

            pages += 1
            if reached_known or offset == 0:
                break
            offset = max(0, offset - PAGE_SIZE)

        log.info(
            "flatfox: %d pages, %d new listings (cursor %s -> %s)",
            pages,
            len(listings),
            last_pk,
            highest_pk,
        )
        return listings, {"max_pk": highest_pk}


def parse_listing(raw: dict[str, Any]) -> Listing | None:
    """Map one Flatfox record onto our `Listing`. Returns None if unusable."""
    pk = to_int(raw.get("pk"))
    url = raw.get("url") or raw.get("short_url")
    if pk is None or not url:
        return None
    if str(raw.get("offer_type") or "").upper() != "RENT":
        return None
    if str(raw.get("status") or "act") not in ("act", "ACTIVE", "active"):
        return None

    # `object_category` is the coarse bucket (APARTMENT/HOUSE/PARK/...) and
    # `object_type` the specific one (ATTIC, ROW_HOUSE, ...). Prefer the coarse
    # one, fall back to the specific.
    category = map_category(raw.get("object_category"))
    if category == "OTHER":
        category = map_category(raw.get("object_type"))

    # rent_gross already includes charges; otherwise add them up ourselves.
    net = to_int(raw.get("rent_net"))
    charges = to_int(raw.get("rent_charges"))
    gross = to_int(raw.get("rent_gross"))
    if gross is None:
        if net is not None:
            gross = net + (charges or 0)
        elif str(raw.get("price_display_type") or "") == "TOTAL":
            gross = to_int(raw.get("price_display"))

    space = to_int(raw.get("surface_living")) or to_int(raw.get("livingspace"))
    description = clean_text(raw.get("description"))
    title = clean_text(raw.get("public_title") or raw.get("short_title"))

    flags = [
        str(a.get("name", "")) if isinstance(a, dict) else str(a)
        for a in (raw.get("attributes") or [])
    ]
    amenities = {ATTRIBUTE_MAP[f] for f in flags if f in ATTRIBUTE_MAP}
    # Structured flags are sparse on Flatfox, so top them up from the prose.
    amenities.update(detect_amenities(description, title))

    available_from, immediate = parse_date(raw.get("moving_date"))
    if str(raw.get("moving_date_type") or "") == "agr" and available_from is None:
        immediate = True

    return Listing(
        source="flatfox",
        source_id=str(pk),
        url=url if url.startswith("http") else f"{BASE}{url}",
        title=title,
        description=description,
        price_chf=gross,
        price_net_chf=net,
        charges_chf=charges,
        rooms=to_float(raw.get("number_of_rooms")),
        living_space_m2=space,
        floor=to_int(raw.get("floor")),
        street=clean_text(raw.get("street")),
        zipcode=parse_zipcode(raw.get("zipcode")),
        city=clean_text(raw.get("city")),
        lat=to_float(raw.get("latitude")),
        lon=to_float(raw.get("longitude")),
        available_from=available_from,
        available_immediately=immediate,
        category=category,
        is_furnished=bool(raw.get("is_furnished")),
        is_temporary=bool(raw.get("is_temporary")),
        year_built=to_int(raw.get("year_built")),
        year_renovated=to_int(raw.get("year_renovated")),
        amenities=sorted(amenities),
        images=_parse_images(raw.get("images")),
        published=parse_datetime(raw.get("published") or raw.get("created")),
    )


def _parse_images(images: Any) -> list[Image]:
    if not isinstance(images, list):
        return []
    out: list[Image] = []
    for i, img in enumerate(images):
        if not isinstance(img, dict):
            continue  # un-expanded response: bare integer ids, no URL to use
        url = img.get("url")
        if not url:
            continue
        thumb = img.get("url_listing_search") or img.get("url_thumb_m")
        out.append(
            Image(
                url=url if url.startswith("http") else f"{BASE}{url}",
                thumb_url=(
                    None if not thumb else thumb if thumb.startswith("http") else f"{BASE}{thumb}"
                ),
                caption=clean_text(img.get("caption")),
                ordering=to_int(img.get("ordering")) or i,
            )
        )
    return sorted(out, key=lambda im: im.ordering)
