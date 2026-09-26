"""Course reference data: courses.yaml -> clubs/courses/tees/tee_holes, round mapping, checks,
and an OpenGolfAPI autofill that only ever *proposes* entries.

courses.yaml is human-owned and hand-verified: it is the truth for par, stroke index, rating and
slope. The export never says which course of a multi-course facility was played, so:
- a club with one course maps automatically; a multi-course club needs `default_course` or a
  per-round entry under `rounds:`;
- the tee is, in order: the round's entry under `rounds:`, the tee 18Birdies' GPS recorded
  (rounds.tee_name, from shotEntries) when the course has a tee of that name, else the course's
  default tee (round_overrides still win over all of these);
- a 9-length holeStrokes array can't tell front from back: on a 9-hole course (`holes: 9`) it is
  the course's nine ("front"); elsewhere `rounds: {<id>: {nine: back}}` or round_overrides.nine says,
  and failing those, the one nine whose par layout fits the export (the par checksum plus the
  export's own birdie/par/bogey/double counts; infer_nine). Both or neither fitting stays unknown.
OpenGolfAPI ratings disagree with other sources, so autofilled tees get rating_source='opengolfapi'
and an empty checked_on, and check_courses nags until Shane confirms them against the card or USGA.
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import Config
from .db import dumps, loads, upsert

OPENGOLFAPI_BASE = "https://api.opengolfapi.org/api/v1"
ODBL_ATTRIBUTION = ("Contains data from OpenGolfAPI (https://opengolfapi.org), made available under the",
                    "Open Database License (ODbL) 1.0: https://opendatacommons.org/licenses/odbl/1-0/")
USER_AGENT = "golf-analytics/0.1 (personal, local; course autofill)"
MIN_MATCH_SCORE = 70.0
SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}


class CoursesFileError(ValueError):
    """courses.yaml is malformed; the message names the problem."""


class AutofillError(RuntimeError):
    """OpenGolfAPI could not produce a confident proposal; the message says what to do instead."""


# ---------------------------------------------------------------------- models
class _Strict(BaseModel):
    """Hand-edited file: unknown keys are typos, so they are errors."""
    model_config = ConfigDict(extra="forbid")


class HoleSpec(_Strict):
    hole: int = Field(ge=1, le=18)
    par: int = Field(ge=3, le=6)
    si: int | None = Field(default=None, ge=1, le=18)
    yards: int | None = Field(default=None, ge=0)


class TeeSpec(_Strict):
    name: str
    gender: Literal["M", "F"] = "M"
    par: int | None = None
    yards: int | None = None
    cr18: float | None = None
    slope18: int | None = Field(default=None, ge=55, le=155)
    cr_f9: float | None = None
    slope_f9: int | None = Field(default=None, ge=55, le=155)
    cr_b9: float | None = None
    slope_b9: int | None = Field(default=None, ge=55, le=155)
    rating_source: str = ""
    checked_on: str = ""              # date Shane confirmed the rating; "" = not yet
    is_default: bool = False
    holes: list[HoleSpec] = []

    @field_validator("checked_on", "rating_source", mode="before")
    @classmethod
    def _text(cls, v: Any) -> Any:
        if v is None:
            return ""
        return v.isoformat() if isinstance(v, date) else v      # YAML turns 2026-09-01 into a date

    @model_validator(mode="after")
    def _consistent(self) -> "TeeSpec":
        numbers = [h.hole for h in self.holes]
        if len(numbers) != len(set(numbers)):
            raise ValueError(f"tee {self.name!r}: duplicate hole numbers")
        sis = [h.si for h in self.holes if h.si is not None]
        if len(sis) != len(set(sis)):
            raise ValueError(f"tee {self.name!r}: duplicate stroke indexes")
        if self.par is not None and self.holes and sum(h.par for h in self.holes) != self.par:
            raise ValueError(f"tee {self.name!r}: par {self.par} but holes add up to {sum(h.par for h in self.holes)}")
        return self

    @property
    def total_par(self) -> int | None:
        return self.par if self.par is not None else (sum(h.par for h in self.holes) or None)

    @property
    def total_yards(self) -> int | None:
        if self.yards is not None:
            return self.yards
        return sum(h.yards for h in self.holes) if self.holes and all(h.yards for h in self.holes) else None


class RoundMap(_Strict):
    course: str | None = None
    tee: str | None = None
    nine: Literal["front", "back"] | None = None


class CourseSpec(_Strict):
    name: str
    holes: int = 18
    source: str = "courses.yaml"
    source_ref: str = ""
    tees: list[TeeSpec] = []

    @field_validator("source_ref", mode="before")
    @classmethod
    def _ref_text(cls, v: Any) -> Any:
        return "" if v is None else str(v)

    @model_validator(mode="after")
    def _one_default(self) -> "CourseSpec":
        if sum(t.is_default for t in self.tees) > 1:
            raise ValueError(f"course {self.name!r}: more than one tee has is_default: true")
        ids = [(t.name.lower(), t.gender) for t in self.tees]
        if len(ids) != len(set(ids)):
            raise ValueError(f"course {self.name!r}: duplicate tee name/gender")
        return self

    def default_tee(self) -> TeeSpec | None:
        """The explicit default, or the only tee."""
        marked = [t for t in self.tees if t.is_default]
        return marked[0] if marked else (self.tees[0] if len(self.tees) == 1 else None)

    def tee_named(self, name: str) -> TeeSpec | None:
        matches = [t for t in self.tees if t.name.lower() == name.lower()]
        default = self.default_tee()
        preferred = [t for t in matches if default is not None and t.gender == default.gender]
        return (preferred or matches or [None])[0]


def _str_keys(v: Any) -> Any:
    return {str(k): val for k, val in v.items()} if isinstance(v, dict) else v


class ClubSpec(_Strict):
    name: str = ""
    default_course: str | None = None
    courses: dict[str, CourseSpec] = {}
    rounds: dict[str, RoundMap] = {}

    @field_validator("courses", "rounds", mode="before")
    @classmethod
    def _keys(cls, v: Any) -> Any:
        return {} if v is None else _str_keys(v)

    @model_validator(mode="after")
    def _references(self) -> "ClubSpec":
        if self.default_course and self.default_course not in self.courses:
            raise ValueError(f"default_course {self.default_course!r} is not one of this club's courses")
        for rid, m in self.rounds.items():
            course_key = m.course or self.implied_course()
            if m.course and m.course not in self.courses:
                raise ValueError(f"round {rid}: course {m.course!r} is not one of this club's courses")
            if m.tee and (course_key is None or self.courses[course_key].tee_named(m.tee) is None):
                raise ValueError(f"round {rid}: tee {m.tee!r} not found on course {course_key!r}")
        return self

    def implied_course(self) -> str | None:
        """The course every round maps to unless listed under rounds: (single course or default_course)."""
        return next(iter(self.courses)) if len(self.courses) == 1 else self.default_course


class CoursesFile(_Strict):
    clubs: dict[str, ClubSpec] = {}

    @field_validator("clubs", mode="before")
    @classmethod
    def _keys(cls, v: Any) -> Any:
        return {} if v is None else _str_keys(v)

    @model_validator(mode="after")
    def _unique_course_keys(self) -> "CoursesFile":
        seen: dict[str, str] = {}
        for club_id, club in self.clubs.items():
            for key in club.courses:
                if key in seen:
                    raise ValueError(f"course key {key!r} used by clubs {seen[key]} and {club_id}")
                seen[key] = club_id
        return self


def tee_id_for(course_key: str, tee: TeeSpec) -> str:
    return f"{course_key}:{tee.name}:{tee.gender}"


def default_courses_path(cfg: Config) -> Path:
    return cfg.root / "courses.yaml"


def load_courses(path: Path | str) -> CoursesFile:
    path = Path(path)
    if not path.exists():
        return CoursesFile()
    try:
        return CoursesFile.model_validate(yaml.safe_load(path.read_text()) or {})
    except yaml.YAMLError as e:
        raise CoursesFileError(f"{path.name}: not valid YAML: {e}") from None
    except ValidationError as e:
        raise CoursesFileError(f"{path.name}: {e}") from None


# ------------------------------------------------------------------------ sync
def _array_len(raw: str | None) -> int:
    return len(loads(raw, {}).get("holeStrokes") or [])


def _resolve_nine(conn: sqlite3.Connection, rnd: sqlite3.Row, wanted: str) -> bool:
    """Set front/back on a 9-length export round and move its holes to match (1-9 or 10-18)."""
    if rnd["source"] != "18b_export" or _array_len(rnd["raw"]) != 9 or rnd["nine"] == wanted:
        return False
    shift = (9 if wanted == "back" else 0) - (9 if rnd["nine"] == "back" else 0)
    if shift:
        conn.execute("UPDATE round_holes SET hole = hole + ? WHERE round_id = ?", (shift, rnd["round_id"]))
    conn.execute("UPDATE rounds SET nine = ? WHERE round_id = ?", (wanted, rnd["round_id"]))
    return True


_ROUNDS_WITH_OVERRIDE = ("SELECT r.round_id, r.club_id, r.source, r.nine, r.raw, r.tee_name, r.entry_mode, "
                         "r.holes_played, r.par_played, r.eagles_plus, r.birdies, r.pars, r.bogeys, r.dbl_plus, "
                         "o.nine AS nine_override FROM rounds r LEFT JOIN round_overrides o ON o.round_id = r.round_id")
NINES = {"front": range(1, 10), "back": range(10, 19)}


def score_type_counts(strokes: list[int], pars: list[int]) -> tuple[int, int, int, int, int]:
    """(eagles or better, birdies, pars, bogeys, double bogeys or worse) of strokes against pars."""
    diffs = [s - p for s, p in zip(strokes, pars)]
    return (sum(d <= -2 for d in diffs), sum(d == -1 for d in diffs), sum(d == 0 for d in diffs),
            sum(d == 1 for d in diffs), sum(d >= 2 for d in diffs))


def export_counts(rnd: sqlite3.Row) -> tuple[int, int, int, int, int] | None:
    """The export's own score-type counts for a round, when they cover every hole played."""
    vals = tuple(rnd[k] for k in ("eagles_plus", "birdies", "pars", "bogeys", "dbl_plus"))
    if any(v is None for v in vals) or not rnd["holes_played"] or sum(vals) != rnd["holes_played"]:
        return None
    return vals  # type: ignore[return-value]


