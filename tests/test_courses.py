"""courses.yaml loading/sync, round mapping, check_courses, and OpenGolfAPI autofill (mocked HTTP)."""
from __future__ import annotations

import copy
import shutil
from pathlib import Path

import httpx
import pytest
import yaml

from fixtures.synthetic_archive import (
    CASES, DUNES, DUNES_PARS, ORPHAN, PINES, make_archive, rounds_of, write_archive,
)
from golf.courses import (
    AutofillError, CoursesFileError, apply_nine_overrides, autofill_club, check_courses, load_courses, sync_courses,
)
from golf.ingest.birdies_export import import_export

EXAMPLE_YAML = Path(__file__).resolve().parent.parent / "courses.example.yaml"
WHITE = "synthetic-pines:White:M"


@pytest.fixture
def db(conn, cfg, tmp_path):
    import_export(conn, write_archive(tmp_path / "a_20260101.json", make_archive()), cfg)
    return conn


def _round(conn, case):
    return conn.execute("SELECT * FROM rounds WHERE round_id = ?", (CASES[case],)).fetchone()


def _holes(conn, case):
    return {r["hole"]: (r["strokes"], r["par"], r["si"]) for r in conn.execute(
        "SELECT * FROM round_holes WHERE round_id = ? ORDER BY hole", (CASES[case],))}


def _codes(issues):
    return [(i["code"], i["round_id"] or i["tee_id"] or i["course_key"] or i["club_id"]) for i in issues]


def _example() -> dict:
    return yaml.safe_load(EXAMPLE_YAML.read_text())


def _write(tmp_path, data: dict, name="courses.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def _dunes_course(name: str, *, default: bool = True) -> dict:
    holes = [{"hole": i + 1, "par": p, "si": i + 1} for i, p in enumerate(DUNES_PARS)]
    return {"name": name, "tees": [
        {"name": "Blue", "cr18": 69.8, "slope18": 121, "is_default": default, "holes": holes},
        {"name": "White", "cr18": 68.0, "slope18": 117, "holes": holes}]}


# -------------------------------------------------------------------- loading
def test_example_yaml_is_valid():
    spec = load_courses(EXAMPLE_YAML)
    club = spec.clubs[PINES]
    course = club.courses["synthetic-pines"]
    white = course.default_tee()
    assert white.name == "White" and white.total_par == 72 and white.total_yards == 6612
    assert white.checked_on == "2026-09-01" and course.tee_named("red").gender == "F"
    assert club.rounds[CASES["nine_array"]].nine == "back"


def test_missing_yaml_is_empty(tmp_path):
    assert load_courses(tmp_path / "nope.yaml").clubs == {}


def test_unquoted_date_becomes_text(tmp_path):
    data = _example()
    data["clubs"][PINES]["courses"]["synthetic-pines"]["tees"][0]["checked_on"] = "2026-09-02"
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(data).replace("'2026-09-02'", "2026-09-02"))
    assert load_courses(path).clubs[PINES].courses["synthetic-pines"].tees[0].checked_on == "2026-09-02"


def _mutate(fn):
    data = _example()
    fn(data["clubs"][PINES])
    return data


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c["courses"]["synthetic-pines"]["tees"][0]["holes"][1].update(si=7), "duplicate stroke"),
    (lambda c: c["courses"]["synthetic-pines"]["tees"][0].update(par=71), "holes add up to 72"),
    (lambda c: c["courses"]["synthetic-pines"]["tees"][1].update(is_default=True), "more than one tee"),
    (lambda c: c["courses"]["synthetic-pines"]["tees"][0].update(slope=128), "slope"),
    (lambda c: c.update(rounds={"x": {"course": "nowhere"}}), "not one of this club's courses"),
    (lambda c: c.update(rounds={"x": {"tee": "Purple"}}), "tee 'Purple' not found"),
    (lambda c: c.update(default_course="nowhere"), "default_course"),
])
def test_invalid_yaml_is_rejected(tmp_path, mutate, message):
    with pytest.raises(CoursesFileError, match=message):
        load_courses(_write(tmp_path, _mutate(mutate)))


