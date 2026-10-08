"""Racing query engine — structured access to Ladbrokes racing data.

Provides dataclasses and query functions for filtering races by country,
meeting, runner count, favourite price, second favourite price, time window, etc.

Two-stage filtering:
    Stage A (metadata) — country, territory, meeting, time_from, time_to
        Uses a single API call to fetch all events. No per-race detail calls.
    Stage B (detail/price) — min_runners, max_fav_price, min_second_fav_price
        Only triggered when one or more of these filters is present.
        Makes one cached API call per race that passed Stage A.

Usage:
    from racing_query import get_races, get_race_prices, get_favourite, query_races

    # Metadata-only (fast, single API call):
    races = query_races(filters={"country": "FR", "time_from": "14:00", "time_to": "17:00"})

    # With price filters (slower, per-race API calls):
    results = query_races(filters={"country": "FR", "max_fav_price": 2.0})
"""

import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Union

from lads_client import LadsClient, parse_event_name, detect_country, is_virtual_event, is_placeholder_selection, is_non_runner


# ── In-Memory Cache ──


class _DetailCache:
    """Simple in-memory cache for event detail API calls.

    Avoids repeated HTTP requests for the same event_id during one script run.
    """

    def __init__(self):
        self._store: dict[int, "RaceDetail"] = {}
        self.hits: int = 0
        self.misses: int = 0

    def get(self, event_id: int) -> "RaceDetail | None":
        result = self._store.get(event_id)
        if result is not None:
            self.hits += 1
        return result

    def put(self, event_id: int, detail: "RaceDetail"):
        self._store[event_id] = detail

    def has(self, event_id: int) -> bool:
        return event_id in self._store

    def clear(self):
        self._store.clear()
        self.hits = 0
        self.misses = 0

    @property
    def size(self) -> int:
        return len(self._store)


# Module-level cache — lives for the duration of the script run
_cache = _DetailCache()


def clear_cache():
    """Clear the in-memory event detail cache."""
    _cache.clear()


# ── Query Stats ──


@dataclass
class QueryStats:
    """Lightweight summary of what a query did."""
    total_events_fetched: int = 0
    after_metadata_filters: int = 0
    detail_calls_made: int = 0
    cache_hits: int = 0
    results_returned: int = 0
    needs_detail: bool = False

    def print_summary(self):
        print(f"\n  ⚡ Query stats:")
        print(f"     Events from API:       {self.total_events_fetched}")
        print(f"     After metadata filter: {self.after_metadata_filters}")
        if self.needs_detail:
            print(f"     Detail API calls:      {self.detail_calls_made}")
            print(f"     Cache hits:            {self.cache_hits}")
        else:
            print(f"     Detail API calls:      0 (metadata-only query)")
        print(f"     Results returned:      {self.results_returned}")


# ── Dataclasses ──


@dataclass
class Runner:
    """A single runner (horse) in a race."""
    number: int
    name: str
    price_decimal: float | None
    price_fractional: str
    is_nr: bool
    status: str


@dataclass
class Race:
    """Race metadata (no prices — cheap to build from the events list)."""
    event_id: int
    event_name: str
    time: str
    track: str
    country: str
    status: str


@dataclass
class RaceDetail:
    """Full race detail including runners and derived favourite info."""
    event_id: int
    event_name: str
    time: str
    track: str
    country: str
    status: str
    runners: list[Runner] = field(default_factory=list)
    ew_places: int | None = None
    ew_fraction: str | None = None

    @property
    def active_runners(self) -> list[Runner]:
        """Runners that are not non-runners."""
        return [r for r in self.runners if not r.is_nr]

    @property
    def runner_count(self) -> int:
        """Number of active (non-NR) runners."""
        return len(self.active_runners)

    @property
    def favourite(self) -> Runner | None:
        """Shortest-priced active runner (lowest decimal price)."""
        priced = [r for r in self.active_runners if r.price_decimal and r.price_decimal > 0]
        if not priced:
            return None
        return min(priced, key=lambda r: r.price_decimal)

    @property
    def second_favourite(self) -> Runner | None:
        """Second shortest-priced active runner.

        Returns None if fewer than two priced active runners.
        """
        priced = sorted(
            [r for r in self.active_runners if r.price_decimal and r.price_decimal > 0],
            key=lambda r: r.price_decimal,
        )
        return priced[1] if len(priced) >= 2 else None


