"""Dashboard build (demo data and empty DB), per-9 scoring, course views, club distances, public mode,
export age, self-containment, escaping, and the demo seeder."""
from __future__ import annotations

import json
import re
from datetime import date

import pytest

from golf.dashboard.build import PLACEHOLDER, TEMPLATE_PATH, build, dashboard_data, render_dashboard
from golf.db import dumps
from golf.demo import clear_demo, has_demo, seed_demo

END = date(2026, 9, 20)
TODAY = date(2026, 9, 25)
DATA_RE = re.compile(r'<script type="application/json" id="golf-data">(.*?)</script>', re.S)
BAG = {"Driver", "3W", "5H", "6i", "7i", "8i", "9i", "PW", "SW", "Putter"}


def _embedded(html: str) -> dict:
    return json.loads(DATA_RE.search(html).group(1))


def _assert_self_contained(html: str) -> None:
    assert "http://" not in html and "https://" not in html
    assert not re.search(r"<script[^>]*\bsrc\s*=", html, re.I)
    assert not re.search(r"<link\b", html, re.I)
    assert "@import" not in html and "url(" not in html
    assert html.count("</script>") == 2


def test_build_with_demo_data(conn, cfg):
    seed_demo(conn, end=END)
    path = build(conn, cfg, today=TODAY)
    assert path == cfg.site_dir / "index.html" and path.exists()
    html = path.read_text()
    _assert_self_contained(html)
    data = _embedded(html)
    assert data["is_demo"] is True and data["empty"] is False and data["public"] is False
    assert data["metric"]["key"] == "differential" and data["score"]["key"] == "to_par_9"
    assert [k["id"] for k in data["kpis"]] == ["last5", "best9", "hi", "season", "lesson", "export"]
    kp = {k["id"]: k for k in data["kpis"]}
    assert kp["last5"]["badge"] == "to par per 9" and kp["last5"]["display"].startswith("+")
    assert kp["hi"]["badge"] == "unofficial" and "9-hole rounds included" in kp["hi"]["sub"]
    assert kp["export"]["display"] == "none" and kp["export"]["sub"] == "demo data only; no real export yet"
    assert kp["season"]["sub"] == "17 nines · 7 eighteens"
    assert len(data["rounds"]) == 24 and len(data["lessons"]["lessons"]) == 3
    assert "differential points (roughly" in data["lessons"]["mde_sentence"]
    assert "strokes per 9 holes) to stand out with 5 rounds each side" in data["lessons"]["mde_sentence"]
    keys = {p["key"] for p in data["multiples"]}
    assert {"fir_pct", "gir_9", "putts_9", "to_green_9", "putts_over_9", "dbl_9", "par3_avg", "three_putt_pct",
            "scramble_pct", "penalties_9"} <= keys
    assert not keys & {"putts_18", "dbl_18", "gir_pct"}
    q = data["quality"]
    assert q["demo"] and q["unmapped_clubs"] and q["flagged_rounds"] and q["pending_events"] == 1
    assert [p["id"] for p in q["partial_putts"]] == ["demo-008"] and q["include_nine"] is True
    assert {e["lane"] for e in data["events"]} == {"lesson", "practice", "equipment", "injury"}
    assert data["spans"]["injury"] and len(data["spans"]["practice"]) == 3
    ident = data["identity"]
    assert ident["to_par"] == pytest.approx(ident["to_green"] + ident["putts_over"], abs=1e-3)


def test_primary_score_is_to_par_per_9(conn, cfg):
    seed_demo(conn, end=END)
    data = dashboard_data(conn, cfg, today=TODAY, n_boot=200)
    pts = data["hero"]["points"]
    assert len(pts) == 24 and data["hero"]["missing"] == 0
    for p in pts:                                   # 9-hole rounds as played, 18-hole rounds halved
        assert p["y"] == pytest.approx(p["to_par"] * 9 / p["holes"])
    full = [p for p in pts if p["holes"] == 18]
    assert len(full) == 7 and any(p["halves"] for p in full)
    assert len(data["hero"]["trend"]) == 24 and data["hero"]["sigma"] > 0
    rows = {r["id"]: r for r in data["rounds"]}
    nine = next(r for r in data["rounds"] if r["holes"] == 9 and r["putts"] is not None)
    assert nine["to_par_9"] == nine["to_par"]
    putts = next(p for p in data["multiples"] if p["key"] == "putts_9")
    assert all(pt["id"] != "demo-008" for pt in putts["points"])            # partial putts left out
    assert rows["demo-008"]["putts_partial"] and rows["demo-008"]["putts"] is None
    recorded = conn.execute("SELECT putts FROM rounds WHERE round_id = 'demo-008'").fetchone()[0]
    assert 0 < rows["demo-008"]["putts_recorded"] == recorded < 9


