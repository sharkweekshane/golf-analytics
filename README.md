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
- to Meta's Model API (`api.meta.ai`), only when you ask the [Caddie](#the-caddie-in-the-web-app-muse-spark) a
  question (from the published [Caddie page](#caddie-on-github-pages), the question goes from your browser to
  your own Cloudflare Worker, which forwards it to Meta);
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
golf serve            # dashboard at http://127.0.0.1:8765, plus Caddie / Inbox / Review / Timeline / 18Birdies / Status
golf notes sync       # after writing notes
golf status           # counts, export age, pending reviews, API spend, watcher and publishing state
golf build            # write data/site/index.html: one self-contained file, no network
```

New 18Birdies exports need no command: download one and the watcher imports it.

## Publishing (GitHub Pages)

`golf publish` puts the dashboard at <https://sharkweekshane.github.io/golf-analytics/>, and the
[Caddie page](#caddie-on-github-pages) at <https://sharkweekshane.github.io/golf-analytics/caddie/>.

- **Public:** rounds, scores, stats and club distances, the unofficial handicap, lessons and the other
  timeline events with their summaries and focus areas. `golf-data.json` (what the Caddie page reads) adds
  the hole-by-hole and shot tables, drills and swing thoughts, but never your notes' own words.
- **Never public:** file paths or anything naming this Mac, screenshots and notebook photos (or their
  paths), GPS coordinates, API cost and usage, keys, and contact details.

How it works: the dashboard is built with `public=True` into a temporary folder (`index.html`,
`caddie/index.html`, `golf-data.json`, `.nojekyll`, `404.html`), and every file is scanned by
`golf.privacy.site_findings` before anything is pushed. The scan looks for local paths and this Mac's name, paths to screenshots or notebook photos,
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
identical to the last published one (apart from its build time) is not pushed again; that comparison
covers every file, so a change to the Caddie page or `golf-data.json` alone is pushed too.

Settings live under `[publish]` in `config.toml`:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turned on once the GitHub repo and its Pages site exist |
| `auto` | `true` | Republish after each new 18Birdies export the watcher imports |
| `remote` | `origin` | The project's git remote (or a URL) to push `gh-pages` to |
| `branch` | `gh-pages` | The only branch `golf publish` will force-push |
| `site_url` | `https://sharkweekshane.github.io/golf-analytics/` | Shown after publishing and on the Status page; the push target must be its repo |
| `allow_any_remote` | `false` | Push to a remote that is not `site_url`'s GitHub repo (tests use a local bare repo) |
| `caddie_worker_url` | `""` | The Caddie's Cloudflare Worker, `https://golf-caddie.<you>.workers.dev`. Empty: the Caddie page says it isn't connected and the dashboard doesn't link to it |

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
| `golf build [--out PATH] [--public]` | Self-contained dashboard HTML: the local, complete version, or with `--public` the whole public site `golf publish` pushes (dashboard, `caddie/` page and `golf-data.json`, in `data/site/public/`; with `--out`, just the dashboard) |
| `golf serve [--port 8765] [--open]` | Local web app, bound to 127.0.0.1; also watches Downloads every minute |
| `golf mcp` | Read-only MCP server on stdio |
| `golf caddie status` / `key` / `ask "<question>"` | The Caddie: key set or not (never shown), model and calls so far; how to add the key; one question in Terminal |
| `golf demo seed [--force]` / `golf demo clear` | Synthetic demo data |
| `golf privacy-check [files or folders] [--staged]` | Scan for personal data (the pre-commit hook runs this); a folder expands to what git would commit |

The web app answers only on 127.0.0.1 / localhost. It refuses other Host headers, which blocks DNS
rebinding, and refuses POSTs from any other origin, including another port on localhost (a Jupyter or
dev-server page). The file route serves only images under `data/`. The Caddie's `POST /api/caddie` also
accepts only `application/json`, which another site can't send without a CORS preflight this app never
answers, so no other page can spend your Meta credits.

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

## Chat page on claude.ai (Shane's Caddie)

<https://claude.ai/artifact/AwShFfn1R8fSb1p6RadvgT> is a private claude.ai page (only your account can
open it) with a scorecard summary and a chat box. It works on your phone in the Claude app or the browser,
and its questions run on your Claude plan through the claude.ai `sample` capability, so no API key and no
API billing. The first question asks you to allow the page to use Claude.

