"""golf.watch: hands-off import of new 18Birdies exports, offline. The "Downloads" folders are temp
folders, the archives are synthetic (tests/fixtures/synthetic_archive.py), launchctl is a recorder and
the LaunchAgents folder is a temp folder: nothing here touches ~/Library or the real Downloads."""
from __future__ import annotations

import json
import os
import plistlib
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import golf.cli as cli
import golf.llm as llm
from golf import watch
from golf.db import connect

runner = CliRunner()
MTIME = 1_790_000_000            # 2026-09-21 10:13 in New York


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def no_api(request, use_fallbacks=False):
        raise AssertionError("unexpected Claude API call")

    monkeypatch.setattr(llm, "SEND", no_api)
    monkeypatch.setattr(watch, "STABLE_WAIT", 0)          # the 2 s download-settling pause, where not under test


@pytest.fixture
def downloads(cfg, tmp_path) -> Path:
    """A temp 'Downloads' folder, configured as the only watched folder (plus one that does not exist)."""
    folder = tmp_path / "Downloads"
    folder.mkdir()
    cfg_file = Path(cfg.root) / "config.toml"
    cfg_file.write_text(cfg_file.read_text() + f'\n[watch]\ndirs = ["{folder}", "{tmp_path / "iCloud"}"]\n'
                                               'interval_seconds = 30\n')
    return folder


@pytest.fixture
def conn(cfg):
    c = connect(cfg.db_path)
    yield c
    c.close()


def archive(path: Path, *, n_rounds: int = 26, seed: int = 7, mtime: int = MTIME) -> Path:
    from fixtures.synthetic_archive import make_archive, write_archive

    write_archive(path, make_archive(n_rounds=n_rounds, seed=seed))
    os.utime(path, (mtime, mtime))
    return path


def scan(conn, cfg, **kw):
    kw.setdefault("stable_wait", 0)
    kw.setdefault("build", False)
    return watch.scan_once(conn, cfg, **kw)


def stored(cfg) -> list[str]:
    folder = cfg.data_dir / "raw" / "18birdies"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