# ── Internal Helpers ──


def _build_race(event: dict) -> Race:
    """Convert a raw event dict into a Race dataclass."""
    time_str, track, _ = parse_event_name(event.get("eventName", ""))
    return Race(
        event_id=event["key"],
        event_name=event.get("eventName", ""),
        time=time_str,
        track=track,
        country=detect_country(event),
        status=event.get("eventStatus", ""),
    )


def _is_today(event: dict) -> bool:
    """Check if event is scheduled for today (UTC)."""
    dt_str = event.get("eventDateTime", "")
    if not dt_str:
        return event.get("isNext24HourEvent", False)
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.date() == datetime.now(timezone.utc).date()
    except (ValueError, TypeError):
        return event.get("isNext24HourEvent", False)


def _time_in_range(race_time: str, time_from: str | None, time_to: str | None) -> bool:
    """Check if a race time (HH:MM) falls within a time window.

    Both time_from and time_to are inclusive. If race_time is empty, returns False.
    """
    if not race_time:
        return False
    if time_from and race_time < time_from:
        return False
    if time_to and race_time > time_to:
        return False
    return True


def _parse_price_list(sel: dict) -> tuple[float | None, str]:
    """Extract decimal price and fractional string from a selection dict.

    Prefers LP (Live Price) over SP.
    """
    prices_obj = sel.get("prices", {})
    price_list = prices_obj.get("price", []) if isinstance(prices_obj, dict) else []
    if isinstance(price_list, dict):
        price_list = [price_list]

    decimal_price = None
    num_price = None
    den_price = None

    for p in price_list:
        if p.get("selectionPriceType", "").upper() == "LP":
            decimal_price = p.get("decimalPrice")
            num_price = p.get("numPrice")
            den_price = p.get("denPrice")
            break
    if decimal_price is None and price_list:
        p = price_list[0]
        decimal_price = p.get("decimalPrice")
        num_price = p.get("numPrice")
        den_price = p.get("denPrice")

    fractional = f"{num_price}/{den_price}" if num_price is not None and den_price else ""
    dec = float(decimal_price) if decimal_price else None
    return dec, fractional


# ── Query Functions ──


def get_races(
    date_filter: date | None = None,
    country: str | None = None,
    territory: str | None = None,
    meeting: str | None = None,
) -> list[Race]:
    """Fetch races with optional filters. No per-race API calls.

    Args:
        date_filter: If None, defaults to today's races only.
        country: Country code (UK, IRE, AU, US, FR, ZA, HK, JP, KR, NZ).
        territory: "uk_ire", "international", or "all".
        meeting: Track name (partial, case-insensitive match).

    Returns:
        List of Race objects (metadata only, no prices).
    """
    client = LadsClient()
    events = client.get_all_real_events()

    # Filter to today by default
    if date_filter is None:
        events = [e for e in events if _is_today(e)]
    else:
        for_date = date_filter if isinstance(date_filter, date) else date.today()
        filtered = []
        for e in events:
            dt_str = e.get("eventDateTime", "")
            if dt_str:
                try:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    if dt.date() == for_date:
                        filtered.append(e)
                except (ValueError, TypeError):
                    pass
        events = filtered

    # Filter by territory
    if territory:
        t = territory.lower()
        if t == "uk_ire":
            events = [e for e in events if detect_country(e) in ("UK", "IRE")]
        elif t == "international":
            events = [e for e in events if detect_country(e) not in ("UK", "IRE")]

    # Filter by country
    if country:
        country_upper = country.upper()
        events = [e for e in events if detect_country(e) == country_upper]

    # Filter by meeting/track name
    if meeting:
        meeting_lower = meeting.lower()
        filtered = []
        for e in events:
            _, track, _ = parse_event_name(e.get("eventName", ""))
            if meeting_lower in track.lower():
                filtered.append(e)
        events = filtered

    return [_build_race(e) for e in events]


