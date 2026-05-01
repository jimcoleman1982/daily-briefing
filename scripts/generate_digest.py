#!/usr/bin/env python3
"""
The Daily Briefing -- National & world news gathering and summarization script.

Runs 6x/day as a rolling news wire. Each run finds the single most important
NEW story not yet covered today, prepends it to the day's JSON file, and
updates the Top National and Top International stories.

Uses Brave Search API for news gathering, Anthropic Claude Sonnet 4.6 for
curation and summarization, and Google News RSS for top stories.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

import anthropic
import requests
from bs4 import BeautifulSoup

# --- Configuration ---
DENVER_TZ = ZoneInfo("America/Denver")
MAX_CANDIDATES = 25  # gather this many before curation
MAX_STORIES_PER_RUN = 4  # select up to this many new stories per run
MAX_STORIES_PER_DAY = 24  # HARD cap on stories per day (6 pulls x 4 stories = 24)
MAX_PULLS_PER_DAY = 6  # HARD cap on number of pulls per day -- protects against
                       # GitHub cron + cron-job.org + retries firing more than
                       # 6 times. Counted by distinct addedAt timestamps in today's JSON.

# Scheduled pull slots in Denver local time. The day is divided into these six
# windows; each window allows AT MOST ONE successful pull. This prevents the
# bunch-up problem where multiple cron triggers (GitHub schedule + cron-job.org
# + DST duplicates) eat the day's budget in the first few hours.
# Window assignments cover all 24 hours: midnight maps to the 9 PM slot.
SCHEDULED_SLOT_HOURS = [5, 8, 11, 14, 18, 21]
ARTICLE_TEXT_LIMIT = 3000  # chars per article
ANTHROPIC_MAX_TOKENS = 8000  # hard cap on output tokens (higher for multi-story output)
ANTHROPIC_MODEL = "claude-sonnet-4-6"
# Lighter-weight model used for batched new/update/stale classification of
# borderline dedup cases. Haiku is fast and cheap; the classification task
# is simple enough that a smaller model is fine.
ANTHROPIC_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
SITE_URL = "https://101news.org"

# Category display order and labels (must match site JavaScript)
CATEGORY_ORDER = ["politics", "world", "business", "technology", "science_health", "other"]
CATEGORY_LABELS = {
    "politics": "POLITICS & GOVERNMENT",
    "world": "WORLD NEWS",
    "business": "BUSINESS & ECONOMY",
    "technology": "TECHNOLOGY",
    "science_health": "SCIENCE & HEALTH",
    "other": "NATIONAL NEWS",
}

# --- Budget Safety Limits ---
MAX_BRAVE_QUERIES_PER_RUN = 20  # lower per-run cap since we run 6x/day
MAX_ANTHROPIC_CALLS_PER_RUN = 5  # story selection + top national + top intl + retries
brave_query_count = 0
anthropic_call_count = 0

# --- Brave Search queries: topic-rotated by run hour ---
# Two baseline queries always run. Topic buckets rotate by hour so different
# angles surface across the day's 6 runs instead of hitting the same pool.
BASELINE_QUERIES = [
    "breaking news today United States",
    "top news today",
]

TOPIC_BUCKETS = {
    "politics_courts": [
        "Supreme Court ruling today",
        "White House news today",
        "Congress vote today",
    ],
    "economy_markets": [
        "Federal Reserve news today",
        "earnings report today",
        "stock market news today",
    ],
    "world_hotspots": [
        "Israel Iran news today",
        "Middle East news today",
    ],
    "world_broad": [
        "Ukraine Russia news today",
        "China news today",
        "Europe news today",
    ],
    "tech": [
        "artificial intelligence news today",
        "technology news today",
    ],
    "science_health": [
        "medical research news today",
        "health news today",
        "science news today",
    ],
    "culture_sports": [
        "sports news today",
        "cultural news today",
        "entertainment news today",
    ],
    "right_angles": [
        "site:foxnews.com news today",
        "site:wsj.com news today",
        "site:nationalreview.com news today",
        "site:washingtonexaminer.com news today",
        "site:nypost.com news today",
    ],
    "wire_deep": [
        "site:apnews.com news today",
        "site:reuters.com news today",
        "site:bbc.com news today",
    ],
}

# Which topic buckets run at each Denver-hour scheduled slot. Off-schedule
# (manual / cron-job.org) runs use the closest scheduled hour.
HOUR_BUCKETS = {
    5:  ["politics_courts", "world_hotspots", "wire_deep"],
    8:  ["economy_markets", "tech", "right_angles"],
    11: ["politics_courts", "world_broad", "wire_deep"],
    14: ["science_health", "tech", "culture_sports", "right_angles"],
    18: ["politics_courts", "economy_markets", "world_hotspots"],
    21: ["world_broad", "culture_sports", "right_angles"],
}

# Brave News search queries (separate news endpoint, 2 per run)
NEWS_SEARCH_QUERIES = [
    "top news today United States",
    "breaking news today",
]

# --- Named RSS feeds (direct from publishers, no query budget cost) ---
# (display_name, url, lean) -- lean is L/C/R for balance tracking.
# Failures are logged and skipped; we do not retry. URLs verified working
# at the time of writing; if a publisher breaks their feed, gracefully drops.
# Note: AP and Reuters have retired public RSS -- they are picked up via
# Google News RSS and site: Brave queries instead.
NAMED_RSS_FEEDS = [
    # Wire / Center
    ("BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml", "C"),
    ("Axios", "https://api.axios.com/feed/", "C"),
    ("The Hill", "https://thehill.com/feed/", "C"),
    ("PBS NewsHour", "https://www.pbs.org/newshour/feeds/rss/headlines", "C"),
    ("CBS News", "https://www.cbsnews.com/latest/rss/main", "C"),
    ("NBC Politics", "https://feeds.nbcnews.com/nbcnews/public/politics", "C"),
    ("ABC News", "https://abcnews.go.com/abcnews/topstories", "C"),
    ("Bloomberg Politics", "https://feeds.bloomberg.com/politics/news.rss", "C"),
    # Left-leaning
    ("NPR", "https://feeds.npr.org/1001/rss.xml", "L"),
    ("Politico", "https://rss.politico.com/politics-news.xml", "L"),
    ("The Guardian US", "https://www.theguardian.com/us-news/rss", "L"),
    ("New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml", "L"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "L"),
    # Right-leaning
    ("Fox News Politics", "https://moxie.foxnews.com/google-publisher/politics.xml", "R"),
    ("Fox News Latest", "https://moxie.foxnews.com/google-publisher/latest.xml", "R"),
    ("National Review", "https://www.nationalreview.com/feed/", "R"),
    ("Washington Examiner", "https://www.washingtonexaminer.com/rss", "R"),
    ("New York Post", "https://nypost.com/feed/", "R"),
    ("Washington Times Politics", "https://www.washingtontimes.com/rss/headlines/news/politics", "R"),
    ("Free Beacon", "https://freebeacon.com/feed/", "R"),
]

# --- Balanced source strategy ---
PREFERRED_SOURCES = [
    # Wire/Center
    "apnews.com",
    "reuters.com",
    "bbc.com",
    "bbc.co.uk",
    "pbs.org",
    "thehill.com",
    "axios.com",
    "bloomberg.com",
    # Left-leaning
    "npr.org",
    "nytimes.com",
    "washingtonpost.com",
    "cnn.com",
    "theatlantic.com",
    # Right-leaning
    "foxnews.com",
    "wsj.com",
    "washingtonexaminer.com",
    "nypost.com",
    "nationalreview.com",
]

# Political lean classification: L = left, C = center/wire, R = right.
# Used to display lean tags to Claude and enforce source-balance on politics stories.
SOURCE_LEAN = {
    # Center / wire
    "apnews.com": "C", "reuters.com": "C", "bbc.com": "C", "bbc.co.uk": "C",
    "pbs.org": "C", "bloomberg.com": "C", "axios.com": "C", "thehill.com": "C",
    "usatoday.com": "C", "abcnews.go.com": "C", "cbsnews.com": "C", "nbcnews.com": "C",
    "reuters": "C", "associated press": "C", "bbc news": "C", "pbs": "C",
    "bloomberg": "C", "axios": "C", "the hill": "C", "usa today": "C",
    "abc news": "C", "cbs news": "C", "nbc news": "C", "sciencedaily": "C",
    "sciencedaily.com": "C",
    # Left-leaning
    "npr.org": "L", "nytimes.com": "L", "washingtonpost.com": "L", "cnn.com": "L",
    "theatlantic.com": "L", "theguardian.com": "L", "politico.com": "L",
    "aljazeera.com": "L", "latimes.com": "L", "vox.com": "L", "motherjones.com": "L",
    "npr": "L", "new york times": "L", "washington post": "L", "cnn": "L",
    "the atlantic": "L", "the guardian": "L", "politico": "L",
    "al jazeera": "L", "los angeles times": "L",
    # Right-leaning
    "foxnews.com": "R", "wsj.com": "R", "washingtonexaminer.com": "R",
    "nypost.com": "R", "nationalreview.com": "R", "washingtontimes.com": "R",
    "dailywire.com": "R", "thefederalist.com": "R", "newsmax.com": "R",
    "freebeacon.com": "R", "theblaze.com": "R",
    "fox news": "R", "wall street journal": "R", "washington examiner": "R",
    "new york post": "R", "national review": "R", "washington times": "R",
    "daily wire": "R", "free beacon": "R",
}

# Sources to exclude from results entirely
UNRELIABLE_SOURCES = [
    "facebook.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "reddit.com",
    "tiktok.com",
    "instagram.com",
    "tmz.com",
    "eonline.com",
    "people.com",
    "usmagazine.com",
    "buzzfeed.com",
    "dailymail.co.uk",
    "pagesix.com",
]

# Request headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DailyBriefingBot/1.0)"
}


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="The Daily Briefing digest generator")
    parser.add_argument(
        "--date",
        help="Generate digest for a specific date (YYYY-MM-DD). Used for backfill.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip DST time check (for manual/webhook triggers).",
    )
    return parser.parse_args()


def check_denver_time():
    """Exit early if Denver local time is before 4:30 AM or if the wrong DST cron fired."""
    now = datetime.datetime.now(DENVER_TZ)
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    is_dst = bool(now.dst())
    utc_hour = utc_now.hour

    # DST guard: skip if wrong timezone cron fired
    if utc_hour <= 11 and not is_dst:
        print(f"Denver time is {now.strftime('%H:%M %Z')} (MST) but UTC hour is {utc_hour} (MDT cron). Skipping.")
        sys.exit(0)
    if utc_hour >= 12 and is_dst:
        print(f"Denver time is {now.strftime('%H:%M %Z')} (MDT) but UTC hour is {utc_hour} (MST cron). Skipping.")
        sys.exit(0)

    if now.hour < 4 or (now.hour == 4 and now.minute < 30):
        print(f"Denver time is {now.strftime('%H:%M %Z')} -- too early, skipping.")
        sys.exit(0)


def load_existing_digest(date_str):
    """Load the existing digest file for today, if any. Returns dict or None."""
    filepath = os.path.join(OUTPUT_DIR, f"{date_str}.json")
    if os.path.exists(filepath):
        try:
            with open(filepath) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None
    return None


def count_distinct_pulls_today(existing_data):
    """Count the number of distinct pulls that have produced stories today.

    Each pull writes >=1 story sharing an `addedAt` timestamp. Distinct
    timestamps == distinct pulls. Used to enforce MAX_PULLS_PER_DAY so that
    extra cron triggers don't cause us to overshoot the intended 6 pulls/day.
    """
    if not existing_data:
        return 0
    timestamps = set()
    for story in existing_data.get("stories", []):
        ts = story.get("addedAt")
        if ts:
            timestamps.add(ts)
    return len(timestamps)


def get_slot_for_hour(denver_hour):
    """Map a Denver hour (0-23) to one of the 6 scheduled pull slots, or None.

    Slot boundaries (NO midnight wrap -- a midnight trigger does not "belong"
    to the 9 PM slot, because that would block the actual 9 PM run later same day):
        slot 5  ->  03:00-06:59  (covers ~5 AM scheduled run)
        slot 8  ->  07:00-09:59  (covers ~8 AM scheduled run)
        slot 11 ->  10:00-12:59  (covers ~11 AM scheduled run)
        slot 14 ->  13:00-15:59  (covers ~2 PM scheduled run)
        slot 18 ->  16:00-19:59  (covers ~6 PM scheduled run)
        slot 21 ->  20:00-22:59  (covers ~9 PM scheduled run)
        None    ->  23:00-02:59  (dead-of-night -- no pull window)

    Returns the slot identifier (one of SCHEDULED_SLOT_HOURS), or None if the
    given hour is outside any scheduled pull window.
    """
    h = denver_hour % 24
    if 3 <= h <= 6:
        return 5
    if 7 <= h <= 9:
        return 8
    if 10 <= h <= 12:
        return 11
    if 13 <= h <= 15:
        return 14
    if 16 <= h <= 19:
        return 18
    if 20 <= h <= 22:
        return 21
    # 23 and 0-2 -> dead-of-night, no pull
    return None


def has_pulled_in_current_slot(existing_data, current_dt):
    """Return True if a story has already been added in the current slot today.

    Returns False if current_dt is outside any pull window (slot is None) --
    in that case there is no slot to occupy, and the caller should be exiting
    on the "outside any slot" check anyway.
    """
    if not existing_data:
        return False

    current_slot = get_slot_for_hour(current_dt.hour)
    if current_slot is None:
        return False  # caller should exit on slot=None separately

    for story in existing_data.get("stories", []):
        iso = story.get("addedAtISO", "")
        if iso:
            try:
                dt = datetime.datetime.fromisoformat(iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=DENVER_TZ)
                else:
                    dt = dt.astimezone(DENVER_TZ)
                if get_slot_for_hour(dt.hour) == current_slot:
                    return True
                continue
            except (ValueError, TypeError):
                pass

        # Fallback: parse addedAt like "5:08 AM MT" or "11:08 PM MT"
        added = story.get("addedAt", "")
        m = re.match(r"(\d+):(\d+)\s*([AaPp])M", added)
        if m:
            h = int(m.group(1)) % 12
            if m.group(3).upper() == "P":
                h += 12
            if get_slot_for_hour(h) == current_slot:
                return True

    return False


def get_existing_headlines(existing_data):
    """Get headlines from the existing digest for within-day dedup."""
    headlines = []
    if existing_data:
        for story in existing_data.get("stories", []):
            headlines.append(story.get("headline", "").lower())
        # Also include top stories
        for key in ["topNationalStory", "topInternationalStory"]:
            ts = existing_data.get(key)
            if ts and ts.get("headline"):
                headlines.append(ts["headline"].lower())
    return headlines


def extract_source_name(url):
    """Extract a readable source name from a URL."""
    source_map = {
        "apnews.com": "Associated Press",
        "reuters.com": "Reuters",
        "nytimes.com": "New York Times",
        "washingtonpost.com": "Washington Post",
        "bbc.com": "BBC News",
        "bbc.co.uk": "BBC News",
        "npr.org": "NPR",
        "politico.com": "Politico",
        "thehill.com": "The Hill",
        "bloomberg.com": "Bloomberg",
        "wsj.com": "Wall Street Journal",
        "cnn.com": "CNN",
        "cbsnews.com": "CBS News",
        "nbcnews.com": "NBC News",
        "abcnews.go.com": "ABC News",
        "theguardian.com": "The Guardian",
        "aljazeera.com": "Al Jazeera",
        "axios.com": "Axios",
        "theatlantic.com": "The Atlantic",
        "foxnews.com": "Fox News",
        "nypost.com": "New York Post",
        "washingtonexaminer.com": "Washington Examiner",
        "nationalreview.com": "National Review",
        "pbs.org": "PBS",
        "usatoday.com": "USA Today",
        "latimes.com": "Los Angeles Times",
        "chicagotribune.com": "Chicago Tribune",
        "si.com": "Sports Illustrated",
        "espn.com": "ESPN",
    }
    for domain, name in source_map.items():
        if domain in url:
            return name
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc.replace("www.", "")
    except Exception:
        return "Unknown"


def extract_publish_date_from_url(url):
    """Extract publish date from URL path patterns like /2026/02/20/ or /2026-02-20."""
    m = re.search(r'/(\d{4})/(\d{2})/(\d{2})/', url)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(r'/(\d{4})-(\d{2})-(\d{2})/', url)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def is_article_url(url):
    """Return True if the URL looks like a specific article, not a homepage or section page.

    Homepage and section-page URLs are a major source of stale stories because
    their text contains multiple stories (including old ones still featured),
    and Claude ends up picking from that grab-bag.  We want only URLs that
    point to a single, specific article.
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    path = parsed.path.rstrip("/")

    # Root or empty path -> homepage
    if not path or path == "":
        return False

    # Common section-page patterns (e.g. /us-news, /sections/politics/, /world/us)
    # These have very short paths with no article slug
    segments = [s for s in path.split("/") if s]
    if len(segments) <= 2:
        # Allow if path contains a date pattern (article archived by date)
        if re.search(r'\d{4}[/-]\d{2}[/-]\d{2}', path):
            return True
        # Allow if last segment is long enough to be an article slug (30+ chars)
        if segments and len(segments[-1]) >= 30:
            return True
        # Allow if last segment contains a hash/ID pattern (hex, uuid)
        if segments and re.search(r'[a-f0-9]{8,}', segments[-1]):
            return True
        return False

    # Even with 3+ segments, reject common section-page patterns:
    # short terminal segment that's mostly numeric or a generic word
    last_seg = segments[-1] if segments else ""
    SECTION_SLUGS = {
        "top-stories", "latest", "breaking", "home", "index",
        "news", "politics", "world", "business", "technology",
        "science", "health", "sports", "opinion", "us-news",
        "us", "uk", "europe", "asia", "africa", "americas",
    }
    if last_seg.lower() in SECTION_SLUGS:
        return False
    # Short numeric-style IDs in section paths (e.g. /s-9097, /t-1234)
    if re.match(r'^[a-z]?-?\d{1,6}$', last_seg):
        return False

    return True