def test_differential_chart_carries_18birdies_beside_ours(conn, cfg):
    seed_demo(conn, end=END)
    data = dashboard_data(conn, cfg, today=TODAY, n_boot=200)
    diff = data["diff"]
    assert diff["n_ours"] == 23 and diff["n_18b"] == 23 and diff["hi"]
    muni = next(p for p in diff["points"] if p["ours"] is None)
    assert muni["b18"] is not None                                          # 18Birdies rates the unmapped club
    ss = next(p for p in diff["points"] if p["id"].startswith("demo-ss-"))
    assert ss["b18"] is None and ss["ours"] is not None                     # screenshot-only: not in 18Birdies
    rows = {r["id"]: r for r in data["rounds"]}
    assert rows["demo-023"]["b18"] == pytest.approx(rows["demo-023"]["diff_equiv"], abs=0.05)
    # "Ours" is the differential that counts toward the HI (so the dots and the HI line agree), not the
    # lesson analysis' 2 x 9-hole value; a nine also carries its own 9-hole differential for the tooltip.
    stored = dict(conn.execute("SELECT round_id, differential FROM handicap_history").fetchall())
    assert all(p["ours"] == stored.get(p["id"]) for p in diff["points"])
    nine = next(p for p in diff["points"] if p["holes"] == 9 and p["ours"] is not None)
    assert nine["nine9"] == pytest.approx(rows[nine["id"]]["diff_equiv"] / 2, abs=0.05)
    assert nine["ours"] == rows[nine["id"]]["differential"] != pytest.approx(rows[nine["id"]]["diff_equiv"], abs=0.05)


def test_course_filter_views(conn, cfg):
    seed_demo(conn, end=END)
    data = dashboard_data(conn, cfg, today=TODAY, n_boot=200)
    opts = data["courses"]
    assert [(o["key"], o["n"]) for o in opts] == [("all", 24), ("c1", 16), ("c2", 7), ("other", 1)]
    assert opts[1]["label"] == "Demo Brook Nine (synthetic)"
    assert set(data["views"]) == {"c1", "c2", "other"}
    for o in opts[1:]:
        view = data["views"][o["key"]]
        assert view["n"] == o["n"] == len(view["hero"]["points"])
        assert sum(r["view"] == o["key"] for r in data["rounds"]) == o["n"]
        assert set(view) >= {"hero", "diff", "lessons", "multiples", "identity", "clubs"}
    home = data["views"]["c1"]
    assert all(p["holes"] == 9 for p in home["hero"]["points"])
    assert home["clubs"]["coverage"]["rounds_total"] == 16
    assert data["views"]["other"]["clubs"] is None                          # no shots at the muni


def test_club_distances_panel(conn, cfg):
    seed_demo(conn, end=END)
    data = dashboard_data(conn, cfg, today=TODAY, n_boot=200)
    dist, cov = data["clubs"]["distances"], data["clubs"]["coverage"]
    clubs = [c["club"] for c in dist["clubs"]]
    assert clubs[0] == "Driver" and clubs[-1] == "SW" and "Putter" not in clubs and set(clubs) <= BAG
    for c in dist["clubs"]:
        assert c["q1"] <= c["median"] <= c["q3"] and c["min"] <= c["q1"] and c["q3"] <= c["max"]
        assert c["n"] == len(c["shots"]) and all(5 <= s["d"] <= 400 for s in c["shots"])
    bad = sorted((x["club"], x["d"], x["reason"]) for x in dist["invalid"])
    assert ("Driver", 451.0, "over 400 yards") in bad and ("SW", 2.0, "under 5 yards") in bad
    defaults = [b for b in bad if b[2].startswith("same distance")]          # after-round default spots
    assert len(defaults) == 4 and {b[1] for b in defaults} == {131.6}
    assert dist["n_after"] > 0 and cov["rounds_logged_after"] == 2
    assert {r["logged"] for r in cov["rounds"]} == {"live", "after"}
    assert any(s["after"] for c in dist["clubs"] for s in c["shots"])
    assert dist["n_putts"] > 0 and dist["min_yards"] == 5 and dist["max_yards"] == 400
    assert 0 < cov["rounds_with_shots"] < cov["rounds_total"] == 24
    assert cov["holes_with_shots"] < cov["holes_played"]                    # some holes untracked
    assert all(h["rounds"] <= h["played"] for h in cov["by_hole"])