def get_race_prices(event_id: int | str, delay: float = 1.5) -> RaceDetail:
    """Fetch full race detail with runners and prices.

    Uses in-memory cache to avoid repeated API calls for the same event.
    Only applies delay when making an actual API call.
    """
    eid = int(event_id)

    # Check cache first
    cached = _cache.get(eid)
    if cached is not None:
        return cached

    # Not cached — record miss and fetch from API
    _cache.misses += 1
    client = LadsClient()
    detail = client.get_event_detail(eid)

    event_name = detail.get("eventName", f"Event {eid}")
    time_str, track, _ = parse_event_name(event_name)
    country = detect_country(detail)

    # Find Win or Each Way market
    markets = detail.get("markets", {}).get("market", [])
    win_market = None
    for mkt in markets:
        name = mkt.get("marketName", "").lower()
        if name in ("win or each way", "win and each way", "win or e/w"):
            win_market = mkt
            break

    runners = []
    ew_places = None
    ew_fraction = None

    if win_market:
        ew_places = win_market.get("eachWayPlaces")
        ew_num = win_market.get("eachWayFactorNum")
        ew_den = win_market.get("eachWayFactorDen")
        ew_fraction = f"{ew_num}/{ew_den}" if ew_num is not None and ew_den else None

        selections = win_market.get("selections", {}).get("selection", [])

        for sel in selections:
            sel_name = sel.get("selectionName", "").strip()
            if is_placeholder_selection(sel_name):
                continue

            number = sel.get("runnerNumber", 0) or 0
            is_nr = is_non_runner(sel)
            status = (sel.get("selectionStatus", "") or "").strip()

            decimal_price, fractional = _parse_price_list(sel)

            runners.append(Runner(
                number=int(number),
                name=sel_name,
                price_decimal=decimal_price,
                price_fractional=fractional,
                is_nr=is_nr,
                status=status,
            ))

    race_detail = RaceDetail(
        event_id=eid,
        event_name=event_name,
        time=time_str,
        track=track,
        country=country,
        status=detail.get("eventStatus", ""),
        runners=runners,
        ew_places=ew_places,
        ew_fraction=ew_fraction,
    )

    _cache.put(eid, race_detail)
    return race_detail


def get_favourite(event_id: int | str) -> Runner | None:
    """Return the favourite (lowest-priced active runner) for a race."""
    detail = get_race_prices(event_id)
    return detail.favourite


def get_second_favourite(event_id: int | str) -> Runner | None:
    """Return the second favourite (second lowest-priced active runner).

    Returns None if fewer than two priced active runners.
    """
    detail = get_race_prices(event_id)
    return detail.second_favourite


def filter_races_by_favourite_price(
    date_filter: date | None = None,
    country: str | None = None,
    max_fav_price: float = 2.0,
    delay: float = 1.5,
) -> list[RaceDetail]:
    """Return races where the favourite is priced at or below max_fav_price.

    Uses cache — only sleeps before uncached API calls.
    """
    races = get_races(date_filter=date_filter, country=country)
    results = []

    for race in races:
        if not _cache.has(race.event_id):
            _time.sleep(delay)
        detail = get_race_prices(race.event_id)
        fav = detail.favourite
        if fav and fav.price_decimal and fav.price_decimal <= max_fav_price:
            results.append(detail)

    return results


def filter_races_by_runner_count(
    date_filter: date | None = None,
    country: str | None = None,
    min_runners: int = 8,
    delay: float = 1.5,
) -> list[RaceDetail]:
    """Return races with at least min_runners active runners.

    Uses cache — only sleeps before uncached API calls.
    """
    races = get_races(date_filter=date_filter, country=country)
    results = []

    for race in races:
        if not _cache.has(race.event_id):
            _time.sleep(delay)
        detail = get_race_prices(race.event_id)
        if detail.runner_count >= min_runners:
            results.append(detail)

    return results