def filter_homepage_urls(candidates):
    """Remove candidates whose URLs are homepages or section pages, not articles."""
    kept = []
    removed = 0
    for c in candidates:
        if is_article_url(c["url"]):
            kept.append(c)
        else:
            removed += 1
            print(f"  Homepage/section URL filtered: {c['url']} ({c['title'][:50]}...)")
    if removed:
        print(f"  Removed {removed} homepage/section URLs, {len(kept)} remain")
    return kept


def filter_by_publish_date(candidates, target_date_str, max_delta_days=0):
    """Remove candidates whose URL-embedded publish date is not today."""
    target = datetime.date.fromisoformat(target_date_str)
    kept = []
    removed = 0
    for c in candidates:
        pub_date = extract_publish_date_from_url(c["url"])
        if pub_date is not None:
            delta = abs((pub_date - target).days)
            if delta > max_delta_days:
                removed += 1
                print(f"  Date filter: '{c['title'][:55]}...' (published {pub_date}, target {target_date_str})")
                continue
        kept.append(c)
    if removed:
        print(f"  Removed {removed} stories with wrong publish dates, {len(kept)} remain")
    return kept


def extract_significant_words(text):
    """Extract significant words from text for keyword-based dedup."""
    stop_words = {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
        "has", "had", "have", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "can", "this", "that", "these", "those",
        "it", "its", "his", "her", "he", "she", "they", "them", "their",
        "our", "we", "you", "your", "my", "me", "who", "what", "when",
        "where", "how", "why", "not", "no", "all", "each", "every", "both",
        "few", "more", "most", "other", "some", "such", "than", "too", "very",
        "just", "about", "after", "before", "new", "first", "last", "over",
        "into", "also", "back", "up", "out", "says", "said", "news", "today",
        "report", "reports",
    }
    words = re.findall(r'[a-z]+', text.lower())
    return set(w for w in words if len(w) >= 3 and w not in stop_words)


