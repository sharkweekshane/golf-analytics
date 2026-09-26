"""CLI smoke tests on a temp data dir (Typer's CliRunner). Offline: fake_llm stands in for Claude, a fake
runner for Apple Notes, and all data is synthetic.

privacy-check: synthetic (this file writes fake PII into temp files to prove the scanner catches it)
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from typer.testing import CliRunner

import golf.cli as cli
import golf.llm as llm
import golf.notes.applenotes as applenotes
from golf.config import PROJECT_ROOT
from golf.db import connect
from golf.demo import seed_demo

FIX = Path(__file__).parent / "fixtures"
CLEAN = json.loads((FIX / "rounds" / "clean_18.json").read_text())
runner = CliRunner()


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def no_api(request, use_fallbacks=False):
        raise AssertionError("unexpected Claude API call")

    def no_osascript(args):
        raise AssertionError("unexpected osascript call")

    monkeypatch.setattr(llm, "SEND", no_api)
    monkeypatch.setattr(applenotes, "_default_runner", no_osascript)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


def run(*args, input: str | None = None, code: int = 0):
    result = runner.invoke(cli.app, [str(a) for a in args], input=input)
    assert result.exit_code == code, f"exit {result.exit_code}\n{result.output}\n{result.exception!r}"
    return result.output


def pngs(folder: Path, n: int = 3) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        p = folder / f"IMG_{i:03d}.png"
        Image.new("RGB", (60, 120), (40 * i, 90, 200 - 30 * i)).save(p)
        out.append(p)
    return out


def db(cfg):
    return connect(cfg.db_path)


# ------------------------------------------------------------------ setup / status
def test_init_creates_folders_db_config_copies_and_hook(cfg):
    root = Path(cfg.root)
    for name in ("courses.example.yaml", "entities.example.yaml", ".env.example"):
        shutil.copyfile(PROJECT_ROOT / name, root / name)
    (root / ".git" / "hooks").mkdir(parents=True)

    out = run("init")
    assert cfg.db_path.exists() and (cfg.data_dir / "inbox" / "screenshots").is_dir()
    assert (cfg.data_dir / "raw" / "18birdies").is_dir() and (cfg.data_dir / "inbox" / "notebook").is_dir()
    assert (cfg.data_dir / "logs").is_dir()
    for name in ("courses.yaml", "entities.yaml", ".env"):
        assert (root / name).exists(), name
    # empty starters, not copies of the synthetic examples (a fake club / placeholder coaches in real data)
    courses, entities = (root / "courses.yaml").read_text(), (root / "entities.yaml").read_text()
    assert "Synthetic Pines" not in courses and "clubs: {}" in courses and "courses.example.yaml" in courses
    assert "Mike Rossi" not in entities and "coaches: []" in entities
    from golf.courses import load_courses
    from golf.notes.entities import load_entities_file

    assert not load_courses(root / "courses.yaml").clubs and not load_entities_file(root / "entities.yaml").coaches
    assert "Synced 0 club(s)" in run("courses", "sync")
    hook = root / ".git" / "hooks" / "pre-commit"
    assert "privacy-check --staged" in hook.read_text() and os.access(hook, os.X_OK)
    assert f'PYTHONPATH="{root.resolve()}' in hook.read_text()
    assert "installed" in out and "golf demo seed" in out

    (root / "entities.yaml").write_text("coaches: []\n")
    again = run("init")
    assert "entities.yaml: exists, left as is." in again and (root / "entities.yaml").read_text() == "coaches: []\n"

    hook.write_text("#!/bin/sh\necho mine\n")
    assert "already exists" in run("init") and hook.read_text() == "#!/bin/sh\necho mine\n"


def test_status_reports_but_never_prints_the_key(cfg, monkeypatch):
    out = run("status")
    assert "NOT set" in out and "never imported" in out and "courses.yaml: missing" in out
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-DO-NOT-PRINT-123")
    out = run("status")
    assert "key set" in out and "DO-NOT-PRINT" not in out


def test_status_counts_billed_calls_and_unpriced_ones(cfg):
    conn = db(cfg)
    conn.execute("INSERT INTO llm_calls(cache_key, purpose, model, prompt_version, schema_version, cost_usd, "
                 "created_at, billed_calls, cost_total_usd) VALUES ('a', 'note', 'claude-opus-5', 'v1', 'v1', 0.1, "
                 "'2026-09-25', 3, 0.3), ('b', 'note', 'mystery-model', 'v1', 'v1', NULL, '2026-09-25', 1, NULL)")
    conn.commit()
    conn.close()
    out = run("status")
    assert "4 calls billed, $0.30 spent so far (1 on a model with no known price" in out


def test_status_flags_placeholder_entities(cfg):
    shutil.copyfile(PROJECT_ROOT / "entities.example.yaml", Path(cfg.root) / "entities.example.yaml")
    shutil.copyfile(PROJECT_ROOT / "entities.example.yaml", Path(cfg.root) / "entities.yaml")
    assert "still the example's placeholder names" in run("status")


# ------------------------------------------------------------------ export
def test_missing_export_explains_where_to_download_it(cfg, tmp_path):
    out = run("import-18b", code=2)
    assert "18birdies.com/download-account-data" in out and "Request My Data" in out
    out = run("import-18b", tmp_path / "nope.json", code=2)
    assert "No file at" in out and "imported automatically" in out and "golf watch install" in out
    bad = tmp_path / "18Birdies_archive_20260101.json"
    bad.write_text("{not json")
    assert "does not look like an 18Birdies export" in run("import-18b", bad, code=2)
    assert not (cfg.data_dir / "raw" / "18birdies").exists() or not list((cfg.data_dir / "raw" / "18birdies").iterdir())


def test_import_pipeline_copies_the_file_maps_courses_and_is_idempotent(cfg, tmp_path):
    from fixtures.synthetic_archive import make_archive, write_archive

    shutil.copyfile(PROJECT_ROOT / "courses.example.yaml", Path(cfg.root) / "courses.yaml")
    src = write_archive(tmp_path / "downloads" / "18Birdies_archive_20251201.json", make_archive())
    assert "9-hole rounds left out" in run("recompute", "--no-include-nine")      # 18-hole-only index below
    out = run("import-18b", src)
    assert "inserted" in out and "Courses:" in out and "Handicap (unofficial, PCC 0)" in out and "index 9.2" in out
    kept = cfg.data_dir / "raw" / "18birdies" / src.name
    assert kept.read_bytes() == src.read_bytes()
    assert "already imported" in run("import-18b", src)
    assert "already imported" in run("import-18b")                     # default: newest file in data/raw/18birdies
    assert "Using" in run("import-18b")
    out = run("inspect-export", kept)
    assert "Schema check OK" in out and "fake.golfer" not in out
    check = run("courses", "check")
    assert "unmapped_club" in check or "ERROR" in check or "WARN" in check
    assert "Synced" in run("courses", "sync") and "index 9.2" in run("recompute")


def test_the_nine_hole_choice_is_remembered_by_every_later_recompute(cfg, tmp_path):
    from fixtures.synthetic_archive import make_archive, write_archive

    shutil.copyfile(PROJECT_ROOT / "courses.example.yaml", Path(cfg.root) / "courses.yaml")
    out = run("import-18b", write_archive(tmp_path / "18Birdies_archive_20251201.json", make_archive()))
    assert "9-hole rounds included." in out                            # the default: Shane's golf is mostly 9s
    assert "9-hole rounds left out." in run("recompute", "--no-include-nine")
    for args in (("courses", "sync"), ("recompute",), ("import-18b",)):
        assert "9-hole rounds left out." in run(*args), args
    assert "9-hole rounds left out" in run("status")
    assert "9-hole rounds included." in run("recompute", "--include-nine")
    assert "9-hole rounds included." in run("courses", "sync")
    conn = db(cfg)
    assert conn.execute("SELECT value FROM meta WHERE key = 'whs_include_nine'").fetchone()[0] == "1"
    conn.close()


def test_newest_export_is_chosen_by_snapshot_date_not_by_name(cfg, tmp_path):
    folder = cfg.data_dir / "raw" / "18birdies"
    folder.mkdir(parents=True)
    old, newer, dated = (folder / "18Birdies_archive_20260801.json", folder / "18Birdies_archive_20260801-2.json",
                         folder / "18Birdies_archive_20260920.json")
    for i, p in enumerate((old, newer)):
        p.write_text("{}")
        os.utime(p, (1_000_000 + i * 100, 1_000_000 + i * 100))
    assert cli.latest_export_file(cfg) == newer                     # same snapshot day: the later file
    dated.write_text("{}")
    os.utime(dated, (900_000, 900_000))
    assert cli.latest_export_file(cfg) == dated                     # a later snapshot date wins over mtime


def test_import_stores_a_dated_copy_once_whatever_the_download_is_called(cfg, tmp_path):
    from fixtures.synthetic_archive import make_archive, write_archive

    src = write_archive(tmp_path / "Downloads" / "18Birdies_archive (1).json", make_archive())
    os.utime(src, (1_790_000_000, 1_790_000_000))                     # 2026-09-21 local
    run("import-18b", src)
    dup = tmp_path / "Downloads" / "18Birdies_archive-2.json"
    shutil.copy2(src, dup)
    assert "already imported" in run("import-18b", dup)
    stored = sorted(p.name for p in (cfg.data_dir / "raw" / "18birdies").iterdir())
    assert stored == ["18Birdies_archive_20260921.json"] and src.exists() and dup.exists()


def test_import_clears_demo_data_first(cfg, tmp_path):
    from fixtures.synthetic_archive import make_archive, write_archive

    conn = db(cfg)
    seed_demo(conn)
    conn.close()
    out = run("import-18b", write_archive(tmp_path / "18Birdies_archive_20251201.json", make_archive()))
    assert "Removed the synthetic demo data first." in out
    conn = db(cfg)
    assert conn.execute("SELECT COUNT(*) FROM rounds WHERE round_id LIKE 'demo-%'").fetchone()[0] == 0
    conn.close()


# ------------------------------------------------------------------ screenshots + review
def test_extract_without_a_key_explains_the_env_file(cfg, tmp_path, monkeypatch):
    def unavailable(request, use_fallbacks=False):
        raise llm.LLMUnavailable("No Anthropic API key.")

    monkeypatch.setattr(llm, "SEND", unavailable)
    out = run("extract", *pngs(tmp_path / "shots", 2), "--date", "2026-09-20", code=2)
    assert "ANTHROPIC_API_KEY" in out and ".env" in out and "console.anthropic.com" in out
    assert "Traceback" not in out
    assert run("extract", tmp_path / "missing", code=2).strip().startswith("No such file or folder")
    assert "Dates are YYYY-MM-DD" in run("extract", tmp_path / "shots", "--date", "9/20", code=2)


def test_extract_copies_outside_images_into_data_and_auto_accepts(cfg, tmp_path, fake_llm):
    fake_llm.push(json.dumps(CLEAN))
    pngs(tmp_path / "shots-folder")
    out = run("extract", tmp_path / "shots-folder")
    assert "auto_accepted" in out and "round ss-" in out
    copies = list((cfg.data_dir / "raw" / "screenshots").rglob("*.png"))
    assert len(copies) == 3
    conn = db(cfg)
    stored = json.loads(conn.execute("SELECT image_paths FROM extractions").fetchone()[0])
    conn.close()
    assert all(p.startswith("raw/screenshots/cli-") for p in stored)
    assert "No screenshot rounds waiting" in run("review", "rounds")
    assert "(cached)" in run("extract", tmp_path / "shots-folder")        # same images: no second API call


def test_terminal_review_round_fix_accept_reject(cfg, tmp_path, fake_llm):
    d = copy.deepcopy(CLEAN)
    d["date_iso"], d["date_text"] = "", ""
    fake_llm.push(json.dumps(d))
    run("extract", *pngs(tmp_path / "a"))
    listing = run("review", "rounds")
    assert "#1" in listing and "(no date)" in listing and "1 error(s)" in listing
    grid = run("review", "round", 1)
    assert "D1" in grid and "strokes" in grid and "Maple Ridge GC" in grid
    assert "date_iso must be YYYY-MM-DD" in run("review", "fix", 1, "-", "date_iso", "20/9/26", code=2)
    assert "accepted" in run("review", "fix", 1, "-", "date_iso", "2026-09-20")
    assert "rejected" in run("review", "reject", 1)
    assert "No extraction" in run("review", "round", 99, code=2)


def test_extract_refuses_an_unreadable_image_before_copying_anything(cfg, tmp_path):
    folder = tmp_path / "shots"
    pngs(folder, 2)
    (folder / "empty.png").write_bytes(b"")
    out = run("extract", folder, "--date", "2025-10-20", code=2)
    assert "empty.png: not a readable image" in out and "Traceback" not in out
    assert not (cfg.data_dir / "raw" / "screenshots").exists() or not list(
        (cfg.data_dir / "raw" / "screenshots").rglob("*.png"))


def test_a_failed_extraction_leaves_no_staged_copies(cfg, tmp_path, monkeypatch):
    def unavailable(request, use_fallbacks=False):
        raise llm.LLMUnavailable("No Anthropic API key.")

    monkeypatch.setattr(llm, "SEND", unavailable)
    shots = pngs(tmp_path / "shots", 2)
    for _ in range(2):
        run("extract", *shots, code=2)
    assert not [p for p in (cfg.data_dir / "raw" / "screenshots").rglob("*") if p.is_file()]


@pytest.fixture
def transient(monkeypatch):
    """llm.LLMTransient (a subclass of LLMUnavailable) for network / 429 / 529 / unknown-model errors."""
    cls = getattr(llm, "LLMTransient", None) or type("LLMTransient", (llm.LLMUnavailable,), {})
    monkeypatch.setattr(llm, "LLMTransient", cls, raising=False)

    def busy(request, use_fallbacks=False):
        raise cls("Claude API unreachable or busy (APIConnectionError: Connection error.)")

    monkeypatch.setattr(llm, "SEND", busy)
    return cls


def test_api_outage_says_try_later_not_fix_your_key(cfg, tmp_path, transient, monkeypatch):
    out = run("extract", *pngs(tmp_path / "shots", 2), "--date", "2026-09-20", code=1)
    assert "Claude API unreachable or busy; re-run later" in out and "Traceback" not in out
    assert "console.anthropic.com" not in out
    out = run("notebook", *pngs(tmp_path / "pages", 1), code=1)
    assert "Claude API unreachable or busy; re-run later" in out
    monkeypatch.setattr(cli, "NOTES_RUNNER", FakeNotes())
    out = run("notes", "sync", code=1)
    assert "Claude API unreachable or busy; re-run later" in out and "Traceback" not in out
    assert "ANTHROPIC_API_KEY" in cli.llm_help(cfg, llm.LLMUnavailable("No Anthropic API key."))
    assert "ANTHROPIC_API_KEY" in cli.llm_help(cfg, "Anthropic API rejected the key: 401")
    model = cli.llm_help(cfg, "The Claude API does not know the model 'claude-x' (404). Check [llm].model.")
    assert "[llm].model" in model and "ANTHROPIC_API_KEY" not in model
    for text in ("The Claude API rate limit was reached (429). Wait a minute.",
                 "Could not reach the Claude API (no connection, or it timed out).",
                 "The Claude API is having problems right now (503). Re-run the same command later: ..."):
        assert cli.is_transient(text) and "re-run later" in cli.llm_help(cfg, text), text
    assert not cli.is_transient("The Anthropic API key is not allowed to make this request (403).")


def test_review_fix_refuses_holes_outside_1_to_18_and_warns_about_a_new_date(cfg, tmp_path, fake_llm):
    fake_llm.push(json.dumps(CLEAN))
    shots = pngs(tmp_path / "a")
    run("extract", *shots)
    assert "Hole must be 1-18, got 19" in run("review", "fix", 1, 19, "putts", 2, code=2)
    out = run("extract", *shots, "--date", "2026-09-22")
    assert "(cached)" in out and "golf review fix 1 - date_iso 2026-09-22" in out


def test_inbox_dry_run_and_empty(cfg, tmp_path):
    assert "Screenshot inbox: empty." in run("inbox")
    pngs(cfg.inbox_dir / "screenshots" / "2026-09-20 Maple", 2)
    out = run("inbox", "--dry-run")
    assert "2026-09-20 Maple (2 image(s)): extract" in out


# ------------------------------------------------------------------ notes, notebook, add
class FakeNotes:
    def __init__(self, fail: str | None = None):
        self.fail = fail
        self.note = {"id": "x-coredata://N/1", "name": "Range", "created": "2026-03-15T20:00:00.000Z",
                     "modified": "2026-03-15T20:00:00.000Z", "locked": False, "folder": "Golf"}

    def __call__(self, args):
        if self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr=self.fail)
        if args[4] == "meta":
            payload = {"ok": True, "accounts": ["iCloud"], "folders": [
                {"id": "f", "name": "Golf", "account": "iCloud", "subfolders": [], "notes": [self.note]}]}
        else:
            payload = {"ok": True, "bodies": [{"id": self.note["id"], "plaintext": "Range\n2026-03-15 range, wedges"}]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


def test_notes_sync_dry_run_and_permission_error(cfg, monkeypatch):
    monkeypatch.setattr(cli, "NOTES_RUNNER", FakeNotes())
    out = run("notes", "sync", "--dry-run")
    assert "[dry run] Apple Notes 'Golf' (iCloud): 1 new" in out
    monkeypatch.setattr(cli, "NOTES_RUNNER", FakeNotes(fail="Not authorized to send Apple events to Notes. (-1743)"))
    out = run("notes", "sync", code=2)
    assert "Automation" in out and "Traceback" not in out
    assert "Golf log" in run("notes", "template")
    assert "OFF" in run("notes", "auto-accept") and "ON" in run("notes", "auto-accept", "on")


def test_add_with_type_needs_no_api_and_bad_input_is_explained(cfg):
    out = run("add", "lesson w/ Mike, pump drill", "--date", "2026-09-20", "--type", "lesson", "--coach", "Mike",
              "--focus", "full swing, putting")
    assert "Added lesson on 2026-09-20" in out
    assert "unknown focus" in run("add", "x", "--type", "lesson", "--focus", "wedges", code=2)
    assert "unknown event type" in run("add", "x", "--type", "brunch", code=2)
    assert "Dates are YYYY-MM-DD" in run("add", "x", "--date", "Sept 20", code=2)
    conn = db(cfg)
    assert conn.execute("SELECT COUNT(*) FROM timeline_raw").fetchone()[0] == 1
    conn.close()


def test_add_without_a_key_stores_the_text_anyway(cfg, monkeypatch):
    def unavailable(request, use_fallbacks=False):
        raise llm.LLMUnavailable("No Anthropic API key.")

    monkeypatch.setattr(llm, "SEND", unavailable)
    out = run("add", "range 45 min, wedges", "--date", "2026-09-21")
    assert "Added other on 2026-09-21" in out and "Stored without extraction" in out


def test_notebook_without_a_key_explains_it(cfg, tmp_path, monkeypatch):
    def unavailable(request, use_fallbacks=False):
        raise llm.LLMUnavailable("No Anthropic API key.")

    monkeypatch.setattr(llm, "SEND", unavailable)
    pages = pngs(tmp_path / "pages", 1)
    for _ in range(2):
        out = run("notebook", *pages, code=2)
        assert "ANTHROPIC_API_KEY" in out and "Notebook: 1 page(s)" in out
    assert not [p for p in (cfg.data_dir / "raw" / "notebook").rglob("*") if p.is_file()]   # no copies pile up
    assert pages[0].exists()                                            # the original is untouched


def test_review_events_offers_only_carry_over_or_dismiss_for_an_orphaned_event(cfg):
    conn = db(cfg)
    seed_demo(conn)
    eid = conn.execute("SELECT event_id FROM events WHERE status = 'pending' AND event_id NOT IN "
                       "(SELECT event_id FROM event_reviews)").fetchone()[0]
    conn.execute("INSERT INTO event_reviews(event_id, decision, reviewed_at) VALUES (?, 'accepted', '2026-01-01')",
                 (eid,))
    conn.execute("UPDATE events SET status = 'orphaned' WHERE event_id = ?", (eid,))
    conn.commit()
    conn.close()
    out = run("review", "events", input="a\n")
    assert "[d]ismiss" in out and "[a]ccept" not in out and "only be carried over, dismissed or merged" in out
    assert "saved." in run("review", "events", input="d\n")
    assert "No events waiting" in run("review", "events")


def test_review_events_interactive_accept(cfg):
    conn = db(cfg)
    seed_demo(conn)
    conn.close()
    out = run("review", "events", input="a\n")
    assert "1 event(s) to review" in out and "saved." in out
    assert "No events waiting" in run("review", "events")


# ------------------------------------------------------------------ outputs
def test_demo_seed_build_and_clear(cfg):
    assert "synthetic rounds" in run("demo", "seed")
    out = run("build")
    assert "Dashboard written to" in out
    html = (cfg.site_dir / "index.html").read_text()
    assert '"is_demo":true' in html
    custom = Path(cfg.root) / "out" / "d.html"
    run("build", "--out", custom)
    assert custom.exists()
    assert "Public dashboard written to" in run("build", "--public")
    public = (cfg.site_dir / "public" / "index.html").read_text()
    assert '"public":true' in public and str(cfg.data_dir) not in public
    assert "round-to-round spread" in run("analyze", "lessons")
    assert "Removed" in run("demo", "clear")
    conn = db(cfg)
    conn.execute("INSERT INTO rounds(round_id, source, played_on_local, entry_mode) "
                 "VALUES ('real-1', '18b_export', '2026-01-01', 'hole_by_hole')")
    conn.commit()
    conn.close()
    assert "already holds 1 real round" in run("demo", "seed", code=2)


# ------------------------------------------------------------------ privacy
def test_privacy_check_on_explicit_paths(cfg, tmp_path):
    root = Path(cfg.root)
    (root / "data").mkdir(exist_ok=True)
    files = {
        "notes.md": "call me at 617-555-2368 or golfer.person@gmail.com\n",
        "leak.json": '{"mobileNumber": "6175552368", "birthYear": 1980}\n',
        "clean.py": "x = 'support@18birdies.com'  # role address\nprint(555-0100)\n",
        "fixture.py": '"""privacy-check: synthetic"""\nd = {"email": "a@b.co"}\n',
        "data/golf.db": "",
        "shot.PNG": "",
    }
    for name, text in files.items():
        (root / name).write_text(text)
    out = run("privacy-check", *files, code=1)
    assert "notes.md:1: [phone]" in out and "notes.md:1: [email]" in out
    assert "leak.json:1: [pii_key]" in out and "[data_dir]" in out and "shot.PNG: [image]" in out
    assert "clean.py" not in out and "fixture.py" not in out
    assert "clean" in run("privacy-check", "clean.py", "fixture.py")


def test_privacy_check_finds_note_text_from_the_database(cfg):
    conn = db(cfg)
    conn.execute("INSERT INTO source_docs(doc_id, source, text) VALUES ('an-1', 'apple_notes', ?)",
                 ("Golf\nLesson with coach: keep the trail elbow in front of the hip",))
    conn.commit()
    conn.close()
    (Path(cfg.root) / "README.md").write_text(
        "Example:\n  Lesson with coach: keep the trail elbow in front of the hip\n")
    assert "[note_text]" in run("privacy-check", "README.md", code=1)


def test_privacy_check_recurses_into_folders_and_finds_embedded_note_text_and_bare_phones(cfg):
    root = Path(cfg.root)
    conn = db(cfg)
    conn.execute("INSERT INTO source_docs(doc_id, source, text) VALUES ('an-2', 'apple_notes', ?)",
                 ("Golf\n9/8 range 60 min: pump drill again, better",))
    conn.commit()
    conn.close()
    (root / "tests" / "deep").mkdir(parents=True)
    (root / "tests" / "deep" / "leak.py").write_text(
        'x = ["9/8 range 60 min: pump drill again, better"]\nA = "+16178675309"\nB = "6178675309"\n')
    (root / "tests" / "ok.py").write_text("ts = 1726000000\nid = 'a6099e3c61786753'\nlat = 42.3601234\n")
    out = run("privacy-check", "tests", code=1)
    assert "tests/deep/leak.py:1: [note_text]" in out
    assert "tests/deep/leak.py:2: [phone]" in out and "tests/deep/leak.py:3: [phone]" in out
    assert "ok.py" not in out
    assert "No such file or folder" in run("privacy-check", "nope", code=2)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_privacy_check_on_a_folder_honours_gitignore(cfg):
    root = Path(cfg.root)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    (root / ".gitignore").write_text("data/\n")
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "golf.db").write_text("")
    (root / "docs").mkdir()
    (root / "docs" / "a.md").write_text("mail golfer.person@gmail.com\n")
    out = run("privacy-check", ".", code=1)
    assert "docs/a.md:1: [email]" in out and "data/golf.db" not in out


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_privacy_check_scans_staged_blobs(cfg):
    root = Path(cfg.root)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    (root / "ok.txt").write_text("nothing personal\n")
    subprocess.run(["git", "add", "ok.txt"], cwd=root, check=True, env=env)
    assert "1 file(s) clean" in run("privacy-check")
    (root / "ok.txt").write_text('{"paymentMethod": "VISA"}\n')           # working tree differs from the index
    assert "1 file(s) clean" in run("privacy-check")
    subprocess.run(["git", "add", "-f", "ok.txt"], cwd=root, check=True, env=env)
    assert "[pii_key]" in run("privacy-check", "--staged", code=1)
