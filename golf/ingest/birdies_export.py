"""18Birdies official export (18Birdies_archive.json) -> clubs, rounds, round_holes, shots.

The export is a full account snapshot that Shane downloads by hand, so every import is a complete
picture: rounds missing from a newer snapshot were deleted in the app. Parsing follows
docs/RESEARCH_PLAN.md §2 "Parsing rules (corrected after fact-check)", plus two round keys found in
Shane's real export (Sep 2026) that the research did not know:
- roundHandicap: a STRING like "43.8", 18Birdies' own per-round handicap differential (18-hole
  equivalent) -> rounds.round_handicap_18b, used only as a cross-check (golf.whs.compare_18birdies);
- shotEntries: GPS shot tracking -> the shots table (club normalised, ordered by timestamp).

Privacy: only rounds and playedClubs are read. accountData, friendData, feedData, subscriptionData
and any unknown section are dropped at parse time. Unknown keys are logged by name only (never
values), and rounds.raw keeps only the round fields this parser knows, because a key added later
could carry playing partners' names. shotEntries are left out of rounds.raw: their GPS coordinates
live only in the shots table, which is local-only and never published.

Whose export: the one value kept from accountData is a salted sha256 fingerprint of userId
(account_fingerprint; the id itself is never stored). The first real import pins it in meta
'export_account_fp', and an export from another account is refused (ExportAccountError) unless the
import says new_account=True (`golf import-18b <file> --new-account`). preview_export tells the
watcher, before anything is copied or written, whose file it is and how many rounds it would mark
deleted, so a stranger's export (or a shrunken one) is never applied and published unattended.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..config import Config
from ..db import dumps, loads, now_iso

KIND = "18b_export"
ACCOUNT_KEY = "export_account_fp"     # meta: fingerprint of the account the exports belong to
PARSER_VERSION = 3                    # bump when the importer reads more of the same file, or reads it differently
# (v2: shots + roundHandicap; v3: putts partial unless girHoleCount covers every hole, account fingerprint)
SG_SENTINEL = 100
KNOWN_SECTIONS = frozenset({"accountData", "activityData", "clubData", "feedData", "friendData", "subscriptionData"})
KNOWN_ACTIVITY_KEYS = frozenset({"roundCount", "rounds"})
KNOWN_CLUB_DATA_KEYS = frozenset({"playedClubs", "postedInClubs"})
KNOWN_ROUND_KEYS = frozenset({"id", "timestamp", "clubId", "score", "strokes", "holeStrokes", "stats",
                              "roundHandicap", "shotEntries"})
RAW_ROUND_KEYS = KNOWN_ROUND_KEYS - {"shotEntries"}      # GPS stays out of rounds.raw
SHOT_COLUMNS = ("round_id", "seq", "hole", "shot_at_utc", "club_type", "club_number", "club", "loft",
                "distance_yards", "tee_name", "start_lat", "start_lon", "end_lat", "end_lon")


class ExportFormatError(ValueError):
    """The file is not a usable 18Birdies archive: the export format may have changed."""


class ExportAccountError(ValueError):
    """The export belongs to a different 18Birdies account than the one already imported."""


def account_fingerprint(user_id: Any) -> str | None:
    """A stable, non-reversible tag for the 18Birdies account an export belongs to (None without an id).
    Salted, so it can't be matched against a bare sha256 of the id published anywhere else."""
    if user_id is None or isinstance(user_id, bool) or not isinstance(user_id, (str, int)):
        return None
    text = str(user_id).strip()
    return hashlib.sha256(f"golf-analytics/18birdies-account:{text}".encode()).hexdigest()[:16] if text else None


def foreign_account_error(path: Path | str) -> ExportAccountError:
    path = Path(path)
    return ExportAccountError(
        f"{path.name} is an export of a different 18Birdies account than the rounds already imported, so "
        f"nothing was imported. If you really switched accounts, run: golf import-18b '{path}' --new-account")


def pinned_account(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (ACCOUNT_KEY,)).fetchone()
    return row[0] if row and row[0] else None


# ---------------------------------------------------------------------- models
def _alias(*names: str) -> Any:
    return Field(default=None, validation_alias=AliasChoices(*names))


class _Record(BaseModel):
    """extra='allow' so new keys survive validation and can be reported instead of silently lost."""
    model_config = ConfigDict(extra="allow")


class ClubRef(_Record):
    id: str


class RoundStats(_Record):
    """Round-level stats. Names are from the Dec 2025 archive; singular spellings are accepted in
    case the research's "fairwayLefts/Middles/..." shorthand was expanded differently."""
    aces: int | None = _alias("aces", "holeInOnes")
    double_eagle_or_better: int | None = _alias("doubleEagleOrBetter", "doubleEaglesOrBetter")
    eagles: int | None = _alias("eagles")
    birdies: int | None = _alias("birdies")
    pars: int | None = _alias("pars")
    bogeys: int | None = _alias("bogeys")
    double_bogey_or_worse: int | None = _alias("doubleBogeyOrWorse", "doubleBogeysOrWorse")
    fairway_middles: int | None = _alias("fairwayMiddles", "fairwayMiddle")
    fairway_lefts: int | None = _alias("fairwayLefts", "fairwayLeft")
    fairway_rights: int | None = _alias("fairwayRights", "fairwayRight")
    fairway_shorts: int | None = _alias("fairwayShorts", "fairwayShort")
    fairway_longs: int | None = _alias("fairwayLongs", "fairwayLong")
    fairway_hole_count: int | None = _alias("fairwayHoleCount", "fairwayHolesCount")
    gir: int | None = _alias("gir", "girs")
    gir_lefts: int | None = _alias("girLefts", "girLeft")
    gir_rights: int | None = _alias("girRights", "girRight")
    gir_shorts: int | None = _alias("girShorts", "girShort")
    gir_longs: int | None = _alias("girLongs", "girLong")
    gir_no_chances: int | None = _alias("girNoChances", "girNoChance")
    gir_hole_count: int | None = _alias("girHoleCount", "girHolesCount")
    putts: int | None = _alias("putts")
    putt_hole_count: int | None = _alias("puttHoleCount")      # unreliable (0 while putts > 0); unused
    stroke_gain_overall: float | None = _alias("strokeGainOverall")
    stroke_gain_tee_to_green: float | None = _alias("strokeGainTeeToGreen")
    recommended_stats: list | None = _alias("recommendedStats")

    @field_validator("stroke_gain_overall", "stroke_gain_tee_to_green")
    @classmethod
    def _sg_sentinel(cls, v: float | None) -> float | None:
        return None if v == SG_SENTINEL else v


