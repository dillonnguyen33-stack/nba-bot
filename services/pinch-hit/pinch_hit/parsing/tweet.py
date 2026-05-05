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

PRESENT_FUTURE_MARKERS = [
    r'\bis\b', r'\bwill\b', r'\bslated\b', r'\bon\s+deck\b',
    r'\bexpected\b', r'\bgoing\s+to\b', r'\bset\s+to\b',
    r'\bcoming\s+up\b', r'\bheading\b', r'\bwarming\b',
    r'\bscheduled\b', r'\bdue\s+to\b', r'\bappears\b',
    r'\blooks\s+like\b', r'\bunclear\b',
    r'\btaking\s+over\b', r'\bcoming\s+in\b',
    r'\bout\s+of\s+the\s+game\b', r'\bleft\s+the\s+game\b',
]

REJECT_PHRASES = [
    # Past tense / results
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
    # Opinion / complaint
    "why pinch hit", "why would", "should have", "shouldn't have",
    "should not have", "bad decision", "bad manager", "terrible decision",
    "doesn't make sense", "makes no sense", "i hate when",
    "can't believe", "cannot believe", "questionable",
    "what a waste", "poor decision", "wrong decision",
    "never should", "he keeps", "keeps making", "mistake",
    "would you pinch", "if i were", "hypothetically",
    "in theory", "imagine if", "what if",
    "how often", "it's funny", "its funny", "funny how",
    "rewards players", "punish", "not to blame",
    "i would have", "i would not", "i wouldn't",
    "unless he", "unless they", "unless the",
    # Hypotheticals / predictions
    "i predict", "i would probably", "i'd probably", "i'd pinch",
    "always gets pinch", "routinely being", "routinely pinch",
    "they tend to", "they usually", "he usually", "he always",
    "probably pinch hit", "likely pinch hit", "might pinch hit",
    "could pinch hit", "would pinch hit", "may pinch hit",
    "i think", "bet they", "bet he",
    "tomorrow", "next game", "next at bat", "next time",
    "i really", "really expected", "i expected",
    "my expectations", "for the record",
    # Questions / complaints
    "how is he not", "why is he not", "how is she not",
    "not in the lineup", "not starting", "shouldn't be",
    "how does", "why does", "why do they",
    "?)",
    # College / non-MLB
    "mississippi state", "mississippi st", "husker",
    "college", "university", "high school",
    "ncaa", "minor league", "minors", "triple-a", "triple a", "double-a",
    "farm team", "prospect", "affiliate",
    "softball", "little league",
]

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

    # "X left the game...Y will pinch" — group(1) is replaced, group(2) is hitter (inverted)
    m = re.search(
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:has\s+)?left\s+the\s+game.{0,100}?([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?pinch',
        clean,
    )
    if m:
        return m.group(2).strip(), m.group(1).strip()

    # "X is getting pinch hit for" — X is the replaced player, hitter unknown
    m = re.search(
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?(?:getting\s+)?pinch[- ]hit\s+for',
        clean,
    )
    if m:
        return None, m.group(1).strip()

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

    ok, reason = is_present_future(text)
    if not ok:
        return {**base, "reject_reason": reason}

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
