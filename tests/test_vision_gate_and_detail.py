"""Which listings get photographed, what the digest says, and the detail page.

The gate is the expensive decision here: every candidate that slips through
costs an OpenAI call, and before this it was possible to spend the whole budget
on listings whose commute was not resolved and whose score was therefore
provisional.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scout import store
from scout.config import Config, get_config
from scout.db import connect, init_db, load_criteria, save_criteria
from scout.dedup import MergedListing
from scout.models import Commute, Image, Listing, ScoreBreakdown, VisionResult
from scout.notify.email import render_digest, render_text


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_DB_PATH", str(tmp_path / "scout.db"))
    monkeypatch.setattr(Config, "model_config", {"env_file": None, "extra": "ignore"})
    get_config.cache_clear()
    init_db()
    with connect() as conn:
        criteria = load_criteria(conn)
        save_criteria(
            conn, criteria.model_copy(update={"label_a": "Ada", "label_b": "Bo"})
        )
    yield
    get_config.cache_clear()


#: `dedup_key` buckets on (zipcode, rooms), so fixtures must differ in one of
#: them or they merge into a single listing and overwrite each other's score.
_SLOTS: dict[str, int] = {}


def add(
    conn,
    key: str,
    *,
    score: float,
    images: int = 3,
    commutes: bool = True,
    vision: bool = False,
    lat: float | None = 47.39,
) -> str:
    zipcode = 5000 + _SLOTS.setdefault(key, len(_SLOTS))
    listing = Listing(
        source="flatfox",
        source_id=key,
        url=f"https://flatfox.ch/de/flat/{key}/",
        title=f"Flat {key}",
        price_chf=2200,
        rooms=3.5,
        living_space_m2=90,
        zipcode=zipcode,
        city="Aarau",
        lat=lat,
        lon=8.05 if lat is not None else None,
        images=[Image(url=f"https://img.example/{key}/{i}.jpg") for i in range(images)],
    )
    listing_id, _ = store.upsert_listing(conn, MergedListing([listing]))
    store.save_score(conn, listing_id, ScoreBreakdown(total=score), 1)
    if commutes:
        store.save_commutes(
            conn,
            listing_id,
            {
                "a": Commute(minutes=34, transfers=1, origin_station="Aarau"),
                "b": Commute(minutes=41, transfers=0, origin_station="Aarau"),
            },
        )
    if vision:
        store.save_vision(
            conn, listing_id, "gpt-4.1-mini", VisionResult(score=80, verdict="Bright"), 3
        )
    return listing_id


# -- the gate ---------------------------------------------------------------


def test_only_listings_above_the_score_floor_are_photographed(db):
    with connect() as conn:
        good = add(conn, "high", score=85)
        add(conn, "low", score=40)
        picked = store.vision_candidates(conn, limit=10, min_score=70)
    assert [p["id"] for p in picked] == [good]


def test_a_listing_without_resolved_commutes_is_never_photographed(db):
    """The regression this gate exists for.

    Its score is price and size only, so cheap roomy places an hour outside the
    corridor sit at the very top — exactly where the old selection looked.
    """
    with connect() as conn:
        add(conn, "no-commute", score=95, commutes=False)
        assert store.vision_candidates(conn, limit=10, min_score=70) == []


def test_listings_without_photos_are_skipped(db):
    with connect() as conn:
        add(conn, "no-photos", score=95, images=0)
        assert store.vision_candidates(conn, limit=10, min_score=70) == []


def test_a_listing_is_photographed_once_not_once_per_run(db):
    with connect() as conn:
        add(conn, "seen", score=95, vision=True)
        assert store.vision_candidates(conn, limit=10, min_score=70) == []


def test_candidates_come_back_best_first_and_respect_the_limit(db):
    with connect() as conn:
        add(conn, "a", score=75)
        add(conn, "b", score=95)
        add(conn, "c", score=85)
        picked = store.vision_candidates(conn, limit=2, min_score=70)
    assert [round(p["score"]) for p in picked] == [95, 85]


# -- the digest -------------------------------------------------------------


@pytest.fixture
def digest_items(db):
    with connect() as conn:
        listing_id = add(conn, "one", score=88, vision=True)
        conn.execute(
            "UPDATE vision SET result = ? WHERE listing_id = ?",
            (
                VisionResult(
                    score=82,
                    verdict="Bright rooms, modern kitchen.",
                    condition="renovated",
                    brightness="bright",
                    kitchen="white cabinetry with island",
                    bathroom="not shown",
                    renovation_era="2020s",
                    red_flags=["street-facing windows"],
                ).model_dump_json(),
                listing_id,
            ),
        )
        conn.commit()
        return store.ranked(conn, limit=5), load_criteria(conn)


def test_plain_text_digest_names_the_partners(digest_items):
    items, criteria = digest_items
    text = render_text(items, criteria)
    assert "commute: Ada 34' / Bo 41'" in text
    # The old format was an unlabelled pair whose order you had to remember.
    assert "commute: 34' / 41'" not in text


def test_plain_text_digest_carries_the_photo_evaluation(digest_items):
    items, criteria = digest_items
    text = render_text(items, criteria)
    assert "photos: Bright rooms, modern kitchen." in text
    assert "condition: renovated" in text
    assert "! street-facing windows" in text
    # "not shown" is the model saying it could not tell; printing it is noise.
    assert "bathroom: not shown" not in text


def test_html_digest_carries_the_photo_evaluation_and_the_names(digest_items):
    items, criteria = digest_items
    _subject, html = render_digest(items, criteria)
    assert "What the photos show" in html
    assert "Bright rooms, modern kitchen." in html
    assert "Condition renovated" in html
    assert "street-facing windows" in html
    assert "Ada" in html and "Bo" in html
    assert "Bathroom not shown" not in html


# -- the detail page --------------------------------------------------------


@pytest.fixture
def client(db):
    from scout.web.app import app

    return TestClient(app)


def test_the_dashboard_links_each_card_to_its_detail_page(client):
    with connect() as conn:
        listing_id = add(conn, "one", score=88)
    assert f'href="/listing/{listing_id}"' in client.get("/").text


def test_the_detail_page_shows_the_listing(client):
    with connect() as conn:
        listing_id = add(conn, "one", score=88, images=4, vision=True)
    page = client.get(f"/listing/{listing_id}").text
    assert "Flat one" in page
    assert page.count("https://img.example/one/") >= 4  # every photo, not just one
    assert "Ada" in page and "Bo" in page
    assert "34′" in page and "41′" in page


def test_the_detail_page_maps_a_listing_that_has_coordinates(client):
    with connect() as conn:
        listing_id = add(conn, "one", score=88, lat=47.39)
    page = client.get(f"/listing/{listing_id}").text
    assert "openstreetmap.org/export/embed.html" in page
    assert "marker=47.39" in page


def test_a_listing_without_coordinates_falls_back_to_an_address_search(client):
    """Mailbox listings carry a town but no coordinates."""
    with connect() as conn:
        listing_id = add(conn, "one", score=88, lat=None)
    page = client.get(f"/listing/{listing_id}").text
    assert "export/embed.html" not in page
    assert "openstreetmap.org/search" in page


def test_an_unevaluated_listing_offers_on_demand_evaluation(client):
    with connect() as conn:
        listing_id = add(conn, "one", score=88, vision=False)
    page = client.get(f"/listing/{listing_id}").text
    assert f'action="/listing/{listing_id}/vision"' in page


def test_an_already_evaluated_listing_shows_the_result_instead(client):
    with connect() as conn:
        listing_id = add(conn, "one", score=88, vision=True)
    page = client.get(f"/listing/{listing_id}").text
    assert "Bright" in page
    assert f'action="/listing/{listing_id}/vision"' not in page


def test_unknown_listing_is_a_404(client):
    assert client.get("/listing/nope").status_code == 404


def test_feedback_returns_to_the_detail_page_when_it_came_from_there(client):
    with connect() as conn:
        listing_id = add(conn, "one", score=88)
    r = client.post(
        f"/listing/{listing_id}/feedback",
        data={"verdict": "shortlist", "back": f"/listing/{listing_id}"},
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/listing/{listing_id}"


def test_feedback_still_returns_to_the_dashboard_by_default(client):
    with connect() as conn:
        listing_id = add(conn, "one", score=88)
    r = client.post(
        f"/listing/{listing_id}/feedback",
        data={"verdict": "up"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"


def test_feedback_will_not_redirect_off_site(client):
    with connect() as conn:
        listing_id = add(conn, "one", score=88)
    r = client.post(
        f"/listing/{listing_id}/feedback",
        data={"verdict": "up", "back": "https://evil.example/"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"
