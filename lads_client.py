"""Ladbrokes Sportsbook API v4 — Core Client for UK/Ire Racing.

Usage:
    from lads_client import LadsClient

    client = LadsClient()
    meetings = client.get_uk_ire_meetings()
    for track, events in meetings.items():
        print(f"{track}: {len(events)} races")
        for ev in events:
            runners = client.get_runners(ev["key"])
"""

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://sb-api.ladbrokes.com"
API_KEY = os.getenv("LADS_API_KEY", "")
LOCALE = "en-GB"
TIMEOUT = 15

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Origin": "https://sports.ladbrokes.com",
    "Referer": "https://sports.ladbrokes.com/",
}

# Track-based country detection (no dedicated flag code for these)
_TRACK_COUNTRY_MAP = {
    "HK": ["happy valley", "sha tin"],
    "JP": ["funabashi", "mombetsu", "nagoya", "sonoda"],
    "KR": ["busan", "seoul"],
    "NZ": ["ellerslie", "trentham", "cambridge", "te aroha"],
    "SA_LATAM": ["san isidro", "gavea", "maronas", "club hipico", "hipo chile",
                 "concepcion", "valparaiso", "las piedras"],
}

# Flag codes that map directly to a country
_FLAG_COUNTRY_MAP = {
    "UK": "UK",
    "IRE": "IRE",
    "IE": "IRE",
    "AU": "AU",
    "US": "US",
    "FR": "FR",
    "ZA": "ZA",
}


def _params(**extra):
    p = {"api-key": API_KEY, "locale": LOCALE}
    p.update(extra)
    return p


def parse_event_name(name: str) -> tuple[str, str, bool]:
    """Parse 'HH:MM Track' → (time_str, track, is_overnight)."""
    m = re.match(r"(\d{1,2}:\d{2})\s+(\+1d\s+)?(.*)", name.strip())
    if m:
        return m.group(1), m.group(3).strip(), bool(m.group(2))
    return "", name.strip(), False


def is_virtual_event(event: dict) -> bool:
    """Return True if the event is virtual racing."""
    return "VR" in event.get("typeFlagCode", "")


# Patterns for unnamed favourite/second favourite placeholder selections
_PLACEHOLDER_PATTERNS = [
    "unnamed favourite",
    "unnamed 2nd favourite",
    "unnamed second favourite",
    "unnamed fav",
    "unnamed 2nd fav",
    "unnamed second fav",
]


def is_placeholder_selection(name: str) -> bool:
    """Return True if the selection name is an unnamed favourite/second favourite placeholder.

    Case-insensitive. These selections should be excluded from:
      - favourite detection
      - second favourite detection
      - active priced runner count
      - results display
    """
    lower = name.strip().lower()
    return any(pattern in lower for pattern in _PLACEHOLDER_PATTERNS)


# Patterns that indicate a non-runner when found in the runner name (case-insensitive)
_NR_NAME_PATTERNS = [
    " n/r",
    "n/r",
    "(nr)",
    " nr ",
    " non runner",
    "non-runner",
    " non-runner",
    "scratched",
]


def is_non_runner(sel: dict | None = None, *, name: str = "", result_type: str = "", status: str = "") -> bool:
    """Return True if the selection/runner is a non-runner.

    Checks both structured API fields and name-based markers.

    Can be called two ways:
        is_non_runner(selection_dict)           — pass the raw API selection dict
        is_non_runner(name=..., result_type=..., status=...)  — pass individual fields

    Returns True if any of:
        - resultType is "NR" or "SCRATCHED"
        - selectionStatus / status is "Void"
        - Runner name contains a non-runner marker (e.g. "N/R", "(NR)", "NON RUNNER")
    """
    if sel is not None:
        name = (sel.get("selectionName", "") or "").strip()
        result_type = (sel.get("resultType", "") or "").strip()
        status = (sel.get("selectionStatus", "") or "").strip()

    # Structured field checks
    rt_upper = result_type.upper()
    if rt_upper in ("NR", "SCRATCHED"):
        return True
    if status.lower() == "void":
        return True

    # Name-based checks (case-insensitive)
    name_lower = name.lower()
    for pattern in _NR_NAME_PATTERNS:
        if pattern in name_lower:
            return True

    return False