def layout_fits(strokes: list[int], pars: list[int], par_played: int | None,
                counts: tuple[int, ...] | None) -> bool:
    """Whether a par layout agrees with the export: the par checksum (sum of par == strokes - score)
    and, when the export recorded them, its birdie/par/bogey/double counts recomputed from the strokes."""
    if par_played is None or len(strokes) != len(pars) or sum(pars) != par_played:
        return False
    return counts is None or score_type_counts(strokes, pars) == tuple(counts)


def infer_nine(rnd: sqlite3.Row, pars: dict[int, tuple[int, int]]) -> str | None:
    """Front or back for a 9-length export round on an 18-hole course, from the course layout alone.

    The export stores a nine as a bare 9-long holeStrokes array. When exactly one nine of the
    course's par layout passes layout_fits (the par checksum plus the export's score-type counts),
    that is the nine played; when both or neither fit, None (courses.yaml or round_overrides decide).
    """
    strokes = loads(rnd["raw"], {}).get("holeStrokes") or []
    if rnd["source"] != "18b_export" or rnd["entry_mode"] != "hole_by_hole" or len(strokes) != 9 \
            or any(not isinstance(s, int) or s <= 0 for s in strokes):
        return None
    counts = export_counts(rnd)
    fits = [nine for nine, span in NINES.items()
            if all(h in pars for h in span) and layout_fits(strokes, [pars[h][0] for h in span],
                                                            rnd["par_played"], counts)]
    return fits[0] if len(fits) == 1 else None


