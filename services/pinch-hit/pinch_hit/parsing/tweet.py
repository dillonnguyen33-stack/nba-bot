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
PINCH_HIT_PHRASES = {"pinch hit", "pinch-hit", "ph for", "pinch hitting", "pinch-hitting"}

REPORTER_BY_HANDLE: dict[str, Reporter] = {r["handle"].lower(): r for r in REPORTERS}

player_team_map: dict[str, str] = {}
last_roster_refresh: float = 0.0
_refresh_lock = asyncio.Lock()


_REJECT_SIGNALS = [
    "pinched",
    "homered",
    "singled",
    "doubled",
    "tripled",
    "grounded out",
    "flied out",
    "lined out",
    "struck out",
    "popped out",
    "drove in",
    "drove home",
    "home run",
    "last night", "yesterday",
    "college", "university", "high school", "ncaa",
    "minor league", "minors", "triple-a", "double-a",
    "softball", "little league",
]


def has_reject_signal(text: str) -> tuple[bool, str]:
    tl = text.lower()
    for signal in _REJECT_SIGNALS:
        if signal in tl:
            return True, signal
    return False, ""


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
        logger.critical("all %s teams returned empty; keeping stale roster", empty_count)
        return
    player_team_map = new_map
    last_roster_refresh = time.time()
    if empty_count > 5:
        logger.warning("%s/%s teams returned empty rosters; possible API issue", empty_count, len(MLB_TEAM_IDS))
    logger.info("%s roster entries loaded", len(new_map))


async def refresh_if_stale() -> None:
    if time.time() - last_roster_refresh >= 21600 or not player_team_map:
        async with _refresh_lock:
            if time.time() - last_roster_refresh >= 21600 or not player_team_map:
                await build_player_team_map()


def lookup_player_team(name: str) -> str | None:
    if not name or not player_team_map:
        return None
    return player_team_map.get(name.lower().strip())


def resolve_last_name_on_team(last_name: str, team: str) -> str | None:
    """Given a bare last name and a known team, find the full roster name."""
    if not last_name or not player_team_map:
        return None
    target = last_name.lower().strip()
    for name, t in player_team_map.items():
        if t == team and " " in name and name.split()[-1] == target:
            return name
    return None


def normalize_player_last_name(name: str) -> str:
    cleaned = re.sub(r"[^\w\s'-]", "", name)
    return normalize_last_name(cleaned)


_TWITTER_DATE_FMT = "%a %b %d %H:%M:%S %z %Y"


def is_recent(created_at_str: str) -> bool:
    try:
        tweet_time = datetime.fromisoformat(created_at_str)
    except ValueError:
        try:
            tweet_time = datetime.strptime(created_at_str, _TWITTER_DATE_FMT)
        except ValueError:
            # Fail-open: unparseable timestamps pass through so we don't silently
            # drop legitimate alerts due to an unexpected date format.
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

    # Last-name-only fallback: one or both names may be a single capitalized word.
    # Only match when at least one name is extractable.
    _NAME = r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)'
    mixed_patterns = [
        rf'{_NAME}\s+(?:(?:is|will)\s+)?(?:on\s+deck\s+to\s+)?pinch[- ]hit(?:ting)?\s+for\s+{_NAME}',
        rf'{_NAME}\s+(?:will\s+)?(?:bat|hit|ph(?:ing)?)\s+for\s+{_NAME}',
    ]
    for p in mixed_patterns:
        m = re.search(p, clean)
        if m:
            return m.group(1).strip(), m.group(2).strip()

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

    if created_at is None:
        return {**base, "reject_reason": "missing timestamp"}

    if not is_recent(created_at):
        return {**base, "reject_reason": "tweet too old"}

    if await is_seen_fn(tweet_id):
        return {**base, "reject_reason": "duplicate tweet_id"}

    tl = text.lower()
    if not any(phrase in tl for phrase in PINCH_HIT_PHRASES):
        return {**base, "reject_reason": "no pinch-hit phrase"}

    past, matched_signal = has_reject_signal(text)
    if past:
        logger.info(
            "tweet %s rejected: past-tense signal %r — %s",
            tweet_id, matched_signal, text[:100],
        )
        return {**base, "reject_reason": f"past-tense: {matched_signal}"}

    hitter_raw, replaced = extract_players(text)
    if not hitter_raw:
        return {**base, "reject_reason": "no player name extracted"}

    # Try direct roster lookup on both names
    hitter_team = lookup_player_team(hitter_raw)
    replaced_team = lookup_player_team(replaced) if replaced else None

    # Cross-reference: if one name is full and on the roster, use their team
    # to resolve the other name as a last-name-only match.
    if not hitter_team and replaced_team:
        resolved = resolve_last_name_on_team(hitter_raw, replaced_team)
        if resolved:
            hitter_raw = resolved
            hitter_team = replaced_team
    elif hitter_team and replaced and not replaced_team:
        resolved = resolve_last_name_on_team(replaced, hitter_team)
        if resolved:
            replaced = resolved

    if not hitter_team:
        return {**base, "reject_reason": f"not on active roster: {hitter_raw}"}

    reporter = REPORTER_BY_HANDLE.get(reporter_handle.lower())
    team = reporter["team"] if reporter else hitter_team
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
