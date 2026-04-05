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
MAX_CANDIDATES = 15  # gather this many before curation (lower since we pick 1)
ARTICLE_TEXT_LIMIT = 3000  # chars per article
ANTHROPIC_MAX_TOKENS = 4000  # hard cap on output tokens
ANTHROPIC_MODEL = "claude-sonnet-4-6"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "site", "data")
SITE_URL = "https://jimcoleman1982.github.io/daily-briefing"

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

# Brave Search queries -- national/world focus, kept lean (~10 per run)
SEARCH_QUERIES = [
    "breaking news today United States",
    "top US news today",
    "US politics news today Congress White House",
    "world international news today",
    "economy business news today",
    "technology AI news today",
    "science health news today",
    "site:apnews.com news today",
    "site:reuters.com news today",
]

# Brave News search queries (separate news endpoint)
NEWS_SEARCH_QUERIES = [
    "top news today United States",
    "breaking news today",
]

# --- Balanced source strategy ---
# Wire/Center
# Left-leaning
# Right-leaning
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


def filter_by_publish_date(candidates, target_date_str, max_delta_days=1):
    """Remove candidates whose URL-embedded publish date is too far from the target date."""
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
    """Run all search queries (web + news) and collect results."""
    all_results = []

    for query in SEARCH_QUERIES:
        print(f"  Searching (web): {query}")
        results = brave_search(query, api_key, freshness=freshness)
        all_results.extend(results)
        time.sleep(0.3)

    for query in NEWS_SEARCH_QUERIES:
        print(f"  Searching (news): {query}")
        results = brave_news_search(query, api_key, freshness=freshness)
        all_results.extend(results)
        time.sleep(0.3)

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
            if len(shared) >= 4:
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
        best["source_count"] = len(set(r["source"] for r in group))
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
    "update", "arrested", "arrest", "charged", "convicted", "verdict",
    "sentenced", "indicted", "identified", "confirmed", "dead", "dies",
    "killed", "death toll", "aftermath", "response", "fallout", "ruling",
    "decision", "settlement", "reopened", "recalled", "expanded", "closed",
    "investigation", "cause", "lawsuit", "sues", "sued",
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
            if similarity > 0.55:
                if has_update_indicators(title_lower):
                    break
                is_duplicate = True
                print(f"  Cross-day dup removed: '{candidate['title'][:60]}...'")
                break
            candidate_words = extract_significant_words(title_lower)
            prev_words = extract_significant_words(prev_headline)
            shared = candidate_words & prev_words
            if len(shared) >= 4:
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


def fetch_article_text_cached(url):
    """Try fetching article text via Google's web cache as a paywall fallback."""
    try:
        from urllib.parse import quote
        cache_url = f"https://webcache.googleusercontent.com/search?q=cache:{quote(url, safe='')}"
        cache_headers = {**HEADERS, "Referer": "https://www.google.com/"}
        resp = requests.get(cache_url, headers=cache_headers, timeout=10)
        if resp.status_code != 200:
            return None
        return _extract_article_text(resp.text)
    except Exception:
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


# --- Claude API for single story selection ---

SYSTEM_PROMPT = """You are the editor-in-chief of The Daily Briefing, a national and world news digest. You select and summarize the most important stories. Write factual, detailed summaries in clean newspaper style. Present stories without political bias. When covering politically divisive topics, include perspectives from both sides. No editorializing, no emojis. Categories: politics, world, business, technology, science_health, other."""


def build_prompt(stories, target_date_str, existing_headlines=None):
    """Build the prompt for selecting and summarizing the single best new story."""
    story_blocks = []
    for i, story in enumerate(stories, 1):
        block = f"""[CANDIDATE {i}]
Headline: {story['title']}
Source: {story['source']}
URL: {story['url']}
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
        prev_list = "\n".join(f"- {h}" for h in prev_headlines[:30])
        prev_block = f"""
STORIES FROM PREVIOUS DAYS (avoid repeating unless major new development):
{prev_list}"""

    return f"""Date: {target_date_str}. Below are {len(stories)} candidate national/world news stories.

YOUR TASK: Select the SINGLE most important, newsworthy story that has NOT already been covered today. This is a breaking news wire -- we want the biggest developing story right now.

Selection criteria:
- Choose the story with the most national or global significance
- Prefer breaking or developing stories over routine news
- Ensure political neutrality: do not favor stories from one political perspective
- When the story involves a politically divisive topic, the summary MUST include perspectives from both sides
- CRITICAL: Do NOT select a story already covered today (see list below)
{dedup_block}
{prev_block}

{stories_text}

---

Return a single JSON object with:
- "category": one of "politics", "world", "business", "technology", "science_health", or "other"
- "headline": clear, factual headline
- "summary": 3-4 paragraphs, each 2-4 sentences. Use \\n\\n between paragraphs. Cover what happened, who is involved, why it matters. If politically divisive, include both sides.
- "source": publication name
- "url": direct link to the original article