def test_public_mode_has_no_paths_coordinates_or_cost(conn, cfg):
    seed_demo(conn, end=END)
    conn.execute("""INSERT INTO llm_calls(cache_key, purpose, model, prompt_version, schema_version, cost_usd,
                    created_at) VALUES ('k1', 'note', 'claude-opus-5', 'v1', 'v1', 0.4321, '2026-09-01T00:00:00')""")
    conn.execute("INSERT INTO imports(kind, path, sha256, imported_at, summary) VALUES "
                 "('18b_export', '/Users/someone/Downloads/18Birdies_archive_20260910.json', 'real-sha', "
                 "'2026-09-11T01:30:00+00:00', ?)", (dumps({"snapshot_date": "2026-09-10"}),))
    # A distinctive, real-looking position on a real round, plus every long-precision demo coordinate.
    conn.execute("""INSERT INTO shots(round_id, seq, hole, club_type, club_number, club, distance_yards, start_lat,
                    start_lon, end_lat, end_lon) VALUES ('demo-023', 999, 1, 'WOOD', '1', 'Driver', 201.0,
                    42.281937, -71.556204, 42.283311, -71.557891)""")
    coords = {str(v) for row in conn.execute("SELECT start_lat, start_lon, end_lat, end_lon FROM shots")
              for v in row if v is not None and len(str(v).split(".")[-1]) >= 5}
    assert {"42.281937", "-71.556204"} <= coords and len(coords) > 100
    out = build(conn, cfg, public=True, today=TODAY)
    assert out == cfg.site_dir / "public" / "index.html"
    html = out.read_text()
    _assert_self_contained(html)
    data = _embedded(html)
    blob = json.dumps(data)
    assert data["public"] is True
    for needle in ("cost", "spend", "usd", "0.4321", "imported_at", "IMG_0001", ".PNG", "golf.db", "/Users",
                   "Downloads", str(cfg.root), str(cfg.data_dir), "golf courses check", "golf recompute",
                   "golf demo clear", "_lat", "_lon", "latitude", "longitude"):
        assert needle not in blob, needle
    assert not any(c in blob for c in coords)
    # ...but all the golf is there: rounds, per-9 scores, club distances, lessons with their notes.
    assert len(data["rounds"]) == 24 and data["clubs"]["distances"]["clubs"] and data["hero"]["points"]
    assert data["lessons"]["lessons"][0]["summary_note"] and any(e["summary"] for e in data["events"])
    assert data["quality"]["export"] == {"snapshot_date": "2026-09-10", "age_days": 15, "future": False,
                                         "source": "snapshot"}
    private = dashboard_data(conn, cfg, today=TODAY, n_boot=200)
    assert private["quality"]["export"]["imported_at"] and not any(c in json.dumps(private) for c in coords)
    assert any("golf demo clear" in i["text"] for i in private["quality"]["items"])


def test_export_age_uses_local_snapshot_date(conn, cfg):
    def imp(sha, imported_at, summary):
        conn.execute("INSERT INTO imports(kind, sha256, imported_at, summary) VALUES ('18b_export', ?, ?, ?)",
                     (sha, imported_at, dumps(summary)))

    def export() -> dict:
        return dashboard_data(conn, cfg, today=TODAY, n_boot=50)["quality"]["export"]

    imp("demo-export-7", "2026-09-24T12:00:00+00:00", {"demo": 1, "snapshot_date": "2026-09-24"})
    assert export()["age_days"] is None                                     # demo rows never count
    # An evening import (20:44 EDT is already tomorrow in UTC) with no snapshot date: 0 days, not -1.
    imp("a", "2026-09-26T00:44:00+00:00", {})
    assert export() == {"snapshot_date": "2026-09-25", "age_days": 0, "future": False, "source": "imported",
                        "imported_at": "2026-09-26T00:44:00+00:00"}
    # An old snapshot imported today is old: the snapshot date wins over the import time.
    conn.execute("DELETE FROM imports WHERE sha256 = 'a'")
    imp("b", "2026-09-26T00:44:00+00:00", {"snapshot_date": "2026-06-01"})
    data = dashboard_data(conn, cfg, today=TODAY, n_boot=50)
    tile = next(k for k in data["kpis"] if k["id"] == "export")
    assert tile["value"] == 116 and tile["status"] == "warning" and "fresh one" in tile["sub"]
    # A snapshot dated in the future is clamped to 0 and flagged.
    imp("c", "2026-09-20T12:00:00+00:00", {"snapshot_date": "2026-12-01"})
    exp = export()
    assert exp["age_days"] == 0 and exp["future"] is True and exp["snapshot_date"] == "2026-12-01"
    items = dashboard_data(conn, cfg, today=TODAY, n_boot=50)["quality"]["items"]
    assert any("in the future" in i["text"] for i in items)


