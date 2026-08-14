"""Newhome adapter.

Status: **partly verified**. Newhome sits behind a Cloudflare challenge that
plain HTTP and headless Chromium both fail, but a headed Chromium clears it and
renders the site. What was not pinned down during development is the exact
search result URL - the paths tried redirected to a not-found page, and the
site is an Angular SPA that loads results by XHR after the consent banner is
dismissed.

So the URL patterns below are the best candidates, and parsing falls back to the
generic hydration-state heuristics in `generic.py`. If Newhome returns nothing,
run `scout probe newhome`: it opens the site in the same browser profile, dumps
the rendered HTML, the hydration state and every XHR the page made, which is
what you need to pin the URL down and, if the shape warrants it, replace this
with a precise parser.
"""

from __future__ import annotations

from ..models import Criteria
from .generic import GenericBrowserSource

BASE = "https://www.newhome.ch"

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


class NewhomeSource(GenericBrowserSource):
    name = "newhome"
    label = "Newhome"
    known_blocked_note = "Cloudflare challenge; needs headed Chromium"

    def base_url(self) -> str:
        return BASE

    def search_urls(self, criteria: Criteria) -> list[str]:
        urls = []
        for canton in criteria.cantons:
            slug = CANTON_SLUGS.get(canton.upper())
            if not slug:
                continue
            query = (
                f"?priceFrom={criteria.price_min}&priceTo={criteria.price_max}"
                f"&roomsFrom={criteria.rooms_min:g}&roomsTo={criteria.rooms_max:g}"
                f"&spaceFrom={criteria.space_min_m2}&sort=newest"
            )
            urls.append(f"{BASE}/de/mieten/suchen/wohnung/{slug}{query}")
        return urls
