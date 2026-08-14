"""Adapter tests against recorded real payloads - no network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scout.normalize import (
    clean_text,
    detect_amenities,
    dedup_key,
    normalize_street,
    parse_date,
    parse_zipcode,
    to_float,
    to_int,
)
from scout.sources.flatfox import parse_listing as parse_flatfox
from scout.sources.immoscout import parse_listing as parse_immoscout

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def flatfox_records():
    return json.loads((FIXTURES / "flatfox_page.json").read_text())["results"]


@pytest.fixture(scope="module")
def immoscout_records():
    state = json.loads((FIXTURES / "immoscout_state.json").read_text())
    result = state["resultList"]["search"]["fullSearch"]["result"]
    return [wrapper["listing"] for wrapper in result["listings"]]


# --------------------------------------------------------------------------
# scalar parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("3.5", 3.5),
        ("3,5", 3.5),
        ("CHF 2'350.–", 2350.0),
        ("2’350", 2350.0),
        (1870, 1870.0),
        (None, None),
        ("", None),
        ("nach Vereinbarung", None),
    ],
)
def test_to_float_handles_swiss_formatting(raw, expected):
    assert to_float(raw) == expected


def test_to_int_rounds():
    assert to_int("80.4") == 80
    assert to_int("80.6") == 81


@pytest.mark.parametrize(
    "raw,expected", [(5600, 5600), ("8005", 8005), (99, None), ("", None), (12345, None)]
)
def test_parse_zipcode_rejects_non_swiss(raw, expected):
    assert parse_zipcode(raw) == expected


def test_parse_date_recognises_swiss_phrasing():
    assert parse_date("2026-09-01") == (__import__("datetime").date(2026, 9, 1), False)
    assert parse_date("per sofort")[1] is True
    assert parse_date("nach Vereinbarung")[1] is True
    assert parse_date(None) == (None, False)


def test_clean_text_strips_html():
    assert clean_text("<p>Hell &amp; <b>freundlich</b></p>") == "Hell & freundlich"


# --------------------------------------------------------------------------
# amenities
# --------------------------------------------------------------------------


def test_detect_amenities_from_prose():
    found = detect_amenities("Schöne Wohnung mit Balkon, Lift und Geschirrspüler.")
    assert {"BALCONY", "LIFT", "DISHWASHER"} <= set(found)


def test_detect_amenities_respects_negation():
    assert "PETS_ALLOWED" not in detect_amenities("Keine Haustiere erlaubt.")


def test_detect_amenities_is_stable_order():
    a = detect_amenities("Lift, Balkon")
    b = detect_amenities("Balkon, Lift")
    assert a == b


# --------------------------------------------------------------------------
# flatfox
# --------------------------------------------------------------------------


def test_flatfox_parses_real_records(flatfox_records):
    parsed = [p for p in (parse_flatfox(r) for r in flatfox_records) if p]
    assert parsed, "fixture should yield listings"
    for listing in parsed:
        assert listing.source == "flatfox"
        assert listing.url.startswith("https://flatfox.ch/")
        assert listing.source_id.isdigit()


def test_flatfox_maps_structured_attributes(flatfox_records):
    for raw in flatfox_records:
        names = {a["name"] for a in (raw.get("attributes") or []) if isinstance(a, dict)}
        if "balconygarden" not in names:
            continue
        listing = parse_flatfox(raw)
        assert listing is not None and "BALCONY" in listing.amenities
        return
    pytest.skip("no fixture record carries the balcony flag")


def test_flatfox_computes_gross_rent_from_parts():
    listing = parse_flatfox(
        {
            "pk": 1,
            "url": "/en/flat/x/1/",
            "offer_type": "RENT",
            "status": "act",
            "object_category": "APARTMENT",
            "rent_net": 2000,
            "rent_charges": 250,
            "number_of_rooms": "3.5",
        }
    )
    assert listing is not None
    assert listing.price_chf == 2250
    assert listing.rooms == 3.5


def test_flatfox_skips_sales_and_inactive():
    base = {"pk": 1, "url": "/x/", "object_category": "APARTMENT", "status": "act"}
    assert parse_flatfox({**base, "offer_type": "BUY"}) is None
    assert parse_flatfox({**base, "offer_type": "RENT", "status": "expired"}) is None


def test_flatfox_ignores_unexpanded_image_ids():
    listing = parse_flatfox(
        {
            "pk": 2,
            "url": "/x/",
            "offer_type": "RENT",
            "status": "act",
            "object_category": "APARTMENT",
            "images": [123, 456],  # what you get without expand=images
        }
    )
    assert listing is not None and listing.images == []


# --------------------------------------------------------------------------
# immoscout24
# --------------------------------------------------------------------------


def test_immoscout_parses_real_records(immoscout_records):
    parsed = [p for p in (parse_immoscout(r) for r in immoscout_records) if p]
    assert parsed
    for listing in parsed:
        assert listing.source == "immoscout"
        assert listing.price_chf is None or listing.price_chf > 0
        assert listing.url.startswith("https://www.immoscout24.ch/")


def test_immoscout_extracts_the_fields_that_drive_scoring(immoscout_records):
    parsed = [p for p in (parse_immoscout(r) for r in immoscout_records) if p]
    assert any(p.rooms for p in parsed)
    assert any(p.living_space_m2 for p in parsed)
    assert any(p.zipcode for p in parsed)
    assert any(p.lat and p.lon for p in parsed)
    assert any(p.published for p in parsed)
    assert any(p.images for p in parsed)


def test_immoscout_maps_characteristics_to_amenities():
    listing = parse_immoscout(
        {
            "id": "123",
            "offerType": "RENT",
            "categories": ["APARTMENT"],
            "characteristics": {
                "hasBalcony": True,
                "hasElevator": True,
                "numberOfRooms": 4.5,
                "livingSpace": 110,
            },
            "prices": {"rent": {"gross": 2400, "net": 2200, "extra": 200}},
            "address": {"postalCode": "5000", "locality": "Aarau"},
            "localization": {"primary": "de", "de": {"text": {"title": "T"}}},
        }
    )
    assert listing is not None
    assert {"BALCONY", "LIFT"} <= set(listing.amenities)
    assert listing.price_chf == 2400
    assert listing.living_space_m2 == 110


# --------------------------------------------------------------------------
# dedup keys
# --------------------------------------------------------------------------


def test_normalize_street_collapses_abbreviations():
    assert normalize_street("Bahnhofstrasse 12a") == normalize_street("Bahnhofstr. 12 A")


def test_dedup_key_ignores_fields_portals_disagree_on(flatfox_records):
    """A rent that differs by CHF 20 must not split one flat into two."""
    parsed = [p for p in (parse_flatfox(r) for r in flatfox_records) if p]
    listing = parsed[0]
    nudged = listing.model_copy(
        update={"price_chf": (listing.price_chf or 2000) + 20, "living_space_m2": None}
    )
    assert dedup_key(listing) == dedup_key(nudged)