def handicap_number(v: Any) -> float | None:
    """roundHandicap is a string ("43.8"); tolerate numbers, "", None and junk (-> None).

    Handicap notation writes plus (better than scratch) values as "+2.1": that is -2.1 here.
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
    elif isinstance(v, str):
        s = v.strip().replace(",", ".")
        if not s:
            return None
        try:
            x = -float(s[1:]) if s.startswith("+") else float(s)
        except ValueError:
            return None
    else:
        return None
    return x if math.isfinite(x) else None


class ExportRound(_Record):
    id: str
    timestamp: int                                        # epoch ms, UTC
    club: ClubRef = Field(validation_alias="clubId")      # facility id; join key to playedClubs[].clubId
    score: int                                            # to par of the holes actually played
    strokes: int
    hole_strokes: list[int] = Field(validation_alias="holeStrokes")
    stats: RoundStats | None = None
    round_handicap: float | None = Field(default=None, validation_alias="roundHandicap")
    shot_entries: list[Any] | None = Field(default=None, validation_alias="shotEntries")  # parsed one by one

    @field_validator("id", mode="before")
    @classmethod
    def _id_text(cls, v: Any) -> Any:
        return str(v) if isinstance(v, int) and not isinstance(v, bool) else v

    @field_validator("round_handicap", mode="before")
    @classmethod
    def _handicap(cls, v: Any) -> float | None:
        return handicap_number(v)

    @field_validator("shot_entries", mode="before")
    @classmethod
    def _shots_list(cls, v: Any) -> Any:
        return v if isinstance(v, list) else None

    @field_validator("club", mode="before")
    @classmethod
    def _flat_club(cls, v: Any) -> Any:
        return {"id": v} if isinstance(v, str) else v

    @field_validator("hole_strokes", mode="before")
    @classmethod
    def _null_is_unplayed(cls, v: Any) -> Any:
        return [0 if x is None else x for x in v] if isinstance(v, list) else v


class PlayedClub(_Record):
    club_id: str = Field(validation_alias="clubId")
    name: str | None = None
    city: str | None = None
    state: str | None = _alias("state", "stateCode")

    @field_validator("club_id", mode="before")
    @classmethod
    def _nested_id(cls, v: Any) -> Any:
        return v.get("id") if isinstance(v, dict) else v


class ShotPoint(_Record):
    latitude: float | None = None
    longitude: float | None = None


class ShotClub(_Record):
    type: str | None = None                               # WOOD | IRON | HYBRID | WEDGE | PUTTER
    number: str | None = None                             # '1', '7', 'P', 'S', 'Putter', ...
    loft_angle: int | None = Field(default=None, validation_alias="loftAngle")

    @field_validator("number", mode="before")
    @classmethod
    def _number_text(cls, v: Any) -> Any:
        return str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


class ShotEntry(_Record):
    """One GPS-tracked shot. Validated per entry: a malformed shot is skipped, never fatal."""
    timestamp: int | None = None                          # epoch ms, UTC
    hole_number: int = Field(validation_alias="holeNumber")
    stick: ShotClub | None = Field(default=None, validation_alias="stickTypeAndNumber")
    start: ShotPoint | None = Field(default=None, validation_alias="startPoint")
    end: ShotPoint | None = Field(default=None, validation_alias="endPoint")
    distance_yards: float | None = Field(default=None, validation_alias="distanceInYards")
    tee_name: str | None = Field(default=None, validation_alias="teeName")


_WEDGE_LETTERS = {"P": "PW", "PW": "PW", "A": "GW", "AW": "GW", "G": "GW", "GW": "GW", "U": "GW",
                  "S": "SW", "SW": "SW", "L": "LW", "LW": "LW"}


def normalize_club(club_type: str | None, number: str | None, loft: int | None = None) -> str | None:
    """18Birdies stickTypeAndNumber -> Driver, 3W, 5W, 4H, 5H, 4i..9i, PW, GW, SW, LW, Putter.

    Seen in Shane's export: WOOD/1, WOOD/3, HYBRID/5, IRON/6-9, WEDGE/P (46 deg), WEDGE/S (56 deg),
    PUTTER/Putter. A wedge given by loft instead of a letter is binned by loft. None = unrecognised
    (the raw type and number are still stored).
    """
    t = (club_type or "").strip().upper()
    n = (number or "").strip().upper()
    if t == "PUTTER" or n == "PUTTER":
        return "Putter"
    if t == "DRIVER" or (t == "WOOD" and n in ("1", "D", "DR", "DRIVER")):
        return "Driver"
    if t == "WOOD":
        return f"{n}W" if n.isdigit() else None
    if t == "HYBRID":
        return f"{n}H" if n.isdigit() else None
    if t == "IRON":
        return f"{n}i" if n.isdigit() else _WEDGE_LETTERS.get(n)
    if t == "WEDGE":
        if n in _WEDGE_LETTERS:
            return _WEDGE_LETTERS[n]
        deg = int(n) if n.isdigit() else (loft or 0)
        if deg:
            return "PW" if deg <= 48 else "GW" if deg <= 53 else "SW" if deg <= 57 else "LW"
    return None


def _alias_names(model: type[BaseModel]) -> frozenset[str]:
    names: set[str] = set()
    for name, f in model.model_fields.items():
        va = f.validation_alias
        if isinstance(va, AliasChoices):
            names.update(c for c in va.choices if isinstance(c, str))
        else:
            names.add(va if isinstance(va, str) else name)
    return frozenset(names)


KNOWN_STAT_KEYS = _alias_names(RoundStats)


# ------------------------------------------------------------- parsed records
@dataclass
class ParsedRound:
    round_id: str
    played_at_utc: str
    played_on_local: str
    club_id: str
    entry_mode: str
    holes_played: int
    nine: str | None
    gross: int | None
    to_par: int | None
    par_played: int | None
    array_len: int
    hole_strokes: dict[int, int]      # 1-based array position -> strokes; empty when per-hole data is unusable
    flags: list[str]
    stats: dict[str, int | None]      # rounds.* stat columns
    sg_overall: float | None
    sg_tee_to_green: float | None
    raw: dict
    round_handicap_18b: float | None = None
    tee_name: str | None = None       # most common shot teeName
    shots: list[dict] = field(default_factory=list)      # shots rows (SHOT_COLUMNS), seq order
    shots_skipped: int = 0            # malformed shot entries left out

    def row(self) -> dict:
        """The rounds columns this importer owns (never course_key/tee_id: courses.yaml owns those)."""
        return {
            "round_id": self.round_id, "source": KIND, "played_at_utc": self.played_at_utc,
            "played_on_local": self.played_on_local, "club_id": self.club_id, "entry_mode": self.entry_mode,
            "holes_played": self.holes_played, "nine": self.nine, "gross": self.gross, "to_par": self.to_par,
            "par_played": self.par_played, **self.stats, "dq_flags": dumps(self.flags), "raw": dumps(self.raw),
            "deleted_in_source": 0, "round_handicap_18b": self.round_handicap_18b,
            "sg_overall": self.sg_overall, "sg_tee_to_green": self.sg_tee_to_green, "tee_name": self.tee_name,
        }


@dataclass
class ParsedArchive:
    rounds: list[ParsedRound]
    clubs: list[PlayedClub]
    unknown_keys: list[str]
    round_count: int | None           # activityData.roundCount, cross-checked against len(rounds)
    account_fp: str | None = None     # account_fingerprint(accountData.userId); the id itself is dropped


# -------------------------------------------------------------------- parsing
_ID_LIKE = re.compile(r"[0-9a-fA-F-]{12,}|\d+|[^@\s]+@[^@\s]+")


def _key_label(key: Any) -> str:
    """Key names are logged, but a dict keyed by ids/emails would leak them: mask those."""
    return "<id>" if _ID_LIKE.fullmatch(str(key)) else str(key)


def classify(hole_strokes: list[int], strokes: int) -> tuple[str, int, str | None, dict[int, int], list[str]]:
    """Entry mode from holeStrokes and strokes -> (entry_mode, holes_played, nine, {position: strokes}, flags).

    0 in holeStrokes means "not played". The per-hole dict is returned whenever holes were entered;
    callers drop it for sum_mismatch rounds.
    """
    n = len(hole_strokes)
    played = {i + 1: s for i, s in enumerate(hole_strokes) if s > 0}
    k, total = len(played), sum(played.values())
    flags = [] if n in (9, 18) else ["unexpected_hole_array"]
    if strokes <= 0:
        return "abandoned", k, None, {}, flags + (["sum_mismatch"] if total else [])
    if total == 0:
        return "total_only", n, ("unknown" if n == 9 else None), {}, flags
    if total != strokes:
        flags.append("sum_mismatch")
    positions = sorted(played)
    if k == 18 and n == 18:
        return "hole_by_hole", 18, None, played, flags
    if k == 9 and n == 9:
        return "hole_by_hole", 9, "unknown", played, flags
    if k == 9 and n == 18 and positions == list(range(1, 10)):
        return "hole_by_hole", 9, "front", played, flags
    if k == 9 and n == 18 and positions == list(range(10, 19)):
        return "hole_by_hole", 9, "back", played, flags
    return "partial", k, None, played, flags + ["partial"]


STAT_COLUMNS = ("fw_hit", "fw_left", "fw_right", "fw_short", "fw_long", "fw_chances",
                "gir", "gir_left", "gir_right", "gir_short", "gir_long", "gir_chances", "gir_no_chance",
                "putts", "putts_tracked", "eagles_plus", "birdies", "pars", "bogeys", "dbl_plus")


def stats_columns(st: RoundStats | None, holes_played: int) -> dict[str, int | None]:
    """Tracked-vs-zero: untracked stats are exported as 0, so they become NULL here.

    FIR is tracked iff fairwayHoleCount > 0, GIR iff girHoleCount > 0 (puttHoleCount is ignored: it
    is 0 in most rounds that have putts). Putts are kept whenever > 0, but count as tracked
    (putts_tracked = 1) only when they cover every hole played:
    - at least one putt per hole: Shane's real export has 9-hole rounds with 7, 10 and 14 putts;
    - and, when the export says, 18Birdies' own GIR coverage: it derives greens in regulation from each
      hole's putts (strokes - putts), so girHoleCount below the holes played means some holes have no
      putts entered. In the real export every round with girHoleCount == holes has 21+ putts per 9,
      while 10 putts on a 65 and 24 putts over 18 holes with 16 doubles both have girHoleCount 0.
    Partial rounds keep putts_tracked = 0 and are flagged 'putts_partial' by the caller. Shane can
    overrule the rule per round (round_overrides.putts, `golf round putts <id> full|partial`).
    """
    cols: dict[str, int | None] = dict.fromkeys(STAT_COLUMNS)
    cols["putts_tracked"] = 0
    if st is None:
        return cols
    if (st.fairway_hole_count or 0) > 0:
        cols.update(fw_hit=st.fairway_middles, fw_left=st.fairway_lefts, fw_right=st.fairway_rights,
                    fw_short=st.fairway_shorts, fw_long=st.fairway_longs, fw_chances=st.fairway_hole_count)
    if (st.gir_hole_count or 0) > 0:
        cols.update(gir=st.gir, gir_left=st.gir_lefts, gir_right=st.gir_rights, gir_short=st.gir_shorts,
                    gir_long=st.gir_longs, gir_chances=st.gir_hole_count, gir_no_chance=st.gir_no_chances)
    if (st.putts or 0) > 0:
        every_hole = st.putts >= holes_played
        if st.gir_hole_count is not None and st.gir_hole_count < holes_played:
            every_hole = False              # 18Birdies had no putts on some hole, so it computed no GIR there
        cols.update(putts=st.putts, putts_tracked=int(every_hole))
    dist = [st.double_eagle_or_better, st.eagles, st.birdies, st.pars, st.bogeys, st.double_bogey_or_worse]
    if any(dist):
        eagles_plus = (st.double_eagle_or_better or 0) + (st.eagles or 0)
        # Aces may be counted on their own or inside eagles; add them only when the categories
        # otherwise fall short of the holes played by exactly the ace count.
        if st.aces and sum(x or 0 for x in dist) + st.aces == holes_played:
            eagles_plus += st.aces
        cols.update(eagles_plus=eagles_plus, birdies=st.birdies or 0, pars=st.pars or 0,
                    bogeys=st.bogeys or 0, dbl_plus=st.double_bogey_or_worse or 0)
    return cols


def _raw_subset(rec: dict) -> dict:
    raw = {k: rec[k] for k in RAW_ROUND_KEYS if k in rec}
    if isinstance(raw.get("clubId"), dict):
        raw["clubId"] = {"id": raw["clubId"].get("id")}
    if isinstance(raw.get("stats"), dict):
        raw["stats"] = {k: v for k, v in raw["stats"].items() if k in KNOWN_STAT_KEYS}
    return raw


def _utc_from_ms(ms: int | None) -> str | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def parse_shots(round_id: str, entries: list[Any] | None,
                unknown: set[str] | None = None) -> tuple[list[dict], str | None, int]:
    """shotEntries -> (shots rows in seq order, the round's most common teeName, entries skipped).

    seq follows the shot timestamp, then the order in the file (a shot without a timestamp sorts
    last). loftAngle 0 means "not set" and becomes NULL. Unknown keys are added to `unknown` by name.
    """
    unknown = set() if unknown is None else unknown
    parsed: list[tuple[int, ShotEntry]] = []
    skipped = 0
    for i, raw in enumerate(entries or []):
        try:
            se = ShotEntry.model_validate(raw)
        except ValidationError:
            skipped += 1
            continue
        if se.hole_number < 1:
            skipped += 1
            continue
        unknown.update(f"rounds[].shotEntries[].{_key_label(k)}" for k in se.model_extra or {})
        for label, sub in (("stickTypeAndNumber", se.stick), ("startPoint", se.start), ("endPoint", se.end)):
            if sub is not None:
                unknown.update(f"rounds[].shotEntries[].{label}.{_key_label(k)}" for k in sub.model_extra or {})
        parsed.append((i, se))
    parsed.sort(key=lambda p: (p[1].timestamp is None, p[1].timestamp or 0, p[0]))
    rows: list[dict] = []
    for seq, (_, se) in enumerate(parsed, start=1):
        stick = se.stick or ShotClub()
        start, end = se.start or ShotPoint(), se.end or ShotPoint()
        tee = (se.tee_name or "").strip() or None
        rows.append({
            "round_id": round_id, "seq": seq, "hole": se.hole_number, "shot_at_utc": _utc_from_ms(se.timestamp),
            "club_type": stick.type, "club_number": stick.number,
            "club": normalize_club(stick.type, stick.number, stick.loft_angle),
            "loft": stick.loft_angle or None, "distance_yards": se.distance_yards, "tee_name": tee,
            "start_lat": start.latitude, "start_lon": start.longitude,
            "end_lat": end.latitude, "end_lon": end.longitude,
        })
    tees = Counter(r["tee_name"] for r in rows if r["tee_name"])
    return rows, (tees.most_common(1)[0][0] if tees else None), skipped


def _parse_round(er: ExportRound, rec: dict, tz: ZoneInfo, club_ids: set[str],
                 unknown: set[str] | None = None) -> ParsedRound:
    utc = datetime.fromtimestamp(er.timestamp / 1000, tz=timezone.utc)
    mode, holes, nine, played, flags = classify(er.hole_strokes, er.strokes)
    if er.club.id not in club_ids:
        flags.append("unknown_club")
    live = mode != "abandoned"
    usable_holes = mode in ("hole_by_hole", "partial") and "sum_mismatch" not in flags
    stats = stats_columns(er.stats, holes)
    if stats["putts"] and not stats["putts_tracked"]:
        flags.append("putts_partial")
    shots, tee_name, skipped = parse_shots(er.id, er.shot_entries, unknown)
    return ParsedRound(
        round_id=er.id,
        played_at_utc=utc.isoformat(timespec="seconds"),
        played_on_local=utc.astimezone(tz).date().isoformat(),
        club_id=er.club.id,
        entry_mode=mode,
        holes_played=holes,
        nine=nine,
        gross=er.strokes if live else None,
        to_par=er.score if live else None,
        par_played=er.strokes - er.score if live else None,    # never abs(score): breaks under-par rounds
        array_len=len(er.hole_strokes),
        hole_strokes=played if usable_holes else {},
        flags=sorted(set(flags)),
        stats=stats,
        sg_overall=er.stats.stroke_gain_overall if er.stats else None,
        sg_tee_to_green=er.stats.stroke_gain_tee_to_green if er.stats else None,
        raw=_raw_subset(rec),
        round_handicap_18b=er.round_handicap,
        tee_name=tee_name,
        shots=shots,
        shots_skipped=skipped,
    )


def _error_lines(prefix: str, err: ValidationError) -> list[str]:
    """Locations and error types only: pydantic's own messages would echo input values."""
    return [f"{prefix}.{'.'.join(str(p) for p in e['loc'])}: {e['type']}" for e in err.errors()]


def _my_data(data: dict, unknown: set[str]) -> dict:
    if isinstance(data.get("myData"), dict):
        unknown.update(f"<root>.{_key_label(k)}" for k in data if k != "myData")
        return data["myData"]
    if "activityData" in data:          # file saved without the {"myData": ...} wrapper
        return data
    raise ExportFormatError("not an 18Birdies archive: no myData or activityData section")


def parse_archive(data: Any, tz_name: str) -> ParsedArchive:
    """Validate and parse the archive. Raises ExportFormatError listing every problem (no values)."""
    if not isinstance(data, dict):
        raise ExportFormatError("not an 18Birdies archive: top level is not a JSON object")
    unknown: set[str] = set()
    my = _my_data(data, unknown)
    unknown.update(f"myData.{_key_label(k)}" for k in my if k not in KNOWN_SECTIONS)
    activity, club_data = my.get("activityData"), my.get("clubData")
    missing = []
    if not isinstance(activity, dict) or not isinstance(activity.get("rounds"), list):
        missing.append("myData.activityData.rounds")
    if not isinstance(club_data, dict) or not isinstance(club_data.get("playedClubs"), list):
        missing.append("myData.clubData.playedClubs")
    if missing:
        raise ExportFormatError("required key missing: " + ", ".join(missing))
    unknown.update(f"myData.activityData.{_key_label(k)}" for k in activity if k not in KNOWN_ACTIVITY_KEYS)
    unknown.update(f"myData.clubData.{_key_label(k)}" for k in club_data if k not in KNOWN_CLUB_DATA_KEYS)

    errors: list[str] = []
    clubs: list[PlayedClub] = []
    for i, rec in enumerate(club_data["playedClubs"]):
        try:
            club = PlayedClub.model_validate(rec)
        except ValidationError as e:
            errors += _error_lines(f"playedClubs[{i}]", e)
            continue
        unknown.update(f"playedClubs[].{_key_label(k)}" for k in club.model_extra or {})
        clubs.append(club)

    tz = ZoneInfo(tz_name)
    club_ids = {c.club_id for c in clubs}
    rounds: list[ParsedRound] = []
    for i, rec in enumerate(activity["rounds"]):
        try:
            er = ExportRound.model_validate(rec)
        except ValidationError as e:
            errors += _error_lines(f"rounds[{i}]", e)
            continue
        unknown.update(f"rounds[].{_key_label(k)}" for k in er.model_extra or {})
        unknown.update(f"rounds[].clubId.{_key_label(k)}" for k in er.club.model_extra or {})
        if er.stats is not None:
            unknown.update(f"rounds[].stats.{_key_label(k)}" for k in er.stats.model_extra or {})
        rounds.append(_parse_round(er, rec, tz, club_ids, unknown))

    dupes = [rid for rid, n in Counter(r.round_id for r in rounds).items() if n > 1]
    if dupes:
        errors.append(f"{len(dupes)} duplicate round id(s)")
    if errors:
        shown = "\n  ".join(errors[:25])
        raise ExportFormatError(f"{len(errors)} problem(s) in the export; the format may have changed:\n  {shown}")
    count = activity.get("roundCount")
    account = my.get("accountData")
    fp = account_fingerprint(account.get("userId")) if isinstance(account, dict) else None
    return ParsedArchive(rounds, clubs, sorted(unknown), count if isinstance(count, int) else None, fp)


def load_archive(path: Path | str) -> Any:
    try:
        return json.loads(Path(path).read_bytes())
    except json.JSONDecodeError as e:
        raise ExportFormatError(f"not valid JSON (line {e.lineno})") from None


# ------------------------------------------------------------------ importing
_DATE_IN_NAME = re.compile(r"(20\d\d)[-_]?([01]\d)[-_]?([0-3]\d)")


def resolve_snapshot_date(path: Path, tz: ZoneInfo, *, today: date | None = None) -> tuple[str, list[str]]:
    """When the snapshot was taken, plus warnings: a YYYYMMDD in the file name, else the file's mtime
    (local date). A date later than today (local) can't be a real download date, and trusting it
    would make every later export look older ("stale") until that day: it is ignored with a warning.
    """
    today = today or datetime.now(tz).date()
    warnings: list[str] = []
    m = _DATE_IN_NAME.search(path.name)
    if m:
        try:
            named = date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            named = None
        if named is not None and named <= today:
            return named.isoformat(), warnings
        if named is not None:
            warnings.append(f"the date in the file name ({named.isoformat()}) is after today; "
                            "using the file's modification date instead")
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz).date()
    if modified > today:
        warnings.append(f"the file's modification date ({modified.isoformat()}) is after today; using today")
        modified = today
    return modified.isoformat(), warnings


