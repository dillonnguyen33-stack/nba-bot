import asyncio
import re
import time
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable
from typing import TypedDict

import httpx

from pinch_hit.config.reporters import REPORTERS, Reporter

# ── CONSTANTS ─────────────────────────────────────────────────────────────────

MAX_TWEET_AGE_SECS = 600

MLB_TEAM_IDS: dict[str, int] = {
    "Orioles": 110, "Red Sox": 111, "Yankees": 147, "Rays": 139, "Blue Jays": 141,
    "White Sox": 145, "Guardians": 114, "Tigers": 116, "Royals": 118, "Twins": 142,
    "Astros": 117, "Angels": 108, "Athletics": 133, "Mariners": 136, "Rangers": 140,
    "Braves": 144, "Marlins": 146, "Mets": 121, "Phillies": 143, "Nationals": 120,
    "Cubs": 112, "Reds": 113, "Brewers": 158, "Pirates": 134, "Cardinals": 138,
    "Diamondbacks": 109, "Rockies": 115, "Dodgers": 119, "Padres": 135, "Giants": 137,
}

PRESENT_FUTURE_MARKERS = [
    r'\bis\b', r'\bwill\b', r'\bslated\b', r'\bon\s+deck\b',
    r'\bexpected\b', r'\bgoing\s+to\b', r'\bset\s+to\b',
    r'\bcoming\s+up\b', r'\bheading\b', r'\bwarming\b',
    r'\bscheduled\b', r'\bdue\s+to\b', r'\bappears\b',
    r'\blooks\s+like\b', r'\bshould\b', r'\bunclear\b',
    r'\btaking\s+over\b', r'\bcoming\s+in\b',
]

REJECT_PHRASES = [
    "home run", "homered", "hit a", "singled", "doubled", "tripled",
    "drove in", "struck out", "flies out", "grounds out",
    "pinch-hit home run", "pinch hit home run",
    "pinch-hit single", "pinch hit single",
    "pinch-hit rbi", "pinch hit rbi",
    "just hit", "last night", "yesterday",
    "in the 1st", "in the 2nd", "in the 3rd",
    "in the 4th", "in the 5th", "in the 6th",
    "in the 7th", "in the 8th", "in the 9th",
    "went 1-for", "went 0-for", "went 2-for",
]

# Must match the 5 phrases registered as stream filter Rule 2
PINCH_HIT_PHRASES = ["pinch hit", "pinch-hit", "ph for", "pinch hitting", "pinch-hitting"]

REPORTER_BY_HANDLE: dict[str, Reporter] = {r["handle"].lower(): r for r in REPORTERS}

# ── MODULE STATE ──────────────────────────────────────────────────────────────

player_team_map: dict[str, str] = {}
last_roster_refresh: float = 0.0
_refresh_lock = asyncio.Lock()

# ── ROSTER ────────────────────────────────────────────────────────────────────


async def _fetch_team_roster(
    client: httpx.AsyncClient, team_name: str, team_id: int
) -> dict[str, str]:
    entries: dict[str, str] = {}
    try:
        r = await client.get(
            f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster",
            params={"rosterType": "active"},
        )
        r.raise_for_status()
        for player in r.json().get("roster", []):
            full_name = player.get("person", {}).get("fullName", "")
            if not full_name:
                continue
            parts = full_name.split()
            last = parts[-1].lower()
            full_low = full_name.lower()
            entries[last] = team_name
            entries[full_low] = team_name
            if len(parts) >= 2:
                entries[parts[0].lower() + " " + last] = team_name
    except (httpx.HTTPError, httpx.TimeoutException, ValueError) as e:
        print(f"[roster error] {team_name}: {type(e).__name__}: {e}")
    return entries


async def build_player_team_map() -> None:
    global player_team_map, last_roster_refresh
    print("[roster] Refreshing MLB roster lookup...")
    async with httpx.AsyncClient(timeout=10) as client:
        results = await asyncio.gather(*[
            _fetch_team_roster(client, name, tid)
            for name, tid in MLB_TEAM_IDS.items()
        ])
    new_map: dict[str, str] = {}
    for entries in results:
        new_map.update(entries)
    player_team_map = new_map
    last_roster_refresh = time.time()
    print(f"[roster] {len(new_map)} entries loaded")


async def refresh_if_stale() -> None:
    """Refresh roster if 6h have elapsed since last fetch. Call before each tweet."""
    if time.time() - last_roster_refresh >= 21600 or not player_team_map:
        async with _refresh_lock:
            if time.time() - last_roster_refresh >= 21600 or not player_team_map:
                await build_player_team_map()


