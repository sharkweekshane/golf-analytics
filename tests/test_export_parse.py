"""18Birdies export parsing: every rule in RESEARCH_PLAN.md §2, idempotency, deletions, privacy."""
from __future__ import annotations

import copy
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from fixtures.synthetic_archive import (
    CASES, DUNES, FAKE_PII, HBH_18_HANDICAP, ORPHAN, PINES, SHOT_BASE_MS, UNKNOWN_ROUND_KEY, UNKNOWN_SECTION,
    UNKNOWN_SHOT_KEY, UNKNOWN_STATS_KEY, make_archive, rounds_of, without_round, write_archive,
)
from golf.db import loads
from golf.ingest.birdies_export import (
    PARSER_VERSION, ExportFormatError, RoundStats, classify, handicap_number, import_export, inspect_export,
    normalize_club, parse_archive, snapshot_date, stats_columns,
)


def _parsed(archive=None, tz="America/New_York"):
    return {r.round_id: r for r in parse_archive(archive or make_archive(), tz).rounds}


def _round(conn, case):
    return conn.execute("SELECT * FROM rounds WHERE round_id = ?", (CASES[case],)).fetchone()


def _holes(conn, case):
    return {r["hole"]: r for r in conn.execute("SELECT * FROM round_holes WHERE round_id = ? ORDER BY hole",
                                               (CASES[case],))}


def _dump(conn) -> list[str]:
    return list(conn.iterdump())


@pytest.fixture
def imported(conn, cfg, tmp_path):
    path = write_archive(tmp_path / "18Birdies_archive_20260101.json", make_archive())
    summary = import_export(conn, path, cfg)
    return conn, summary, path


# ----------------------------------------------------------------- pure rules
@pytest.mark.parametrize("holes, strokes, expected", [
    ([0] * 18, 0, ("abandoned", 0, None)),
    ([0] * 18, 101, ("total_only", 18, None)),
    ([0] * 9, 50, ("total_only", 9, "unknown")),
    ([5] * 18, 90, ("hole_by_hole", 18, None)),
    ([5] * 9 + [0] * 9, 45, ("hole_by_hole", 9, "front")),
    ([0] * 9 + [5] * 9, 45, ("hole_by_hole", 9, "back")),
    ([5] * 9, 45, ("hole_by_hole", 9, "unknown")),
    ([5] * 4 + [0] * 14, 20, ("partial", 4, None)),
    ([5] * 8 + [0, 5] + [0] * 8, 45, ("partial", 9, None)),     # 9 holes, but not a clean nine
])
def test_classify_entry_modes(holes, strokes, expected):
    mode, n, nine, _, _ = classify(holes, strokes)
    assert (mode, n, nine) == expected


def test_classify_sum_mismatch_keeps_mode_but_flags():
    mode, n, _, played, flags = classify([5] * 18, 93)
    assert mode == "hole_by_hole" and n == 18 and "sum_mismatch" in flags and len(played) == 18


def test_parse_every_case():
    p = _parsed()
    expect = {
        "hbh_18": ("hole_by_hole", 18, None), "under_par": ("hole_by_hole", 18, None),
        "total_only_18": ("total_only", 18, None), "total_only_9": ("total_only", 9, "unknown"),
        "front_nine": ("hole_by_hole", 9, "front"), "back_nine": ("hole_by_hole", 9, "back"),
        "nine_array": ("hole_by_hole", 9, "unknown"), "partial": ("partial", 4, None),
        "abandoned": ("abandoned", 0, None), "sum_mismatch": ("hole_by_hole", 18, None),
    }
    for case, (mode, holes, nine) in expect.items():
        r = p[CASES[case]]
        assert (r.entry_mode, r.holes_played, r.nine) == (mode, holes, nine), case
    assert "sum_mismatch" in p[CASES["sum_mismatch"]].flags
    assert p[CASES["sum_mismatch"]].hole_strokes == {}          # per-hole data untrusted
    raw = next(r for r in rounds_of(make_archive()) if r["id"] == CASES["sum_mismatch"])
    assert p[CASES["sum_mismatch"]].gross == raw["strokes"] == sum(raw["holeStrokes"]) + 3   # strokes trusted
    assert "partial" in p[CASES["partial"]].flags


def test_par_played_is_strokes_minus_score_never_abs():
    r = _parsed()[CASES["under_par"]]
    assert r.to_par == -3 and r.gross == 69 and r.par_played == 72     # abs(score) would give 66


