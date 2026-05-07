import logging
import re
from collections.abc import Awaitable, Callable
from typing import Literal, TypedDict

from pinch_hit.config import REPORTERS, Reporter
from pinch_hit.parsing.names import normalize_last_name

logger = logging.getLogger(__name__)

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


def normalize_player_last_name(name: str) -> str:
    cleaned = re.sub(r"[^\w\s'-]", "", name)
    return normalize_last_name(cleaned)



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

    if await is_seen_fn(tweet_id):
        return {**base, "reject_reason": "duplicate tweet_id"}

    # Phrase pre-filter (client-side mirror of the "phrases" filter rule)
    tl = text.lower()
    if not any(phrase in tl for phrase in PINCH_HIT_PHRASES):
        return {**base, "reject_reason": "no pinch-hit phrase"}

    # Best-effort player/team extraction (non-blocking for POC)
    hitter_raw, replaced = extract_players(text)
    reporter = REPORTER_BY_HANDLE.get(reporter_handle.lower())
    team = reporter["team"] if reporter else "Unknown"
    team_id = MLB_TEAM_IDS.get(team, 0)

    return {
        "passed": True,
        "tweet_id": tweet_id,
        "reporter_handle": reporter_handle,
        "team": team,
        "team_id": team_id,
        "pinch_hitter_raw": hitter_raw or "Unknown",
        "pinch_hitter_normalized": normalize_player_last_name(hitter_raw) if hitter_raw else "",
        "replaced_player": replaced,
    }
