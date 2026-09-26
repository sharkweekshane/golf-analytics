"""Golf notes -> dated timeline: Apple Notes sync, notebook photos, `golf add`, and review.

Entry points for the CLI / web layer are re-exported here; see each module for the WHY.
"""
from __future__ import annotations

from .applenotes import NotesError, NotesPermissionError, format_sync_summary, sync_apple_notes
from .extract import extract_doc
from .manual import add_manual_event, load_manual_yaml
from .notebook import import_notebook_pages
from .reconcile import (
    auto_accept_enabled,
    carry_over_review,
    flag_duplicates,
    link_rounds,
    pending_events,
    review_event,
    set_auto_accept,
    timeline,
)

NOTE_TEMPLATE_HELP = """\
How to write golf notes the timeline can date exactly
=====================================================

Keep every golf note in ONE iCloud folder named "Golf" (not "On My iPhone": those notes never reach
the Mac). Locked notes are skipped. Apple Notes stores no per-line timestamps, so an entry is only as
datable as the date you type on it: put the date FIRST on every entry, in ISO form.

Recommended entry line (one line per occasion; the date first):

    2026-09-25 — lesson w/ Mike: shallowing, pump drill 3x10. feel: trail elbow in front of hip. 7i 158->164
    2026-09-27 — range 45 min: pump drill again, wedges chunky
    2026-09-28 — played Stow North back 9, 44, 3 three-putts
    2026-10-02 — bought Vokey SM10 56.10 (replaces old 56)
    2026-10-09 — PLANNED lesson w/ Mike, driver

- One running note ("Golf Log") with new entries appended at the bottom works well; so does one note
  per lesson. Mixing is fine.
- Use the coach names / initials listed in entities.yaml (copy entities.example.yaml to start).
- "yesterday", "last Sat" and "3/14" are understood too, but they are resolved against the note's
  creation date and may be flagged for review; ISO dates never are.

iPhone Shortcut: "Golf log" (appends a dated line in two taps)
---------------------------------------------------------------
1. Shortcuts app > + (new shortcut), name it "Golf log".
2. Add "Ask for Input": Input Type = Text, Prompt = "What happened?"
3. Add "Ask for Input": Input Type = Date, Prompt = "When?", Default Answer = Current Date.
4. Add "Format Date": Date = (Provided Input from step 3), Date Format = Custom, Format String = yyyy-MM-dd
5. Add "Text":  (Formatted Date) — (Provided Input from step 2)
6. Add "Find Notes": filter Folder is Golf, and Name is "Golf Log"; Limit = 1.
7. Add "Append to Note": append (Text) to (Notes from step 6).
   Create the "Golf Log" note in the iCloud Golf folder once by hand; Append to Note needs it to exist.
8. Optional: add the shortcut to the Home Screen or the Action button.

On the Mac, run `golf notes sync` (keep Notes open for a minute after phone edits so iCloud catches up).
From the terminal you can also type `golf add "lesson w/ Mike, pump drill" --date 2026-09-25`.
Remembered past events without notes go in manual_events.yaml, e.g.

    - {date: 2024-05, type: fitting, summary: "Iron fitting, ordered T200s", coach: ""}
"""

__all__ = [
    "NOTE_TEMPLATE_HELP", "NotesError", "NotesPermissionError", "add_manual_event", "auto_accept_enabled",
    "carry_over_review", "extract_doc", "flag_duplicates", "format_sync_summary", "import_notebook_pages",
    "link_rounds", "load_manual_yaml", "pending_events", "review_event", "set_auto_accept", "sync_apple_notes",
    "timeline",
]
