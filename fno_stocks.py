# -*- coding: utf-8 -*-
"""
Fetch overnight India market & macro news from public RSS feeds and write
data/news.json for the dashboard.

Run locally:   python scripts/fetch_news.py
Runs automatically via .github/workflows/update-news.yml

Design notes
------------
* Each feed is fetched with a browser-like User-Agent (some Indian news sites
  return 403 to the default Python agent).
* A failing feed is skipped, never fatal — one dead feed must not break the run.
* "Overnight" = everything published since the most recent NSE close (3:30 PM IST),
  rolling back over weekends. Holidays are not special-cased; items are timestamped
  anyway, so nothing is hidden.
* Stories that appear in more than one outlet are merged into one card that lists
  all the outlets that carried it (a rough corroboration signal).
"""

import datetime as dt
import html
import json
import re
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser

from fno_stocks import FNO_STOCKS

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")
OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "news.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Feeds: (display source, category hint, url)
# category hint is just a default lane; real tagging happens per-headline below.
# Add or remove lines freely.
# ---------------------------------------------------------------------------
FEEDS = [
    # Economic Times
    ("Economic Times — Markets",  "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Economic Times — Stocks",   "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"),
    ("Economic Times — Economy",  "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms"),
    ("Economic Times — Top News", "https://economictimes.indiatimes.com/rssfeedstopstories.cms"),
    # Business Standard
    ("Business Standard — Markets", "https://www.business-standard.com/rss/markets-106.rss"),
    ("Business Standard — Economy", "https://www.business-standard.com/rss/economy-102.rss"),
    ("Business Standard — Latest",  "https://www.business-standard.com/rss/latest.rss"),
    # Mint
    ("Mint — Markets",   "https://www.livemint.com/rss/markets"),
    ("Mint — Economy",   "https://www.livemint.com/rss/economy"),
    ("Mint — Companies", "https://www.livemint.com/rss/companies"),
    # Times of India
    ("Times of India — Business", "https://timesofindia.indiatimes.com/rssfeeds/1898055.cms"),
    # Moneycontrol
    ("Moneycontrol — Business",     "https://www.moneycontrol.com/rss/business.xml"),
    ("Moneycontrol — Latest News",  "https://www.moneycontrol.com/rss/latestnews.xml"),
    ("Moneycontrol — Market Reports","https://www.moneycontrol.com/rss/marketreports.xml"),
    # Financial Express
    ("Financial Express — Market",  "https://www.financialexpress.com/market/feed/"),
    ("Financial Express — Economy", "https://www.financialexpress.com/economy/feed/"),
    # Business Today
    ("Business Today — Markets", "https://www.businesstoday.in/rss/markets"),
]

# ---------------------------------------------------------------------------
# Macro keywords -> tagged as "macro". Lowercase; matched as whole words.
# ---------------------------------------------------------------------------
MACRO_TERMS = [
    "rbi", "reserve bank", "repo rate", "monetary policy", "mpc", "shaktikanta",
    "inflation", "cpi", "wpi", "retail inflation", "core inflation",
    "gdp", "gva", "fiscal deficit", "current account", "trade deficit",
    "balance of payments", "forex reserves", "fx reserves",
    "rupee", "usd/inr", "usd-inr", "dollar index", "currency",
    "crude", "brent", "wti", "oil price",
    "fii", "fpi", "dii", "foreign investors", "foreign portfolio",
    "bond yield", "g-sec", "gsec", "10-year yield", "government bond",
    "budget", "gst", "gst collection", "direct tax", "tax collection",
    "iip", "factory output", "pmi", "manufacturing pmi", "services pmi",
    "unemployment", "jobs data", "wage",
    "fed", "fomc", "federal reserve", "powell", "rate cut", "rate hike",
    "us cpi", "us inflation", "treasury yield", "ecb", "bank of japan", "boj",
    "tariff", "tariffs", "sebi", "nifty", "sensex", "bank nifty",
    "monsoon", "imd", "rainfall", "msp", "crop",
]

WORD = re.compile  # alias


def now_ist():
    return dt.datetime.now(IST)


