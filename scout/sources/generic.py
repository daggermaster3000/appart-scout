"""Best-effort browser source for portals whose payload shape is unverified.

Flatfox and ImmoScout24 were reverse-engineered against live responses, so their
adapters parse known fields. Homegate, Newhome and Comparis could not be fully
verified while building this - Homegate stayed hard-blocked, and Newhome and
Comparis rendered but their result URLs were not pinned down (see the table in
`scout.browser`).

Rather than ship parsers written against imagined payloads, those three use this
generic adapter, which:

1. loads the configured search URL in the browser,
2. pulls the page's hydration JSON (`__NEXT_DATA__` / `__INITIAL_STATE__` / `__NUXT__`),
3. walks it looking for an array of listing-shaped objects,
4. maps whatever recognizable field names it finds onto `Listing`.

If it cannot find listings it raises a `SourceError` naming the portal and
telling you to run `scout probe <source>`, which dumps the page and its state to
disk so the adapter can be finished in one sitting against real data.

This is deliberately honest about being a heuristic: it may work, it reports
clearly when it doesn't, and it never fabricates a listing.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..browser import Blocked, extract_state
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
from .base import Source, SourceError

log = logging.getLogger(__name__)

# Field-name synonyms seen across Swiss portals, most specific first.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "listingId", "objectId", "pk", "adId"),
    "url": ("url", "detailUrl", "link", "seoUrl", "canonicalUrl"),
    "title": ("title", "headline", "name", "shortTitle", "publicTitle"),
    "description": ("description", "text", "descriptionText", "teaser"),
    "price": ("grossPrice", "priceGross", "rentGross", "price", "rent", "grossRent"),
    "price_net": ("netPrice", "rentNet", "priceNet", "netRent"),
    "charges": ("extraCosts", "additionalCosts", "rentCharges", "charges"),
    "rooms": ("numberOfRooms", "rooms", "roomCount", "numRooms"),
    "space": ("livingSpace", "surfaceLiving", "space", "area", "livingArea", "squareMeters"),
    "floor": ("floor", "storey", "level"),
    "street": ("street", "streetName", "addressLine", "address1"),
    "zipcode": ("zip", "zipCode", "postalCode", "plz", "zipcode"),
    "city": ("city", "locality", "town", "place", "municipality"),
    "lat": ("latitude", "lat"),
    "lon": ("longitude", "lng", "lon"),
    "published": ("createdAt", "publishedAt", "published", "created", "insertDate"),
    "available": ("availableFrom", "moveInDate", "movingDate", "availableAt"),
    "images": ("images", "attachments", "pictures", "photos", "media"),
}

#: An object needs this many recognizable listing fields to count as a listing.
MIN_SIGNALS = 3
_SIGNAL_KEYS = ("price", "rooms", "space", "zipcode", "street")


class GenericBrowserSource(Source):
    """Subclasses supply `search_urls()`; everything else is inherited."""

    needs_browser = True
    rate_limit = 3.0

    #: dotted paths to try before falling back to the generic tree walk
    known_paths: tuple[tuple[str, ...], ...] = ()
    #: set on adapters known to be blocked, so the error explains itself
    known_blocked_note: str = ""

    def search_urls(self, criteria: Criteria) -> list[str]:
        raise NotImplementedError

    async def fetch(
        self,
        client: httpx.AsyncClient,
        criteria: Criteria,
        settings: Settings,
        state: dict[str, Any],
    ) -> tuple[list[Listing], dict[str, Any]]:
        if self.session is None:
            raise SourceError(f"{self.name} requires a browser session")

        seen_before: set[str] = set(state.get("seen_ids") or [])
        listings: list[Listing] = []
        fresh_ids: set[str] = set()
        recognized_any = False

        for url in self.search_urls(criteria):
            try:
                html = await self.session.load(url, wait_ms=9000)
            except Blocked as exc:
                note = f" ({self.known_blocked_note})" if self.known_blocked_note else ""
                raise SourceError(f"{self.name} blocked{note}: {exc}") from exc

            payload = extract_state(html)
            if payload is None:
                log.warning("%s: no hydration state on %s", self.name, url)
                continue

            raw_listings = self._locate(payload)
            if not raw_listings:
                continue
            recognized_any = True

            for raw in raw_listings:
                listing = self.parse(raw)
                if listing is None:
                    continue
                fresh_ids.add(listing.source_id)
                if listing.source_id not in seen_before:
                    listings.append(listing)

        if not recognized_any:
            raise SourceError(
                f"{self.name}: could not locate listings in the page state. "
                f"Run `scout probe {self.name}` to dump the page and finish the adapter."
            )

        remembered = sorted(fresh_ids | seen_before)[-5000:]
        return listings, {"seen_ids": remembered}

    # -- location ----------------------------------------------------------

    def _locate(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        for path in self.known_paths:
            node: Any = payload
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
                if node is None:
                    break
            if isinstance(node, list) and node:
                return [x for x in node if isinstance(x, dict)]
        return find_listing_array(payload)

    def parse(self, raw: dict[str, Any]) -> Listing | None:
        return parse_generic(raw, self.name, self.base_url())

    def base_url(self) -> str:
        return ""


# --------------------------------------------------------------------------
# heuristics
# --------------------------------------------------------------------------


def _pick(obj: dict[str, Any], field: str) -> Any:
    for alias in FIELD_ALIASES[field]:
        if alias in obj and obj[alias] not in (None, "", []):
            return obj[alias]
    return None


def _flatten(obj: dict[str, Any], depth: int = 2) -> dict[str, Any]:
    """Merge one or two levels of nesting so aliases match wherever they sit."""
    flat: dict[str, Any] = {}
    for key, value in obj.items():
        if isinstance(value, dict) and depth > 0:
            for inner_key, inner_value in _flatten(value, depth - 1).items():
                flat.setdefault(inner_key, inner_value)
        else:
            flat.setdefault(key, value)
    for key, value in obj.items():
        flat[key] = value if not isinstance(value, dict) else flat.get(key, value)
    return flat


def looks_like_listing(obj: dict[str, Any]) -> bool:
    flat = _flatten(obj)
    signals = sum(1 for field in _SIGNAL_KEYS if _pick(flat, field) is not None)
    return signals >= MIN_SIGNALS and _pick(flat, "id") is not None


def find_listing_array(node: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Depth-first search for the largest array of listing-shaped objects."""
    if depth > 8:
        return []
    best: list[dict[str, Any]] = []
    if isinstance(node, list):
        dicts = [x for x in node if isinstance(x, dict)]
        if len(dicts) >= 2 and sum(looks_like_listing(d) for d in dicts[:5]) >= 2:
            return dicts
        for item in node[:20]:
            found = find_listing_array(item, depth + 1)
            if len(found) > len(best):
                best = found
    elif isinstance(node, dict):
        for value in node.values():
            found = find_listing_array(value, depth + 1)
            if len(found) > len(best):
                best = found
    return best


