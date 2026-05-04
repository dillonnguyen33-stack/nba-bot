import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from pinch_hit.alerts.discord import build_green_embed, delete_message, post_initial_alert
from pinch_hit.alerts.odds import schedule_odds_fetch
from pinch_hit.config import REPORTERS
from pinch_hit.eval.logger import log_event
from pinch_hit.parsing import TweetResult
from pinch_hit.parsing.tweet import (
    PINCH_HIT_PHRASES,
    REPORTER_BY_HANDLE,
    process_tweet,
    refresh_if_stale,
)
from pinch_hit.state.repository import insert_pending_alert, is_tweet_seen, try_claim_tweet
from pinch_hit.state.runtime import runtime_state

logger = logging.getLogger(__name__)

_PLAYER_COOLDOWN_SEC = 7200
_PLAYER_MAX_ALERTS = 2
_player_alert_count: dict[str, int] = {}
_player_alert_time: dict[str, float] = {}


def _is_player_on_cooldown(name: str) -> bool:
    if not name:
        return False
    key = name.lower().strip()
    now = time.time()
    if key in _player_alert_time and now - _player_alert_time[key] > _PLAYER_COOLDOWN_SEC:
        del _player_alert_count[key]
        del _player_alert_time[key]
        return False
    return _player_alert_count.get(key, 0) >= _PLAYER_MAX_ALERTS


def _record_player_alert(name: str) -> None:
    if not name:
        return
    key = name.lower().strip()
    _player_alert_count[key] = _player_alert_count.get(key, 0) + 1
    _player_alert_time[key] = time.time()


# twitterapi.io uses X-API-Key header auth, not Bearer token.
_RULES_URL = "https://api.twitterapi.io/twitter/tweet/search/stream/rule"
_STREAM_WSS = "wss://stream.twitterapi.io/v1/tweet/search"

_MAX_RULE_CHARS = 512


def _headers() -> dict[str, str]:
    return {"X-API-Key": os.environ.get("TWITTERAPI_IO_KEY", "")}


def _build_reporter_rules() -> list[str]:
    """Split reporter handles into rules that each fit under _MAX_RULE_CHARS."""
    clauses = [f"from:{r['handle']}" for r in REPORTERS]
    rules: list[str] = []
    current: list[str] = []
    current_len = 0

    for clause in clauses:
        added_len = len(clause) + (4 if current else 0)
        if current and current_len + added_len > _MAX_RULE_CHARS:
            rules.append(" OR ".join(current))
            current = [clause]
            current_len = len(clause)
        else:
            current.append(clause)
            current_len += added_len

    if current:
        rules.append(" OR ".join(current))
    return rules


async def _register_rules(client: httpx.AsyncClient) -> None:
    headers = _headers()

    # Cleanup is best-effort — stale rules cause extra stream volume but don't break
    # processing. Registration failure is fatal (no rules = no stream data).
    try:
        r = await client.get(_RULES_URL, headers=headers)
        r.raise_for_status()
        existing = r.json().get("data", [])
        if existing:
            ids = [item["id"] for item in existing]
            del_r = await client.request("DELETE", _RULES_URL, headers=headers, json={"ids": ids})
            del_r.raise_for_status()
            logger.info("deleted %s existing rule(s)", len(ids))
    except (httpx.HTTPError, ValueError):
        logger.exception("rule cleanup error")

    phrase_rule = " OR ".join(f'"{p}"' for p in PINCH_HIT_PHRASES)
    reporter_rules = _build_reporter_rules()

    rules_to_add = [
        {"value": rule, "tag": f"reporters_{chr(ord('a') + i)}"}
        for i, rule in enumerate(reporter_rules)
    ]
    rules_to_add.append({"value": phrase_rule, "tag": "phrases"})

    try:
        r = await client.post(_RULES_URL, headers=headers, json={"add": rules_to_add})
        r.raise_for_status()
        logger.info("filter rules registered: %s", r.json())
    except (httpx.HTTPError, ValueError):
        logger.exception("rule registration failed")
        raise  # Fatal — cannot consume stream without rules


def _extract_reporter_handle(data: dict[str, Any], tweet: dict[str, Any]) -> str:
    """Extract author username with fallback chain."""
    users = (data.get("includes") or {}).get("users", [])
    if users:
        username = users[0].get("username", "")
        if username:
            return username
    return tweet.get("author", {}).get("username", "")


