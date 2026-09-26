"""Structured output for 18Birdies scorecard screenshots (prompt_version round-v1)."""
from __future__ import annotations

from typing import Literal

from . import Conf, Lenient

SCHEMA_VERSION = "round-schema-1"

HoleField = Literal[
    "par", "si", "strokes", "symbol", "putts", "fairway", "gir", "gir_miss", "penalties", "chips", "sand"
]


class ImageInfo(Lenient):
    image_index: int                      # 1-based, matches the "Image N:" label in the prompt
    screen_type: Literal[
        "round_summary", "scorecard_scores", "scorecard_stats", "benchmarks", "share_scorecard", "other"
    ]
    orientation: Literal["portrait", "landscape"]
    first_hole: int                       # -1 if no hole grid on this image
    last_hole: int
    problems: str                         # "" if none (cropped, blurry, overlay covering cells, ...)


class HoleRow(Lenient):
    hole: int
    par: int                              # -1 if not visible
    par_alt: int                          # second value of "4/5" men/women pairs; -1 if absent
    si: int                               # stroke index / handicap row; -1 if not visible
    si_alt: int
    yards: int                            # -1 if not visible
    strokes: int                          # read independently; -1 if not visible
    symbol: Literal["none", "circle", "double_circle", "square", "double_square", "max_star", "not_visible"]
    putts: int                            # -1 = not recorded / not visible (see stats_visible)
    penalties: int
    chips: int
    sand: int
    fairway: Literal[
        "hit", "left", "right", "short", "long", "miss", "not_applicable", "not_recorded", "not_visible"
    ]
    gir: Literal["hit", "miss", "not_recorded", "not_visible"]
    gir_miss: Literal["left", "right", "short", "long", "no_chance", "none", "not_visible"]
    stats_visible: bool                   # True if this hole's stats cells were captured at all
    confidence: Conf
    uncertain_fields: list[HoleField]
    source_images: list[int]


class Displayed(Lenient):
    """Totals printed on the screens, transcribed (not computed). -1 / "" if absent."""
    gross: int
    front: int
    back: int
    to_par_text: str
    total_putts: int
    fairways_hit: int
    gir: int
    penalties: int


class ExtractedRound(Lenient):
    images: list[ImageInfo]
    player_name: str
    other_players: list[str]
    course_text: str
    tee_text: str
    date_text: str
    date_iso: str                          # "YYYY-MM-DD" if a full date is visible, else ""
    course_par: int
    rating_text: str
    holes: list[HoleRow]                   # empty if no hole grid visible
    displayed: Displayed
    overall_confidence: Conf
    issues: list[str]


class CellReread(Lenient):
    """Targeted re-read of one flagged cell (prompt_version reread-v1)."""
    hole: int
    field: HoleField
    value: str                             # as read; "" if unreadable
    confidence: Conf
    note: str