- The page is `golf/chat_page.html`. It reads `golf-data.json`, published next to it and built by
  `golf chat-data` (`golf/chat.py`): the headline numbers, and tables of rounds, holes, shots (no GPS),
  lessons/notes, handicap history and course cards. Round ids are the public aliases `r1..rN`, and the file
  must pass the same privacy check as the public dashboard.
- Claude sees the headline numbers and the round table with every question, and can call two page
  functions for detail: `query_table` (filter / group / aggregate any table) and `get_round`.
- **Freshness.** A scheduled task in the Claude app, *Refresh Shane's Caddie* (nightly around 9:40 pm,
  while the app is open), runs `golf chat-data` and republishes `golf-data.json` when its `content_sha`
  changed. To refresh right away, click *Run now* on that task, or ask Claude Code to "refresh Shane's
  Caddie".
- Difference from the MCP connection above: the page works anywhere but sees a nightly snapshot; Claude
  Desktop with `golf mcp` is Mac-only but reads `golf.db` live and can run any SELECT.

## The Caddie in the web app (Muse Spark)

`golf serve` has a **Caddie** page (<http://127.0.0.1:8765/caddie>, in the top bar): the Shane's Caddie card on
the left (headline numbers, score sparkline, yardage book) and a chat on the right. It answers with Meta's
**Muse Spark** model through the **Meta Model API**, using your own API key. `golf caddie ask "Am I improving?"`
asks the same way in Terminal.

**The key (once).** It lives in your macOS login Keychain, never in a file. Copy it from the API keys tab at
<https://dev.meta.ai>, then in Terminal run this exactly as shown. It asks for the key: paste it and press
Return (nothing shows as you paste). When it asks you to retype it, paste it again and press Return.

```sh
security add-generic-password -U -s golf-analytics.muse-spark -a muse-spark -w
golf caddie status      # "Key: set (macOS Keychain)"; it never shows the key
```

Never put the key after `-w` on the command line: it would land in your shell history and, while the
command runs, in the process list. `golf caddie key` prints these steps; it never asks for the key itself.
The same command with `-U` replaces the key; `security delete-generic-password -s golf-analytics.muse-spark
-a muse-spark` removes it. The app reads the key from the Keychain only when you ask a question (by running
`/usr/bin/security`), puts it only in the request's `Authorization` header, and never logs it, stores it,
writes it to `golf.db` or sends it to the page. The Status and Caddie pages and `golf caddie status` only
check that the Keychain item exists (the same command without `-w`), so they never load the key. If macOS
asks whether `security` may use the item, click *Always Allow*.

`MUSE_API_KEY` in the environment overrides the Keychain; it is meant for tests, and `golf caddie status`
and the Status page say when it is in use. It is never read from `.env`: a `MUSE_API_KEY` line there is
ignored, and the Status page tells you to delete it.

The Keychain keeps the key out of files, iCloud and git, but not away from programs you run: because the
item trusts `/usr/bin/security`, any process running as you (a script, a coding agent) can read it with
`security find-generic-password ... -w` without a prompt. If you want Claude Code never to do that, you
could add deny rules for `Bash(security find-generic-password*)` and `Bash(security dump-keychain*)` to its
permission settings. That is your call; this project doesn't change them.

**How it answers.** Each question goes to Meta's Responses API with `store: false` (Meta is asked not to keep
the conversation) and a fresh copy of the instructions: the same caddie rules as the claude.ai page (direct
answer first, never invent numbers, per-9 comparisons, sample size, no causal claims about lessons,
beginner-friendly practice), today's date, the headline figures and the rounds table. The model then looks
things up with read-only tools over `golf.db`, the same functions as `golf mcp`: `list_rounds`, `get_round`,
`trend`, `timeline`, `lesson_effects`, `club_distances`, `db_schema` and `query_sql` (one SELECT only, stopped
after 5 seconds). The page shows each lookup as it happens ("looked up 9 rounds"). At most 6 rounds of lookups
and about 80,000 characters of looked-up data per question; after that the next call is the last one and the
model is told to answer with what it has (`tool_choice: "none"`). The browser keeps the conversation (the last 16 messages go with each question);
*Clear chat* forgets it, and nothing is kept on the server.

