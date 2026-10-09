"""Scan today's cards for same-horse doubles and build a static results page
(index.html) for GitHub Pages.

A "same-horse double" here is a horse that (a) runs in a race on today's card
AND (b) also appears in a future ante-post feature race (e.g. the Cesarewitch,
a big handicap, a Classic). The "win today AND win the feature" double is priced
with the independent model, and shown with 25% / 50% / 75% margin quotes.

This is the hosted version of batch_same_horse_doubles.py. It runs on a schedule
via GitHub Actions (09:00 and 12:00 UK), using the Ladbrokes API key stored as a
GitHub Secret in the environment variable LADS_API_KEY. It never prints the key.

Usage (local test):
    set LADS_API_KEY=...        (Windows cmd) / $env:LADS_API_KEY (PowerShell)
    python build_doubles_page.py
    python build_doubles_page.py --countries UK,IRE --correlation 0.1
"""

import argparse
import html
from datetime import datetime
from zoneinfo import ZoneInfo

import console_utf8  # noqa: F401  (force UTF-8 console output on Windows)
from lads_client import LadsClient
from racing_query import get_races
from contingency_pricer import price_independent_double, apply_margin

UK_TZ = ZoneInfo("Europe/London")
DEFAULT_COUNTRIES = ("UK", "IRE", "FR", "US", "AU")
MARGIN_TIERS = (25.0, 50.0, 75.0)


def _log(msg):
    print(msg, flush=True)


def _norm(name: str) -> str:
    return " ".join(name.strip().lower().split())


def gather_todays_runners(client, wanted, countries, workers):
    """{normalised_name: {name, price, race, time}} for today's priced runners
    whose name is in `wanted` (horses that also appear ante-post)."""
    races = get_races()
    country_set = {c.upper() for c in countries}
    races = [r for r in races if r.country in country_set]
    _log(f"Today's races to scan ({'/'.join(countries)}): {len(races)}")

    race_by_id = {r.event_id: r for r in races}
    runners_by_id = client.get_runners_bulk(
        list(race_by_id.keys()), antepost=False, max_workers=workers)

    todays = {}
    for eid, runners in runners_by_id.items():
        race = race_by_id[eid]
        for r in runners:
            if r["is_nr"] or not r["price_decimal"] or r["price_decimal"] <= 0:
                continue
            key = _norm(r["name"])
            if key not in wanted:
                continue
            entry = {"name": r["name"], "price": r["price_decimal"],
                     "race": race.track, "time": race.time}
            if key not in todays or entry["price"] < todays[key]["price"]:
                todays[key] = entry
    _log(f"Horses running today that also run ante-post: {len(todays)}")
    return todays


def scan(countries, correlation, workers):
    """Return a list of priced same-horse-double rows, sorted shortest fair first."""
    client = LadsClient()

    _log(f"Discovering ante-post feature races ({workers} workers)...")
    antepost = client.discover_antepost_events(max_workers=workers)
    _log(f"Ante-post markets found: {len(antepost)}")

    wanted = set()
    for ap in antepost:
        for r in ap["runners"]:
            if not r["is_nr"] and r["price_decimal"] and r["price_decimal"] > 0:
                wanted.add(_norm(r["name"]))
    _log(f"Unique ante-post horses to look for today: {len(wanted)}")

    todays = gather_todays_runners(client, wanted, countries, workers)

    rows = []
    for ap in antepost:
        ap_name = ap["event"].get("eventName", "")
        for r in ap["runners"]:
            if r["is_nr"] or not r["price_decimal"] or r["price_decimal"] <= 0:
                continue
            today = todays.get(_norm(r["name"]))
            if today is None:
                continue
            result = price_independent_double(
                price_a=today["price"], price_b=r["price_decimal"],
                margin_pct=0.0, correlation=correlation,
                labels=(today["name"], r["name"]))
            fair = result.fair_price
            impossible = (fair == float("inf"))
            row = {
                "horse": today["name"],
                "today_time": today["time"],          # "HH:MM" — used for sorting
                "today_race": f"{today['time']} {today['race']}",
                "today_price": round(today["price"], 2),
                "antepost_race": ap_name,
                "antepost_price": round(r["price_decimal"], 2),
                "fair_prob_pct": round(result.fair_prob * 100, 2),
                "fair_price": None if impossible else round(fair, 2),
            }
            for tier in MARGIN_TIERS:
                row[f"quote_{int(tier)}"] = (None if impossible
                                             else round(apply_margin(fair, tier), 2))
            rows.append(row)

    # Sort chronologically by today's race time (earliest first); the horse
    # name breaks ties so two runners in the same race stay grouped.
    rows.sort(key=lambda x: (x["today_time"], x["horse"].lower()))
    return rows