def parse_generic(raw: dict[str, Any], source: str, base_url: str) -> Listing | None:
    flat = _flatten(raw)
    listing_id = _pick(flat, "id")
    if listing_id is None:
        return None
    listing_id = str(listing_id)

    url = _pick(flat, "url") or ""
    url = str(url)
    if url and not url.startswith("http"):
        url = f"{base_url.rstrip('/')}/{url.lstrip('/')}"
    if not url:
        url = base_url

    description = clean_text(_pick(flat, "description"))
    title = clean_text(_pick(flat, "title"))

    gross = to_int(_pick(flat, "price"))
    net = to_int(_pick(flat, "price_net"))
    charges = to_int(_pick(flat, "charges"))
    if gross is None and net is not None:
        gross = net + (charges or 0)

    available_from, immediate = parse_date(_pick(flat, "available"))

    return Listing(
        source=source,
        source_id=listing_id,
        url=url,
        title=title,
        description=description,
        price_chf=gross,
        price_net_chf=net,
        charges_chf=charges,
        rooms=to_float(_pick(flat, "rooms")),
        living_space_m2=to_int(_pick(flat, "space")),
        floor=to_int(_pick(flat, "floor")),
        street=clean_text(_pick(flat, "street")),
        zipcode=parse_zipcode(_pick(flat, "zipcode")),
        city=clean_text(_pick(flat, "city")),
        lat=to_float(_pick(flat, "lat")),
        lon=to_float(_pick(flat, "lon")),
        available_from=available_from,
        available_immediately=immediate,
        category=map_category(raw.get("category") or raw.get("objectCategory") or "APARTMENT"),
        amenities=detect_amenities(description, title),
        images=_generic_images(_pick(flat, "images"), base_url),
        published=parse_datetime(_pick(flat, "published")),
    )


def _generic_images(value: Any, base_url: str) -> list[Image]:
    if not isinstance(value, list):
        return []
    out: list[Image] = []
    for i, item in enumerate(value[:12]):
        url = None
        if isinstance(item, str):
            url = item
        elif isinstance(item, dict):
            for key in ("url", "src", "large", "original", "href"):
                if isinstance(item.get(key), str):
                    url = item[key]
                    break
        if not url:
            continue
        if not url.startswith("http"):
            url = f"{base_url.rstrip('/')}/{url.lstrip('/')}"
        out.append(Image(url=url, ordering=i))
    return out
