"""Mailbox adapter tests. No IMAP server and no network - the parsing is the
part that can break, and it takes a raw message as input anyway."""

from __future__ import annotations

from pathlib import Path

import pytest

from scout.sources.mailbox import (
    PORTALS,
    Portal,
    _category,
    _place,
    _price,
    _rooms,
    _space,
    clean_url,
    listing_id,
    match_portal,
    parse_alert_email,
)

FIXTURES = Path(__file__).parent / "fixtures"
HOMEGATE = Portal("homegate", ("homegate.ch",), "homegate.ch")


@pytest.fixture(scope="module")
def alert_listings():
    raw = (FIXTURES / "homegate_alert.eml").read_bytes()
    return parse_alert_email(raw)


def test_every_listing_in_the_mail_is_found_exactly_once(alert_listings):
    ids = [listing.source_id for listing in alert_listings]
    assert ids == ["4001923847", "4001923901", "4002001122"]


def test_fields_come_from_the_block_around_each_link(alert_listings):
    first = alert_listings[0]
    assert first.price_chf == 2350
    assert first.rooms == 3.5
    assert first.living_space_m2 == 88
    assert first.zipcode == 5000
    assert first.city == "Aarau"
    assert first.title == "Helle 3.5 Zimmer Wohnung mit Balkon"
    assert first.category == "APARTMENT"


def test_prices_do_not_bleed_between_listings(alert_listings):
    # The failure mode of walking too far up the DOM: every card gets the
    # first card's price.
    assert [listing.price_chf for listing in alert_listings] == [2350, 2790, 1450]


def test_listings_are_attributed_to_the_portal_not_to_the_adapter(alert_listings):
    assert {listing.source for listing in alert_listings} == {"homegate"}


def test_thumbnail_is_taken_but_tracking_pixels_are_not(alert_listings):
    with_photo, _, without_photo = alert_listings
    assert with_photo.images[0].url.endswith("1_large.jpg")
    assert without_photo.images == []
    assert not any("pixel" in image.url for l in alert_listings for image in l.images)


def test_send_date_is_used_as_publication_time(alert_listings):
    assert alert_listings[0].published is not None
    assert alert_listings[0].published.year == 2026


def test_floor_is_read_when_stated(alert_listings):
    assert alert_listings[1].floor == 2
    assert alert_listings[0].floor is None


# -- link handling ----------------------------------------------------------


def test_click_tracker_is_unwrapped_to_the_real_listing_url():
    wrapped = (
        "https://click.homegate.ch/r?url=https%3A%2F%2Fwww.homegate.ch"
        "%2Fmieten%2F4001923847&uid=abc123"
    )
    assert clean_url(wrapped, HOMEGATE) == "https://www.homegate.ch/mieten/4001923847"


def test_tracking_query_is_stripped_so_one_flat_is_one_url():
    plain = "https://www.homegate.ch/mieten/4001923847?utm_source=alert&s=9"
    assert clean_url(plain, HOMEGATE) == "https://www.homegate.ch/mieten/4001923847"


@pytest.mark.parametrize(
    "href",
    [
        "https://www.homegate.ch/account/unsubscribe?token=xyz",
        "https://www.homegate.ch/impressum",
        "https://example.com/mieten/4001923847",
        "mailto:support@homegate.ch",
        "#top",
        "",
    ],
)
def test_non_listing_links_are_rejected(href):
    url = clean_url(href, HOMEGATE)
    assert url is None or listing_id(url) is None


def test_listing_id_is_the_id_in_the_path():
    assert listing_id("https://www.homegate.ch/mieten/4001923847") == "4001923847"
    assert (
        listing_id("https://www.immoscout24.ch/de/d/wohnung-mieten/zuerich/8046231")
        == "8046231"
    )
    assert listing_id("https://www.homegate.ch/mieten") is None


def test_sender_decides_which_portal_a_mail_belongs_to():
    assert match_portal("Homegate <noreply@homegate.ch>").source == "homegate"
    assert match_portal("ImmoScout24 <no-reply@immoscout24.ch>").source == "immoscout"
    assert match_portal("Some Newsletter <hi@example.com>") is None


def test_mail_from_an_unknown_sender_yields_nothing():
    raw = b"From: spam@example.com\nSubject: hi\n\n<a href='https://www.homegate.ch/mieten/4001923847'>x</a>"
    assert parse_alert_email(raw) == []


def test_portal_names_match_the_source_registry():
    from scout.sources.registry import SOURCES

    for portal in PORTALS:
        assert portal.source in SOURCES


# -- field extraction edge cases --------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("CHF 2'350.–", 2350),
        ("CHF 2’350", 2350),
        ("Fr. 1450.-", 1450),
        ("2'790 CHF", 2790),
        ("CHF 12'500", 12500),
        ("keine Preisangabe", None),
        # Out of band: a surface, a year or a phone fragment that happened to
        # land next to a currency symbol.
        ("CHF 88", None),
        ("CHF 1'250'000", None),
    ],
)
def test_price_parsing(text, expected):
    assert _price(text) == expected


def test_price_is_the_rent_not_the_postcode_on_the_address_line():
    # Both numbers sit in the same block; only one follows a currency symbol.
    assert _price("CHF 2'350.– · 3.5 Zimmer · 88 m² Bahnhofstrasse 12, 5000 Aarau") == 2350


@pytest.mark.parametrize(
    "text,expected",
    [("3.5 Zimmer", 3.5), ("4,5 Zimmer", 4.5), ("2 Zi.", 2.0), ("3 rooms", 3.0), ("x", None)],
)
def test_rooms_parsing(text, expected):
    assert _rooms(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [("88 m²", 88), ("112m2", 112), ("95 qm", 95), ("5 m²", None), ("x", None)],
)
def test_space_parsing(text, expected):
    assert _space(text) == expected


def test_place_parsing_takes_the_swiss_postcode_and_town():
    assert _place("Bahnhofstrasse 12, 5000 Aarau") == (5000, "Aarau")
    assert _place("4310 Rheinfelden, AG") == (4310, "Rheinfelden")
    assert _place("kein Ort") == (None, "")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("3.5 Zimmer Wohnung in einem Mehrfamilienhaus", "APARTMENT"),
        ("Einfamilienhaus mit Garten", "HOUSE"),
        ("Reihenhaus 5.5 Zimmer", "HOUSE"),
        ("WG-Zimmer in Basel", "SHARED"),
        ("Attikawohnung", "APARTMENT"),
        ("Objekt 12", "APARTMENT"),
    ],
)
def test_category_is_inferred_from_prose(text, expected):
    assert _category(text) == expected