def test_course_keys_unique_across_clubs(tmp_path):
    data = _example()
    data["clubs"][DUNES] = {"courses": {"synthetic-pines": _dunes_course("Dup")}}
    with pytest.raises(CoursesFileError, match="used by clubs"):
        load_courses(_write(tmp_path, data))


# ----------------------------------------------------------------------- sync
def test_sync_maps_single_course_club_and_fills_par_si(db):
    s = sync_courses(db, EXAMPLE_YAML)
    assert (s["clubs"], s["courses"], s["tees"], s["tee_holes"]) == (1, 1, 2, 18)
    assert s["rounds_mapped"] == 23 and s["nine_resolved"] == 1 and s["unknown_round_ids"] == []
    # QA: the summary used to say "unmapped 0" while the Dunes and orphan-club rounds were unmapped
    assert s["rounds_unmapped"] == 2 and s["clubs_unmapped"] == 2
    r = _round(db, "hbh_18")
    assert (r["course_key"], r["tee_id"]) == ("synthetic-pines", WHITE)
    holes = _holes(db, "hbh_18")
    assert holes[4][1:] == (5, 1) and holes[16][1:] == (3, 18)
    assert _holes(db, "back_nine")[12][1:] == (5, 2)
    tee = db.execute("SELECT * FROM tees WHERE tee_id = ?", (WHITE,)).fetchone()
    assert (tee["par"], tee["yards"], tee["cr18"], tee["is_default"]) == (72, 6612, 71.4, 1)
    assert tee["checked_on"] == "2026-09-01"
    assert _round(db, "dunes")["course_key"] is None and _round(db, "orphan_club")["course_key"] is None


def test_sync_resolves_nine_array_and_renumbers_holes(db):
    before = [v[0] for v in _holes(db, "nine_array").values()]
    sync_courses(db, EXAMPLE_YAML)
    assert _round(db, "nine_array")["nine"] == "back"
    after = _holes(db, "nine_array")
    assert list(after) == list(range(10, 19))
    assert [v[0] for v in after.values()] == before
    assert after[10][1:] == (4, 8) and after[18][1:] == (4, 14)


def test_sync_can_undo_a_nine_mapping(db, tmp_path):
    sync_courses(db, EXAMPLE_YAML)
    data = _example()
    data["clubs"][PINES]["rounds"] = {}
    # Without the export's birdie/par/bogey counts both Pines nines fit (par 36 each): unknown again.
    db.execute("UPDATE rounds SET pars = NULL WHERE round_id = ?", (CASES["nine_array"],))
    s = sync_courses(db, _write(tmp_path, data))
    assert s["nine_resolved"] == 1 and s["nine_inferred"] == 0 and _round(db, "nine_array")["nine"] == "unknown"
    assert list(_holes(db, "nine_array")) == list(range(1, 10))


def test_sync_infers_the_nine_from_the_par_layout(db, tmp_path):
    """The synthetic nine is really the back nine: with no mapping, only the back nine's par layout
    reproduces the export's own birdie/par/bogey/double counts, so sync places it there."""
    data = _example()
    data["clubs"][PINES]["rounds"] = {}
    s = sync_courses(db, _write(tmp_path, data))
    assert s["nine_inferred"] == 1 and _round(db, "nine_array")["nine"] == "back"
    assert list(_holes(db, "nine_array")) == list(range(10, 19))
    assert not [i for i in check_courses(db) if i["round_id"] == CASES["nine_array"]]
    # A round override still wins over the inference.
    db.execute("INSERT INTO round_overrides(round_id, nine) VALUES (?, 'front')", (CASES["nine_array"],))
    s = sync_courses(db, _write(tmp_path, data))
    assert s["nine_inferred"] == 0 and _round(db, "nine_array")["nine"] == "front"