def get_source_lean(source_or_url):
    """Return 'L', 'C', 'R', or '?' for a given source name or URL."""
    if not source_or_url:
        return "?"
    s = source_or_url.lower()
    # Try exact/substring match on the lean map
    for key, lean in SOURCE_LEAN.items():
        if key in s:
            return lean
    return "?"


def get_queries_for_run():
    """Return the topic-rotated Brave web queries for this run.

    Uses the current Denver hour to pick a bucket set. Off-schedule runs
    snap to the closest scheduled hour. Within each bucket, a different
    query is picked per day-of-year so the set rotates over the week.
    """
    now = datetime.datetime.now(DENVER_TZ)
    current_hour = now.hour
    # Snap to closest scheduled hour
    scheduled_hours = sorted(HOUR_BUCKETS.keys())
    closest = min(scheduled_hours, key=lambda h: min(abs(h - current_hour),
                                                      24 - abs(h - current_hour)))
    bucket_names = HOUR_BUCKETS[closest]
    day_of_year = now.timetuple().tm_yday

    queries = list(BASELINE_QUERIES)
    for bucket_name in bucket_names:
        bucket = TOPIC_BUCKETS.get(bucket_name, [])
        if not bucket:
            continue
        # Rotate: day-of-year + stable bucket offset.
        # Python's builtin hash() is randomized per-process (unless
        # PYTHONHASHSEED is set), which breaks deterministic rotation across
        # cron runs. Use a stable character-sum instead.
        stable_offset = sum(ord(c) for c in bucket_name)
        idx = (day_of_year + stable_offset) % len(bucket)
        queries.append(bucket[idx])

    print(f"  Run hour {current_hour} -> snapped to {closest}: buckets {bucket_names}")
    return queries