def last_market_close(ref=None):
    """Most recent NSE close (15:30 IST), rolled back over weekends."""
    ref = ref or now_ist()
    close = ref.replace(hour=15, minute=30, second=0, microsecond=0)
    if ref < close:
        close -= dt.timedelta(days=1)
    while close.weekday() >= 5:  # Sat=5, Sun=6 -> roll back to Friday
        close -= dt.timedelta(days=1)
        close = close.replace(hour=15, minute=30, second=0, microsecond=0)
    return close


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_feed(url):
    """Return a parsed feed using a browser UA, or None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read()
        d = feedparser.parse(raw)
        if d.entries:
            return d
        # Fallback: let feedparser fetch directly (handles some redirects)
        d = feedparser.parse(url, request_headers={"User-Agent": USER_AGENT})
        return d if d.entries else None
    except Exception as e:  # noqa: BLE001
        print(f"  ! {url} -> {e}", file=sys.stderr)
        return None


def entry_time_utc(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return dt.datetime.fromtimestamp(time.mktime(t), tz=UTC)
    return None


def build_matcher(aliases):
    parts = [re.escape(a.strip()) for a in aliases if a.strip()]
    return re.compile(r"(?<![\w])(?:%s)(?![\w])" % "|".join(parts), re.IGNORECASE)


MACRO_RE = build_matcher(MACRO_TERMS)
STOCK_RE = {tk: build_matcher(info["aliases"]) for tk, info in FNO_STOCKS.items()}


def categorize(text):
    cats, tickers = set(), []
    for tk, rx in STOCK_RE.items():
        if rx.search(text):
            tickers.append({"ticker": tk, "name": FNO_STOCKS[tk]["name"]})
            cats.add("fno")
    if MACRO_RE.search(text):
        cats.add("macro")
    if not cats:
        cats.add("markets")
    return sorted(cats), tickers


def norm_title(title):
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def main():
    close = last_market_close()
    print(f"Coverage window: since {close:%a %d %b %Y %H:%M} IST")

    sources_status, by_title = [], {}

    for source, url in FEEDS:
        d = fetch_feed(url)
        if not d:
            sources_status.append({"name": source, "ok": False, "count": 0})
            print(f"FAIL  {source}")
            continue
        kept = 0
        for e in d.entries:
            title = strip_html(e.get("title", ""))
            link = e.get("link", "")
            if not title or not link:
                continue
            t_utc = entry_time_utc(e)
            summary = strip_html(e.get("summary", e.get("description", "")))[:320]
            key = norm_title(title)
            rec = by_title.get(key)
            if rec is None:
                t_ist = t_utc.astimezone(IST) if t_utc else None
                cats, tickers = categorize(f"{title}. {summary}")
                rec = {
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "source": source,
                    "sources": [source],
                    "published_utc": t_utc.isoformat() if t_utc else None,
                    "published_ist": f"{t_ist:%Y-%m-%d %H:%M} IST" if t_ist else None,
                    "_sort": t_utc or dt.datetime(1970, 1, 1, tzinfo=UTC),
                    "categories": cats,
                    "tickers": tickers,
                    "in_window": bool(t_utc and t_utc.astimezone(IST) >= close),
                }
                by_title[key] = rec
                kept += 1
            else:
                if source not in rec["sources"]:
                    rec["sources"].append(source)
                if not rec["summary"] and summary:
                    rec["summary"] = summary
        sources_status.append({"name": source, "ok": True, "count": kept})
        print(f"OK    {source}: {kept} new")

    items = sorted(by_title.values(), key=lambda r: r["_sort"], reverse=True)
    for r in items:
        r.pop("_sort", None)

    # Top F&O stocks by number of in-window headlines
    counter = defaultdict(int)
    names = {}
    for r in items:
        if not r["in_window"]:
            continue
        for t in r["tickers"]:
            counter[t["ticker"]] += 1
            names[t["ticker"]] = t["name"]
    top_stocks = sorted(
        ({"ticker": tk, "name": names[tk], "count": c} for tk, c in counter.items()),
        key=lambda x: (-x["count"], x["ticker"]),
    )[:15]

    in_window = [r for r in items if r["in_window"]]
    payload = {
        "generated_at_utc": dt.datetime.now(UTC).isoformat(),
        "generated_at_ist": f"{now_ist():%Y-%m-%d %H:%M} IST",
        "window": {
            "since_utc": close.astimezone(UTC).isoformat(),
            "since_ist": f"{close:%Y-%m-%d %H:%M} IST",
            "label": f"Since previous close — {close:%a %d %b, %-I:%M %p} IST",
        },
        "counts": {
            "total_fetched": len(items),
            "in_window": len(in_window),
            "macro": sum(1 for r in in_window if "macro" in r["categories"]),
            "fno": sum(1 for r in in_window if "fno" in r["categories"]),
        },
        "sources": sources_status,
        "top_stocks": top_stocks,
        "items": items,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH}  ({len(in_window)} in-window / {len(items)} total)")


if __name__ == "__main__":
    main()