def lookup_player_team(name: str) -> str | None:
    if not name or not player_team_map:
        return None
    nl = name.lower().strip()
    if nl in player_team_map:
        return player_team_map[nl]
    return player_team_map.get(nl.split()[-1])


def is_mlb_player(name: str) -> bool:
    return lookup_player_team(name) is not None


# ── TWEET FILTERS ─────────────────────────────────────────────────────────────


def is_recent(created_at_str: str | None) -> bool:
    if not created_at_str:
        return True
    try:
        tweet_time = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - tweet_time).total_seconds()
        return age <= MAX_TWEET_AGE_SECS
    except Exception:
        return True


def strip_mentions(text: str) -> str:
    return re.sub(r'@\w+', '', text)


def is_present_future(text: str) -> tuple[bool, str]:
    tl = text.lower()
    for phrase in REJECT_PHRASES:
        if phrase in tl:
            return False, f"rejected: '{phrase}'"
    for marker in PRESENT_FUTURE_MARKERS:
        if re.search(marker, text, re.IGNORECASE):
            return True, f"matched: '{marker}'"
    return False, "no present/future marker"


def extract_players(text: str) -> tuple[str | None, str | None]:
    clean = strip_mentions(text)

    patterns_both = [
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?(?:on\s+deck\s+to\s+)?pinch[- ]hit(?:ting)?\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?(?:bat|hit)\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+slated\s+to\s+pinch[- ]hit\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+taking\s+over\s+\w+\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+ph(?:ing)?\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
    ]
    for p in patterns_both:
        m = re.search(p, clean)
        if m:
            return m.group(1).strip(), m.group(2).strip()

    patterns_hitter = [
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?(?:on\s+deck|slated|expected|set)\s+to\s+pinch[- ]hit',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?pinch[- ]hit',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?pinch[- ]hitting',
        r'(?:ph|pinch[- ]hit(?:ting)?)\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
    ]
    for p in patterns_hitter:
        m = re.search(p, clean)
        if m:
            return m.group(1).strip(), None

    return None, None


# ── PIPELINE ──────────────────────────────────────────────────────────────────


class TweetResult(TypedDict):
    passed: bool
    tweet_id: str
    reporter_handle: str
    team: str
    team_id: int
    pinch_hitter_raw: str
    pinch_hitter_normalized: str
    replaced_player: str | None
    reject_reason: str | None


async def process_tweet(
    tweet_id: str,
    text: str,
    created_at: str | None,
    reporter_handle: str,
    is_seen_fn: Callable[[str], Awaitable[bool]],
) -> TweetResult:
    """
    Full parsing pipeline. Returns TweetResult with passed=True on success.
    Caller logs tweet_rejected / alert_fired based on result.
    """
    base: TweetResult = {
        "passed": False,
        "tweet_id": tweet_id,
        "reporter_handle": reporter_handle,
        "team": "",
        "team_id": 0,
        "pinch_hitter_raw": "",
        "pinch_hitter_normalized": "",
        "replaced_player": None,
        "reject_reason": None,
    }

    # 1. Age check
    if not is_recent(created_at):
        return {**base, "reject_reason": "tweet too old"}

    # 2. Dedup
    if await is_seen_fn(tweet_id):
        return {**base, "reject_reason": "duplicate tweet_id"}

    # 3. Phrase pre-filter (client-side AND with stream rule 2)
    tl = text.lower()
    if not any(phrase in tl for phrase in PINCH_HIT_PHRASES):
        return {**base, "reject_reason": "no pinch-hit phrase"}

    # 4. Tense filter (also checks REJECT_PHRASES)
    ok, reason = is_present_future(text)
    if not ok:
        return {**base, "reject_reason": reason}

    # 5. Name extraction
    hitter_raw, replaced = extract_players(text)
    if not hitter_raw:
        return {**base, "reject_reason": "no player name extracted"}

    # 6. Roster validation
    if not is_mlb_player(hitter_raw):
        return {**base, "reject_reason": f"not on active roster: {hitter_raw}"}

    # 7. Reporter lookup — reject if tweet author isn't a known reporter
    reporter = REPORTER_BY_HANDLE.get(reporter_handle.lower())
    if not reporter:
        return {**base, "reject_reason": f"unknown reporter handle: {reporter_handle}"}

    team = reporter["team"]
    team_id = MLB_TEAM_IDS.get(team, 0)

    return {
        **base,
        "passed": True,
        "team": team,
        "team_id": team_id,
        "pinch_hitter_raw": hitter_raw,
        "pinch_hitter_normalized": hitter_raw.lower().strip(),
        "replaced_player": replaced,
    }