def build_html(rows, countries, correlation):
    esc = html.escape
    generated = datetime.now(UK_TZ).strftime("%A %d %B %Y, %H:%M %Z")

    def cell(v):
        return "&ndash;" if v is None else esc(str(v))

    body_rows = ""
    for r in rows:
        body_rows += (
            "<tr>"
            f"<td class='horse'>{esc(r['horse'])}</td>"
            f"<td>{esc(r['today_race'])}</td>"
            f"<td class='num'>{cell(r['today_price'])}</td>"
            f"<td class='ap'>{esc(r['antepost_race'])}</td>"
            f"<td class='num'>{cell(r['antepost_price'])}</td>"
            f"<td class='num fair'>{cell(r['fair_price'])}</td>"
            f"<td class='num'>{cell(r['quote_25'])}</td>"
            f"<td class='num'>{cell(r['quote_50'])}</td>"
            f"<td class='num'>{cell(r['quote_75'])}</td>"
            "</tr>\n"
        )

    if rows:
        table = (
            f"<p class='count'>{len(rows)} same-horse double(s) found "
            f"&middot; in race-time order for today</p>"
            "<table>"
            "<colgroup>"
            "<col class='c-horse'><col class='c-trace'><col class='c-tp'>"
            "<col class='c-ap'><col class='c-app'><col class='c-fair'>"
            "<col class='c-q'><col class='c-q'><col class='c-q'>"
            "</colgroup>"
            "<thead><tr>"
            "<th>Horse</th><th>Today's race</th><th>Today</th>"
            "<th>Ante-post race</th><th>A-P</th>"
            "<th>True price</th><th>25%</th><th>50%</th><th>75%</th>"
            "</tr></thead>"
            f"<tbody>{body_rows}</tbody></table>"
        )
    else:
        table = ("<p class='none'>No same-horse doubles found right now. This is "
                 "normal when few ante-post horses are also declared on today's "
                 "card.</p>")

    corr_note = ("" if correlation == 0
                 else f" &middot; correlation {esc(str(correlation))}")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Same-Horse Doubles</title>
<style>
  :root {{ --blue:#4ec3ff; --ink:#1b2733; --muted:#64748b; --line:#e2e8f0; --bg:#f1f5f9; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
         Helvetica,Arial,sans-serif; background:var(--bg); color:var(--ink); }}
  header {{ background:var(--blue); color:#063047; padding:18px 20px; }}
  header h1 {{ margin:0; font-size:22px; }}
  header p {{ margin:4px 0 0; font-size:13px; color:#0a3f5c; }}
  main {{ padding:16px 20px 40px; }}
  .count {{ font-size:15px; font-weight:700; margin:6px 0 14px; }}
  .none {{ font-size:16px; color:var(--muted); background:#fff; border:1px solid var(--line);
          border-radius:10px; padding:18px; }}
  table {{ width:100%; border-collapse:collapse; background:#fff;
          border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
  th,td {{ text-align:left; padding:9px 10px; font-size:13.5px;
          border-bottom:1px solid var(--line); vertical-align:top; }}
  th {{ background:#f8fafc; color:var(--muted); font-weight:600; white-space:nowrap; }}
  tr:last-child td {{ border-bottom:none; }}
  td.horse {{ font-weight:700; }}
  td.ap {{ color:#334155; }}
  .num {{ text-align:left; font-variant-numeric:tabular-nums; white-space:nowrap; }}
  .fair {{ font-weight:700; }}
  footer {{ padding:0 20px 30px; font-size:12px; color:var(--muted); }}
</style>
</head>
<body>
<header>
  <h1>Same-Horse Doubles</h1>
  <p>Horses running today that also run in a future ante-post race &middot;
     "win today AND win the feature" priced with the independent model</p>
</header>
<main>
  {table}
</main>
<footer>
  Updated {esc(generated)} &middot; scanning {esc('/'.join(countries))}{corr_note} &middot;
  cards &amp; prices from the Ladbrokes feed. True price is the fair (0% margin)
  double; 25/50/75% are margin-added quotes. Matching is by exact horse name.
</footer>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="Build the same-horse-doubles results page")
    ap.add_argument("--countries", default=",".join(DEFAULT_COUNTRIES))
    ap.add_argument("--correlation", type=float, default=0.0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--output", default="index.html")
    args = ap.parse_args()

    countries = tuple(c.strip().upper() for c in args.countries.split(",") if c.strip())
    rows = scan(countries, args.correlation, args.workers)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(build_html(rows, countries, args.correlation))
    _log(f"Wrote {args.output} with {len(rows)} double(s).")


if __name__ == "__main__":
    main()
