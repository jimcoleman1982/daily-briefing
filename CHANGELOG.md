# Changelog

## v2026.4.24

Fix within-day duplicate stories, add daily cap, and introduce a "breaking news" urgency flag with a siren icon on the site.

**Context:** On 2026-04-24 the digest showed the Supreme Court military contractors ruling 4 times and the Iran leadership vacuum story twice within a single day's output. Root cause was a gap in the dedup logic: pre-Claude dedup compared Brave's raw titles against existing headlines, but raw titles differ enough from Claude's rewritten headlines that similarity matching missed re-emerging stories. post_claude_dedup then only checked against PRIOR days, not within-day, so Claude-written near-duplicates slipped through.

**Changes:**

- **Within-day dedup fix in post_claude_dedup.** The function now accepts `existing_today_headlines` and checks candidates against them FIRST, before any other branching. Same-day matches are always dropped regardless of Claude's `disposition` (an update to a story already in today's digest is still a duplicate). The "Update:" prefix is stripped before comparison so prefixed stories still match their unprefixed predecessors.
- **MAX_STORIES_PER_DAY = 36 hard cap.** The pipeline short-circuits out of main() before any API calls once today's JSON has 36+ stories. `determine_stories_needed` rewritten to respect the cap: returns 0 at/above cap, scales down pacing as the cap approaches so the day doesn't all cluster into the first few runs. Bumped MAX_STORIES_PER_RUN from 5 to 6 and STORIES_SOFT_CAP from 25 to 30 to fit the new daily budget cleanly across 6 runs.
- **Breaking-news flag.** Added a `breaking: true/false` field to Claude's selection schema with strict criteria: only urgent, high-stakes developing stories qualify (mass casualty events, active shooters, major attacks, assassinations, coups, declarations of war, major natural disasters with loss of life, pivotal political events). Explicitly NOT for routine news, policy announcements, or court rulings. The prompt instructs Claude to be very selective, typically zero or one story per day.
- **Siren icon on the site.** `index.html` renders a 🚨 emoji (with a CSS pulse animation and red drop-shadow glow) in place of the normal diamond bullet when `story.breaking === true`. The headline text turns dark red with 600-weight when breaking. Read stories dim the siren and stop its animation.
- **Manual cleanup of today's JSON.** Removed 4 existing duplicates from data/2026-04-24.json (3 SCOTUS copies + 1 Iran copy) so the live site stops showing them immediately.

## v2026.4.16

Major overhaul of story sourcing and dedup logic. The digest had drifted toward a narrow pool of wire-service and center/left sources (5.6% right-leaning vs 20.5% left across 13 days), and the aggressive cross-day dedup was labeling 61% of stories "Update:" on multi-day news cycles.

**Changes:**

- **Topic-rotated Brave queries.** Replaced 9 fixed broad queries with 2 baseline + 3-4 topic-bucket queries that rotate by the run's Denver hour (politics/courts, economy, world hotspots, tech, science/health, culture/sports, right-leaning sites, wire-deep). Every run hits a different angle instead of the same popular-headline pool.
- **Named publisher RSS feeds.** Added direct RSS fetching from 20 named publishers (BBC, NPR, NYT, Guardian, Al Jazeera, Fox News [2 feeds], WSJ-adjacent, NY Post, National Review, Washington Examiner, Washington Times, Free Beacon, etc.). No Brave budget cost. Dramatically increases candidate diversity and right-leaning representation (feed mix: 40% C, 25% L, 35% R).
- **Source lean tagging in the Claude prompt.** Every candidate is now tagged `[L]`, `[C]`, or `[R]` with a lean-count summary. The prompt explicitly instructs Claude to prefer right-leaning or wire sources when multiple candidates cover the same event, and to flag historical over-indexing on left/center.
- **Claude-based stale detection.** Replaced the word-matching heuristic in `post_claude_dedup` (which was flagging nearly every repeat as "Update:" because common words like "new" or "today" appear in every summary) with a batched Claude Haiku call that classifies borderline cases as `new`, `update`, or `stale`. Added a `disposition` field to the main selection schema so Claude pre-classifies each story.
- **Loosened clustering threshold.** Dropped the keyword-overlap threshold in `deduplicate_and_rank` from 4 to 2 shared words. Previously most stories had sourceCount=1 even when 5+ outlets were covering them; now the multi-source signal actually works.
- **Removed dead code.** Deleted `fetch_top_stories` and its helpers (`cluster_headlines`, `score_cluster`, `_summarize_top_story`, `_fetch_cluster_articles`, `fetch_article_text_cached`, `TOP_STORY_SYSTEM_PROMPT`, `WORLD_NEWS_PREFERRED`, `WORLD_NEWS_NOISE`) -- ~300 lines of orphaned logic that was never called from main() and whose output keys were actively stripped by `write_output`.

**Hardening pass (post-review fixes):**

- **Stable query rotation.** Replaced `hash(bucket_name)` with a per-character sum. Python's builtin `hash()` is randomized per-process, so the intended day-of-year rotation was silently non-deterministic across cron runs.
- **RSS date bounds.** Drop items with unparseable `pubDate` (previously kept) and items dated more than 2h in the future (TZ bugs / scheduled posts). Broadened `_try_parse_date` exception handling (`parsedate_to_datetime` can raise `IndexError` on certain malformed inputs).
- **Lean-tag URL fallback fix.** `get_source_lean()` returns `"?"` (truthy) for unknown sources, so the `or` fallback to URL never fired. Replaced with explicit `if lean == "?":` check. Sources with unknown names but known URLs now get correctly tagged.
- **Classifier hardening.** Promoted Haiku model ID to `ANTHROPIC_CLASSIFIER_MODEL` constant. Made fallback paths explicit: non-list responses, count mismatches, and API errors each log clearly and return the safe `"update"` default. Wrapped `disposition` in `str()` to prevent crash on non-string values Claude might hallucinate.
- **Cold-start consistency.** `post_claude_dedup` now applies Claude's `disposition` (stale → drop, update → prefix) even when no previous-days data exists, matching main-path behavior.
- **Softened contradictory prompt wording.** Previous wording pushed both "do not favor a political side" and "prefer right-leaning sources." Now clearly separated: story-topic selection is neutral; among candidates covering the same event with comparable quality, prefer [R]/[C] to correct for historical left/center over-indexing.

Verified with 49 tests across unit and integration harnesses + adversarial code review.

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
