"""Score-ordered commute budget and the vetted/shortlist dashboard views."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scout import store
from scout.config import Config, get_config
from scout.db import connect, init_db, load_criteria, load_settings
from scout.dedup import MergedListing
from scout.models import Commute, Image, Listing
from scout.pipeline import _rank


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_DB_PATH", str(tmp_path / "scout.db"))
    monkeypatch.setattr(Config, "model_config", {"env_file": None, "extra": "ignore"})
    get_config.cache_clear()
    init_db()
    yield
    get_config.cache_clear()


def add_listing(conn, key: str, *, price: int, resolved: bool = False) -> str:
    """A listing that passes every metadata filter; price steers its score."""
    zipcode = 5000 + abs(hash(key)) % 900  # distinct dedup bucket per key
    listing = Listing(
        source="flatfox",
        source_id=key,
        url=f"https://flatfox.ch/de/flat/{key}/",
        title=f"Flat {key}",
        price_chf=price,
        rooms=3.5,
        living_space_m2=90,
        zipcode=zipcode,
        city="Aarau",
    )
    listing_id, _ = store.upsert_listing(conn, MergedListing([listing]))
    if resolved:
        store.save_commutes(
            conn,
            listing_id,
            {
                "a": Commute(minutes=34, transfers=1, origin_station="Aarau"),
                "b": Commute(minutes=41, transfers=0, origin_station="Aarau"),
            },
        )
    return listing_id


class FakeCommuteService:
    """Counts lookups and records their order; never touches the network."""

    def __init__(self, budget_used_per_listing: int = 3) -> None:
        self.api_calls = 0
        self.searchch_calls = 0
        self.throttled = False
        self.searchch_throttled = False
        self.calls_per_listing = budget_used_per_listing
        self.order: list[str] = []

    @property
    def calls(self) -> int:
        return self.api_calls + self.searchch_calls

    @property
    def exhausted(self) -> bool:
        return self.throttled and self.searchch_throttled

    async def commutes(self, listing: Listing) -> dict[str, Commute | None]:
        self.api_calls += self.calls_per_listing
        self.order.append(listing.title.removeprefix("Flat "))
        return {
            "a": Commute(minutes=30, transfers=0, origin_station="X"),
            "b": Commute(minutes=45, transfers=1, origin_station="X"),
        }


async def run_rank(conn, commute, max_calls: int):
    criteria = load_criteria(conn)
    settings = load_settings(conn).model_copy(update={"max_commute_calls_per_run": max_calls})
    stats: dict = {}
    kept = await _rank(conn, criteria, settings, commute, version=1, stats=stats)
    return kept, stats


async def test_commute_budget_is_spent_on_the_best_provisional_scores_first(db):
    """The regression: DB order spent the budget on arbitrary rows."""
    with connect() as conn:
        # Cheaper flats score higher (w_price dominates when all else is equal).
        add_listing(conn, "cheap", price=1600)
        add_listing(conn, "mid", price=2200)
        add_listing(conn, "dear", price=2750)

        commute = FakeCommuteService()
        # Budget for exactly two listings (3 calls each).
        _kept, stats = await run_rank(conn, commute, max_calls=6)

    assert commute.order == ["cheap", "mid"]  # best first, budget stops before "dear"
    assert stats["commutes_resolved"] == 2


async def test_already_resolved_listings_cost_no_budget(db):
    with connect() as conn:
        add_listing(conn, "done", price=1600, resolved=True)
        add_listing(conn, "todo", price=2200)

        commute = FakeCommuteService()
        await run_rank(conn, commute, max_calls=30)

    assert commute.order == ["todo"]


async def test_a_listing_disqualified_by_its_commute_is_dropped_from_kept(db):
    class OverCeiling(FakeCommuteService):
        async def commutes(self, listing):
            self.api_calls += 3
            self.order.append(listing.title.removeprefix("Flat "))
            # Default ceilings are 50/50, total 90.
            return {
                "a": Commute(minutes=80, transfers=2, origin_station="X"),
                "b": Commute(minutes=85, transfers=2, origin_station="X"),
            }

    with connect() as conn:
        listing_id = add_listing(conn, "far", price=1600)
        kept, stats = await run_rank(conn, OverCeiling(), max_calls=30)

    assert listing_id not in kept
    assert stats["dropped"] == 1


async def test_both_services_throttled_stops_pass_two_but_keeps_provisional_scores(db):
    with connect() as conn:
        listing_id = add_listing(conn, "waiting", price=1600)
        commute = FakeCommuteService()
        commute.throttled = True
        commute.searchch_throttled = True
        kept, stats = await run_rank(conn, commute, max_calls=30)

    assert commute.order == []
    assert listing_id in kept  # scored on metadata, waiting for next run
    assert stats["commutes_resolved"] == 0


async def test_the_primary_timetable_throttling_alone_does_not_stop_pass_two(db):
    """opendata.ch giving up is not the end: search.ch answers the same question."""
    with connect() as conn:
        listing_id = add_listing(conn, "waiting", price=1600)
        commute = FakeCommuteService()
        commute.throttled = True
        kept, stats = await run_rank(conn, commute, max_calls=30)

    assert commute.order == ["waiting"]
    assert listing_id in kept
    assert stats["commutes_resolved"] == 1


# -- dashboard views ---------------------------------------------------------


@pytest.fixture
def client(db):
    from scout.web.app import app

    return TestClient(app)


def seed(conn):
    """One vetted listing, one provisional, one shortlisted-and-vetted."""
    from scout.models import ScoreBreakdown

    vetted = add_listing(conn, "vetted", price=1700, resolved=True)
    provisional = add_listing(conn, "provisional", price=1600)
    starred = add_listing(conn, "starred", price=1800, resolved=True)
    for listing_id, total in ((vetted, 80.0), (provisional, 90.0), (starred, 75.0)):
        store.save_score(conn, listing_id, ScoreBreakdown(total=total), 1)
    store.set_feedback(conn, starred, "shortlist")
    return vetted, provisional, starred


def test_default_view_shows_only_fully_vetted_listings(client):
    with connect() as conn:
        vetted, provisional, starred = seed(conn)
    page = client.get("/").text
    assert f'/listing/{vetted}' in page
    assert f'/listing/{starred}' in page
    assert f'/listing/{provisional}' not in page  # top provisional score, still hidden


def test_everything_view_still_shows_provisional_listings(client):
    with connect() as conn:
        _vetted, provisional, _starred = seed(conn)
    page = client.get("/?view=all").text
    assert f'/listing/{provisional}' in page


def test_shortlist_view_shows_exactly_the_starred_listings(client):
    with connect() as conn:
        vetted, provisional, starred = seed(conn)
    page = client.get("/?view=shortlist").text
    assert f'/listing/{starred}' in page
    assert f'/listing/{vetted}' not in page
    assert f'/listing/{provisional}' not in page


def test_nav_shows_the_shortlist_count(client):
    with connect() as conn:
        seed(conn)
    assert "Shortlist (1)" in client.get("/").text


def test_starring_from_the_grid_lands_it_on_the_shortlist(client):
    with connect() as conn:
        vetted, _provisional, _starred = seed(conn)
    client.post(f"/listing/{vetted}/feedback", data={"verdict": "shortlist"})
    page = client.get("/?view=shortlist").text
    assert f'/listing/{vetted}' in page
    assert "Shortlist (2)" in page


def test_unknown_view_falls_back_to_best(client):
    with connect() as conn:
        _vetted, provisional, _starred = seed(conn)
    page = client.get("/?view=banana").text
    assert f'/listing/{provisional}' not in page


def test_fresh_listing_gets_a_new_pill(client):
    with connect() as conn:
        vetted, _p, _s = seed(conn)  # first_seen is now
    assert '<span class="pill new">new</span>' in client.get("/").text