def test_infer_nine_needs_exactly_one_fitting_nine():
    from golf.courses import infer_nine, layout_fits, score_type_counts

    pars = {h: (p, h) for h, p in enumerate([5, 4, 3, 4, 3, 4, 5, 3, 5, 4, 3, 5, 4, 3, 4, 5, 4, 4], 1)}
    card = [7, 7, 5, 6, 6, 6, 8, 5, 7]                       # front: all doubles or worse; back: a par on 12
    base = {"raw": '{"holeStrokes": [7, 7, 5, 6, 6, 6, 8, 5, 7]}', "source": "18b_export",
            "entry_mode": "hole_by_hole", "holes_played": 9, "par_played": 36}
    counts = dict(zip(("eagles_plus", "birdies", "pars", "bogeys", "dbl_plus"), (0, 0, 0, 0, 9)))
    assert score_type_counts(card, [5, 4, 3, 4, 3, 4, 5, 3, 5]) == (0, 0, 0, 0, 9)
    assert infer_nine({**base, **counts}, pars) == "front"
    assert infer_nine({**base, **dict.fromkeys(counts)}, pars) is None      # both nines are par 36
    assert infer_nine({**base, **counts, "par_played": 35}, pars) is None   # neither passes the checksum
    assert infer_nine({**base, **counts, "entry_mode": "total_only"}, pars) is None
    assert layout_fits(card, [4] * 9, 36, None) and not layout_fits(card, [4] * 9, 36, (0, 0, 0, 1, 8))


def test_sync_is_idempotent(db):
    sync_courses(db, EXAMPLE_YAML)
    dump = list(db.iterdump())
    again = sync_courses(db, EXAMPLE_YAML)
    assert again["holes_filled"] == 0 and again["nine_resolved"] == 0
    assert list(db.iterdump()) == dump


def test_sync_removes_tees_dropped_from_yaml(db, tmp_path):
    sync_courses(db, EXAMPLE_YAML)
    data = _example()
    data["clubs"][PINES]["courses"]["synthetic-pines"]["tees"].pop()          # drop Red
    sync_courses(db, _write(tmp_path, data))
    assert [r[0] for r in db.execute("SELECT tee_id FROM tees")] == [WHITE]


def test_reimport_after_sync_keeps_mapping_and_par(db, cfg, tmp_path):
    sync_courses(db, EXAMPLE_YAML)
    s = import_export(db, write_archive(tmp_path / "a_20260201.json", make_archive(), indent=1), cfg)
    assert s["updated"] == 0 and s["holes_written"] == 0
    assert _round(db, "hbh_18")["tee_id"] == WHITE and _holes(db, "hbh_18")[4][1:] == (5, 1)
    assert _round(db, "nine_array")["nine"] == "back"


def test_multi_course_club_needs_explicit_mapping(db, tmp_path):
    data = _example()
    data["clubs"][DUNES] = {"name": "Synthetic Dunes", "courses": {
        "dunes-links": _dunes_course("Dunes Links"), "dunes-meadow": _dunes_course("Dunes Meadow")}}
    sync_courses(db, _write(tmp_path, data))
    assert _round(db, "dunes")["course_key"] is None
    assert ("unmapped_rounds", DUNES) in _codes(check_courses(db))

    data["clubs"][DUNES]["default_course"] = "dunes-links"
    sync_courses(db, _write(tmp_path, data))
    assert (_round(db, "dunes")["course_key"], _round(db, "dunes")["tee_id"]) == ("dunes-links", "dunes-links:Blue:M")

    data["clubs"][DUNES]["rounds"] = {CASES["dunes"]: {"course": "dunes-meadow", "tee": "White"}}
    sync_courses(db, _write(tmp_path, data))
    dunes = _round(db, "dunes")
    assert (dunes["course_key"], dunes["tee_id"]) == ("dunes-meadow", "dunes-meadow:White:M")
    assert _holes(db, "dunes")[5][1:] == (5, 5)
    assert not [i for i in check_courses(db) if i["club_id"] == DUNES and i["severity"] == "error"]


def test_round_override_tee_drives_par_fill(db, tmp_path):
    data = _example()
    data["clubs"][DUNES] = {"courses": {"dunes-links": _dunes_course("Dunes Links")}}
    sync_courses(db, _write(tmp_path, data))
    db.execute("INSERT INTO round_overrides(round_id, course_key, tee_id) VALUES (?, 'dunes-links', "
               "'dunes-links:White:M')", (CASES["unknown_keys"],))
    sync_courses(db, _write(tmp_path, data))
    assert _holes(db, "unknown_keys")[1][1:] == (DUNES_PARS[0], 1)       # the override's tee, not Pines'
    assert ("par_mismatch", CASES["unknown_keys"]) in _codes(check_courses(db))


