"""Homegate adapter.

Status: **blocked**. Homegate is the one portal that refused every approach
tried while building this - plain HTTP, headless Chromium and headed Chromium
all get a DataDome captcha (HTTP 403), even on the bare homepage.

It is kept enabled anyway because the block is IP-reputation sensitive: a
residential Swiss connection (which is where this actually runs) is a very
different proposition to the address it was developed from, so it may simply
work for you. If it does not, the run records "blocked" for this source and
carries on.

The practical loss is small. Homegate and ImmoScout24 are both SMG properties
and syndicate the same inventory - ImmoScout24 listings carry
`platforms: ["homegate", "immoscout24", ...]` - so the ImmoScout24 adapter
already covers most of what Homegate would return, and `dedup.py` would merge
them anyway.
"""

from __future__ import annotations

from ..models import Criteria
from .generic import GenericBrowserSource

BASE = "https://www.homegate.ch"

CANTON_SLUGS = {
    "AG": "kanton-aargau",
    "ZH": "kanton-zuerich",
    "BL": "kanton-basel-landschaft",
    "BS": "kanton-basel-stadt",
    "SO": "kanton-solothurn",
    "BE": "kanton-bern",
    "LU": "kanton-luzern",
    "ZG": "kanton-zug",
}


class HomegateSource(GenericBrowserSource):
    name = "homegate"
    label = "Homegate"
    known_blocked_note = (
        "Homegate served DataDome captchas to every client tried during "
        "development; ImmoScout24 carries largely the same inventory"
    )
    # Homegate runs the same front-end stack as ImmoScout24, so if it ever
    # loads, the result set should be in the same place.
    known_paths = (
        ("resultList", "search", "fullSearch", "result", "listings"),
        ("props", "pageProps", "listings"),
    )

    def base_url(self) -> str:
        return BASE

    def search_urls(self, criteria: Criteria) -> list[str]:
        urls = []
        for canton in criteria.cantons:
            slug = CANTON_SLUGS.get(canton.upper())
            if not slug:
                continue
            for page in range(1, criteria.max_pages_per_region + 1):
                urls.append(
                    f"{BASE}/mieten/immobilien/{slug}/trefferliste"
                    f"?ep={criteria.price_min}&et={criteria.price_max}"
                    f"&ac={criteria.rooms_min:g}&ad={criteria.rooms_max:g}"
                    f"&ah={criteria.space_min_m2}&be={page}&sortBy=dateCreated&sortDir=desc"
                )
        return urls
