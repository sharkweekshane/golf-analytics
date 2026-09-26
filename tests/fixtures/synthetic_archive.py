"""Synthetic 18Birdies archive shaped like the real export (docs/RESEARCH_PLAN.md §2).

Every value is invented. The PII sections carry obviously fake markers (FAKE_PII) so tests can prove
they never reach the database. Special rounds cover every entry mode and data-quality case; their
ids name the case (CASES) so tests can find them. The remaining rounds are ordinary hole-by-hole
rounds at Synthetic Pines, whose layout matches courses.example.yaml.

privacy-check: synthetic (fake PII markers on purpose; see golf/privacy.py)
"""
from __future__ import annotations

import copy
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PINES = "00000000-0000-4000-8000-00000000a001"    # single-course club (courses.example.yaml)
DUNES = "00000000-0000-4000-8000-00000000b002"    # two-course club: needs an explicit yaml mapping
ORPHAN = "00000000-0000-4000-8000-00000000c003"   # used by a round but missing from playedClubs

PINES_PARS = [4, 4, 3, 5, 4, 4, 3, 4, 5, 4, 3, 5, 4, 4, 4, 3, 5, 4]    # 36 + 36 = 72
DUNES_PARS = [4, 3, 4, 4, 5, 3, 4, 4, 4, 4, 4, 3, 4, 5, 3, 4, 4, 4]    # 70

FAKE_PII = {
    "email": "fake.golfer@example.invalid",
    "phone": "+1-555-010-9999",
    "user_name": "Fakey McTestface",
    "friend": "Imaginary Friend Zed",
    "feed": "FAKE-FEED-POST-7f3a notes about my swing",
    "payment": "FAKE-VISA-0000",
    "user_id": "fake-user-id-00042",
}

CASES = {
    "hbh_18": "syn-hbh-18",                 # putts tracked while puttHoleCount = 0
    "under_par": "syn-under-par",           # score < 0: par_played = strokes - score (not abs)
    "total_only_18": "syn-total-only-18",
    "total_only_9": "syn-total-only-9",
    "front_nine": "syn-front-nine",         # padded 18-array, holes 1-9 played
    "back_nine": "syn-back-nine",           # padded 18-array, holes 10-18 played
    "nine_array": "syn-nine-array",         # 9-length array: nine unknown
    "partial": "syn-partial",               # 4 holes
    "abandoned": "syn-abandoned",
    "sum_mismatch": "syn-sum-mismatch",
    "sg_real": "syn-sg-real",               # strokeGain not the 100 sentinel; tracked zeros
    "late_night": "syn-late-night",         # 01:30 UTC = previous evening in New York
    "dunes": "syn-dunes",                   # multi-course club
    "orphan_club": "syn-orphan-club",
    "unknown_keys": "syn-unknown-keys",     # new round key + new stats key + new shot key
    "putts_partial": "syn-putts-partial",   # putts entered on a few holes only (11 putts over 18)
}
UNKNOWN_ROUND_KEY = "shotTrackingV2"
UNKNOWN_STATS_KEY = "sandSaves"
UNKNOWN_SHOT_KEY = "lieType"
UNKNOWN_SECTION = "practiceLog"

# Keys found in Shane's real export (Sep 2026), now parsed: rounds[].roundHandicap (a string) and
# rounds[].shotEntries (GPS shots). Coordinates below are fake (a spot in the Atlantic).
HBH_18_HANDICAP = "18.4"
SHOT_BASE_MS = 1_750_000_000_000

_START = datetime(2025, 4, 5, 14, 0, tzinfo=timezone.utc)


