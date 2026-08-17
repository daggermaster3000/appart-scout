"""The corridor map: station registry, resolution order, and the timetable fallback."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scout import corridor, stations
from scout.config import Config, get_config
from scout.db import connect, init_db, load_criteria, set_kv
from scout.geo import CommuteService, SEARCH_CH_API
from scout.models import Listing
from scout.stations import DATASET, Station


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_DB_PATH", str(tmp_path / "scout.db"))
    monkeypatch.setattr(Config, "model_config", {"env_file": None, "extra": "ignore"})
    get_config.cache_clear()
    init_db()
    yield
    get_config.cache_clear()


def add_stations(conn, *rows: tuple[int, str, float, float, str]) -> None:
    stations.save(
        conn,
        [
            Station(id=i, name=name, lat=lat, lon=lon, municipality=town, canton="AG")
            for i, name, lat, lon, town in rows
        ],
    )


def price(conn, origin: str, destination: str, minutes: int | None, arrive_by="08:30") -> None:
    conn.execute(
        "INSERT INTO route_cache (origin, destination, arrive_by, minutes, transfers, "
        "fetched_at) VALUES (?,?,?,?,0,'2099-01-01T00:00:00+00:00')",
        (origin, destination, arrive_by, minutes),
    )
    conn.commit()


# -- the SBB station register ------------------------------------------------


def test_the_registry_query_asks_only_for_current_corridor_train_stops(db):
    with connect() as conn:
        criteria = load_criteria(conn)
    where = stations._where(criteria)

    assert 'stoppoint="true"' in where              # not junctions or depots
    assert 'meansoftransport LIKE "TRAIN"' in where  # not bus or boat stops
    assert "in_bbox(geopos, 47.05, 7.35, 47.7, 8.9)" in where
    assert 'cantonabbreviation in ("AG", "ZH", "BL", "BS", "SO")' in where


@respx.mock
async def test_the_registry_is_paged_and_deduplicated_by_didok_number(db):
    def page(request):
        offset = int(dict(request.url.params)["offset"])
        if offset:
            # Second page repeats one station and is short, ending the paging.
            return httpx.Response(200, json={"results": [_record(2, "Brugg")]})
        return httpx.Response(
            200,
            json={"results": [_record(1, "Aarau"), _record(2, "Brugg")] * 50},
        )

    respx.get(url__startswith=DATASET).mock(side_effect=page)
    with connect() as conn:
        found = await stations.fetch_corridor(httpx.AsyncClient(), load_criteria(conn))

    assert [s.name for s in found] == ["Aarau", "Brugg"]


def _record(number: int, name: str) -> dict:
    return {
        "number": number,
        "designationofficial": name,
        "geopos": {"lat": 47.39, "lon": 8.05},
        "municipalityname": name,
        "cantonabbreviation": "AG",
    }


def test_records_without_a_position_are_skipped(db):
    assert stations._to_station({"number": 1, "designationofficial": "X", "geopos": {}}) is None
    assert stations._to_station({"number": 1, "geopos": {"lat": 47.0, "lon": 8.0}}) is None


def test_stations_outside_the_corridor_are_pruned_after_a_criteria_edit(db):
    with connect() as conn:
        add_stations(
            conn,
            (1, "Aarau", 47.39, 8.05, "Aarau"),
            (2, "Lugano", 46.00, 8.95, "Lugano"),
        )
        removed = stations.prune_outside(conn, load_criteria(conn))
        assert removed == 1
        assert [s.name for s in stations.load(conn)] == ["Aarau"]


# -- reading the map ---------------------------------------------------------


def test_the_map_reports_both_commutes_the_gap_and_whether_it_clears_the_limits(db):
    with connect() as conn:
        criteria = load_criteria(conn)
        add_stations(conn, (1, "Aarau", 47.39, 8.05, "Aarau"))
        price(conn, "Aarau", criteria.workplace_a, 34)
        price(conn, "Aarau", criteria.workplace_b, 41)

        point = corridor.load_map(conn, criteria)["points"][0]

    assert (point["a"], point["b"]) == (34, 41)
    assert point["gap"] == 7
    assert point["total"] == 75
    assert point["worst"] == 41          # the partner who has it worse
    assert point["ok"] is True           # 34/41 clear the 50/50/90 ceilings


def test_a_station_over_a_ceiling_is_still_shown_but_marked_out_of_limits(db):
    with connect() as conn:
        criteria = load_criteria(conn)
        add_stations(conn, (1, "Far", 47.39, 8.05, "Far"))
        price(conn, "Far", criteria.workplace_a, 80)
        price(conn, "Far", criteria.workplace_b, 20)

        point = corridor.load_map(conn, criteria)["points"][0]

    assert point["ok"] is False
    assert point["gap"] == 60


def test_an_unpriced_station_has_no_derived_numbers_rather_than_zeroes(db):
    with connect() as conn:
        add_stations(conn, (1, "Aarau", 47.39, 8.05, "Aarau"))
        data = corridor.load_map(conn, load_criteria(conn))

    point = data["points"][0]
    assert (point["a"], point["b"], point["gap"], point["total"]) == (None, None, None, None)
    assert data["resolved"] == 0 and data["total"] == 1


def test_scheduling_flags_never_reach_the_browser(db):
    with connect() as conn:
        add_stations(conn, (1, "Aarau", 47.39, 8.05, "Aarau"))
        criteria = load_criteria(conn)
        assert "_due_a" in corridor.load_map(conn, criteria)["points"][0]
        assert "_due_a" not in corridor.public_map(conn, criteria)["points"][0]


# -- resolution order --------------------------------------------------------


def test_stations_nearest_the_workplace_line_are_priced_first(db):
    """Budget order is the feature: the axis has to read after one batch."""
    anchors = {
        "a": {"name": "Zürich HB", "lat": 47.378, "lon": 8.540},
        "b": {"name": "Basel SBB", "lat": 47.547, "lon": 7.590},
    }
    points = [
        {"name": "off-axis", "lat": 47.05, "lon": 8.60},   # far south of the line
        {"name": "on-axis", "lat": 47.46, "lon": 8.05},    # roughly the midpoint
    ]
    assert [p["name"] for p in corridor._priority(points, anchors)] == ["on-axis", "off-axis"]


def test_without_anchors_the_order_is_at_least_stable(db):
    points = [{"name": "Brugg", "lat": 47.5, "lon": 8.2}, {"name": "Aarau", "lat": 47.4, "lon": 8.0}]
    assert [p["name"] for p in corridor._priority(points, {})] == ["Aarau", "Brugg"]


def test_a_recent_empty_answer_is_not_re_asked_in_the_next_batch(db):
    """Otherwise the same few failures sit at the head of every batch forever."""
    with connect() as conn:
        criteria = load_criteria(conn)
        add_stations(conn, (1, "Aarau", 47.39, 8.05, "Aarau"))
        price(conn, "Aarau", criteria.workplace_a, None)   # asked, came back empty
        price(conn, "Aarau", criteria.workplace_b, 41)

        point = corridor.load_map(conn, criteria)["points"][0]

    assert point["a"] is None
    assert point["_due_a"] is False


def test_anchors_are_forgotten_when_a_workplace_is_renamed(db):
    with connect() as conn:
        criteria = load_criteria(conn)
        set_kv(
            conn,
            "workplace_geo",
            {"key": "Zürich HB|Basel SBB", "a": {"name": "Zürich HB", "lat": 47.4, "lon": 8.5}},
        )
        assert corridor.load_anchors(conn, criteria)

        moved = criteria.model_copy(update={"workplace_b": "Bern"})
        assert corridor.load_anchors(conn, moved) == {}


# -- the timetable and its fallback ------------------------------------------


@respx.mock
async def test_search_ch_answers_when_opendata_ch_will_not(db):
    respx.get(url__startswith="https://transport.opendata.ch/v1/connections").mock(
        return_value=httpx.Response(429)
    )
    respx.get(url__startswith=SEARCH_CH_API).mock(
        return_value=httpx.Response(
            200,
            json={
                "connections": [
                    # 29 minutes, one change (the last leg is arrival-only).
                    {"duration": 1740, "legs": [{}, {}, {}]},
                    {"duration": 1500, "legs": [{}, {}]},
                    {"duration": 3060, "legs": [{}, {}, {}]},
                ]
            },
        )
    )

    with connect() as conn:
        service = CommuteService(conn, httpx.AsyncClient(), load_criteria(conn), rate_limit=0)
        commute = await service.route("Aarau", "Zürich HB")

    assert commute is not None
    assert commute.minutes == 29           # median of the three shortest
    assert commute.transfers == 1
    assert service.searchch_calls == 1


@respx.mock
async def test_a_route_neither_service_could_answer_is_not_cached_as_a_fact(db):
    """A dead API is a fact about the API, not about the route."""
    respx.get(url__startswith="https://transport.opendata.ch/v1/connections").mock(
        return_value=httpx.Response(429)
    )
    respx.get(url__startswith=SEARCH_CH_API).mock(return_value=httpx.Response(500))

    with connect() as conn:
        service = CommuteService(conn, httpx.AsyncClient(), load_criteria(conn), rate_limit=0)
        assert await service.route("Aarau", "Zürich HB") is None
        assert conn.execute("SELECT COUNT(*) c FROM route_cache").fetchone()["c"] == 0


async def test_living_at_the_workplace_station_is_zero_minutes_not_unknown(db):
    with connect() as conn:
        service = CommuteService(conn, httpx.AsyncClient(), load_criteria(conn), rate_limit=0)
        commute = await service.route("Basel SBB", "Basel SBB")

        assert commute is not None and commute.minutes == 0
        # Recorded, or the two anchors would be re-queued in every batch.
        assert conn.execute(
            "SELECT minutes FROM route_cache WHERE origin = 'Basel SBB'"
        ).fetchone()["minutes"] == 0
    assert service.api_calls == 0


async def test_a_listing_finds_its_station_from_the_register_without_an_api_call(db):
    """The register turns geocoding from a metered call into arithmetic."""
    with connect() as conn:
        add_stations(conn, (1, "Aarau", 47.3914, 8.0513, "Aarau"))
        service = CommuteService(conn, httpx.AsyncClient(), load_criteria(conn), rate_limit=0)
        listing = Listing(source="x", source_id="1", url="", lat=47.3930, lon=8.0530)

        name, walk = await service.nearest_station(listing)

    assert name == "Aarau"
    assert walk <= 5          # a few hundred metres
    assert service.api_calls == 0


@respx.mock
async def test_a_flat_far_from_every_station_still_asks_the_timetable_api(db):
    """The register only knows trains; the API also knows buses and trams."""
    respx.get(url__startswith="https://transport.opendata.ch/v1/locations").mock(
        return_value=httpx.Response(
            200,
            json={"stations": [{"id": "1", "name": "Somewhere, Dorf", "distance": 240}]},
        )
    )
    with connect() as conn:
        add_stations(conn, (1, "Aarau", 47.3914, 8.0513, "Aarau"))
        service = CommuteService(conn, httpx.AsyncClient(), load_criteria(conn), rate_limit=0)
        listing = Listing(source="x", source_id="1", url="", lat=47.60, lon=8.40)

        name, _walk = await service.nearest_station(listing)

    assert name == "Somewhere, Dorf"
    assert service.api_calls == 1


async def test_a_batch_yields_to_a_scouting_run_rather_than_competing_with_it(db, monkeypatch):
    """Two rate limiters that don't know about each other is how you get throttled."""
    monkeypatch.setattr(corridor.scheduler, "is_running", lambda: True)
    with connect() as conn:
        add_stations(conn, (1, "Aarau", 47.39, 8.05, "Aarau"))

    await corridor._job(refresh=False, batch=10)

    assert corridor.progress()["note"] == "waiting for the current scouting run"
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM route_cache").fetchone()["c"] == 0


# -- the page ----------------------------------------------------------------


@pytest.fixture
def client(db):
    from scout.web.app import app

    with TestClient(app) as test_client:
        yield test_client


def test_the_map_page_offers_to_load_stations_before_it_has_any(client):
    body = client.get("/corridor").text
    assert "Load stations from SBB" in body


def test_the_map_page_embeds_its_data_and_a_table_of_the_same_numbers(client):
    with connect() as conn:
        criteria = load_criteria(conn)
        add_stations(conn, (1, "Aarau", 47.39, 8.05, "Aarau"))
        price(conn, "Aarau", criteria.workplace_a, 34)
        price(conn, "Aarau", criteria.workplace_b, 41)

    body = client.get("/corridor").text
    assert '<script id="cor-data" type="application/json">' in body
    assert "Table view" in body

    data = client.get("/api/corridor").json()
    assert data["points"][0]["name"] == "Aarau"
    assert data["resolved"] == 1


def test_embedded_json_cannot_close_the_script_block_early(client):
    with connect() as conn:
        add_stations(conn, (1, "</script><script>alert(1)</script>", 47.39, 8.05, "Bad"))

    body = client.get("/corridor").text
    assert "</script><script>alert(1)" not in body
    assert "\\u003c/script>" in body
