# Decisions (2026-09-25)

`RESEARCH_PLAN.md` is the full researched and fact-checked plan. This file records how that plan was
adapted to Shane's answers. **Where the two disagree, this file wins.**

## Shane's answers
- **Notes:** Apple Notes going forward, plus an old **paper notebook**. The notebook is photographed once and read by Claude vision.
- **App form:** a **local** app on the Mac. That means a Python pipeline and a small local web page for dropping in screenshots and photos, reviewing extractions, and viewing the dashboard. Nothing is published.
- **GHIN:** none. The Handicap Index is self-computed and always labelled *unofficial*. No GHIN import.
- **History:** **under 30 rounds**.

## Consequences
1. **Data sources, in priority order**
   1. **18Birdies official export** (`18Birdies_archive.json`, downloaded by hand at 18birdies.com/download-account-data/). It is the source of truth for per-hole strokes and round totals.
   2. **Screenshots + Claude vision.** They add per-hole putts, FIR, GIR, penalties and par/SI. They are also a full **fallback**: a round seen only in screenshots becomes a `source='screenshot'` round (`round_id='ss-<hash>'`). When a later export contains the same round (same local date + club, strokes agree), the export's strokes win, the screenshot stats are kept, and the `ss-` round is merged into the export round.
   3. **Apple Notes**, from the iCloud folder `Golf`, via JXA/osascript.
   4. **Notebook photos**: transcription plus event extraction. **Every notebook event needs review**; none are auto-accepted.
   5. **`golf add`** for quick manual entries.
2. **Lesson impact** (n < 30): **descriptive only**.
   - Before/after windows of score differentials (to-par for unrated courses), with bootstrap intervals.
   - A minimum detectable effect (MDE) computed from Shane's own σ, displayed prominently.
   - Focus-matched secondary stats.
   - Regression-to-the-mean and confounding caveats.
   - **No PyMC / Bayesian ITS for now.** It can be added once there are more than 40 rounds.
3. **No Batch API** at this scale. Per-call cost is logged, and re-runs are served from the `llm_calls` cache.
4. **Model** is the config value `[llm].model`, default `claude-opus-5`. `golf eval` compares models on the golden set once it exists; Shane picks the default.
5. **MCP:** a local, read-only MCP server over `golf.db` (`golf mcp`), so Claude Desktop or Claude Code can query rounds and the timeline. This is the realistic version of "tap into 18Birdies via MCP", since 18Birdies has no API or MCP.
6. **Privacy**
   - Everything personal lives under `data/`, which is gitignored.
   - The export parser drops account, friend, feed and subscription sections at ingest. `rounds.raw` keeps only the round record.
   - No external network calls, except:
     - the Anthropic API, at extraction time
     - OpenGolfAPI, only when Shane runs `golf courses autofill`
   - The dashboard HTML is self-contained, with no CDN.
7. **The screenshot prompt is provisional** until Shane's real screenshots arrive. The screen guide lives in its own prompt section so it can be rewritten from real captures. `prompt_version` is bumped on every change.