def _map_rounds(conn: sqlite3.Connection, club_id: str, club: ClubSpec) -> dict[str, int]:
    counts = {"nine_resolved": 0, "nine_inferred": 0}
    for rnd in conn.execute(_ROUNDS_WITH_OVERRIDE + " WHERE r.club_id = ?", (club_id,)).fetchall():
        m = club.rounds.get(rnd["round_id"]) or RoundMap()
        course_key = m.course or club.implied_course()
        tee_id = None
        course = club.courses[course_key] if course_key else None
        if course is not None:
            tee = (course.tee_named(m.tee) if m.tee else None) \
                or (course.tee_named(rnd["tee_name"]) if rnd["tee_name"] else None) or course.default_tee()
            tee_id = tee_id_for(course_key, tee) if tee else None
        conn.execute("UPDATE rounds SET course_key = ?, tee_id = ? WHERE round_id = ?",
                     (course_key, tee_id, rnd["round_id"]))
        whole_course = "front" if course is not None and course.holes == 9 else None
        wanted = rnd["nine_override"] or m.nine or whole_course
        inferred = infer_nine(rnd, _hole_par_si(conn, tee_id, course_key)) \
            if wanted is None and course is not None else None
        changed = _resolve_nine(conn, rnd, wanted or inferred or "unknown")
        counts["nine_resolved"] += changed
        counts["nine_inferred"] += changed and inferred is not None
    return counts


def apply_nine_overrides(conn: sqlite3.Connection, skip_clubs: set[str] | frozenset[str] = frozenset()) -> int:
    """Apply round_overrides.nine to 9-length export rounds (moving their holes to 10-18 for 'back').

    sync_courses does this for rounds at clubs in courses.yaml (skip_clubs) as part of mapping; this
    covers every other round, so an override works even before the club is in courses.yaml.
    """
    return sum(_resolve_nine(conn, rnd, rnd["nine_override"])
               for rnd in conn.execute(_ROUNDS_WITH_OVERRIDE + " WHERE o.nine IS NOT NULL").fetchall()
               if rnd["club_id"] not in skip_clubs)


def _hole_par_si(conn: sqlite3.Connection, tee_id: str | None, course_key: str | None) -> dict[int, tuple[int, int]]:
    """Par/SI for a round: its own tee's holes, else the course default tee's (par/SI rarely differ)."""
    candidates = [tee_id] if tee_id else []
    if course_key:
        candidates += [r["tee_id"] for r in conn.execute(
            "SELECT tee_id FROM tees WHERE course_key = ? AND is_default = 1", (course_key,))]
    for tid in candidates:
        holes = {r["hole"]: (r["par"], r["si"]) for r in conn.execute(
            "SELECT hole, par, si FROM tee_holes WHERE tee_id = ? AND par IS NOT NULL", (tid,))}
        if holes:
            return holes
    return {}


def _effective_rounds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT r.*, COALESCE(o.course_key, r.course_key) AS course_key_eff, "
        "COALESCE(o.tee_id, r.tee_id) AS tee_id_eff FROM rounds r "
        "LEFT JOIN round_overrides o ON o.round_id = r.round_id").fetchall()


def fill_round_holes(conn: sqlite3.Connection) -> int:
    """Copy par/SI from the mapped tee onto round_holes; returns the number of rows changed."""
    changed = 0
    for rnd in _effective_rounds(conn):
        if rnd["nine"] == "unknown":
            continue                    # positions are not hole numbers until the nine is known
        for hole, (par, si) in _hole_par_si(conn, rnd["tee_id_eff"], rnd["course_key_eff"]).items():
            changed += conn.execute(
                "UPDATE round_holes SET par = ?, si = ? WHERE round_id = ? AND hole = ? "
                "AND (par IS NOT ? OR si IS NOT ?)", (par, si, rnd["round_id"], hole, par, si)).rowcount
    return changed