def test_total_only_keeps_totals():
    r = _parsed()[CASES["total_only_18"]]
    assert (r.gross, r.to_par, r.par_played) == (101, 29, 72)
    assert r.hole_strokes == {}


def test_abandoned_has_no_score():
    r = _parsed()[CASES["abandoned"]]
    assert r.gross is None and r.to_par is None and r.par_played is None


def test_local_date_uses_player_timezone():
    p = _parsed()
    r = p[CASES["late_night"]]
    assert r.played_at_utc.endswith("T01:30:00+00:00")
    utc_day = r.played_at_utc[:10]
    assert r.played_on_local < utc_day                               # evening before, in New York
    assert _parsed(tz="UTC")[CASES["late_night"]].played_on_local == utc_day


def test_stroke_gain_sentinel_is_null():
    p = _parsed()
    assert p[CASES["hbh_18"]].sg_overall is None and p[CASES["hbh_18"]].sg_tee_to_green is None
    assert p[CASES["sg_real"]].sg_overall == -4.2 and p[CASES["sg_real"]].sg_tee_to_green == -1.5


def test_tracked_vs_zero():
    p = _parsed()
    untracked = p[CASES["total_only_18"]].stats
    assert all(untracked[c] is None for c in ("fw_hit", "fw_chances", "gir", "gir_chances", "putts"))
    assert untracked["putts_tracked"] == 0
    assert untracked["gir_no_chance"] is None
    zeros = p[CASES["sg_real"]].stats                                 # tracked, and genuinely zero
    assert (zeros["fw_hit"], zeros["fw_left"], zeros["fw_right"], zeros["fw_chances"]) == (0, 9, 5, 14)
    assert (zeros["gir"], zeros["gir_short"], zeros["gir_left"], zeros["gir_chances"]) == (0, 8, 8, 18)
    assert zeros["gir_no_chance"] == 2
    assert zeros["putts"] is None and zeros["putts_tracked"] == 0


def test_putt_hole_count_is_ignored():
    raw = next(r for r in rounds_of(make_archive()) if r["id"] == CASES["hbh_18"])
    assert raw["stats"]["puttHoleCount"] == 0 and raw["stats"]["putts"] > 0
    st = _parsed()[CASES["hbh_18"]].stats
    assert st["putts"] == raw["stats"]["putts"] and st["putts_tracked"] == 1


def test_scoring_distribution():
    p = _parsed()
    st = p[CASES["under_par"]].stats
    assert st["birdies"] == 3 and st["pars"] == 15 and st["eagles_plus"] == 0 and st["dbl_plus"] == 0
    assert p[CASES["total_only_18"]].stats["pars"] is None           # all-zero distribution = not tracked


def test_aces_counted_once():
    archive = make_archive()
    rec = next(r for r in rounds_of(archive) if r["id"] == CASES["hbh_18"])
    st = rec["stats"]
    st["aces"], st["pars"] = 1, st["pars"] - 1          # ace reported separately from the categories
    assert _parsed(archive)[CASES["hbh_18"]].stats["eagles_plus"] == st["eagles"] + st["doubleEagleOrBetter"] + 1
    st["eagles"] += 1                                   # ...or inside eagles as well: don't double count
    assert _parsed(archive)[CASES["hbh_18"]].stats["eagles_plus"] == st["eagles"] + st["doubleEagleOrBetter"]


def test_alternate_stat_spellings_accepted():
    archive = make_archive()
    st = next(r for r in rounds_of(archive) if r["id"] == CASES["sg_real"])["stats"]
    st["fairwayMiddle"] = st.pop("fairwayMiddles")
    st["girLeft"] = st.pop("girLefts")
    parsed = parse_archive(archive, "America/New_York")
    r = next(x for x in parsed.rounds if x.round_id == CASES["sg_real"])
    assert r.stats["fw_hit"] == 0 and r.stats["gir_left"] == 8
    assert not any("fairwayMiddle" in k or "girLeft" in k for k in parsed.unknown_keys)


def test_club_join_uses_club_id_key():
    parsed = parse_archive(make_archive(), "America/New_York")
    assert {c.club_id for c in parsed.clubs} == {PINES, DUNES}
    by_id = {r.round_id: r for r in parsed.rounds}
    assert by_id[CASES["dunes"]].club_id == DUNES
    assert "unknown_club" in by_id[CASES["orphan_club"]].flags