# --------------------------------------------------------------------- checks
def test_check_courses_after_example_sync(db):
    sync_courses(db, EXAMPLE_YAML)
    issues = check_courses(db)
    codes = _codes(issues)
    assert ("unmapped_club", DUNES) in codes and ("unmapped_club", ORPHAN) in codes
    orphan = next(i for i in issues if i["club_id"] == ORPHAN)
    assert orphan["count"] == 1 and ORPHAN in orphan["message"]
    assert next(i for i in issues if i["club_id"] == DUNES)["message"].startswith("Synthetic Dunes Golf Resort")
    assert ("nine_unknown", CASES["total_only_9"]) in codes
    assert not [c for c in codes if c[0] in ("par_mismatch", "unchecked_rating", "tee_missing_rating")]


def test_check_courses_before_any_yaml(db):
    issues = check_courses(db)
    assert {i["code"] for i in issues} == {"unmapped_club"}
    assert next(i for i in issues if i["club_id"] == PINES)["count"] == 23      # non-abandoned Pines rounds


def test_check_courses_par_checksum(conn, cfg, tmp_path):
    archive = make_archive()
    for rec in rounds_of(archive):
        if rec["id"] in (CASES["hbh_18"], CASES["total_only_18"], CASES["back_nine"], CASES["partial"]):
            rec["score"] += 1                                  # implies par one lower than the card
    import_export(conn, write_archive(tmp_path / "a.json", archive), cfg)
    sync_courses(conn, EXAMPLE_YAML)
    errors = {i["round_id"] for i in check_courses(conn) if i["code"] == "par_mismatch"}
    assert errors == {CASES["hbh_18"], CASES["total_only_18"], CASES["back_nine"], CASES["partial"]}
    assert check_courses(conn)[0]["severity"] == "error"                       # most severe first


def test_check_courses_nine_rating_needed_for_nines(db, tmp_path):
    """Shane plays nines on 18-hole courses too: a nine without its own rating is called out."""
    sync_courses(db, EXAMPLE_YAML)
    assert not [i for i in check_courses(db) if i["code"] == "tee_missing_nine_rating"]
    data = _example()
    white = data["clubs"][PINES]["courses"]["synthetic-pines"]["tees"][0]
    white.pop("cr_b9")
    sync_courses(db, _write(tmp_path, data))
    issues = [i for i in check_courses(db) if i["code"] == "tee_missing_nine_rating"]
    assert [i["tee_id"] for i in issues] == [WHITE] and "back nine" in issues[0]["message"]
    assert issues[0]["severity"] == "warn" and issues[0]["count"] >= 1


def test_check_courses_par_pattern(db, tmp_path):
    """Swapping two holes' pars keeps every checksum but not the export's own birdie/par/bogey split."""
    sync_courses(db, EXAMPLE_YAML)
    assert not [i for i in check_courses(db) if i["code"] == "par_pattern_mismatch"]
    data = _example()
    holes = data["clubs"][PINES]["courses"]["synthetic-pines"]["tees"][0]["holes"]
    holes[2]["par"], holes[3]["par"] = holes[3]["par"], holes[2]["par"]       # par 3 and par 5 swapped
    sync_courses(db, _write(tmp_path, data))
    issues = check_courses(db)
    pattern = [i for i in issues if i["code"] == "par_pattern_mismatch"]
    assert pattern and all(i["severity"] == "warn" and "18Birdies counted" in i["message"] for i in pattern)
    assert CASES["hbh_18"] in {i["round_id"] for i in pattern}
    assert not [i for i in issues if i["code"] == "par_mismatch"]


