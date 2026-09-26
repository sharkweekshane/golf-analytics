"""Known entities (entities.yaml): coaches with aliases/initials, practice locations, the current bag.

They are given to the extractor as context so "MR", "coach Mike" and "Mike" all become one coach, and
so "switched to the G440" can be read against what is in the bag. The file's hash is part of the
extraction cache key: editing an alias re-extracts notes (from the cache when nothing else changed).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from golf.config import Config
from golf.db import dumps

ENTITIES_FILE = "entities.yaml"


@dataclass(frozen=True)
class Entities:
    coaches: dict[str, list[str]] = field(default_factory=dict)      # canonical name -> aliases/initials
    locations: dict[str, dict[str, Any]] = field(default_factory=dict)  # canonical -> {"kind", "aliases"}
    bag: list[str] = field(default_factory=list)
    hash: str = ""

    def canonical_coach(self, name: str | None) -> str | None:
        return _canonical(name, self.coaches)

    def canonical_location(self, name: str | None) -> str | None:
        return _canonical(name, {k: v.get("aliases", []) for k, v in self.locations.items()})

    def location_kind(self, canonical: str | None) -> str | None:
        return (self.locations.get(canonical or "") or {}).get("kind")

    def prompt_context(self) -> str:
        """The <known_*> blocks of the extraction prompt (deterministic text, so it hashes stably)."""
        coaches = {k: v for k, v in sorted(self.coaches.items())}
        locations = {k: v.get("aliases", []) for k, v in sorted(self.locations.items())}
        return (
            f"<known_coaches>{dumps(coaches)}</known_coaches>\n"
            f"<known_locations>{dumps(locations)}</known_locations>\n"
            f"<current_bag>{dumps(self.bag)}</current_bag>"
        )


def _canonical(name: str | None, table: dict[str, list[str]]) -> str | None:
    if not name:
        return None
    key = " ".join(name.split()).casefold()
    for canonical, aliases in table.items():
        if key == canonical.casefold() or key in {" ".join(a.split()).casefold() for a in aliases}:
            return canonical
    return None


def _sorted_aliases(aliases: Iterable[str]) -> list[str]:
    """Deduplicated, in a TOTAL order. Sorting by casefold alone left "Mike"/"mike" (or "mr"/"MR") in
    set-iteration order, which depends on PYTHONHASHSEED: the entities hash (part of the extraction
    cache key) then changed between runs and a notes sync could re-extract and re-bill every note."""
    return sorted(set(aliases), key=lambda a: (a.casefold(), a))


def parse_entities(data: dict[str, Any] | None) -> Entities:
    """Normalise the YAML structure; tolerant of missing sections so a half-filled file still works."""
    data = data or {}
    coaches: dict[str, list[str]] = {}
    for c in data.get("coaches") or []:
        if isinstance(c, str):
            coaches[c] = []
            continue
        if not c.get("name"):
            raise ValueError(f"entities.yaml: every coach needs a name (got {c!r})")
        aliases = [str(a) for a in (c.get("aliases") or [])] + [str(a) for a in (c.get("initials") or [])]
        coaches[str(c["name"])] = _sorted_aliases(aliases)
    locations: dict[str, dict[str, Any]] = {}
    for loc in data.get("locations") or []:
        if isinstance(loc, str):
            locations[loc] = {"kind": "other", "aliases": []}
            continue
        if not loc.get("name"):
            raise ValueError(f"entities.yaml: every location needs a name (got {loc!r})")
        locations[str(loc["name"])] = {
            "kind": str(loc.get("kind") or "other"),
            "aliases": _sorted_aliases(str(a) for a in (loc.get("aliases") or [])),
        }
    bag = [str(b) for b in (data.get("bag") or [])]
    canonical = {"coaches": coaches, "locations": locations, "bag": bag}
    digest = hashlib.sha256(dumps(canonical).encode()).hexdigest()[:16]
    return Entities(coaches=coaches, locations=locations, bag=bag, hash=digest)


def load_entities_file(path: Path | str) -> Entities:
    path = Path(path)
    if not path.exists():
        return parse_entities({})
    return parse_entities(yaml.safe_load(path.read_text()) or {})


def load_entities(cfg: Config) -> Entities:
    """entities.yaml at the project root; an absent file means no known entities (hash of empty)."""
    return load_entities_file(Path(cfg.root) / ENTITIES_FILE)