def test_unknown_keys_logged_by_name_only():
    parsed = parse_archive(make_archive(), "America/New_York")
    assert parsed.unknown_keys == sorted([f"myData.{UNKNOWN_SECTION}", f"rounds[].{UNKNOWN_ROUND_KEY}",
                                          f"rounds[].stats.{UNKNOWN_STATS_KEY}",
                                          f"rounds[].shotEntries[].{UNKNOWN_SHOT_KEY}"])
    assert not any("roundHandicap" in k or k == "rounds[].shotEntries" for k in parsed.unknown_keys)


def test_id_like_keys_are_masked():
    archive = make_archive()
    archive["myData"]["activityData"]["a1b2c3d4-e5f6-4711-8000-abcdefabcdef"] = {}
    archive["myData"]["activityData"]["someone@example.invalid"] = 1
    keys = parse_archive(archive, "UTC").unknown_keys
    assert "myData.activityData.<id>" in keys and not any("example.invalid" in k for k in keys)


def test_file_without_mydata_wrapper():
    archive = make_archive()
    assert len(parse_archive(archive["myData"], "UTC").rounds) == len(rounds_of(archive))


@pytest.mark.parametrize("key", ["holeStrokes", "strokes", "score", "clubId", "timestamp", "id"])
def test_missing_required_round_key_fails_loudly(key):
    archive = make_archive()
    del rounds_of(archive)[5][key]
    with pytest.raises(ExportFormatError) as err:
        parse_archive(archive, "UTC")
    assert f"rounds[5].{key}: missing" in str(err.value)


def test_missing_section_fails_loudly():
    archive = make_archive()
    del archive["myData"]["clubData"]
    with pytest.raises(ExportFormatError, match="clubData.playedClubs"):
        parse_archive(archive, "UTC")
    with pytest.raises(ExportFormatError, match="not an 18Birdies archive"):
        parse_archive({"hello": 1}, "UTC")


def test_error_messages_never_echo_values():
    archive = make_archive()
    rounds_of(archive)[0]["strokes"] = FAKE_PII["email"]
    with pytest.raises(ExportFormatError) as err:
        parse_archive(archive, "UTC")
    assert FAKE_PII["email"] not in str(err.value) and "rounds[0].strokes" in str(err.value)


# ---------------------------------------------------------------- importing
def test_import_summary(imported):
    conn, s, _ = imported
    n = len(rounds_of(make_archive()))
    assert s["inserted"] == n and s["updated"] == s["unchanged"] == s["deleted_in_source"] == 0
    assert s["abandoned"] == 1 and s["flagged"] == 4 and s["rounds_in_file"] == n
    assert s["putts_partial"] == 1 and s["shots_in_file"] == 8 and s["shot_rounds_written"] == 2
    assert s["parser_version"] == PARSER_VERSION and s["max_round_utc"] and not s["reapplied"]
    assert s["by_entry_mode"] == {"hole_by_hole": 21, "total_only": 3, "partial": 1, "abandoned": 1}
    assert s["unknown_keys"] and s["snapshot_date"] == "2026-01-01"
    row = conn.execute("SELECT * FROM imports").fetchone()
    assert row["kind"] == "18b_export" and loads(row["unknown_keys"]) == s["unknown_keys"]
    assert loads(row["summary"])["inserted"] == n


def test_imported_rows(imported):
    conn, _, _ = imported
    r = _round(conn, "under_par")
    assert (r["source"], r["gross"], r["to_par"], r["par_played"]) == ("18b_export", 69, -3, 72)
    assert r["first_import_id"] == r["last_import_id"] == 1 and r["deleted_in_source"] == 0
    assert loads(_round(conn, "sum_mismatch")["dq_flags"]) == ["sum_mismatch"]
    names = dict(conn.execute("SELECT club_id, name FROM clubs"))
    assert names[PINES] == "Synthetic Pines Golf Club" and names[ORPHAN] is None
    city = conn.execute("SELECT city, state FROM clubs WHERE club_id = ?", (PINES,)).fetchone()
    assert tuple(city) == ("Faketown", "ZZ")
    live = {r[0] for r in conn.execute("SELECT round_id FROM v_rounds")}
    assert CASES["abandoned"] not in live and CASES["total_only_18"] in live


