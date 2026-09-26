# Golf Analytics App: Architecture Plan (2026-09-25)

**TL;DR**
- There is no live "tap into 18Birdies" today. 18Birdies has no API, no MCP server and no partner program for individuals.
- 18Birdies does have an official self-serve export, `18Birdies_archive.json`. It holds every round with per-hole strokes and round-level FIR/GIR/putts totals. That export is the backbone of the plan.
- Screenshots plus Claude vision become a narrow add-on: per-hole putts/FIR/GIR, and par/stroke index once per course. The export's totals serve as a free checksum on what vision reads.
- Notes come from an iCloud Apple Notes "Golf" folder through a small JXA script. An LLM extracts events, but it only describes dates; Python works out the actual dates.
- The "MCP" part is Shane's own local MCP server over `golf.db`.
- Everything runs locally on the Mac. By default nothing is published.

---

## 1. Bottom line on "tap straight into 18Birdies / MCP"

Ranked by what is actually possible today:

| # | Path | What you get | Evidence status | Verdict |
|---|---|---|---|---|
| 1 | **Official export**: sign in at https://18birdies.com/download-account-data/ and "Request My Data" downloads `18Birdies_archive.json` immediately | All rounds. Each has id, epoch-ms timestamp, club UUID, gross strokes, score to par, `holeStrokes[9 or 18]`, and round-level stats: FIR/GIR hit and miss directions, putts, scoring distribution. There is **no** per-hole par, putts, FIR or GIR, no tee, no rating/slope, no notes, and no practice log. | **Confirmed** from the site's own JS bundle and help article 780 (updated Jul 2, 2026). The schema is **confirmed** from a real Dec 2025 archive and from parsers dated Jun and Aug 2026. Whether a live 2026 file still matches is **unconfirmed**. | **Primary source.** Sanctioned, and each download is a full snapshot. |
| 2 | **Your own local MCP server over `golf.db`** | Claude Desktop or Claude Code can query your rounds, stats and timeline | Works today. MCP Python SDK v2.2.0 (2026-09-07) uses `from mcp.server import MCPServer`; I checked this in the SDK README today. | **This is the realistic "MCP" answer.** Build it in Phase 4. |
| 3 | **Screenshots + Claude vision** | Per-hole putts, FIR, GIR, penalties, chips, sand; per-hole par and stroke index | The API facts are **confirmed**. The current 18Birdies screen layouts are **unconfirmed**: the help-center images date from 2019 to 2025 and one round-summary redesign has already happened. | **Supplement only**, for rounds and fields the export lacks |
| 4 | **Strava official MCP** (18Birdies auto-shares rounds to Strava since Jun 16, 2026) | Future rounds with totals only: course, gross, birdie/par/bogey counts, duration. Past rounds can be shared one at a time. | Auto-share and the MCP are **confirmed**. It needs a paid Strava subscription. Access to the photo or scorecard graphic is **unconfirmed** and likely absent. Strava's API Policy §5.3 (Jun 1, 2026) forbids putting Strava API data into any AI context outside its own MCP. | **Skip** unless Shane already pays for Strava. Do **not** build a Strava API + vision pipeline. |
| 5 | **Scripted export** (replay the site's `userLogin` → `getMyData` calls) | Same file as #1, fetched unattended | Endpoints read from JS but **never executed**. Token lifetime is unknown. It needs a password login; phone + SMS login can't run unattended. The ToS (Jan 29, 2021) bans "automated means" unless authorized in writing. | **Don't**, unless Shane gets written permission through the partnership form (help article 771, Jun 19, 2026). Default to downloading by hand on a reminder. |
| 6 | **GHIN unofficial API** | Only rounds Shane posted to GHIN, but with official differential, PCC, tee name and HI history | 18Birdies does **not** sync to GHIN (confirmed, Jul 24, 2026). Evidence the API still works runs only to **Apr 2026**. Automated access is ToS-prohibited. | Only if he posts to GHIN **and** accepts the gray area. Fallback: paste a DevTools token by hand. |
| 7 | Public API / partner API / reverse-engineered mobile API / Apple Health / Garmin / TheGrint etc. | Nothing usable | None exists. Reverse engineering is barred by ToS §8.9. Health holds no scores. | **Not viable.** |

On the MCP registries: the official MCP registry returns 0 results for "18birdies". The only community server on Glama (chrisdecali/golf-reports) just parses the export file, has a wrong course-name join key, and has no license. Use it as a reference only.

---

## 2. Recommended data-ingestion strategy

### Primary: official export, downloaded by hand
1. Shane signs in himself at https://18birdies.com/download-account-data/ with **email + password or phone + SMS code**. The web form (per its JS) has no Apple, Google or Facebook sign-in. The sources conflict on whether the app offers Apple/Google sign-in. If he signs into the app only with Apple or Google, he should first add an email or phone number in the app's Settings.
2. He saves the file as `~/Desktop/golf-analytics/data/raw/18birdies/18Birdies_archive_YYYYMMDD.json`. This path is gitignored. The file holds his email, phone, birth year, friends list and payment status.
3. `golf import-18b <file>` upserts rounds by 18Birdies round id. Every snapshot is kept, so edits and deletions can be detected.
4. Cadence: download monthly, or before any analysis session. `golf status` prints "last export N days ago".

Claude never signs in to 18Birdies and never stores his 18Birdies password.

### Parsing rules (corrected after fact-check)
- Join course names on `rounds[].clubId.id == playedClubs[].clubId`. The key is `clubId`, **not** `id`.
- `clubId` identifies a facility, not a course. A multi-course facility needs a one-time manual mapping.
- `played_on_local` = epoch ms converted from UTC to `America/New_York` (configurable) **before** taking the date.
- `par_played = strokes − score`. Never use `abs(score)`; one existing parser does, and it breaks under-par rounds. `score` is relative to the par of the holes actually played.
- Entry-mode classification:

| Condition | entry_mode | Use |
|---|---|---|
| `strokes == 0` | `abandoned` | Drop from analysis; keep the raw record |
| `strokes > 0` and `sum(holeStrokes) == 0` | `total_only` (full round entered by total; 6 of 8 sum-mismatches in the sample) | Holes = `len(holeStrokes)`. Include in score trends; exclude from per-hole analysis. |
| Non-zero holes count ∈ {9, 18} and sum == strokes | `hole_by_hole` | Everything |
| 18-length array with exactly 9 non-zero at positions 1–9 or 10–18 | `hole_by_hole`, `nine = front/back` | 9-hole rules (§5) |
| 9-length array | `hole_by_hole`, `nine = unknown` | Needs a manual front/back mapping for stroke index |
| Other non-zero counts | `partial` | Exclude from WHS; flag |
| Non-zero holes and sum ≠ strokes | Flag `sum_mismatch` | Trust `strokes` for totals; exclude from per-hole analysis |

- In stats, `strokeGain* == 100` is a sentinel for "not available", stored as NULL.
- `puttHoleCount` is unreliable: it was 0 while putts > 0 in 44 of 75 sample rounds. Ignore it.
- "Tracked vs zero": FIR is tracked iff `fairwayHoleCount > 0`, GIR iff `girHoleCount > 0`, putts iff `putts > 0`.
- Validate with Pydantic using `extra='allow'`. Log every unknown key, including any new top-level section such as a practice log, and fail loudly if a required key is missing.

### Course reference (`courses.yaml`, committed, hand-verified)
- For each club: course, the default tee Shane plays, CR/Slope (18, plus front and back if available), and per-hole par, stroke index and yards. Also record `rating_source`, `checked_on` and `effective_from`.
- Autofill par, stroke index and yards from **OpenGolfAPI** (keyless, ODbL; attribution required). Then check the tee he actually plays against the physical scorecard, the 18Birdies club page (viewed by hand; **never scripted**, because of the ToS), or USGA NCRDB (browse by hand; it blocks scripts).
- The sources disagree: for the same tee, OpenGolfAPI shows 71.2/132 and 18Birdies shows 72.2/136.
- GolfCourseAPI (free tier, 35 requests/day) is the fallback, but Shane must sign up himself and its license forbids publishing the data.
- **Automatic sanity check:** for complete rounds, Σ par from `courses.yaml` must equal `strokes − score` from the export. A mismatch means the course or tee mapping is wrong.
- Tee is unknown in the export. Use the per-club default, with a per-round override table.

### Fallbacks and add-ons
- **Per-hole stats:** screenshots (§3). Only needed if Shane logs putts, FIR and GIR per hole.
- **Rounds since the last export:** download again (the cheapest option). Use screenshots only for per-hole stats.
- **Notes:** Apple Notes via JXA (§4). Adapters for Google Docs, Notion or Markdown if his notes live elsewhere.

### What Shane must provide or do
| When | Action |
|---|---|
| Phase 0 | Download the export once and drop it in `data/raw/18birdies/`. Tell me his sign-in method. |
| Phase 0 | Capture one recent round's screens: Round Summary; Full Scorecard in **Scores** and **Stats** views, both portrait swipes and one landscape; Basic Stats / Benchmarks. These are for prompt design. |
| Phase 1 | Confirm the course and tee for each club that `golf courses check` lists (about 5 minutes per club). |
| Phase 2 | Move golf notes into one **iCloud** Notes folder named `Golf` ("On My iPhone" notes never reach the Mac). Accept the one-time macOS prompt "Terminal wants to control Notes". Write `entities.yaml` (coaches with nicknames/initials, practice locations, current bag). |
| Ongoing | Download the export monthly. Add future lesson and practice lines through the iPhone "Golf log" Shortcut (§4) so each line carries an ISO date. |
| API | Set up an Anthropic API key with `ant auth login` or in a gitignored `.env`. |
| Optional | GHIN credentials (he enters them into the macOS Keychain himself); GolfCourseAPI signup; the partnership-form request for scripted export. |

---

## 3. Screenshot extraction design

**Scope, narrowed by the export.** Per-hole strokes, date, club and round totals already come from the export. Vision only needs:
- **(a)** per-hole par and stroke index, once per course and tee, if OpenGolfAPI or the scorecard doesn't already cover them
- **(b)** per-hole putts, fairway result, GIR, penalties, chips and sand from the Full Scorecard **Stats** view, for rounds where he tracked them

### Capture protocol
- **Per course/tee, once:** Full Scorecard, Scores view, showing the Par and Handicap rows. Use landscape if all holes fit; otherwise portrait swipes that overlap by at least one hole.
- **Per round:** Full Scorecard, Stats view: 1–2 landscape shots or 2–3 portrait swipes. Add the top of the Round Summary if the Stats header doesn't show date and course (**unconfirmed** whether it does). Capturing Benchmarks is optional, because the export's totals replace it as the checksum.
- Don't crop or zoom. On iOS 26, HDR screen captures are saved as **HEIC**, so ingest sniffs the file type and converts to PNG with `pillow-heif` or `sips`.
- Delivery: AirDrop, or `iCloud Drive/Golf/Inbox/`. Phase 5 can auto-pull with `osxphotos query --screenshot` (I confirmed today that the flag exists in osxphotos v0.77.1).

### Schema
All fields are required and none are nullable. Unknowns use `-1`, `""`, or explicit enum values. This keeps the schema at 0 optional and 0 union parameters, well under the structured-output limits of 24 optional and 16 union-typed parameters.

```python
Conf = Literal["high","medium","low"]
HoleField = Literal["par","si","strokes","symbol","putts","fairway","gir","gir_miss","penalties","chips","sand"]

class ImageInfo(BaseModel):
    image_index: int
    screen_type: Literal["round_summary","scorecard_scores","scorecard_stats","benchmarks","share_scorecard","other"]
    orientation: Literal["portrait","landscape"]
    first_hole: int; last_hole: int          # -1 if none
    problems: str                            # "" if none

class HoleRow(BaseModel):
    hole: int
    par: int; par_alt: int                   # "4/5" men/women pairs; -1 if absent
    si: int; si_alt: int; yards: int
    strokes: int                             # read independently; never copied from export
    symbol: Literal["none","circle","double_circle","square","double_square","max_star","not_visible"]
    putts: int; penalties: int; chips: int; sand: int          # -1 = not recorded / not visible
    fairway: Literal["hit","left","right","short","long","miss","not_applicable","not_recorded","not_visible"]
    gir: Literal["hit","miss","not_recorded","not_visible"]
    gir_miss: Literal["left","right","short","long","no_chance","none","not_visible"]
    stats_visible: bool                      # distinguishes not-recorded from not-captured
    confidence: Conf
    uncertain_fields: list[HoleField]
    source_images: list[int]

class Displayed(BaseModel):                  # printed values, transcribed not computed; -1/"" if absent
    gross: int; front: int; back: int; to_par_text: str
    total_putts: int; fairways_hit: int; gir: int; penalties: int

class ExtractedRound(BaseModel):
    images: list[ImageInfo]
    player_name: str; other_players: list[str]
    course_text: str; tee_text: str; date_text: str; date_iso: str
    course_par: int; rating_text: str
    holes: list[HoleRow]                     # empty if no hole grid visible
    displayed: Displayed
    overall_confidence: Conf
    issues: list[str]
```

### Call pattern (hedged against verified SDK behavior)
- Use `client.messages.create(..., output_config={"format": {"type": "json_schema", "schema": WIRE_SCHEMA}, "effort": "high"}, thinking={"type": "adaptive"})`.
  - Generate `WIRE_SCHEMA` once with the SDK's schema-transform helper and commit it as `schemas/extracted_round.v1.json`. Confirm the helper's import path at build time.
  - Parse the text with a lenient Pydantic model that has case-insensitive enum `BeforeValidator`s.
  - **Don't use `messages.parse()`.** It raises on any validation miss, such as enum case drift, and loses the raw output. The same code path then serves the Batch API.
- Store the raw JSON, model, `prompt_version`, `schema_version`, `usage` and cost for every run.
- Check `stop_reason` for `max_tokens` or `refusal` before parsing.
- **Before writing anything else, make one real call to confirm the schema compiles** (no "Schema is too complex"). Fallback: split into two calls, one for the grid and one for stats.
- Send images in native resolution, labelled `Image N:`, with images before the text. **Never stitch swipes into one image**: a 1206×6000 strip is downscaled to 518×2576.

### Prompt outline (system prompt frozen for caching; `prompt_version = round-v1`)
1. Role: transcribe 18Birdies screenshots into the schema. Accuracy beats completeness; never guess, use the sentinel and flag it.
2. Screen guide: Scores view (Par/Handicap rows; "x/y" = men's/women's values) and Stats view rows. **Build this from Shane's Phase 0 screenshots, not from the help-center images.**
3. Symbol legend: double circle / circle / plain / square / double square / red starburst "Max". Record what is visible, independent of par.
4. Rules:
   - Transcribe, don't compute.
   - `not_visible` means not captured; `not_recorded` means the cell is visible but empty.
   - Extract only the named player; list the others.
   - Merge overlapping swipes into one row per hole; on conflict, use the clearer image, mark confidence low and log it in `issues`.
   - Par 3 → fairway `not_applicable`.
   - Every unsure field goes into `uncertain_fields`.
   - Compare against printed totals and look again, but **never change a read value to make totals match**.
5. User turn: images, then "Extract the round for player <name>; expected <9|18> holes; played <date> at <club>."
   - **Do not pass the export's hole strokes or totals.** They are the independent checksum.

### Validation rules (deterministic, in code)
**Hard (E), any one sends the round to review:**
- **R1** Per-hole `strokes` == export `holeStrokes`. If it still mismatches after a targeted re-read, the export may be stale (the round was edited later): download it again.
- **R2** Checks against the export's totals, when tracked:
  - Σ putts == `stats.putts`
  - count(fairway == hit) == `fairwayMiddles`
  - count(left) and count(right) == `fairwayLefts` and `fairwayRights`
  - count(gir == hit) == `stats.gir`
  - GIR miss-direction counts == `girLefts`, `girRights`, `girShorts`, `girLongs`
- **R3** Per-hole par == `courses.yaml` (when mapped). Σ par == `strokes − score`.
- **R4** par ∈ {3,4,5,6}; 0 ≤ putts ≤ 6; putts ≤ strokes − 1; penalties, chips and sand each 0–6.
- **R5** Fairway `not_applicable` ⇔ par 3.
- **R6** Holes unique and complete. On 18-hole cards, stroke index values are distinct and cover 1–18.
- **R7** Score symbol consistent with strokes − par (skip `max_star`).

**Soft (W):**
- **W1** GIR agrees with strokes − putts ≤ par − 2. Warning only: fringe putts break it, and if 18Birdies' "Automatic Stats Calculation" setting is on, the GIR itself was derived, so the check is circular.
- **W2** strokes ≥ 1 + putts + chips + sand + penalties.
- **W3** Any uncertain field, low confidence, or non-empty `issues`.
- **W4** Benchmarks totals, if captured. Treat 18Birdies' up-and-down % as opaque: its published definition is inconsistent, so don't try to recompute it.

**Auto-accept:** no E, at most one W, and no low-confidence holes. The export checksums are very strong, so the auto-accept rate should be high. Measure it.

### Review flow
- States: `extracted → auto_accepted | needs_review → accepted | rejected`. Only accepted rounds feed per-hole stats.
- **Targeted re-read first:** for up to 5 flagged cells, send only the relevant image with a one-cell question and a tiny schema (about 4K tokens, ≈ $0.02). If both reads agree, accept; otherwise send it to Shane.
- **v1 review:** `data/review/rounds_pending.yaml` (round, hole, field, value, reason, image number). Shane edits it and runs `golf apply-review`.
- **v2 review:** a local FastAPI + htmx page (Phase 3b) with the screenshot on the left and an editable grid on the right; E cells red, W cells amber.
- Each correction is stored as `{extraction_id, hole, field, model_value, corrected_value, prompt_version, model}`. Corrected rounds become the golden set.

### Model choice and cost
- **Default model:** a config value, set to `claude-opus-5` (the established project default).
- The fact-check found that `claude-opus-5-5` (current since Sep 22, 2026, $4/$20) now lists Opus 5 as legacy. Opus 5.5's default effort is `medium`, so effort must be set explicitly. Its forced `tool_choice` returns 400, which doesn't matter here because we use `output_config.format`.
- **Recommendation:** once the 10-round golden set exists, run it on `claude-opus-5`, `claude-opus-5-5` and `claude-sonnet-5`, and let Shane choose the cheapest model that meets the accuracy bar. I won't downgrade models without his decision.

Per-round cost assumes 2–4 images at about 3,956 visual tokens each plus about 3K tokens of prompt, and 3–8K output tokens including adaptive thinking. The output figure is an estimate; log `usage` to measure it.

| Model | Per round (typical 3 imgs) | Range | 50–100-round backfill | Same via Batch API (−50%) |
|---|---|---|---|---|
| claude-opus-5 ($5/$25) | ~$0.18 | $0.13–0.29 | $9–18 (worst case ~$29) | $4.5–9 |
| claude-opus-5-5 ($4/$20) | ~$0.14 | $0.10–0.24 | $7–14 | $3.5–7 |
| claude-sonnet-5 ($2/$10) | ~$0.07 | $0.05–0.12 | $3.5–7 | $1.75–3.5 |

- Per-course par/SI captures: fewer than 10 calls in total, a negligible cost.
- Batches: key results by `custom_id`, never by order. The server-side `fallbacks` parameter isn't allowed in batches.
- The backfill is **optional**: only rounds where he tracked per-hole stats and cares about them.

---

## 4. Notes-to-timeline design

### Source connector
**Default: an in-house JXA script through `osascript` over the iCloud "Golf" folder.**
- It needs only the Automation permission: no Full Disk Access, no third-party code and no MCP.
- I checked the macOS 26.5.1 `Notes.sdef`: each note exposes `id`, `name`, `plaintext`, `creationDate`, `modificationDate`, `passwordProtected` and `shared`. It has **no** tags.
- Metadata is read in bulk (one Apple Event per property array):
  ```js
  const ns = Notes.folders.whose({name: "Golf"})()[0].notes;
  // ns.id(), ns.name(), ns.creationDate(), ns.modificationDate(), ns.passwordProtected()
  // body only for changed, unlocked notes: Notes.notes.byId(id).plaintext()
  ```
- Python calls it with `subprocess.run([...], capture_output=True, encoding="utf-8")`. Always decode as UTF-8, because apple-notes-to-sqlite has a mac_roman decoding bug. For robustness under launchd, the script emits ASCII-escaped JSON.
- **Locked notes:** store the metadata row with status `locked` and skip the body.
- **Run context:** interactive (`golf notes sync`). A launchd job would need an app wrapper for TCC, so that waits for Phase 5. GitHub Actions can never read Apple Notes.
- **First-run checks:** whether `whose({name})` includes nested sub-folders; duplicate "Golf" folders across accounts; iCloud freshness (keep Notes.app open, and re-sync if an iPhone edit was just made).
- **Alternatives, depending on where his notes live:**
  - Google Docs: Drive `files.export` as `text/markdown`. An OAuth app in "Testing" mode means re-authenticating every 7 days.
  - Notion: `GET /v1/pages/{id}/markdown`. Per-block `created_time` gives real per-entry dates.
  - Markdown folder: use front-matter or filename dates, or `git blame` for per-line dates.
  - Keep: Takeout JSON only.
  - kzaremski/apple-notes-exporter (GPL-3.0, v2.1, supports `--incremental`) is the ready-made option **only if** Shane grants Full Disk Access.

### Event schema
The researcher's schema had about 30 `Optional[...]` fields, roughly twice the 16-union limit, so it is **refuted as written**. This flattened version uses sentinels only (0 unions, 0 optional):

```python
class DateRef(BaseModel):
    expression: str                          # verbatim date words, "" if none
    kind: Literal["absolute","relative","header","none"]
    year: int; month: int; day: int          # -1 if absent
    time_hhmm: str                           # "" if absent
    anchor: Literal["note_created","entry_header","na"]
    header_line: int                         # -1 if n/a
    rel_kind: Literal["offset_days","weekday","period","none"]
    rel_value: int                           # offset days (yesterday=-1) or period offset (last week=-1); 0 valid when rel_kind set
    weekday: Literal["mon","tue","wed","thu","fri","sat","sun","none"]
    weekday_rel: Literal["last","this","next","unspecified","none"]
    period: Literal["week","weekend","month","year","none"]
    approximate: bool

class FocusArea(BaseModel):
    game_area: Literal["full_swing","driving","approach","short_game","bunker","putting",
                       "course_management","mental","physical","equipment"]
    detail: str                              # Shane's words, <=12 words

class Drill(BaseModel): name: str; description: str; dose: str
class Equipment(BaseModel):
    action: Literal["added","removed","replaced","adjusted","fitted","tested","considering"]
    category: Literal["driver","fairway_wood","hybrid","iron_set","iron","wedge","putter","ball","shaft","grip","shoes","launch_monitor","training_aid","other"]
    brand: str; model: str; specs: str; replaces: str
class Measurement(BaseModel): metric: str; value: str; unit: str; club: str
class Injury(BaseModel):
    body_part: str
    status: Literal["new","ongoing","improving","resolved"]
    severity: Literal["minor","moderate","severe","unknown"]
class SourceSpan(BaseModel): line_start: int; line_end: int; excerpt: str   # verbatim, <=300 chars

class TimelineEvent(BaseModel):
    event_type: Literal["lesson","practice","on_course","equipment_change","fitting","injury",
                        "fitness","goal","swing_thought","milestone","other"]
    date: DateRef
    is_planned: bool
    coach: str
    lesson_format: Literal["in_person","playing_lesson","remote_video","group_clinic","none"]
    location: str
    location_kind: Literal["range","course","indoor_sim","studio","home","gym","other","none"]
    duration_minutes: int
    focus_areas: list[FocusArea]; drills: list[Drill]; swing_thoughts: list[str]
    equipment: list[Equipment]; measurements: list[Measurement]
    injury: list[Injury]                     # 0 or 1 (list instead of Optional; enforce len<=1 in code)
    goal_text: str
    summary: str                             # one factual sentence, <=25 words
    source: SourceSpan
    confidence: Literal["high","medium","low"]
    review_reasons: list[str]

class EntryHeader(BaseModel): line: int; date: DateRef
class NoteExtraction(BaseModel):
    note_kind: Literal["single_entry","running_log","reference","mixed","not_golf"]
    log_order: Literal["chronological","reverse_chronological","unordered","na"]
    entry_headers: list[EntryHeader]
    events: list[TimelineEvent]
```

- **Prompt** (`prompt_version = tl-1`): the researcher's prompt, updated for sentinels. It covers event definitions, the grouping rule (one lesson = one event), running-log header handling, **"describe dates, never calculate"**, and one worked example. Context passed in: the note's created and modified datetimes in local time, and `entities.yaml` (coaches and aliases, locations, current bag). The hash of `entities.yaml` is part of the cache key.
- **Model:** same config default and same "make one compile test call first" rule. Cost is about 3.5K input and 1.5–3K output tokens per note: roughly $0.05–0.09 per note on Opus 5, or $0.02–0.04 on Sonnet 5. A 150-note backfill costs about $3–14, and half that through the Batch API.

### Date resolution (all in Python)
| Case | Rule | precision / source |
|---|---|---|
| `header` | Inherit the resolved entry header's date | header's precision |
| `absolute` with month and day but no year | Pick the year within [created − 180 d, modified + 7 d] (+180 d if planned); flag `year_inferred` | `day` (or `exact` with a time) / `explicit_text` |
| Running-log headers | Walk the headers in log order and keep dates monotone (≥ previous − 3 d) inside [created − 14 d, modified + 1 d]. This handles Dec→Jan rollover and flags `non_monotonic` | `day` |
| `relative` + offset_days | anchor + offset. The anchor is the note's created date, or the entry header if `anchor = entry_header` | `day` / `relative_reference` |
| `relative` + weekday | "last"/"unspecified" → strictly before the anchor (1–7 days). "next", or planned → strictly after. "this" → same Mon–Sun week. Flag `ambiguous_last_weekday` | `day` |
| `relative` + period | Monday-anchored week or calendar month, shifted by `rel_value`. Bounds go into `date_start` and `date_end` | `week` / `month` |
| `approximate` | Widen: day→week, week→month | — |
| `none` | Single-entry note → created date. Inside a log → between neighboring headers | `inferred` / `note_created` or `log_position` |

- **Cross-check:** `dateparser` with `RELATIVE_BASE` and `PREFER_DATES_FROM="past"`. If it disagrees beyond the precision width, flag `date_disagreement`.
- **Sanity flags:** `future_date` (after modified + 1 d and not planned), `far_past` (before created − 365 d), `created_unreliable` (many notes created in the same second, i.e. bulk imports). Any flag forces review.
- **Round linking:** `on_course` events whose resolved date equals an export round date get `linked_round_id`.

### Incremental sync (idempotent)
```
meta = jxa_meta("Golf")
for m in meta:
  if m.locked: upsert(status='locked'); continue
  if prev.modified_at == m.modified: continue                  # equality, not a cursor (late iCloud edits)
  text = normalize(jxa_body(m.id)); h = sha256(text)
  if prev.content_hash == h: touch(); continue
  upsert_doc(); queue(m.id)
mark_deleted(missing ids)                                      # soft; re-key if same created_at+hash reappears
for doc in queue:
  key = sha256(h|PROMPT_VERSION|SCHEMA_VERSION|MODEL|entities_hash)
  extr = cache.get(key) or call_claude(doc)                    # re-runs never re-bill
  reconcile(doc, resolve_dates(validate(extr, doc)))
```
- Every excerpt must be a substring of the note after collapsing whitespace. If not, fall back to a difflib window match (ratio ≥ 0.9); otherwise set `excerpt_verified = 0`.
- `event_id = sha256(doc_id | event_type | norm(excerpt) | ordinal)[:16]`, so an event keeps its id when other parts of the note are edited.
- Reconcile:
  - Same id → update the machine fields and keep the status.
  - New id → `pending`, or `auto_accepted`.
  - Missing id → `orphaned` if it was reviewed (and propose carrying the review over at excerpt similarity ≥ 0.8), otherwise `superseded`.
- `event_reviews` is a human overlay that the extractor never writes. Edits are merge-patches applied when read.
- Cross-note duplicates (same type, overlapping dates, same coach or equipment model) are flagged `possible_duplicate` and resolved with `merge`.

### Review
- `golf sync` prints, e.g., "14 new: 10 auto-accepted, 4 need review".
- `golf review events` shows the excerpt, the resolved date with its reasoning (`'last sat' ← header L004 → 2026-03-14 (day)`), fields and flags. Keys: accept / reject / edit ($EDITOR, YAML) / merge / skip / open in Notes.
- For the **first backfill, review everything.** Auto-accept is enabled only after the gold-set targets in Phase 2 are met. The policy: confidence high, excerpt verified, no flags, date source explicit or relative, not approximate. After that, spot-audit 10% of auto-accepted events.
- **Quick-add:**
  - `golf add "<text>" [--date] [--type --coach --focus]` creates a manual document, which is auto-accepted.
  - iPhone Shortcut: Ask for Input (text, then date) → Format Date `yyyy-MM-dd` → Append to Note "Golf Log" as `YYYY-MM-DD | text`.
  - `manual_events.yaml` holds remembered past events at month precision.

---

## 5. Analytics spec

### Metric availability tiers
| Tier | Needs | Metrics |
|---|---|---|
| A: every export round | Export only | Gross, to-par, front/back (hole_by_hole rounds), FIR% with L/R/short/long split, GIR% with miss directions, putts/round, birdie/par/bogey/double+ rates, rolling-20 mean/SD/MAD, the round-level identity split `to_par = [(strokes − putts) − (par − 2·holes)] + [putts − 2·holes]` ("to-green over regulation" vs "putts over two"; exact, and needs only round totals) |
| B: + `courses.yaml` | Per-hole par/SI, CR/Slope | Par-3/4/5 average to par, scoring by SI band, net-double-bogey AGS, differentials, unofficial HI, blow-up rate, hole-to-hole "tilt" (P(bogey+ \| previous bogey+) vs base rate, permutation test) |
| C: + screenshot rounds | Per-hole putts/FIR/GIR | 3-putt and 1-putt rates, putts per GIR, scrambling % (missed GIR → par or better), blow-up anatomy (precursor: penalty / missed FIR / missed GIR + failed scramble / 3-putt), two-state SG-lite against Shane's trailing-40-round baseline, hole-level mixed model `score−par ~ par_type + FIR + GIR + penalties + (1\|round)` (**associational only**) |

- Every rate gets a Wilson CI and an EWMA trend (λ ≈ 0.2–0.3).
- Label putts/round as "putts", never "putting skill": putt distances are unknown.
- **Not defensible, so don't build:** true strokes-gained categories (no distances), PGA Tour baselines, and a GHIN-exact HI.
- **Benchmarks:** Shot Scope (~2020) and Break X (Sep 2026) tables in version-controlled CSVs with a `source_url` column, used for a "stat-implied handicap" dot chart. The two vendors disagree, and the figures weren't fact-checked, so the dashboard shows which table is in use.

### WHS math (2024 Rules of Handicapping; verified in two passes; no newer edition found)
- **Course Handicap** = HI × Slope/113 + (CR − Par), rounded at the end. For 9 holes: (HI/2, rounded to 0.1) × Slope9/113 + (CR9 − Par9). Always use the HI in effect **before** the round, so recompute in date order.
- **Net double bogey cap** = par + 2 + strokes received on the hole, where strokes received = ⌊CH/18⌋ + (SI ≤ CH mod 18). Before an HI exists (< 54 holes), the cap is par + 5. With CH > 54 and 4+ strokes on a hole, it is also par + 5.
- **Score Differential** = (113/Slope) × (AGS − CR − PCC), rounded to 0.1. Negative values round toward 0 (−1.55 → −1.5).
  - **PCC cannot be computed** (it needs at least 8 same-day scores from the field), so store `pcc = 0` and label every differential "unofficial".
  - Total-only rounds or unmapped holes: use gross as AGS and mark `differential_kind = gross_upper_bound`.
- **Handicap Index** = mean of the lowest 8 of the most recent 20 differentials. There is **no 0.96 multiplier**. The fewer-than-20 table:
  - 3 → lowest 1 − 2.0
  - 4 → lowest 1 − 1.0
  - 5 → lowest 1
  - 6 → mean of lowest 2 − 1.0
  - 7–8 → 2; 9–11 → 3; 12–14 → 4; 15–16 → 5; 17–18 → 6; 19 → 7
  - Maximum 54.0; 54 holes needed to establish an HI.
- **Caps:** Low HI is the lowest HI in the 365 days before the latest score, and applies once 20 scores exist. The soft cap halves any increase beyond Low HI + 3.0; the hard cap is Low HI + 5.0.
- **Exceptional score reduction:** −1.0 for a differential 7.0–9.9 below HI; −2.0 for 10.0 or more. It is applied to the last 20 differentials and is cumulative.
- **9-hole rounds (post-2024):** the 18-hole differential = diff9 + the "expected 9-hole differential" for the player's HI. **That formula is unpublished.** The only public approximation, 0.52 × HI + 1.2 (unofficial, low confidence), reproduces the USGA example (HI 14.0, diff9 7.2 → 15.7).
  - **Hedge:** by default, exclude 9-hole rounds from the self-computed HI and show them as their own series. A toggle includes them using the approximation, with a label.
  - 10–17-hole rounds: excluded and flagged.
  - Fewer than 9 holes: not acceptable.
- If Shane posts to GHIN, `official_differential` wins, and computed minus official should differ only by PCC.
- The 18Birdies in-app handicap is not a WHS index either.
- **Unit tests from official examples:**
  - 15.3 / 15.2 / 16.6 → 13.2
  - six differentials with lowest-two mean 38.4 → 37.4
  - rounding cases
  - the 9-hole example above

### Lesson-impact method
- **Outcome:** per-round AGS differential, 18-hole rounds; 9-hole rounds enter as 2·diff9 at weight 0.5. **Never use the Handicap Index as the outcome**: it is a lagged best-8 "potential". Never use WHS-scaled 9-hole differentials either, because half of each value comes from the prior HI.
  - Fallback for unrated courses: to-par with a course random intercept.
- **Primary model:** Bayesian segmented regression (an interrupted time series) in PyMC:
  - `y_r = α + β·t_r + Σ_k δ_k·1[t_r ≥ L_k + w] + Σ_k ω_k·1[L_k ≤ t_r < L_k + w] + γ·x_r + ε_r`
  - `ε ~ StudentT(ν, 0, σ/√weight)`
  - w = a 14-day or 3-round learning window, which allows for a post-lesson dip
  - x = early-season flag (first rounds after a gap of more than 60 days), home course, 9-hole flag, total-only flag
  - Partial pooling: δ_k ~ N(μ_δ, τ), μ_δ ~ N(0, 2), τ ~ HalfNormal(1.5)
  - If lessons are evenly spaced, the linear trend and the steps become confounded; use a local-level baseline instead.
- **Report for each lesson:** posterior mean, 80% and 95% intervals, P(δ < 0), P(δ < −1), and the number of rounds on each side.
- **Robustness checks:**
  - In-time placebos: steps at non-lesson dates at least w away from a lesson; report the rank of the real effect.
  - Drop the 5 rounds before each lesson (to remove the pre-lesson dip).
  - An exact single-changepoint posterior.
  - OLS with Newey–West errors as a cross-check.
  - Bootstrap only with BCa or t intervals.
- **Focus-matched secondary outcomes:**
  - putting → putts/round and 3-putts
  - driving → FIR% and penalties
  - approach / full swing → GIR%
  - short game → scrambling
  - course management → double+ rate
- **Caveats, printed on the dashboard:**
  - **Noise.** MDE at 80% power, α = 0.05, is 2.8σ√(2/n). With σ ≈ 3–3.5, that is about 5–6 strokes for 5 rounds on each side, about 4 for 10, and about 3 for 20. σ is estimated from Shane's own data; the 2.74 + 0.053·HI prior comes from a 1997 pre-WHS study and was not fact-checked.
  - **Regression to the mean.** Lessons booked after a bad stretch look like improvements. The researcher's simulation shows about 2.2 strokes of false "gain" at zero true effect; `test_lesson_sim` re-derives this rather than trusting it.
  - Equipment changes made at the same time as a lesson can't be separated from it.
  - Seasonality, course and tee mix, and unmodeled PCC.
  - Results are **descriptive, not causal.** If n is too small, the dashboard shows the MDE and a smoothed ability line, and makes no effect claims.

### Dashboard views (one self-contained HTML file, data embedded as JSON, local by default)
1. **KPI strip:** unofficial HI (PCC = 0), Low HI, rolling-20 mean and SD of differentials, rounds this season, days and rounds since the last lesson, and export age.
2. **Hero chart:** differential timeline. Dots, y-axis inverted so better is up, hollow dots for 9-hole rounds, a local-level "ability" line with an 80% band, a dashed HI step labelled "potential, unofficial", vertical rules for lessons, ticks for equipment changes, shaded spans for practice blocks and injuries.
3. **Lesson effects:** forest plot of δ_k and μ_δ, the placebo histogram, and the MDE note ("with your σ you need about N rounds on each side to see 3 strokes").
4. **Small multiples** on a shared date axis with lesson rules: FIR%, GIR%, putts/round, the to-green vs putts split, double+/round, par-3/4/5 average. Tier C adds 3-putt rate and scrambling. Each panel has per-round dots, an EWMA line and the benchmark band for his HI.
5. **Stat-implied handicap** dot chart.
6. **Event swimlane:** lessons, practice density, equipment, injuries. Hover shows the note summary; the full quote appears only in the local build.
7. **Round table** with a drilldown: color-coded scorecard, stats against the rolling baseline, linked notes, and a screenshot thumbnail (local only).
8. **Data-quality panel:** unmapped clubs or tees, flagged rounds, pending reviews, export age.

---

## 6. Architecture

### Stack
- **Runtime:** Python 3.12 in a project venv, not base anaconda 3.10. Anthropic Python SDK 1.x (requires Python ≥ 3.10).
- **Core libraries:** `pydantic` v2, `typer`, `sqlite3`, `jinja2`, `altair` + `vl-convert-python` for inline offline charts (or hand-built SVG as in bell-rent-watch), `dateparser`, `rapidfuzz`, `pillow` + `pillow-heif`.
- **Phase 3b:** `fastapi` + `uvicorn` + htmx.
- **Phase 4:** `pymc` + `arviz`, `statsmodels`, `mcp` 2.2.0.
- **Optional:** `ocrmac` (local digit cross-check), `osxphotos`, `keyring` (only for GHIN or automation credentials).
- **Anthropic credentials:** `ant auth login` or a gitignored `.env`.
- **Repo:** private, `sharkweekshane/golf-analytics`, at `~/Desktop/golf-analytics`. No GitHub Actions for ingestion: the export is manual and Apple Notes is Mac-only.

### Directory layout
```
golf-analytics/
  pyproject.toml  .env.example  .gitignore  config.toml  courses.yaml  entities.yaml  manual_events.yaml
  golf/
    cli.py  db.py  schema.sql  config.py  privacy.py  dates.py
    ingest/  birdies_export.py  notes_applenotes.py  notes_dump.js  notes_manual.py  screenshots.py
    extract/ client.py  cache.py  round_models.py  note_models.py  prompts/{round_v1.md,notes_tl1.md}
    validate/ export.py  extraction.py  events.py
    whs/     differential.py  ndb.py  course_handicap.py  index.py  nine_hole.py
    analytics/ stats.py  sg_lite.py  trends.py  lesson_impact.py  benchmarks.py
    dashboard/ build.py  template.html.j2  charts.py
    web/     app.py  templates/            # phase 3b
    mcp_server.py                          # phase 4
  schemas/   extracted_round.v1.json  note_extraction.tl1.json   # committed wire schemas
  benchmarks/ shotscope_2020.csv  breakx_2026.csv                 # with source_url column
  tests/     fixtures/synthetic_archive.json  fixtures/notes/*.txt  test_*.py
  data/      (gitignored) golf.db  raw/18birdies/  raw/screenshots/  cache/llm/  review/  golden/  site/
```

### SQLite schema (key columns)
```sql
imports(import_id PK, kind CHECK(kind IN('18b_export','screenshot','note','manual','ghin')), path, sha256 UNIQUE, imported_at, unknown_keys JSON)
clubs(club_id TEXT PK, name)                                   -- 18B facility UUID
courses(course_key PK, club_id FK, name, holes, par)
tees(tee_id PK, course_key FK, name, gender, par, yards, cr18, slope18, cr_f9, slope_f9, cr_b9, slope_b9, bogey18,
     rating_source, checked_on, effective_from)
tee_holes(tee_id FK, hole, par, si, yards, PRIMARY KEY(tee_id, hole))
rounds(round_id TEXT PK, played_at_utc, played_on_local, club_id FK, course_key, tee_id, tee_is_default,
       entry_mode CHECK(entry_mode IN('hole_by_hole','total_only','partial','abandoned')), holes_played, nine,
       gross, to_par, par_played, fw_hit, fw_left, fw_right, fw_short, fw_long, fw_chances,
       gir, gir_left, gir_right, gir_short, gir_long, gir_no_chance, gir_chances, putts, putts_tracked,
       eagles_plus, birdies, pars, bogeys, dbl_plus, dq_flags JSON, first_import_id, last_import_id, deleted_in_source)
round_overrides(round_id PK, course_key, tee_id, exclude, reason)          -- human-owned
round_holes(round_id FK, hole, strokes, par, si, putts, fairway, gir, gir_miss, penalties, chips, sand,
            strokes_src, stats_src CHECK(stats_src IN('18b_export','screenshot','manual')), PRIMARY KEY(round_id, hole))
extractions(extraction_id PK, round_id, image_sha256s JSON, model, prompt_version, schema_version, raw_json,
            usage JSON, cost_usd, status, flags JSON, created_at)
corrections(id PK, entity, entity_id, field, model_value, corrected_value, extraction_id, prompt_version, model, corrected_at)
handicap_history(round_id PK, as_of, n_scores, ags, differential,
                 differential_kind CHECK(differential_kind IN('ndb_adjusted','gross_upper_bound','official')),
                 hi_after, low_hi, cap_applied, esr)
source_docs(doc_id PK, source, title, folder, created_at, modified_at, text, content_hash, status, flags, deleted_at)
extraction_runs(run_id PK, doc_id, cache_key UNIQUE, model, prompt_version, schema_version, response_json, input_tokens, output_tokens)
events(event_id PK, doc_id, run_id, status, event_type, is_planned, date, date_start, date_end, date_precision, date_source,
       date_expression, coach, location, focus_areas JSON, game_areas, equipment JSON, measurements JSON, injury JSON,
       summary, source_excerpt, line_start, line_end, excerpt_verified, confidence, flags JSON, linked_round_id)
event_reviews(event_id PK, decision, merged_into, edits_json, comment, reviewed_at)   -- human-owned
VIEW timeline  -- events + reviews applied, accepted/auto_accepted only
VIEW v_round_facts, v_hole_facts
```

### Entry points
- **Data in:** `golf init` · `golf import-18b <file>` · `golf courses check|autofill <club>` · `golf notes sync` · `golf add "…" [--date]` · `golf extract screenshots <dir> [--batch]`
- **Review:** `golf review rounds|events` · `golf apply-review`
- **Compute and output:** `golf recompute` (WHS, full chronological recompute) · `golf analyze lessons` · `golf build [--public]` · `golf status`
- **Later phases:** `golf eval extraction|notes` · `golf serve` (Phase 3b) · `golf mcp` (Phase 4)
- **MCP server:** stdio, `MCPServer`. The database is opened read-only (`file:golf.db?mode=ro`). Tools: `list_rounds`, `get_round`, `trend(metric, window)`, `timeline(start, end)`, `compare_periods(event_id, n_before, n_after)`, and `query_sql` (SELECT only).

### Privacy: never commit, never publish
- **Never:**
  - the raw archive (email, phone, birth year, friends, feed posts, payment status)
  - `golf.db`, screenshots, the LLM cache, review files, golden screenshots
  - note text and excerpts, `.env`
  - raw GolfCourseAPI data (its license forbids publishing)
  - any credentials
- **Enforcement:**
  - `data/` is gitignored.
  - A pre-commit hook runs `golf privacy-check`, which greps staged files for `mobileNumber|email|birthYear|friends|paymentMethod` and for note-text markers.
  - The parser drops `accountData`, `friendData`, `feedData` and `subscriptionData` at ingest.
- **GitHub Pages is public even from a private repo.**
  - The default is no publishing: open `data/site/index.html` locally.
  - `golf build --public` strips notes, quotes, injuries and screenshots, can optionally strip course names and shift dates, and is used only if Shane asks.
  - For phone access with full data, use private hosting such as Cloudflare Pages behind Access. Its free-tier details are **unconfirmed**.
- Note text and screenshots are sent to the Anthropic API at extraction time. Shane must OK this (Q7).

### Tests
- **`test_export_parse`:** a **synthetic** fixture built from the confirmed schema. Do not copy the public ericlu28 archive; it contains a third party's personal data. Cases: total-only, 9-length, padded 18, abandoned, sum mismatch, SG sentinel 100, unknown-key logging, idempotent re-import, deletion detection.
- **`test_whs`:** official examples, caps, ESR, NDB before and after an HI exists, 9-hole path.
- **`test_dates`:** weekday math at every anchor weekday, Dec→Jan rollover, reverse-chronological logs, `approximate` widening.
- **`test_extract_replay`:** runs offline from cached LLM responses. **`test_extract_live`** (pytest marker `-m live`) re-scores the golden set whenever the prompt or model changes and reports per-field accuracy and cost.
- **`test_notes_gold`:** event F1, type accuracy, date-in-range, excerpt-verified rate.
- **`test_lesson_sim`:** synthetic series (σ = 3.5, known step, AR(1) noise, a regression-to-the-mean booking rule). Checks bias, 80% and 95% coverage, and a placebo false-positive rate ≈ α.
- **`test_build`:** no external URLs in the output, and the `--public` output contains no note text or PII keys.

---

## 7. Phased build plan

**P0: Shane's inputs (about 45 minutes, no code).**
- Download the export.
- Answer Q1–Q5.
- Move notes into the iCloud "Golf" folder.
- Capture one round's screens: all views, portrait and landscape.
- List the courses and tees he plays.
- *Accept when:* `data/raw/18birdies/` holds a file, and `golf inspect-export` prints its key tree (keys only) and matches the documented schema, or lists the differences.

**P1: Scores MVP (about 1 weekend).**
- Export parser and validation, `courses.yaml` with OpenGolfAPI autofill and manual checks, WHS module, `golf build` dashboard v1 (KPI strip, differential/to-par timeline, Tier A small multiples, round table, data-quality panel).
- *Accept when:*
  - Non-abandoned round count equals the app's Rounds Played count, as Shane checks by eye.
  - Re-importing the same file changes 0 rows.
  - All parser fixtures and WHS official examples pass.
  - Σ par from `courses.yaml` equals `strokes − score` on every complete round at mapped clubs.
  - The dashboard is one offline HTML file and passes the PII grep test.

**P2: Notes → timeline (about 1 weekend).**
- JXA connector, flattened schema, date resolver, reconcile, review, `golf add`, Shortcut instructions, and the events overlay on the hero chart plus the swimlane.
- *Accept when:*
  - The schema compiles on the first real call.
  - On a 15-note gold set (3 or more running logs), event F1 ≥ 0.85, date-in-range ≥ 0.95, excerpt verified ≥ 0.95. Everything is reviewed until these are met.
  - A re-sync with no edits makes 0 API calls and changes 0 events.
  - Editing one log entry changes only that entry's events, and reviewed events survive.

**P3: Screenshot per-hole stats (only if Q2 = yes; about 1–2 weekends).**
- Build the capture protocol and prompt from the P0 screenshots, then extraction, R/W validators, targeted re-read, YAML review, and Tier C metrics. **P3b:** the FastAPI review page.
- *Accept when:*
  - A 10-round golden set hand-verified by Shane scores ≥ 99% cell accuracy on putts, fairway and GIR, and 100% on par.
  - Every accepted round passes R1 and R2 against the export.
  - Logged cost per round is within ±50% of the §3 estimate.
  - The model comparison (opus-5 / opus-5-5 / sonnet-5) has been run, and Shane has picked a default.

**P4: Inference and MCP (about 1–2 weekends).**
- Lesson ITS model, placebos, MDE panel, focus-matched secondary outcomes, forest plot, local MCP server.
- *Accept when:*
  - `test_lesson_sim` coverage is within 92–98% for nominal 95%.
  - The placebo false-positive rate is ≤ 0.07.
  - The regression-to-the-mean scenario is not reported as an effect.
  - The dashboard shows the MDE for Shane's real σ and round counts.
  - Claude Code answers "how did my putts change after <lesson>?" through MCP tools, with the database read-only.

**P5: Optional.**
- iCloud inbox with launchd (idempotent, debounced), or an `osxphotos --screenshot` auto-pull.
- Scripted export, **only with written permission from 18Birdies**.
- GHIN import, if he posts and accepts the ToS risk.
- Strava MCP, if he is a subscriber.
- A stripped public build, or private hosting.

---

## 8. Open questions for Shane (prioritized; each changes what gets built)

1. **Where do your golf notes live, and in what shape?** Apple Notes (iCloud or On My iPhone, any locked notes), Google Docs, Notion or Markdown? One running log or one note per lesson? Do you type dates? This decides the connector and the running-log logic.
2. **Do you log putts, fairways, GIR and penalties per hole in 18Birdies?** If not, P3 is dropped entirely and Tier C metrics don't exist.
3. **Local-only, phone-accessible, or public dashboard?** This decides the publish pipeline and how much gets stripped. Default is local-only.
4. **Do you post to GHIN or hold an official Handicap Index?** This decides whether to build the GHIN import and whether the headline HI is official or self-computed.
5. **How many courses and tees do you play, and how often 9 holes?** This decides course-data effort and whether 9-hole handling matters.
6. **Is it OK to send golf-note text and screenshots to the Anthropic API?** If not, notes fall back to manual or regex entry, and screenshots to local OCR (much weaker).
7. **Model and cost.** Keep the `claude-opus-5` default, or let the P3 golden-set comparison pick `claude-opus-5-5` or `claude-sonnet-5`?
8. **Roughly how many rounds per year and lessons so far?** This decides whether the Bayesian lesson model is worth building or whether descriptive before/after plus an MDE is the honest ceiling.
9. **How do you sign in to 18Birdies, and do you want unattended export?** That would require asking 18Birdies for written permission through the partnership form. P5 only.
10. **Are you a paying Strava subscriber?** Only matters for the optional Strava MCP.

---

## 9. Claims that remain unverified, and how the build hedges

| Unverified claim | Hedge in the build |
|---|---|
| A 2026 export still matches the Dec 2025 schema (supported by Jun and Aug 2026 parsers, but no fresh file seen) | P0 inspects Shane's file first. Pydantic `extra='allow'`, unknown-key logging, loud failure on missing keys. |
| The export has no practice-log or hole-notes section (the sample user may simply not use those features) | Log unknown sections. If a practice section appears, ingest it as `practice` events. Otherwise capture Practice History by screenshot if Shane wants it. |
| Sign-in: sources conflict on whether Apple/Google app accounts can use the web export | Shane checks once. The fix, if needed, is adding email or phone in app Settings. |
| The scripted `userLogin`/`getMyData` flow works (read from JS, never run; token lifetime unknown) | Not built unless 18Birdies gives written permission. Default is a manual download on a reminder. |
| Current 18Birdies screen layouts: legend art from 2019, Round Summary redesigned Apr 2025, Scores/Stats toggle article removed, landscape hole count unknown | The prompt is built from Shane's P0 screenshots. `screen_type` includes `other`. `prompt_version` is tracked. The golden set catches UI changes. |
| The extraction schemas compile under structured-output limits (0 unions by design, but grammar size is untested) | One test call per schema before building further. Fallback: split into two calls. |
| Structured outputs on `claude-opus-5-5` (listed in live docs per the verifier; my cached reference omits it) | Model id is a config value. The compile test runs per model. |
| `messages.parse()` raises on enum case drift | Use `messages.create` with `output_config.format`, lenient case-insensitive validation, and the raw text kept. |
| WHS 9-hole expected score (0.52×HI+1.2 is unofficial) and 10–17-hole handling | 9-hole rounds are excluded from the self-computed HI by default; the approximation is behind a labelled toggle. 10–17-hole rounds are excluded. The HI is always labelled "unofficial"; GHIN values win if present. |
| PCC can't be computed | `pcc = 0`, labelled. The GHIN cross-check shows the gap. |
| Course ratings: OpenGolfAPI and 18Birdies disagree; GolfCourseAPI bogey and 9-hole fields are sparse | Hand-check the tees played against NCRDB or the scorecard. Store `rating_source` and `checked_on`. Auto-check Σ par against the export. |
| Meaning of the 18Birdies "Max" symbol; up-and-down definition; whether "Automatic Stats Calculation" derives GIR | Validation skips `max_star`. Benchmarks up & down is an opaque check only. Shane is asked for his stats setting, and W1 stays a warning. |
| `clubId` at a multi-course facility | `course_key` mapping via `courses.yaml`, a per-round override, and a Σ-par check to spot wrong mappings. |
| JXA folder scoping (nested folders, duplicate names), x-coredata id stability, iCloud freshness | First-run diagnostics. Re-key on (created_at, content_hash). Sync compares `modified_at` for equality instead of using a cursor. |
| The Strava MCP exposes the round's scorecard graphic or description | Not in the plan. Optional P5 only. |
| The GHIN unofficial API still works in Sep 2026 (evidence runs to Apr 2026) | Optional P5 only, with a manual DevTools token fallback. Credentials go in the Keychain, never the repo. |
| Analytics priors: SD ≈ 2.74 + 0.053·HI (1997), Shot Scope and Break X benchmarks, the 2.2-stroke regression-to-the-mean simulation, Altair inline export (not fact-checked) | Used only as priors or context. σ and effects are estimated from Shane's data. `test_lesson_sim` re-derives the regression-to-the-mean bias. Benchmark CSVs carry source URLs. Hand-built SVG is the fallback if Altair inline fails. |
| iOS 26 screenshot format (HDR saves as HEIC) | Ingest sniffs the file type and converts HEIC to PNG. |
| Cost estimates (output and thinking tokens are guesses) | Log `usage` and cost for every call. P3 acceptance requires the logged cost to fall within ±50% of the estimate. |

I verified two items myself today (2026-09-25): MCP Python SDK v2.2.0 exposes `from mcp.server import MCPServer`, and osxphotos v0.77.1 has the `--screenshot` query flag.