def parse_rss_feed(xml_text, source_name, lean):
    """Parse an RSS XML feed into a list of entry dicts. Returns items from the last 36h."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    entries = []
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(hours=36)

    # RSS 2.0: channel/item
    items = root.findall(".//item")
    # Atom fallback: entries
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    def _find_any(parent, tags):
        """Return the first matching Element, or None. Avoids `or` on Elements
        (which are falsy when they have no subelements -- common for leaf text)."""
        for t in tags:
            el = parent.find(t)
            if el is not None:
                return el
        return None

    for item in items[:25]:  # cap per feed
        title_el = _find_any(item, ["title", "{http://www.w3.org/2005/Atom}title"])
        link_el = _find_any(item, ["link", "{http://www.w3.org/2005/Atom}link"])
        desc_el = _find_any(item, ["description", "{http://www.w3.org/2005/Atom}summary"])
        pub_el = _find_any(item, ["pubDate", "{http://www.w3.org/2005/Atom}published",
                                   "{http://www.w3.org/2005/Atom}updated"])

        if title_el is None:
            continue

        title = (title_el.text or "").strip()
        # Link can be in text or href attribute (Atom)
        if link_el is not None:
            link = (link_el.text or link_el.get("href") or "").strip()
        else:
            link = ""
        desc = ((desc_el.text if desc_el is not None else "") or "").strip()

        if not title or not link:
            continue

        # Filter by pub_date. If pubDate is present but we can't parse it,
        # drop the item -- better to lose one than let a stale item slip in.
        # Also drop items dated in the future (timezone bugs, scheduled posts).
        if pub_el is not None and pub_el.text:
            pub_dt = _try_parse_date(pub_el.text)
            if pub_dt is None:
                # Unparseable date on an item that has a pubDate -- be conservative
                continue
            if pub_dt < cutoff:
                continue
            if pub_dt > now + datetime.timedelta(hours=2):
                # Future-dated item (likely TZ bug or scheduled post) -- skip
                continue

        # Strip any HTML from description
        if desc:
            desc = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)[:500]

        entries.append({
            "title": title,
            "url": link,
            "snippet": desc or title,
            "source": source_name,
            "lean": lean,
        })

    return entries


def _try_parse_date(text):
    """Best-effort parse of RSS pubDate / Atom date formats. Returns UTC datetime or None."""
    if not text:
        return None
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        # parsedate_to_datetime has been observed to raise TypeError, ValueError,
        # and IndexError on various malformed inputs in the wild.
        pass
    # Atom ISO format
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
            datetime.timezone.utc
        )
    except Exception:
        return None


def fetch_named_rss_feeds():
    """Fetch all named RSS feeds in sequence. Returns a flat list of entries."""
    all_entries = []
    successes = 0
    failures = 0
    for source_name, feed_url, lean in NAMED_RSS_FEEDS:
        try:
            resp = requests.get(feed_url, headers=HEADERS, timeout=8)
            if resp.status_code != 200 or not resp.text.strip():
                failures += 1
                print(f"    RSS miss: {source_name} ({resp.status_code})")
                continue
            entries = parse_rss_feed(resp.text, source_name, lean)
            if entries:
                all_entries.extend(entries)
                successes += 1
                print(f"    RSS ok: {source_name} ({len(entries)} items)")
            else:
                print(f"    RSS empty: {source_name}")
        except Exception as e:
            failures += 1
            print(f"    RSS error: {source_name}: {e}")
    print(f"  Named RSS feeds: {successes} ok, {failures} failed, {len(all_entries)} total entries")
    return all_entries


def brave_search(query, api_key, count=10, freshness="pd"):
    """Run a single Brave Search API query. Returns list of result dicts."""
    global brave_query_count
    brave_query_count += 1
    if brave_query_count > MAX_BRAVE_QUERIES_PER_RUN:
        print(f"  LIMIT: Brave query cap ({MAX_BRAVE_QUERIES_PER_RUN}) reached. Skipping.")
        return []

    url = "https://api.search.brave.com/res/v1/web/search"
    params = {
        "q": query,
        "count": count,
        "freshness": freshness,
    }
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("web", {}).get("results", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
                "source": extract_source_name(item.get("url", "")),
            })
        return results
    except Exception as e:
        print(f"  Search failed for '{query}': {e}")
        return []


def brave_news_search(query, api_key, count=10, freshness="pd"):
    """Run a Brave News Search API query. Returns list of result dicts."""
    global brave_query_count
    brave_query_count += 1
    if brave_query_count > MAX_BRAVE_QUERIES_PER_RUN:
        print(f"  LIMIT: Brave query cap ({MAX_BRAVE_QUERIES_PER_RUN}) reached. Skipping.")
        return []

    url = "https://api.search.brave.com/res/v1/news/search"
    params = {
        "q": query,
        "count": count,
        "freshness": freshness,
    }
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("results", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
                "source": extract_source_name(item.get("url", "")),
            })
        return results
    except Exception as e:
        print(f"  News search failed for '{query}': {e}")
        return []


def gather_search_results(api_key, freshness="pd"):
    """Run all search queries (topic-rotated Brave + news + RSS feeds) and collect results."""
    all_results = []

    # Topic-rotated Brave web queries for this run
    web_queries = get_queries_for_run()
    for query in web_queries:
        print(f"  Searching (web): {query}")
        results = brave_search(query, api_key, freshness=freshness)
        all_results.extend(results)
        time.sleep(0.3)

    # Brave News endpoint
    for query in NEWS_SEARCH_QUERIES:
        print(f"  Searching (news): {query}")
        results = brave_news_search(query, api_key, freshness=freshness)
        all_results.extend(results)
        time.sleep(0.3)

    # Google News RSS (free, not budget-counted)
    print("  Adding Google News RSS entries to main pool...")
    rss_entries = fetch_google_news_rss()
    rss_added = 0
    for entry in rss_entries:
        url = entry.get("link", "")
        if "news.google.com" in url:
            url = resolve_google_news_url(url)
            if "news.google.com" in url:
                continue
        if any(n in url for n in UNRELIABLE_SOURCES):
            continue
        all_results.append({
            "title": entry["title"],
            "url": url,
            "snippet": entry.get("snippet", entry["title"]),
            "source": entry.get("source", extract_source_name(url)),
        })
        rss_added += 1
    print(f"  Added {rss_added} Google News RSS entries")

    # Named publisher RSS feeds (free, not budget-counted, includes right-leaning outlets)
    print("  Fetching named publisher RSS feeds...")
    named_rss = fetch_named_rss_feeds()
    named_added = 0
    for entry in named_rss:
        url = entry.get("url", "")
        if not url or any(n in url for n in UNRELIABLE_SOURCES):
            continue
        all_results.append({
            "title": entry["title"],
            "url": url,
            "snippet": entry.get("snippet", entry["title"]),
            "source": entry["source"],
        })
        named_added += 1
    print(f"  Added {named_added} named RSS entries")

    print(f"  Total raw results: {len(all_results)}")
    return all_results


def deduplicate_and_rank(results, existing_headlines=None):
    """
    Group similar results by headline similarity and keyword overlap.
    Filter out unreliable sources. Rank by source coverage.
    Return top MAX_CANDIDATES stories for Claude to curate.
    """
    # Filter out unreliable sources
    filtered = [r for r in results if not any(s in r["url"] for s in UNRELIABLE_SOURCES)]

    # Remove exact URL duplicates
    seen_urls = set()
    unique = []
    for r in filtered:
        normalized = r["url"].split("?")[0].rstrip("/")
        if normalized not in seen_urls:
            seen_urls.add(normalized)
            unique.append(r)

    # --- Pass 1: Group by headline similarity (SequenceMatcher) ---
    groups = []
    used = set()

    for i, item in enumerate(unique):
        if i in used:
            continue
        group = [item]
        used.add(i)
        for j, other in enumerate(unique):
            if j in used:
                continue
            similarity = SequenceMatcher(
                None, item["title"].lower(), other["title"].lower()
            ).ratio()
            if similarity > 0.5:
                group.append(other)
                used.add(j)
        groups.append(group)

    # --- Pass 2: Merge groups that share the same event (keyword overlap) ---
    group_keywords = []
    for group in groups:
        all_titles = " ".join(r["title"] for r in group)
        keywords = extract_significant_words(all_titles)
        group_keywords.append(keywords)

    merged = [True] * len(groups)
    for i in range(len(groups)):
        if not merged[i]:
            continue
        for j in range(i + 1, len(groups)):
            if not merged[j]:
                continue
            shared = group_keywords[i] & group_keywords[j]
            # 2 shared significant words is enough to group -- prior threshold of 4
            # was leaving most stories as orphaned sourceCount=1 singletons.
            if len(shared) >= 2:
                groups[i].extend(groups[j])
                group_keywords[i] = group_keywords[i] | group_keywords[j]
                merged[j] = False

    active_groups = [g for g, m in zip(groups, merged) if m]

    # Score each group by number of unique sources
    scored = []
    for group in active_groups:
        sources = set(r["source"] for r in group)
        preferred_count = sum(
            1 for s in sources
            if any(p in s.lower() for p in ["associated press", "reuters", "bbc", "nyt",
                                             "washington post", "wall street", "fox news",
                                             "cnn", "npr", "politico", "bloomberg"])
        )
        score = len(sources) * 2 + preferred_count
        # Pick the best representative (prefer preferred sources)
        best = group[0]
        for r in group:
            if any(p in r["url"] for p in PREFERRED_SOURCES):
                best = r
                break
        scored.append((score, best, group))

    # Sort by score descending, take top MAX_CANDIDATES
    scored.sort(key=lambda x: x[0], reverse=True)
    top_stories = []
    for score, best, group in scored[:MAX_CANDIDATES]:
        all_snippets = " ".join(r["snippet"] for r in group if r["snippet"])
        best["combined_snippets"] = all_snippets
        source_names = sorted(set(r["source"] for r in group))
        best["source_count"] = len(source_names)
        best["covering_sources"] = source_names
        top_stories.append(best)

    # Filter against existing headlines (within-day dedup)
    if existing_headlines:
        before = len(top_stories)
        kept = []
        for story in top_stories:
            title_lower = story["title"].lower()
            is_dup = False
            for prev in existing_headlines:
                sim = SequenceMatcher(None, title_lower, prev).ratio()
                if sim > 0.5:
                    is_dup = True
                    print(f"  Within-day dup: '{story['title'][:60]}...'")
                    break
                # Keyword overlap check
                story_words = extract_significant_words(title_lower)
                prev_words = extract_significant_words(prev)
                if len(story_words & prev_words) >= 4:
                    is_dup = True
                    print(f"  Within-day dup (keywords): '{story['title'][:60]}...'")
                    break
            if not is_dup:
                kept.append(story)
        top_stories = kept
        if before != len(top_stories):
            print(f"  Within-day dedup: {before} -> {len(top_stories)} candidates")

    print(f"  Selected {len(top_stories)} candidate stories after dedup/ranking")
    return top_stories


def load_recent_headlines(target_date_str, days_back=3):
    """Load headlines from previous days' JSON files for cross-day dedup."""
    target = datetime.date.fromisoformat(target_date_str)
    previous_headlines = []
    for i in range(1, days_back + 1):
        prev_date = target - datetime.timedelta(days=i)
        prev_file = os.path.join(OUTPUT_DIR, f"{prev_date.isoformat()}.json")
        if os.path.exists(prev_file):
            try:
                with open(prev_file) as f:
                    data = json.load(f)
                for story in data.get("stories", []):
                    previous_headlines.append(story.get("headline", "").lower())
            except (json.JSONDecodeError, KeyError):
                continue
    return previous_headlines