def test_check_courses_rating_problems(db, tmp_path):
    data = _example()
    white, red = data["clubs"][PINES]["courses"]["synthetic-pines"]["tees"]
    white["checked_on"] = ""
    white["rating_source"] = "opengolfapi"
    sync_courses(db, _write(tmp_path, data))
    unchecked = [i for i in check_courses(db) if i["code"] == "unchecked_rating"]
    assert [i["tee_id"] for i in unchecked] == [WHITE] and "opengolfapi" in unchecked[0]["message"]

    for k in ("cr18", "slope18"):
        white.pop(k)
    white["holes"] = white["holes"][:9]
    sync_courses(db, _write(tmp_path, data))
    codes = {i["code"] for i in check_courses(db) if i["tee_id"] == WHITE}
    assert codes == {"tee_missing_rating", "tee_missing_holes"}

    white["is_default"] = False
    sync_courses(db, _write(tmp_path, data))
    assert ("no_default_tee", "synthetic-pines") in _codes(check_courses(db))


# ------------------------------------------------------------------- autofill
SIS = [3, 15, 7, 11, 1, 17, 9, 5, 13, 4, 10, 18, 8, 2, 16, 6, 12, 14]


def _payloads_a() -> dict[str, object]:
    """One plausible OpenGolfAPI shape: wrapped lists, per-hole yardages keyed by tee."""
    return {
        "/api/v1/courses/search": {"courses": [
            {"id": 101, "course_name": "Dunes Links", "club_name": "Synthetic Dunes Golf Resort",
             "city": "Nowhere", "state": "ZZ"},
            {"id": 102, "course_name": "Dunes Meadow", "club_name": "Synthetic Dunes Golf Resort",
             "city": "Nowhere", "state": "ZZ"},
            {"id": 999, "course_name": "Elsewhere", "club_name": "Unrelated Country Club"}]},
        "/api/v1/courses/101/tees": {"tees": [
            {"tee_name": "Blue", "gender": "male", "course_rating": 70.1, "slope_rating": 124, "par": 70,
             "total_yards": 6100, "front_course_rating": 35.2, "front_slope_rating": 122},
            {"tee_name": "Red", "gender": "female", "course_rating": 71.5, "slope_rating": 126, "par": 70}]},
        "/api/v1/courses/101/holes": {"holes": [
            {"hole_number": i + 1, "par": p, "handicap_index": SIS[i], "yardages": {"Blue": 330 + i, "Red": 280 + i}}
            for i, p in enumerate(DUNES_PARS)]},
        "/api/v1/courses/102/tees": {"tees": [{"tee_name": "Gold", "gender": "M", "rating": 66.0, "slope": 110}]},
        "/api/v1/courses/102/holes": {"holes": [{"hole_number": i + 1, "par": 3, "handicap_index": i + 1}
                                                for i in range(9)]},
    }


def _payloads_b() -> dict[str, object]:
    """Another plausible shape: bare lists, tees grouped by gender, one hole row per tee."""
    return {
        "/api/v1/courses/search": [{"id": "abc", "name": "Synthetic Dunes Golf Resort"}],
        "/api/v1/courses/abc/tees": {"data": {"male": [{"name": "White", "rating": "69.0", "slope": "120"}],
                                              "female": [{"name": "White", "rating": 72.0, "slope": 125}]}},
        "/api/v1/courses/abc/holes": [
            {"hole": i + 1, "par": p, "handicap": SIS[i], "tee": tee, "yardage": 300 + i}
            for i, p in enumerate(DUNES_PARS) for tee in ("White", "Red")],
    }


class FakeOpenGolfAPI:
    def __init__(self, payloads: dict[str, object]):
        self.payloads = payloads
        self.requests: list[httpx.Request] = []
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "api.opengolfapi.org"
        if request.url.path not in self.payloads:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=self.payloads[request.url.path])


@pytest.fixture
def courses_yaml(tmp_path) -> Path:
    path = tmp_path / "courses.yaml"
    shutil.copy(EXAMPLE_YAML, path)
    return path