def test_build_with_empty_db(conn, cfg, tmp_path):
    out = build(conn, cfg, tmp_path / "empty.html", today=TODAY)
    html = out.read_text()
    _assert_self_contained(html)
    data = _embedded(html)
    assert data["empty"] is True and data["rounds"] == [] and data["hero"]["points"] == []
    assert data["lessons"]["sigma"] is None and "can't be estimated" in data["lessons"]["mde_sentence"]
    assert data["quality"]["items"][0]["text"] == "No 18Birdies export imported yet."
    kp = {k["id"]: k for k in data["kpis"]}
    assert kp["export"]["display"] == "none" and kp["last5"]["display"] == "—" and kp["best9"]["value"] is None
    assert data["clubs"] is None and data["diff"]["points"] == [] and data["views"] == {}
    assert [o["key"] for o in data["courses"]] == ["all"]
    public = _embedded(build(conn, cfg, tmp_path / "p.html", public=True, today=TODAY).read_text())
    assert public["empty"] is True and public["public"] is True


def test_nine_hole_setting_is_read_from_meta(conn, cfg):
    seed_demo(conn, end=END)
    conn.execute("INSERT INTO meta(key, value) VALUES ('whs_include_nine', '0')")
    data = dashboard_data(conn, cfg, today=TODAY, n_boot=50)
    assert data["quality"]["include_nine"] is False
    assert "18-hole rounds only" in next(k for k in data["kpis"] if k["id"] == "hi")["sub"]


def test_dashboard_data_is_strict_json(conn, cfg):
    seed_demo(conn, end=END)
    data = dashboard_data(conn, cfg, today=TODAY, n_boot=300)
    json.dumps(data, allow_nan=False)
    again = dashboard_data(conn, cfg, today=TODAY, n_boot=300)
    data.pop("generated_at"), again.pop("generated_at")
    assert data == again                                     # deterministic for a fixed DB and date


def test_render_escapes_hostile_text():
    evil = "</script><script>alert(1)</script> see https://example.com/x & <b>"
    data = {"note": evil, "empty": True}
    html = render_dashboard(data)
    _assert_self_contained(html)
    assert _embedded(html)["note"] == evil


def test_template_contract():
    tpl = TEMPLATE_PATH.read_text()
    assert tpl.count(PLACEHOLDER) == 1
    assert "prefers-color-scheme: dark" in tpl and ':root[data-theme="dark"]' in tpl and "system-ui" in tpl
    assert 'name="viewport"' in tpl
    for el in ('id="course"', 'id="hero-chart"', 'id="diff-chart"', 'id="clubs-chart"', 'id="rounds-table"'):
        assert el in tpl, el
    assert "innerHTML = '<svg></svg>'" in tpl and tpl.count("innerHTML") == 1      # data only via textContent


def test_to_par_mode_shows_only_18birdies_differentials(conn, cfg):
    seed_demo(conn, end=END)
    conn.execute("UPDATE tees SET cr18 = NULL, slope18 = NULL, cr_f9 = NULL, slope_f9 = NULL, cr_b9 = NULL, "
                 "slope_b9 = NULL")
    conn.execute("UPDATE handicap_history SET differential = NULL")
    data = dashboard_data(conn, cfg, today=TODAY, n_boot=200)
    assert data["metric"]["key"] == "to_par_9" and data["metric"]["short"] == "to par per 9"
    assert data["lessons"]["sigma_basis"] == "9-hole rounds" and "two nines" in data["lessons"]["method"]
    diff = data["diff"]
    assert diff["n_ours"] == 0 and diff["n_18b"] == 23 and diff["hi"] == []
    assert diff["note"].startswith("No course ratings yet") and "courses.yaml" in diff["note"]
    assert "courses.yaml" not in dashboard_data(conn, cfg, public=True, today=TODAY, n_boot=50)["diff"]["note"]
    assert next(k for k in data["kpis"] if k["id"] == "hi")["value"] is not None