# ── Detail filter keys — presence of any of these triggers Stage B ──
_DETAIL_FILTER_KEYS = {"min_runners", "max_fav_price", "min_second_fav_price"}


def query_races(
    date_filter: date | None = None,
    filters: dict | None = None,
    delay: float = 1.5,
    verbose: bool = True,
) -> Union[list[Race], list[RaceDetail]]:
    """General-purpose query with composable two-stage filters.

    Stage A (metadata — fast, single API call):
        country, territory, meeting, time_from, time_to

    Stage B (detail/price — per-race API call, only if needed):
        min_runners, max_fav_price, min_second_fav_price

    Returns:
        list[Race] if only metadata filters are used (no detail calls).
        list[RaceDetail] if any detail/price filters are present.

    Args:
        date_filter: Date to query. None = today.
        filters: Dict of filter criteria (see keys above).
        delay: Seconds between uncached detail API calls.
        verbose: If True, prints a QueryStats summary after the query.
    """
    if filters is None:
        filters = {}

    stats = QueryStats()

    # ── Stage A: Metadata filters (single API call) ──

    country = filters.get("country")
    territory = filters.get("territory")
    meeting = filters.get("meeting")
    time_from = filters.get("time_from")
    time_to = filters.get("time_to")

    # This makes one HTTP call to get all events, then filters in-memory
    client = LadsClient()
    all_events = client.get_all_real_events()
    stats.total_events_fetched = len(all_events)

    # Date filter
    if date_filter is None:
        events = [e for e in all_events if _is_today(e)]
    else:
        for_date = date_filter if isinstance(date_filter, date) else date.today()
        events = []
        for e in all_events:
            dt_str = e.get("eventDateTime", "")
            if dt_str:
                try:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    if dt.date() == for_date:
                        events.append(e)
                except (ValueError, TypeError):
                    pass

    # Territory filter
    if territory:
        t = territory.lower()
        if t == "uk_ire":
            events = [e for e in events if detect_country(e) in ("UK", "IRE")]
        elif t == "international":
            events = [e for e in events if detect_country(e) not in ("UK", "IRE")]

    # Country filter
    if country:
        country_upper = country.upper()
        events = [e for e in events if detect_country(e) == country_upper]

    # Meeting filter
    if meeting:
        meeting_lower = meeting.lower()
        events = [e for e in events
                  if meeting_lower in parse_event_name(e.get("eventName", ""))[1].lower()]

    # Build Race objects
    races = [_build_race(e) for e in events]

    # Time window filter (still metadata — no API call)
    if time_from or time_to:
        races = [r for r in races if _time_in_range(r.time, time_from, time_to)]

    stats.after_metadata_filters = len(races)

    # ── Check if Stage B is needed ──

    needs_detail = bool(filters.keys() & _DETAIL_FILTER_KEYS)
    stats.needs_detail = needs_detail

    if not needs_detail:
        # Metadata-only query — return Race objects, no detail calls
        stats.results_returned = len(races)
        if verbose:
            stats.print_summary()
        return races

    # ── Stage B: Detail/price filters (per-race API calls) ──

    min_runners = filters.get("min_runners")
    max_fav_price = filters.get("max_fav_price")
    min_second_fav_price = filters.get("min_second_fav_price")

    results: list[RaceDetail] = []
    cache_hits_before = _cache.hits

    for race in races:
        if not _cache.has(race.event_id):
            _time.sleep(delay)
            stats.detail_calls_made += 1
        detail = get_race_prices(race.event_id)

        # Apply price-based filters
        if min_runners is not None and detail.runner_count < min_runners:
            continue

        if max_fav_price is not None:
            fav = detail.favourite
            if not fav or not fav.price_decimal or fav.price_decimal > max_fav_price:
                continue

        if min_second_fav_price is not None:
            sec = detail.second_favourite
            if not sec or not sec.price_decimal or sec.price_decimal < min_second_fav_price:
                continue

        results.append(detail)

    stats.cache_hits = _cache.hits - cache_hits_before
    stats.results_returned = len(results)

    if verbose:
        stats.print_summary()

    return results
