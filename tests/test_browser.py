"""Hydration-state extraction and block detection.

Both fixtures are real captures: `immoscout_page.html` is the genuine ImmoScout24
markup (trimmed to three listings) including the DataDome script tag its normal
pages carry, and `immoscout_blocked.html` is an actual captcha interstitial.
Between them they pin the distinction the browser layer has to get right.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scout.browser import dig, extract_state, is_blocked
from scout.sources.generic import find_listing_array, parse_generic
from scout.sources.immoscout import parse_listing

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def page_html() -> str:
    return (FIXTURES / "immoscout_page.html").read_text()


@pytest.fixture(scope="module")
def blocked_html() -> str:
    return (FIXTURES / "immoscout_blocked.html").read_text()


# --------------------------------------------------------------------------
# block detection
# --------------------------------------------------------------------------


def test_real_captcha_page_is_detected(blocked_html):
    assert is_blocked(blocked_html, "immoscout24.ch")


def test_healthy_page_is_not_blocked_despite_datadome_script(page_html):
    """The regression this guards: a loaded page still embeds dd-tags.js."""
    assert "dd-tags.js" in page_html
    assert not is_blocked(page_html, "651 Immobilien zum Mieten: Kanton Aargau")


def test_cloudflare_style_challenge_is_detected():
    assert is_blocked("<html><body>small</body></html>", "Just a moment…")


def test_challenge_title_on_a_large_page_is_not_a_block():
    """A real listing whose title happens to say 'just a moment' is content."""
    assert not is_blocked("x" * 200_000, "Just a moment…")


# --------------------------------------------------------------------------
# state extraction
# --------------------------------------------------------------------------


def test_extract_state_reads_initial_state(page_html):
    state = extract_state(page_html)
    assert state is not None
    assert "resultList" in state


def test_extract_state_returns_none_on_a_blocked_page(blocked_html):
    assert extract_state(blocked_html) is None


def test_dig_walks_the_known_path(page_html):
    state = extract_state(page_html)
    result = dig(state, "resultList", "search", "fullSearch", "result")
    assert result and result["listings"]


def test_dig_returns_none_for_a_wrong_path(page_html):
    assert dig(extract_state(page_html), "nope", "missing") is None


def test_full_extraction_path_yields_usable_listings(page_html):
    """End-to-end through exactly the code the live adapter runs."""
    state = extract_state(page_html)
    result = dig(state, "resultList", "search", "fullSearch", "result")
    listings = [parse_listing(w["listing"]) for w in result["listings"]]
    listings = [x for x in listings if x]
    assert listings
    for listing in listings:
        assert listing.source == "immoscout"
        assert listing.zipcode and 1000 <= listing.zipcode <= 9999
        assert listing.price_chf and listing.price_chf > 0


# --------------------------------------------------------------------------
# generic fallback heuristics
# --------------------------------------------------------------------------


def test_generic_finder_locates_a_listing_array():
    payload = {
        "page": {"meta": {"x": 1}},
        "data": {
            "results": [
                {"id": 1, "grossPrice": 2200, "numberOfRooms": 4.5, "livingSpace": 95, "zip": 5200},
                {"id": 2, "grossPrice": 2400, "numberOfRooms": 3.5, "livingSpace": 88, "zip": 5000},
            ]
        },
    }
    found = find_listing_array(payload)
    assert len(found) == 2


def test_generic_finder_ignores_unrelated_arrays():
    payload = {"nav": [{"id": 1, "label": "Home"}, {"id": 2, "label": "Search"}]}
    assert find_listing_array(payload) == []


def test_generic_parser_maps_alias_field_names():
    listing = parse_generic(
        {
            "id": 77,
            "grossPrice": 2350,
            "numberOfRooms": "4.5",
            "livingSpace": 102,
            "zip": "5200",
            "city": "Brugg",
            "street": "Bahnhofstrasse 12",
            "detailUrl": "/de/flat/77",
            "description": "Mit Balkon und Lift.",
        },
        "newhome",
        "https://www.newhome.ch",
    )
    assert listing is not None
    assert listing.price_chf == 2350
    assert listing.rooms == 4.5
    assert listing.living_space_m2 == 102
    assert listing.zipcode == 5200
    assert listing.url == "https://www.newhome.ch/de/flat/77"
    assert "BALCONY" in listing.amenities


def test_generic_parser_rejects_an_object_with_no_id():
    assert parse_generic({"grossPrice": 2000}, "newhome", "https://x") is None