def detect_country(event: dict) -> str:
    """Detect canonical country code from an event's flags and track name.

    Returns one of: UK, IRE, AU, US, FR, ZA, HK, JP, KR, NZ, SA_LATAM, INT, or UNKNOWN.
    """
    flags = event.get("typeFlagCode", "")
    event_name = event.get("eventName", "").lower()

    # Check flag-based countries (order matters — UK/IRE first for specificity)
    for flag, country in _FLAG_COUNTRY_MAP.items():
        if flag in flags:
            # NZ tracks often carry AU flag — check track name first
            if flag == "AU":
                for track in _TRACK_COUNTRY_MAP.get("NZ", []):
                    if track in event_name:
                        return "NZ"
            return country

    # Check track-based countries
    for country, tracks in _TRACK_COUNTRY_MAP.items():
        for track in tracks:
            if track in event_name:
                return country

    # Fallback: has INT flag but no specific match
    if "INT" in flags:
        return "INT"

    return "UNKNOWN"


# Market-name groups (all lower-case for case-insensitive matching).
_WIN_MARKET_NAMES = ("win or each way", "win and each way", "win or e/w")
_ANTEPOST_MARKET_NAMES = ("ante post", "ante-post", "antepost", "outright", "outright betting")


def _normalise_markets(detail: dict) -> list[dict]:
    """Return the list of market dicts from an event detail, coping with the
    API sometimes giving a single dict instead of a list."""
    markets = detail.get("markets", {}).get("market", [])
    if isinstance(markets, dict):
        markets = [markets]
    return markets


def _find_market(detail: dict, names: tuple[str, ...]) -> dict | None:
    """Find the first market whose (lower-cased) name matches one of `names`."""
    for mkt in _normalise_markets(detail):
        if mkt.get("marketName", "").strip().lower() in names:
            return mkt
    return None


def _extract_price(sel: dict) -> tuple[float | None, str]:
    """Extract (decimal_price, fractional_str) from a selection, preferring LP."""
    prices_obj = sel.get("prices", {})
    price_list = prices_obj.get("price", []) if isinstance(prices_obj, dict) else []
    if isinstance(price_list, dict):
        price_list = [price_list]

    decimal_price = None
    num_price = None
    den_price = None

    # Prefer LP (Live Price)
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


def _parse_selections(market: dict) -> list[dict]:
    """Convert a market's selections into the standard runner dict list.

    Skips placeholder (unnamed favourite) selections. Shared by win and
    ante-post market parsing so the two stay consistent.
    """
    selections = market.get("selections", {}).get("selection", [])
    if isinstance(selections, dict):
        selections = [selections]

    runners = []
    for sel in selections:
        name = (sel.get("selectionName", "") or "").strip()
        if is_placeholder_selection(name):
            continue

        number = sel.get("runnerNumber", 0) or 0
        is_nr = is_non_runner(sel)
        status = (sel.get("selectionStatus", "") or "").strip()
        decimal_price, fractional = _extract_price(sel)

        runners.append({
            "number": int(number),
            "name": name,
            "price_decimal": decimal_price,
            "price_fractional": fractional,
            "is_nr": is_nr,
            "status": status,
        })
    return runners