Return ONLY the JSON object, no other text."""


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


def call_anthropic_single_story(stories, target_date_str, existing_headlines=None):
    """Send candidates to Anthropic and get the single best new story back."""
    global anthropic_call_count

    client = anthropic.Anthropic()
    user_prompt = build_prompt(stories, target_date_str, existing_headlines)

    print(f"\n--- Anthropic API Call (Story Selection) ---")
    print(f"  Model: {ANTHROPIC_MODEL}")
    print(f"  Candidates sent: {len(stories)}")

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

    if parsed and isinstance(parsed, dict) and "headline" in parsed:
        return parsed

    print(f"  Failed to parse story JSON. Raw: {raw_text[:300]}")
    return None


# --- Top Story (National and International) ---

GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss"

WORLD_NEWS_PREFERRED = [
    "apnews.com", "reuters.com", "bbc.com", "bbc.co.uk",
    "nytimes.com", "washingtonpost.com", "theguardian.com",
    "cbsnews.com", "nbcnews.com", "abcnews.go.com",
    "aljazeera.com", "npr.org", "politico.com", "bloomberg.com",
    "foxnews.com", "wsj.com",
]

WORLD_NEWS_NOISE = [
    "tmz.com", "eonline.com", "people.com", "usmagazine.com",
    "buzzfeed.com", "dailymail.co.uk", "pagesix.com",
]

TOP_STORY_SYSTEM_PROMPT = """You are the editor-in-chief of The Daily Briefing, a national and world news digest. You write detailed, factual summaries of major news stories. Present all topics with political neutrality. When covering politically divisive topics, include perspectives from both sides. Clean newspaper style. No editorializing, no emojis."""


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


def cluster_headlines(entries):
    """Group RSS entries by topic similarity."""
    clusters = []
    used = set()

    for i, entry in enumerate(entries):
        if i in used:
            continue
        cluster = [entry]
        used.add(i)
        for j, other in enumerate(entries):
            if j in used:
                continue
            sim = SequenceMatcher(
                None, entry["title"].lower(), other["title"].lower()
            ).ratio()
            if sim > 0.35:
                cluster.append(other)
                used.add(j)
                continue
            words_a = extract_significant_words(entry["title"])
            words_b = extract_significant_words(other["title"])
            shared = words_a & words_b
            if len(shared) >= 3:
                cluster.append(other)
                used.add(j)
        clusters.append(cluster)

    return clusters


def score_cluster(cluster):
    """Score a cluster by source diversity and news quality."""
    sources = set(e["source"] for e in cluster)
    preferred_count = sum(
        1 for s in sources
        if any(p in s.lower() for p in ["ap ", "reuters", "bbc", "nyt", "washington post",
                                         "cbs", "nbc", "abc", "guardian", "npr", "bloomberg",
                                         "fox news", "wall street"])
    )
    noise_count = sum(
        1 for e in cluster
        if any(n in e.get("link", "").lower() for n in WORLD_NEWS_NOISE)
    )
    return len(sources) * 2 + preferred_count - noise_count * 3


def _summarize_top_story(article_texts, top_cluster, story_type="national", using_snippet_fallback=False):
    """Summarize a top story cluster via Claude. Returns dict or None."""
    global anthropic_call_count

    anthropic_call_count += 1
    if anthropic_call_count > MAX_ANTHROPIC_CALLS_PER_RUN:
        print(f"  LIMIT: Anthropic call cap reached. Skipping {story_type} top story.")
        return None

    source_blocks = []
    for i, at in enumerate(article_texts, 1):
        source_blocks.append(f"[SOURCE {i}: {at['source']}]\n{at['text']}")
    sources_text = "\n\n".join(source_blocks)

    representative_headline = top_cluster[0]["title"]

    bias_instruction = "Present the story with political neutrality. If the topic is politically divisive, include perspectives from both sides."

    if using_snippet_fallback:
        user_prompt = f"""Below are headlines and descriptions about the top {story_type} news story.

Topic: {representative_headline}

{sources_text}

---

Write a summary of this story. 2-3 paragraphs, each 2-4 sentences. Cover what happened, who is involved, why it matters. {bias_instruction}

Also write a clear, factual headline.

Return ONLY a JSON object with "headline" and "summary" (with \\n\\n between paragraphs). No other text."""
    else:
        user_prompt = f"""Below are {len(article_texts)} articles about the top {story_type} news story.

Topic: {representative_headline}

{sources_text}

---

Write a detailed summary. 3-4 paragraphs, each 2-4 sentences. Cover what happened, who is involved, the broader context, why it matters. {bias_instruction}

