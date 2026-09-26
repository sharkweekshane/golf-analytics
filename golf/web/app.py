"""Local web app: live dashboard, screenshot / notebook inbox, review pages (RESEARCH_PLAN §3 "v2 review").

It runs on 127.0.0.1 only and every page is built from golf.db on request. Requests whose Host header
is not a loopback name are refused (a DNS-rebinding page could otherwise drive the app from another
site), and so are POSTs whose Origin is not this very app (another localhost port is another origin).
Extraction runs in a background thread, one job at a time, with its own database connection; the same
pipeline functions as the CLI (golf.cli) do the work. While `golf serve` runs, a watcher thread also
looks for new 18Birdies exports in Downloads every minute (golf.watch.scan_once).
"""
from __future__ import annotations

import io
import re
import shutil
import threading
import traceback
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from golf import cli as ops
from golf import config as config_mod
from golf import llm
from golf import watch as watch_mod
from golf.config import Config
from golf.notes import NOTE_TEMPLATE_HELP, NotesError, pending_events, timeline

HERE = Path(__file__).parent
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
SERVABLE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif"}
CONVERT_FOR_BROWSER = {".heic", ".heif"}
FILE_HEADERS = {"Cache-Control": "no-store", "Cross-Origin-Resource-Policy": "same-origin"}
NAV = [("Dashboard", "/"), ("Inbox", "/inbox"), ("Review", "/review"), ("Timeline", "/timeline"),
       ("18Birdies", "/status#birdies"), ("Status", "/status")]
EXPORT_STALE_DAYS = config_mod.EXPORT_STALE_DAYS     # one threshold for the pill, the tile and golf status
EXPORT_STEPS = [
    "Sign in (email + password, or phone + SMS code). If you only ever used Apple or Google sign-in, first add "
    "an email or phone in the 18Birdies app's Settings.",
    "Click Request My Data. The browser saves 18Birdies_archive.json into Downloads (on the iPhone: Files > "
    "iCloud Drive > Downloads).",
    "That's it: this app picks it up automatically, imports it, recomputes the handicap and refreshes the "
    "dashboard (and the public site, when publishing is on).",
]
ROUND_FIELDS = ("date_iso", "course_text", "tee_text", "course_par")
EVENT_TYPES = ("lesson", "practice", "on_course", "equipment_change", "fitting", "injury", "fitness", "goal",
               "swing_thought", "milestone", "other")

CAPTURE_CHECKLIST = [
    ("Full Scorecard, Stats view", "Round Summary > View Full Scorecard > Stats. Landscape if every hole fits, "
     "otherwise portrait swipes that overlap by at least one hole. This is where putts, fairways, GIR, "
     "penalties, chips and sand come from."),
    ("Full Scorecard, Scores view", "Same card, Scores toggle, showing the Par and Handicap rows. Needed once "
     "per course and tee (par and stroke index), and for any round not in your 18Birdies export yet."),
    ("Top of the Round Summary", "Only if the Stats view header does not show the date and course."),
    ("Benchmarks (optional)", "Round Summary > More Stats > Benchmarks: printed totals the extractor uses as a "
     "cross-check. Skip it when the round is already in your export."),
]
CAPTURE_TIPS = ("Don't crop or zoom; HEIC screenshots are fine. Upload one round at a time. The date you enter "
                "here beats the date read off the screens; the course hint helps matching.")


# ================================================================ background jobs
class Jobs:
    """In-memory job list, run one at a time in a worker thread (SQLite prefers a single writer).
    Jobs are for display only: every result also lands in golf.db, so a restart loses nothing."""

    def __init__(self, background: bool = True):
        self.background = background
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    def submit(self, kind: str, label: str, fn: Callable[[], dict[str, Any]], *, cfg: Config) -> str:
        job = {"id": uuid.uuid4().hex[:10], "kind": kind, "label": label, "status": "queued",
               "started_at": datetime.now().strftime("%H:%M:%S"), "finished_at": None, "text": "", "links": [],
               "error": None, "help": None}
        with self._lock:
            self._items[job["id"]] = job

        def run() -> None:
            with self._run_lock:
                job["status"] = "running"
                try:
                    ops.api_key_set(cfg)          # picks up a key added to .env since the server started
                    out = fn()
                    job.update(status="done", text=out.get("text", ""), links=out.get("links", []),
                               help=out.get("help"))
                except llm.LLMUnavailable as e:   # also llm.LLMTransient: network, overload, unknown model
                    job.update(status="error", error=ops.llm_headline(e), help=ops.llm_help(cfg, e))
                except (NotesError, ValueError, FileNotFoundError) as e:   # messages written for Shane
                    job.update(status="error", error=str(e))
                except Exception as e:   # shown on the page; the traceback goes to the server log
                    traceback.print_exc()
                    job.update(status="error", error=f"{type(e).__name__}: {e}")
                finally:
                    job["finished_at"] = datetime.now().strftime("%H:%M:%S")

        if self.background:
            t = threading.Thread(target=run, name=f"golf-job-{job['id']}", daemon=True)
            self._threads.append(t)
            t.start()
        else:
            run()
        return job["id"]

    def record(self, kind: str, label: str, text: str, links: list | None = None, error: str | None = None) -> None:
        """A finished job that did not go through submit() (the watcher's automatic import)."""
        now = datetime.now().strftime("%H:%M:%S")
        job = {"id": uuid.uuid4().hex[:10], "kind": kind, "label": label, "status": "error" if error else "done",
               "started_at": now, "finished_at": now, "text": text, "links": links or [], "error": error,
               "help": None}
        with self._lock:
            self._items[job["id"]] = job

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self._items.get(job_id)

    def recent(self, n: int = 8) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._items.values())[-n:][::-1]

    def wait(self, timeout: float | None = None) -> None:
        for t in list(self._threads):
            t.join(timeout)