def sync_courses(conn: sqlite3.Connection, yaml_path: Path | str) -> dict:
    """Upsert everything in courses.yaml, map rounds at its clubs, and fill round_holes par/SI.

    rounds_mapped / rounds_unmapped count every counted round (v_rounds: not deleted, not
    abandoned), including rounds at clubs missing from courses.yaml; clubs_unmapped counts those
    clubs (`golf courses check` lists them).
    """
    spec = load_courses(yaml_path)
    summary = {"clubs": 0, "courses": 0, "tees": 0, "tee_holes": 0, "rounds_mapped": 0, "rounds_unmapped": 0,
               "clubs_unmapped": 0, "nine_resolved": 0, "nine_inferred": 0, "holes_filled": 0,
               "unknown_round_ids": []}
    with conn:
        for club_id, club in spec.clubs.items():
            conn.execute("INSERT INTO clubs(club_id, name) VALUES (?, ?) ON CONFLICT(club_id) DO UPDATE SET "
                         "name = COALESCE(clubs.name, excluded.name)", (club_id, club.name or None))
            summary["clubs"] += 1
            for key, course in club.courses.items():
                default = course.default_tee()
                upsert(conn, "courses", {
                    "course_key": key, "club_id": club_id, "name": course.name, "holes": course.holes,
                    "par": default.total_par if default else None, "source": course.source,
                    "source_ref": course.source_ref or None}, ["course_key"])
                summary["courses"] += 1
                keep = []
                for tee in course.tees:
                    tid = tee_id_for(key, tee)
                    keep.append(tid)
                    upsert(conn, "tees", {
                        "tee_id": tid, "course_key": key, "name": tee.name, "gender": tee.gender,
                        "par": tee.total_par, "yards": tee.total_yards, "cr18": tee.cr18, "slope18": tee.slope18,
                        "cr_f9": tee.cr_f9, "slope_f9": tee.slope_f9, "cr_b9": tee.cr_b9, "slope_b9": tee.slope_b9,
                        "rating_source": tee.rating_source or None, "checked_on": tee.checked_on or None,
                        "is_default": int(tee is default)}, ["tee_id"])
                    conn.execute("DELETE FROM tee_holes WHERE tee_id = ?", (tid,))
                    for h in tee.holes:
                        conn.execute("INSERT INTO tee_holes(tee_id, hole, par, si, yards) VALUES (?, ?, ?, ?, ?)",
                                     (tid, h.hole, h.par, h.si, h.yards))
                    summary["tees"] += 1
                    summary["tee_holes"] += len(tee.holes)
                stale = [r["tee_id"] for r in conn.execute(
                    "SELECT tee_id FROM tees WHERE course_key = ? AND tee_id NOT IN (SELECT value FROM json_each(?))",
                    (key, dumps(keep)))]
                for tid in stale:
                    conn.execute("DELETE FROM tee_holes WHERE tee_id = ?", (tid,))
                    conn.execute("DELETE FROM tees WHERE tee_id = ?", (tid,))
            for k, v in _map_rounds(conn, club_id, club).items():
                summary[k] += v
            known = {r["round_id"] for r in conn.execute("SELECT round_id FROM rounds WHERE club_id = ?", (club_id,))}
            summary["unknown_round_ids"] += sorted(set(club.rounds) - known)
        summary["nine_resolved"] += apply_nine_overrides(conn, frozenset(spec.clubs))
        summary["holes_filled"] = fill_round_holes(conn)
        mapped = conn.execute(
            "SELECT COALESCE(SUM(course_key_eff IS NOT NULL), 0), COALESCE(SUM(course_key_eff IS NULL), 0), "
            "COUNT(DISTINCT CASE WHEN course_key_eff IS NULL THEN COALESCE(club_id, '') END) FROM v_rounds").fetchone()
        summary["rounds_mapped"], summary["rounds_unmapped"], summary["clubs_unmapped"] = tuple(mapped)
    return summary


# ---------------------------------------------------------------------- checks
def _issue(code: str, severity: str, message: str, **context: Any) -> dict:
    base = {"code": code, "severity": severity, "message": message,
            "club_id": None, "course_key": None, "tee_id": None, "round_id": None, "count": None}
    base.update(context)
    return base


def _played_holes(conn: sqlite3.Connection, rnd: sqlite3.Row) -> list[int] | None:
    """Hole numbers whose par should add up to the export's strokes - score, or None if unknowable."""
    if rnd["nine"] == "unknown" or "sum_mismatch" in loads(rnd["dq_flags"], []):
        return None
    if rnd["entry_mode"] == "total_only":
        if rnd["holes_played"] == 18:
            return list(range(1, 19))
        return {"front": list(range(1, 10)), "back": list(range(10, 19))}.get(rnd["nine"])
    holes = [r["hole"] for r in conn.execute(
        "SELECT hole FROM round_holes WHERE round_id = ? AND strokes IS NOT NULL ORDER BY hole", (rnd["round_id"],))]
    return holes if holes and len(holes) == rnd["holes_played"] else None


