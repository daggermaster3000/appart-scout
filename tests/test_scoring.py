"""Filtering, ranking and cross-portal merging."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.dedup import merge, same_flat
from scout.geo import parse_duration
from scout.models import Commute, Criteria, Image, Listing, VisionResult
from scout.scoring import in_search_area, passes_filters, score


def make_listing(**overrides) -> Listing:
    base = dict(
        source="flatfox",
        source_id="1",
        url="https://flatfox.ch/x",
        title="Schöne 4.5-Zimmer-Wohnung",
        description="Mit Balkon und Lift.",
        price_chf=2200,
        rooms=4.5,
        living_space_m2=95,
        street="Bahnhofstrasse 12",
        zipcode=5200,
        city="Brugg",
        lat=47.48,
        lon=8.21,
        category="APARTMENT",
        amenities=["BALCONY", "LIFT"],
        published=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return Listing(**base)


# --------------------------------------------------------------------------
# hard filters
# --------------------------------------------------------------------------


def test_default_listing_passes():
    assert passes_filters(make_listing(), Criteria())[0]


@pytest.mark.parametrize(
    "overrides,fragment",
    [
        ({"price_chf": 3500}, "price"),
        ({"price_chf": None}, "no price"),
        ({"rooms": 1.5}, "rooms"),
        ({"living_space_m2": 30}, "m2"),
        ({"category": "SHARED"}, "category"),
        ({"is_furnished": True}, "furnished"),
        ({"is_temporary": True}, "temporary"),
        ({"lat": 46.2, "lon": 6.1}, "outside search area"),
    ],
)
def test_hard_filters_drop_with_a_reason(overrides, fragment):
    kept, reason = passes_filters(make_listing(**overrides), Criteria())
    assert not kept
    assert fragment in reason


def test_missing_size_is_tolerated():
    # Plenty of genuine listings omit m2; dropping them would hide good flats.
    assert passes_filters(make_listing(living_space_m2=None), Criteria())[0]


def test_must_have_amenity_is_enforced():
    criteria = Criteria(must_have=["DISHWASHER"])
    kept, reason = passes_filters(make_listing(), criteria)
    assert not kept and "DISHWASHER" in reason


def test_exclude_keywords_match_description():
    criteria = Criteria(exclude_keywords=["möbliert"])
    kept, reason = passes_filters(
        make_listing(description="Komplett möbliert."), criteria
    )
    assert not kept and "möbliert" in reason


def test_commute_ceilings_are_enforced():
    criteria = Criteria(commute_a_max_min=30)
    legs = {"a": Commute(minutes=55), "b": Commute(minutes=20)}
    kept, reason = passes_filters(make_listing(), criteria, legs)
    assert not kept and "commute" in reason


def test_combined_commute_ceiling():
    criteria = Criteria(commute_a_max_min=60, commute_b_max_min=60, commute_total_max_min=70)
    legs = {"a": Commute(minutes=40), "b": Commute(minutes=40)}
    kept, reason = passes_filters(make_listing(), criteria, legs)
    assert not kept and "combined" in reason


def test_search_area_falls_back_to_postcode():
    criteria = Criteria()
    assert in_search_area(make_listing(lat=None, lon=None, zipcode=5200), criteria)
    assert not in_search_area(make_listing(lat=None, lon=None, zipcode=1200), criteria)


def test_search_area_keeps_listings_with_no_location_at_all():
    assert in_search_area(make_listing(lat=None, lon=None, zipcode=None), Criteria())


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


def test_score_is_bounded_and_explained():
    breakdown = score(make_listing(), Criteria())
    assert 0 <= breakdown.total <= 100
    assert breakdown.parts
    assert breakdown.reasons


def test_cheaper_scores_higher():
    criteria = Criteria()
    cheap = score(make_listing(price_chf=1900), criteria).parts["price"]
    dear = score(make_listing(price_chf=2750), criteria).parts["price"]
    assert cheap > dear


def test_fairness_prefers_a_balanced_pair_of_commutes():
    """The whole point of scoring for a couple rather than one person."""
    criteria = Criteria()
    balanced = score(make_listing(), criteria, {"a": Commute(minutes=40), "b": Commute(minutes=40)})
    lopsided = score(make_listing(), criteria, {"a": Commute(minutes=15), "b": Commute(minutes=65)})
    assert balanced.parts["commute_fairness"] > lopsided.parts["commute_fairness"]
    assert balanced.total > lopsided.total


def test_missing_subscores_do_not_count_as_zero():
    """A listing must not be punished for data we have not fetched yet."""
    criteria = Criteria()
    without = score(make_listing(), criteria)
    with_vision = score(make_listing(), criteria, vision=VisionResult(score=100))
    assert "vision" not in without.parts
    # No vision data must not drag the total towards zero.
    assert without.total > 50
    assert with_vision.total > without.total


def test_vision_score_moves_the_ranking():
    criteria = Criteria()
    good = score(make_listing(), criteria, vision=VisionResult(score=95, verdict="bright"))
    bad = score(make_listing(), criteria, vision=VisionResult(score=10, verdict="dated"))
    assert good.total > bad.total
    assert "bright" in " ".join(good.reasons)


def test_zero_weight_removes_a_dimension():
    criteria = Criteria(w_price=0)
    breakdown = score(make_listing(), criteria)
    assert "price" not in breakdown.weights


def test_stale_listings_lose_freshness():
    criteria = Criteria()
    old = make_listing(published=datetime.now(timezone.utc) - timedelta(days=40))
    assert score(old, criteria).parts["freshness"] == 0.0


# --------------------------------------------------------------------------
# dedup
# --------------------------------------------------------------------------


def test_same_flat_across_portals_is_merged():
    a = make_listing(source="flatfox", source_id="1")
    b = make_listing(source="immoscout", source_id="2", street="Bahnhofstr. 12")
    merged = merge([a, b])
    assert len(merged) == 1
    assert {item.source for item in merged[0].sources} == {"flatfox", "immoscout"}


def test_different_flats_stay_separate():
    a = make_listing(street="Bahnhofstrasse 12", price_chf=2200)
    b = make_listing(street="Seestrasse 4", price_chf=2600, source_id="2")
    assert len(merge([a, b])) == 2


def test_merge_fills_gaps_from_the_other_portal():
    a = make_listing(source="flatfox", living_space_m2=None, amenities=["BALCONY"])
    b = make_listing(
        source="immoscout",
        source_id="2",
        street="Bahnhofstr. 12",
        living_space_m2=95,
        amenities=["DISHWASHER"],
        images=[Image(url="https://example.com/a.jpg")],
    )
    result = merge([a, b])[0].merged()
    assert result.living_space_m2 == 95
    assert {"BALCONY", "DISHWASHER"} <= set(result.amenities)
    assert result.images  # the richer gallery wins


def test_same_flat_needs_matching_postcode():
    a = make_listing(zipcode=5200)
    b = make_listing(zipcode=8005, source_id="2")
    assert not same_flat(a, b)


# --------------------------------------------------------------------------
# geo helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("00d00:27:00", 27), ("00d01:05:00", 65), ("01d00:00:00", 1440), ("", None), ("x", None)],
)
def test_parse_duration(raw, expected):
    assert parse_duration(raw) == expected


def test_small_price_difference_still_merges():
    """The failure mode this guards: 2199 on one portal, 2201 on another."""
    a = make_listing(source="flatfox", price_chf=2199)
    b = make_listing(source="immoscout", source_id="2", street="Bahnhofstr. 12", price_chf=2201)
    assert len(merge([a, b])) == 1


def test_size_present_on_only_one_portal_still_merges():
    a = make_listing(source="flatfox", living_space_m2=None)
    b = make_listing(source="immoscout", source_id="2", living_space_m2=95)
    assert len(merge([a, b])) == 1


def test_clearly_different_rent_does_not_merge():
    a = make_listing(price_chf=1800)
    b = make_listing(source_id="2", price_chf=2600)
    assert len(merge([a, b])) == 2


# --------------------------------------------------------------------------
# notification eligibility
# --------------------------------------------------------------------------


def test_digest_skips_listings_without_a_resolved_commute(tmp_path, monkeypatch):
    """Guards the failure this caused: cheap flats far outside the corridor
    ranked top on price alone and would have been emailed before the timetable
    lookup could drop them."""
    monkeypatch.setenv("SCOUT_DB_PATH", str(tmp_path / "t.db"))
    from scout.config import get_config

    get_config.cache_clear()

    from scout import store
    from scout.db import connect, init_db
    from scout.dedup import merge
    from scout.models import Commute, ScoreBreakdown

    init_db()
    with connect() as conn:
        near = make_listing(source_id="near", street="Bahnhofstrasse 12", zipcode=5200)
        far = make_listing(source_id="far", street="Dorfstrasse 1", zipcode=3400, price_chf=1800)
        near_id, _ = store.upsert_listing(conn, merge([near])[0])
        far_id, _ = store.upsert_listing(conn, merge([far])[0])

        store.save_commutes(conn, near_id, {"a": Commute(minutes=30), "b": Commute(minutes=35)})
        store.save_commutes(conn, far_id, {"a": None, "b": None})
        store.save_score(conn, near_id, ScoreBreakdown(total=80.0), 1)
        store.save_score(conn, far_id, ScoreBreakdown(total=95.0), 1)

        eligible = [item["id"] for item in store.unnotified(conn, "digest", limit=10)]

    assert near_id in eligible
    assert far_id not in eligible, "unresolved commute must not be emailed"
    get_config.cache_clear()


def test_stale_score_is_removed_when_a_listing_stops_qualifying(tmp_path, monkeypatch):
    """A listing scored before its commute was known must lose that score once
    the timetable data arrives and disqualifies it - otherwise the ranking keeps
    serving a flat that no longer passes the filters."""
    monkeypatch.setenv("SCOUT_DB_PATH", str(tmp_path / "s.db"))
    from scout.config import get_config

    get_config.cache_clear()

    from scout import store
    from scout.db import connect, init_db
    from scout.dedup import merge
    from scout.models import ScoreBreakdown

    init_db()
    with connect() as conn:
        listing_id, _ = store.upsert_listing(conn, merge([make_listing()])[0])
        store.save_score(conn, listing_id, ScoreBreakdown(total=90.0), 1)
        assert store.ranked(conn, limit=10)

        store.drop_score(conn, listing_id)
        assert store.ranked(conn, limit=10) == []
    get_config.cache_clear()
