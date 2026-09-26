"""Project configuration: config.toml + .env, resolved relative to the project root."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10
    import tomli as tomllib

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# An 18Birdies export older than this is "time for a fresh one" everywhere (dashboard tile, top-bar pill,
# golf status). Two weeks: in season Shane plays a couple of nines a week.
EXPORT_STALE_DAYS = 14


@dataclass(frozen=True)
class Config:
    root: Path
    player_name: str = "Shane"
    timezone: str = "America/New_York"
    model: str = "claude-opus-5"
    effort: str = "high"
    use_fallbacks: bool = True
    max_tokens: int = 32000
    apple_notes_folder: str = "Golf"
    data_dir: Path = field(default=Path("data"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "golf.db"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def site_dir(self) -> Path:
        return self.data_dir / "site"


# Keys that must never come from a file: the Caddie's Meta key lives in the macOS Keychain (golf.secrets).
# A line for one of these in .env is skipped (never put in the environment) and its name noted here, so
# `golf caddie status` and the Status page can say to delete it. The value is never kept or shown.
NEVER_FROM_DOTENV = frozenset({"MUSE_API_KEY"})
DOTENV_IGNORED: set[str] = set()


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; never overrides variables already set, and never loads the
    keys in NEVER_FROM_DOTENV."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        name = key.removeprefix("export ").strip()          # `export MUSE_API_KEY=...` is caught too
        if name in NEVER_FROM_DOTENV:
            DOTENV_IGNORED.add(name)
            continue
        value = value.strip().strip('"').strip("'")
        if value and key not in os.environ:
            os.environ[key] = value


def load_config(root: Path | None = None) -> Config:
    root = Path(root or os.environ.get("GOLF_ROOT") or PROJECT_ROOT)
    load_dotenv(root / ".env")
    raw: dict = {}
    cfg_file = root / "config.toml"
    if cfg_file.exists():
        raw = tomllib.loads(cfg_file.read_text())
    player, llm, notes, paths = (raw.get(k, {}) for k in ("player", "llm", "notes", "paths"))
    # "~/golf-data" works too: a data folder outside the iCloud-synced Desktop (see README, Privacy).
    data_dir = Path(os.path.expanduser(str(os.environ.get("GOLF_DATA_DIR") or paths.get("data_dir", "data"))))
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    return Config(
        root=root,
        player_name=player.get("name", "Shane"),
        timezone=player.get("timezone", "America/New_York"),
        model=os.environ.get("GOLF_MODEL") or llm.get("model", "claude-opus-5"),
        effort=llm.get("effort", "high"),
        use_fallbacks=bool(llm.get("use_fallbacks", True)),
        max_tokens=int(llm.get("max_tokens", 32000)),
        apple_notes_folder=notes.get("apple_notes_folder", "Golf"),
        data_dir=data_dir,
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()