def check_courses(conn: sqlite3.Connection) -> list[dict]:
    """Everything that blocks or weakens differentials, most severe first.

    Codes: unmapped_club, unmapped_rounds, no_default_tee, unknown_tee, tee_missing_rating,
    tee_missing_nine_rating (9-hole rounds on an 18-hole course's nine without cr_f9/cr_b9),
    unchecked_rating, tee_missing_holes, nine_unknown, par_mismatch (the export checksum:
    sum of courses.yaml par over the holes played must equal strokes - score), and
    par_pattern_mismatch (the checksum holds, but the export's own birdie/par/bogey/double counts
    don't match the card under courses.yaml's per-hole par).
    """
    issues: list[dict] = []
    for r in conn.execute(
            "SELECT v.club_id, c.name, COUNT(*) AS n, "
            "(SELECT COUNT(*) FROM courses k WHERE k.club_id = v.club_id) AS n_courses "
            "FROM v_rounds v LEFT JOIN clubs c ON c.club_id = v.club_id "
            "WHERE v.course_key_eff IS NULL GROUP BY v.club_id ORDER BY n DESC, v.club_id"):
        label = r["name"] or r["club_id"]
        if r["n_courses"] == 0:
            issues.append(_issue("unmapped_club", "warn",
                                 f"{label}: {r['n']} round(s) but no course in courses.yaml "
                                 f"(try `golf courses autofill {r['club_id']}`)", club_id=r["club_id"], count=r["n"]))
        else:
            issues.append(_issue("unmapped_rounds", "warn",
                                 f"{label}: {r['n']} round(s) not mapped to one of its {r['n_courses']} courses; "
                                 "set default_course or list them under rounds:", club_id=r["club_id"], count=r["n"]))

    for r in conn.execute("SELECT course_key_eff, COUNT(*) AS n FROM v_rounds WHERE course_key_eff IS NOT NULL "
                          "AND tee_id_eff IS NULL GROUP BY course_key_eff"):
        issues.append(_issue("no_default_tee", "warn",
                             f"{r['course_key_eff']}: {r['n']} round(s) have no tee; mark one tee is_default: true",
                             course_key=r["course_key_eff"], count=r["n"]))
    for r in conn.execute("SELECT tee_id_eff, COUNT(*) AS n FROM v_rounds WHERE tee_id_eff IS NOT NULL "
                          "AND tee_id_eff NOT IN (SELECT tee_id FROM tees) GROUP BY tee_id_eff"):
        issues.append(_issue("unknown_tee", "error", f"{r['n']} round(s) point at tee {r['tee_id_eff']}, which "
                             "is not in courses.yaml", tee_id=r["tee_id_eff"], count=r["n"]))

    for t in conn.execute(
            "SELECT t.*, c.holes AS course_holes, "
            "(SELECT COUNT(*) FROM v_rounds v WHERE v.tee_id_eff = t.tee_id) AS n_rounds, "
            "(SELECT COUNT(*) FROM v_rounds v WHERE v.tee_id_eff = t.tee_id AND v.holes_played = 9 "
            " AND v.nine = 'front') AS n_front, "
            "(SELECT COUNT(*) FROM v_rounds v WHERE v.tee_id_eff = t.tee_id AND v.holes_played = 9 "
            " AND v.nine = 'back') AS n_back, "
            "(SELECT COUNT(*) FROM tee_holes h WHERE h.tee_id = t.tee_id AND h.par IS NOT NULL "
            " AND h.si IS NOT NULL) AS n_holes "
            "FROM tees t JOIN courses c ON c.course_key = t.course_key ORDER BY t.tee_id"):
        if not (t["is_default"] or t["n_rounds"]):
            continue                    # tees nobody plays don't need checking
        ctx = {"tee_id": t["tee_id"], "course_key": t["course_key"], "count": t["n_rounds"]}
        # A 9-hole course is scored on its 9-hole rating (cr_f9/slope_f9); an 18-hole one on cr18/slope18.
        cr, slope, fields = (t["cr_f9"], t["slope_f9"], "cr_f9/slope_f9") if t["course_holes"] == 9 else \
            (t["cr18"], t["slope18"], "cr18/slope18")
        if cr is None or slope is None:
            issues.append(_issue("tee_missing_rating", "warn",
                                 f"{t['tee_id']}: no course rating/slope ({fields}), so no differentials", **ctx))
        elif not t["checked_on"]:
            issues.append(_issue("unchecked_rating", "info",
                                 f"{t['tee_id']}: rating {cr}/{slope} "
                                 f"({t['rating_source'] or 'unknown source'}) not yet checked against the "
                                 "scorecard or USGA NCRDB; set checked_on", **ctx))
        if t["course_holes"] != 9:      # 9-hole rounds on an 18-hole course need that nine's own rating
            for nine, n, cr9, slope9 in (("front", t["n_front"], t["cr_f9"], t["slope_f9"]),
                                         ("back", t["n_back"], t["cr_b9"], t["slope_b9"])):
                if n and (cr9 is None or slope9 is None):
                    issues.append(_issue(
                        "tee_missing_nine_rating", "warn",
                        f"{t['tee_id']}: {n} 9-hole round(s) on the {nine} nine but no {nine}-nine rating "
                        f"(cr_{nine[0]}9/slope_{nine[0]}9), so they don't count toward the index",
                        **{**ctx, "count": n}))
        if t["n_holes"] < (t["course_holes"] or 18):
            issues.append(_issue("tee_missing_holes", "info",
                                 f"{t['tee_id']}: per-hole par/SI incomplete ({t['n_holes']} holes), so rounds "
                                 "get gross (upper-bound) differentials", **ctx))

    for rnd in conn.execute("SELECT * FROM v_rounds WHERE nine = 'unknown' AND course_key_eff IS NOT NULL "
                            "ORDER BY played_on_local"):
        issues.append(_issue("nine_unknown", "warn",
                             f"{rnd['round_id']} ({rnd['played_on_local']}): 9 holes, front or back unknown; add "
                             f"rounds: {{{rnd['round_id']}: {{nine: front|back}}}} to its club",
                             round_id=rnd["round_id"], club_id=rnd["club_id"], course_key=rnd["course_key_eff"]))

    for rnd in conn.execute("SELECT * FROM v_rounds WHERE par_played IS NOT NULL AND course_key_eff IS NOT NULL "
                            "ORDER BY played_on_local, round_id").fetchall():
        holes = _played_holes(conn, rnd)
        pars = _hole_par_si(conn, rnd["tee_id_eff"], rnd["course_key_eff"])
        if not holes or not all(h in pars for h in holes):
            continue
        expected = sum(pars[h][0] for h in holes)
        ctx = {"round_id": rnd["round_id"], "club_id": rnd["club_id"], "course_key": rnd["course_key_eff"],
               "tee_id": rnd["tee_id_eff"]}
        if expected != rnd["par_played"]:
            issues.append(_issue("par_mismatch", "error",
                                 f"{rnd['round_id']} ({rnd['played_on_local']}): courses.yaml par {expected} but "
                                 f"the export says strokes - score = {rnd['par_played']}; wrong course or tee?", **ctx))
            continue
        # The export also counts birdies/pars/bogeys/doubles itself: a different split means the par
        # layout is wrong for this round (another course of the facility, the other nine, a typo).
        counts = export_counts(rnd) if rnd["entry_mode"] == "hole_by_hole" else None
        strokes = dict(conn.execute("SELECT hole, strokes FROM round_holes WHERE round_id = ? "
                                    "AND strokes IS NOT NULL", (rnd["round_id"],)).fetchall())
        if counts is None or any(h not in strokes for h in holes):
            continue
        ours = score_type_counts([strokes[h] for h in holes], [pars[h][0] for h in holes])
        if ours != counts:
            label = ("eagles+", "birdies", "pars", "bogeys", "doubles+")
            show = lambda c: ", ".join(f"{n} {k}" for n, k in zip(c, label) if n)  # noqa: E731
            issues.append(_issue("par_pattern_mismatch", "warn",
                                 f"{rnd['round_id']} ({rnd['played_on_local']}): with courses.yaml par per hole "
                                 f"the card has {show(ours)}, but 18Birdies counted {show(counts)}; wrong course, "
                                 "nine or per-hole par?", **ctx))
    return sorted(issues, key=lambda i: SEVERITY_ORDER[i["severity"]])