class LadsClient:
    """Client for the Ladbrokes Sportsbook API."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or API_KEY
        if not self.api_key or self.api_key == "YOUR_KEY_HERE":
            raise ValueError("No API key set. Add LADS_API_KEY to .env")

    def _get(self, path: str, **params):
        url = f"{BASE_URL}{path}"
        all_params = {"api-key": self.api_key, "locale": LOCALE, **params}
        # Ladbrokes sits behind Cloudflare, which sometimes returns a transient
        # 403/429 for requests from datacentre IPs (e.g. GitHub Actions runners).
        # Retry a few times with a growing pause before giving up, so an
        # occasional block doesn't fail the whole run.
        attempts = 4
        last_exc = None
        for i in range(attempts):
            try:
                resp = requests.get(url, params=all_params, headers=HEADERS,
                                    timeout=TIMEOUT)
                if resp.status_code in (403, 429, 503) and i < attempts - 1:
                    time.sleep(3 * (i + 1))   # 3s, 6s, 9s backoff
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_exc = exc
                if i < attempts - 1:
                    time.sleep(3 * (i + 1))
                    continue
                raise
        if last_exc:
            raise last_exc

    # ── Events ──

    def get_all_racing_events(self) -> list[dict]:
        """Fetch all horse racing events (category 21)."""
        data = self._get("/v4/sportsbook-api/categories/21/events")
        return data.get("events", {}).get("event", [])

    def get_all_real_events(self) -> list[dict]:
        """Fetch all horse racing events excluding virtual races."""
        return [e for e in self.get_all_racing_events() if not is_virtual_event(e)]

    def search_events(self, name_substring: str) -> list[dict]:
        """Return real events whose eventName contains `name_substring`.

        Case-insensitive. Useful for locating feature / ante-post races (e.g.
        "balmoral", "cesarewitch", "derby") whose event IDs aren't otherwise
        obvious. Does not fetch per-event detail, so it's a single API call.
        """
        needle = name_substring.strip().lower()
        return [
            e for e in self.get_all_real_events()
            if needle in e.get("eventName", "").lower()
        ]

    # Name hints that suggest a feature / ante-post race. Used to prefilter which
    # events are worth a per-event detail call when discovering ante-post markets.
    _ANTEPOST_NAME_HINTS = (
        "handicap", "derby", "oaks", "guineas", "st leger", "leger",
        "gold cup", "champion", "stakes", "cup", "arc", "cesarewitch",
        "cambridgeshire", "lincoln", "national", "hurdle", "chase",
        "trophy", "classic", "futurity",
    )

    def discover_antepost_events(
        self,
        delay: float = 0.3,
        name_hints: tuple[str, ...] | None = None,
        max_workers: int = 8,
    ) -> list[dict]:
        """Find events that expose an "Ante Post" market.

        Confirming an ante-post market requires a per-event detail call, which is
        expensive across the full event list. To keep it tractable we first
        prefilter to events whose name looks like a feature race (via name hints),
        then fetch detail only for those and keep the ones that actually carry an
        ante-post market.

        Returns a list of dicts: {"event": <raw event>, "runners": [...]}.

        `delay` is retained for API compatibility but ignored when max_workers > 1
        (the thread pool provides the concurrency instead of paced sleeps).
        """
        hints = name_hints or self._ANTEPOST_NAME_HINTS
        candidates = [
            e for e in self.get_all_real_events()
            if any(h in e.get("eventName", "").lower() for h in hints)
        ]
        by_id = {e.get("key"): e for e in candidates}

        results = []

        def _detail(eid):
            return eid, self.get_event_detail(eid)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_detail, eid) for eid in by_id]
            for fut in as_completed(futures):
                try:
                    eid, detail = fut.result()
                except Exception:
                    continue
                market = _find_market(detail, _ANTEPOST_MARKET_NAMES)
                if market is not None:
                    results.append({"event": by_id[eid], "runners": _parse_selections(market)})
        return results

    def get_events_by_country(self, country: str) -> list[dict]:
        """Fetch real racing events filtered to a specific country code.

        Supported codes: UK, IRE, AU, US, FR, ZA, HK, JP, KR, NZ, SA_LATAM, INT.
        """
        all_events = self.get_all_real_events()
        country_upper = country.upper()
        return [e for e in all_events if detect_country(e) == country_upper]

    def get_uk_ire_events(self) -> list[dict]:
        """Filter to UK and Irish racing events only."""
        all_events = self.get_all_racing_events()
        return [e for e in all_events if self._is_uk_ire(e)]

    def get_uk_ire_meetings(self) -> dict[str, list[dict]]:
        """Group UK/Ire events by track. Returns {track_name: [events]}."""
        from collections import defaultdict
        events = self.get_uk_ire_events()
        meetings = defaultdict(list)
        for ev in events:
            _, track, _ = parse_event_name(ev.get("eventName", ""))
            if track:
                meetings[track].append(ev)
        # Sort races within each meeting by time
        for track in meetings:
            meetings[track].sort(key=lambda e: e.get("eventName", ""))
        return dict(meetings)

    @staticmethod
    def _is_uk_ire(event: dict) -> bool:
        # Reuse detect_country so UK/Ire detection stays consistent with the
        # canonical flag map. Real Irish races carry the "IE" flag (not "IRE"),
        # which detect_country already maps to "IRE"; a naive "IRE" substring
        # check on the raw flags misses them.
        return detect_country(event) in ("UK", "IRE")

    # ── Runners & Prices ──

    def get_event_detail(self, event_id: int | str) -> dict:
        """Fetch full event detail with markets and selections."""
        data = self._get(f"/v4/sportsbook-api/events/{event_id}", expand="selection")
        return data.get("event", data)

    def get_runners(self, event_id: int | str) -> list[dict]:
        """Fetch runners from the Win or Each Way market.

        Returns list of:
        {
            "number": int,
            "name": str,
            "price_decimal": float | None,
            "price_fractional": str,
            "is_nr": bool,
            "status": str,
        }
        """
        detail = self.get_event_detail(event_id)
        win_market = _find_market(detail, _WIN_MARKET_NAMES)
        if not win_market:
            return []
        return _parse_selections(win_market)

    def get_antepost_runners(self, event_id: int | str) -> list[dict]:
        """Fetch runners from an ante-post / outright market.

        Feature races (e.g. the Balmoral Handicap, Cesarewitch, Derby) expose an
        "Ante Post" market rather than "Win or Each Way". The runner dict shape
        matches get_runners. Falls back to the win market if no ante-post market
        is present, and returns [] if neither exists.
        """
        detail = self.get_event_detail(event_id)
        market = _find_market(detail, _ANTEPOST_MARKET_NAMES)
        if not market:
            # Some events may still use a win-style market — fall back to it.
            market = _find_market(detail, _WIN_MARKET_NAMES)
        if not market:
            return []
        return _parse_selections(market)

    def find_runner_price(
        self, event_id: int | str, name_substring: str, *, antepost: bool = True
    ) -> dict | None:
        """Find a single runner in an event by (case-insensitive) name substring.

        By default searches the ante-post market (with win-market fallback); set
        antepost=False to search the win/each-way market only. Returns the runner
        dict, or None if no active priced runner matches.
        """
        runners = (
            self.get_antepost_runners(event_id)
            if antepost
            else self.get_runners(event_id)
        )
        needle = name_substring.strip().lower()
        for r in runners:
            if needle in r["name"].lower():
                return r
        return None

    # ── Concurrent bulk fetching ──

    def get_runners_bulk(
        self,
        event_ids: list,
        *,
        antepost: bool = False,
        max_workers: int = 8,
        progress=None,
    ) -> dict:
        """Fetch runners for many events concurrently.

        Uses a bounded thread pool so we parallelise the network waits without
        hammering the API (Cloudflare may throttle rapid bursts). Each event is
        fetched independently; failures are skipped rather than aborting the run.

        Args:
            event_ids: iterable of event ids to fetch.
            antepost: if True use the ante-post market, else the win market.
            max_workers: max concurrent requests (keep modest, default 8).
            progress: optional callable(done:int, total:int) for progress updates.

        Returns:
            {event_id: [runner dicts]} — only for events that fetched successfully.
        """
        ids = list(event_ids)
        total = len(ids)
        fetch = self.get_antepost_runners if antepost else self.get_runners
        results: dict = {}

        def _one(eid):
            return eid, fetch(eid)

        done = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_one, eid): eid for eid in ids}
            for fut in as_completed(futures):
                done += 1
                try:
                    eid, runners = fut.result()
                    results[eid] = runners
                except Exception:
                    # Skip failed events (network error, unexpected shape, etc.)
                    pass
                if progress is not None:
                    progress(done, total)
        return results

    # ── Utilities ──

    def get_meeting_with_runners(self, track: str, delay: float = 1.5) -> list[dict]:
        """Fetch all races + runners for a specific track.

        Returns list of race dicts with runners attached.
        """
        meetings = self.get_uk_ire_meetings()
        events = meetings.get(track, [])
        results = []
        for ev in events:
            time_str, _, _ = parse_event_name(ev.get("eventName", ""))
            runners = self.get_runners(ev["key"])
            results.append({
                "event_id": ev["key"],
                "event_name": ev["eventName"],
                "time": time_str,
                "status": ev.get("eventStatus"),
                "runners": runners,
            })
            time.sleep(delay)
        return results
