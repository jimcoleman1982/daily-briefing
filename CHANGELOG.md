# Changelog

## v2026.4.16

Major overhaul of story sourcing and dedup logic. The digest had drifted toward a narrow pool of wire-service and center/left sources (5.6% right-leaning vs 20.5% left across 13 days), and the aggressive cross-day dedup was labeling 61% of stories "Update:" on multi-day news cycles.

**Changes:**

- **Topic-rotated Brave queries.** Replaced 9 fixed broad queries with 2 baseline + 3-4 topic-bucket queries that rotate by the run's Denver hour (politics/courts, economy, world hotspots, tech, science/health, culture/sports, right-leaning sites, wire-deep). Every run hits a different angle instead of the same popular-headline pool.
- **Named publisher RSS feeds.** Added direct RSS fetching from 20 named publishers (BBC, NPR, NYT, Guardian, Al Jazeera, Fox News [2 feeds], WSJ-adjacent, NY Post, National Review, Washington Examiner, Washington Times, Free Beacon, etc.). No Brave budget cost. Dramatically increases candidate diversity and right-leaning representation (feed mix: 40% C, 25% L, 35% R).
- **Source lean tagging in the Claude prompt.** Every candidate is now tagged `[L]`, `[C]`, or `[R]` with a lean-count summary. The prompt explicitly instructs Claude to prefer right-leaning or wire sources when multiple candidates cover the same event, and to flag historical over-indexing on left/center.
- **Claude-based stale detection.** Replaced the word-matching heuristic in `post_claude_dedup` (which was flagging nearly every repeat as "Update:" because common words like "new" or "today" appear in every summary) with a batched Claude Haiku call that classifies borderline cases as `new`, `update`, or `stale`. Added a `disposition` field to the main selection schema so Claude pre-classifies each story.
- **Loosened clustering threshold.** Dropped the keyword-overlap threshold in `deduplicate_and_rank` from 4 to 2 shared words. Previously most stories had sourceCount=1 even when 5+ outlets were covering them; now the multi-source signal actually works.
- **Removed dead code.** Deleted `fetch_top_stories` and its helpers (`cluster_headlines`, `score_cluster`, `_summarize_top_story`, `_fetch_cluster_articles`, `fetch_article_text_cached`, `TOP_STORY_SYSTEM_PROMPT`, `WORLD_NEWS_PREFERRED`, `WORLD_NEWS_NOISE`) -- ~300 lines of orphaned logic that was never called from main() and whose output keys were actively stripped by `write_output`.

## v2026.4.13

Fix stale/repeated stories appearing in the daily digest.

**Root cause:** Brave Search was returning homepage and section-page URLs (e.g. `apnews.com/`, `axios.com/`, `foxnews.com/`). The script fetched these pages, extracted text containing multiple stories -- including old ones still featured on the homepage -- and Claude picked from that text. Pre-Claude dedup couldn't catch these because it compared Brave's generic page titles, not the headlines Claude would ultimately generate.

**Changes:**

- Add `is_article_url()` + `filter_homepage_urls()` -- filters out homepage and section-page URLs before they become candidates. Only URLs with article slugs, date patterns, or long hash IDs pass through. This eliminates the #1 source of stale stories.
- Add `post_claude_dedup()` -- a final dedup pass that runs AFTER Claude generates headlines, comparing them against the previous 3 days. Stale repeats are removed; stories with genuine new developments get an "Update: " prefix.
- Tighten cross-day dedup thresholds -- similarity threshold lowered from 0.55 to 0.45, keyword overlap from 4 to 3 shared words.
- Trim `UPDATE_INDICATOR_WORDS` -- removed overly common words (confirmed, closed, response, fallout, killed, etc.) that were false-positive-ing nearly every headline through the update bypass.
- Strengthen Claude's prompt -- added explicit freshness check instruction, stricter previous-days rules, and "Update: " headline prefix requirement for follow-up stories.

## 1.0.0 (2026-04-12)

Initial versioned release. The site has been running since April 4, 2026. This release captures the current state as v1.0.0.

- Automated 6x/day national and world news digest
- Brave Search API + Google News RSS for news gathering
- Claude Sonnet 4.6 for curation and multi-paragraph summarization
- Balanced left/center/right source strategy
- Multi-pass deduplication with update-detection keywords
- Overnight catch-up mode on first daily run
- Cookie-based read tracking (iOS home screen persistent)
- Deep linking support for individual stories
- 14-day rolling JSON archive
- DST-aware dual cron schedules
- Custom domain: 101news.org
- README with setup docs, cost estimate, and architecture