UPDATE_INDICATOR_WORDS = {
    # Only words that strongly signal a NEW development on a prior story.
    # Deliberately narrow -- false negatives (dropping a valid update) are
    # far less harmful than false positives (letting stale stories through).
    "update", "arrested", "arrest", "charged", "convicted", "verdict",
    "sentenced", "indicted", "identified", "reversal", "overturned",
    "recalled", "resigned", "fired", "sues", "sued", "settlement",
}


def has_update_indicators(title_lower):
    """Check if a headline contains words suggesting a new development."""
    title_words = set(re.findall(r'[a-z]+', title_lower))
    return bool(title_words & UPDATE_INDICATOR_WORDS)


def filter_cross_day_duplicates(candidates, target_date_str):
    """Remove candidates whose headlines are too similar to recent days' stories."""
    previous_headlines = load_recent_headlines(target_date_str)
    if not previous_headlines:
        print("  No previous days' data found -- skipping cross-day dedup")
        return candidates

    print(f"  Checking against {len(previous_headlines)} headlines from previous days")
    kept = []
    removed = 0
    for candidate in candidates:
        title_lower = candidate["title"].lower()
        is_duplicate = False
        for prev_headline in previous_headlines:
            similarity = SequenceMatcher(None, title_lower, prev_headline).ratio()
            if similarity > 0.45:
                if has_update_indicators(title_lower):
                    break
                is_duplicate = True
                print(f"  Cross-day dup removed: '{candidate['title'][:60]}...'")
                break
            candidate_words = extract_significant_words(title_lower)
            prev_words = extract_significant_words(prev_headline)
            shared = candidate_words & prev_words
            if len(shared) >= 3:
                if has_update_indicators(title_lower):
                    break
                is_duplicate = True
                print(f"  Cross-day dup removed (keywords): '{candidate['title'][:60]}...'")
                break
        if is_duplicate:
            removed += 1
        else:
            kept.append(candidate)

    if removed > 0:
        print(f"  Removed {removed} cross-day duplicates, {len(kept)} candidates remain")
    return kept