# ================================================================ helpers
def _hostname(netloc: str) -> str:
    netloc = (netloc or "").strip().lower()
    if netloc.startswith("["):
        return netloc[1:].split("]", 1)[0]
    return netloc.rsplit(":", 1)[0] if netloc.count(":") == 1 else netloc


def _port(netloc: str, default: int = 80) -> int:
    netloc = (netloc or "").strip()
    tail = netloc.rsplit("]", 1)[-1] if netloc.startswith("[") else netloc
    if tail.count(":") == 1:
        try:
            return int(tail.rsplit(":", 1)[1])
        except ValueError:
            return -1
    return default


def same_origin(origin: str, host: str) -> bool:
    """An Origin header is this app itself: http, a loopback name, and the same port as the Host header
    (localhost and 127.0.0.1 count as the same machine). Another local dev server (a Jupyter page on
    :8888, say) is a different origin even though it is also localhost."""
    parts = urlsplit(origin or "")
    if parts.scheme != "http" or _hostname(parts.netloc) not in ALLOWED_HOSTS:
        return False
    return _hostname(host) in ALLOWED_HOSTS and _port(parts.netloc) == _port(host)


def _safe_name(name: str | None, default: str = "upload") -> str:
    base = Path(name or "").name
    stem, suffix = Path(base).stem, Path(base).suffix.lower()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")[:80] or default
    return stem + re.sub(r"[^a-z0-9.]", "", suffix)


def _real_uploads(items: list[Any] | None) -> list[Any]:
    """Uploaded files only: an empty file input arrives as '' (or a nameless part); drop those."""
    return [f for f in items or [] if hasattr(f, "filename") and hasattr(f, "file") and (f.filename or "").strip()]


def _unreadable_uploads(files: list[Any]) -> list[str]:
    """Names of uploaded 'images' Pillow cannot open (empty, truncated, text named .png)."""
    from PIL import Image
    import pillow_heif

    pillow_heif.register_heif_opener()
    bad = []
    for f in files:
        try:
            f.file.seek(0)
            head = f.file.read(1)
            f.file.seek(0)
            if not head:
                raise ValueError("empty file")
            with Image.open(f.file) as im:
                im.verify()
        except Exception:
            bad.append(f.filename)
        finally:
            f.file.seek(0)
    return bad


def _save_uploads(files: list[Any], folder: Path, allowed: set[str]) -> list[Path]:
    bad = [f.filename for f in files if Path(f.filename or "").suffix.lower() not in allowed]
    if bad:
        raise ValueError(f"Not an accepted file type: {', '.join(map(str, bad))} "
                         f"(accepted: {', '.join(sorted(allowed))}).")
    if allowed is ops.IMAGE_EXTS or allowed == ops.IMAGE_EXTS:
        broken = _unreadable_uploads(files)
        if broken:
            raise ValueError(f"Not a readable image: {', '.join(map(str, broken))}. Nothing was saved; export "
                             "the screenshot again from Photos and upload it.")
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        dest = ops._unique(folder / _safe_name(f.filename))
        with dest.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(dest)
    return saved