# ----------------------------------------------------------------------------- demo seeder
def test_seed_demo_contents(conn):
    info = seed_demo(conn, end=END)
    assert info["rounds"] == 24 and info["lessons"] == 3 and info["shots"] > 200
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    assert q("SELECT COUNT(*) FROM rounds WHERE round_id NOT LIKE 'demo-%'") == 0
    assert q("SELECT COUNT(*) FROM rounds WHERE holes_played = 9") == 17
    assert q("SELECT COUNT(*) FROM rounds WHERE holes_played = 9 AND course_key = 'demo-brook'") == 16
    assert q("SELECT COUNT(*) FROM rounds WHERE holes_played = 18") == 7
    assert q("SELECT COUNT(*) FROM rounds WHERE entry_mode = 'total_only'") == 1
    assert q("SELECT COUNT(*) FROM rounds WHERE source = 'screenshot'") == 1
    assert q("SELECT COUNT(*) FROM rounds WHERE putts_tracked = 0 AND dq_flags LIKE '%putts_partial%'") == 1
    assert q("SELECT COUNT(*) FROM rounds WHERE source = '18b_export' AND round_handicap_18b IS NULL") == 0
    assert q("SELECT COUNT(*) FROM rounds WHERE source = 'screenshot' AND round_handicap_18b IS NOT NULL") == 0
    assert q("SELECT COUNT(*) FROM rounds WHERE sg_overall IS NOT NULL") >= 2
    assert q("SELECT COUNT(DISTINCT round_id) FROM round_holes WHERE stats_src = 'screenshot'") >= 5
    assert q("SELECT COUNT(*) FROM tee_holes") == 27 and q("SELECT cr_f9 FROM tees WHERE tee_id LIKE 'demo-brook%'")
    assert q("SELECT COUNT(*) FROM handicap_history WHERE hi_after IS NOT NULL") > 10
    assert q("SELECT COUNT(*) FROM handicap_history WHERE differential_kind = 'nine_hole_scaled' "
             "AND differential IS NOT NULL") > 10                          # 9-hole rounds count (the default)
    assert q("SELECT COUNT(*) FROM events WHERE event_type = 'lesson' AND coach <> '' AND focus_areas <> '[]'") == 3
    for kind in ("practice", "equipment_change", "injury"):
        assert q(f"SELECT COUNT(*) FROM events WHERE event_type = '{kind}'") >= 1
    assert q("SELECT MIN(played_on_local) FROM rounds") >= "2026-01-25"
    assert q("SELECT MAX(played_on_local) FROM rounds") <= END.isoformat()
    # Shots: bag labels, raw 18Birdies type/number kept, synthetic mid-Atlantic coordinates.
    clubs = {r[0] for r in conn.execute("SELECT DISTINCT club FROM shots")}
    assert clubs <= BAG and {"Driver", "PW", "SW", "Putter"} <= clubs
    assert q("SELECT COUNT(*) FROM shots WHERE club_type IS NULL OR club_number IS NULL") == 0
    assert q("SELECT MIN(start_lat) FROM shots") > 0 and q("SELECT MAX(start_lat) FROM shots") < 1
    assert q("SELECT MIN(start_lon) FROM shots") == -30.0
    assert q("SELECT COUNT(*) FROM shots WHERE distance_yards < 5 AND club <> 'Putter'") >= 1
    assert q("SELECT COUNT(*) FROM shots WHERE distance_yards > 400") >= 1
    assert q("SELECT COUNT(*) FROM shots s JOIN rounds r USING (round_id) WHERE r.tee_name IS NULL") == 0
    assert has_demo(conn)


def test_seed_demo_is_deterministic_and_idempotent(conn):
    seed_demo(conn, end=END)
    first = conn.execute("SELECT round_id, played_on_local, gross, round_handicap_18b FROM rounds "
                         "ORDER BY round_id").fetchall()
    shots = conn.execute("SELECT round_id, seq, club, distance_yards FROM shots ORDER BY round_id, seq").fetchall()
    seed_demo(conn, end=END)
    again = conn.execute("SELECT round_id, played_on_local, gross, round_handicap_18b FROM rounds "
                         "ORDER BY round_id").fetchall()
    assert [tuple(r) for r in first] == [tuple(r) for r in again]
    assert [tuple(r) for r in shots] == [tuple(r) for r in conn.execute(
        "SELECT round_id, seq, club, distance_yards FROM shots ORDER BY round_id, seq")]
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1