def _stats(holes: list[int], pars: list[int], rng: random.Random, *, tracked: bool) -> dict[str, Any]:
    """Round stats in the export's shape; untracked stats are exported as zeros."""
    to_par = [s - p for s, p in zip(holes, pars) if s > 0]
    fairway_holes = sum(1 for s, p in zip(holes, pars) if s > 0 and p > 3)
    played = len(to_par)
    st: dict[str, Any] = {
        "aces": 0,
        "doubleEagleOrBetter": sum(1 for d in to_par if d <= -3),
        "eagles": sum(1 for d in to_par if d == -2),
        "birdies": sum(1 for d in to_par if d == -1),
        "pars": sum(1 for d in to_par if d == 0),
        "bogeys": sum(1 for d in to_par if d == 1),
        "doubleBogeyOrWorse": sum(1 for d in to_par if d >= 2),
        "fairwayLefts": 0, "fairwayMiddles": 0, "fairwayRights": 0, "fairwayShorts": 0, "fairwayLongs": 0,
        "fairwayHoleCount": 0,
        "gir": 0, "girLefts": 0, "girRights": 0, "girShorts": 0, "girLongs": 0, "girNoChances": 0,
        "girHoleCount": 0,
        "putts": 0, "puttHoleCount": 0,
        "strokeGainOverall": 100, "strokeGainTeeToGreen": 100,
        "recommendedStats": [{"type": "FAIRWAY_HIT_PERCENTAGE", "noStats": True, "value": 0}],
    }
    if tracked and played:
        hit = rng.randint(0, fairway_holes)
        left = rng.randint(0, fairway_holes - hit)
        gir = rng.randint(0, played // 2)
        st.update(fairwayMiddles=hit, fairwayLefts=left, fairwayRights=fairway_holes - hit - left,
                  fairwayHoleCount=fairway_holes, gir=gir, girShorts=played - gir, girHoleCount=played,
                  putts=played * 2 - rng.randint(0, 3))
    return st


def _round(rid: str, when: datetime, club: str, holes: list[int], pars: list[int], rng: random.Random, *,
           tracked: bool = True, strokes: int | None = None, score: int | None = None) -> dict[str, Any]:
    strokes = sum(holes) if strokes is None else strokes
    par_played = sum(p for s, p in zip(holes, pars) if s > 0) or sum(pars[:len(holes)])
    return {
        "id": rid,
        "timestamp": int(when.timestamp() * 1000),
        "clubId": {"id": club},
        "score": strokes - par_played if score is None else score,
        "strokes": strokes,
        "holeStrokes": holes,
        "stats": _stats(holes, pars, rng, tracked=tracked),
    }


def _shot(ms: int | None, hole: int, kind: str, number: str, loft: int, yards: float, tee: str | None,
          **extra: Any) -> dict[str, Any]:
    shot: dict[str, Any] = {
        "holeNumber": hole,
        "stickTypeAndNumber": {"type": kind, "number": number, "loftAngle": loft},
        "startPoint": {"latitude": 40.0 + hole / 1000, "longitude": -60.0},
        "endPoint": {"latitude": 40.0 + hole / 1000 + yards / 120_000, "longitude": -60.0},
        "distanceInYards": yards,
        **extra,
    }
    if ms is not None:
        shot["timestamp"] = ms
    if tee is not None:
        shot["teeName"] = tee
    return shot


def hbh_shots() -> list[dict[str, Any]]:
    """GPS shots in the export's shape, deliberately NOT in time order (the importer sorts them).

    Time order: driver, 7i, PW (hole 1), putter (hole 1), driver (hole 2, Blue tee), 5H; then one
    shot with no timestamp, which sorts last. Most common teeName: White.
    """
    t = SHOT_BASE_MS
    return [
        _shot(t + 60_000, 1, "IRON", "7", 0, 151.5, "White"),
        _shot(t, 1, "WOOD", "1", 0, 231.25, "White"),
        _shot(t + 150_000, 1, "WEDGE", "P", 46, 88.0, "White"),
        _shot(t + 240_000, 1, "PUTTER", "Putter", 0, 4, None),
        _shot(t + 600_000, 2, "WOOD", "1", 0, 244.0, "Blue"),
        _shot(t + 660_000, 2, "HYBRID", "5", 0, 170.0, None),
        _shot(None, 2, "WEDGE", "S", 56, 22.5, "White"),
    ]


def _handicap_like_18birdies(strokes: int) -> str:
    """A plausible 18Birdies roundHandicap for a Pines round (White 71.4/128): a string, like theirs."""
    return f"{(strokes - 71.4) * 113 / 128:.1f}"


def _card(rng: random.Random, pars: list[int], spread: tuple[int, ...] = (0, 0, 1, 1, 1, 2, 3)) -> list[int]:
    return [p + rng.choice(spread) for p in pars]


def _special_rounds(rng: random.Random) -> list[tuple[str, Any]]:
    """(case, builder(when) -> round) pairs, in the order they are played."""
    P = PINES_PARS

    def under_par(when):
        holes = list(P)
        holes[1], holes[3], holes[7] = 3, 4, 3              # three birdies, 69 on a par 72
        r = _round(CASES["under_par"], when, PINES, holes, P, rng)
        r["roundHandicap"] = "+2.1"                          # plus (better than scratch) notation
        return r

    def total_only(case, n, strokes, score):
        return lambda when: _round(CASES[case], when, PINES, [0] * n, P, rng, tracked=False,
                                   strokes=strokes, score=score)

    def front(when):
        return _round(CASES["front_nine"], when, PINES, _card(rng, P[:9]) + [0] * 9, P, rng)

    def back(when):
        return _round(CASES["back_nine"], when, PINES, [0] * 9 + _card(rng, P[9:]), P, rng)

    def nine_array(when):                                    # really the back nine, but the export can't say
        return _round(CASES["nine_array"], when, PINES, _card(rng, P[9:]), P[9:], rng)

    def partial(when):
        return _round(CASES["partial"], when, PINES, _card(rng, P[:4]) + [0] * 14, P, rng, tracked=False)

    def abandoned(when):
        return _round(CASES["abandoned"], when, PINES, [0] * 18, P, rng, tracked=False, strokes=0, score=0)

    def sum_mismatch(when):
        holes = _card(rng, P)
        return _round(CASES["sum_mismatch"], when, PINES, holes, P, rng, strokes=sum(holes) + 3)

    def sg_real(when):
        r = _round(CASES["sg_real"], when, PINES, _card(rng, P), P, rng, tracked=False)
        r["stats"].update(strokeGainOverall=-4.2, strokeGainTeeToGreen=-1.5,
                          fairwayHoleCount=14, fairwayMiddles=0, fairwayLefts=9, fairwayRights=5,
                          girHoleCount=18, gir=0, girShorts=8, girLefts=8, girNoChances=2)
        r["roundHandicap"] = ""                              # seen as an empty string: no value
        return r

    def late_night(when):
        r = _round(CASES["late_night"], when.replace(hour=1, minute=30), PINES, _card(rng, P), P, rng)
        r["roundHandicap"] = None
        return r

    def dunes(when):
        return _round(CASES["dunes"], when, DUNES, _card(rng, DUNES_PARS), DUNES_PARS, rng)

    def orphan(when):
        return _round(CASES["orphan_club"], when, ORPHAN, [0] * 18, P, rng, tracked=False, strokes=95, score=23)

    def unknown_keys(when):
        r = _round(CASES["unknown_keys"], when, PINES, _card(rng, P), P, rng)
        r[UNKNOWN_ROUND_KEY] = {"shots": [{"club": "7i", "distance": 150}]}
        r["stats"][UNKNOWN_STATS_KEY] = 1
        r["shotEntries"] = [_shot(SHOT_BASE_MS, 1, "WOOD", "1", 0, 200.0, "White", **{UNKNOWN_SHOT_KEY: "tee"})]
        return r

    def hbh(when):
        r = _round(CASES["hbh_18"], when, PINES, _card(rng, P), P, rng, tracked=True)
        r["stats"]["puttHoleCount"] = 0
        r["roundHandicap"] = HBH_18_HANDICAP
        r["shotEntries"] = hbh_shots()
        return r

    def putts_partial(when):
        r = _round(CASES["putts_partial"], when, PINES, _card(rng, P), P, rng, tracked=True)
        r["stats"]["putts"] = 11                             # fewer putts than holes: partial entry
        return r

    return [
        ("hbh_18", hbh), ("under_par", under_par),
        ("total_only_18", total_only("total_only_18", 18, 101, 29)),
        ("total_only_9", total_only("total_only_9", 9, 50, 14)),
        ("front_nine", front), ("back_nine", back), ("nine_array", nine_array), ("partial", partial),
        ("abandoned", abandoned), ("sum_mismatch", sum_mismatch), ("sg_real", sg_real),
        ("late_night", late_night), ("dunes", dunes), ("orphan_club", orphan), ("unknown_keys", unknown_keys),
        ("putts_partial", putts_partial),
    ]


def make_archive(n_rounds: int = 26, seed: int = 7) -> dict[str, Any]:
    """A complete fake archive: 3 ordinary rounds first (so a Handicap Index exists early), then every
    special case, then ordinary rounds up to n_rounds. Rounds are 9 days apart."""
    rng = random.Random(seed)
    specials = _special_rounds(rng)
    if n_rounds < len(specials) + 3:
        raise ValueError(f"n_rounds must be at least {len(specials) + 3}")
    builders: list[Any] = []
    normal = iter(range(1, n_rounds + 1))

    def ordinary(when):
        i = next(normal)
        r = _round(f"syn-{i:03d}", when, PINES, _card(rng, PINES_PARS), PINES_PARS, rng,
                   tracked=rng.random() < 0.6)
        r["roundHandicap"] = _handicap_like_18birdies(r["strokes"])
        return r

    builders += [ordinary] * 3 + [b for _, b in specials] + [ordinary] * (n_rounds - len(specials) - 3)
    rounds = [build(_START + timedelta(days=9 * i)) for i, build in enumerate(builders)]
    return {
        "myData": {
            "accountData": {
                "userId": FAKE_PII["user_id"], "userName": FAKE_PII["user_name"], "accountCreatedSource": "EMAIL",
                "registerTimestamp": 1_600_000_000_000, "birthYear": 1900,
                "mobileNumber": FAKE_PII["phone"], "email": FAKE_PII["email"],
            },
            "activityData": {"roundCount": len(rounds), "rounds": rounds},
            "clubData": {
                "playedClubs": [
                    {"clubId": PINES, "name": "Synthetic Pines Golf Club", "city": "Faketown", "state": "ZZ"},
                    {"clubId": DUNES, "name": "Synthetic Dunes Golf Resort"},
                ],
                "postedInClubs": [{"clubId": PINES, "name": "Synthetic Pines Golf Club"}],
            },
            "feedData": {"messageCount": 1, "messages": [{"messageId": "fake-msg-1", "content": FAKE_PII["feed"]}]},
            "friendData": {"friendCount": 1, "friends": [{"userId": "fake-friend-1", "name": FAKE_PII["friend"]}]},
            "subscriptionData": {"status": "FAKE_PREMIUM", "paymentMethod": FAKE_PII["payment"],
                                 "expiredTimestamp": 1_900_000_000_000},
            UNKNOWN_SECTION: {"sessions": [{"date": "2025-05-01", "note": FAKE_PII["feed"]}]},
        }
    }


def rounds_of(archive: dict[str, Any]) -> list[dict[str, Any]]:
    return archive["myData"]["activityData"]["rounds"]


def without_round(archive: dict[str, Any], round_id: str) -> dict[str, Any]:
    out = copy.deepcopy(archive)
    out["myData"]["activityData"]["rounds"] = [r for r in rounds_of(out) if r["id"] != round_id]
    out["myData"]["activityData"]["roundCount"] = len(rounds_of(out))
    return out


def write_archive(path: Path, archive: dict[str, Any], *, indent: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(archive, indent=indent))
    return path