# -------------------------------------------------------------------- autofill
def _pick(d: Any, *names: str) -> Any:
    if not isinstance(d, dict):
        return None
    for n in names:
        if d.get(n) not in (None, ""):
            return d[n]
    return None


def _num(v: Any, kind: type) -> Any:
    if v is None or isinstance(v, bool) or v == "":
        return None
    try:
        return kind(float(v))
    except (TypeError, ValueError):
        return None


def _gender(v: Any) -> str:
    s = str(v or "").strip().lower()
    return "F" if s in {"f", "w", "l", "female", "women", "womens", "women's", "ladies"} else "M"


def _rows(data: Any, *keys: str) -> list[dict]:
    """The list of records in an API payload, whatever it is wrapped in (the exact shapes are unverified)."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if not isinstance(data, dict):
        return []
    for k in (*keys, "data", "results", "items"):
        v = data.get(k)
        if isinstance(v, list):
            return [r for r in v if isinstance(r, dict)]
        if isinstance(v, dict):
            nested = _rows(v, *keys)
            if nested:
                return nested
    grouped = []
    for g in ("male", "female", "men", "women"):
        for r in data.get(g) or []:
            if isinstance(r, dict):
                grouped.append({"gender": g, **r})
    return grouped


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "course"


def _get(client: httpx.Client, path: str, params: dict | None = None) -> Any:
    resp = client.get(f"{OPENGOLFAPI_BASE}{path}", params=params)
    resp.raise_for_status()
    return resp.json()


def search_candidates(payload: Any) -> list[dict]:
    """Normalise a /courses/search response to [{id, name, club_name, city, state}]."""
    out = []
    for c in _rows(payload, "courses"):
        cid = _pick(c, "id", "course_id", "uuid")
        if cid is None:
            continue
        loc = c.get("location") if isinstance(c.get("location"), dict) else {}
        out.append({"id": str(cid), "name": str(_pick(c, "course_name", "name") or ""),
                    "club_name": str(_pick(c, "club_name", "facility_name", "club") or ""),
                    "city": _pick(c, "city") or _pick(loc, "city"),
                    "state": _pick(c, "state", "state_code") or _pick(loc, "state", "state_code")})
    return out


def rank_candidates(candidates: list[dict], name: str, city: str | None = None,
                    state: str | None = None) -> list[dict]:
    """Fuzzy name match (no shared id with 18Birdies exists), nudged by matching city/state."""
    from rapidfuzz import fuzz

    ranked = []
    compact = name.lower().replace(" ", "")
    for c in candidates:
        label = " ".join(x for x in (c["club_name"], c["name"]) if x)
        # Spacing differs between sources ("Butter Brook" vs "Butterbrook Golf Club"): also compare without spaces.
        score = max(fuzz.token_set_ratio(name.lower(), label.lower()),
                    fuzz.token_set_ratio(compact, label.lower().replace(" ", "")))
        for mine, theirs in ((city, c["city"]), (state, c["state"])):
            if mine and theirs and str(mine).strip().lower() == str(theirs).strip().lower():
                score += 5
        ranked.append({**c, "score": round(min(score, 100.0), 1)})
    return sorted(ranked, key=lambda c: -c["score"])


def _parse_holes(payload: Any) -> tuple[dict[int, dict], dict[str, dict[int, int]]]:
    """-> ({hole: {par, si}}, {tee name (lower) or "": {hole: yards}}) from a /courses/{id}/holes response."""
    common: dict[int, dict] = {}
    yards: dict[str, dict[int, int]] = defaultdict(dict)
    for i, h in enumerate(_rows(payload, "holes", "scorecard")):
        num = _num(_pick(h, "hole_number", "hole", "number", "hole_num"), int) or i + 1
        entry = common.setdefault(num, {})
        par = _num(_pick(h, "par"), int)
        si = _num(_pick(h, "handicap_index", "handicap", "stroke_index", "si", "hcp"), int)
        if par and "par" not in entry:
            entry["par"] = par
        if si and "si" not in entry:
            entry["si"] = si
        per_tee = _pick(h, "yardages", "tees")
        if isinstance(per_tee, dict):
            for tee, v in per_tee.items():
                y = _num(_pick(v, "yardage", "yards", "distance") if isinstance(v, dict) else v, int)
                if y:
                    yards[str(tee).lower()][num] = y
        elif isinstance(per_tee, list):
            for item in per_tee:
                tee = _pick(item, "tee_name", "name", "tee", "color")
                y = _num(_pick(item, "yardage", "yards", "distance"), int)
                if tee and y:
                    yards[str(tee).lower()][num] = y
        else:
            tee = _pick(h, "tee_name", "tee", "tee_color", "color")
            y = _num(_pick(h, "yardage", "yards", "distance"), int)
            if y:
                yards[str(tee).lower() if tee else ""][num] = y
    return common, yards


def _slope(v: Any) -> int | None:
    s = _num(v, int)
    return s if s is not None and 55 <= s <= 155 else None


def _rating(v: Any) -> float | None:
    r = _num(v, float)
    return r if r else None


def proposed_tees(tees_payload: Any, holes_payload: Any) -> list[dict]:
    """courses.yaml tee entries from /tees and /holes responses (rating_source=opengolfapi, unchecked)."""
    common, yards_by_tee = _parse_holes(holes_payload)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for t in _rows(tees_payload, "tees"):
        name = str(_pick(t, "tee_name", "name", "tee", "color", "tee_color") or "").strip()
        gender = _gender(_pick(t, "gender", "sex"))
        if not name or (name.lower(), gender) in seen:
            continue
        seen.add((name.lower(), gender))
        own_holes, own_yards = _parse_holes(t["holes"]) if isinstance(t.get("holes"), list) else ({}, {})
        layout = own_holes or common
        tee_yards = own_yards.get("") or yards_by_tee.get(name.lower()) or {}
        holes = []
        for num in sorted(layout):
            par = layout[num].get("par")
            if not par or not 3 <= par <= 6 or not 1 <= num <= 18:
                continue
            hole = {"hole": num, "par": par, "si": layout[num].get("si"), "yards": tee_yards.get(num)}
            holes.append({k: v for k, v in hole.items() if v is not None})
        par = _num(_pick(t, "par", "par_total"), int)
        tee = {
            "name": name, "gender": gender,
            "par": par if par and (not holes or sum(h["par"] for h in holes) == par) else None,
            "yards": _num(_pick(t, "total_yards", "yardage", "yards", "length"), int),
            "cr18": _rating(_pick(t, "course_rating", "rating", "cr")),
            "slope18": _slope(_pick(t, "slope_rating", "slope")),
            "cr_f9": _rating(_pick(t, "front_course_rating", "front9_rating", "front_rating", "rating_front")),
            "slope_f9": _slope(_pick(t, "front_slope_rating", "front9_slope", "front_slope", "slope_front")),
            "cr_b9": _rating(_pick(t, "back_course_rating", "back9_rating", "back_rating", "rating_back")),
            "slope_b9": _slope(_pick(t, "back_slope_rating", "back9_slope", "back_slope", "slope_back")),
            "rating_source": "opengolfapi", "checked_on": "", "is_default": False, "holes": holes,
        }
        if len({h.get("si") for h in holes if h.get("si")}) != len([h for h in holes if h.get("si")]):
            for h in holes:
                h.pop("si", None)       # conflicting SIs are worse than none
        out.append({k: v for k, v in tee.items() if v is not None})
    return out


_GENERIC_WORDS = {"golf", "club", "course", "courses", "links", "country", "resort", "and", "the", "cc", "gc"}


def search_queries(name: str) -> list[str]:
    """Queries to try, in order: OpenGolfAPI's search is literal, so 'Barefoot Resort & Golf' (the '&')
    and 'Butter Brook' (listed as 'Butterbrook') find nothing as exported."""
    words = re.sub(r"[^\w\s'-]", " ", name).split()
    core = [w for w in words if w.lower() not in _GENERIC_WORDS] or words
    out: list[str] = []
    for q in (name, " ".join(words), " ".join(core), "".join(core), core[0] if core else ""):
        q = q.strip()
        if len(q) >= 3 and q.lower() not in {x.lower() for x in out}:
            out.append(q)
    return out


def _search_all(client: httpx.Client, name: str) -> list[dict]:
    """Candidates from the first query variant that returns any (see search_queries)."""
    for q in search_queries(name):
        found = search_candidates(_get(client, "/courses/search", {"q": q}))
        if found:
            return found
    return []


def search_opengolfapi(query: str, *, http: httpx.Client | None = None) -> list[dict]:
    """Candidate courses for a free-text query (for the CLI to show when autofill isn't confident)."""
    client = http or httpx.Client(timeout=20.0, headers={"User-Agent": USER_AGENT})
    try:
        return search_candidates(_get(client, "/courses/search", {"q": query}))
    finally:
        if http is None:
            client.close()


# ------------------------------------------------------------- yaml writing
def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _find_block(lines: list[str], start: int, end: int, indent: int, key: str) -> tuple[int, int] | None:
    """[first, last) line span of `key:` at `indent` within lines[start:end], trailing comments excluded."""
    heads = {f"{' ' * indent}{k}:" for k in (key, f"'{key}'", f'"{key}"')}
    for i in range(start, end):
        line = lines[i].rstrip()
        if not any(line == h or line.startswith(h + " ") for h in heads):
            continue
        j = i + 1
        while j < end and (not lines[j].strip() or lines[j].lstrip().startswith("#") or _indent(lines[j]) > indent):
            j += 1
        while j > i + 1 and (not lines[j - 1].strip() or
                             (lines[j - 1].lstrip().startswith("#") and _indent(lines[j - 1]) <= indent)):
            j -= 1
        return i, j
    return None


def _render_club(club_id: str, club: dict) -> list[str]:
    text = yaml.safe_dump({club_id: club}, sort_keys=False, default_flow_style=None, width=110, allow_unicode=True)
    lines = ["  " + line if line else line for line in text.rstrip("\n").split("\n")]
    out: list[str] = []
    for line in lines:
        key = line.strip().rstrip(":")
        course = club.get("courses", {}).get(key)
        if _indent(line) == 6 and line.rstrip().endswith(":") and isinstance(course, dict) \
                and course.get("source") == "opengolfapi":
            out += [f"      # {line}" for line in ODBL_ATTRIBUTION]
            out += ["      # PROPOSED by `golf courses autofill`: check the tee you play against the physical",
                    "      # scorecard or USGA NCRDB, then set checked_on and is_default: true on that tee."]
        out.append(line)
    return out


def write_club_block(yaml_path: Path, club_id: str, club: dict) -> None:
    """Replace (or add) one club's block in courses.yaml, leaving the rest of the file untouched.

    PyYAML can't round-trip comments, so only this club's block is re-rendered: comments above it and
    everywhere else survive; comments inside it are lost.
    """
    text = yaml_path.read_text() if yaml_path.exists() else \
        "# Course reference data, hand-verified. Format: see courses.example.yaml.\n"
    lines = text.rstrip("\n").split("\n")
    clubs_head = next((i for i, l in enumerate(lines) if re.match(r"^clubs:\s*(\{\s*\})?\s*(#.*)?$", l)), None)
    if clubs_head is None:
        lines += ["clubs:"]
        clubs_head = len(lines) - 1
    lines[clubs_head] = "clubs:"
    span = _find_block(lines, 0, len(lines), 0, "clubs")
    assert span is not None
    block = _render_club(club_id, club)
    existing = _find_block(lines, span[0] + 1, span[1], 2, club_id)
    if existing:
        lines[existing[0]:existing[1]] = block
    else:
        lines[span[1]:span[1]] = ([""] if span[1] > span[0] + 1 else []) + block
    yaml_path.write_text("\n".join(lines) + "\n")


def autofill_club(conn: sqlite3.Connection, club_id: str, yaml_path: Path | str, *,
                  http: httpx.Client | None = None, course_id: str | None = None, force: bool = False) -> dict:
    """Propose a course for `club_id` from OpenGolfAPI and write it into courses.yaml.

    Searches by the club name from the export, fetches the course's tees and holes, and writes one
    course (all tees, none default, none checked). Pass `course_id` to pick a specific OpenGolfAPI
    course, e.g. the second course of a multi-course facility. Refuses to overwrite a course whose
    ratings Shane already checked unless force=True. The only network calls are to OpenGolfAPI.
    """
    yaml_path = Path(yaml_path)
    club = conn.execute("SELECT name, city, state FROM clubs WHERE club_id = ?", (club_id,)).fetchone()
    if club is None or not club["name"]:
        raise AutofillError(f"club {club_id} has no name in golf.db; import the 18Birdies export first")
    spec = load_courses(yaml_path)
    client = http or httpx.Client(timeout=20.0, headers={"User-Agent": USER_AGENT})
    try:
        ranked = rank_candidates(_search_all(client, club["name"]), club["name"], club["city"], club["state"])
        shown = "; ".join(f"{c['id']}: {c['club_name']} {c['name']} ({c['city']}, {c['state']}) "
                          f"score {c['score']}" for c in ranked[:6]) or "none"
        if course_id is not None:
            chosen = next((c for c in ranked if c["id"] == str(course_id)), None)
            if chosen is None:
                info = search_candidates([_get(client, f"/courses/{course_id}")])
                chosen = {**(info[0] if info else {"id": str(course_id), "name": "", "club_name": ""}),
                          "id": str(course_id), "score": None}
        elif ranked and ranked[0]["score"] >= MIN_MATCH_SCORE:
            chosen = ranked[0]
        else:
            raise AutofillError(f"no confident OpenGolfAPI match for {club['name']!r}. Candidates: {shown}. "
                                "Re-run with course_id=<id>, or add the course by hand.")
        tees = proposed_tees(_get(client, f"/courses/{chosen['id']}/tees"),
                             _get(client, f"/courses/{chosen['id']}/holes"))
    finally:
        if http is None:
            client.close()
    if not tees:
        raise AutofillError(f"OpenGolfAPI course {chosen['id']} returned no usable tees")

    current = spec.clubs.get(club_id)
    club_dict: dict = current.model_dump(exclude_defaults=True) if current else {}
    club_dict.setdefault("name", club["name"])
    courses: dict = club_dict.setdefault("courses", {})
    same_ref = [k for k, c in (current.courses.items() if current else []) if c.source_ref == chosen["id"]]
    if same_ref:
        key = same_ref[0]
        if any(t.checked_on for t in current.courses[key].tees) and not force:
            raise AutofillError(f"{key} already has checked ratings; pass force=True to replace it")
    else:
        taken = {k for c in spec.clubs.values() for k in c.courses}
        base = slugify(chosen["name"] or club["name"])
        key, n = base, 2
        while key in taken:
            key, n = f"{base}-{n}", n + 1
    holes = max((h["hole"] for t in tees for h in t.get("holes", [])), default=18)
    if holes <= 9:
        tees = [_nine_hole_ratings(t) for t in tees]
    courses[key] = {"name": chosen["name"] or club["name"], "holes": 9 if holes <= 9 else 18,
                    "source": "opengolfapi", "source_ref": chosen["id"], "tees": tees}
    try:
        expected = ClubSpec.model_validate(club_dict)
    except ValidationError as e:
        raise AutofillError(f"OpenGolfAPI data did not validate as a courses.yaml entry: {e}") from None

    before = yaml_path.read_text() if yaml_path.exists() else None
    write_club_block(yaml_path, club_id, club_dict)
    try:
        ok = load_courses(yaml_path).clubs.get(club_id) == expected
    except CoursesFileError:
        ok = False
    if not ok:
        if before is None:
            yaml_path.unlink()
        else:
            yaml_path.write_text(before)
        raise AutofillError("could not write the proposal into courses.yaml without breaking it; file restored")
    # A multi-course facility: the club name matches several courses equally, and the export never
    # says which one was played. The caller warns; check_courses' par checks catch a wrong pick.
    tied = [] if course_id is not None or chosen.get("score") is None else \
        [c for c in ranked if c["id"] != chosen["id"] and c["score"] >= chosen["score"] - 1.0]
    return {"club_id": club_id, "course_key": key, "opengolfapi_id": chosen["id"],
            "matched_name": " ".join(x for x in (chosen.get("club_name"), chosen.get("name")) if x),
            "match_score": chosen.get("score"), "candidates": ranked[:5], "tees": len(tees),
            "holes": holes, "yaml_path": str(yaml_path), "also_matched": tied}


def _nine_hole_ratings(tee: dict) -> dict:
    """On a 9-hole course the WHS scores a round on the nine's rating (cr_f9/slope_f9). A source's
    single rating is either the nine's own (under ~50) or an 18-hole equivalent (the nine played
    twice, e.g. 64.2 for a 32.1 nine); the slope is the same number either way."""
    tee = dict(tee)
    cr, slope = tee.get("cr18"), tee.get("slope18")
    if tee.get("cr_f9") is None and cr is not None:
        tee["cr_f9"] = round(cr / 2, 1) if cr >= 50 else cr
        tee["cr18"] = cr if cr >= 50 else round(cr * 2, 1)
    if tee.get("slope_f9") is None and slope is not None:
        tee["slope_f9"] = slope
    order = ("name", "gender", "par", "yards", "cr18", "slope18", "cr_f9", "slope_f9", "cr_b9", "slope_b9")
    return {**{k: tee[k] for k in order if k in tee}, **{k: v for k, v in tee.items() if k not in order}}
