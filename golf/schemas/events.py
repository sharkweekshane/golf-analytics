"""Structured output for golf notes / notebook pages -> timeline events (prompt_version tl-1).

The model only DESCRIBES dates (DateRef); golf.notes.dates resolves them in Python against the
note's creation date / entry headers. Never ask the model to do calendar arithmetic.
"""
from __future__ import annotations

from typing import Literal

from . import Conf, Lenient

SCHEMA_VERSION = "tl-schema-1"

EventType = Literal[
    "lesson", "practice", "on_course", "equipment_change", "fitting", "injury",
    "fitness", "goal", "swing_thought", "milestone", "other",
]
GameArea = Literal[
    "full_swing", "driving", "approach", "short_game", "bunker", "putting",
    "course_management", "mental", "physical", "equipment",
]


class DateRef(Lenient):
    expression: str                       # verbatim date words from the text, "" if none
    kind: Literal["absolute", "relative", "header", "none"]
    year: int                             # -1 if absent
    month: int                            # -1 if absent
    day: int                              # -1 if absent
    time_hhmm: str                        # "" if absent
    anchor: Literal["note_created", "entry_header", "na"]
    header_line: int                      # line number of the governing entry header; -1 if n/a
    rel_kind: Literal["offset_days", "weekday", "period", "none"]
    rel_value: int                        # offset days (yesterday=-1) or period offset (last week=-1)
    weekday: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun", "none"]
    weekday_rel: Literal["last", "this", "next", "unspecified", "none"]
    period: Literal["week", "weekend", "month", "year", "none"]
    approximate: bool


class FocusArea(Lenient):
    game_area: GameArea
    detail: str                           # Shane's words, <= 12 words


class Drill(Lenient):
    name: str
    description: str
    dose: str                             # "50 balls", "3x10", "" if unknown


class Equipment(Lenient):
    action: Literal["added", "removed", "replaced", "adjusted", "fitted", "tested", "considering"]
    category: Literal[
        "driver", "fairway_wood", "hybrid", "iron_set", "iron", "wedge", "putter", "ball",
        "shaft", "grip", "shoes", "launch_monitor", "training_aid", "other",
    ]
    brand: str
    model: str
    specs: str
    replaces: str


class Measurement(Lenient):
    metric: str                           # "carry", "club speed", "handicap", ...
    value: str
    unit: str
    club: str


class Injury(Lenient):
    body_part: str
    status: Literal["new", "ongoing", "improving", "resolved"]
    severity: Literal["minor", "moderate", "severe", "unknown"]


class SourceSpan(Lenient):
    line_start: int                       # 1-based line numbers in the numbered text given to the model
    line_end: int
    excerpt: str                          # verbatim, <= 300 chars


class TimelineEvent(Lenient):
    event_type: EventType
    date: DateRef
    is_planned: bool
    coach: str
    lesson_format: Literal["in_person", "playing_lesson", "remote_video", "group_clinic", "none"]
    location: str
    location_kind: Literal["range", "course", "indoor_sim", "studio", "home", "gym", "other", "none"]
    duration_minutes: int                 # -1 if unknown
    focus_areas: list[FocusArea]
    drills: list[Drill]
    swing_thoughts: list[str]
    equipment: list[Equipment]
    measurements: list[Measurement]
    injury: list[Injury]                  # 0 or 1 items (list instead of Optional)
    goal_text: str
    summary: str                          # one factual sentence, <= 25 words
    source: SourceSpan
    confidence: Conf
    review_reasons: list[str]


class EntryHeader(Lenient):
    line: int
    date: DateRef


class NoteExtraction(Lenient):
    note_kind: Literal["single_entry", "running_log", "reference", "mixed", "not_golf"]
    log_order: Literal["chronological", "reverse_chronological", "unordered", "na"]
    entry_headers: list[EntryHeader]
    events: list[TimelineEvent]


class NotebookPage(Lenient):
    """A photographed handwritten notebook page: transcribe first, then extract from the transcription."""
    transcription: str                    # full page, line breaks preserved; [illegible] for unreadable words
    legibility: Conf
    page_date_text: str                   # date written at the top of the page, "" if none
    note_kind: Literal["single_entry", "running_log", "reference", "mixed", "not_golf"]
    log_order: Literal["chronological", "reverse_chronological", "unordered", "na"]
    entry_headers: list[EntryHeader]      # line numbers refer to the transcription
    events: list[TimelineEvent]
