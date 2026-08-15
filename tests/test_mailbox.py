"""Mailbox adapter tests. No IMAP server and no network - the parsing is the
part that can break, and it takes a raw message as input anyway."""

from __future__ import annotations

from pathlib import Path

import httpx
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


# -- wrappers seen in real Homegate mail -------------------------------------


def test_path_embedded_tracking_wrapper_is_unwrapped():
    """tracking.notification.homegate.ch/CL0/<urlencoded>/1/<token> style."""
    wrapped = (
        "https://tracking.notification.homegate.ch/CL0/"
        "https:%2F%2Fwww.homegate.ch%2Frent%2F4003387976%3Futm_source=crm/1/0107abc/tok="
    )
    assert clean_url(wrapped, HOMEGATE) == "https://www.homegate.ch/rent/4003387976"


def test_unresolved_tracking_subdomain_is_rejected_not_mistaken_for_a_listing():
    """The regression: tracking-path digits became a fake listing id."""
    wrapped = (
        "https://tracking.notification.homegate.ch/CL0/"
        "https:%2F%2Fwww.example.com%2F/1/010701a00657e908-89cf56c6/token="
    )
    url = clean_url(wrapped, HOMEGATE)
    assert url is None or listing_id(url) is None


def test_opaque_sendgrid_link_is_flagged_for_http_resolution():
    from scout.sources.mailbox import opaque_redirect

    assert opaque_redirect("https://u8489473.ct.sendgrid.net/ls/click?upn=u001.XYZ")
    # Never follow anything that could cancel the alert subscription.
    assert opaque_redirect("https://u8489473.ct.sendgrid.net/wf/unsubscribe?upn=x") is None
    assert opaque_redirect("https://www.homegate.ch/rent/123456") is None


def test_english_locale_price_with_comma_parses():
    assert _price("CHF 2,500.–") == 2500


def test_place_falls_back_to_the_title_when_the_body_runs_on():
    from scout.sources.mailbox import Portal, build_listing

    listing = build_listing(
        portal=Portal("homegate", ("homegate.ch",), "homegate.ch"),
        source_id="4003387945",
        url="https://www.homegate.ch/rent/4003387945",
        text="CHF 1,780.– 3.5 rooms Hauptstrasse 94 4450 Sissach CHF 1,780.–",
        image_url=None,
        published=None,
        title="Hauptstrasse 94 4450 Sissach",
    )
    assert (listing.zipcode, listing.city) == (4450, "Sissach")


def test_resolve_pending_follows_redirects_without_touching_the_portal_page():
    import asyncio

    from scout.sources.mailbox import pending_id, resolve_pending
    from scout.models import Listing

    wrapper = "https://u8489473.ct.sendgrid.net/ls/click?upn=u001.ABC"
    pending = Listing(
        source="homegate",
        source_id=pending_id(wrapper),
        url=wrapper,
        title="x",
        price_chf=2500,
        raw={"resolve_url": wrapper},
    )

    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(
            302, headers={"location": "https://www.homegate.ch/rent/4003388035?utm=x"}
        )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await resolve_pending(client, [pending])

    resolved = asyncio.run(go())
    assert len(resolved) == 1
    assert resolved[0].url == "https://www.homegate.ch/rent/4003388035"
    assert resolved[0].source_id == "4003388035"
    assert resolved[0].price_chf == 2500  # fields from the mail survive
    # The chain stopped at the Location header - the portal page (and its
    # anti-bot layer) was never requested.
    assert fetched == [wrapper]


def test_resolve_pending_drops_chains_that_never_reach_the_portal():
    import asyncio

    from scout.sources.mailbox import pending_id, resolve_pending
    from scout.models import Listing

    wrapper = "https://u8489473.ct.sendgrid.net/ls/click?upn=u001.LOGO"
    pending = Listing(
        source="homegate", source_id=pending_id(wrapper), url=wrapper,
        title="x", price_chf=1000, raw={"resolve_url": wrapper},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not a redirect")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await resolve_pending(client, [pending])

    assert asyncio.run(go()) == []


# -- IMAP cursor -------------------------------------------------------------


class FakeImap:
    """Just enough of imaplib for `_collect`'s fetch loop."""

    def __init__(self, uids: list[int], fail_uid: int | None = None) -> None:
        self.uids = uids
        self.fail_uid = fail_uid
        self.untagged_responses = {"UIDVALIDITY": [b"7"]}

    def login(self, *a):
        return "OK", []

    def select(self, *a, **k):
        return "OK", [b"3"]

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [" ".join(str(u) for u in self.uids).encode()]
        if command == "FETCH":
            uid = int(args[0])
            if uid == self.fail_uid:
                return "NO", [None]
            return "OK", [(b"1 (BODY[] {3})", b"raw"), b")"]
        raise AssertionError(command)

    def logout(self):
        return "BYE", []


def collect(fake, state):
    from unittest.mock import patch

    from scout.sources.mailbox import MailboxSource

    class Cfg:
        imap_host, imap_port, imap_ssl = "h", 993, True
        imap_user, imap_password, imap_folder = "u", "p", "INBOX"

    with patch("scout.sources.mailbox.imaplib.IMAP4_SSL", return_value=fake):
        return MailboxSource()._collect(Cfg(), state)


def test_cursor_advances_over_successfully_fetched_messages():
    bodies, state = collect(FakeImap([5, 6, 7]), {"uidvalidity": 7, "max_uid": 4})
    assert len(bodies) == 3
    assert state["max_uid"] == 7


def test_a_failed_fetch_does_not_advance_the_cursor_past_the_failure():
    """The cursor is a high-water mark: skipping a failed UID loses the message
    forever. It must stop at the failure and retry from there next run."""
    bodies, state = collect(FakeImap([5, 6, 7], fail_uid=6), {"uidvalidity": 7, "max_uid": 4})
    assert len(bodies) == 1  # uid 5 only
    assert state["max_uid"] == 5  # 6 and 7 retried next run


def test_uidvalidity_change_rescans_from_zero():
    bodies, state = collect(FakeImap([1, 2]), {"uidvalidity": 999, "max_uid": 50})
    assert len(bodies) == 2
    assert state["uidvalidity"] == 7