class Launchctl:
    def __init__(self, fail: str | None = None, loaded: bool = True):
        self.calls: list[list[str]] = []
        self.fail, self.loaded = fail, loaded

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "bootstrap" and self.fail:
            return SimpleNamespace(returncode=5, stdout="", stderr=self.fail)
        if args[0] == "print":
            return SimpleNamespace(returncode=0 if self.loaded else 113,
                                   stdout="\tstate = not running\n\tlast exit code = 0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


# ------------------------------------------------------------------ settings + finding files
def test_settings_default_to_downloads_and_icloud_downloads(cfg):
    st = watch.settings(cfg)
    assert st.dirs == (Path.home() / "Downloads",
                       Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Downloads")
    assert st.pattern == "18Birdies_archive*.json" and st.interval == watch.DEFAULT_INTERVAL


def test_project_config_toml_has_the_watch_and_publish_sections():
    from golf.config import PROJECT_ROOT, load_config

    real = load_config(PROJECT_ROOT)
    assert watch.read_section(real, "watch")["pattern"] == "18Birdies_archive*.json"
    pub = watch.read_section(real, "publish")
    assert pub["branch"] == "gh-pages" and pub["site_url"] == "https://sharkweekshane.github.io/golf-analytics/"


def test_find_downloads_matches_browser_variants_and_skips_partial_ones(tmp_path):
    d = tmp_path / "dl"
    d.mkdir()
    for name in ("18Birdies_archive.json", "18Birdies_archive (1).json", "18birdies_archive-2.json",
                 "18Birdies_archive (2).json", "18Birdies_archive (2).json.crdownload", "other.json",
                 "18Birdies_archive.txt"):
        (d / name).write_text("{}")
    (d / ".18Birdies_archive (3).json.icloud").write_text("")
    (d / "18Birdies_archive_folder.json").mkdir()
    found, pending, denied = watch.find_downloads([d, tmp_path / "missing"])
    assert sorted(p.name for p in found) == ["18Birdies_archive (1).json", "18Birdies_archive.json",
                                             "18birdies_archive-2.json"]
    assert {(x["file"], x["reason"][:5]) for x in pending} == {("18Birdies_archive (2).json", "still"),
                                                                ("18Birdies_archive (3).json", "in iC")}
    assert denied == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores folder permissions")
def test_a_folder_we_may_not_read_is_reported_not_fatal(cfg, conn, downloads):
    os.chmod(downloads, 0)
    try:
        s = scan(conn, cfg)
    finally:
        os.chmod(downloads, 0o755)
    assert s["denied_dirs"] == [str(downloads)] and not s["errors"]
    assert "No permission to read" in watch.format_scan(s)


# ------------------------------------------------------------------ scan_once
def test_a_new_download_is_copied_dated_imported_and_left_in_place(cfg, conn, downloads):
    from golf.config import PROJECT_ROOT

    shutil.copyfile(PROJECT_ROOT / "courses.example.yaml", Path(cfg.root) / "courses.yaml")   # synthetic club
    src = archive(downloads / "18Birdies_archive.json")
    before = src.read_bytes()
    s = scan(conn, cfg, build=True)
    assert [x["file"] for x in s["imported"]] == ["18Birdies_archive.json"] and not s["errors"]
    assert stored(cfg) == ["18Birdies_archive_20260921.json"]
    assert src.exists() and src.read_bytes() == before and os.stat(src).st_mtime == MTIME   # never moved or touched
    assert conn.execute("SELECT COUNT(*) FROM rounds WHERE source = '18b_export'").fetchone()[0] > 20
    assert conn.execute("SELECT COUNT(*) FROM handicap_history").fetchone()[0] > 0     # courses + HI ran
    assert "Courses:" in s["imported"][0]["text"] and "Handicap (unofficial" in s["imported"][0]["text"]
    assert s["built"] and Path(s["built"]).exists()                                  # the local dashboard
    last = json.loads(conn.execute("SELECT value FROM meta WHERE key = 'watch_last_scan'").fetchone()[0])
    assert last["imported"] == ["18Birdies_archive.json"] and last["source"] == "cli"
    log = (cfg.data_dir / "logs" / "watch.log").read_text()
    assert "Imported 18Birdies_archive.json" in log and "fake.golfer" not in log

    again = scan(conn, cfg)
    assert again["imported"] == [] and again["already"] == ["18Birdies_archive.json"]
    assert "No new 18Birdies export (1 already imported)" in watch.format_scan(again)
    shutil.copy2(src, downloads / "18Birdies_archive (1).json")                     # the same file downloaded twice
    third = scan(conn, cfg)
    assert third["imported"] == [] and len(third["already"]) == 2 and stored(cfg) == ["18Birdies_archive_20260921.json"]


def test_an_older_download_stored_earlier_by_hand_is_reused_not_copied_again(cfg, conn, downloads):
    src = archive(downloads / "18Birdies_archive.json")
    (cfg.data_dir / "raw" / "18birdies").mkdir(parents=True)
    shutil.copy2(src, cfg.data_dir / "raw" / "18birdies" / "18Birdies_archive_20260920.json")
    s = scan(conn, cfg)
    assert len(s["imported"]) == 1 and stored(cfg) == ["18Birdies_archive_20260920.json"]


def test_a_different_file_never_overwrites_a_stored_snapshot(cfg, conn, downloads):
    archive(downloads / "18Birdies_archive.json")
    scan(conn, cfg)
    archive(downloads / "18Birdies_archive (1).json", n_rounds=27, seed=8)          # same day, new content
    s = scan(conn, cfg)
    assert [x["file"] for x in s["imported"]] == ["18Birdies_archive (1).json"]
    assert stored(cfg) == ["18Birdies_archive_20260921-2.json", "18Birdies_archive_20260921.json"]


def test_a_download_still_in_progress_waits_for_the_next_scan(cfg, conn, downloads):
    src = downloads / "18Birdies_archive.json"
    src.write_text('{"myData": {"activityData": {"rounds": [')                      # half-written JSON

    def grows(_seconds):
        with src.open("a") as f:
            f.write(" ")

    s = scan(conn, cfg, stable_wait=2, sleep=grows)
    assert s["pending"] == [{"file": "18Birdies_archive.json", "reason": "still downloading (size changing)"}]
    s = scan(conn, cfg)
    assert s["pending"][0]["reason"].startswith("not complete JSON") and not s["errors"] and stored(cfg) == []
    archive(src)
    s = scan(conn, cfg)
    assert len(s["imported"]) == 1 and s["pending"] == []


def test_valid_json_that_is_not_an_export_is_reported_once(cfg, conn, downloads):
    (downloads / "18Birdies_archive.json").write_text('{"something": "else"}')
    s = scan(conn, cfg)
    assert "not an 18Birdies export" in s["errors"][0]["error"] and stored(cfg) == []
    s = scan(conn, cfg)
    assert s["errors"] == [] and s["ignored"] == ["18Birdies_archive.json"]


def test_several_new_downloads_are_imported_oldest_first(cfg, conn, downloads):
    archive(downloads / "18Birdies_archive (1).json", n_rounds=27, seed=8, mtime=MTIME + 86400)
    archive(downloads / "18Birdies_archive.json", mtime=MTIME)
    s = scan(conn, cfg)
    assert [x["file"] for x in s["imported"]] == ["18Birdies_archive.json", "18Birdies_archive (1).json"]
    assert not any(x["stale"] for x in s["imported"])


def test_a_second_scanner_backs_off_while_one_is_running(cfg, conn, downloads):
    archive(downloads / "18Birdies_archive.json")
    with watch._lock(cfg) as got:
        assert got
        s = scan(conn, cfg)
    assert s["busy"] and s["imported"] == [] and "Another scan" in watch.format_scan(s)


def test_lock_file_is_outside_the_project_and_open_failures_mean_busy(cfg, conn, downloads, monkeypatch, tmp_path):
    monkeypatch.setenv("GOLF_LOCK_DIR", str(tmp_path / "locks"))
    with watch._lock(cfg) as got:
        assert got and (tmp_path / "locks" / "watch.lock").exists()
    archive(downloads / "18Birdies_archive.json")
    real_open = open

    def icloud_refuses(path, *a, **kw):                     # what iCloud did on 2026-10-02
        if str(path).endswith("watch.lock"):
            raise OSError(11, "Resource deadlock avoided")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", icloud_refuses)
    s = scan(conn, cfg)
    monkeypatch.setattr("builtins.open", real_open)
    assert s["busy"] and s["imported"] == []


def test_publish_runs_after_an_import_only_when_enabled_and_auto(cfg, conn, downloads, monkeypatch):
    from golf import publish

    calls = []
    monkeypatch.setattr(publish, "publish", lambda conn, cfg, runner=None: calls.append(1) or
                        {"action": "pushed", "site_url": "https://example.test/", "commit": "abc"})
    archive(downloads / "18Birdies_archive.json")
    scan(conn, cfg)
    assert calls == []                                                       # [publish] enabled defaults to false
    cfg_file = Path(cfg.root) / "config.toml"
    cfg_file.write_text(cfg_file.read_text() + "\n[publish]\nenabled = true\n")
    assert scan(conn, cfg)["published"] is None and calls == []             # nothing new: nothing to publish
    archive(downloads / "18Birdies_archive (1).json", n_rounds=27, seed=8, mtime=MTIME + 86400)
    s = scan(conn, cfg)
    assert calls == [1] and "Published to https://example.test/" in watch.format_scan(s)

    def broken(conn, cfg, runner=None):
        raise publish.PublishError("git push failed: no network")

    monkeypatch.setattr(publish, "publish", broken)
    archive(downloads / "18Birdies_archive (2).json", n_rounds=28, seed=9, mtime=MTIME + 2 * 86400)
    s = scan(conn, cfg)
    assert len(s["imported"]) == 1 and s["publish_error"] == "git push failed: no network"


def test_run_forever_reports_the_first_scan_and_new_imports(cfg, downloads):
    out, naps = [], []
    connect(cfg.db_path).close()

    def nap(seconds):
        naps.append(seconds)
        archive(downloads / "18Birdies_archive.json")

    watch.run_forever(lambda: connect(cfg.db_path), cfg, echo=out.append, sleep=nap, max_scans=2)
    assert naps == [30] and "No new 18Birdies export" in out[0] and "Imported 18Birdies_archive.json" in out[1]


# ------------------------------------------------------------------ LaunchAgent
def test_install_writes_the_plist_and_loads_it(cfg, downloads, tmp_path):
    lc = Launchctl()
    agents = tmp_path / "LaunchAgents"
    r = watch.install(cfg, agents_dir=agents, launchctl=lc, python="/venvs/golf/bin/python")
    path = agents / "com.golfanalytics.watch.plist"
    assert r["plist"] == str(path)
    with path.open("rb") as f:
        p = plistlib.load(f)
    assert p["Label"] == "com.golfanalytics.watch"
    assert p["ProgramArguments"] == ["/venvs/golf/bin/python", "-m", "golf.cli", "watch", "--once"]
    assert p["WatchPaths"] == [str(downloads)]                        # only folders that exist
    assert p["StartInterval"] == 1800 and p["RunAtLoad"] is True
    assert p["EnvironmentVariables"]["GOLF_ROOT"] == str(Path(cfg.root).resolve())
    logs = (cfg.data_dir / "logs").resolve()
    assert p["StandardOutPath"] == str(logs / "watch.out.log") and p["StandardErrorPath"] == str(logs / "watch.err.log")
    domain = f"gui/{os.getuid()}"
    assert lc.calls == [["bootout", f"{domain}/com.golfanalytics.watch"], ["bootstrap", domain, str(path)]]

    st = watch.agent_status(cfg, agents_dir=agents, launchctl=lc)
    assert st["installed"] and st["loaded"] and st["last_exit"] == "0" and st["watch_paths"] == [str(downloads)]

    u = watch.uninstall(cfg, agents_dir=agents, launchctl=lc)
    assert u["removed"] and not path.exists() and lc.calls[-1] == ["bootout", f"{domain}/com.golfanalytics.watch"]
    assert not watch.agent_status(cfg, agents_dir=agents, launchctl=Launchctl(loaded=False))["installed"]


def test_install_failure_explains_itself(cfg, tmp_path):
    with pytest.raises(watch.WatchError, match="launchctl bootstrap failed"):
        watch.install(cfg, agents_dir=tmp_path / "LA",
                      launchctl=Launchctl(fail="Bootstrap failed: 5: Input/output error"))


def test_agent_python_is_the_real_venv_not_the_icloud_symlink(cfg, monkeypatch, tmp_path):
    real = tmp_path / "venvs" / "golf"
    (real / "bin").mkdir(parents=True)
    link = tmp_path / "project" / ".venv"
    link.parent.mkdir()
    link.symlink_to(real)
    monkeypatch.setattr("sys.prefix", str(link))
    monkeypatch.setattr("sys.base_prefix", "/usr/local")
    assert watch.agent_python(cfg) == str(real.resolve() / "bin" / "python")


# ------------------------------------------------------------------ CLI
def run(*args, code: int = 0) -> str:
    result = runner.invoke(cli.app, [str(a) for a in args])
    assert result.exit_code == code, f"exit {result.exit_code}\n{result.output}\n{result.exception!r}"
    return result.output


def test_cli_watch_once_install_status_uninstall(cfg, downloads, tmp_path, monkeypatch):
    lc = Launchctl()
    monkeypatch.setattr(cli, "AGENTS_DIR", tmp_path / "LaunchAgents")
    monkeypatch.setattr(cli, "LAUNCHCTL", lc)
    archive(downloads / "18Birdies_archive.json")
    out = run("watch", "--once")
    assert "Imported 18Birdies_archive.json -> data/raw/18birdies/18Birdies_archive_20260921.json" in out
    assert "No new 18Birdies export" in run("watch", "--once")
    assert "not installed" in run("watch", "status")

    out = run("watch", "install")
    assert "LaunchAgent installed" in out and "Full Disk Access" in out
    assert (tmp_path / "LaunchAgents" / "com.golfanalytics.watch.plist").exists()
    status = run("watch", "status")
    assert "installed" in status and "loaded" in status and str(downloads) in status and "Last scan:" in status
    assert "LaunchAgent installed" in run("status")
    assert "removed" in run("watch", "uninstall")
    assert not (tmp_path / "LaunchAgents" / "com.golfanalytics.watch.plist").exists()


def test_cli_watch_once_exits_nonzero_on_errors(cfg, downloads):
    (downloads / "18Birdies_archive.json").write_text('{"not": "an export"}')
    assert "not an 18Birdies export" in run("watch", "--once", code=1)


def test_a_lingering_problem_is_logged_once_not_every_scan(cfg, conn, downloads):
    (downloads / "18Birdies_archive.json").write_text("{}")
    (downloads / "18Birdies_archive.json.crdownload").write_text("")      # a download the browser abandoned
    first = scan(conn, cfg)
    assert first["news"] and first["pending"]
    for _ in range(3):
        assert not scan(conn, cfg)["news"]
    log = (cfg.data_dir / "logs" / "watch.log").read_text()
    assert log.count("Waiting: 18Birdies_archive.json") == 1
    (downloads / "18Birdies_archive.json.crdownload").unlink()
    archive(downloads / "18Birdies_archive.json")
    assert scan(conn, cfg)["news"]


# ------------------------------------------------------------------ unattended imports must be harmless
def _live_export_rounds(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM rounds WHERE source = '18b_export' AND deleted_in_source = 0"
                        ).fetchone()[0]


def _enable_publish(cfg, monkeypatch) -> list:
    from golf import publish

    calls: list = []
    monkeypatch.setattr(publish, "publish", lambda conn, cfg, runner=None: calls.append(1) or
                        {"action": "pushed", "site_url": "https://example.test/", "commit": "abc"})
    cfg_file = Path(cfg.root) / "config.toml"
    cfg_file.write_text(cfg_file.read_text() + "\n[publish]\nenabled = true\n")
    return calls


def test_another_accounts_export_is_held_never_copied_imported_or_published(cfg, conn, downloads, monkeypatch):
    from fixtures.synthetic_archive import make_archive, rounds_of, write_archive

    calls = _enable_publish(cfg, monkeypatch)
    archive(downloads / "18Birdies_archive.json")
    assert len(scan(conn, cfg)["imported"]) == 1 and calls == [1]
    live = _live_export_rounds(conn)
    pinned = conn.execute("SELECT value FROM meta WHERE key = 'export_account_fp'").fetchone()[0]
    assert pinned and "fake-user-id" not in pinned                     # a fingerprint, never the id

    other = make_archive(seed=3)                                        # someone else's download on this Mac
    other["myData"]["accountData"]["userId"] = "another-account-77"
    other["myData"]["activityData"]["rounds"] = [dict(rounds_of(other)[0], id="stranger-round-1")]
    other["myData"]["clubData"]["playedClubs"][0]["name"] = "VISIT evil.example FOR FREE GOLF"
    src = write_archive(downloads / "18Birdies_archive (1).json", other)
    os.utime(src, (MTIME + 86400, MTIME + 86400))
    s = scan(conn, cfg, build=True)
    assert s["imported"] == [] and s["built"] is None and calls == [1]
    assert "different 18Birdies account" in s["held"][0]["reason"] and "--new-account" in s["held"][0]["reason"]
    assert _live_export_rounds(conn) == live and stored(cfg) == ["18Birdies_archive_20260921.json"]
    assert conn.execute("SELECT COUNT(*) FROM clubs WHERE name LIKE '%evil%'").fetchone()[0] == 0
    assert "Not imported automatically" in watch.format_scan(s) and s["news"]
    again = scan(conn, cfg)
    assert again["held"] and not again["news"] and again["imported"] == []   # reported, not re-logged each scan

    out = run("import-18b", src, code=1)                                # by hand, it still needs --new-account
    assert "different 18Birdies account" in out and _live_export_rounds(conn) == live


def test_an_export_without_an_account_id_is_held(cfg, conn, downloads):
    from fixtures.synthetic_archive import make_archive, write_archive

    archive(downloads / "18Birdies_archive.json")
    scan(conn, cfg)
    data = make_archive(n_rounds=27, seed=8)
    del data["myData"]["accountData"]
    write_archive(downloads / "18Birdies_archive (1).json", data)
    s = scan(conn, cfg)
    assert s["imported"] == [] and "no 18Birdies account id" in s["held"][0]["reason"]


def test_a_snapshot_missing_rounds_waits_for_confirmation_and_is_never_auto_published(cfg, conn, downloads,
                                                                                      monkeypatch):
    from fixtures.synthetic_archive import make_archive, without_round, write_archive

    calls = _enable_publish(cfg, monkeypatch)
    archive(downloads / "18Birdies_archive.json")
    scan(conn, cfg)
    live = _live_export_rounds(conn)
    src = write_archive(downloads / "18Birdies_archive (1).json", without_round(make_archive(), "syn-001"))
    os.utime(src, (MTIME + 86400, MTIME + 86400))
    s = scan(conn, cfg)
    assert s["imported"] == [] and _live_export_rounds(conn) == live and calls == [1]
    assert f"mark 1 of your {live} rounds as deleted" in s["held"][0]["reason"]
    assert f"golf import-18b '{src}'" in s["held"][0]["reason"]
    out = run("import-18b", src)                                        # Shane confirms by importing by hand
    assert "deleted in 18Birdies 1" in out and _live_export_rounds(conn) == live - 1
    after = scan(conn, cfg)
    assert after["held"] == [] and "18Birdies_archive (1).json" in after["already"] and calls == [1]


def test_no_unattended_publish_after_rounds_were_marked_deleted(cfg, conn, downloads, monkeypatch):
    """Belt and braces: even if a deleting import got through, the watcher would not publish it."""
    from golf.ingest import birdies_export

    calls = _enable_publish(cfg, monkeypatch)
    archive(downloads / "18Birdies_archive.json")
    real = cli.import_18b

    def deleting(conn, cfg, path, **kw):
        out = real(conn, cfg, path, **kw)
        out["import"]["deleted_in_source"] = 2
        return out

    monkeypatch.setattr(cli, "import_18b", deleting)
    s = scan(conn, cfg)
    assert len(s["imported"]) == 1 and calls == [] and "2 round(s) were marked deleted" in s["publish_skipped"]
    assert "Not published automatically" in watch.format_scan(s)
    assert birdies_export.ACCOUNT_KEY == "export_account_fp"


def test_under_launchd_a_quiet_scan_prints_nothing(cfg, downloads, monkeypatch):
    """launchd appends stdout to watch.out.log on every run; 'nothing new' must not grow it forever."""
    monkeypatch.setenv("XPC_SERVICE_NAME", watch.LABEL)
    archive(downloads / "18Birdies_archive.json")
    assert "Imported 18Birdies_archive.json" in run("watch", "--once")
    assert run("watch", "--once").strip() == ""
    monkeypatch.delenv("XPC_SERVICE_NAME")
    assert "No new 18Birdies export" in run("watch", "--once")