def test_round_holes_written_for_usable_hole_data(imported):
    conn, _, _ = imported
    full = _holes(conn, "hbh_18")
    assert list(full) == list(range(1, 19))
    assert all(h["strokes_src"] == "18b_export" and h["par"] is None and h["si"] is None for h in full.values())
    assert list(_holes(conn, "front_nine")) == list(range(1, 10))
    assert list(_holes(conn, "back_nine")) == list(range(10, 19))
    assert list(_holes(conn, "nine_array")) == list(range(1, 10))           # positions until mapped
    assert list(_holes(conn, "partial")) == [1, 2, 3, 4]
    for case in ("sum_mismatch", "total_only_18", "abandoned"):
        assert _holes(conn, case) == {}, case


def test_raw_keeps_only_known_round_fields(imported):
    conn, _, _ = imported
    raw = loads(_round(conn, "unknown_keys")["raw"])
    assert set(raw) == {"id", "timestamp", "clubId", "score", "strokes", "holeStrokes", "stats"}
    assert UNKNOWN_STATS_KEY not in raw["stats"] and raw["stats"]["strokeGainOverall"] == 100
    raw = loads(_round(conn, "hbh_18")["raw"])                       # GPS lives only in the shots table
    assert raw["roundHandicap"] == HBH_18_HANDICAP and "shotEntries" not in raw


def test_pii_sections_never_reach_the_db(imported):
    conn, _, _ = imported
    dump = "\n".join(_dump(conn))
    for marker in FAKE_PII.values():
        assert marker not in dump, marker
    for section in ("accountData", "friendData", "feedData", "subscriptionData", "birthYear", "mobileNumber"):
        assert section not in dump, section


def test_reimport_same_file_changes_nothing(imported, cfg):
    conn, _, path = imported
    before = _dump(conn)
    again = import_export(conn, path, cfg)
    assert again["already_imported"] and again["import_id"] == 1
    assert again["inserted"] == again["updated"] == again["deleted_in_source"] == 0
    assert _dump(conn) == before


def test_reimport_reserialized_snapshot_is_all_unchanged(imported, cfg, tmp_path):
    conn, _, _ = imported
    path = write_archive(tmp_path / "18Birdies_archive_20260102.json", make_archive(), indent=2)
    s = import_export(conn, path, cfg)
    assert not s["already_imported"] and s["import_id"] == 2
    assert s["inserted"] == s["updated"] == s["deleted_in_source"] == 0
    assert s["unchanged"] == len(rounds_of(make_archive())) and s["holes_written"] == 0
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 2
    assert _round(conn, "hbh_18")["first_import_id"] == 1 and _round(conn, "hbh_18")["last_import_id"] == 2


def test_deleted_round_detected_and_restored(imported, cfg, tmp_path):
    conn, _, _ = imported
    gone = CASES["hbh_18"]
    s = import_export(conn, write_archive(tmp_path / "a_20260201.json", without_round(make_archive(), gone)), cfg)
    assert s["deleted_in_source"] == 1
    assert _round(conn, "hbh_18")["deleted_in_source"] == 1
    assert gone not in {r[0] for r in conn.execute("SELECT round_id FROM v_rounds")}
    s = import_export(conn, write_archive(tmp_path / "a_20260301.json", make_archive(), indent=1), cfg)
    assert s["restored"] == 1 and _round(conn, "hbh_18")["deleted_in_source"] == 0
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 3


def test_account_is_pinned_by_fingerprint_and_another_account_is_refused(imported, cfg, tmp_path):
    from golf.ingest.birdies_export import ExportAccountError, account_fingerprint, preview_export

    conn, summary, _ = imported
    fp = conn.execute("SELECT value FROM meta WHERE key = 'export_account_fp'").fetchone()[0]
    assert fp == summary["account_fp"] == account_fingerprint(FAKE_PII["user_id"]) and summary["account"] == "new"
    assert FAKE_PII["user_id"] not in json.dumps(_dump(conn))                  # the id itself is never stored
    other = make_archive()
    other["myData"]["accountData"]["userId"] = "someone-else-1"
    path = write_archive(tmp_path / "a_20260201.json", other)
    before = _dump(conn)
    assert preview_export(conn, path, cfg)["account"] == "different"
    with pytest.raises(ExportAccountError, match="--new-account"):
        import_export(conn, path, cfg)
    assert _dump(conn) == before                                               # nothing written
    s = import_export(conn, path, cfg, new_account=True)
    assert s["account"] == "different" and any("--new-account" in w for w in s["warnings"])
    assert conn.execute("SELECT value FROM meta WHERE key = 'export_account_fp'").fetchone()[0] == \
        account_fingerprint("someone-else-1")


