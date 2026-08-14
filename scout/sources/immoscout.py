"""ImmoScout24 adapter (browser-backed).

ImmoScout24 refuses plain HTTP but renders fine in a headed Chromium, and - the
useful part - ships its entire result set as JSON in `window.__INITIAL_STATE__`
under `resultList.search.fullSearch.result`. So this adapter never touches the
DOM: it loads the search URL and reads the page's own hydration state.

Search filters are plain URL query parameters (verified against the resulting
`searchModel` echoed back in that same state):

    pf / pt   price from / to (CHF, gross)
    nrf / nrt number of rooms from / to
    slf       living space from (m2)
    pn        page number, 20 results per page
    o         sort; `dateCreated-desc` is newest-first

This is also the closest thing we have to Homegate coverage: each listing
carries a `platforms` list, and Homegate/ImmoScout24 listings are syndicated
across both (same parent company).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..browser import Blocked, dig, extract_state
from ..models import Criteria, Image, Listing, Settings
from ..normalize import (
    clean_text,
    detect_amenities,
    map_category,
    parse_datetime,
    parse_zipcode,
    to_float,
    to_int,
)
from .base import Source, SourceError

log = logging.getLogger(__name__)

BASE = "https://www.immoscout24.ch"
PAGE_SIZE = 20

# Canton code -> the slug ImmoScout24 uses in its search path.
CANTON_SLUGS = {
    "AG": "kanton-aargau",
    "ZH": "kanton-zuerich",
    "BL": "kanton-basel-landschaft",
    "BS": "kanton-basel-stadt",
    "SO": "kanton-solothurn",
    "BE": "kanton-bern",
    "LU": "kanton-luzern",
    "ZG": "kanton-zug",
    "SG": "kanton-st-gallen",
    "TG": "kanton-thurgau",
    "SH": "kanton-schaffhausen",
    "SZ": "kanton-schwyz",
}

# `characteristics` booleans -> our amenity vocabulary.
CHARACTERISTIC_MAP = {
    "hasBalcony": "BALCONY",
    "hasTerrace": "TERRACE",
    "hasGarden": "GARDEN",
    "hasElevator": "LIFT",
    "hasDishwasher": "DISHWASHER",
    "hasWashingMachine": "WASHING_MACHINE",
    "hasParking": "PARKING",
    "hasGarage": "GARAGE",
    "arePetsAllowed": "PETS_ALLOWED",
    "isWheelchairAccessible": "WHEELCHAIR",
    "hasFireplace": "FIREPLACE",
    "hasCellar": "CELLAR",
    "isQuiet": "QUIET",
    "isNewBuilding": "NEW_BUILD",
    "isMinergieCertified": "MINERGIE",
}


class ImmoScout24Source(Source):
    name = "immoscout"
    label = "ImmoScout24"
    needs_browser = True
    rate_limit = 3.0

    def base_url(self) -> str:
        return BASE

    def search_urls(self, criteria: Criteria) -> list[str]:
        """Every page we would visit. Also what `scout probe immoscout` dumps."""
        urls = []
        for canton in criteria.cantons:
            slug = CANTON_SLUGS.get(canton.upper())
            if not slug:
                continue
            urls.extend(
                _search_url(slug, criteria, page)
                for page in range(1, criteria.max_pages_per_region + 1)
            )
        return urls

    async def fetch(
        self,
        client: httpx.AsyncClient,
        criteria: Criteria,
        settings: Settings,
        state: dict[str, Any],
    ) -> tuple[list[Listing], dict[str, Any]]:
        if self.session is None:
            raise SourceError("immoscout requires a browser session")

        seen_before: set[str] = set(state.get("seen_ids") or [])
        listings: list[Listing] = []
        fresh_ids: set[str] = set()

        for canton in criteria.cantons:
            slug = CANTON_SLUGS.get(canton.upper())
            if not slug:
                log.warning("immoscout: no slug for canton %s, skipping", canton)
                continue

            known_streak = 0
            for page in range(1, criteria.max_pages_per_region + 1):
                url = _search_url(slug, criteria, page)
                try:
                    html = await self.session.load(url, wait_ms=6000)
                except Blocked as exc:
                    raise SourceError(f"immoscout blocked: {exc}") from exc

                payload = extract_state(html)
                result = dig(
                    payload or {}, "resultList", "search", "fullSearch", "result"
                )
                if not result:
                    log.warning("immoscout: no result state on %s", url)
                    break

                raw_listings = result.get("listings") or []
                for wrapper in raw_listings:
                    raw = wrapper.get("listing") or {}
                    listing_id = str(raw.get("id") or "")
                    if not listing_id:
                        continue
                    fresh_ids.add(listing_id)
                    if listing_id in seen_before:
                        known_streak += 1
                        continue
                    parsed = parse_listing(raw)
                    if parsed is not None:
                        listings.append(parsed)

                # Sorted newest-first, so a full page of already-known listings
                # means everything below is old too.
                if known_streak >= PAGE_SIZE or not result.get("hasNextPage"):
                    break

        # Cap the remembered set so the cursor row cannot grow without bound.
        remembered = sorted(fresh_ids | seen_before)[-5000:]
        log.info("immoscout: %d new listings", len(listings))
        return listings, {"seen_ids": remembered}


def _search_url(slug: str, criteria: Criteria, page: int) -> str:
    params = [
        f"pf={criteria.price_min}",
        f"pt={criteria.price_max}",
        f"nrf={criteria.rooms_min:g}",
        f"nrt={criteria.rooms_max:g}",
        f"slf={criteria.space_min_m2}",
        "o=dateCreated-desc",
        f"pn={page}",
    ]
    return f"{BASE}/de/immobilien/mieten/{slug}?" + "&".join(params)


def parse_listing(raw: dict[str, Any]) -> Listing | None:
    listing_id = str(raw.get("id") or "")
    if not listing_id:
        return None
    if str(raw.get("offerType") or "").upper() != "RENT":
        return None

    address = raw.get("address") or {}
    coords = address.get("geoCoordinates") or {}
    chars = raw.get("characteristics") or {}
    rent = ((raw.get("prices") or {}).get("rent")) or {}

    localization = raw.get("localization") or {}
    primary = localization.get("primary") or "de"
    local = localization.get(primary) or localization.get("de") or {}
    text = local.get("text") or {}

    title = clean_text(text.get("title"))
    description = clean_text(text.get("description"))

    categories = raw.get("categories") or []
    category = "OTHER"
    for candidate in categories:
        mapped = map_category(candidate)
        if mapped != "OTHER":
            category = mapped
            break

    amenities = {
        name for key, name in CHARACTERISTIC_MAP.items() if chars.get(key)
    }
    amenities.update(detect_amenities(description, title))

    gross = to_int(rent.get("gross"))
    net = to_int(rent.get("net"))
    extra = to_int(rent.get("extra"))
    if gross is None and net is not None:
        gross = net + (extra or 0)

    images = [
        Image(url=att["url"], caption=clean_text(att.get("alt")), ordering=i)
        for i, att in enumerate(local.get("attachments") or [])
        if isinstance(att, dict)
        and att.get("url")
        and str(att.get("type", "IMAGE")).upper() == "IMAGE"
    ]

    return Listing(
        source="immoscout",
        source_id=listing_id,
        url=f"{BASE}/de/d/{listing_id}",
        title=title,
        description=description,
        price_chf=gross,
        price_net_chf=net,
        charges_chf=extra,
        rooms=to_float(chars.get("numberOfRooms")),
        living_space_m2=to_int(chars.get("livingSpace")),
        floor=to_int(chars.get("floor")),
        street=clean_text(address.get("street")),
        zipcode=parse_zipcode(address.get("postalCode")),
        city=clean_text(address.get("locality")),
        lat=to_float(coords.get("latitude")),
        lon=to_float(coords.get("longitude")),
        category=category,
        year_built=to_int(chars.get("yearBuilt")),
        year_renovated=to_int(chars.get("yearLastRenovated")),
        amenities=sorted(amenities),
        images=images,
        published=parse_datetime((raw.get("meta") or {}).get("createdAt")),
        raw={"platforms": raw.get("platforms") or []},
    )
