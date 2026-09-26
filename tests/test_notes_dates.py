"""Date resolution (golf.notes.dates): every row of the RESEARCH_PLAN §4 table, checked against brute force."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from golf.notes.dates import WEEKDAYS, Resolved, resolve, resolve_headers
from golf.schemas.events import DateRef, EntryHeader

TZ = ZoneInfo("America/New_York")
FULL = {"mon": "monday", "tue": "tuesday", "wed": "wednesday", "thu": "thursday", "fri": "friday",
        "sat": "saturday", "sun": "sunday"}


def ref(**kw) -> DateRef:
    base = dict(expression="", kind="none", year=-1, month=-1, day=-1, time_hhmm="", anchor="na", header_line=-1,
                rel_kind="none", rel_value=0, weekday="none", weekday_rel="none", period="none", approximate=False)
    base.update(kw)
    return DateRef(**base)


def at(y, m, d, h=12, mi=0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=TZ)


def D(s: str) -> date:
    return date.fromisoformat(s)


def hdr(line: int, month: int, day: int, year: int = -1, expr: str | None = None) -> EntryHeader:
    return EntryHeader(line=line, date=ref(kind="absolute", month=month, day=day, year=year,
                                           expression=expr or f"{month}/{day}"))


# ------------------------------------------------------------------ weekdays at every anchor weekday
ANCHORS = [date(2026, 3, 9) + timedelta(days=i) for i in range(7)]          # Mon 2026-03-09 .. Sun 03-15


def brute(anchor: date, wd: str, rel: str, planned: bool) -> date:
    t = WEEKDAYS.index(wd)
    if rel == "next" or (planned and rel in ("unspecified", "this")):
        return next(anchor + timedelta(k) for k in range(1, 8) if (anchor + timedelta(k)).weekday() == t)
    if rel == "this":
        mon = anchor - timedelta(anchor.weekday())
        return mon + timedelta(t)
    return next(anchor - timedelta(k) for k in range(1, 8) if (anchor - timedelta(k)).weekday() == t)


@pytest.mark.parametrize("anchor", ANCHORS, ids=lambda d: d.strftime("%a"))
@pytest.mark.parametrize("wd", WEEKDAYS)
@pytest.mark.parametrize("rel,planned", [("last", False), ("unspecified", False), ("this", False),
                                         ("next", False), ("unspecified", True)])
def test_weekday_every_anchor(anchor, wd, rel, planned):
    expr = FULL[wd] if rel == "unspecified" else f"{rel} {wd}"
    created = datetime.combine(anchor, datetime.min.time(), TZ).replace(hour=20)
    r = resolve(ref(kind="relative", anchor="note_created", rel_kind="weekday", weekday=wd, weekday_rel=rel,
                    expression=expr), created, created, is_planned=planned)
    want = brute(anchor, wd, rel, planned)
    assert r.date == want and r.date_start == r.date_end == want
    assert (r.precision, r.source) == ("day", "relative_reference")
    mon, sun = anchor - timedelta(anchor.weekday()), anchor + timedelta(6 - anchor.weekday())
    assert ("ambiguous_last_weekday" in r.flags) == (rel == "last" and want >= mon)
    assert ("ambiguous_next_weekday" in r.flags) == (rel == "next" and want <= sun)
    if rel in ("last", "unspecified") and not planned:
        assert 1 <= (anchor - want).days <= 7
    # the independent dateparser reading agrees wherever it can parse the words
    assert "date_disagreement" not in r.flags
    assert ("future_date" in r.flags) == (not planned and want > anchor + timedelta(1))


def test_weekday_anchored_to_entry_header():
    headers = resolve_headers([hdr(4, 3, 21)], "chronological", at(2026, 3, 1), at(2026, 3, 22))
    assert headers[4].date == D("2026-03-21")                                    # a Saturday
    r = resolve(ref(kind="relative", anchor="entry_header", header_line=4, rel_kind="weekday", weekday="sat",
                    weekday_rel="last", expression="last sat"), at(2026, 3, 1), at(2026, 3, 22), headers=headers)
    assert r.date == D("2026-03-14") and r.source == "relative_reference"
    assert "header L004" in r.reasoning and "year_inferred" in r.flags          # inherited, informational


# ------------------------------------------------------------------ offsets
@pytest.mark.parametrize("expr,offset,want", [("yesterday", -1, "2026-04-14"), ("today", 0, "2026-04-15"),
                                              ("tomorrow", 1, "2026-04-16"), ("3 days ago", -3, "2026-04-12")])
def test_offset_days_from_note_created(expr, offset, want):
    created = at(2026, 4, 15, 23, 30)                                          # late evening local time
    r = resolve(ref(kind="relative", anchor="note_created", rel_kind="offset_days", rel_value=offset, expression=expr),
                created, created, is_planned=offset > 0)
    assert r.date == D(want) and r.precision == "day" and not r.flags


def test_offset_days_rolls_over_new_year():
    r = resolve(ref(kind="relative", anchor="note_created", rel_kind="offset_days", rel_value=-1, expression="yesterday"),
                at(2026, 1, 1), at(2026, 1, 1))
    assert r.date == D("2025-12-31")


# ------------------------------------------------------------------ periods
def test_last_week_is_the_previous_monday_to_sunday():
    r = resolve(ref(kind="relative", anchor="note_created", rel_kind="period", period="week", rel_value=-1,
                    expression="last week"), at(2026, 4, 15), at(2026, 4, 15))
    assert (r.date_start, r.date_end, r.precision) == (D("2026-04-06"), D("2026-04-12"), "week")
    assert r.date_start <= r.date <= r.date_end and "date_disagreement" not in r.flags


def test_this_week_is_clipped_at_the_anchor_for_past_events():
    r = resolve(ref(kind="relative", anchor="note_created", rel_kind="period", period="week", rel_value=0,
                    expression="this week"), at(2026, 4, 15), at(2026, 4, 15))
    assert (r.date_start, r.date_end) == (D("2026-04-13"), D("2026-04-15"))


@pytest.mark.parametrize("anchor,k,want", [
    ((2026, 1, 20), -1, ("2025-12-01", "2025-12-31")),                        # Jan -> Dec of the previous year
    ((2026, 1, 20), 0, ("2026-01-01", "2026-01-20")),
    ((2026, 3, 31), -1, ("2026-02-01", "2026-02-28")),
])
def test_month_periods(anchor, k, want):
    expr = "last month" if k else "this month"
    r = resolve(ref(kind="relative", anchor="note_created", rel_kind="period", period="month", rel_value=k,
                    expression=expr), at(*anchor), at(*anchor))
    assert (r.date_start.isoformat(), r.date_end.isoformat(), r.precision) == (*want, "month")
    assert "date_disagreement" not in r.flags


@pytest.mark.parametrize("anchor,k,planned,want,flag", [
    ((2026, 4, 13), -1, False, ("2026-04-11", "2026-04-12"), None),           # Monday: last weekend = 2 days ago
    ((2026, 4, 18), -1, False, ("2026-04-11", "2026-04-12"), None),           # Saturday: the one before
    ((2026, 4, 15), 0, True, ("2026-04-18", "2026-04-19"), None),             # planned "this weekend"
    ((2026, 4, 15), 0, False, ("2026-04-11", "2026-04-12"), "ambiguous_weekend"),
])
def test_weekends(anchor, k, planned, want, flag):
    r = resolve(ref(kind="relative", anchor="note_created", rel_kind="period", period="weekend", rel_value=k,
                    expression="weekend"), at(*anchor), at(*anchor), is_planned=planned)
    assert (r.date_start.isoformat(), r.date_end.isoformat()) == want and r.date == r.date_start
    assert (flag in r.flags) if flag else "ambiguous_weekend" not in r.flags


def test_last_year_is_coarse():
    r = resolve(ref(kind="relative", anchor="note_created", rel_kind="period", period="year", rel_value=-1,
                    expression="last year"), at(2026, 4, 15), at(2026, 4, 15))
    assert (r.date_start, r.date_end, r.precision) == (D("2025-01-01"), D("2025-12-31"), "inferred")
    assert "coarse_period" in r.flags


# ------------------------------------------------------------------ approximate
def test_approximate_day_widens_to_week():
    r = resolve(ref(kind="absolute", month=3, day=14, year=2026, approximate=True, expression="around 3/14/2026"),
                at(2026, 3, 20), at(2026, 3, 20))
    assert (r.date, r.date_start, r.date_end, r.precision) == (D("2026-03-14"), D("2026-03-11"), D("2026-03-17"), "week")
    assert "approximate" in r.flags


def test_approximate_week_widens_to_month():
    r = resolve(ref(kind="relative", anchor="note_created", rel_kind="period", period="week", rel_value=-2,
                    approximate=True, expression="a couple weeks ago"), at(2026, 4, 15), at(2026, 4, 15))
    assert r.precision == "month" and r.date_start <= D("2026-03-30") and r.date_end >= D("2026-04-05")
    assert (r.date_end - r.date_start).days >= 30


def test_approximate_month_stays_month():
    r = resolve(ref(kind="absolute", month=5, approximate=True, expression="early May"), at(2026, 6, 10), at(2026, 6, 10))
    assert (r.date_start, r.date_end, r.precision) == (D("2026-05-01"), D("2026-05-31"), "month")
    assert {"approximate", "year_inferred"} <= set(r.flags)


# ------------------------------------------------------------------ absolute dates
def test_absolute_with_year():
    r = resolve(ref(kind="absolute", year=2025, month=3, day=14, expression="March 14, 2025"), at(2025, 3, 20), at(2025, 3, 20))
    assert (r.date, r.precision, r.source, r.flags) == (D("2025-03-14"), "day", "explicit_text", ())


def test_absolute_with_time_is_exact():
    r = resolve(ref(kind="absolute", month=3, day=14, time_hhmm="17:30", expression="3/14 5:30pm"),
                at(2026, 3, 14, 20), at(2026, 3, 14, 20))
    assert r.precision == "exact" and r.date == D("2026-03-14")


def test_year_inferred_across_new_year():
    r = resolve(ref(kind="absolute", month=12, day=28, expression="12/28"), at(2026, 1, 5), at(2026, 1, 5))
    assert r.date == D("2025-12-28") and "year_inferred" in r.flags
    assert "date_disagreement" not in r.flags


def test_planned_absolute_looks_ahead():
    r = resolve(ref(kind="absolute", month=3, day=14, expression="3/14"), at(2025, 11, 20), at(2025, 11, 20),
                is_planned=True)
    assert r.date == D("2026-03-14") and "future_date" not in r.flags


def test_unplanned_absolute_outside_window_is_flagged():
    r = resolve(ref(kind="absolute", month=3, day=14, expression="3/14"), at(2025, 11, 20), at(2025, 11, 20))
    assert r.date == D("2025-03-14") and "out_of_window" in r.flags


def test_month_only_without_year():
    r = resolve(ref(kind="absolute", month=5, expression="May"), at(2026, 6, 10), at(2026, 6, 10))
    assert (r.date_start, r.date_end, r.precision) == (D("2026-05-01"), D("2026-05-31"), "month")


def test_sanity_flags():
    far = resolve(ref(kind="absolute", year=2019, month=6, day=1, expression="6/1/2019"), at(2026, 3, 1), at(2026, 3, 1))
    assert "far_past" in far.flags
    fut = resolve(ref(kind="absolute", year=2026, month=5, day=1, expression="5/1/2026"), at(2026, 3, 1), at(2026, 3, 1))
    assert "future_date" in fut.flags
    planned = resolve(ref(kind="absolute", year=2026, month=5, day=1, expression="5/1/2026"), at(2026, 3, 1),
                      at(2026, 3, 1), is_planned=True)
    assert "future_date" not in planned.flags


def test_invalid_date_falls_back_and_is_flagged():
    r = resolve(ref(kind="absolute", month=2, day=30, expression="2/30"), at(2026, 3, 1), at(2026, 3, 1))
    assert r.precision == "inferred" and "unparsed_date" in r.flags


def test_dateparser_disagreement_is_flagged():
    r = resolve(ref(kind="absolute", month=3, day=14, year=2026, expression="March 15 2026"), at(2026, 3, 20), at(2026, 3, 20))
    assert r.date == D("2026-03-14") and "date_disagreement" in r.flags


def test_dateparser_fallback_when_fields_are_empty():
    r = resolve(ref(kind="relative", anchor="note_created", expression="2 weeks ago"), at(2026, 4, 15), at(2026, 4, 15))
    assert r.date == D("2026-04-01") and "parsed_by_dateparser" in r.flags


# ------------------------------------------------------------------ running logs
def test_chronological_log_with_dec_jan_rollover():
    headers = [hdr(2, 12, 27), hdr(3, 12, 30), hdr(4, 1, 2), hdr(5, 1, 6)]
    out = resolve_headers(headers, "chronological", at(2025, 12, 27), at(2026, 1, 6))
    assert [out[i].date.isoformat() for i in (2, 3, 4, 5)] == ["2025-12-27", "2025-12-30", "2026-01-02", "2026-01-06"]
    assert all("non_monotonic" not in r.flags and "year_inferred" in r.flags for r in out.values())


def test_reverse_chronological_log_newest_first():
    headers = [hdr(2, 1, 9), hdr(3, 1, 3), hdr(4, 12, 28), hdr(5, 12, 20)]
    out = resolve_headers(headers, "reverse_chronological", at(2025, 12, 20), at(2026, 1, 9))
    assert [out[i].date.isoformat() for i in (2, 3, 4, 5)] == ["2026-01-09", "2026-01-03", "2025-12-28", "2025-12-20"]
    assert not any("non_monotonic" in r.flags for r in out.values())


def test_multi_year_log_stays_monotone():
    days = [(3, 14), (6, 2), (12, 28), (1, 5), (3, 14), (1, 20)]
    headers = [hdr(i + 2, m, d) for i, (m, d) in enumerate(days)]
    out = resolve_headers(headers, "chronological", at(2024, 3, 1), at(2026, 2, 1))
    assert [out[i + 2].date.isoformat() for i in range(6)] == [
        "2024-03-14", "2024-06-02", "2024-12-28", "2025-01-05", "2025-03-14", "2026-01-20"]


def test_out_of_order_header_is_flagged_but_small_slips_are_not():
    out = resolve_headers([hdr(2, 3, 14), hdr(3, 3, 2)], "chronological", at(2026, 3, 1), at(2026, 3, 20))
    assert out[3].date == D("2026-03-02") and "non_monotonic" in out[3].flags
    ok = resolve_headers([hdr(2, 3, 14), hdr(3, 3, 12)], "chronological", at(2026, 3, 1), at(2026, 3, 20))
    assert "non_monotonic" not in ok[3].flags


def test_explicit_year_header_in_log():
    out = resolve_headers([hdr(2, 3, 14, 2025), hdr(3, 3, 20)], "chronological", at(2025, 3, 10), at(2025, 3, 25))
    assert out[2].date == D("2025-03-14") and out[3].date == D("2025-03-20")


def test_event_inherits_its_header():
    created, modified = at(2025, 12, 27), at(2026, 1, 6)
    headers = resolve_headers([hdr(2, 12, 27), hdr(4, 1, 2)], "chronological", created, modified)
    r = resolve(ref(kind="header", header_line=4), created, modified, headers=headers, note_kind="running_log",
                log_order="chronological", event_line=4)
    assert (r.date, r.precision, r.source) == (D("2026-01-02"), "day", "entry_header")
    assert "inherits header L004" in r.reasoning


def test_bad_header_line_uses_the_governing_header():
    created, modified = at(2026, 3, 1), at(2026, 3, 25)
    headers = resolve_headers([hdr(2, 3, 14), hdr(5, 3, 21)], "chronological", created, modified)
    r = resolve(ref(kind="header", header_line=99), created, modified, headers=headers, note_kind="running_log",
                log_order="chronological", event_line=6)
    assert r.date == D("2026-03-21") and "bad_header_line" in r.flags


def test_undated_event_inside_log_is_bounded_by_neighbours():
    created, modified = at(2026, 3, 1), at(2026, 3, 25)
    headers = resolve_headers([hdr(2, 3, 14), hdr(5, 3, 21)], "chronological", created, modified)
    r = resolve(ref(kind="none"), created, modified, headers=headers, note_kind="running_log",
                log_order="chronological", event_line=3)
    assert (r.date, r.date_start, r.date_end) == (D("2026-03-14"), D("2026-03-14"), D("2026-03-21"))
    assert (r.precision, r.source) == ("inferred", "log_position")
    rev = resolve_headers([hdr(2, 3, 21), hdr(5, 3, 14)], "reverse_chronological", created, modified)
    top = resolve(ref(kind="none"), created, modified, headers=rev, note_kind="running_log",
                  log_order="reverse_chronological", event_line=1)
    assert (top.date_start, top.date_end) == (D("2026-03-21"), D("2026-03-25"))


def test_undated_header_is_placed_by_position():
    headers = [hdr(2, 3, 14), EntryHeader(line=4, date=ref(kind="none", expression="")), hdr(6, 3, 21)]
    out = resolve_headers(headers, "chronological", at(2026, 3, 1), at(2026, 3, 25))
    assert (out[4].date_start, out[4].date_end, out[4].source) == (D("2026-03-14"), D("2026-03-21"), "log_position")


# ------------------------------------------------------------------ no date words, unreliable anchors, doc dates
def test_single_entry_without_date_uses_note_created():
    r = resolve(ref(kind="none"), at(2026, 3, 14, 19), at(2026, 3, 14, 19), note_kind="single_entry")
    assert (r.date, r.precision, r.source) == (D("2026-03-14"), "inferred", "note_created")


def test_created_unreliable_downgrades_relative_dates():
    r = resolve(ref(kind="relative", anchor="note_created", rel_kind="offset_days", rel_value=-1, expression="yesterday"),
                at(2026, 3, 14), at(2026, 3, 14), created_unreliable=True)
    assert r.precision == "inferred" and "created_unreliable" in r.flags
    u = resolve(ref(kind="none"), at(2026, 3, 14), at(2026, 3, 14), created_unreliable=True)
    assert "created_unreliable" in u.flags


def test_doc_date_overrides_note_created():
    d = D("2026-09-20")
    manual = Resolved(d, d, d, "day", "manual", "entry date")
    r = resolve(ref(kind="none"), at(2026, 9, 25), at(2026, 9, 25), doc_date=manual)
    assert (r.date, r.precision, r.source) == (d, "day", "manual")
    y = resolve(ref(kind="relative", anchor="note_created", rel_kind="offset_days", rel_value=-1, expression="yesterday"),
                at(2026, 9, 25), at(2026, 9, 25), doc_date=manual)
    assert y.date == D("2026-09-19") and y.precision == "day"


def test_relative_to_a_month_precise_page_date_is_inferred():
    s, e = D("2019-03-01"), D("2019-03-31")
    page = Resolved(D("2019-03-16"), s, e, "month", "entry_header", "page date 'Mar 2019'")
    r = resolve(ref(kind="relative", anchor="note_created", rel_kind="offset_days", rel_value=-1, expression="yesterday"),
                at(2019, 3, 1), at(2019, 4, 1), doc_date=page)
    assert r.precision == "inferred" and r.date_start == D("2019-02-28") and r.date_end == D("2019-03-30")


def test_resolved_is_the_documented_tuple():
    r = resolve(ref(kind="none"), at(2026, 3, 14), at(2026, 3, 14))
    date_, start, end, precision, source, reasoning, flags = r
    assert (date_, precision, source) == (D("2026-03-14"), "inferred", "note_created") and isinstance(flags, tuple)