async def _handle_message(raw: str, client: httpx.AsyncClient) -> None:
    try:
        data: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("malformed JSON from stream: %s raw_prefix=%s", e, raw[:200])
        return

    # twitterapi.io wraps the tweet object in a "data" key; fall back to top-level
    tweet = data.get("data") or data
    tweet_id = str(tweet.get("id", ""))
    text = tweet.get("text", "")
    created_at = tweet.get("created_at")
    reporter_handle = _extract_reporter_handle(data, tweet)

    if not tweet_id or not text or not reporter_handle:
        logger.warning(
            "dropping message: missing fields id=%s text=%s handle=%s",
            bool(tweet_id),
            bool(text),
            bool(reporter_handle),
        )
        return

    # Update heartbeat AFTER validation so dropped messages don't mask a broken stream
    runtime_state.last_twitter_message_at = time.time()

    log_event(
        "tweet_received",
        source="twitter",
        raw_payload={"tweet_id": tweet_id, "text": text, "reporter_handle": reporter_handle},
    )

    await refresh_if_stale()

    result: TweetResult = await process_tweet(
        tweet_id=tweet_id,
        text=text,
        created_at=created_at,
        reporter_handle=reporter_handle,
        is_seen_fn=is_tweet_seen,
    )

    if not result["passed"]:
        log_event(
            "tweet_rejected",
            source="twitter",
            raw_payload={"tweet_id": tweet_id, "reason": result["reject_reason"], "text": text},
        )
        return

    # Player cooldown — prevent spam on the same player.
    # Record optimistically before awaiting side effects to close the race window.
    key_player = result["pinch_hitter_raw"]
    if _is_player_on_cooldown(key_player):
        logger.info("player %s on cooldown; skipping", key_player)
        return
    _record_player_alert(key_player)

    reporter_info = REPORTER_BY_HANDLE.get(reporter_handle.lower())
    reporter_display = reporter_info["handle"] if reporter_info else reporter_handle

    log_event(
        "tweet_parsed",
        source="twitter",
        pinch_hitter=result["pinch_hitter_raw"],
        team_id=result["team_id"],
        raw_payload={"tweet_id": tweet_id, "text": text},
    )

    # Discord POST first: a duplicate alert is recoverable, a lost alert is not.
    message_id = await post_initial_alert(
        pinch_hitter=result["pinch_hitter_raw"],
        team=result["team"],
        tweet_text=text,
        reporter=reporter_display,
        client=client,
    )

    if not message_id:
        logger.error("Discord post failed for tweet %s; skipping DB insert", tweet_id)
        return

    try:
        claimed = await try_claim_tweet(tweet_id)
    except Exception:
        logger.exception("failed to claim tweet %s", tweet_id)
        if not await delete_message(message_id, client):
            logger.critical("ORPHANED ALERT: message %s has no DB row and could not be deleted", message_id)
        raise
    if not claimed:
        logger.info("duplicate tweet %s; deleting duplicate Discord alert", tweet_id)
        if not await delete_message(message_id, client):
            logger.error("ORPHANED DUPLICATE: message %s for tweet %s could not be deleted", message_id, tweet_id)
        return

    base_embed = build_green_embed(
        pinch_hitter=result["pinch_hitter_raw"],
        team=result["team"],
        tweet_text=text,
        reporter=reporter_display,
        include_odds_placeholder=bool(os.environ.get("ODDS_API_KEY", "")),
    )

    try:
        await insert_pending_alert(
            discord_message_id=message_id,
            pinch_hitter_raw=result["pinch_hitter_raw"],
            pinch_hitter_normalized=result["pinch_hitter_normalized"],
            team_id=result["team_id"],
            tweet_id=tweet_id,
            replaced_player=result["replaced_player"],
        )
    except Exception:
        logger.exception("DB insert failed for tweet %s; deleting Discord alert", tweet_id)
        if not await delete_message(message_id, client):
            logger.critical("ORPHANED ALERT: message %s has no DB row and could not be deleted", message_id)
        raise

    schedule_odds_fetch(
        player_last_name=result["pinch_hitter_raw"].split()[-1],
        message_id=message_id,
        base_embed=base_embed,
    )

    log_event(
        "alert_fired",
        source="twitter",
        pinch_hitter=result["pinch_hitter_raw"],
        team_id=result["team_id"],
        raw_payload={"tweet_id": tweet_id, "text": text},
    )

    logger.info("alert fired: %s (%s)", result["pinch_hitter_raw"], result["team"])


async def twitter_consumer() -> None:
    """
    Persistent WebSocket consumer. Runs forever as an asyncio task.
    Uses websockets built-in reconnection with exponential backoff.
    """
    await refresh_if_stale()

    async with httpx.AsyncClient(timeout=15) as client:
        await _register_rules(client)

        async for ws in connect(
            _STREAM_WSS,
            additional_headers=_headers(),
            open_timeout=10,
            ping_interval=30,
        ):
            try:
                logger.info("WebSocket connected")
                async for message in ws:
                    await _handle_message(str(message), client)
            except ConnectionClosed:
                logger.info("connection lost, reconnecting")
                continue
            except Exception:
                logger.exception("unexpected error in message loop")
                await asyncio.sleep(1)
                continue