def test_clear_demo_keeps_real_rows(conn):
    conn.execute("INSERT INTO rounds(round_id, source, played_on_local, entry_mode, gross) "
                 "VALUES ('real-1', 'manual', '2026-06-01', 'total_only', 88)")
    conn.execute("INSERT INTO source_docs(doc_id, source) VALUES ('man-1', 'manual')")
    seed_demo(conn, end=END, n_rounds=10)
    removed = clear_demo(conn)
    assert removed["rounds"] == 10 and removed["events"] > 0
    assert not has_demo(conn)
    for table in ("round_holes", "handicap_history", "events", "event_reviews", "tees", "tee_holes", "courses",
                  "clubs", "imports", "extractions", "shots"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert [r[0] for r in conn.execute("SELECT round_id FROM rounds")] == ["real-1"]
    assert [r[0] for r in conn.execute("SELECT doc_id FROM source_docs")] == ["man-1"]


def test_first_screen_verdict_home_handicap_triples_and_hole_averages(conn, cfg):
    seed_demo(conn, end=END)
    data = dashboard_data(conn, cfg, today=TODAY, n_boot=50)
    verdict = data["hero"]["verdict"]
    assert verdict.startswith("Trend now ") and "Three weeks earlier" in verdict and "80% noise band" in verdict
    hi = next(k for k in data["kpis"] if k["id"] == "hi")
    if hi["home"]:                                     # 9-hole Course Handicap at the most-played rated nine
        assert f"≈ {hi['home']['strokes']} strokes per 9 at" in hi["sub"]
    assert any(p["key"] == "triple_9" for p in data["multiples"])
    holes = data["holes"]
    assert holes and holes["rounds"] >= 5 and holes["holes"]
    assert all({"hole", "par", "si", "avg", "n", "last"} <= set(h) for h in holes["holes"])
    assert data["data_through"] == max(r["date"] for r in data["rounds"])


def test_public_page_speaks_about_the_player_and_hides_maintenance_details(conn, cfg):
    seed_demo(conn, end=END)
    pub = dashboard_data(conn, cfg, public=True, today=TODAY, n_boot=50)
    ids = [k["id"] for k in pub["kpis"]]
    assert "export" not in ids and ids[-1] == "through"
    assert pub["identity"]["sentence"].startswith("Per 9 holes Shane averages")
    assert not any("Per-hole putts" in i["text"] or "tier" in i["text"] for i in pub["quality"]["items"])
    assert "your" not in pub["lessons"]["mde_sentence"].lower()
    assert {r["id"] for r in pub["rounds"]} == {f"r{i}" for i in range(1, 25)}
    assert not re.search(r"demo-0\d\d", json.dumps(pub))                  # every round id aliased, everywhere
    local = dashboard_data(conn, cfg, today=TODAY, n_boot=50)
    assert local["identity"]["sentence"].startswith("Per 9 holes you average")
    assert any("screenshots (Inbox)" in i["text"] for i in local["quality"]["items"])


def test_ask_the_caddie_link_only_when_the_relay_is_configured(conn, cfg, tmp_path):
    seed_demo(conn, end=END)
    tpl = TEMPLATE_PATH.read_text()
    assert re.search(r'<a class="caddie-link" id="caddie-link" href="caddie/" hidden>', tpl)   # hidden by default
    plain = build(conn, cfg, tmp_path / "plain.html", public=True, today=TODAY).read_text()
    assert "caddie_url" not in _embedded(plain)
    linked = build(conn, cfg, tmp_path / "linked.html", public=True, today=TODAY, caddie_link="caddie/").read_text()
    _assert_self_contained(linked)                          # a relative link: still no network, no URL
    assert _embedded(linked)["caddie_url"] == "caddie/"
    for bad in ("https://evil.example/", "//evil.example/", "../../x/", "/caddie/"):
        with pytest.raises(ValueError):
            build(conn, cfg, tmp_path / "bad.html", public=True, today=TODAY, caddie_link=bad)
