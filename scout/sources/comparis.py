"""Comparis adapter.

Status: **partly verified**. Comparis is behind DataDome, which plain HTTP and
headless Chromium fail; a headed Chromium gets through and renders real pages
(the marketplace front page comes back as a normal 690 KB Next.js document with
`__NEXT_DATA__`). What was not pinned down is the result URL - the search takes
an internal location id rather than a canton name, so guessed URLs land on a
404 shell.

Parsing therefore uses the generic hydration-state heuristics. Run
`scout probe comparis` to dump a rendered search page and its state once you
have a working result URL (easiest way to get one: run a search on comparis.ch
in a normal browser and copy the address bar).

Comparis is an aggregator of the other portals, so it mostly adds duplicate
coverage - `dedup.py` merges those - and is the least costly of the five to be
missing.
"""

from __future__ import annotations

from ..models import Criteria
from .generic import GenericBrowserSource

BASE = "https://www.comparis.ch"

#: Comparis' own deal-type / property-type codes, from its result URLs.
DEAL_TYPE_RENT = 10
PROPERTY_TYPE_FLAT = 1


class ComparisSource(GenericBrowserSource):
    name = "comparis"
    label = "Comparis"
    known_blocked_note = "DataDome; needs headed Chromium"
    known_paths = (
        ("props", "pageProps", "searchResult", "properties"),
        ("props", "pageProps", "listings"),
    )

    def base_url(self) -> str:
        return BASE

    def search_urls(self, criteria: Criteria) -> list[str]:
        urls = []
        for canton in criteria.cantons:
            urls.append(
                f"{BASE}/immobilien/marktplatz/ergebnisse"
                f"?dealtype={DEAL_TYPE_RENT}&propertytypes={PROPERTY_TYPE_FLAT}"
                f"&location={canton}"
                f"&minprice={criteria.price_min}&maxprice={criteria.price_max}"
                f"&minrooms={criteria.rooms_min:g}&maxrooms={criteria.rooms_max:g}"
                f"&minspace={criteria.space_min_m2}&sort=newest"
            )
        return urls