def _redirect(path: str, *, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    path, hash_, fragment = path.partition("#")
    q = []
    if msg:
        q.append("msg=" + quote(msg))
    if err:
        q.append("err=" + quote(err))
    return RedirectResponse(path + (("?" + "&".join(q)) if q else "") + hash_ + fragment, status_code=303)


def _blank(v: Any) -> str:
    return "" if v is None or v == -1 else str(v)


def _flag_class(flags: list[dict[str, Any]]) -> str:
    sev = {f.get("severity") for f in flags}
    return "cell-E" if "E" in sev else "cell-W" if "W" in sev else ""


# ================================================================ app
class Watcher:
    """golf serve's background scan for new 18Birdies exports (golf.watch.scan_once), every `every`
    seconds. It takes the job lock, so it never writes while an extraction job does; a scan that imported
    something (or failed) shows up in the Recent jobs list."""

    def __init__(self, cfg: Config, jobs: Jobs, every: float, *, first_after: float = 5.0,
                 publish_runner: Any = None):
        self.cfg, self.jobs, self.every, self.first_after = cfg, jobs, every, first_after
        self.publish_runner = publish_runner
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="golf-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def scan(self) -> dict[str, Any]:
        with self.jobs._run_lock, ops.open_db(self.cfg) as conn:
            s = watch_mod.scan_once(conn, self.cfg, source="serve", publish_runner=self.publish_runner)
        if s["imported"] or (s.get("news") and (s["errors"] or s.get("held") or s.get("publish_error"))):
            self.jobs.record("watch", "18Birdies export found in Downloads", watch_mod.format_scan(s),
                             links=[("Dashboard", "/"), ("18Birdies", "/status#birdies")],
                             error=(s["errors"][0]["error"] if s["errors"] and not s["imported"] else None))
        return s

    def _loop(self) -> None:
        wait = self.first_after
        while not self._stop.wait(wait):
            try:
                self.scan()
            except Exception:           # the watcher must outlive any one bad scan
                traceback.print_exc()
            wait = self.every


def create_app(cfg: Config | None = None, *, background: bool = True, notes_runner: Any = None,
               watch_every: float | None = watch_mod.SERVE_INTERVAL, agents_dir: Path | None = None,
               publish_runner: Any = None) -> FastAPI:
    """The app for `golf serve`. Tests pass background=False (jobs run inline, no watcher thread), a fake
    Notes runner, a temporary LaunchAgents folder and a fake git runner."""
    cfg = cfg or ops.get_cfg()
    jobs = Jobs(background=background)
    watcher = Watcher(cfg, jobs, watch_every or watch_mod.SERVE_INTERVAL, publish_runner=publish_runner)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if background and watch_every:
            watcher.start()
        try:
            yield
        finally:
            watcher.stop()

    app = FastAPI(title="Golf", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.jobs = jobs
    app.state.watcher = watcher
    app.state.notes_runner = notes_runner
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["localtime"] = lambda iso: localtime(iso, cfg.timezone)
    templates.env.filters["plural"] = plural
    templates.env.filters["source_label"] = lambda key: SOURCE_LABELS.get(key, str(key).replace("_", " "))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "")
        if _hostname(host) not in ALLOWED_HOSTS:
            return PlainTextResponse("This app only answers on 127.0.0.1 / localhost.", status_code=403)
        if request.url.path.startswith("/files/") and request.headers.get("sec-fetch-site") in ("cross-site",
                                                                                              "same-site"):
            # <img src="http://127.0.0.1:8765/files/..."> on another site could show a screenshot or probe
            # which files exist; only this app's own pages may load them.
            return PlainTextResponse("Cross-origin request refused.", status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site")
            if (origin is not None and not same_origin(origin, host)) or fetch_site not in (None, "same-origin",
                                                                                           "none"):
                return PlainTextResponse("Cross-origin request refused.", status_code=403)
        return await call_next(request)

    @contextmanager
    def db() -> Iterator[Any]:
        with ops.open_db(cfg) as conn:
            yield conn

    def counts(conn) -> dict[str, Any]:
        age = export_age_days(conn, cfg)
        return {"rounds": conn.execute("SELECT COUNT(*) FROM extractions WHERE status IN "
                                       "('extracted','needs_review')").fetchone()[0],
                "events": len(pending_events(conn)), "export_age": age,
                "export_stale": age is None or age > EXPORT_STALE_DAYS}

    def page(request: Request, name: str, active: str, **ctx: Any) -> HTMLResponse:
        with db() as conn:
            c = counts(conn)
        return templates.TemplateResponse(request, name, {
            "nav": NAV, "active": active, "counts": c, "key_set": ops.api_key_set(cfg),
            "msg": request.query_params.get("msg"), "err": request.query_params.get("err"),
            "jobs": app.state.jobs.recent(), **ctx})

    # ------------------------------------------------------------ dashboard
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        from golf.dashboard import dashboard_data, render_dashboard

        with db() as conn:
            html = render_dashboard(dashboard_data(conn, cfg))
            c = counts(conn)
        nav = templates.get_template("_nav.html").render(nav=NAV, active="/", counts=c)
        style = '<link rel="stylesheet" href="/static/nav.css">'
        html = html.replace("</head>", style + "\n</head>", 1).replace("<body>", "<body>\n" + nav, 1)
        return HTMLResponse(html)

    # ------------------------------------------------------------ inbox
    @app.get("/inbox", response_class=HTMLResponse)
    def inbox_page(request: Request) -> HTMLResponse:
        with db() as conn:
            clubs = [dict(r) for r in conn.execute(
                "SELECT c.club_id, c.name, COUNT(r.round_id) AS n FROM clubs c LEFT JOIN rounds r"
                " ON r.club_id = c.club_id GROUP BY c.club_id ORDER BY n DESC, c.name")]
            waiting = ops.status_info(conn, cfg)["inbox"]
        return page(request, "inbox.html", "/inbox", clubs=clubs, waiting=waiting, checklist=CAPTURE_CHECKLIST,
                    tips=CAPTURE_TIPS, note_help=NOTE_TEMPLATE_HELP, event_types=EVENT_TYPES,
                    today=datetime.now(ZoneInfo(cfg.timezone)).date().isoformat(),
                    export_dir=_short_dir(cfg, cfg.data_dir / ops.EXPORT_DIR))

    @app.post("/inbox")
    async def inbox_post(request: Request) -> RedirectResponse:
        form = await request.form()
        return await run_in_threadpool(_inbox_action, cfg, app.state.jobs, form)

    @app.post("/import")
    async def import_post(request: Request) -> RedirectResponse:
        form = await request.form()
        return await run_in_threadpool(_import_action, cfg, app.state.jobs, form)

    @app.post("/add")
    async def add_post(request: Request) -> RedirectResponse:
        form = await request.form()
        return await run_in_threadpool(_add_action, cfg, form)

    @app.post("/notes/sync")
    def notes_sync_post(dry_run: str = Form("")) -> RedirectResponse:
        runner = app.state.notes_runner
        app.state.jobs.submit("notes", "Apple Notes sync" + (" (dry run)" if dry_run else ""),
                              lambda: _notes_job(cfg, runner, bool(dry_run)), cfg=cfg)
        return _redirect("/inbox", msg="Syncing Apple Notes (the first time, macOS asks to allow access).")

    @app.get("/jobs/{job_id}")
    def job_status(job_id: str) -> JSONResponse:
        job = app.state.jobs.get(job_id)
        return JSONResponse(job or {"error": "unknown job"}, status_code=200 if job else 404)

    # ------------------------------------------------------------ review
    @app.get("/review", response_class=HTMLResponse)
    def review_index(request: Request) -> HTMLResponse:
        from golf.extract.rounds import pending_round_reviews

        with db() as conn:
            rounds_q = pending_round_reviews(conn)
            events_q = pending_events(conn)
            recent = [dict(r) for r in conn.execute(
                "SELECT extraction_id, status, round_id, reviewed_at, created_at FROM extractions"
                " WHERE status IN ('accepted','auto_accepted','rejected') ORDER BY extraction_id DESC LIMIT 10")]
        return page(request, "review.html", "/review", rounds=rounds_q, events=events_q, recent=recent)

    @app.get("/review/round/{extraction_id}", response_class=HTMLResponse)
    def review_round(request: Request, extraction_id: int) -> HTMLResponse:
        from golf.extract.rounds import get_review

        with db() as conn:
            try:
                rv = get_review(conn, extraction_id)
            except KeyError:
                return HTMLResponse("No such extraction.", status_code=404)
        return page(request, "review_round.html", "/review", **_round_context(cfg, rv))

    @app.post("/review/round/{extraction_id}")
    async def review_round_post(request: Request, extraction_id: int) -> RedirectResponse:
        form = await request.form()
        return await run_in_threadpool(_review_round_action, cfg, extraction_id, dict(form))

    @app.get("/review/events", response_class=HTMLResponse)
    def review_events(request: Request) -> HTMLResponse:
        with db() as conn:
            queue = pending_events(conn)
            accepted = timeline(conn)
        candidates = [{"event_id": e["event_id"], "label": f"{e.get('date') or '?'} · {e['event_type']} · "
                                                           f"{(e.get('summary') or '')[:60]}"}
                      for e in accepted + queue]
        for ev in queue:
            ev["dup_of"] = next((f.get("other_event_id") for f in ev["flags"] if f.get("other_event_id")), None)
            ev["carry_to"] = next((f.get("carryover_to") for f in ev["flags"] if f.get("carryover_to")), None)
            ev["focus_text"] = ", ".join(f.get("game_area", "") for f in ev.get("focus_areas") or [])
            ev["image"] = (ev.get("doc_external_id") if ev.get("doc_source") == "notebook"
                           and Path(ev.get("doc_external_id") or "").suffix.lower() in SERVABLE else None)
        return page(request, "review_events.html", "/review", events=queue, candidates=candidates,
                    event_types=EVENT_TYPES)

    @app.post("/review/events")
    async def review_events_post(request: Request) -> RedirectResponse:
        form = await request.form()
        return await run_in_threadpool(_review_event_action, cfg, dict(form))

    # ------------------------------------------------------------ timeline, status
    @app.get("/timeline", response_class=HTMLResponse)
    def timeline_page(request: Request) -> HTMLResponse:
        with db() as conn:
            events = timeline(conn)
        for ev in events:
            ev["focus_text"] = ", ".join(f.get("game_area", "") for f in ev.get("focus_areas") or [])
        return page(request, "timeline.html", "/timeline", events=events[::-1])

    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request) -> HTMLResponse:
        from golf.courses import check_courses

        with db() as conn:
            info = ops.status_info(conn, cfg)
            issues = check_courses(conn)
            birdies = watch_mod.status_info(conn, cfg, agents_dir=agents_dir)
        return page(request, "status.html", "/status", info=info, text=ops.format_status(info), issues=issues,
                    birdies=birdies, export_url=ops.EXPORT_URL, export_steps=EXPORT_STEPS,
                    stale_days=EXPORT_STALE_DAYS)

    @app.post("/watch/scan")
    def watch_scan_post() -> RedirectResponse:
        def job() -> dict[str, Any]:
            with ops.open_db(cfg) as conn:
                s = watch_mod.scan_once(conn, cfg, source="serve", publish_runner=publish_runner)
            if s["errors"] and not s["imported"]:
                raise ValueError(watch_mod.format_scan(s))
            return {"text": watch_mod.format_scan(s), "links": [("Dashboard", "/")]}

        app.state.jobs.submit("watch", "Look in Downloads for a new 18Birdies export", job, cfg=cfg)
        return _redirect("/status#birdies", msg="Looking for a new export.")

    @app.post("/publish")
    def publish_post() -> RedirectResponse:
        from golf import publish as publish_mod

        if not publish_mod.settings(cfg).enabled:
            return _redirect("/status#birdies", err="Publishing is off: set enabled = true under [publish] in "
                                                    "config.toml.")

        def job() -> dict[str, Any]:
            with ops.open_db(cfg) as conn:
                out = publish_mod.publish(conn, cfg, runner=publish_runner)
            return {"text": publish_mod.format_publish(out), "links": [("Open the public site", out["site_url"])]}

        app.state.jobs.submit("publish", "Publish to GitHub Pages", job, cfg=cfg)
        return _redirect("/status#birdies", msg="Publishing.")

    # ------------------------------------------------------------ files under data/
    @app.get("/files/{rel:path}")
    def data_file(rel: str) -> Response:
        root = Path(cfg.data_dir).resolve()
        try:
            path = (root / rel).resolve()
            path.relative_to(root)
        except (ValueError, OSError):
            return PlainTextResponse("Not found.", status_code=404)
        if not path.is_file() or path.suffix.lower() not in SERVABLE:
            return PlainTextResponse("Not found.", status_code=404)
        if path.suffix.lower() in CONVERT_FOR_BROWSER:
            try:
                jpeg = _to_jpeg(path)
            except Exception:     # zero-byte or corrupt HEIC: PIL / pillow_heif raise assorted errors
                return PlainTextResponse("Unreadable image.", status_code=415)
            return Response(jpeg, media_type="image/jpeg", headers=FILE_HEADERS)
        media = {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}.get(path.suffix.lower(),
                                                                                         "image/jpeg")
        return Response(path.read_bytes(), media_type=media, headers=FILE_HEADERS)

    return app


# ================================================================ display helpers (Jinja filters)
SOURCE_LABELS = {"18b_export": "18Birdies export", "screenshot": "screenshots", "manual": "manual entry",
                 "launchd": "LaunchAgent", "serve": "this app", "cli": "Terminal", "loop": "golf watch"}


def localtime(iso: Any, tz: str) -> str:
    """A stored UTC timestamp as local wall time ('Sep 25, 22:07'): 02:07 UTC reads as tomorrow."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)[:16].replace("T", " ")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    local = dt.astimezone(ZoneInfo(tz))
    return f"{local:%b} {local.day}, {local:%H:%M}"


def _short_dir(cfg: Config, folder: Path) -> str:
    """A folder as the project sees it (data/raw/18birdies): short, and it doesn't name the Mac's user."""
    shown = ops._display_path(cfg, folder)
    if Path(shown).is_absolute():
        shown = f"{cfg.data_dir.name}/" + folder.relative_to(cfg.data_dir).as_posix() \
            if folder.is_relative_to(cfg.data_dir) else folder.name
    return shown


def plural(n: Any, word: str, many: str | None = None) -> str:
    n = n or 0
    return f"{n} {word if n == 1 else (many or word + 's')}"


# ================================================================ form actions
def _add_action(cfg: Config, form: Any) -> RedirectResponse:
    """The Inbox 'Quick entry' form: a timeline event typed by hand, no API call (golf add --type)."""
    from golf.notes import add_manual_event

    text = str(form.get("text") or "").strip()
    event_type = str(form.get("event_type") or "").strip()
    on = str(form.get("date") or "").strip() or None
    if not text or not event_type:
        return _redirect("/inbox#quick", err="Quick entry needs a type and a few words about what happened.")
    if on:
        try:
            on = datetime.strptime(on, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return _redirect("/inbox#quick", err="The date must be YYYY-MM-DD.")
    try:
        with ops.open_db(cfg) as conn:
            r = add_manual_event(conn, cfg, text, date=on, event_type=event_type,
                                 coach=str(form.get("coach") or "").strip() or None,
                                 focus=str(form.get("focus") or "").strip() or None, use_llm=False)
    except ValueError as e:
        return _redirect("/inbox#quick", err=str(e))
    ev = r["events"][0] if r["events"] else {}
    return _redirect("/timeline", msg=f"Added {str(ev.get('event_type', event_type)).replace('_', ' ')} on "
                                      f"{ev.get('date', on or 'today')}. It is on the dashboard now.")


def _inbox_action(cfg: Config, jobs: Jobs, form: Any) -> RedirectResponse:
    """Upload handling. The multipart form is read by hand: an empty file input arrives as a plain
    string, which a typed UploadFile parameter would turn into a 422 instead of a friendly message."""
    if form.get("action") == "process":
        jobs.submit("inbox", "Process inbox folders", lambda: _process_inbox_job(cfg), cfg=cfg)
        return _redirect("/inbox", msg="Processing the inbox folders.")
    shots, pages = _real_uploads(form.getlist("screenshots")), _real_uploads(form.getlist("notebook"))
    if not shots and not pages:
        return _redirect("/inbox", err="Choose screenshot or notebook files first.")
    played_on = str(form.get("played_on") or "").strip()
    if played_on:
        try:
            played_on = datetime.strptime(played_on, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return _redirect("/inbox", err="The date must be YYYY-MM-DD.")
    club_id = str(form.get("club_id") or "").strip() or None
    holes = str(form.get("holes") or "")
    stamp = datetime.now(ZoneInfo(cfg.timezone)).strftime("%Y%m%d-%H%M%S")
    try:
        if shots:
            key = f"{played_on}-web-{stamp}" if played_on else f"web-{stamp}"
            paths = _save_uploads(shots, cfg.inbox_dir / "screenshots" / key, ops.IMAGE_EXTS)
            n_holes = int(holes) if holes in ("9", "18") else None
            jobs.submit("screenshots", f"Round screenshots ({len(paths)} image(s))",
                        lambda: _screenshot_job(cfg, key, paths, played_on or None, club_id, n_holes), cfg=cfg)
        if pages:
            paths_nb = _save_uploads(pages, cfg.inbox_dir / "notebook" / f"web-{stamp}", ops.IMAGE_EXTS)
            jobs.submit("notebook", f"Notebook pages ({len(paths_nb)} photo(s))",
                        lambda: _notebook_job(cfg, paths_nb), cfg=cfg)
    except ValueError as e:
        return _redirect("/inbox", err=str(e))
    return _redirect("/inbox", msg="Uploaded. Extraction is running; this page refreshes until it is done.")


def _import_action(cfg: Config, jobs: Jobs, form: Any) -> RedirectResponse:
    """The upload is saved under data/tmp/ first; the job validates it and only then files it as
    data/raw/18birdies/18Birdies_archive_YYYYMMDD.json (or reuses an identical snapshot). A failed or
    duplicate upload leaves nothing behind."""
    files = _real_uploads(form.getlist("archive"))
    if not files or not files[0].filename.lower().endswith(".json"):
        return _redirect("/inbox", err="Choose the 18Birdies_archive*.json file first.")
    archive = files[0]
    tmp = _save_uploads([archive], cfg.data_dir / "tmp", {".json"})[0]
    original = archive.filename
    jobs.submit("import", f"18Birdies export {Path(original).name}", lambda: _import_job(cfg, tmp, original),
                cfg=cfg)
    return _redirect("/inbox", msg="Importing the export.")


# ================================================================ job bodies
def _llm_error(message: str) -> llm.LLMUnavailable:
    """Re-raise a summary's API failure as the right kind (a missing key vs. the API being busy)."""
    transient = getattr(llm, "LLMTransient", None)
    return transient(message) if transient is not None and ops.is_transient(message) else llm.LLMUnavailable(message)


def _screenshot_job(cfg: Config, key: str, paths: list[Path], played_on: str | None, club_id: str | None,
                    holes: int | None) -> dict[str, Any]:
    from golf.extract import inbox

    ordered = [p for _, _, p in sorted((inbox.capture_time(p, cfg), p.name, p) for p in paths)]
    group = {"key": key, "kind": "folder", "paths": ordered, "played_on": played_on,
             "captured_at": None}
    with ops.open_db(cfg) as conn:
        r = inbox.process_group(conn, cfg, group, club_id=club_id, expected_holes=holes)
        if r["action"] == "error":
            raise RuntimeError(f"{r['error']} (the screenshots stay in the inbox)")
        xid = r["extraction_id"]
        if r["outcome"] is None:
            text = f"These screenshots were extracted before (extraction {xid}); nothing was sent again."
        else:
            text = ops.format_screenshot_outcome(r["outcome"])
            if r["outcome"]["status"] in ("accepted", "auto_accepted"):
                ops.refresh_derived(conn, cfg)
    return {"text": text, "links": [("Open the review page", f"/review/round/{xid}")]}


def _notebook_job(cfg: Config, paths: list[Path]) -> dict[str, Any]:
    with ops.open_db(cfg) as conn:
        s = ops.import_notebook_files(conn, cfg, paths, from_inbox=True)
    if s.get("llm_unavailable"):
        raise _llm_error(s["llm_unavailable"])
    return {"text": ops.format_notebook(s), "links": [("Review the new events", "/review/events")]}


def _process_inbox_job(cfg: Config) -> dict[str, Any]:
    with ops.open_db(cfg) as conn:
        out = ops.process_inboxes(conn, cfg)
        if any(g["action"] == "extracted" for g in out["screenshots"]):
            ops.refresh_derived(conn, cfg)
    if out["notebook"] and out["notebook"].get("llm_unavailable"):
        raise _llm_error(out["notebook"]["llm_unavailable"])
    links = [(f"Review extraction {g['extraction_id']}", f"/review/round/{g['extraction_id']}")
             for g in out["screenshots"] if g.get("extraction_id") is not None]
    return {"text": ops.format_inbox(out), "links": links}


def _import_job(cfg: Config, tmp: Path, original_name: str | None = None) -> dict[str, Any]:
    from golf.ingest.birdies_export import ExportFormatError, preview_export

    name = original_name or tmp.name
    try:
        with ops.open_db(cfg) as conn:
            foreign = preview_export(conn, tmp, cfg)["account"] == "different"
    except ExportFormatError:
        foreign = False                       # store_upload below explains it (and keeps nothing)
    if foreign:                               # someone else's personal data: not even kept under data/
        Path(tmp).unlink(missing_ok=True)
        raise ValueError(f"{name} is an export of a different 18Birdies account than your rounds, so nothing was "
                         "imported or kept. If you really switched accounts, import it in Terminal: "
                         "golf import-18b <the file> --new-account")
    try:
        stored = ops.store_upload(cfg, tmp, original_name)
    except ExportFormatError as e:
        raise ValueError(f"{original_name or tmp.name} does not look like an 18Birdies export ({e}). Nothing was "
                         "kept.") from None
    with ops.open_db(cfg) as conn:
        out = ops.import_18b(conn, cfg, stored)
    return {"text": ops.format_import(out), "links": [("Dashboard", "/"), ("Course check", "/status")]}


def export_age_days(conn: Any, cfg: Config) -> int | None:
    last = ops.last_export_info(conn, cfg)
    return last["age_days"] if last else None


def _notes_job(cfg: Config, runner: Any, dry_run: bool) -> dict[str, Any]:
    from golf.notes import format_sync_summary, sync_apple_notes

    with ops.open_db(cfg) as conn:
        s = sync_apple_notes(conn, cfg, runner=runner, dry_run=dry_run)
    if s.get("llm_unavailable"):
        raise _llm_error(s["llm_unavailable"])
    return {"text": format_sync_summary(s), "links": [("Review events", "/review/events")]}


# ================================================================ review handlers
def _round_context(cfg: Config, rv: dict[str, Any]) -> dict[str, Any]:
    """Template-ready rows: every hole of the round (missing ones blank, so Shane can add them), each
    cell with its display value and flag class."""
    from golf.extract.validate import cell_key

    by_hole = {h["hole"]: h for h in rv["holes"]}
    top = max(by_hole, default=0)
    expected = range(1, 19) if top > 9 or len(by_hole) > 9 else range(1, 10)
    cells = rv["flags_by_cell"]
    rows = []
    for n in sorted(set(expected) | set(by_hole)):
        h = by_hole.get(n)
        row_flags = cells.get(cell_key(n, None), [])
        rows.append({
            "hole": n, "missing": h is None,
            "hole_class": _flag_class(row_flags), "hole_title": " ".join(f["message"] for f in row_flags),
            "reference": h.get("reference_strokes") if h else None,
            "confidence": h.get("confidence") if h else "",
            "cells": [{"field": f, "value": _blank(h.get(f)) if h else "",
                       "cls": _flag_class(cells.get(cell_key(n, f), [])),
                       "title": " ".join(x["message"] for x in cells.get(cell_key(n, f), [])),
                       "uncertain": bool(h and f in h.get("uncertain_fields", []))}
                      for f in rv["fields"]],
        })
    header = rv["header"]
    round_cells = [{"field": f, "value": _blank(header.get(f)), "cls": _flag_class(cells.get(cell_key(None, f), [])),
                    "title": " ".join(x["message"] for x in cells.get(cell_key(None, f), []))}
                   for f in ROUND_FIELDS]
    root = Path(cfg.data_dir).resolve()
    images = []
    for i, p in enumerate(rv["image_paths"], start=1):
        info = next((im for im in rv["images"] if im.get("image_index") == i), {})
        full = (root / p).resolve()
        ok = full.is_file() and full.suffix.lower() in SERVABLE and ops._inside(full, root)
        images.append({"index": i, "url": "/files/" + quote(p) if ok else None, "name": Path(p).name,
                       "info": info})
    n_errors = sum(f["severity"] == "E" for f in rv["flags"])
    return {"rv": rv, "rows": rows, "round_cells": round_cells, "images": images, "n_errors": n_errors,
            "choices": rv["choices"], "int_fields": [f for f in rv["fields"] if f not in rv["choices"]]}


def _review_round_action(cfg: Config, extraction_id: int, form: dict[str, Any]) -> RedirectResponse:
    from golf.extract.rounds import accept_extraction, apply_corrections, get_review, reject_extraction

    back = f"/review/round/{extraction_id}"
    action = form.get("action", "save")
    with ops.open_db(cfg) as conn:
        try:
            rv = get_review(conn, extraction_id)
            if action == "reject":
                reject_extraction(conn, extraction_id)
                ops.refresh_derived(conn, cfg)
                return _redirect("/review", msg=f"Extraction {extraction_id} rejected.")
            changes = changed_cells(rv, form)
            out = apply_corrections(conn, extraction_id, changes) if changes else None
            if action in ("accept", "accept_force"):
                out = accept_extraction(conn, extraction_id, force=action == "accept_force")
            ops.refresh_derived(conn, cfg)
        except (ValueError, KeyError) as e:
            return _redirect(back, err=str(e.args[0] if isinstance(e, KeyError) else e))
    if out is None:
        return _redirect(back, msg="No changes.")
    done = out["status"] in ("accepted", "auto_accepted")
    text = (f"{len(changes)} correction(s) saved. " if changes else "") + f"Status: {out['status']}."
    if done:
        return _redirect("/review", msg=f"Extraction {extraction_id}: {text}")
    return _redirect(back, msg=text + " Fix the remaining red cells, or accept anyway.")


def changed_cells(rv: dict[str, Any], form: dict[str, Any]) -> list[dict[str, Any]]:
    """Form cells that differ from what is stored, as apply_corrections items. Blank means 'not visible'."""
    by_hole = {h["hole"]: h for h in rv["holes"]}
    out = []
    for key, raw in form.items():
        m = re.fullmatch(r"c-(\d+)-([a-z_]+)", key)
        if m:
            hole, field = int(m.group(1)), m.group(2)
            if field not in rv["fields"]:
                continue
            current = _blank(by_hole[hole].get(field)) if hole in by_hole else ""
            value = str(raw).strip()
            if value != current:
                out.append({"hole": hole, "field": field, "value": value or None})
        elif key.startswith("r-") and key[2:] in ROUND_FIELDS:
            field = key[2:]
            value = str(raw).strip()
            if value != _blank(rv["header"].get(field)):
                out.append({"hole": None, "field": field, "value": value or None})
    return out


def _review_event_action(cfg: Config, form: dict[str, Any]) -> RedirectResponse:
    from golf.notes import carry_over_review, review_event

    event_id, action = form.get("event_id", ""), form.get("action", "")
    anchor = "/review/events"
    with ops.open_db(cfg) as conn:
        try:
            ev = next((e for e in pending_events(conn) if e["event_id"] == event_id), None)
            if ev is None:
                return _redirect(anchor, err=f"Event {event_id} is not waiting for review.")
            if action == "accept":
                review_event(conn, event_id, "accept")
            elif action == "reject":
                review_event(conn, event_id, "reject", comment=form.get("comment") or None)
            elif action == "edit":
                edits = ops.event_edits(ev, date_=form.get("date"), event_type=form.get("event_type"),
                                        coach=form.get("coach"), summary=form.get("summary"),
                                        focus=form.get("focus"))
                review_event(conn, event_id, "edit" if edits else "accept", edits=edits or None)
            elif action == "merge":
                review_event(conn, event_id, "merge", merged_into=(form.get("merged_into") or "").strip())
            elif action == "carry" and form.get("carry_to"):
                carry_over_review(conn, event_id, form["carry_to"])
            else:
                return _redirect(anchor, err="Unknown action.")
        except (ValueError, KeyError) as e:
            return _redirect(anchor, err=str(e.args[0] if isinstance(e, KeyError) else e))
    return _redirect(anchor, msg=f"Saved ({action}).")


def _to_jpeg(path: Path) -> bytes:
    """HEIC (iPhone) for browsers that can't show it."""
    from PIL import Image, ImageOps
    import pillow_heif

    pillow_heif.register_heif_opener()
    with Image.open(path) as im:
        rgb = ImageOps.exif_transpose(im).convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=88)
        return buf.getvalue()