def snapshot_date(path: Path, tz: ZoneInfo, *, today: date | None = None) -> str:
    """The snapshot date alone (see resolve_snapshot_date)."""
    return resolve_snapshot_date(path, tz, today=today)[0]


def _latest_snapshot(conn: sqlite3.Connection, today: date) -> tuple[str | None, str | None]:
    """(latest snapshot date, latest round timestamp seen) over earlier real imports.

    Future snapshot dates recorded before they were validated are ignored, so an old mis-named
    file can't block newer exports.
    """
    dates, newest = [], []
    for r in conn.execute("SELECT summary FROM imports WHERE kind = ? AND (sha256 IS NULL OR sha256 NOT LIKE 'demo-%')",
                          (KIND,)):
        s = loads(r["summary"], {})
        if s.get("snapshot_date") and s["snapshot_date"][:10] <= today.isoformat():
            dates.append(s["snapshot_date"])
            if s.get("max_round_utc"):
                newest.append(s["max_round_utc"])
    return (max(dates) if dates else None), (max(newest) if newest else None)


def _is_stale(snap: str, file_newest: str | None, latest: tuple[str | None, str | None]) -> bool:
    """Older than the latest import by date, and not newer by content either.

    The file name's date alone is not trusted: a file whose newest round is later than anything
    imported so far is applied even if its name says otherwise.
    """
    latest_snap, latest_newest = latest
    if latest_snap is None or snap >= latest_snap:
        return False
    return not (file_newest and latest_newest and file_newest > latest_newest)