**What leaves the Mac, and what doesn't.** Sent to Meta: your question and the conversation so far, the
headline block and rounds table, and whatever the tools return (round and hole scores, club distances,
lessons and notes with coach names and quoted excerpts). Round ids go as the aliases `r1..rN`, like the
claude.ai page. Never sent: GPS coordinates, whole note bodies, screenshot extractions, file paths, the raw
export record, the import log's summary (it holds the export's account fingerprint and file name) and the
key (the Caddie's SQL reads those columns as NULL). The OpenAI SDK's own environment settings
(`OPENAI_ORG_ID`, `OPENAI_PROJECT_ID`, `OPENAI_CUSTOM_HEADERS`) are dropped, so no other header goes along;
`OPENAI_LOG=debug` would print request bodies (your golf data) to the terminal, and the Caddie warns when
it is set. On Meta's Standard tier, Meta says it
does not train on your content; how long it keeps requests is up to its terms. The Caddie refuses
`*-contributor` models, whose prompts Meta may train on.

**Settings** (`[caddie]` in `config.toml`, read on every question):

| Key | Default | Meaning |
|---|---|---|
| `model` | `muse-spark-1.1` | A Standard-tier Muse Spark model (`muse-spark-1.3` costs the same) |
| `base_url` | `https://api.meta.ai/v1` | Must be https, on `api.meta.ai` (the key goes to this host) |
| `allow_any_base_url` | `false` | `true` lets `base_url` name another https host. Leave it off unless you mean it |
| `effort` | `low` | Reasoning effort: `minimal`, `low`, `medium`, `high`, `xhigh` |
| `tools` | `auto` | `native` tool calling; `auto` falls back to a JSON lookup protocol if Meta refuses it; `lookup` starts there |
| `max_tool_rounds` | `6` | Rounds of lookups per question |
| `max_output_tokens` | `8000` | Reasoning tokens count against it too |
| `timeout_seconds` | `120` | Per request. A timed-out request is never re-sent (it may still be running, and billed); a rate limit, a 500/502/503 or a dropped connection is retried once |

**When it can't answer**, the page says why, in plain words: no key yet (with the command above), key
rejected (copy it again and store it exactly as copied), out of credits (add some at dev.meta.ai), rate
limited or Meta's API down (try again in a minute), no connection. Meta's own error text is never shown,
because an auth error can echo part of the key.

**Cost.** Each API call's tokens, model and latency are logged in `llm_calls` (purpose `caddie`; never the
prompt, the answer or the key). Standard-tier prices are $1.25 per million input tokens ($0.15 cached) and
$4.25 per million output tokens, reasoning included. A question usually takes 2–3 calls, so a few cents.
The Status page's Caddie card and `golf caddie status` show questions, calls and spend; the Claude API
figures leave these calls out.

Compared with the claude.ai page: the Caddie runs on your Meta API credits instead of your Claude plan, and
reads `golf.db` live, so it always sees the latest import. It needs `golf serve` (or Terminal) on this Mac.

## Caddie on GitHub Pages

<https://sharkweekshane.github.io/golf-analytics/caddie/> is the Caddie on the public site: the same card and
chat, working from any phone or computer with nothing running on this Mac. Once it is connected, the
dashboard's header has an **Ask the Caddie** button. Answers come from Muse Spark (`muse-spark-1.3`, medium
effort), on your Meta API credits. The chat is locked with a passcode; the card is open to everyone, like the
dashboard.

