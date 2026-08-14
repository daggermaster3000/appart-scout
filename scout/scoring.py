"""Hard filtering and weighted ranking.

Two stages, deliberately separate:

`passes_filters()` answers "is this even a candidate?" - price, size, category,
furnished/temporary, must-have amenities, commute ceilings. Failing here removes
the listing entirely.

`score()` answers "how good is it?" over the survivors. Every sub-score is
normalized to 0..1 and combined as a weighted mean, so the weights the user
drags around in the UI mean what they look like they mean. Sub-scores that
cannot be computed (no commute data yet, no photo evaluation yet) are dropped
from the mean rather than counted as zero - otherwise a listing would be
punished for our own missing data.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .models import Commute, Criteria, Listing, ScoreBreakdown, VisionResult

FRESHNESS_HORIZON_DAYS = 21.0
#: commute gap at which "unfair to one partner" scores zero
FAIRNESS_HORIZON_MIN = 45.0


def _ramp(value: float, good: float, bad: float) -> float:
    """1.0 at `good`, 0.0 at `bad`, linear in between. Works in either direction."""
    if good == bad:
        return 1.0
    return max(0.0, min(1.0, (bad - value) / (bad - good)))


# --------------------------------------------------------------------------
# hard filters
# --------------------------------------------------------------------------


def in_search_area(listing: Listing, criteria: Criteria) -> bool:
    """Cheap geographic gate, applied before any timetable lookup.

    Resolving a commute costs an API call and a cache row, so listings that are
    obviously nowhere near the corridor get dropped on coordinates (or, failing
    that, postcode) first. Deliberately generous - the commute ceilings do the
    real work; this only stops us pricing a trip from Lugano.
    """
    if listing.lat is not None and listing.lon is not None:
        return (
            criteria.lat_min <= listing.lat <= criteria.lat_max
            and criteria.lon_min <= listing.lon <= criteria.lon_max
        )
    if listing.zipcode is not None:
        return any(low <= listing.zipcode <= high for low, high in criteria.zip_ranges)
    # No location at all: keep it and let the commute stage decide.
    return True


def passes_filters(
    listing: Listing,
    criteria: Criteria,
    commutes: dict[str, Commute | None] | None = None,
) -> tuple[bool, str]:
    """Return (kept, reason-if-dropped)."""
    if listing.category not in criteria.categories:
        return False, f"category {listing.category}"

    if not in_search_area(listing, criteria):
        return False, "outside search area"

    if listing.is_furnished and not criteria.allow_furnished:
        return False, "furnished"
    if listing.is_temporary and not criteria.allow_temporary:
        return False, "temporary"

    price = listing.price_chf
    if price is None:
        return False, "no price"
    if price < criteria.price_min:
        return False, f"price {price} < {criteria.price_min}"
    if price > criteria.price_max:
        return False, f"price {price} > {criteria.price_max}"

    if listing.rooms is not None and not (
        criteria.rooms_min <= listing.rooms <= criteria.rooms_max
    ):
        return False, f"{listing.rooms} rooms"

    space = listing.living_space_m2
    if space is not None and not (criteria.space_min_m2 <= space <= criteria.space_max_m2):
        return False, f"{space} m2"
    # Missing size is tolerated: plenty of real listings omit it, and dropping
    # them would silently hide good flats.

    missing = [a for a in criteria.must_have if a not in listing.amenities]
    if missing:
        return False, f"missing {', '.join(missing)}"

    blob = f"{listing.title}\n{listing.description}".lower()
    for word in criteria.exclude_keywords:
        if word.strip() and word.strip().lower() in blob:
            return False, f"keyword {word!r}"

    if listing.available_from:
        if criteria.move_in_earliest and listing.available_from < criteria.move_in_earliest:
            return False, f"available {listing.available_from} too early"
        if criteria.move_in_latest and listing.available_from > criteria.move_in_latest:
            return False, f"available {listing.available_from} too late"

    if commutes:
        a, b = commutes.get("a"), commutes.get("b")
        if a and a.minutes > criteria.commute_a_max_min:
            return False, f"{criteria.label_a} commute {a.minutes}'"
        if b and b.minutes > criteria.commute_b_max_min:
            return False, f"{criteria.label_b} commute {b.minutes}'"
        if a and b and (a.minutes + b.minutes) > criteria.commute_total_max_min:
            return False, f"combined commute {a.minutes + b.minutes}'"

    return True, ""


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


def score(
    listing: Listing,
    criteria: Criteria,
    commutes: dict[str, Commute | None] | None = None,
    vision: VisionResult | None = None,
) -> ScoreBreakdown:
    parts: dict[str, float] = {}
    reasons: list[str] = []
    commutes = commutes or {}

    # Price: anything at or under the ideal is perfect; decays to the ceiling.
    if listing.price_chf is not None:
        parts["price"] = (
            1.0
            if listing.price_chf <= criteria.price_ideal
            else _ramp(listing.price_chf, criteria.price_ideal, criteria.price_max)
        )
        if listing.price_chf <= criteria.price_ideal:
            reasons.append(f"CHF {listing.price_chf} is at or under target")

    # Space: ramps up to the ideal, then flat - a bigger flat is not better
    # per square metre once it is big enough.
    if listing.living_space_m2 is not None:
        parts["space"] = (
            1.0
            if listing.living_space_m2 >= criteria.space_ideal_m2
            else _ramp(listing.living_space_m2, criteria.space_ideal_m2, criteria.space_min_m2)
        )

    if listing.rooms is not None:
        spread = max(criteria.rooms_ideal - criteria.rooms_min, criteria.rooms_max - criteria.rooms_ideal, 1.0)
        parts["rooms"] = _ramp(abs(listing.rooms - criteria.rooms_ideal), 0.0, spread)

    a, b = commutes.get("a"), commutes.get("b")
    if a:
        parts["commute_a"] = _ramp(a.minutes, 0.0, float(criteria.commute_a_max_min))
    if b:
        parts["commute_b"] = _ramp(b.minutes, 0.0, float(criteria.commute_b_max_min))
    if a and b:
        gap = abs(a.minutes - b.minutes)
        parts["commute_fairness"] = _ramp(gap, 0.0, FAIRNESS_HORIZON_MIN)
        reasons.append(
            f"{criteria.label_a} {a.minutes}' / {criteria.label_b} {b.minutes}'"
            + (f" (gap {gap}')" if gap > 15 else " (balanced)")
        )

    if criteria.nice_to_have:
        hits = [x for x in criteria.nice_to_have if x in listing.amenities]
        parts["amenities"] = len(hits) / len(criteria.nice_to_have)
        if hits:
            reasons.append("has " + ", ".join(x.lower().replace("_", " ") for x in hits))

    if listing.published:
        published = listing.published
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - published).total_seconds() / 86400
        parts["freshness"] = _ramp(age_days, 0.0, FRESHNESS_HORIZON_DAYS)
        if age_days <= 2:
            reasons.append("posted in the last 48h")

    if vision is not None:
        parts["vision"] = max(0.0, min(1.0, vision.score / 100.0))
        if vision.verdict:
            reasons.append(vision.verdict)
        for flag in vision.red_flags[:2]:
            reasons.append(f"photos: {flag}")

    weights = criteria.weights()
    active = {k: weights.get(k, 0.0) for k in parts if weights.get(k, 0.0) > 0}
    total_weight = sum(active.values())
    total = (
        100.0 * sum(parts[k] * w for k, w in active.items()) / total_weight
        if total_weight
        else 0.0
    )

    return ScoreBreakdown(
        total=round(total, 1),
        parts={k: round(v, 3) for k, v in parts.items()},
        weights=active,
        reasons=reasons,
    )