def _classify_borderline_stories_with_claude(borderline_cases):
    """For each (story, matched_prev_headline) pair, ask Claude whether it's
    'new', 'update', or 'stale'. Returns list of classifications.

    One batched API call instead of a word-list heuristic. Much more reliable
    than matching on words like "new" or "today" in the summary.
    """
    global anthropic_call_count

    if not borderline_cases:
        return []

    if anthropic_call_count >= MAX_ANTHROPIC_CALLS_PER_RUN:
        print(f"  Anthropic cap reached -- defaulting borderline cases to 'update'")
        return ["update"] * len(borderline_cases)

    pair_blocks = []
    for i, (story, matched_prev) in enumerate(borderline_cases, 1):
        summary_snip = story.get("summary", "")[:500]
        pair_blocks.append(
            f"[PAIR {i}]\n"
            f"Previous headline: {matched_prev}\n"
            f"Candidate headline: {story['headline']}\n"
            f"Candidate summary excerpt: {summary_snip}"
        )
    pairs_text = "\n\n".join(pair_blocks)

    user_prompt = f"""For each pair below, classify the candidate as one of:
- "new": the candidate is about a genuinely different event, not the previous story
- "update": the candidate is the same ongoing story, but the summary contains a concrete new development (new ruling, new action, new death, new reversal, new fact that wasn't known before)
- "stale": the candidate is the same story as the previous headline with no material new development, just ongoing coverage

{pairs_text}

Return ONLY a JSON array with one string per pair in order, e.g. ["new", "update", "stale"]. No other text."""

    print(f"  Classifying {len(borderline_cases)} borderline cases via {ANTHROPIC_CLASSIFIER_MODEL}...")
    anthropic_call_count += 1
    client = anthropic.Anthropic()

    # Safe default: "update" preserves the story with an Update: prefix rather
    # than deleting it. Any failure path falls through to this.
    safe_default = ["update"] * len(borderline_cases)

    try:
        response = client.messages.create(
            model=ANTHROPIC_CLASSIFIER_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = response.content[0].text
        parsed = _try_parse_json(raw)
    except Exception as e:
        print(f"  Borderline classification API error: {e}")
        return safe_default

    if not isinstance(parsed, list):
        print(f"  Borderline classification: non-list response, falling back to 'update'. Raw: {str(raw)[:200]}")
        return safe_default

    # Normalize each classification string
    result = []
    for c in parsed:
        if not isinstance(c, str):
            result.append("update")
            continue
        c = c.strip().lower()
        if c not in {"new", "update", "stale"}:
            c = "update"
        result.append(c)

    # Count mismatch: be conservative -- if Claude returned a different number
    # of classifications than we asked for, something is off. Fall back entirely
    # rather than silently mis-align the decisions.
    if len(result) != len(borderline_cases):
        print(f"  Borderline classification count mismatch "
              f"(got {len(result)}, expected {len(borderline_cases)}) -- falling back")
        return safe_default

    return result


def post_claude_dedup(new_stories, target_date_str, existing_today_headlines=None):
    """Final dedup pass on Claude's generated headlines.

    Checks against BOTH:
    - today's already-published stories (within-day dedup -- catches the case where
      Brave returns a raw title that doesn't match Claude's earlier rewritten headline,
      but Claude then writes a near-duplicate of the earlier headline)
    - the last 3 days of previous headlines (cross-day dedup)

    Uses Claude's own disposition field when present, then a semantic
    classification via Claude for borderline cases.
    """
    prior_days = load_recent_headlines(target_date_str, days_back=3)
    today_existing = [h for h in (existing_today_headlines or []) if h]
    # Combined list for similarity checking. Today's list is authoritative for
    # "stale == dupe within today" -- we always drop those, never mark as Update:.
    previous_headlines = today_existing + prior_days

    if not previous_headlines:
        # Cold start: no prior data to dedup against. Still honor Claude's
        # explicit dispositions so the behavior is consistent with the main path.
        out = []
        for s in new_stories:
            disposition = str(s.get("disposition") or "").strip().lower()
            if disposition == "stale":
                continue
            if disposition == "update" and not s["headline"].startswith("Update:"):
                s["headline"] = "Update: " + s["headline"]
            out.append(s)
        return out

    print(f"\n  Post-Claude dedup: checking {len(new_stories)} stories against "
          f"{len(today_existing)} same-day + {len(prior_days)} prior-day headlines")

    # First pass: trust Claude's disposition when present
    kept = []
    borderline = []  # (index_in_kept, story, matched_prev) for Claude classification
    removed = 0

    for story in new_stories:
        headline_lower = story["headline"].lower()
        # Coerce to string first -- Claude occasionally hallucinates non-string values
        disposition = str(story.get("disposition") or "").strip().lower()
        story_words = extract_significant_words(headline_lower)

        # STEP 1: Same-day duplicate check runs FIRST, regardless of disposition.
        # The story is already in today's digest -- don't add it again whether
        # Claude labeled it new, update, or already-prefixed Update:. Compare
        # on the version WITHOUT the "Update:" prefix so we match more reliably.
        compare_headline = re.sub(r"^update:\s*", "", headline_lower)
        compare_words = extract_significant_words(compare_headline)
        matched_today = None
        for prev_headline in today_existing:
            prev_compare = re.sub(r"^update:\s*", "", prev_headline)
            similarity = SequenceMatcher(None, compare_headline, prev_compare).ratio()
            if similarity > 0.45:
                matched_today = prev_headline
                break
            prev_words = extract_significant_words(prev_compare)
            if len(compare_words & prev_words) >= 3:
                matched_today = prev_headline
                break

        if matched_today:
            removed += 1
            print(f"  Post-Claude: SAME-DAY duplicate, dropping: '{story['headline'][:65]}...'")
            print(f"    Matched existing today: '{matched_today[:65]}...'")
            continue

        # STEP 2: Honor Claude's explicit "stale" disposition
        if disposition == "stale":
            removed += 1
            print(f"  Post-Claude: Claude marked stale, dropping: '{story['headline'][:65]}...'")
            continue

        # STEP 3: Already prefixed "Update:" (and not a same-day dup) -> keep as-is
        if headline_lower.startswith("update:"):
            kept.append(story)
            continue

        # STEP 4: Explicit "update" disposition (against prior days) -> add prefix and keep
        if disposition == "update":
            if not story["headline"].startswith("Update:"):
                story["headline"] = "Update: " + story["headline"]
            kept.append(story)
            continue

        # STEP 5: disposition == "new" or missing: check for similarity to PRIOR days
        matched_prior = None
        for prev_headline in prior_days:
            similarity = SequenceMatcher(None, headline_lower, prev_headline).ratio()
            if similarity > 0.45:
                matched_prior = prev_headline
                break
            prev_words = extract_significant_words(prev_headline)
            if len(story_words & prev_words) >= 3:
                matched_prior = prev_headline
                break

        if matched_prior:
            # Defer to Claude classification (batched below)
            borderline.append((len(kept), story, matched_prior))
            kept.append(story)  # tentative; may be modified or removed after classification
        else:
            kept.append(story)

    # Batch-classify borderline cases with Claude
    if borderline:
        classifications = _classify_borderline_stories_with_claude(
            [(s, m) for _, s, m in borderline]
        )
        # Apply decisions (reverse order to preserve indices when removing)
        to_remove = set()
        for (idx, story, matched_prev), decision in zip(borderline, classifications):
            if decision == "stale":
                to_remove.add(idx)
                removed += 1
                print(f"  Post-Claude: Claude classified stale, dropping: '{story['headline'][:65]}...'")
                print(f"    Matched previous: '{matched_prev[:65]}...'")
            elif decision == "update":
                if not kept[idx]["headline"].startswith("Update:"):
                    kept[idx]["headline"] = "Update: " + kept[idx]["headline"]
                print(f"  Post-Claude: Claude classified update: '{kept[idx]['headline'][:65]}...'")
            # "new" -> leave as-is
        if to_remove:
            kept = [s for i, s in enumerate(kept) if i not in to_remove]

    if removed > 0:
        print(f"  Post-Claude dedup: removed {removed} stale stories, {len(kept)} remain")
    return kept


# --- Article text extraction ---

def _extract_article_text(html):
    """Extract article body text from HTML content."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(["script", "style", "nav", "header", "footer", "aside", "figure", "figcaption"]):
        tag.decompose()

    article_body = None
    selectors = [
        "article",
        '[class*="article-body"]',
        '[class*="story-body"]',
        '[class*="entry-content"]',
        '[class*="post-content"]',
        '[class*="article-content"]',
        "main",
    ]
    for selector in selectors:
        found = soup.select_one(selector)
        if found:
            article_body = found
            break

    if not article_body:
        article_body = soup.body if soup.body else soup

    paragraphs = article_body.find_all("p")
    text = "\n".join(p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 30)

    if len(text) < 100:
        return None

    return text[:ARTICLE_TEXT_LIMIT]


def fetch_article_text(url):
    """Fetch and extract article body text from a URL."""
    if any(s in url for s in UNRELIABLE_SOURCES):
        return None

    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        return _extract_article_text(resp.text)
    except Exception as e:
        print(f"  Failed to fetch {url}: {e}")
        return None


def fetch_articles(stories):
    """Fetch article text for each story. Falls back to snippets."""
    for story in stories:
        print(f"  Fetching: {story['url']}")
        text = fetch_article_text(story["url"])
        if text:
            story["article_text"] = text
            print(f"    Got {len(text)} chars of article text")
        else:
            story["article_text"] = story.get("combined_snippets", story.get("snippet", ""))
            print(f"    Using snippet text ({len(story['article_text'])} chars)")
    return stories


# --- Claude API for story selection ---

SYSTEM_PROMPT = """You are the editor-in-chief of The Daily Briefing, a national and world news digest. You select and summarize the most important stories. Write factual, detailed summaries in clean newspaper style. Present stories without political bias. When covering politically divisive topics, include perspectives from both sides. No editorializing, no emojis. Categories: politics, world, business, technology, science_health, other."""


def build_prompt(stories, target_date_str, num_to_select, existing_headlines=None, is_first_run=False):
    """Build the prompt for selecting and summarizing multiple new stories."""
    story_blocks = []
    lean_counts = {"L": 0, "C": 0, "R": 0, "?": 0}
    for i, story in enumerate(stories, 1):
        source_count = story.get("source_count", 1)
        covering = story.get("covering_sources", [story["source"]])
        # Tag each covering source with its political lean. get_source_lean
        # always returns a truthy string ("?" for unknown), so use explicit
        # fallback to the URL rather than `or`.
        covering_tagged = []
        for src in covering:
            lean = get_source_lean(src)
            if lean == "?":
                lean = get_source_lean(story.get("url", ""))
            covering_tagged.append(f"{src} [{lean}]")
        covering_str = ", ".join(covering_tagged)
        primary_lean = get_source_lean(story["source"])
        if primary_lean == "?":
            primary_lean = get_source_lean(story.get("url", ""))
        lean_counts[primary_lean] = lean_counts.get(primary_lean, 0) + 1
        block = f"""[CANDIDATE {i}] [Primary lean: {primary_lean}]
Headline: {story['title']}
Source: {story['source']} [{primary_lean}]
URL: {story['url']}
Sources covering this story: {source_count} ({covering_str})
Article text: {story['article_text']}"""
        story_blocks.append(block)

    stories_text = "\n\n".join(story_blocks)

    # Build dedup block from existing headlines
    dedup_block = ""
    if existing_headlines:
        hl_list = "\n".join(f"- {h}" for h in existing_headlines[:30])
        dedup_block = f"""
IMPORTANT -- STORIES ALREADY PUBLISHED TODAY (do NOT repeat these):
{hl_list}

Do NOT select any candidate that covers the same event as the headlines above. Only select a story with a genuinely new or developing angle."""

    # Also check previous days
    prev_headlines = load_recent_headlines(target_date_str)
    prev_block = ""
    if prev_headlines:
        prev_list = "\n".join(f"- {h}" for h in prev_headlines[:50])
        prev_block = f"""
STORIES FROM PREVIOUS DAYS (do NOT repeat these unless there is a genuinely new development):
{prev_list}

CRITICAL RULE: If a story covers the SAME event as any headline above and there is NO new development since it was last covered, you MUST skip it. "Still being covered" or "still trending" is NOT a new development. A new development means something materially changed: a new official action, arrest, vote, death, reversal, or concrete new information.

If a story IS a genuine update on a previously covered event, you MUST prefix the headline with "Update: " (e.g. "Update: Iran Talks Resume After Ceasefire Extension"). The update must be clearly described in the summary."""

    lean_summary = (f"Candidate pool lean mix: {lean_counts['L']} left, "
                    f"{lean_counts['C']} center/wire, {lean_counts['R']} right, "
                    f"{lean_counts['?']} unclassified.")

    return f"""Date: {target_date_str}. Below are {len(stories)} candidate national/world news stories.

{lean_summary}

YOUR TASK: Select the {num_to_select} MOST IMPORTANT stories from the candidate pool. You are picking only {num_to_select} -- be RUTHLESSLY selective. The reader sees only what you choose; second-tier news gets cut.

THINK ABOUT IMPORTANCE BEFORE YOU PICK. For each candidate, ask:

1. SCALE OF IMPACT -- How many people are directly affected, and how seriously?
   * Tens of millions affected (war, election results, major federal policy, market crash) -> HIGH
   * Millions affected (regional disaster, major industry shift, supreme court ruling) -> HIGH
   * Hundreds of thousands (state-level news, large corporate event) -> MEDIUM
   * Thousands or fewer (single-incident crime story without national pattern) -> LOW

2. SIGNIFICANCE OF EVENT -- What kind of event is this?
   TIER 1 (almost always include if today): mass casualty events, active military operations, declaration of war, head-of-state actions (presidential orders, resignations, impeachment), Supreme Court rulings, major Federal Reserve / interest-rate decisions, election results, major terrorist attacks, presidential nominations to Cabinet/SCOTUS.
   TIER 2 (include when no Tier 1 dominates): significant policy announcements, major corporate news (megamergers, CEO ousters at top-10 companies), cabinet-level testimony, major foreign-policy moves, major scientific breakthroughs, large natural disasters without mass casualties.
   TIER 3 (only if you have room and they're genuinely fresh): cultural events, celebrity news, sports if of national significance (Super Bowl, World Series), local stories that signal a national pattern.
   NOT NEWSWORTHY at this scope: routine corporate earnings beats/misses, daily market moves, individual crime stories without national significance, obituaries of non-public figures, weather unless catastrophic, sports scores from regular-season games, political squabbles without policy substance.

3. SOURCE CORROBORATION -- How many outlets are covering this?
   * 6+ outlets -> strong importance signal, weight heavily
   * 3-5 outlets -> moderate
   * 1-2 outlets -> only include if event is intrinsically major (Tier 1) and time-sensitive

4. FRESHNESS -- Did the event FIRST happen today / in the last 12-24 hours?
   * If you cannot articulate WHAT IS NEW about this story today, do not include it.

HOW TO USE THIS WHEN PICKING {num_to_select}:
- Mentally rank all candidates by tier and impact, THEN choose the top {num_to_select}.
- Tier 1 always beats Tier 2 always beats Tier 3.
- Within a tier, higher source count beats lower.
- Aim for category variety -- but variety NEVER overrides importance. A 4-Tier-1-politics-story day is correct if that's the news.

{"FIRST RUN OF THE DAY: include important stories from the last 12 hours, even if they happened late last night." if is_first_run else "Today's events only. Do NOT pick a story whose actual event happened yesterday or earlier, even if outlets are still writing about it -- unless there is a CONCRETE new development today (new ruling, new death, new reversal, new fact)."}

POLITICAL BALANCE (critical):
- Each candidate is tagged with its primary source lean: [L] left, [C] center/wire, [R] right, [?] unclassified.
- Story-topic selection must be politically neutral: do not suppress stories that are awkward for one side. Pick stories on merit (importance, freshness, multi-source coverage).
- WHEN MULTIPLE CANDIDATES COVER THE SAME EVENT with comparable reporting quality, prefer a right-leaning [R] or wire [C] source over a left-leaning [L] one. Our digest has historically over-indexed on left/center outlets and we want more [R] representation when quality is roughly equal.
- Across your full selection, aim for a mix of leans. If you are selecting 3+ political stories, ideally at least one draws from a right-leaning [R] or [C] source when that coverage is available in the pool.
- When a story covers a politically divisive topic, the summary MUST include perspectives from both sides -- reference how the right and left are framing it if coverage differs.

FRESHNESS & DEDUP:
- CRITICAL: Do NOT select stories already covered today (see list below)
- If two candidates cover the same event, pick the one with better sourcing -- do not include both
- FRESHNESS CHECK: Before selecting any story, ask yourself "did this event FIRST happen today?" If the answer is no, skip it unless there is a concrete new development since the last time we covered it.
{dedup_block}
{prev_block}

{stories_text}

---

Return a JSON array of up to {num_to_select} stories. For each story:
- "category": one of "politics", "world", "business", "technology", "science_health", or "other"
- "headline": clear, factual headline. If this is an update on a previously covered story, prefix with "Update: "
- "summary": 3-4 paragraphs, each 2-4 sentences. Use \\n\\n between paragraphs. Cover what happened, who is involved, why it matters. If politically divisive, include both sides.
- "source": publication name
- "url": direct link to the original article
- "sourceCount": number of outlets covering this story (copy from the candidate info above)
- "disposition": "new" if this is a fresh story with no prior coverage, or "update" if it's a genuinely new development on a story we already covered (something materially changed -- new ruling, new death, new reversal, new concrete fact).
- "breaking": true or false. Set true ONLY for urgent, high-stakes developing stories -- Drudge Report "red siren" territory. Examples: mass casualty events, active shooters, major terrorist attacks, assassinations, coups, declarations of war, natural disasters with major loss of life, pivotal political events (impeachment, resignation of a major head of state), or a fast-moving crisis where the situation is changing hour to hour. DO NOT set true for routine news, policy announcements, earnings reports, sports, court rulings, political disagreements, or ongoing stories that are simply continuing. Be VERY selective -- typically zero or one story per day qualifies. When in doubt, mark false.
- "importance": "tier1" / "tier2" / "tier3" -- which importance tier from the selection criteria above this story falls into. Used for transparency and debugging.

Return ONLY the JSON array, no other text. Remember: you are picking only {num_to_select} stories from {len(stories)} candidates. Be ruthless. A reader who sees these {num_to_select} stories should feel they understand the most important news of the day."""


def _try_parse_json(text):
    """Try multiple strategies to extract a JSON object or array from response text."""
    # 1. Direct parse
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. Strip markdown code fences
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", text.strip())
    cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()
    if cleaned != text.strip():
        try:
            return json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            pass

    # 3. Extract JSON object
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(text[first_brace:last_brace + 1])
        except (json.JSONDecodeError, ValueError):
            pass

    # 4. Extract JSON array
    first_bracket = text.find("[")
    last_bracket = text.rfind("]")
    if first_bracket != -1 and last_bracket > first_bracket:
        try:
            return json.loads(text[first_bracket:last_bracket + 1])
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def determine_stories_needed(existing_count):
    """Decide how many stories to select on this pull.

    Strict 6 per pull, capped at 36 total. Combined with MAX_PULLS_PER_DAY,
    this gives exactly 6 pulls x 6 stories = 36. The selection prompt asks
    Claude to pick the most important stories from the candidate pool.
    """
    remaining = MAX_STORIES_PER_DAY - existing_count
    if remaining <= 0:
        return 0
    return min(MAX_STORIES_PER_RUN, remaining)


def call_anthropic_stories(stories, target_date_str, num_to_select, existing_headlines=None, is_first_run=False):
    """Send candidates to Anthropic and get multiple curated stories back."""
    global anthropic_call_count

    if num_to_select <= 0:
        print("  Already at daily story cap. Skipping story selection.")
        return []

    client = anthropic.Anthropic()
    user_prompt = build_prompt(stories, target_date_str, num_to_select, existing_headlines, is_first_run=is_first_run)

    print(f"\n--- Anthropic API Call (Story Selection) ---")
    print(f"  Model: {ANTHROPIC_MODEL}")
    print(f"  Candidates sent: {len(stories)}")
    print(f"  Selecting up to: {num_to_select} stories")

    anthropic_call_count += 1
    if anthropic_call_count > MAX_ANTHROPIC_CALLS_PER_RUN:
        print(f"  LIMIT: Anthropic call cap reached. Exiting.")
        sys.exit(1)

    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=ANTHROPIC_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        print(f"  API call failed: {e}")
        print("  Retrying in 30 seconds...")
        time.sleep(30)
        anthropic_call_count += 1
        try:
            response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=ANTHROPIC_MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception as e2:
            print(f"  Retry failed: {e2}")
            sys.exit(1)

    usage = response.usage
    cost = (usage.input_tokens * 3 + usage.output_tokens * 15) / 1_000_000
    print(f"  Tokens: {usage.input_tokens:,} in / {usage.output_tokens:,} out")
    print(f"  Cost: ${cost:.4f}")

    raw_text = response.content[0].text
    parsed = _try_parse_json(raw_text)

    if parsed:
        # Handle both single object and array responses
        if isinstance(parsed, dict) and "headline" in parsed:
            return [parsed]
        if isinstance(parsed, list):
            # Validate each item has required fields
            valid = [s for s in parsed if isinstance(s, dict) and "headline" in s]
            return valid[:num_to_select]

    print(f"  Failed to parse story JSON. Raw: {raw_text[:300]}")
    return []


# --- Top Story (National and International) ---

GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss"


def fetch_google_news_rss():
    """Fetch and parse Google News RSS feed."""
    print("  Fetching Google News RSS...")
    try:
        result = subprocess.run(
            ["curl", "-sL", GOOGLE_NEWS_RSS_URL,
             "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
             "--max-time", "15"],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0 or not result.stdout.strip():
            print("  Google News RSS fetch failed")
            return []

        root = ET.fromstring(result.stdout)
        items = root.findall(".//item")
        print(f"  Google News RSS: {len(items)} items")

        entries = []
        for item in items[:20]:
            title_el = item.find("title")
            link_el = item.find("link")
            pub_el = item.find("pubDate")
            if title_el is None or link_el is None:
                continue

            raw_title = title_el.text or ""
            if " - " in raw_title:
                parts = raw_title.rsplit(" - ", 1)
                headline = parts[0].strip()
                source = parts[1].strip()
            else:
                headline = raw_title.strip()
                source = "Unknown"

            entries.append({
                "title": headline,
                "link": link_el.text or "",
                "source": source,
                "pub_date": pub_el.text if pub_el is not None else "",
            })

        return entries
    except Exception as e:
        print(f"  Google News RSS failed: {e}")
        return []


def resolve_google_news_url(google_url):
    """Resolve a Google News redirect URL to the actual article URL."""
    if "news.google.com" not in google_url:
        return google_url
    try:
        result = subprocess.run(
            ["curl", "-sI", "-L", google_url,
             "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
             "--max-time", "10", "--max-redirs", "5"],
            capture_output=True, text=True, timeout=15,
        )
        final_url = google_url
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.lower().startswith("location:"):
                candidate = line.split(":", 1)[1].strip()
                if "news.google.com" not in candidate:
                    final_url = candidate
        return final_url
    except Exception:
        return google_url


def write_output(existing_data, new_stories, date_str, date_formatted):
    """Write/update the JSON data file. Prepends new stories to existing stories."""
    if existing_data:
        output = existing_data
    else:
        output = {
            "date": date_str,
            "dateFormatted": date_formatted,
            "stories": [],
        }

    # Remove legacy top story keys if present from older runs
    output.pop("topNationalStory", None)
    output.pop("topInternationalStory", None)

    # Add timestamps to new stories and prepend (newest first)
    now = datetime.datetime.now(DENVER_TZ)
    time_label = now.strftime("%-I:%M %p MT")
    iso_label = now.isoformat()

    if new_stories:
        for story in reversed(new_stories):
            story["addedAt"] = time_label
            story["addedAtISO"] = iso_label
            output["stories"].insert(0, story)

    # No hard cap -- fresh news always gets added

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(OUTPUT_DIR, f"{date_str}.json")

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    total_stories = len(output["stories"])
    print(f"\nWrote {total_stories} total stories to {filepath}")
    return filepath


def get_freshness_for_date(target_date):
    """Determine Brave Search freshness parameter for a given date."""
    now = datetime.datetime.now(DENVER_TZ).date()
    delta = (now - target_date).days

    if delta <= 0:
        return "pd"
    else:
        start = (target_date - datetime.timedelta(days=1)).isoformat()
        end = (target_date + datetime.timedelta(days=1)).isoformat()
        return f"{start}to{end}"


def main():
    args = parse_args()

    print("=" * 60)
    print("The Daily Briefing -- Generate Digest")
    print("=" * 60)

    # Determine target date
    if args.date:
        try:
            target_date = datetime.date.fromisoformat(args.date)
        except ValueError:
            print(f"ERROR: Invalid date format '{args.date}'. Use YYYY-MM-DD.")
            sys.exit(1)
        target_date_str = args.date
        target_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, tzinfo=DENVER_TZ)
        target_formatted = target_dt.strftime("%A, %B %d, %Y").replace(" 0", " ")
        print(f"Backfill mode: generating for {target_formatted}")
    else:
        if not args.force:
            check_denver_time()
        now = datetime.datetime.now(DENVER_TZ)
        target_date = now.date()
        target_date_str = now.strftime("%Y-%m-%d")
        target_formatted = now.strftime("%A, %B %d, %Y").replace(" 0", " ")
        print(f"Date: {target_formatted} ({target_date_str})")
        print(f"Denver time: {now.strftime('%H:%M %Z')}")

    # Load existing digest for today (for within-day dedup)
    print("\n[Step 0] Loading existing digest for today...")
    existing_data = load_existing_digest(target_date_str)
    existing_headlines = get_existing_headlines(existing_data)
    if existing_headlines:
        print(f"  Found {len(existing_headlines)} existing headlines for today")
    else:
        print("  No existing digest for today -- fresh start")

    # Short-circuit if daily story cap already reached
    existing_story_count = len(existing_data.get("stories", [])) if existing_data else 0
    if existing_story_count >= MAX_STORIES_PER_DAY:
        print(f"\nDaily story cap ({MAX_STORIES_PER_DAY}) already reached "
              f"({existing_story_count} stories). Skipping this run.")
        sys.exit(0)

    # Short-circuit if pull cap already reached. Each pull writes >=1 story
    # with a unique addedAt timestamp, so distinct timestamps == distinct pulls
    # that produced output. Triggers that resulted in zero new stories don't
    # count (they didn't consume budget). This protects against GitHub cron
    # + cron-job.org + retries firing more than 6 times/day.
    pulls_today = count_distinct_pulls_today(existing_data)
    if pulls_today >= MAX_PULLS_PER_DAY:
        print(f"\nDaily pull cap ({MAX_PULLS_PER_DAY}) already reached "
              f"({pulls_today} pulls, {existing_story_count} stories). Skipping this run.")
        sys.exit(0)
    print(f"  Pulls so far today: {pulls_today}/{MAX_PULLS_PER_DAY}")

    # Short-circuit if this slot has already pulled. Without this, multiple
    # cron triggers (GitHub schedule + cron-job.org + DST-bypass duplicates)
    # would eat all 6 pulls in the early-morning slots, leaving nothing for
    # the afternoon and evening slots. By limiting to one pull per ~3-hour
    # slot, the day's stories are spread across all 6 scheduled time blocks.
    now_denver = datetime.datetime.now(DENVER_TZ)
    if not args.date:  # only enforce slot cap for live runs, not backfill
        current_slot = get_slot_for_hour(now_denver.hour)
        if current_slot is None:
            # Triggered outside any scheduled slot (e.g. dead-of-night run from
            # a misconfigured cron). Refuse to pull -- this saves API budget
            # and prevents midnight runs from claiming the 9 PM slot for the day.
            print(f"\nTriggered outside any scheduled slot ({now_denver.strftime('%H:%M %Z')}). "
                  f"Scheduled slots are 5/8/11 AM and 2/6/9 PM. Skipping this run.")
            sys.exit(0)
        if has_pulled_in_current_slot(existing_data, now_denver):
            print(f"\nCurrent slot (slot {current_slot}, {now_denver.strftime('%H:%M %Z')}) "
                  f"has already pulled today. Skipping this run.")
            sys.exit(0)
        print(f"  Current slot: {current_slot} (Denver hour {now_denver.hour})")

    # Verify API keys
    brave_key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if not brave_key:
        print("ERROR: BRAVE_SEARCH_API_KEY not set")
        sys.exit(1)

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    # Determine search freshness
    freshness = get_freshness_for_date(target_date)
    print(f"Search freshness: {freshness}")

    # Step 1: Search
    print("\n[Step 1] Searching for national/world news...")
    raw_results = gather_search_results(brave_key, freshness=freshness)
    if not raw_results:
        print("WARNING: No search results found")
        # Still try top stories even if main search fails

    # Determine how many stories we need this run
    existing_story_count = len(existing_data.get("stories", [])) if existing_data else 0
    num_to_select = determine_stories_needed(existing_story_count)
    print(f"  Stories today so far: {existing_story_count}, selecting up to: {num_to_select}")

    # Step 1b: Filter out homepage/section URLs (major source of stale stories)
    if raw_results:
        print("\n[Step 1b] Filtering homepage/section URLs...")
        raw_results = filter_homepage_urls(raw_results)

    # Step 2: Deduplicate, rank, and filter
    new_stories = []
    if raw_results:
        print("\n[Step 2] Deduplicating and ranking stories...")
        top_stories = deduplicate_and_rank(raw_results, existing_headlines)

        if top_stories:
            print("\n[Step 2b] Filtering by publish date...")
            top_stories = filter_by_publish_date(top_stories, target_date_str)

            print("\n[Step 2c] Filtering cross-day duplicates...")
            top_stories = filter_cross_day_duplicates(top_stories, target_date_str)

        # Step 3: Fetch article content
        if top_stories:
            print("\n[Step 3] Fetching article content...")
            stories_with_text = fetch_articles(top_stories[:15])

            # Step 4: Select and summarize stories
            print(f"\n[Step 4] Selecting {num_to_select} stories via Anthropic...")
            first_run = existing_story_count == 0
            if first_run:
                print("  First run of the day -- expanding window to last 12 hours")
            new_stories = call_anthropic_stories(
                stories_with_text, target_date_str, num_to_select, existing_headlines,
                is_first_run=first_run,
            )
            if new_stories:
                # Step 4b: Post-Claude dedup -- catch stale stories Claude generated.
                # Pass today's existing headlines so we catch within-day duplicates
                # (the case where Brave's raw title didn't match but Claude wrote a
                # near-duplicate headline to something already in today's digest).
                new_stories = post_claude_dedup(
                    new_stories, target_date_str,
                    existing_today_headlines=existing_headlines,
                )

                for s in new_stories:
                    print(f"  Selected: [{s.get('category', '?')}] {s['headline'][:70]}")
                print(f"  Total new stories: {len(new_stories)}")
            else:
                print("  No new stories selected")
        else:
            print("  No candidates after filtering")

    # Step 5: Write output
    if not new_stories:
        print("\nNo new content to add. Exiting.")
        sys.exit(0)

    print("\n[Step 5] Writing JSON output...")
    filepath = write_output(
        existing_data, new_stories, target_date_str, target_formatted,
    )

    print("\nDone!")
    return filepath


if __name__ == "__main__":
    main()
