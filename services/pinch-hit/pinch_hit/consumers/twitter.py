import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError

from pinch_hit.alerts.discord import build_green_embed, delete_message, post_initial_alert, post_startup_ping
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

_WS_URL = "wss://ws.twitterapi.io/twitter/tweet/websocket"
_WS_RECONNECT_DELAY = 90.0

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

_client: httpx.AsyncClient | None = None


def _headers() -> dict[str, str]:
    return {"x-api-key": os.environ.get("TWITTERAPI_IO_KEY", "")}


async def _register_rules(client: httpx.AsyncClient) -> None:
    headers = _headers()

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
    rule_payload = {"value": phrase_rule, "tag": "phrases", "interval_seconds": 100}

    try:
        logger.info("adding rule: %s", rule_payload)
        r = await client.post(_ADD_RULE_URL, headers=headers, json=rule_payload)
        if r.status_code >= 400:
            logger.error("add_rule failed: status=%s body=%s", r.status_code, r.text)
        r.raise_for_status()
        rule_id = r.json().get("rule_id")
        if not rule_id:
            logger.error("add_rule response missing rule_id: %s", r.text)
            raise ValueError("add_rule response missing rule_id")

        activate_r = await client.post(
            _UPDATE_RULE_URL,
            headers=headers,
            json={**rule_payload, "rule_id": rule_id, "is_effect": 1},
        )
        if activate_r.status_code >= 400:
            logger.error("update_rule failed: status=%s body=%s", activate_r.status_code, activate_r.text)
        activate_r.raise_for_status()
        logger.info("activated rule %s (tag=phrases)", rule_id)
        logger.info("filter rules registered and activated: 1 rule")
    except (httpx.HTTPError, ValueError):
        logger.exception("rule registration failed")
        raise


def _get_nested_str(data: dict[str, Any], key: str, nested_key: str) -> str:
    value = data.get(key)
    if not isinstance(value, dict):
        return ""
    nested = value.get(nested_key)
    return nested if isinstance(nested, str) else ""


def _extract_reporter_handle(data: dict[str, Any]) -> str:
    for handle in (
        _get_nested_str(data, "author", "username"),
        _get_nested_str(data, "user", "screen_name"),
        _get_nested_str(data, "author", "userName"),
        _get_nested_str(data, "author", "handle"),
    ):
        if handle:
            return handle
    return ""


async def _handle_message(data: dict[str, Any], client: httpx.AsyncClient) -> None:
    logger.debug("raw tweet payload: %s", json.dumps(data, sort_keys=True, default=str))

    tweet_id = str(data.get("id", ""))
    text = data.get("text", "")
    created_at = data.get("created_at") or data.get("createdAt")
    reporter_handle = _extract_reporter_handle(data)

    if not tweet_id or not text or not reporter_handle:
        logger.warning(
            "dropping message: missing fields id=%s text=%s handle=%s",
            bool(tweet_id),
            bool(text),
            bool(reporter_handle),
        )
        return

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

    log_event(
        "alert_fired",
        source="twitter",
        pinch_hitter=result["pinch_hitter_raw"],
        team_id=result["team_id"],
        raw_payload={"tweet_id": tweet_id, "text": text},
    )

    logger.info("alert fired: %s (%s)", result["pinch_hitter_raw"], result["team"])


async def init_twitter() -> None:
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
    global _client

    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            logger.exception("error closing twitter client")
        finally:
            _client = None


async def twitter_consumer() -> None:
    client = _client
    if client is None:
        raise RuntimeError("twitter client is not initialized")

    api_key = os.environ.get("TWITTERAPI_IO_KEY", "")
    extra_headers = {"x-api-key": api_key}
    startup_pinged = False

    while True:
        try:
            async with connect(_WS_URL, additional_headers=extra_headers) as ws:
                logger.info("WebSocket connected to %s", _WS_URL)
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        logger.warning("non-JSON WebSocket message: %r", raw[:200] if isinstance(raw, (str, bytes)) else raw)
                        continue

                    event_type = msg.get("event_type", "")

                    if event_type == "connected":
                        logger.info("WebSocket handshake confirmed: %s", msg.get("message", ""))
                        runtime_state.last_twitter_message_at = time.time()
                        if not startup_pinged:
                            await post_startup_ping(client)
                            startup_pinged = True

                    elif event_type == "ping":
                        runtime_state.last_twitter_message_at = time.time()

                    elif event_type == "tweet":
                        tweets = msg.get("tweets", [])
                        for tweet in tweets:
                            if not isinstance(tweet, dict):
                                continue
                            try:
                                await _handle_message(tweet, client)
                            except Exception:
                                logger.exception("failed to process WS tweet id=%s", tweet.get("id", "?"))

                    elif event_type == "fast_tweet":
                        logger.debug("fast_tweet event (stream-only); skipping")

                    else:
                        logger.debug("unhandled WS event_type=%s", event_type)

        except asyncio.CancelledError:
            raise
        except ConnectionClosedError as e:
            delay = 60.0 if e.rcvd and e.rcvd.code == 1013 else _WS_RECONNECT_DELAY
            logger.warning(
                "WebSocket closed code=%s reason=%s; reconnecting in %.0fs",
                e.rcvd.code if e.rcvd else None,
                e.rcvd.reason if e.rcvd else "",
                delay,
            )
            await asyncio.sleep(delay)
        except Exception:
            logger.exception("WebSocket error; reconnecting in %.0fs", _WS_RECONNECT_DELAY)
            await asyncio.sleep(_WS_RECONNECT_DELAY)