def test_autofill_writes_a_proposal_and_keeps_the_rest(db, courses_yaml):
    original = courses_yaml.read_text()
    api = FakeOpenGolfAPI(_payloads_a())
    result = autofill_club(db, DUNES, courses_yaml, http=api.client)
    assert result["opengolfapi_id"] == "101" and result["course_key"] == "dunes-links"
    assert result["tees"] == 2 and result["holes"] == 18 and result["candidates"][1]["id"] == "102"
    assert [c["id"] for c in result["also_matched"]] == ["102"]         # same facility name: the CLI warns
    assert api.requests[0].url.params["q"] == "Synthetic Dunes Golf Resort"
    assert [r.url.path for r in api.requests[1:]] == ["/api/v1/courses/101/tees", "/api/v1/courses/101/holes"]

    text = courses_yaml.read_text()
    assert text.startswith(original.rstrip("\n"))                    # every original line and comment kept
    assert "Open Database License (ODbL) 1.0" in text and "opengolfapi.org" in text and "PROPOSED" in text

    course = load_courses(courses_yaml).clubs[DUNES].courses["dunes-links"]
    blue, red = course.tees
    assert (course.source, course.source_ref) == ("opengolfapi", "101")
    assert (blue.name, blue.gender, blue.cr18, blue.slope18, blue.par) == ("Blue", "M", 70.1, 124, 70)
    assert (blue.cr_f9, blue.slope_f9, blue.cr_b9) == (35.2, 122, None)
    assert (blue.rating_source, blue.checked_on, blue.is_default) == ("opengolfapi", "", False)
    assert [h.par for h in blue.holes] == DUNES_PARS and blue.holes[0].si == 3 and blue.holes[17].yards == 347
    assert red.gender == "F" and red.holes[0].yards == 280
    assert load_courses(courses_yaml).clubs[PINES] == load_courses(EXAMPLE_YAML).clubs[PINES]


def test_autofill_proposal_is_nagged_until_confirmed(db, courses_yaml):
    autofill_club(db, DUNES, courses_yaml, http=FakeOpenGolfAPI(_payloads_a()).client)
    sync_courses(db, courses_yaml)
    assert ("no_default_tee", "dunes-links") in _codes(check_courses(db))
    courses_yaml.write_text(courses_yaml.read_text().replace(
        "is_default: false", "is_default: true", 1))                   # Shane picks Blue
    sync_courses(db, courses_yaml)
    unchecked = [i for i in check_courses(db) if i["code"] == "unchecked_rating"]
    assert [i["tee_id"] for i in unchecked] == ["dunes-links:Blue:M"]
    assert _round(db, "dunes")["tee_id"] == "dunes-links:Blue:M"
    assert not [i for i in check_courses(db) if i["code"] == "par_mismatch"]


def test_autofill_second_course_of_a_facility(db, courses_yaml):
    api = FakeOpenGolfAPI(_payloads_a())
    autofill_club(db, DUNES, courses_yaml, http=api.client)
    result = autofill_club(db, DUNES, courses_yaml, http=api.client, course_id="102")
    assert result["course_key"] == "dunes-meadow" and result["holes"] == 9 and result["also_matched"] == []
    club = load_courses(courses_yaml).clubs[DUNES]
    assert list(club.courses) == ["dunes-links", "dunes-meadow"]
    assert club.courses["dunes-meadow"].holes == 9
    gold = club.courses["dunes-meadow"].tees[0]                          # a 9-hole course is scored on cr_f9
    assert (gold.cr18, gold.slope18, gold.cr_f9, gold.slope_f9) == (66.0, 110, 33.0, 110)
    assert courses_yaml.read_text().count("Open Database License") == 2


def test_autofill_alternative_payload_shapes(db, courses_yaml):
    result = autofill_club(db, DUNES, courses_yaml, http=FakeOpenGolfAPI(_payloads_b()).client)
    course = load_courses(courses_yaml).clubs[DUNES].courses[result["course_key"]]
    men, women = course.tees
    assert (men.name, men.gender, men.cr18, men.slope18) == ("White", "M", 69.0, 120)
    assert (women.gender, women.cr18) == ("F", 72.0)
    assert [h.si for h in men.holes] == SIS and all(h.yards == 300 + h.hole - 1 for h in men.holes)


def test_autofill_refuses_to_replace_checked_ratings(db, courses_yaml):
    api = FakeOpenGolfAPI(_payloads_a())
    autofill_club(db, DUNES, courses_yaml, http=api.client)
    courses_yaml.write_text(courses_yaml.read_text().replace(
        "checked_on: ''", "checked_on: '2026-09-20'", 3))              # Pines Red + Dunes Blue/Red
    with pytest.raises(AutofillError, match="already has checked ratings"):
        autofill_club(db, DUNES, courses_yaml, http=api.client)
    autofill_club(db, DUNES, courses_yaml, http=api.client, force=True)
    blue = load_courses(courses_yaml).clubs[DUNES].courses["dunes-links"].tees[0]
    assert blue.checked_on == ""