def test_preview_counts_rounds_a_snapshot_would_mark_deleted(imported, cfg, tmp_path):
    from golf.ingest.birdies_export import preview_export

    conn, _, _ = imported
    path = write_archive(tmp_path / "a_20260201.json", without_round(make_archive(), CASES["hbh_18"]))
    pv = preview_export(conn, path, cfg)
    assert pv["account"] == "same" and pv["would_delete"] == 1 and not pv["stale"]
    old = write_archive(tmp_path / "18Birdies_archive_20251201.json", without_round(make_archive(), CASES["hbh_18"]))
    assert preview_export(conn, old, cfg)["would_delete"] == 0                   # stale: never applied anyway


def test_edited_round_updates_row_and_holes(imported, cfg, tmp_path):
    conn, _, _ = imported
    archive = make_archive()
    rec = next(r for r in rounds_of(archive) if r["id"] == CASES["hbh_18"])
    rec["holeStrokes"][0] += 2
    rec["strokes"] += 2
    rec["score"] += 2
    s = import_export(conn, write_archive(tmp_path / "a_20260201.json", archive), cfg)
    assert s["updated"] == 1 and s["unchanged"] == len(rounds_of(archive)) - 1 and s["holes_written"] == 1
    assert _holes(conn, "hbh_18")[1]["strokes"] == rec["holeStrokes"][0]
    assert _round(conn, "hbh_18")["gross"] == rec["strokes"]


def test_round_switching_to_total_only_drops_export_holes(imported, cfg, tmp_path):
    conn, _, _ = imported
    conn.execute("UPDATE round_holes SET putts = 2, stats_src = 'screenshot' WHERE round_id = ? AND hole = 1",
                 (CASES["hbh_18"],))
    archive = make_archive()
    rec = next(r for r in rounds_of(archive) if r["id"] == CASES["hbh_18"])
    rec["holeStrokes"] = [0] * 18
    import_export(conn, write_archive(tmp_path / "a_20260201.json", archive), cfg)
    holes = _holes(conn, "hbh_18")
    assert list(holes) == [1]                                         # screenshot stats survive
    assert holes[1]["strokes"] is None and holes[1]["putts"] == 2


def test_stale_snapshot_is_recorded_but_not_applied(imported, cfg, tmp_path):
    conn, _, _ = imported
    older = without_round(make_archive(), CASES["hbh_18"])
    s = import_export(conn, write_archive(tmp_path / "18Birdies_archive_20251201.json", older), cfg)
    assert s["stale_snapshot"] and s["deleted_in_source"] == 0 and s["warnings"]
    assert _round(conn, "hbh_18")["deleted_in_source"] == 0
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 2


def test_resolved_nine_survives_reimport(imported, cfg, tmp_path):
    conn, _, _ = imported
    rid = CASES["nine_array"]
    conn.execute("UPDATE rounds SET nine = 'back' WHERE round_id = ?", (rid,))    # as courses.sync does
    conn.execute("UPDATE round_holes SET hole = hole + 9 WHERE round_id = ?", (rid,))
    s = import_export(conn, write_archive(tmp_path / "a_20260201.json", make_archive(), indent=1), cfg)
    assert s["updated"] == 0 and s["holes_written"] == 0
    assert _round(conn, "nine_array")["nine"] == "back"
    assert list(_holes(conn, "nine_array")) == list(range(10, 19))


