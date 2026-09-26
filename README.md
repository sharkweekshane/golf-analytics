# Golf analytics

A personal golf-analytics app that runs on this Mac. It brings everything about your golf into one
SQLite file (`data/golf.db`) and shows it as a dashboard:

- **Rounds** from your 18Birdies export: strokes, totals, fairways, greens, putts, 18Birdies' own
  per-round handicap number, and the GPS-tracked shots (club distances).
- **Per-hole stats** read from 18Birdies screenshots by Claude vision, checked against the export.
- **Lessons, practice, equipment changes and injuries** from your Apple Notes `Golf` folder and from
  photos of your old paper notebook, dated by Python rather than guessed by the model.
- An **unofficial Handicap Index**, and **descriptive before/after comparisons** around each lesson.

Everything is built and stored on this Mac, under `data/`, which is gitignored (the Desktop syncs through
iCloud Drive, so see [Privacy](#privacy) for keeping `data/` off iCloud). A **public copy of the
dashboard** can go to GitHub Pages at <https://sharkweekshane.github.io/golf-analytics/> (see
[Publishing](#publishing-github-pages)); it never carries file paths, screenshots, GPS coordinates or API
cost. The only network calls are:

- to the Claude API, when something is extracted;
- to OpenGolfAPI, only when you run `golf courses autofill`;
- to GitHub, only when the dashboard is published.

`docs/DECISIONS.md` records the design decisions, and `docs/RESEARCH_PLAN.md` the research behind them.

## Setup

The virtual environment lives **outside iCloud**, in `~/.venvs/golf-analytics`, and `.venv` in the project
is a symlink to it:

```sh
cd ~/Desktop/golf-analytics
python3 -m venv ~/.venvs/golf-analytics
ln -s ~/.venvs/golf-analytics .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env          # then put your key in it: ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/golf init
```

**Why outside iCloud.** The Desktop on this Mac syncs through iCloud Drive, and iCloud marks folders
whose names start with a dot (`.venv`) as *hidden* (the `UF_HIDDEN` file flag, which comes straight back
after `chflags nohidden`). Python 3.10.14 and later skip hidden `.pth` files, and the editable install
(`pip install -e`) is a `.pth` file, so a venv inside iCloud Desktop fails with
`No module named 'golf'`. A venv in `~/.venvs` is not synced, keeps its `.pth` files visible, and the
symlink lets every command below keep using `.venv/bin/...`. If you ever see `No module named 'golf'`
again, the venv has ended up inside iCloud: recreate it as above.

`golf init` does five things:

- creates the `data/` folders (including `data/logs/`) and `golf.db`;
- writes an **empty** `courses.yaml` and `entities.yaml` if they are missing, each with header comments.
  The `*.example.yaml` files next to them show the format but are not copied: the course example is a
  synthetic club, and the entities example holds placeholder coach names that would otherwise be
  matched against your real notes;
- creates `.env` if it is missing;
- installs a git pre-commit hook that runs `golf privacy-check`;
- prints the next steps.

Get an API key at console.anthropic.com (Settings > API keys). `.env` is gitignored, and the key is never
printed: `golf status` only says whether it is set. Without a key, importing, reviewing and the dashboard
all still work, and extraction explains what is missing.

Then turn on the hands-off 18Birdies import (once; see below):

```sh
.venv/bin/golf watch install
```

Try everything on synthetic data first:

```sh
golf demo seed      # 24 labelled synthetic rounds, lessons, and one item to review
golf serve          # opens on http://127.0.0.1:8765
golf demo clear     # the first real import also removes demo rows automatically
```

## Updating: download, and that's it

New rounds reach the app through a fresh 18Birdies export. The update protocol is:

1. Open <https://18birdies.com/download-account-data/> (bookmark it) and sign in with email + password,
   or phone + SMS code. If you only ever signed in to the app with Apple or Google, add an email or phone
   in the app's Settings first.
2. Click **Request My Data**. The browser saves `18Birdies_archive.json` into **Downloads** (on the
   iPhone: Files > iCloud Drive > Downloads).
3. Nothing else. The app notices the file, imports it, maps courses, folds in screenshot rounds,
   recomputes the handicap, rebuilds the dashboard, and republishes the public site when publishing is on.

Who notices the file:

- **The LaunchAgent** (`golf watch install`): macOS starts `golf watch --once` whenever `~/Downloads` or
  iCloud Drive's `Downloads` folder changes, and every 30 minutes, even when nothing else is running.
- **`golf serve`**: while the web app runs, it also looks every minute.
- **`golf watch`**: the same scan as a foreground loop, if you prefer a Terminal window.

What a scan does:

- It looks for `18Birdies_archive*.json`, so `18Birdies_archive (1).json` and `18Birdies_archive-2.json`
  count too. Your file in Downloads is only read: never moved, renamed or deleted.
- A download that is still in progress (a `.crdownload` / `.download` sibling, a size still changing
  over two seconds, or JSON that is not complete yet) is left for the next scan; that is not an error.
- A file whose contents were already imported (same sha256) is skipped, whatever its name.
- A new one is copied to `data/raw/18birdies/18Birdies_archive_<YYYYMMDD>.json`, dated by the file's
  modification time (the download day). A different file never overwrites a stored one (it gets `-2`).
- Everything it does goes to `data/logs/watch.log`; the web app's **18Birdies** card (Status page, and a
  pill in the top bar with the export's age in days) shows the last scan and the watcher's state. Under
  the LaunchAgent a scan with nothing new prints nothing, so `watch.out.log` doesn't grow every 30 minutes.

Unattended means careful. A file is imported (and published) on its own only when it is an export of
**your** account and marks none of your rounds as deleted. The first import remembers the account as a
salted fingerprint of its id (never the id itself). Anything else is **held**: not copied, not imported,
not published, and reported on the 18Birdies card, in `golf status` and in the log, with the command that
applies it by hand:

- someone else's export downloaded on this Mac (their rounds would otherwise replace yours, and go
  public under your name): `golf import-18b '<file>' --new-account`, only if you really switched accounts;
- an export with no account id: `golf import-18b '<file>'` if it is yours;
- a snapshot missing rounds you have (deleted in the app?): `golf import-18b '<file>'` once you've
  checked. Even then the watcher never publishes after rounds were marked deleted; run `golf publish`.

**macOS permission (once).** A background program needs your permission to read Downloads, iCloud
Drive and the Desktop (where this project and its `data/` live). macOS grants it to the real Python
binary behind the LaunchAgent, which `golf watch status` prints:

1. **Preferred: Files and Folders.** The first background scans make macOS ask whether that Python may
   access your Downloads, Desktop and iCloud Drive folders: allow each. They then appear under
   System Settings > Privacy & Security > Files and Folders.
2. **Only if no prompt appears** and `golf watch status` still says "No permission": Full Disk Access.
   But the venv's Python is a link to your general-purpose Anaconda Python, and Full Disk Access for it
   would cover every program that runs it (Mail, Messages and Safari data included). Give the agent a
   Python of its own first, and grant that one:

   ```sh
   python3 -m venv --copies ~/.venvs/golf-agent
   ~/.venvs/golf-agent/bin/pip install -e ~/Desktop/golf-analytics
   # config.toml, under [watch]:  python = "~/.venvs/golf-agent/bin/python"
   golf watch install
   ```

Until then the scan cannot list the folders: the log, `golf watch status` and the 18Birdies card say
"No permission to read ~/Downloads". `golf serve` started from Terminal uses Terminal's permission
instead (macOS asks once).

You can still import by hand: `golf import-18b <file>`, or upload the file on the web app's Inbox page.
The upload is validated before anything is kept, and an identical file is recognised and not stored twice.

## The four ways data comes in

### 1. The 18Birdies export (the source of truth for strokes)

See [Updating](#updating-download-and-thats-it) for how a new export arrives. The import pipeline:

- parse the rounds (only `rounds` and `playedClubs`; your email, phone, friends and payment sections are
  dropped on read, and of the account only a salted fingerprint of its id is kept, to recognise a
  stranger's export). New in the real export: `roundHandicap` (18Birdies' own per-round differential) is
  kept per round, and `shotEntries` (GPS shot tracking) become the `shots` table: club, distance, hole.
  Shot coordinates stay in `golf.db` and are never published;
- map courses from `courses.yaml`;
- fold screenshot-only rounds into the export rounds they match;
- recompute the Handicap Index;
- link same-day notes to rounds.

Re-importing the same file changes nothing; an older snapshot is recorded but not applied. An export of a
different 18Birdies account is refused unless you add `--new-account`.

**Partial putts.** 18Birdies counts greens in regulation from each hole's putts, so the export's
`girHoleCount` says on how many holes putts were entered. A round's putt total counts only when it
covers every hole: at least one putt per hole **and** `girHoleCount` equal to the holes played. In your
export every round that passes has 21+ putts per nine, while 10 putts on a 65, 14 on a nine and 24 over
18 holes all have `girHoleCount` 0. The rest are marked partial and left out of every putting stat. If a
round really did have putts on every hole (Aug 18's 20 putts, say), overrule the rule:
`golf round putts 2026-08-18 full` (or `partial`, or `auto` to go back to the rule; a round id works when
two rounds share a date).

Course ratings come from `courses.yaml`, which you check by hand:

- `golf courses check` lists clubs with no ratings, unchecked tees, and rounds whose par doesn't add up.
- `golf courses autofill <club_id>` proposes an entry from OpenGolfAPI. Confirm the tee you play against
  the scorecard or USGA before setting `checked_on` and `is_default: true`: OpenGolfAPI ratings can be off
  by a stroke, and some of its cards are old (Southborough's) or have wrong pars (Barefoot's Dye Course).
  When the export's club name finds nothing ("Barefoot Resort & Golf", "Butter Brook"), simpler forms of
  the name are tried; when it matches several courses of one facility equally, autofill says so and lists
  their `--course-id`s.
- `golf courses sync` loads the file and recomputes.

The export names the facility, never the course, nine or tee, so two checks tie each round to a layout:
the par checksum (sum of par over the holes played equals strokes minus score) and the export's own
birdie/par/bogey/double counts recomputed from the hole scores and `courses.yaml`'s par per hole. A 9-hole
round on an 18-hole course is placed on the one nine that passes both, with no per-round entry needed
(both or neither fitting leaves it unknown, and `golf courses check` asks). The check also warns when a
round's counts don't fit its course (another course of the facility, a wrong par) and when 9-hole rounds
are played on a nine that has no 9-hole rating (`cr_f9`/`cr_b9`), since those can't count toward the index.
On a 9-hole course (`holes: 9`), rounds are its nine and are scored on `cr_f9`/`slope_f9`: a scorecard's
18-hole figure for such a course (the nine played twice, e.g. 64.2) is twice the 9-hole rating (32.1),
with the same slope.

A mistake in `courses.yaml` never undoes an import: the import finishes and says what to fix.

### 2. Screenshots (per-hole putts, fairways, greens, penalties)

Upload ONE round's screenshots on the Inbox page (optionally with the date played and the course), or run
`golf extract <files or folder> [--date YYYY-MM-DD]`. You can also drop them in
`data/inbox/screenshots/<YYYY-MM-DD something>/`, one folder per round, and click **Process inbox
folders** (or run `golf inbox`).

What to capture in 18Birdies:

1. **Full Scorecard, Stats view** (Round Summary > View Full Scorecard > Stats). Landscape if every
   hole fits, otherwise portrait swipes that overlap by a hole. This is the one that matters.
2. **Full Scorecard, Scores view**, showing the Par and Handicap rows. Needed once per course/tee, and
   for any round that isn't in your export yet.
3. **The top of the Round Summary**, only if the Stats header doesn't show the date and course.
4. **Benchmarks** (optional): printed totals, used as a cross-check.

Don't crop or zoom; HEIC is fine.

The export's strokes are never sent to the model and never overwritten: they are the checksum. Every
reading goes through deterministic checks. Doubtful cells get an independent one-cell re-read. Clean
rounds are accepted automatically; the rest wait on the **Review** page, with the screenshots next to an
editable grid (red = contradicts the export or impossible, amber = doubtful). Every correction you make is
stored and becomes the test set for measuring extraction accuracy. Correcting the date or the course
re-matches the round (and moves anything already applied to the right round).

A file that is not a readable image (empty, truncated) is refused before anything is copied or sent; in
the inbox it is moved to `data/inbox/rejected/` so it never blocks the other rounds. A failed extraction
leaves no copies behind: fix the problem and run it again. Running the same screenshots again with a
different `--date` returns the stored result and tells you the `golf review fix ... date_iso` command.

The screen guide in the prompt is provisional until it is rewritten from your real screenshots
(`golf/extract/prompts/screen_guide_18birdies.md`).

### 3. Apple Notes

Keep golf notes in one **iCloud** Notes folder named `Golf`: notes "On My iPhone" never reach the Mac.
Then run `golf notes sync`, or press the button on the Inbox page.

- **Permission.** The first sync makes macOS ask whether Terminal (or the web app's Python) may control
  Notes: allow it. If you missed the prompt: System Settings > Privacy & Security > Automation. Run
  `golf notes sync --dry-run` once first; it reads Notes but writes nothing and calls no API.
- **Dates.** Put an ISO date first on every entry, e.g.
  `2026-09-25 — lesson w/ Mike: pump drill 3x10, 7i 158->164`. `golf notes template` prints the full
  template and a two-tap iPhone Shortcut that appends dated lines to a "Golf Log" note.
- **Names.** Put your coaches (with nicknames and initials), practice places and current bag in
  `entities.yaml` (format: `entities.example.yaml`).
- **Cost.** Only new or edited notes are sent for extraction; a re-sync with no edits makes no API calls.
- **Review.** Every event waits for review until you turn on `golf notes auto-accept on`. Do that only
  once a checked sample shows the extraction is reliable. Auto-accepted events count as reviewed, so with
  publishing on they also go to the public page (summary, coach and focus) without you seeing them first.
  If you edit a note entry you already reviewed, the old event comes back as *text edited after review*:
  carry your review over to the new event, or dismiss it.

### 4. Notebook photos

Photograph each page flat and in good light, then either:

- upload the photos on the Inbox page;
- run `golf notebook <photos or folder>`; or
- drop them in `data/inbox/notebook/` and process the inbox.

Each page is transcribed, then events are pulled out. **Every notebook event needs your review.** A date
written at the top of the page is what makes its entries datable.

Also: `golf add "range 45 min, wedges" --date 2026-09-21 [--type practice --coach ... --focus putting]`
for quick manual entries, or **Quick entry** at the bottom of the Inbox page. These are accepted
immediately; with a type (always, on the Inbox form), no API call is made, so this works without a key.

## Daily use

```sh
golf serve            # dashboard at http://127.0.0.1:8765, plus Inbox / Review / Timeline / 18Birdies / Status
golf notes sync       # after writing notes
golf status           # counts, export age, pending reviews, API spend, watcher and publishing state
golf build            # write data/site/index.html: one self-contained file, no network
```

New 18Birdies exports need no command: download one and the watcher imports it.

## Publishing (GitHub Pages)

`golf publish` puts the dashboard at <https://sharkweekshane.github.io/golf-analytics/>.

- **Public:** rounds, scores, stats and club distances, the unofficial handicap, lessons and the other
  timeline events with their summaries and focus areas.
- **Never public:** file paths or anything naming this Mac, screenshots and notebook photos (or their
  paths), GPS coordinates, API cost and usage, keys, and contact details.

How it works: the dashboard is built with `public=True` into a temporary folder (`index.html`,
`.nojekyll`, `404.html`), and every file is scanned by `golf.privacy.site_findings` before anything is
pushed. The scan looks for local paths and this Mac's name, paths to screenshots or notebook photos,
embedded images, GPS coordinates (by key name, by precision, and against the shot coordinates in
`golf.db`), API cost fields, keys, email addresses and phone numbers. **Any finding aborts the publish.**
The folder then becomes a one-commit git repository that is force-pushed to the `gh-pages` branch of the
project's GitHub remote. That branch holds only the built site, so replacing it is safe; `golf publish`
refuses to push to any other branch, and to any remote that is not the repository `site_url` is served
from (`https://sharkweekshane.github.io/golf-analytics/` is `github.com/sharkweekshane/golf-analytics`),
so a mis-set `origin` can't wipe another repository's site. Nothing from `data/` is ever in that
repository. The commit is authored `golf-analytics <golf-analytics@users.noreply.github.com>`, never your
own git email, and git runs without inherited `GIT_DIR`-style variables (as set inside a git hook), so it
can only ever act on that temporary repository. The public page numbers rounds `r1`, `r2`, ... by date:
18Birdies' own round ids encode when each round was created, so they stay on the Mac. A build that is
identical to the last published one (apart from its build time) is not pushed again.

Settings live under `[publish]` in `config.toml`:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turned on once the GitHub repo and its Pages site exist |
| `auto` | `true` | Republish after each new 18Birdies export the watcher imports |
| `remote` | `origin` | The project's git remote (or a URL) to push `gh-pages` to |
| `branch` | `gh-pages` | The only branch `golf publish` will force-push |
| `site_url` | `https://sharkweekshane.github.io/golf-analytics/` | Shown after publishing and on the Status page; the push target must be its repo |
| `allow_any_remote` | `false` | Push to a remote that is not `site_url`'s GitHub repo (tests use a local bare repo) |

`golf publish --dry-run` builds and checks without pushing (it works while `enabled` is false) and leaves
the result in `data/site/public/` to look at. The Status page has a **Publish now** button once publishing
is on. Pushing uses your normal git credentials (Keychain or SSH) and never waits for a password prompt,
so a missing credential fails with a message instead of hanging the LaunchAgent.

## Commands

| Command | What it does |
|---|---|
| `golf init [--no-hook]` | Folders, database, empty `courses.yaml` / `entities.yaml`, pre-commit privacy hook |
| `golf status` | Round/event counts, unofficial HI, last export age, pending reviews, API calls and spend, key set or not, watcher and publishing state |
| `golf watch` | Foreground loop: import new `18Birdies_archive*.json` from Downloads (`--interval`, default 300 s) |
| `golf watch --once` | One scan (what the LaunchAgent runs) |
| `golf watch install` / `uninstall` / `status` | The LaunchAgent `com.golfanalytics.watch` in `~/Library/LaunchAgents` |
| `golf publish [--dry-run] [--force]` | Build the public dashboard, privacy-check it, push it to `gh-pages` |
| `golf inspect-export <file>` | The export's key tree (types and counts, never values) and a schema check |
| `golf import-18b [file] [--no-sync] [--new-account]` | Import by hand; with no file, the newest snapshot in `data/raw/18birdies/` (by snapshot date). `--new-account` accepts an export of a different 18Birdies account |
| `golf round putts <date or id> full\|partial\|auto` | Overrule the partial-putts rule for one round |
| `golf courses check` / `sync` / `autofill <club_id> [--course-id] [--force]` | Course reference data in `courses.yaml` |
| `golf recompute [--include-nine \| --no-include-nine]` | Rebuild the unofficial HI history; the 9-hole choice is remembered (default: included) |
| `golf extract <files/folder> [--date] [--club] [--holes] [--force] [--no-reread]` | One round's screenshots |
| `golf inbox [--dry-run]` | Process `data/inbox/screenshots` and `data/inbox/notebook` |
| `golf notebook <photos/folder> [--force]` | Transcribe notebook pages into pending events |
| `golf notes sync [--dry-run] [--no-extract] [--force]` | Apple Notes `Golf` folder into the timeline |
| `golf notes template` / `load-manual [yaml]` / `auto-accept on\|off` | Note template, remembered past events, auto-accept |
| `golf add "<text>" [--date] [--type] [--coach] [--focus] [--no-llm]` | Quick manual event |
| `golf review rounds` / `round <id>` / `fix <id> <hole\|-> <field> <value>` / `accept <id> [--force]` / `reject <id>` | Terminal review of screenshot rounds (the web page is easier) |
| `golf review events` | Walk the event queue: accept / reject / edit / merge / carry over |
| `golf analyze lessons` | Before/after comparison per lesson, with the minimum detectable effect |
| `golf build [--out PATH] [--public]` | Self-contained dashboard HTML: the local, complete version, or with `--public` the shareable one `golf publish` pushes (written to `data/site/public/index.html`) |
| `golf serve [--port 8765] [--open]` | Local web app, bound to 127.0.0.1; also watches Downloads every minute |
| `golf mcp` | Read-only MCP server on stdio |
| `golf demo seed [--force]` / `golf demo clear` | Synthetic demo data |
| `golf privacy-check [files or folders] [--staged]` | Scan for personal data (the pre-commit hook runs this); a folder expands to what git would commit |

The web app answers only on 127.0.0.1 / localhost. It refuses other Host headers, which blocks DNS
rebinding, and refuses POSTs from any other origin, including another port on localhost (a Jupyter or
dev-server page). The file route serves only images under `data/`.

When the Claude API is unreachable, overloaded or rejects the configured model, commands say
"Claude API unreachable or busy; re-run later" instead of asking for a key; work already finished is kept
and re-runs never re-bill it.

## Claude Desktop / Claude Code (MCP)

`golf mcp` is a local MCP server over `golf.db`, opened **read-only**. Its tools:

- `list_rounds`, `get_round`, `trend(metric, window)`, `timeline`, `lesson_effects`, `db_schema`;
- `query_sql`, which accepts a single SELECT and nothing else. Whole note bodies and screenshot
  extractions (which can name playing partners) read as NULL through it.

Claude Code:

```sh
claude mcp add golf -- /Users/shane/Desktop/golf-analytics/.venv/bin/golf mcp
```

Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "golf": {
      "command": "/Users/shane/.venvs/golf-analytics/bin/golf",
      "args": ["mcp"],
      "env": {"GOLF_ROOT": "/Users/shane/Desktop/golf-analytics"}
    }
  }
}
```

Quit Claude completely (Cmd-Q) and reopen it; the `golf` tools then show up in a new chat's tools menu.
This runs on your Claude subscription (no API key or API billing) and reads `golf.db` live, so answers
always reflect the latest import. Then ask things like *"How did my putts change after the June lesson?"*
or *"What's my median 9-iron?"* Keep in mind that whatever the tools return goes into that Claude
conversation.

## Privacy

- Everything personal is under `data/` (gitignored): the export archive, `golf.db`, screenshots, notebook
  photos, note text, the LLM response log, logs and the built dashboards. `.env` and `entities.yaml` (your
  coaches' names) are gitignored too, and so are stray `18Birdies_archive*.json`, database and HEIC files
  anywhere in the project.
- **iCloud.** This project lives on the Desktop, which syncs through iCloud Drive, so `data/` is uploaded
  to iCloud as well: the raw export (with your email, phone and friends list) and `golf.db` (with shot GPS
  positions). That is end-to-end encrypted only with Advanced Data Protection turned on. To keep it off
  iCloud, move the folder and point the app at it: under `[paths]` in `config.toml`, set
  `data_dir = "~/golf-data"` (`~` is expanded), then `mv data ~/golf-data`.
- The export parser reads only rounds and played clubs. Account, friends, feed and subscription sections
  never reach the database. Shot GPS coordinates stay in `golf.db`.
- The public site is checked before every push and carries no paths, photos, coordinates, cost or
  machine details (see [Publishing](#publishing-github-pages)).
- The pre-commit hook (`golf privacy-check --staged`) blocks commits that stage any of these:
  - a file under `data/`;
  - `.env`, an 18Birdies archive, a database file, or an image;
  - an email address or a phone number (with or without separators);
  - the export's personal keys with values (`mobileNumber`, `email`, `birthYear`, `friends`,
    `paymentMethod`);
  - any line of your own note text, including one pasted inside code or a table.

  Synthetic test fixtures opt out of the content checks with a `privacy-check: synthetic` line near the top.
- Screenshots, note text and notebook photos are sent to the Claude API when they are extracted, and
  nowhere else.

## Costs

Every API call's tokens and cost are logged in `llm_calls`; `golf status` shows the running total.
Re-running anything already extracted is served from that log for free. Typical figures from the research
plan, on the default model `claude-opus-5` ($5 / $25 per million tokens):

| What | Typical | Notes |
|---|---|---|
| One round of screenshots (about 3 images) | ~$0.18 (range $0.13–0.29) | ~$0.14 on claude-opus-5-5, ~$0.07 on claude-sonnet-5 |
| Targeted re-read of one flagged cell | ~$0.02 | at most 5 per round, only when something is flagged |
| One note | ~$0.05–0.09 | $0.02–0.04 on Sonnet 5; only new or edited notes |
| One notebook page | not estimated | one image plus a transcription; check `golf status` after the first pages |

The model is `[llm].model` in `config.toml`. Once a golden set of corrected rounds exists, compare models
on it before switching.

## How to read the analysis

- **The Handicap Index is unofficial.** It is self-computed with the 2024 WHS rules, with playing
  conditions (PCC) set to 0 and ratings from your `courses.yaml`. It is not a GHIN index.
  - 9-hole rounds **count by default**, because most of your golf is nine holes. Each is turned into an
    18-hole differential with a labelled expected-score approximation; the first index needs 54 holes.
    `golf recompute --no-include-nine` leaves them out, and the choice is remembered by every later
    import, review and courses sync (`--include-nine` turns it back on).
  - Net double bogey caps each hole, and the index never goes above 54.0 (the WHS maximum).
  - 18Birdies' own per-round number (`roundHandicap`) is shown beside yours for comparison; it is
    18Birdies' calculation, not the WHS one. The main differences, seen on real rounds:
    - a nine: 18Birdies doubles the 9-hole differential, where the 2024 WHS adds the expected score for
      the other nine (about 0.52 x HI + 1.2). So yours is higher than 18Birdies' on a good nine and lower
      on a bad one: the WHS method pulls a single nine halfway toward your usual level;
    - before any index exists, 18Birdies capped every hole at double bogey; the WHS caps at par + 5;
    - 18-hole rounds: slightly different course ratings, and net double bogey at a different Course
      Handicap (each app caps holes with its own index).
  - The **differential chart** plots the differentials that count toward your index (for a nine: its
    9-hole differential plus the expected score), 18Birdies' number as a ring, and the index as a step line.
    The lesson comparisons use 2 x the 9-hole differential instead, so the prior index can't pull a
    before/after difference toward zero.
  - Rounds entered only as a total use the gross score as an upper bound, since no per-hole cap is
    possible. The dashboard labels them so they stand apart.
- **Scores over time** plots strokes over par per 9 holes (an 18-hole round counts as its average nine),
  with a smoothed trend line and its noise band. The **course filter** narrows the charts, stats, club
  distances and rounds table to one course (a course needs 3 rounds to get its own entry); the KPI tiles,
  timeline and data-quality notes always cover every course.
- **Club distances** come from 18Birdies' GPS shot tracking. A shot's distance runs to where the next shot
  started, so it includes roll and mishits; read the median and the middle half. Shots entered after the
  round (every shot of the round logged within seconds) are drawn hollow: their positions are where the map
  was tapped. A distance repeated exactly on the same hole is the app's default position, not a
  measurement, and is left out, as are distances under 5 or over 400 yards.
- **Putts** count only when they were entered on every hole (at least one per hole, and 18Birdies' own
  GIR coverage says so; see [Partial putts](#1-the-18birdies-export-the-source-of-truth-for-strokes)).
  Otherwise the round shows "partial" and is left out of every putting stat; `golf round putts` overrules.
- **Hole by hole** (a course with 5+ rounds): the average score to par on each hole, with the latest round
  as a tick, to show which holes cost the most. **Triple bogey or worse per 9** sits next to double bogey
  or worse, because when most holes are doubles the triples are where the big numbers come from.
- **Club distances** also give a *live-logged median* per club (shots tracked during play only), because
  for clubs mostly entered after the round (wedges, say) the headline median is of map taps. Clubs with
  fewer than 5 shots are drawn faint.
- The **export age** turns into a reminder after 14 days (you play a couple of nines a week in season).
- **Lesson comparisons are descriptive, not causal.** Each lesson compares the rounds before with the
  rounds after a 14-day learning window, with bootstrap intervals.
  - The **minimum detectable effect** (MDE) is computed from your own round-to-round spread. A smaller
    difference is indistinguishable from noise, and the page says so. On the differential scale it is in
    differential points and also translated to strokes per 9 holes (about half at slope 113).
  - Be wary of three things:
    - Regression to the mean: lessons booked after a bad stretch look like improvements.
    - Equipment changes or injuries in the same window.
    - Seasonality.
  - With fewer than 30 rounds there is deliberately no causal model. More rounds on each side shrink the
    MDE.

## Development

```sh
.venv/bin/python -m pytest -q          # offline: the API, osascript, launchctl, OpenGolfAPI and GitHub are all faked
```

- Tests use synthetic data only (`tests/fixtures`). The watcher tests use temp "Downloads" folders and a
  temp LaunchAgents folder; the publishing tests push to a local bare repository.
- `-m live` is reserved for tests that call the real API.
- Package layout:

  | Path | What it holds |
  |---|---|
  | `golf/ingest` | The export importer |
  | `golf/courses.py` | Course reference data |
  | `golf/whs.py` | The unofficial HI |
  | `golf/extract` | Screenshot extraction and review |
  | `golf/notes` | Notes, notebook and timeline |
  | `golf/analytics`, `golf/dashboard` | Analysis and the dashboard |
  | `golf/watch.py` | Hands-off import of new exports, and the LaunchAgent |
  | `golf/publish.py` | GitHub Pages publishing |
  | `golf/privacy.py` | The pre-commit check and the public-site check |
  | `golf/web` | The local web app |
  | `golf/mcp_server.py` | The MCP server |
  | `golf/cli.py` | The commands, plus the pipeline functions the web app shares |