def test_autofill_without_a_confident_match(db, courses_yaml):
    payloads = _payloads_a()
    payloads["/api/v1/courses/search"] = {
        "courses": [{"id": 999, "course_name": "Elsewhere", "club_name": "Unrelated Country Club"}]}
    before = courses_yaml.read_text()
    with pytest.raises(AutofillError, match="no confident OpenGolfAPI match.*999"):
        autofill_club(db, DUNES, courses_yaml, http=FakeOpenGolfAPI(payloads).client)
    assert courses_yaml.read_text() == before


def test_autofill_tries_simpler_queries(db, courses_yaml):
    """OpenGolfAPI's search is literal ('Barefoot Resort & Golf' and 'Butter Brook' find nothing)."""
    from golf.courses import search_queries

    assert search_queries("Barefoot Resort & Golf") == ["Barefoot Resort & Golf", "Barefoot Resort Golf", "Barefoot"]
    assert search_queries("Butter Brook") == ["Butter Brook", "ButterBrook", "Butter"]
    payloads = _payloads_a()
    api = FakeOpenGolfAPI(payloads)
    real = api._handle

    def picky(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search") and request.url.params["q"] != "Synthetic Dunes":
            api.requests.append(request)
            return httpx.Response(200, json={"courses": []})
        return real(request)

    api.client = httpx.Client(transport=httpx.MockTransport(picky))
    result = autofill_club(db, DUNES, courses_yaml, http=api.client)
    assert result["opengolfapi_id"] == "101"
    assert [r.url.params["q"] for r in api.requests if r.url.path.endswith("/search")] == \
        ["Synthetic Dunes Golf Resort", "Synthetic Dunes"]


def test_rank_candidates_ignores_spacing():
    from golf.courses import rank_candidates

    ranked = rank_candidates([{"id": "1", "name": "Butterbrook Golf Club", "club_name": "", "city": None,
                               "state": None}], "Butter Brook")
    assert ranked[0]["score"] >= 70


def test_autofill_unknown_club(db, courses_yaml):
    with pytest.raises(AutofillError, match="import the 18Birdies export"):
        autofill_club(db, ORPHAN, courses_yaml, http=FakeOpenGolfAPI(_payloads_a()).client)


def test_autofill_creates_a_new_yaml(db, tmp_path):
    path = tmp_path / "fresh.yaml"
    autofill_club(db, DUNES, path, http=FakeOpenGolfAPI(_payloads_a()).client)
    spec = load_courses(path)
    assert list(spec.clubs) == [DUNES] and spec.clubs[DUNES].name == "Synthetic Dunes Golf Resort"


def test_autofill_replaces_only_its_club_block(db, courses_yaml):
    api = FakeOpenGolfAPI(_payloads_a())
    autofill_club(db, DUNES, courses_yaml, http=api.client)
    payloads = copy.deepcopy(_payloads_a())
    payloads["/api/v1/courses/101/tees"]["tees"][0]["course_rating"] = 70.4
    autofill_club(db, DUNES, courses_yaml, http=FakeOpenGolfAPI(payloads).client)
    spec = load_courses(courses_yaml)
    assert spec.clubs[DUNES].courses["dunes-links"].tees[0].cr18 == 70.4
    assert list(spec.clubs[DUNES].courses) == ["dunes-links"]
    assert spec.clubs[PINES] == load_courses(EXAMPLE_YAML).clubs[PINES]
    heads = [line for line in courses_yaml.read_text().splitlines() if line.strip().startswith(f"{DUNES}:")]
    assert len(heads) == 1


# ------------------------------------------------ tee from GPS, 9-hole courses, nine overrides
def test_gps_tee_name_picks_the_tee_when_the_course_has_it(db, tmp_path):
    sync_courses(db, EXAMPLE_YAML)
    assert _round(db, "hbh_18")["tee_name"] == "White" and _round(db, "hbh_18")["tee_id"] == WHITE
    db.execute("UPDATE rounds SET tee_name = 'Red' WHERE round_id = ?", (CASES["hbh_18"],))
    db.execute("UPDATE rounds SET tee_name = 'Gold' WHERE round_id = ?", (CASES["under_par"],))
    sync_courses(db, EXAMPLE_YAML)
    assert _round(db, "hbh_18")["tee_id"] == "synthetic-pines:Red:F"
    assert _round(db, "under_par")["tee_id"] == WHITE                          # no Gold tee: the default
    data = _example()
    data["clubs"][PINES]["rounds"][CASES["hbh_18"]] = {"tee": "White"}          # courses.yaml wins
    sync_courses(db, _write(tmp_path, data))
    assert _round(db, "hbh_18")["tee_id"] == WHITE


def _nine_hole_club(data: dict) -> dict:
    """Make Synthetic Pines a 9-hole course (holes 1-9 of the example) rated over nine holes, and over
    18 for twice around (so the archive's 18-hole rounds still establish an index)."""
    tee = data["clubs"][PINES]["courses"]["synthetic-pines"]["tees"][0]
    tee["holes"] = tee["holes"][:9]
    for k in ("cr_b9", "slope_b9"):
        tee.pop(k)
    data["clubs"][PINES]["courses"]["synthetic-pines"]["holes"] = 9
    data["clubs"][PINES]["courses"]["synthetic-pines"]["tees"] = [tee]
    data["clubs"][PINES]["rounds"] = {}
    return data


def test_nine_hole_course_resolves_nine_arrays_to_its_nine(db, cfg, tmp_path):
    from golf import whs

    s = sync_courses(db, _write(tmp_path, _nine_hole_club(_example())))
    r = _round(db, "nine_array")
    assert r["nine"] == "front" and _round(db, "total_only_9")["nine"] == "front" and s["nine_resolved"] == 2
    assert list(_holes(db, "nine_array")) == list(range(1, 10)) and _holes(db, "nine_array")[1][1:] == (4, 7)
    assert not [i for i in check_courses(db) if i["code"] == "nine_unknown" and i["round_id"] == r["round_id"]]
    w = whs.recompute(db, cfg)
    assert CASES["nine_array"] not in w["skipped_rounds"].get("nine_unknown", [])
    assert db.execute("SELECT differential_kind FROM handicap_history WHERE round_id = ?",
                      (CASES["nine_array"],)).fetchone()[0] == "nine_hole_scaled"


def test_nine_hole_course_rating_check_uses_the_nine_hole_rating(db, tmp_path):
    data = _nine_hole_club(_example())
    sync_courses(db, _write(tmp_path, data))
    codes = {i["code"] for i in check_courses(db) if i["tee_id"] == WHITE}
    assert "tee_missing_rating" not in codes and "tee_missing_holes" not in codes
    tee = data["clubs"][PINES]["courses"]["synthetic-pines"]["tees"][0]
    tee.pop("slope_f9")
    sync_courses(db, _write(tmp_path, data))
    missing = [i for i in check_courses(db) if i["code"] == "tee_missing_rating"]
    assert [i["tee_id"] for i in missing] == [WHITE] and "cr_f9/slope_f9" in missing[0]["message"]


def test_round_override_nine_wins_and_works_without_courses_yaml(db, tmp_path):
    rid = CASES["nine_array"]
    db.execute("INSERT INTO round_overrides(round_id, nine) VALUES (?, 'front')", (rid,))
    sync_courses(db, EXAMPLE_YAML)                                              # yaml says back
    assert _round(db, "nine_array")["nine"] == "front" and list(_holes(db, "nine_array")) == list(range(1, 10))
    db.execute("UPDATE round_overrides SET nine = 'back' WHERE round_id = ?", (rid,))
    empty = _write(tmp_path, {"clubs": {}})
    s = sync_courses(db, empty)                                                 # club not in the file
    assert s["nine_resolved"] == 1 and _round(db, "nine_array")["nine"] == "back"
    assert list(_holes(db, "nine_array")) == list(range(10, 19))
    assert apply_nine_overrides(db) == 0                                        # idempotent