def test_bad_file_raises_and_writes_nothing(conn, cfg, tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    with pytest.raises(ExportFormatError, match="not valid JSON"):
        import_export(conn, bad, cfg)
    archive = make_archive()
    del rounds_of(archive)[0]["holeStrokes"]
    with pytest.raises(ExportFormatError):
        import_export(conn, write_archive(tmp_path / "x.json", archive), cfg)
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 0


def test_round_count_mismatch_warns(conn, cfg, tmp_path):
    archive = make_archive()
    archive["myData"]["activityData"]["roundCount"] += 1
    s = import_export(conn, write_archive(tmp_path / "a.json", archive), cfg)
    assert any("roundCount" in w for w in s["warnings"])


# ---------------------------------------------------------------- inspection
def test_inspect_export_shows_keys_and_types_but_no_values(tmp_path):
    archive = make_archive()
    text = inspect_export(write_archive(tmp_path / "a.json", archive))
    assert "email: str" in text and "holeStrokes: list[9..18] of int" in text
    assert "rounds: list[26] of object" in text and "Schema check OK" in text
    assert f"{UNKNOWN_SECTION}" in text.split("Unknown keys")[1]
    values = [*FAKE_PII.values(), PINES, CASES["hbh_18"], "Synthetic Pines", "Faketown"]
    for v in values:
        assert v not in text, v


def test_inspect_export_reports_schema_failure(tmp_path):
    archive = copy.deepcopy(make_archive())
    del rounds_of(archive)[0]["score"]
    text = inspect_export(write_archive(tmp_path / "a.json", archive))
    assert "Schema check FAILED" in text and "rounds[0].score: missing" in text


# ------------------------------------- keys found in the real export (Sep 2026)
@pytest.mark.parametrize("value, expected", [
    ("43.8", 43.8), (" 51.9 ", 51.9), ("+2.1", -2.1), ("-0.5", -0.5), (12, 12.0), (7.25, 7.25),
    ("", None), ("  ", None), (None, None), ("n/a", None), (True, None), ([], None), ("nan", None),
])
def test_round_handicap_is_tolerant(value, expected):
    assert handicap_number(value) == expected


def test_round_handicap_parsed_and_stored(imported):
    conn, _, _ = imported
    p = _parsed()
    assert p[CASES["hbh_18"]].round_handicap_18b == 18.4
    assert p[CASES["under_par"]].round_handicap_18b == -2.1                  # "+2.1" is a plus handicap
    for case in ("sg_real", "late_night", "total_only_18"):                   # "", null, key absent
        assert p[CASES[case]].round_handicap_18b is None, case
    raw = next(r for r in rounds_of(make_archive()) if r["id"] == "syn-001")
    assert _round_by_id(conn, "syn-001")["round_handicap_18b"] == float(raw["roundHandicap"])
    assert _round(conn, "hbh_18")["round_handicap_18b"] == 18.4


def _round_by_id(conn, rid):
    return conn.execute("SELECT * FROM rounds WHERE round_id = ?", (rid,)).fetchone()


def test_strokes_gained_and_no_chance_columns(imported):
    conn, _, _ = imported
    hbh, real = _round(conn, "hbh_18"), _round(conn, "sg_real")
    assert hbh["sg_overall"] is None and hbh["sg_tee_to_green"] is None        # the 100 sentinel
    assert (real["sg_overall"], real["sg_tee_to_green"]) == (-4.2, -1.5)
    assert real["gir_no_chance"] == 2 and _round(conn, "total_only_18")["gir_no_chance"] is None
    assert hbh["gir_no_chance"] in (0, None)


@pytest.mark.parametrize("putts, holes, tracked", [(18, 18, 1), (17, 18, 0), (9, 9, 1), (7, 9, 0), (0, 9, 0)])
def test_putts_need_one_per_hole_to_count_as_tracked(putts, holes, tracked):
    cols = stats_columns(RoundStats(putts=putts), holes)
    assert cols["putts_tracked"] == tracked
    assert cols["putts"] == (putts or None)                                   # kept, but not trusted


# Shapes seen in Shane's real export (synthetic numbers of the same form): 18Birdies derives GIR from each
# hole's putts, so girHoleCount below the holes played means some holes had no putts entered.
@pytest.mark.parametrize("putts, holes, gir_holes, tracked", [
    (10, 9, 0, 0),        # 10 putts on a 65 (every hole double bogey or worse): partial
    (14, 9, 0, 0),        # 14 putts over 9: partial
    (24, 18, 0, 0),       # 24 putts over 18 with 16 doubles: partial
    (20, 9, 0, 0),        # plausible total, but the export itself says some holes had no putts
    (22, 9, 9, 1),        # a normal nine with putts on every hole
    (21, 9, 9, 1),
    (36, 18, 18, 1),
    (22, 9, None, 1),     # no girHoleCount in the file: the one-putt-per-hole rule alone
    (8, 9, 9, 0),         # fewer putts than holes is partial whatever the GIR count says
])
def test_putts_count_only_when_18birdies_had_putts_on_every_hole(putts, holes, gir_holes, tracked):
    cols = stats_columns(RoundStats(putts=putts, girHoleCount=gir_holes, gir=0), holes)
    assert (cols["putts"], cols["putts_tracked"]) == (putts, tracked)


def test_partial_putts_flagged(imported):
    conn, _, _ = imported
    r = _round(conn, "putts_partial")
    assert (r["putts"], r["putts_tracked"]) == (11, 0)
    assert loads(r["dq_flags"]) == ["putts_partial"]
    assert _round(conn, "hbh_18")["putts_tracked"] == 1


@pytest.mark.parametrize("kind, number, loft, expected", [
    ("WOOD", "1", 0, "Driver"), ("WOOD", "3", 0, "3W"), ("WOOD", "5", 0, "5W"), ("HYBRID", "4", 0, "4H"),
    ("HYBRID", "5", 0, "5H"), ("IRON", "4", 0, "4i"), ("IRON", "9", 0, "9i"), ("IRON", "P", 0, "PW"),
    ("WEDGE", "P", 46, "PW"), ("WEDGE", "G", 50, "GW"), ("WEDGE", "A", 50, "GW"), ("WEDGE", "S", 56, "SW"),
    ("WEDGE", "L", 60, "LW"), ("WEDGE", "52", 0, "GW"), ("WEDGE", "", 58, "LW"), ("wedge", "s", 0, "SW"),
    ("PUTTER", "Putter", 0, "Putter"), ("DRIVER", "", 0, "Driver"), ("WOOD", "X", 0, None),
    ("ROCKET", "1", 0, None), (None, None, None, None),
])
def test_normalize_club(kind, number, loft, expected):
    assert normalize_club(kind, number, loft) == expected


def _shots(conn, case):
    return [dict(r) for r in conn.execute("SELECT * FROM shots WHERE round_id = ? ORDER BY seq", (CASES[case],))]


def test_shots_imported_in_time_order(imported):
    conn, _, _ = imported
    shots = _shots(conn, "hbh_18")
    assert [s["seq"] for s in shots] == list(range(1, 8))
    assert [s["club"] for s in shots] == ["Driver", "7i", "PW", "Putter", "Driver", "5H", "SW"]
    assert [s["hole"] for s in shots] == [1, 1, 1, 1, 2, 2, 2]
    first = shots[0]
    assert (first["club_type"], first["club_number"], first["loft"]) == ("WOOD", "1", None)   # loft 0 = unset
    assert shots[2]["loft"] == 46 and shots[2]["distance_yards"] == 88.0
    assert first["shot_at_utc"] == "2025-06-15T15:06:40+00:00"                # SHOT_BASE_MS
    assert shots[-1]["shot_at_utc"] is None                                  # no timestamp: sorts last
    assert first["start_lat"] == pytest.approx(40.001) and first["end_lon"] == -60.0
    assert [s["tee_name"] for s in shots] == ["White", "White", "White", None, "Blue", None, "White"]
    assert _round(conn, "hbh_18")["tee_name"] == "White"                     # most common teeName
    assert _round(conn, "total_only_18")["tee_name"] is None and _shots(conn, "total_only_18") == []
    assert SHOT_BASE_MS // 1000 == 1_750_000_000


def test_shots_are_replaced_per_round_and_idempotent(imported, cfg, tmp_path):
    conn, _, _ = imported
    s = import_export(conn, write_archive(tmp_path / "a_20260102.json", make_archive(), indent=2), cfg)
    assert s["shot_rounds_written"] == 0 and s["unchanged"] == s["rounds_in_file"]
    archive = make_archive()
    rec = next(r for r in rounds_of(archive) if r["id"] == CASES["hbh_18"])
    rec["shotEntries"] = rec["shotEntries"][:2]
    rec["shotEntries"][0]["teeName"] = "Blue"
    rec["shotEntries"][1]["teeName"] = "Blue"
    s = import_export(conn, write_archive(tmp_path / "a_20260103.json", archive), cfg)
    assert s["shot_rounds_written"] == 1 and s["updated"] == 1                 # tee_name changed with them
    assert [x["club"] for x in _shots(conn, "hbh_18")] == ["Driver", "7i"]
    assert _round(conn, "hbh_18")["tee_name"] == "Blue"
    del rec["shotEntries"]
    import_export(conn, write_archive(tmp_path / "a_20260104.json", archive), cfg)
    assert _shots(conn, "hbh_18") == [] and _round(conn, "hbh_18")["tee_name"] is None


def test_malformed_shot_entries_are_skipped_not_fatal(conn, cfg, tmp_path):
    archive = make_archive()
    rec = next(r for r in rounds_of(archive) if r["id"] == CASES["hbh_18"])
    rec["shotEntries"] += [{"timestamp": 1, "stickTypeAndNumber": {"type": "IRON"}},     # no holeNumber
                           {"holeNumber": "x"}, "not a shot", {"holeNumber": 0}]
    rounds_of(archive)[0]["shotEntries"] = {"not": "a list"}
    s = import_export(conn, write_archive(tmp_path / "a.json", archive), cfg)
    assert s["inserted"] == s["rounds_in_file"]
    assert any("4 malformed shot entries skipped" in w for w in s["warnings"])
    assert len(_shots(conn, "hbh_18")) == 7


def test_shots_never_leak_values_into_the_key_tree(tmp_path):
    text = inspect_export(write_archive(tmp_path / "a.json", make_archive()))
    assert "shotEntries: list[1..7] of object" in text and "GPS shots: 8 on 2 rounds" in text
    assert "Driver=3" in text and "40.001" not in text and "Blue" not in text


# ----------------------------------------------------------- snapshot dates
def test_future_date_in_file_name_is_ignored(conn, cfg, tmp_path):
    path = write_archive(tmp_path / "18Birdies_archive_20991201.json", make_archive())
    s = import_export(conn, path, cfg)
    assert s["snapshot_date"] < "2099-01-01" and any("after today" in w for w in s["warnings"])
    assert snapshot_date(path, ZoneInfo("UTC"), today=date(2100, 1, 1)) == "2099-12-01"
    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    assert s["snapshot_date"] == today.isoformat()                          # the file's mtime: just written
    later = write_archive(tmp_path / f"18Birdies_archive_{today:%Y%m%d}.json", make_archive(), indent=1)
    assert not import_export(conn, later, cfg)["stale_snapshot"]           # before the fix: stale until 2099


def test_poisoned_future_snapshot_from_before_the_fix_is_ignored(imported, cfg, tmp_path):
    conn, _, _ = imported
    row = conn.execute("SELECT import_id, summary FROM imports").fetchone()
    summary = loads(row["summary"])
    summary["snapshot_date"] = "2099-12-01"
    conn.execute("UPDATE imports SET summary = ? WHERE import_id = ?", (json.dumps(summary), row["import_id"]))
    s = import_export(conn, write_archive(tmp_path / "18Birdies_archive_20251215.json",
                                          without_round(make_archive(), CASES["hbh_18"])), cfg)
    assert not s["stale_snapshot"] and s["deleted_in_source"] == 1


def test_older_name_but_newer_rounds_is_applied(imported, cfg, tmp_path):
    conn, _, _ = imported
    archive = make_archive()
    new = copy.deepcopy(rounds_of(archive)[-1])
    new["id"], new["timestamp"] = "syn-newest", new["timestamp"] + 9 * 86_400_000
    rounds_of(archive).append(new)
    archive["myData"]["activityData"]["roundCount"] += 1
    s = import_export(conn, write_archive(tmp_path / "18Birdies_archive_20251201.json", archive), cfg)
    assert not s["stale_snapshot"] and s["inserted"] == 1


# ------------------------------------------------------ re-applying old imports
def test_file_imported_by_an_older_parser_is_reapplied(imported, cfg):
    conn, first, path = imported
    row = conn.execute("SELECT import_id, summary FROM imports").fetchone()
    old = loads(row["summary"])
    del old["parser_version"]                                               # as written by parser v1
    conn.execute("UPDATE imports SET summary = ? WHERE import_id = ?", (json.dumps(old), row["import_id"]))
    conn.execute("DELETE FROM shots")
    conn.execute("UPDATE rounds SET round_handicap_18b = NULL, tee_name = NULL, sg_overall = NULL, "
                 "gir_no_chance = NULL")
    s = import_export(conn, path, cfg)
    assert s["reapplied"] and not s["already_imported"] and s["import_id"] == row["import_id"]
    assert s["snapshot_date"] == first["snapshot_date"] and s["shot_rounds_written"] == 2
    assert _round(conn, "hbh_18")["round_handicap_18b"] == 18.4 and _round(conn, "sg_real")["sg_overall"] == -4.2
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1
    assert import_export(conn, path, cfg)["already_imported"]