def _upsert_clubs(conn: sqlite3.Connection, clubs: list[PlayedClub], round_club_ids: set[str]) -> None:
    for c in clubs:
        conn.execute(
            "INSERT INTO clubs(club_id, name, city, state) VALUES (?, ?, ?, ?) ON CONFLICT(club_id) DO UPDATE SET "
            "name = COALESCE(excluded.name, clubs.name), city = COALESCE(excluded.city, clubs.city), "
            "state = COALESCE(excluded.state, clubs.state)",
            (c.club_id, c.name, c.city, c.state))
    for club_id in round_club_ids - {c.club_id for c in clubs}:
        conn.execute("INSERT OR IGNORE INTO clubs(club_id) VALUES (?)", (club_id,))


def _write_holes(conn: sqlite3.Connection, pr: ParsedRound, nine: str | None) -> int:
    """Export strokes win over any other source; other columns (par/si/stats) are left alone.

    A 9-length array's positions become holes 10-18 once courses.yaml has said it was the back nine.
    """
    offset = 9 if pr.array_len == 9 and nine == "back" else 0
    desired = {pos + offset: s for pos, s in pr.hole_strokes.items()}
    existing = {r["hole"]: r for r in conn.execute(
        "SELECT hole, strokes, strokes_src, stats_src FROM round_holes WHERE round_id = ?", (pr.round_id,))}
    changes = 0
    for hole, strokes in desired.items():
        old = existing.get(hole)
        if old is None:
            conn.execute("INSERT INTO round_holes(round_id, hole, strokes, strokes_src) VALUES (?, ?, ?, ?)",
                         (pr.round_id, hole, strokes, KIND))
        elif old["strokes"] != strokes or old["strokes_src"] != KIND:
            conn.execute("UPDATE round_holes SET strokes = ?, strokes_src = ? WHERE round_id = ? AND hole = ?",
                         (strokes, KIND, pr.round_id, hole))
        else:
            continue
        changes += 1
    for hole, old in existing.items():
        if hole in desired or old["strokes_src"] != KIND:
            continue
        if old["stats_src"] is None:
            conn.execute("DELETE FROM round_holes WHERE round_id = ? AND hole = ?", (pr.round_id, hole))
        else:
            conn.execute("UPDATE round_holes SET strokes = NULL, strokes_src = NULL WHERE round_id = ? AND hole = ?",
                         (pr.round_id, hole))
        changes += 1
    return changes