**Why there is a Worker.** GitHub Pages only serves files, and anything in them is public, so the Meta key
can't be in the page. A small **Cloudflare Worker** (`worker/` in this repo, free plan) keeps the key as an
encrypted secret and passes the page's questions on to Meta:

```
your browser: caddie/index.html + golf-data.json (from GitHub Pages)
   | runs the conversation and the two lookups (query_table, get_round) over golf-data.json itself
   | POST /chat  {instructions, input, tools, final}   header X-Caddie-Passcode
   v
Cloudflare Worker "golf-caddie" (secrets: MUSE_API_KEY, CADDIE_PASSCODE)
   | checks the origin, the passcode, the size and shape of the request, the rate limits;
   | sets model, store: false, reasoning effort, max_output_tokens, tool_choice "auto" itself
   v
api.meta.ai/v1/responses  ->  trimmed reply {output, usage, status} back to the page
```

- **The page** (`golf/dashboard/caddie_page.html`, built by `golf/dashboard/caddie_page.py`) is
  self-contained: no CDN, no web fonts. Its Content-Security-Policy lets it run only its own script and
  connect only to this site and the Worker, and blocks form submission, so the passcode never ends up in a URL.
  A question gets at most 6 rounds of lookups (about 80,000 characters of results), then one last call
  without tools so the model has to answer. Answers are rendered by the same escape-first markdown renderer
  as the local Caddie. The conversation lives only in that browser tab (**Clear chat** forgets it).
- **The passcode** is sent in a header and compared by the Worker in constant time. The page checks a new
  passcode without calling Meta and keeps it in that browser's `localStorage` (key `golf-caddie.passcode`)
  until you click **Forget passcode**. A wrong or changed passcode brings the passcode box back with a message.
- **The Worker** (`worker/src/relay.js`) answers only `POST /chat` and its CORS preflight, only for
  `https://sharkweekshane.github.io` (anything else gets 403). It accepts only `{instructions, input, tools,
  final}` of known shapes (plain function tools, at most 12; no built-in tools such as web search), at most
  200 KB. It refuses `*-contributor` models, sends `store: false`, and never passes on Meta's own error
  text (an auth error can echo part of the key): the page gets a fixed code, like `out_of_credits`. It logs
  nothing. Without both secrets, or with a passcode under 12 characters, it answers "not configured".
- **What is public:** `golf-data.json` is a public file like the dashboard (anyone can download it; the
  passcode protects your Meta credits, not the data). It is `golf chat-data`'s bundle without the notes'
  verbatim excerpts, and it passes the same privacy check as the rest of the site.
- **What goes to Meta:** the question and the conversation so far, the instructions (headline figures,
  rounds table, column descriptions) and whatever the lookups return, as with the local Caddie.

**Setup (once).** You need a free Cloudflare account and Node 22 (`nvm use 22`; wrangler needs Node 22 or
later).

1. Sign up at <https://dash.cloudflare.com/sign-up> (the free plan is enough) and confirm your email.
2. In Terminal, deploy the Worker:

   ```sh
   cd ~/Desktop/golf-analytics/worker
   npm install
   npx wrangler login      # opens the browser: allow wrangler to use your Cloudflare account
   npx wrangler deploy     # prints the Worker's address: https://golf-caddie.<your-subdomain>.workers.dev
   ```

   If it asks you to choose a `workers.dev` subdomain, pick any name (say `shane-golf`).
3. Give the Worker its two secrets. Each command asks for the value: paste or type it and press Return
   (nothing is saved on this Mac or in the repo).

   ```sh
   npx wrangler secret put MUSE_API_KEY       # paste the Meta API key (the one from dev.meta.ai)
   npx wrangler secret put CADDIE_PASSCODE    # a passcode of at least 12 characters, e.g. four random words
   ```

   To send the key straight from your Keychain instead of pasting it:
   `security find-generic-password -s golf-analytics.muse-spark -a muse-spark -w | npx wrangler secret put MUSE_API_KEY`.
