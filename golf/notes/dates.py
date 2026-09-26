"""Deterministic date resolution for timeline events (RESEARCH_PLAN §4, "Date resolution").

The model only DESCRIBES dates (golf.schemas.events.DateRef); every calendar calculation happens here,
where it is testable and cannot be off by one. Each result carries a reasoning string that the review
screen shows ("'last sat' <- header L004 2026-03-21 (Sat) -> 2026-03-14") plus flags. Any flag other
than the informational ones keeps an event out of auto-accept.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta
from typing import Iterable, Mapping, NamedTuple

from golf.schemas.events import DateRef, EntryHeader

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
FINE = ("exact", "day")
INFO_FLAGS = frozenset({"year_inferred"})

ABS_BACK_DAYS = 180        # a year-less "3/14" is looked for in [created - 180 d, modified + 7 d]
ABS_AHEAD_DAYS = 7
PLANNED_AHEAD_DAYS = 180   # ... or up to modified + 180 d when it is a planned event
LOG_BACK_DAYS = 14         # running-log headers live in [created - 14 d, modified + 1 d]
LOG_AHEAD_DAYS = 1
LOG_SLACK_DAYS = 3         # headers must be >= previous header - 3 d
FAR_PAST_DAYS = 365


class Resolved(NamedTuple):
    date: date | None
    date_start: date | None
    date_end: date | None
    precision: str          # exact | day | week | month | inferred
    source: str             # explicit_text | relative_reference | entry_header | note_created | log_position | manual
    reasoning: str
    flags: tuple[str, ...] = ()

    def with_flags(self, *flags: str) -> "Resolved":
        return self._replace(flags=tuple(dict.fromkeys(self.flags + tuple(f for f in flags if f))))

    def note(self, text: str) -> "Resolved":
        return self._replace(reasoning=f"{self.reasoning}; {text}" if self.reasoning else text)


def blocking_flags(flags: Iterable[str]) -> list[str]:
    return [f for f in flags if f not in INFO_FLAGS]


# ------------------------------------------------------------------ calendar helpers
def _d(x: date | datetime) -> date:
    return x.date() if isinstance(x, datetime) else x


def _noon(d: date) -> datetime:
    return datetime.combine(d, time(12))


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _mid(s: date, e: date) -> date:
    return s + (e - s) // 2


def _wd(d: date) -> str:
    return d.strftime("%a")


def month_bounds(y: int, m: int) -> tuple[date, date]:
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def shift_month(y: int, m: int, k: int) -> tuple[int, int]:
    idx = y * 12 + (m - 1) + k
    return idx // 12, idx % 12 + 1


def week_bounds(d: date) -> tuple[date, date]:
    mon = d - timedelta(days=d.weekday())
    return mon, mon + timedelta(days=6)


def _day(d: date, source: str, reasoning: str, precision: str = "day", flags: Iterable[str] = ()) -> Resolved:
    return Resolved(d, d, d, precision, source, reasoning, tuple(flags))


def _span(s: date, e: date, precision: str, source: str, reasoning: str, flags: Iterable[str] = (),
          rep: date | None = None) -> Resolved:
    return Resolved(rep or _mid(s, e), s, e, precision, source, reasoning, tuple(flags))


def _dist(c: date, lo: date, hi: date) -> int:
    return 0 if lo <= c <= hi else min(abs((c - lo).days), abs((c - hi).days))


# ------------------------------------------------------------------ primitive rules
def weekday_resolve(anchor: date, weekday: str, rel: str, planned: bool) -> tuple[date, tuple[str, ...]]:
    """'last'/unspecified -> strictly before the anchor (1-7 days back); 'next', or a planned event ->
    strictly after; 'this' -> the same Mon-Sun week. Flags the readings people disagree on
    ("last Tue" said on a Thursday could be two or nine days back)."""
    t, aw = WEEKDAYS.index(weekday), anchor.weekday()
    if rel == "next" or (planned and rel in ("unspecified", "this", "none")):
        ahead = (t - aw - 1) % 7 + 1
        ambiguous = rel == "next" and aw + ahead <= 6
        return anchor + timedelta(days=ahead), ("ambiguous_next_weekday",) if ambiguous else ()
    if rel == "this":
        return anchor + timedelta(days=t - aw), ()
    back = (aw - t - 1) % 7 + 1
    ambiguous = rel == "last" and back <= aw
    return anchor - timedelta(days=back), ("ambiguous_last_weekday",) if ambiguous else ()


def period_bounds(anchor: date, period: str, k: int, planned: bool) -> tuple[date, date, str, tuple[str, ...]]:
    """Monday-anchored weeks and calendar months shifted by k. The current period is clipped at the
    anchor for past events: 'this month' written on the 10th happened between the 1st and the 10th."""
    flags: tuple[str, ...] = ()
    if period == "week":
        s = anchor - timedelta(days=anchor.weekday()) + timedelta(weeks=k)
        e, precision = s + timedelta(days=6), "week"
    elif period == "weekend":
        if k == 0 and not planned and anchor.weekday() < 5:
            s = anchor - timedelta(days=anchor.weekday() + 2)      # the weekend just past
            flags = ("ambiguous_weekend",)
        else:
            s = anchor - timedelta(days=anchor.weekday()) + timedelta(days=5, weeks=k)
        e, precision = s + timedelta(days=1), "week"
    elif period == "month":
        s, e = month_bounds(*shift_month(anchor.year, anchor.month, k))
        precision = "month"
    else:  # year: no 'year' precision in the schema, so it is 'inferred' with the year as bounds
        s, e = date(anchor.year + k, 1, 1), date(anchor.year + k, 12, 31)
        precision, flags = "inferred", ("coarse_period",)
    if k == 0 and not planned and s <= anchor < e:
        e = anchor
    return s, e, precision, flags


def pick_year(month: int, day: int, lo: date, hi: date, prefer: date, planned: bool) -> tuple[date | None, tuple[str, ...]]:
    """Year for a year-less month/day: a candidate inside [lo, hi], nearest `prefer` (planned events:
    the first one on/after it). Outside the window the nearest candidate is used and flagged."""
    cands = [c for y in range(lo.year - 1, hi.year + 2) if (c := _safe_date(y, month, day))]
    if not cands:
        return None, ("invalid_date",)
    inside = [c for c in cands if lo <= c <= hi]
    if not inside:
        return min(cands, key=lambda c: (_dist(c, lo, hi), -c.toordinal())), ("out_of_window",)
    flags = ("year_ambiguous",) if len(inside) > 1 else ()
    if planned:
        ahead = [c for c in inside if c >= prefer - timedelta(days=1)]
        if ahead:
            return min(ahead), flags
    return min(inside, key=lambda c: (abs((c - prefer).days), c > prefer)), flags


def widen(r: Resolved) -> Resolved:
    """'approximate' dates: day -> week (+-3 d), week -> month (+-15 d). Coarser stays as it is."""
    if r.date is None:
        return r
    if r.precision in FINE:
        s, e, p = r.date - timedelta(days=3), r.date + timedelta(days=3), "week"
    elif r.precision == "week":
        s, e, p = min(r.date_start, r.date - timedelta(days=15)), max(r.date_end, r.date + timedelta(days=15)), "month"
    else:
        return r.with_flags("approximate").note("approximate")
    return Resolved(r.date, s, e, p, r.source, f"{r.reasoning}; approximate -> widened to {p} {s}..{e}",
                    r.flags).with_flags("approximate")


# ------------------------------------------------------------------ anchors
def _governing(headers: Mapping[int, Resolved], line: int) -> tuple[int, Resolved] | None:
    """Nearest dated header at or above `line` (document order): the entry an event sits in."""
    above = [(ln, r) for ln, r in headers.items() if r.date and 0 < ln <= line]
    return max(above, key=lambda x: x[0]) if above and line > 0 else None


def doc_anchor(created: datetime | date, created_unreliable: bool = False, doc_date: Resolved | None = None) -> Resolved:
    if doc_date is not None:
        return doc_date
    d = _d(created)
    if created_unreliable:
        return _day(d, "note_created", f"note created {d} ({_wd(d)}, unreliable)", "inferred", ("created_unreliable",))
    return _day(d, "note_created", f"note created {d} ({_wd(d)})")


def _anchor(ref: DateRef, created, headers: Mapping[int, Resolved], event_line: int, created_unreliable: bool,
            doc_date: Resolved | None) -> tuple[Resolved, str]:
    if ref.anchor == "entry_header" or (ref.anchor == "na" and ref.header_line in headers):
        line, h = ref.header_line, headers.get(ref.header_line)
        flags: tuple[str, ...] = ()
        if h is None or h.date is None:
            gov = _governing(headers, event_line)
            line, h = gov if gov else (line, None)
            flags = ("bad_header_line",)
        if h is not None and h.date is not None:
            return h.with_flags(*flags), f"header L{line:03d} {h.date} ({_wd(h.date)})"
        a = doc_anchor(created, created_unreliable, doc_date).with_flags("bad_header_line")
        return a, a.reasoning
    a = doc_anchor(created, created_unreliable, doc_date)
    return a, a.reasoning


# ------------------------------------------------------------------ resolution by kind
def _from_header(ref: DateRef, headers: Mapping[int, Resolved], event_line: int) -> Resolved | None:
    line, h, flags = ref.header_line, headers.get(ref.header_line), ()
    if h is None:
        gov = _governing(headers, event_line)
        if gov is None:
            return None
        (line, h), flags = gov, ("bad_header_line",)
    source = h.source if h.source in ("log_position", "note_created") else "entry_header"
    return h._replace(source=source, reasoning=f"inherits header L{line:03d}: {h.reasoning}").with_flags(*flags)


def _absolute(ref: DateRef, created, modified, planned: bool, headers, event_line, created_unreliable,
              doc_date) -> tuple[Resolved | None, datetime]:
    y = ref.year if ref.year > 0 else None
    if y is not None and y < 100:
        y += 2000
    m = ref.month if 1 <= ref.month <= 12 else None
    d = ref.day if 1 <= ref.day <= 31 else None
    lo = _d(created) - timedelta(days=ABS_BACK_DAYS)
    hi = _d(modified) + timedelta(days=PLANNED_AHEAD_DAYS if planned else ABS_AHEAD_DAYS)
    anchor, _ = _anchor(ref, created, headers, event_line, created_unreliable, doc_date) \
        if ref.anchor == "entry_header" else (doc_anchor(created, created_unreliable, doc_date), "")
    prefer = anchor.date or _d(created)
    base = _noon(prefer if planned else hi)
    expr = ref.expression.strip() or "/".join(str(x) for x in (m, d, y) if x)
    unreliable = ("created_unreliable",) if created_unreliable and doc_date is None else ()
    if m and d:
        if y:
            dt = _safe_date(y, m, d)
            if dt is None:
                return None, base
            r = _day(dt, "explicit_text", f"'{expr}' -> {dt}")
        else:
            dt, fl = pick_year(m, d, lo, hi, prefer, planned)
            if dt is None:
                return None, base
            r = _day(dt, "explicit_text",
                     f"'{expr}' (no year) -> {dt}, the nearest to {prefer}",
                     flags=("year_inferred",) + fl + unreliable)
        return r._replace(precision="exact" if ref.time_hhmm.strip() else "day"), base
    if m:
        if y:
            s, e = month_bounds(y, m)
            return _span(s, e, "month", "explicit_text", f"'{expr}' -> month {s:%Y-%m}"), base
        mid, fl = pick_year(m, 15, lo - timedelta(days=15), hi + timedelta(days=15), prefer, planned)
        s, e = month_bounds(mid.year, m)
        return _span(s, e, "month", "explicit_text", f"'{expr}' (no year) -> month {s:%Y-%m}",
                     ("year_inferred",) + fl + unreliable), base
    if y:
        s, e = date(y, 1, 1), date(y, 12, 31)
        return _span(s, e, "inferred", "explicit_text", f"'{expr}' -> year {y} only", ("coarse_period",)), base
    if d:
        cand = None
        for k in range(0, 13):
            yy, mm = shift_month(prefer.year, prefer.month, k if planned else -k)
            c = _safe_date(yy, mm, d)
            if c and (c >= prefer if planned else c <= prefer):
                cand = c
                break
        if cand is None:
            return None, base
        return _day(cand, "explicit_text", f"'{expr}' (day of month only) -> {cand}",
                    flags=("month_inferred",) + unreliable), base
    return None, base


def _relative(ref: DateRef, created, planned: bool, headers, event_line, created_unreliable,
              doc_date) -> tuple[Resolved | None, datetime]:
    anchor, label = _anchor(ref, created, headers, event_line, created_unreliable, doc_date)
    a = anchor.date
    base = _noon(a)
    expr = ref.expression.strip() or ref.rel_kind
    rk = ref.rel_kind
    if rk == "none":
        rk = "weekday" if ref.weekday != "none" else "period" if ref.period != "none" else "none"
    if rk == "offset_days":
        d = a + timedelta(days=ref.rel_value)
        r = _day(d, "relative_reference", f"'{expr}' <- {label} {ref.rel_value:+d} d -> {d}")
    elif rk == "weekday" and ref.weekday != "none":
        d, fl = weekday_resolve(a, ref.weekday, ref.weekday_rel, planned)
        r = _day(d, "relative_reference", f"'{expr}' <- {label} -> {d} ({_wd(d)})", flags=fl)
    elif rk == "period" and ref.period != "none":
        s, e, p, fl = period_bounds(a, ref.period, ref.rel_value, planned)
        r = _span(s, e, p, "relative_reference", f"'{expr}' <- {label} -> {ref.period} {s}..{e}", fl,
                  rep=s if ref.period == "weekend" else None)
    else:
        return None, base
    if anchor.precision not in FINE:
        delta = r.date - a
        r = Resolved(r.date, min(r.date_start, anchor.date_start + delta), max(r.date_end, anchor.date_end + delta),
                     "inferred", r.source, f"{r.reasoning}; anchor is only {anchor.precision}-precise", r.flags)
    return r.with_flags(*anchor.flags), base


def _log_position(line: int, headers: Mapping[int, Resolved], log_order: str, created, modified,
                  created_unreliable: bool) -> Resolved:
    """No date words inside a running log: bounded by the neighbouring headers in chronological order."""
    dated = sorted((ln, r) for ln, r in headers.items() if r.date and r.source != "log_position")
    above = next(((ln, r) for ln, r in reversed(dated) if ln <= line), None)
    below = next(((ln, r) for ln, r in dated if ln > line), None)
    older, newer = (below, above) if log_order == "reverse_chronological" else (above, below)
    lower = older[1].date_start if older else _d(created)
    upper = newer[1].date_end if newer else _d(modified)
    flags: list[str] = []
    if (not older or not newer) and created_unreliable:
        flags.append("created_unreliable")
    if lower > upper:
        lower, upper = upper, lower
        flags.append("non_monotonic")
    if above:
        rep = above[1].date
    else:
        rep = upper if log_order == "reverse_chronological" else lower
    desc = " and ".join(f"header L{ln:03d} ({r.date})" for ln, r in (x for x in (older, newer) if x)) or "the note dates"
    return Resolved(rep, lower, upper, "inferred", "log_position", f"no date words; between {desc}", tuple(flags))


def _undated(created, modified, headers, note_kind: str, log_order: str, event_line: int, created_unreliable: bool,
             doc_date: Resolved | None) -> Resolved:
    has_dated = any(r.date for r in headers.values())
    if note_kind in ("running_log", "mixed") and has_dated and event_line > 0:
        return _log_position(event_line, headers, log_order, created, modified, created_unreliable)
    a = doc_anchor(created, created_unreliable, doc_date)
    if doc_date is None:
        a = a._replace(precision="inferred")
    return a._replace(reasoning=f"no date words; {a.reasoning}")


def _dateparse(expr: str, base: datetime, prefer: str) -> date | None:
    try:
        import dateparser

        dt = dateparser.parse(expr, languages=["en"], settings={
            "RELATIVE_BASE": base.replace(tzinfo=None), "PREFER_DATES_FROM": prefer,
            "DATE_ORDER": "MDY", "RETURN_AS_TIMEZONE_AWARE": False})
    except Exception:
        return None
    return dt.date() if dt else None


def _cross_check(r: Resolved, ref: DateRef, base: datetime, planned: bool) -> Resolved:
    """dateparser as an independent reader; disagreement beyond the precision bounds is flagged."""
    expr = ref.expression.strip()
    if not expr or r.date is None or r.precision == "inferred":
        return r
    parsed = _dateparse(expr, base, "future" if planned and ref.year <= 0 else "past")
    if parsed is None or r.date_start <= parsed <= r.date_end:
        return r
    return r.with_flags("date_disagreement").note(f"dateparser read '{expr}' as {parsed}")


def _sanity(r: Resolved, created, modified, planned: bool) -> Resolved:
    if r.date is None:
        return r
    flags = []
    if not planned and r.date > _d(modified) + timedelta(days=1):
        flags.append("future_date")
    if r.date < _d(created) - timedelta(days=FAR_PAST_DAYS):
        flags.append("far_past")
    return r.with_flags(*flags)


# ------------------------------------------------------------------ public entry points
def resolve(ref: DateRef, created: datetime | date, modified: datetime | date, *,
            headers: Mapping[int, Resolved] | None = None, is_planned: bool = False,
            note_kind: str = "single_entry", log_order: str = "na", event_line: int = -1,
            created_unreliable: bool = False, doc_date: Resolved | None = None) -> Resolved:
    """Resolve one DateRef to (date, date_start, date_end, precision, source, reasoning, flags).

    created/modified are the note's local datetimes; `headers` are resolved entry headers keyed by line
    (see resolve_headers); `doc_date` overrides what "note created" means (a notebook page's written
    date, or the date given to `golf add`)."""
    headers = headers or {}
    if ref.kind == "header" or (ref.kind == "none" and ref.header_line in headers):
        r = _from_header(ref, headers, event_line)
        if r is not None:
            return r
    r, base = None, _noon(_d(created))
    if ref.kind == "absolute":
        r, base = _absolute(ref, created, modified, is_planned, headers, event_line, created_unreliable, doc_date)
    elif ref.kind == "relative":
        r, base = _relative(ref, created, is_planned, headers, event_line, created_unreliable, doc_date)
    if r is None and ref.kind in ("absolute", "relative") and ref.expression.strip():
        parsed = _dateparse(ref.expression, base, "future" if is_planned else "past")
        if parsed is not None:
            src = "explicit_text" if ref.kind == "absolute" else "relative_reference"
            r = _day(parsed, src, f"'{ref.expression}' read by dateparser -> {parsed}", flags=("parsed_by_dateparser",))
    if r is None:
        r = _undated(created, modified, headers, note_kind, log_order, event_line, created_unreliable, doc_date)
        if ref.kind in ("absolute", "relative"):
            r = r.with_flags("unparsed_date")
        elif ref.kind == "header":
            r = r.with_flags("bad_header_line")
    elif ref.approximate:
        r = widen(r)
    if ref.kind in ("absolute", "relative") and "parsed_by_dateparser" not in r.flags:
        r = _cross_check(r, ref, base, is_planned)
    return _sanity(r, created, modified, is_planned)


def _walk_header(ref: DateRef, line: int, lo: date, hi: date, prev: date | None, created_unreliable: bool) -> Resolved:
    expr = ref.expression.strip() or f"{ref.month}/{ref.day}"
    cands = [c for y in range(lo.year - 3, hi.year + 2) if (c := _safe_date(y, ref.month, ref.day))]
    inside = [c for c in cands if lo <= c <= hi]
    flags: list[str] = ["year_inferred"]
    if created_unreliable:
        flags.append("created_unreliable")
    floor = prev - timedelta(days=LOG_SLACK_DAYS) if prev else None
    if floor is None:
        if inside:
            pick = min(inside)
        else:
            before = [c for c in cands if c <= hi]
            pick = max(before) if before else min(cands, key=lambda c: _dist(c, lo, hi))
            flags.append("out_of_window")
    else:
        ok = [c for c in inside if c >= floor]
        if ok:
            pick = min(ok)
        elif inside:
            pick = max(inside)
            flags.append("non_monotonic")
        else:
            later = [c for c in cands if c >= floor]
            pick = min(later) if later else max(cands)
            flags.append("out_of_window")
            if not later:
                flags.append("non_monotonic")
    after = f" after {prev}" if prev else ""
    return _day(pick, "explicit_text", f"header '{expr}' (no year) -> {pick} (log walk{after})", flags=flags)


def resolve_headers(entry_headers: Iterable[EntryHeader], log_order: str, created: datetime | date,
                    modified: datetime | date, *, created_unreliable: bool = False,
                    doc_date: Resolved | None = None) -> dict[int, Resolved]:
    """Resolve a note's entry headers, keyed by line. In chronological / reverse-chronological logs the
    year-less headers are walked oldest-first and kept monotone (>= previous - 3 d) inside
    [created - 14 d, modified + 1 d]: that is what handles the Dec -> Jan rollover and multi-year logs."""
    ordered = sorted(entry_headers, key=lambda h: h.line)
    if log_order == "reverse_chronological":
        ordered.reverse()
    monotone = log_order in ("chronological", "reverse_chronological") and len(ordered) > 1
    lo = _d(created) - timedelta(days=LOG_BACK_DAYS)
    hi = _d(modified) + timedelta(days=LOG_AHEAD_DAYS)
    out: dict[int, Resolved] = {}
    positional: list[EntryHeader] = []
    prev: date | None = None
    for h in ordered:
        ref = h.date
        if ref.kind in ("none", "header"):
            positional.append(h)
            continue
        if monotone and ref.kind == "absolute" and 1 <= ref.month <= 12 and 1 <= ref.day <= 31 and ref.year <= 0 \
                and not ref.approximate:
            r = _sanity(_walk_header(ref, h.line, lo, hi, prev, created_unreliable), created, modified, False)
        else:
            r = resolve(ref, created, modified, headers=out, note_kind="single_entry", event_line=h.line,
                        created_unreliable=created_unreliable, doc_date=doc_date)
            if monotone and prev and r.date and r.date < prev - timedelta(days=LOG_SLACK_DAYS):
                r = r.with_flags("non_monotonic").note(f"earlier than the previous entry ({prev})")
        out[h.line] = r
        if r.date and r.precision != "inferred":
            prev = r.date
    for h in positional:
        out[h.line] = _log_position(h.line, out, log_order, created, modified, created_unreliable)
    return out