def _write_shots(conn: sqlite3.Connection, pr: ParsedRound) -> bool:
    """Replace the round's shots when they differ from the file (so a re-import is a no-op)."""
    existing = [tuple(r) for r in conn.execute(
        f"SELECT {', '.join(SHOT_COLUMNS)} FROM shots WHERE round_id = ? ORDER BY seq", (pr.round_id,))]
    desired = [tuple(s[c] for c in SHOT_COLUMNS) for s in pr.shots]
    if existing == desired:
        return False
    conn.execute("DELETE FROM shots WHERE round_id = ?", (pr.round_id,))
    conn.executemany(f"INSERT INTO shots ({', '.join(SHOT_COLUMNS)}) VALUES ({', '.join('?' for _ in SHOT_COLUMNS)})",
                     desired)
    return True


def _upsert_round(conn: sqlite3.Connection, pr: ParsedRound, import_id: int) -> tuple[str, int]:
    row = pr.row()
    old = conn.execute("SELECT * FROM rounds WHERE round_id = ?", (pr.round_id,)).fetchone()
    if old is not None and pr.array_len == 9 and old["nine"] in ("front", "back"):
        row["nine"] = old["nine"]           # resolved from courses.yaml; the export can't know it
    if old is None:
        row.update(first_import_id=import_id, last_import_id=import_id)
        conn.execute(f"INSERT INTO rounds ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                     list(row.values()))
        outcome = "inserted"
    elif any(old[c] != v for c, v in row.items()):
        outcome = "restored" if old["deleted_in_source"] else "updated"
        conn.execute(f"UPDATE rounds SET {', '.join(f'{c} = ?' for c in row)}, last_import_id = ? "
                     "WHERE round_id = ?", [*row.values(), import_id, pr.round_id])
    else:
        conn.execute("UPDATE rounds SET last_import_id = ? WHERE round_id = ?", (import_id, pr.round_id))
        outcome = "unchanged"
    return outcome, _write_holes(conn, pr, row["nine"])


def _mark_deleted(conn: sqlite3.Connection, round_ids: list[str]) -> int:
    cur = conn.execute(
        "UPDATE rounds SET deleted_in_source = 1 WHERE source = ? AND deleted_in_source = 0 "
        "AND round_id NOT IN (SELECT value FROM json_each(?))", (KIND, dumps(round_ids)))
    return cur.rowcount


def _summary(**overrides: Any) -> dict:
    base = {"import_id": None, "file": None, "sha256": None, "snapshot_date": None, "parser_version": PARSER_VERSION,
            "already_imported": False, "reapplied": False, "stale_snapshot": False, "rounds_in_file": 0,
            "round_count_field": None, "max_round_utc": None,
            "inserted": 0, "updated": 0, "unchanged": 0, "restored": 0, "flagged": 0, "abandoned": 0,
            "deleted_in_source": 0, "holes_written": 0, "clubs": 0, "by_entry_mode": {},
            "shots_in_file": 0, "shot_rounds_written": 0, "putts_partial": 0, "account_fp": None,
            "account": None, "unknown_keys": [], "warnings": []}
    base.update(overrides)
    return base


def _account_status(fp: str | None, pinned: str | None) -> str:
    """'same' | 'new' (nothing pinned yet) | 'different' | 'unknown' (the file has no account id)."""
    if fp is None:
        return "unknown"
    return "new" if pinned is None else "same" if fp == pinned else "different"


def _pin_account(conn: sqlite3.Connection, fp: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (ACCOUNT_KEY, fp))


def import_export(conn: sqlite3.Connection, path: Path | str, cfg: Config, *, new_account: bool = False) -> dict:
    """Import one export snapshot. Idempotent: the same file (by sha256) is detected and changes nothing.

    A snapshot older than one already imported is recorded but not applied, so an old file can
    neither overwrite newer edits nor mark newer rounds as deleted. A file imported before by an
    older PARSER_VERSION is re-applied (same imports row), so fields the importer learned to read
    since (shots, roundHandicap, ...) are filled in without a new download.

    An export of another 18Birdies account than the pinned one raises ExportAccountError before
    anything is written, unless new_account=True (which re-pins the account).
    """
    path = Path(path)
    blob = path.read_bytes()
    sha = hashlib.sha256(blob).hexdigest()
    prior = conn.execute("SELECT import_id, summary, unknown_keys FROM imports WHERE kind = ? AND sha256 = ?",
                         (KIND, sha)).fetchone()
    earlier = loads(prior["summary"], {}) if prior is not None else {}
    if prior is not None and earlier.get("parser_version", 1) >= PARSER_VERSION:
        return _summary(import_id=prior["import_id"], file=path.name, sha256=sha, already_imported=True,
                        snapshot_date=earlier.get("snapshot_date"),
                        rounds_in_file=earlier.get("rounds_in_file", 0),
                        unknown_keys=loads(prior["unknown_keys"], []))

    try:
        data = json.loads(blob)
    except json.JSONDecodeError as e:
        raise ExportFormatError(f"not valid JSON (line {e.lineno})") from None
    parsed = parse_archive(data, cfg.timezone)
    del data, blob                          # the PII sections go no further than this frame
    account = _account_status(parsed.account_fp, pinned_account(conn))
    if account == "different" and not new_account:
        raise foreign_account_error(path)

    tz = ZoneInfo(cfg.timezone)
    today = datetime.now(tz).date()
    snap, snap_warnings = resolve_snapshot_date(path, tz, today=today)
    if prior is not None and earlier.get("snapshot_date") and earlier["snapshot_date"] <= today.isoformat():
        snap, snap_warnings = earlier["snapshot_date"], []          # re-applied: keep the date it had
    newest = max((r.played_at_utc for r in parsed.rounds), default=None)
    latest = _latest_snapshot(conn, today)
    summary = _summary(file=path.name, sha256=sha, snapshot_date=snap, reapplied=prior is not None,
                       stale_snapshot=_is_stale(snap, newest, latest), max_round_utc=newest,
                       rounds_in_file=len(parsed.rounds), round_count_field=parsed.round_count,
                       flagged=sum(1 for r in parsed.rounds if r.flags),
                       abandoned=sum(1 for r in parsed.rounds if r.entry_mode == "abandoned"),
                       clubs=len(parsed.clubs), unknown_keys=parsed.unknown_keys,
                       by_entry_mode=dict(Counter(r.entry_mode for r in parsed.rounds)),
                       shots_in_file=sum(len(r.shots) for r in parsed.rounds),
                       putts_partial=sum(1 for r in parsed.rounds if "putts_partial" in r.flags),
                       account_fp=parsed.account_fp, account=account)
    summary["warnings"] += snap_warnings
    if parsed.round_count is not None and parsed.round_count != len(parsed.rounds):
        summary["warnings"].append(f"roundCount says {parsed.round_count} but {len(parsed.rounds)} rounds listed")
    skipped = sum(r.shots_skipped for r in parsed.rounds)
    if skipped:
        summary["warnings"].append(f"{skipped} malformed shot entr{'y' if skipped == 1 else 'ies'} skipped")
    if summary["stale_snapshot"]:
        summary["warnings"].append(f"snapshot {snap} is older than the latest import ({latest[0]}); not applied")
    if prior is not None:
        summary["warnings"].append(f"{path.name} was imported by an older version of the importer; re-applied")

    with conn:
        if prior is None:
            cur = conn.execute("INSERT INTO imports(kind, path, sha256, imported_at, unknown_keys) "
                               "VALUES (?, ?, ?, ?, ?)", (KIND, str(path), sha, now_iso(), dumps(parsed.unknown_keys)))
            import_id = cur.lastrowid
        else:
            import_id = prior["import_id"]
            conn.execute("UPDATE imports SET unknown_keys = ? WHERE import_id = ?",
                         (dumps(parsed.unknown_keys), import_id))
        summary["import_id"] = import_id
        if parsed.account_fp and (account == "new" or (account == "different" and new_account)):
            _pin_account(conn, parsed.account_fp)
        if account == "different":
            summary["warnings"].append("an export of a different 18Birdies account: imported because you said "
                                       "--new-account; exports of the old account are now refused")
        if not summary["stale_snapshot"]:
            _upsert_clubs(conn, parsed.clubs, {r.club_id for r in parsed.rounds})
            for pr in parsed.rounds:
                outcome, holes = _upsert_round(conn, pr, import_id)
                summary[outcome] += 1
                summary["holes_written"] += holes
                summary["shot_rounds_written"] += _write_shots(conn, pr)
            if parsed.rounds:
                summary["deleted_in_source"] = _mark_deleted(conn, [r.round_id for r in parsed.rounds])
            else:
                summary["warnings"].append("snapshot has no rounds; deletions not applied")
        conn.execute("UPDATE imports SET summary = ? WHERE import_id = ?", (dumps(summary), import_id))
    return summary


def preview_export(conn: sqlite3.Connection, path: Path | str, cfg: Config) -> dict:
    """What importing `path` would do, without writing anything: whose export it is (account: same /
    new / different / unknown), whether it is an older snapshot, and how many live export rounds it
    would mark deleted in 18Birdies. The watcher applies a file on its own only when this is harmless.
    Raises ExportFormatError for a file that is not an export."""
    path = Path(path)
    parsed = parse_archive(load_archive(path), cfg.timezone)
    tz = ZoneInfo(cfg.timezone)
    today = datetime.now(tz).date()
    snap, _ = resolve_snapshot_date(path, tz, today=today)
    newest = max((r.played_at_utc for r in parsed.rounds), default=None)
    stale = _is_stale(snap, newest, _latest_snapshot(conn, today))
    ids = [r.round_id for r in parsed.rounds]
    live = conn.execute("SELECT COUNT(*) FROM rounds WHERE source = ? AND deleted_in_source = 0 "
                        "AND round_id NOT LIKE 'demo-%'", (KIND,)).fetchone()[0]
    would_delete = 0 if stale or not ids else conn.execute(
        "SELECT COUNT(*) FROM rounds WHERE source = ? AND deleted_in_source = 0 AND round_id NOT LIKE 'demo-%' "
        "AND round_id NOT IN (SELECT value FROM json_each(?))", (KIND, dumps(ids))).fetchone()[0]
    return {"account": _account_status(parsed.account_fp, pinned_account(conn)), "account_fp": parsed.account_fp,
            "stale": stale, "rounds_in_file": len(ids), "live_rounds": live, "would_delete": would_delete}


# ------------------------------------------------------------------ inspection
def _type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    return {int: "int", float: "float", str: "str", list: "list", dict: "object"}.get(type(v), type(v).__name__)


def _shape(values: list) -> str:
    parts = []
    for t in sorted({_type_name(v) for v in values}):
        if t != "list":
            parts.append(t)
            continue
        lists = [v for v in values if isinstance(v, list)]
        lo, hi = min(map(len, lists)), max(map(len, lists))
        inner = sorted({_type_name(x) for lst in lists for x in lst})
        parts.append(f"list[{lo if lo == hi else f'{lo}..{hi}'}]" + (f" of {'|'.join(inner)}" if inner else ""))
    return " | ".join(parts)


def _walk(values: list, depth: int, lines: list[str]) -> None:
    objs = [v for v in values if isinstance(v, dict)]
    objs += [x for v in values if isinstance(v, list) for x in v if isinstance(x, dict)]
    children: dict[str, list] = {}
    for obj in objs:
        for k, v in obj.items():
            children.setdefault(_key_label(k), []).append(v)
    for label, vals in children.items():
        if label == "<id>":
            note = f"  ({len(vals)} id-like keys)"
        else:
            note = f"  (in {len(vals)}/{len(objs)})" if len(vals) != len(objs) else ""
        lines.append(f"{'  ' * depth}{label}: {_shape(vals)}{note}")
        _walk(vals, depth + 1, lines)


def inspect_export(path: Path | str) -> str:
    """Key tree of an export with types and counts only, never values (safe to paste anywhere)."""
    path = Path(path)
    data = load_archive(path)
    lines = [f"{path.name}: key tree (types and counts only, no values)", f"<root>: {_shape([data])}"]
    _walk([data], 1, lines)
    lines.append("")
    try:
        parsed = parse_archive(data, "UTC")
    except ExportFormatError as e:
        lines.append(f"Schema check FAILED: {e}")
        return "\n".join(lines)
    modes = Counter(r.entry_mode for r in parsed.rounds)
    lines.append(f"Schema check OK: {len(parsed.rounds)} rounds, {len(parsed.clubs)} played clubs")
    lines.append("Entry modes: " + ", ".join(f"{k}={v}" for k, v in sorted(modes.items())))
    flags = Counter(f for r in parsed.rounds for f in r.flags)
    if flags:
        lines.append("Flags: " + ", ".join(f"{k}={v}" for k, v in sorted(flags.items())))
    shots = [s for r in parsed.rounds for s in r.shots]
    if shots:
        clubs = Counter(s["club"] or "unrecognised" for s in shots)
        lines.append(f"GPS shots: {len(shots)} on {sum(1 for r in parsed.rounds if r.shots)} rounds; clubs: "
                     + ", ".join(f"{k}={v}" for k, v in sorted(clubs.items())))
    with_hcp = sum(1 for r in parsed.rounds if r.round_handicap_18b is not None)
    if with_hcp:
        lines.append(f"18Birdies round handicap present on {with_hcp} of {len(parsed.rounds)} rounds")
    lines.append("Unknown keys (logged, not imported): " + (", ".join(parsed.unknown_keys) or "none"))
    return "\n".join(lines)