Also write a clear, factual headline.

Return ONLY a JSON object with "headline" and "summary" (with \\n\\n between paragraphs). No other text."""

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2000,
            system=TOP_STORY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = response.content[0].text
        usage = response.usage
        cost = (usage.input_tokens * 3 + usage.output_tokens * 15) / 1_000_000
        print(f"  Top {story_type} story: {usage.input_tokens + usage.output_tokens} tokens, ${cost:.4f}")

        result = _try_parse_json(raw_text)
        if result and "headline" in result and "summary" in result:
            return result

        print(f"  Failed to parse top {story_type} story JSON")
        return None
    except Exception as e:
        print(f"  Top {story_type} story summarization failed: {e}")
        return None


def _fetch_cluster_articles(sorted_entries, max_articles=3):
    """Fetch article text from entries in a cluster. Returns (article_texts, sources_used, using_fallback)."""
    article_texts = []
    sources_used = []

    for entry in sorted_entries[:8]:
        url = entry["link"]
        if "news.google.com" in url:
            url = resolve_google_news_url(url)
            if "news.google.com" in url:
                continue
        text = fetch_article_text(url)
        via_cache = False
        if not text or len(text) <= 200:
            text = fetch_article_text_cached(url)
            via_cache = True
        if text and len(text) > 200:
            article_texts.append({"source": entry["source"], "url": url, "text": text})
            sources_used.append({"name": entry["source"], "url": url})
            label = " (via cache)" if via_cache else ""
            print(f"    Fetched {len(text)} chars from {entry['source']}{label}")
            if len(article_texts) >= max_articles:
                break
        time.sleep(0.3)

    # Fallback to headlines + snippets
    using_fallback = False
    if not article_texts:
        print("  Could not fetch article text -- falling back to headlines + snippets")
        using_fallback = True
        for entry in sorted_entries[:8]:
            snippet = entry.get("snippet", "")
            url = entry["link"]
            if "news.google.com" in url:
                url = resolve_google_news_url(url)
            if snippet and len(snippet) > 30:
                article_texts.append({
                    "source": entry["source"],
                    "url": url if "news.google.com" not in url else entry["link"],
                    "text": f"Headline: {entry['title']}\n{snippet}",
                })
                if url and "news.google.com" not in url:
                    sources_used.append({"name": entry["source"], "url": url})
            elif entry["title"]:
                article_texts.append({
                    "source": entry["source"],
                    "url": url if "news.google.com" not in url else entry["link"],
                    "text": f"Headline: {entry['title']}",
                })
                if url and "news.google.com" not in url:
                    sources_used.append({"name": entry["source"], "url": url})
        if not sources_used:
            for entry in sorted_entries[:3]:
                sources_used.append({"name": entry["source"], "url": entry["link"]})

    return article_texts, sources_used, using_fallback


def fetch_top_stories(brave_key, target_date_str):
    """Identify and summarize the top national AND top international stories."""
    global brave_query_count

    # Step 1: Get Google News RSS entries
    entries = fetch_google_news_rss()
    if not entries:
        entries = []

    # Step 2: Supplement with Brave
    if brave_query_count < MAX_BRAVE_QUERIES_PER_RUN:
        brave_results = brave_news_search("top news today", brave_key, count=10, freshness="pd")
        time.sleep(0.3)
        for br in brave_results:
            if any(n in br["url"] for n in WORLD_NEWS_NOISE):
                continue
            entries.append({
                "title": br["title"],
                "link": br["url"],
                "source": br["source"],
                "pub_date": "",
                "snippet": br.get("snippet", ""),
            })

    if brave_query_count < MAX_BRAVE_QUERIES_PER_RUN:
        brave_intl = brave_news_search("world international news today", brave_key, count=10, freshness="pd")
        time.sleep(0.3)
        for br in brave_intl:
            if any(n in br["url"] for n in WORLD_NEWS_NOISE):
                continue
            entries.append({
                "title": br["title"],
                "link": br["url"],
                "source": br["source"],
                "pub_date": "",
                "snippet": br.get("snippet", ""),
            })

    # Step 3: Cluster
    clusters = cluster_headlines(entries)
    if not clusters:
        print("  No clusters found")
        return None, None

    scored = [(score_cluster(c), c) for c in clusters]
    scored.sort(key=lambda x: x[0], reverse=True)

    # Keywords suggesting US domestic vs international
    US_KEYWORDS = {
        "congress", "senate", "house", "president", "white house", "biden", "trump",
        "supreme court", "fbi", "doj", "pentagon", "capitol", "democrat", "republican",
        "governor", "federal", "american", "united states", "u.s.",
    }
    INTL_KEYWORDS = {
        "ukraine", "russia", "china", "europe", "nato", "un", "united nations",
        "middle east", "gaza", "israel", "iran", "india", "africa", "asia",
        "eu", "european", "putin", "zelensky", "minister", "prime minister",
        "bbc", "reuters", "guardian", "international",
    }

    def is_international(cluster):
        """Check if a cluster is about international (non-US) news."""
        text = " ".join(e["title"].lower() for e in cluster)
        intl_score = sum(1 for kw in INTL_KEYWORDS if kw in text)
        us_score = sum(1 for kw in US_KEYWORDS if kw in text)
        return intl_score > us_score

    # Find top national (US-focused) and top international clusters
    top_national_cluster = None
    top_intl_cluster = None

    for score, cluster in scored:
        if is_international(cluster):
            if top_intl_cluster is None:
                top_intl_cluster = cluster
                print(f"  Top international cluster ({len(cluster)} articles, score {score}):")
                for e in cluster[:3]:
                    print(f"    - [{e['source']}] {e['title'][:80]}")
        else:
            if top_national_cluster is None:
                top_national_cluster = cluster
                print(f"  Top national cluster ({len(cluster)} articles, score {score}):")
                for e in cluster[:3]:
                    print(f"    - [{e['source']}] {e['title'][:80]}")

        if top_national_cluster and top_intl_cluster:
            break

    # Summarize national story
    national_result = None
    if top_national_cluster:
        sorted_nat = sorted(
            top_national_cluster,
            key=lambda e: 1 if any(p in e.get("link", "").lower() for p in WORLD_NEWS_PREFERRED) else 0,
            reverse=True,
        )
        article_texts, sources_used, using_fallback = _fetch_cluster_articles(sorted_nat)
        if article_texts:
            national_result = _summarize_top_story(article_texts, top_national_cluster, "national", using_fallback)
            if national_result:
                national_result["sources"] = sources_used

    # Summarize international story
    intl_result = None
    if top_intl_cluster:
        sorted_intl = sorted(
            top_intl_cluster,
            key=lambda e: 1 if any(p in e.get("link", "").lower() for p in WORLD_NEWS_PREFERRED) else 0,
            reverse=True,
        )
        article_texts, sources_used, using_fallback = _fetch_cluster_articles(sorted_intl)
        if article_texts:
            intl_result = _summarize_top_story(article_texts, top_intl_cluster, "international", using_fallback)
            if intl_result:
                intl_result["sources"] = sources_used

    return national_result, intl_result


def write_output(existing_data, new_story, date_str, date_formatted,
                 top_national=None, top_international=None):
    """Write/update the JSON data file. Prepends new story to existing stories."""
    if existing_data:
        output = existing_data
    else:
        output = {
            "date": date_str,
            "dateFormatted": date_formatted,
            "stories": [],
        }

    # Add timestamp to new story
    now = datetime.datetime.now(DENVER_TZ)
    time_label = now.strftime("%-I:%M %p MT")
    iso_label = now.isoformat()

    if new_story:
        new_story["addedAt"] = time_label
        new_story["addedAtISO"] = iso_label
        # Prepend (newest first)
        output["stories"].insert(0, new_story)

    # Update top stories (they can change throughout the day)
    if top_national:
        top_national["updatedAt"] = time_label
        output["topNationalStory"] = top_national
    if top_international:
        top_international["updatedAt"] = time_label
        output["topInternationalStory"] = top_international

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

    # Step 2: Deduplicate, rank, and filter
    new_story = None
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
            stories_with_text = fetch_articles(top_stories[:10])  # top 10 is enough for single selection

            # Step 4: Select and summarize the single best story
            print(f"\n[Step 4] Selecting best new story via Anthropic...")
            new_story = call_anthropic_single_story(
                stories_with_text, target_date_str, existing_headlines
            )
            if new_story:
                print(f"  Selected: [{new_story.get('category', '?')}] {new_story['headline'][:70]}")
            else:
                print("  No new story selected")
        else:
            print("  No candidates after filtering")

    # Step 5: Fetch top national and international stories
    print("\n[Step 5] Fetching top national and international stories...")
    top_national, top_international = fetch_top_stories(brave_key, target_date_str)
    if top_national:
        print(f"  Top national: {top_national['headline'][:70]}...")
    else:
        print("  Top national: skipped (failed)")
    if top_international:
        print(f"  Top international: {top_international['headline'][:70]}...")
    else:
        print("  Top international: skipped (failed)")

    # Step 6: Write output
    if not new_story and not top_national and not top_international:
        print("\nNo new content to add. Exiting.")
        sys.exit(0)

    print("\n[Step 6] Writing JSON output...")
    filepath = write_output(
        existing_data, new_story, target_date_str, target_formatted,
        top_national=top_national, top_international=top_international,
    )

    print("\nDone!")
    return filepath


if __name__ == "__main__":
    main()
