import json
import logging
import os
import time
from typing import Any

import httpx

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


_ADD_RULE_URL = "https://api.twitterapi.io/oapi/tweet_filter/add_rule"
_UPDATE_RULE_URL = "https://api.twitterapi.io/oapi/tweet_filter/update_rule"
_GET_RULES_URL = "https://api.twitterapi.io/oapi/tweet_filter/get_rules"
_DELETE_RULE_URL = "https://api.twitterapi.io/oapi/tweet_filter/delete_rule"

_MAX_RULE_CHARS = 255
_client: httpx.AsyncClient | None = None


def _headers() -> dict[str, str]:
    return {"x-api-key": os.environ.get("TWITTERAPI_IO_KEY", "")}


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

    # Cleanup is best-effort. Registration failure is fatal because no rules means
    # twitterapi.io will not deliver matching webhook payloads.
    try:
        r = await client.get(_GET_RULES_URL, headers=headers)
        r.raise_for_status()
        existing = r.json().get("rules", [])
        if existing:
            for rule in existing:
                rule_id = rule.get("rule_id") or rule.get("id")
                if rule_id:
                    del_r = await client.request(
                        "DELETE", _DELETE_RULE_URL, headers=headers, json={"rule_id": rule_id}
                    )
                    del_r.raise_for_status()
            logger.info("deleted %s existing rule(s)", len(existing))
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            logger.error("auth failure during rule cleanup — API key may be invalid: %s", e)
            raise
        logger.error("rule cleanup failed (non-auth): %s", e)
    except (httpx.HTTPError, ValueError):
        logger.exception("rule cleanup error")

    phrase_rule = " OR ".join(f'"{p}"' for p in PINCH_HIT_PHRASES)
    reporter_rules = _build_reporter_rules()

    rules_to_add = [
        {"value": rule, "tag": f"reporters_{chr(ord('a') + i)}", "interval_seconds": 100}
        for i, rule in enumerate(reporter_rules)
    ]
    rules_to_add.append({"value": phrase_rule, "tag": "phrases", "interval_seconds": 100})

    try:
        for rule in rules_to_add:
            logger.info("adding rule: %s", rule)
            r = await client.post(_ADD_RULE_URL, headers=headers, json=rule)
            if r.status_code >= 400:
                logger.error("add_rule failed: status=%s body=%s", r.status_code, r.text)
            r.raise_for_status()
            rule_id = r.json().get("rule_id")
            if not rule_id:
                logger.error("add_rule response missing rule_id: %s", r.text)
                raise ValueError("add_rule response missing rule_id")

            # Rules default to inactive — activate via update_rule with is_effect=1.
            activate_r = await client.post(
                _UPDATE_RULE_URL,
                headers=headers,
                json={
                    "rule_id": rule_id,
                    "tag": rule["tag"],
                    "value": rule["value"],
                    "interval_seconds": rule["interval_seconds"],
                    "is_effect": 1,
                },
            )
            if activate_r.status_code >= 400:
                logger.error("update_rule failed: status=%s body=%s", activate_r.status_code, activate_r.text)
            activate_r.raise_for_status()
            logger.info("activated rule %s (tag=%s)", rule_id, rule["tag"])

        logger.info("filter rules registered and activated: %d rule(s)", len(rules_to_add))
    except (httpx.HTTPError, ValueError):
        logger.exception("rule registration failed")
        raise  # Fatal — twitterapi.io will not deliver webhooks without rules


async def _handle_message(data: dict[str, Any], client: httpx.AsyncClient) -> None:
    tweet_id = str(data.get("id", ""))
    text = data.get("text", "")
    created_at = data.get("created_at") or data.get("createdAt")
    reporter_handle = data.get("author", {}).get("username", "")

    if not tweet_id or not text or not reporter_handle:
        logger.warning(
            "dropping message: missing fields id=%s text=%s handle=%s",
            bool(tweet_id),
            bool(text),
            bool(reporter_handle),
        )
        return

    # Update heartbeat AFTER validation so dropped messages don't mask broken delivery.
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


async def init_twitter() -> None:
    """Initialize shared HTTP client and register twitterapi.io filter rules."""
    global _client

    await refresh_if_stale()

    client = httpx.AsyncClient(timeout=15)
    try:
        await _register_rules(client)
    except Exception:
        await client.aclose()
        raise

    _client = client


async def close_twitter() -> None:
    """Close shared HTTP client created by init_twitter()."""
    global _client

    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            logger.exception("error closing twitter client")
        finally:
            _client = None


async def handle_webhook_post(body: bytes) -> bool:
    """Process a twitterapi.io webhook payload. Returns False on parse failure."""
    if _client is None:
        raise RuntimeError("twitter client is not initialized")

    try:
        payload: Any = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error("malformed webhook body: %s raw_prefix=%r", e, body[:200])
        return False

    # twitterapi.io webhook format: {"event_type":"tweet","tweets":[...],...}
    # Extract the tweets array; fall back to treating payload as a list or single tweet.
    if isinstance(payload, dict) and "tweets" in payload:
        tweets = payload["tweets"]
    elif isinstance(payload, list):
        tweets = payload
    else:
        tweets = [payload]

    for tweet in tweets:
        if not isinstance(tweet, dict):
            logger.warning("dropping webhook item: expected object got=%s", type(tweet).__name__)
            continue
        try:
            await _handle_message(tweet, _client)
        except Exception:
            logger.exception("failed to process webhook message id=%s", tweet.get("id", "?"))

    return True