4. In `config.toml`, under `[publish]`, set the address `wrangler deploy` printed:
   `caddie_worker_url = "https://golf-caddie.<your-subdomain>.workers.dev"`.
5. `golf publish --dry-run` (it checks the site and says "The dashboard links to the Caddie"), then
   `golf publish`. After a minute or two, open <https://sharkweekshane.github.io/golf-analytics/caddie/> on
   your phone and enter the passcode once.

Later: `npx wrangler secret put CADDIE_PASSCODE` changes the passcode (each browser asks for the new one);
`npx wrangler secret put MUSE_API_KEY` replaces the key; `npx wrangler deploy` (from `worker/`) redeploys after
a change to the Worker or its `[vars]` in `worker/wrangler.toml` (model, effort, output limit); emptying
`caddie_worker_url` and publishing takes the button off the dashboard; `npx wrangler delete` removes the Worker.

**Cost and abuse.**

- Cloudflare's free plan covers this many times over (100,000 requests a day; the Worker uses a millisecond
  or two of CPU per request, and waiting for Meta doesn't count). Rate limiting is included.
- Meta: about a cent a question (2 to 3 calls), as for the local Caddie. These calls are not in `golf.db`'s
  `llm_calls` or `golf status`; dev.meta.ai shows their usage.
- The passcode is the lock: the Origin check stops other websites' pages, but not scripts that fake the
  header. Guessing is slowed to 30 requests a minute per IP address, and even a correct passcode gets at most
  20 calls to Meta a minute (both per Cloudflare location, and approximate). Use a long passcode, change it if
  it may have leaked, and keep the Meta account on prepaid credits (or a spending limit, if dev.meta.ai offers
  one), which caps the worst case.
- Anyone using a browser where you entered the passcode can ask questions until you click **Forget passcode**.
- The Worker keeps no logs (`[observability] enabled = false`). `npx wrangler tail` would show live request
  headers, the passcode's among them, so don't share its output.

**Working on it.** `cd worker && npm test` runs the Worker's tests (Node's own test runner; Meta is stubbed).
`npx wrangler deploy --dry-run` checks the configuration without deploying. To try the whole loop locally, put
**fake** values in `worker/.dev.vars` (gitignored) and point `META_BASE` at a local stub, e.g.
`npx wrangler dev --var META_BASE:http://127.0.0.1:8788/v1 --var ALLOWED_ORIGIN:http://127.0.0.1:8123`, then
build the site with `caddie_worker_url = "http://127.0.0.1:8787"` and serve it on port 8123 (the privacy check
refuses to publish a local address, so this can't go out by mistake).

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
  - an API key (Anthropic, or a Meta Model API key like `LLM|…|…`), or `MUSE_API_KEY` given a value;
  - any line of your own note text, including one pasted inside code or a table.

  Synthetic test fixtures opt out of the content checks with a `privacy-check: synthetic` line near the top.
- Screenshots, note text and notebook photos are sent to the Claude API when they are extracted, and
  nowhere else. The Caddie sends what a question needs to Meta's Model API (see
  [what leaves the Mac](#the-caddie-in-the-web-app-muse-spark)); its key is in the macOS Keychain, not `.env`.

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
.venv/bin/python -m pytest -q          # offline: the APIs, osascript, launchctl, OpenGolfAPI, GitHub and the Keychain are all faked
```

- `cd worker && npm test` runs the Caddie Worker's tests (Node 22).
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
  | `golf/dashboard/caddie_page.*` | The Caddie page for GitHub Pages |
  | `worker/` | The Caddie's Cloudflare Worker (Node; `npm test`) |
  | `golf/privacy.py` | The pre-commit check and the public-site check |
  | `golf/web` | The local web app |
  | `golf/mcp_server.py` | The MCP server |
  | `golf/caddie.py`, `golf/secrets.py` | The Caddie chat (Muse Spark) and the Keychain key reader |
  | `golf/cli.py` | The commands, plus the pipeline functions the web app shares |
