import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable
from typing import Literal, TypedDict

import httpx

from pinch_hit.config import REPORTERS, Reporter
from pinch_hit.parsing.names import normalize_last_name

logger = logging.getLogger(__name__)

MAX_TWEET_AGE_SECS = 600

MLB_TEAM_IDS: dict[str, int] = {
    "Orioles": 110, "Red Sox": 111, "Yankees": 147, "Rays": 139, "Blue Jays": 141,
    "White Sox": 145, "Guardians": 114, "Tigers": 116, "Royals": 118, "Twins": 142,
    "Astros": 117, "Angels": 108, "Athletics": 133, "Mariners": 136, "Rangers": 140,
    "Braves": 144, "Marlins": 146, "Mets": 121, "Phillies": 143, "Nationals": 120,
    "Cubs": 112, "Reds": 113, "Brewers": 158, "Pirates": 134, "Cardinals": 138,
    "Diamondbacks": 109, "Rockies": 115, "Dodgers": 119, "Padres": 135, "Giants": 137,
}

# Must match the phrases registered as the "phrases" filter rule in twitter.py
PINCH_HIT_PHRASES = ["pinch hit", "pinch-hit", "ph for", "pinch hitting", "pinch-hitting"]

REPORTER_BY_HANDLE: dict[str, Reporter] = {r["handle"].lower(): r for r in REPORTERS}

player_team_map: dict[str, str] = {}
last_roster_refresh: float = 0.0
_refresh_lock = asyncio.Lock()


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
            full_low = full_name.lower()
            entries[full_low] = team_name
            if len(parts) >= 2:
                entries[parts[0].lower() + " " + parts[-1].lower()] = team_name
    except (httpx.HTTPError, ValueError):
        logger.exception("roster fetch failed team=%s", team_name)
    return entries


async def build_player_team_map() -> None:
    global player_team_map, last_roster_refresh
    logger.info("refreshing MLB roster lookup")
    async with httpx.AsyncClient(timeout=10) as client:
        results = await asyncio.gather(*[
            _fetch_team_roster(client, name, tid)
            for name, tid in MLB_TEAM_IDS.items()
        ])
    new_map: dict[str, str] = {}
    empty_count = 0
    for entries in results:
        if not entries:
            empty_count += 1
        new_map.update(entries)
    if not new_map and player_team_map:
        # Total API failure — keep stale roster data instead of wiping it
        logger.critical("all %s teams returned empty; keeping stale roster", empty_count)
        return
    player_team_map = new_map
    last_roster_refresh = time.time()
    if empty_count > 5:
        logger.warning("%s/%s teams returned empty rosters; possible API issue", empty_count, len(MLB_TEAM_IDS))
    logger.info("%s roster entries loaded", len(new_map))


async def refresh_if_stale() -> None:
    """Called before each tweet parse."""
    if time.time() - last_roster_refresh >= 21600 or not player_team_map:
        async with _refresh_lock:
            if time.time() - last_roster_refresh >= 21600 or not player_team_map:
                await build_player_team_map()


def is_mlb_player(name: str) -> bool:
    """Requires full name (first + last) to match MLB roster."""
    if not name or not player_team_map:
        return False
    nl = name.lower().strip()
    return nl in player_team_map and " " in nl


def normalize_player_last_name(name: str) -> str:
    cleaned = re.sub(r"[^\w\s'-]", "", name)
    return normalize_last_name(cleaned)


_TWITTER_DATE_FMT = "%a %b %d %H:%M:%S %z %Y"


def is_recent(created_at_str: str | None) -> bool:
    if not created_at_str:
        return True
    try:
        tweet_time = datetime.fromisoformat(created_at_str)
    except ValueError:
        try:
            tweet_time = datetime.strptime(created_at_str, _TWITTER_DATE_FMT)
        except ValueError:
            logger.warning("unparseable created_at=%s; treating as recent", created_at_str)
            return True
    age = (datetime.now(timezone.utc) - tweet_time).total_seconds()
    return age <= MAX_TWEET_AGE_SECS


def strip_mentions(text: str) -> str:
    return re.sub(r'@\w+', '', text)


def extract_players(text: str) -> tuple[str | None, str | None]:
    clean = strip_mentions(text)

    patterns_both = [
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:(?:is|will)\s+)?(?:on\s+deck\s+to\s+)?pinch[- ]hit(?:ting)?\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?(?:bat|hit)\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+slated\s+to\s+pinch[- ]hit\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+taking\s+over\s+\w+\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+ph(?:ing)?\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
    ]
    for p in patterns_both:
        m = re.search(p, clean)
        if m:
            return m.group(1).strip(), m.group(2).strip()

    inverted_patterns = [
        r'[Pp]inch[- ][Hh]it(?:ting)?\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)[,;:\s.—–\-]+(?:(?:is|will\s+be)\s+)?([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?(?:getting\s+)?pinch[- ]hit\s+for\s+by\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:has\s+)?left\s+the\s+game.{0,100}?([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?pinch',
    ]
    for p in inverted_patterns:
        m = re.search(p, clean)
        if m:
            return m.group(2).strip(), m.group(1).strip()

    patterns_hitter_only = [
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:(?:is|will)\s+)?(?:on\s+deck\s+to\s+)?pinch[- ]hit(?:ting)?\b',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+slated\s+to\s+pinch[- ]hit\b',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?(?:bat|hit|ph)\b',
        r'[Pp]inch[- ][Hh]it(?:ting)?[,;:\s.—–\-]+(?:(?:is|will\s+be)\s+)?([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
    ]
    for p in patterns_hitter_only:
        m = re.search(p, clean)
        if m:
            return m.group(1).strip(), None

    return None, None


class TweetRejected(TypedDict):
    passed: Literal[False]
    tweet_id: str
    reporter_handle: str
    reject_reason: str


class TweetAccepted(TypedDict):
    passed: Literal[True]
    tweet_id: str
    reporter_handle: str
    team: str
    team_id: int
    pinch_hitter_raw: str
    pinch_hitter_normalized: str
    replaced_player: str | None


TweetResult = TweetRejected | TweetAccepted


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
    base: TweetRejected = {
        "passed": False,
        "tweet_id": tweet_id,
        "reporter_handle": reporter_handle,
        "reject_reason": "",
    }

    if not is_recent(created_at):
        return {**base, "reject_reason": "tweet too old"}

    if await is_seen_fn(tweet_id):
        return {**base, "reject_reason": "duplicate tweet_id"}

    # Phrase pre-filter (client-side mirror of the "phrases" filter rule)
    tl = text.lower()
    if not any(phrase in tl for phrase in PINCH_HIT_PHRASES):
        return {**base, "reject_reason": "no pinch-hit phrase"}

    hitter_raw, replaced = extract_players(text)
    if not hitter_raw:
        return {**base, "reject_reason": "no player name extracted"}

    if not is_mlb_player(hitter_raw):
        return {**base, "reject_reason": f"not on active roster: {hitter_raw}"}

    # Reject unknown reporters
    reporter = REPORTER_BY_HANDLE.get(reporter_handle.lower())
    if not reporter:
        return {**base, "reject_reason": f"unknown reporter handle: {reporter_handle}"}

    team = reporter["team"]
    team_id = MLB_TEAM_IDS.get(team, 0)

    return {
        "passed": True,
        "tweet_id": tweet_id,
        "reporter_handle": reporter_handle,
        "team": team,
        "team_id": team_id,
        "pinch_hitter_raw": hitter_raw,
        "pinch_hitter_normalized": normalize_player_last_name(hitter_raw),
        "replaced_player": replaced,
    }
