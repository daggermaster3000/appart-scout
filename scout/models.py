"""Domain models.

`Listing` is the normalized shape every source adapter must produce.
`Criteria` is the user-editable search + ranking definition; it is stored as JSON
so the web UI can round-trip it without a migration every time we add a knob.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# Normalized amenity vocabulary. Source adapters map their own vendor-specific
# flags onto these so scoring only ever deals with one set of names.
AMENITIES = [
    "BALCONY",
    "TERRACE",
    "GARDEN",
    "LIFT",
    "DISHWASHER",
    "WASHING_MACHINE",
    "PARKING",
    "GARAGE",
    "PETS_ALLOWED",
    "WHEELCHAIR",
    "FIREPLACE",
    "CELLAR",
    "QUIET",
    "NEW_BUILD",
    "MINERGIE",
]

ObjectCategory = Literal["APARTMENT", "HOUSE", "SHARED", "OTHER"]


class Image(BaseModel):
    url: str
    thumb_url: str | None = None
    caption: str = ""
    ordering: int = 0


class Listing(BaseModel):
    """One flat as seen on one platform, after normalization."""

    source: str
    source_id: str
    url: str

    title: str = ""
    description: str = ""

    price_chf: int | None = None  # gross monthly rent, incl. charges where known
    price_net_chf: int | None = None
    charges_chf: int | None = None
    rooms: float | None = None
    living_space_m2: int | None = None
    floor: int | None = None

    street: str = ""
    zipcode: int | None = None
    city: str = ""
    lat: float | None = None
    lon: float | None = None

    available_from: date | None = None
    available_immediately: bool = False

    category: ObjectCategory = "APARTMENT"
    is_furnished: bool = False
    is_temporary: bool = False
    year_built: int | None = None
    year_renovated: int | None = None

    amenities: list[str] = Field(default_factory=list)
    images: list[Image] = Field(default_factory=list)

    published: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def address(self) -> str:
        bits = [b for b in (self.street, f"{self.zipcode or ''} {self.city}".strip()) if b]
        return ", ".join(bits)


class Criteria(BaseModel):
    """What the couple is looking for, and how much each part matters.

    Every field is editable in the web UI. Hard filters knock listings out
    entirely; weights only reorder what survives.
    """

    name: str = "Zurich <-> Basel"

    # --- hard filters ---
    price_min: int = 1500
    price_max: int = 2800
    rooms_min: float = 3.0
    rooms_max: float = 5.5
    space_min_m2: int = 70
    space_max_m2: int = 200
    allow_furnished: bool = False
    allow_temporary: bool = False
    categories: list[str] = Field(default_factory=lambda: ["APARTMENT", "HOUSE"])
    move_in_earliest: date | None = None
    move_in_latest: date | None = None
    must_have: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(
        default_factory=lambda: ["möbliert", "befristet", "wg-zimmer", "untermiete"]
    )

    # --- where to search ---
    # Canton codes; each adapter maps these onto its own URL slugs. The default
    # is the Zurich <-> Basel corridor plus the two endpoints.
    cantons: list[str] = Field(
        default_factory=lambda: ["AG", "ZH", "BL", "BS", "SO"]
    )
    #: result pages to pull per canton per source (20-100 listings each)
    max_pages_per_region: int = 4

    # Cheap geographic pre-filter, applied before any commute lookup. Flatfox
    # has no server-side location filter and returns all of Switzerland, so
    # without this every run would spend timetable API calls proving that
    # Lausanne is not near Zurich. Defaults bound the Zurich <-> Basel corridor.
    lat_min: float = 47.05
    lat_max: float = 47.70
    lon_min: float = 7.35
    lon_max: float = 8.90
    #: fallback when a listing has no coordinates: inclusive postcode ranges
    zip_ranges: list[tuple[int, int]] = Field(
        default_factory=lambda: [
            (4000, 4699),  # Basel-Stadt, Baselland, Solothurn north
            (5000, 5999),  # Aargau
            (8000, 8499),  # Zurich city and Limmat valley
            (8600, 8999),  # Zurich oberland / lake
        ]
    )

    # --- commute ---
    workplace_a: str = "Zürich HB"
    workplace_b: str = "Basel SBB"
    label_a: str = "Partner A"
    label_b: str = "Partner B"
    arrive_by: str = "08:30"
    commute_a_max_min: int = 50
    commute_b_max_min: int = 50
    commute_total_max_min: int = 90
    max_walk_to_station_min: int = 15

    # --- soft preferences ---
    price_ideal: int = 2200
    space_ideal_m2: int = 95
    rooms_ideal: float = 4.0
    nice_to_have: list[str] = Field(
        default_factory=lambda: ["BALCONY", "DISHWASHER", "WASHING_MACHINE", "LIFT"]
    )

    # --- weights (0-10) ---
    w_price: float = 8
    w_space: float = 5
    w_rooms: float = 4
    w_commute_a: float = 7
    w_commute_b: float = 7
    w_commute_fairness: float = 5
    w_amenities: float = 4
    w_freshness: float = 3
    w_vision: float = 8

    # --- photo evaluation ---
    vision_brief: str = (
        "Bright with real daylight, modern or recently renovated kitchen and bathroom, "
        "no dark 1970s wood panelling, no worn carpet, usable balcony, "
        "rooms that look genuinely spacious rather than wide-angle-lens spacious."
    )

    def weights(self) -> dict[str, float]:
        return {
            "price": self.w_price,
            "space": self.w_space,
            "rooms": self.w_rooms,
            "commute_a": self.w_commute_a,
            "commute_b": self.w_commute_b,
            "commute_fairness": self.w_commute_fairness,
            "amenities": self.w_amenities,
            "freshness": self.w_freshness,
            "vision": self.w_vision,
        }


class Settings(BaseModel):
    """Operational knobs, also editable in the web UI."""

    # Flatfox only by default. The other four portals sit behind DataDome or
    # Cloudflare: every one of them refuses a plain HTTP client outright, and
    # only sometimes lets a headed Chromium through. Enabling them costs a
    # browser launch per run and usually returns nothing, so they are opt-in.
    # Their inventory comes in through the `mailbox` source instead.
    enabled_sources: list[str] = Field(default_factory=lambda: ["flatfox"])
    #: how often to scrape. Separate from the digest cadence: scraping often
    #: catches new listings early (and feeds instant alerts) without burying
    #: anyone in email.
    run_every_hours: int = 6
    digest_every_days: int = 3
    digest_hour: int = 8
    digest_size: int = 8
    recipients: list[str] = Field(default_factory=list)
    send_when_empty: bool = False

    vision_enabled: bool = True
    vision_top_n: int = 10
    vision_max_photos: int = 4
    # Photos cost money, so only look at flats that already earned it on the
    # metrics that are free: price, size, rooms, amenities and both commutes.
    # A listing whose commute is still unresolved is scored on price and size
    # alone, which floats cheap roomy places far outside the corridor to the
    # top — photographing those is the main way this budget got wasted.
    vision_min_score: float = 70.0

    # --- credentials -------------------------------------------------------
    # All of these are editable in the web UI so that changing a password does
    # not mean SSHing to the Pi, editing .env and restarting the unit. Every one
    # of them is optional here: blank (or None) means "fall back to the matching
    # variable in .env". See `config.Config.resolve`, which merges the two, and
    # the Settings page, which never renders a stored secret back.
    openai_api_key: str = ""
    openai_model: str = ""

    smtp_host: str = ""
    smtp_port: int | None = None
    smtp_user: str = ""
    smtp_password: str = ""
    # Tri-state on purpose: None means "whatever .env says", so a checkbox
    # (which submits nothing when unchecked) would be lossy here.
    smtp_starttls: bool | None = None
    smtp_from: str = ""

    imap_host: str = ""
    imap_port: int | None = None
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = ""
    imap_ssl: bool | None = None

    instant_alert_enabled: bool = True
    instant_alert_min_score: float = 85.0

    # Flatfox and friends can return thousands of stale rows; don't consider
    # anything first published longer ago than this.
    max_listing_age_days: int = 45

    # transport.opendata.ch is free and volunteer-run, and starts returning 429
    # well before its documented daily quota. A cold database needs a few
    # hundred lookups (one per station, two routes per station), so the first
    # run deliberately does not try to finish: it resolves this many, and later
    # runs pick up the rest against a warm 30-day cache. Expect commute columns
    # to fill in over the first day rather than the first run.
    max_commute_calls_per_run: int = 150

    # The corridor map prices whole stations rather than listings, two timetable
    # calls each, so one click is deliberately a batch rather than the whole
    # corridor: ~350 stations would be ~700 calls and several throttled minutes.
    # Every answer is cached, so clicking again continues instead of repeating.
    corridor_batch_size: int = 60


class VisionResult(BaseModel):
    score: int = 0  # 0-100
    verdict: str = ""
    condition: str = ""
    brightness: str = ""
    kitchen: str = ""
    bathroom: str = ""
    renovation_era: str = ""
    red_flags: list[str] = Field(default_factory=list)


class ScoreBreakdown(BaseModel):
    total: float = 0.0
    parts: dict[str, float] = Field(default_factory=dict)  # name -> 0..1 subscore
    weights: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)


class Commute(BaseModel):
    minutes: int
    transfers: int = 0
    origin_station: str = ""
