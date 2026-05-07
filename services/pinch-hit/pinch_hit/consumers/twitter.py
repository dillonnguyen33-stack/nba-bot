import asyncio
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError

from pinch_hit.alerts.discord import delete_message, post_initial_alert, post_startup_ping, post_unfiltered_alert
from pinch_hit.eval.logger import log_event
from pinch_hit.parsing import TweetResult
from pinch_hit.parsing.tweet import (
    PINCH_HIT_PHRASES,
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
_last_cooldown_sweep: float = 0.0

_SEEN_CACHE_MAX = 500
_seen_tweet_ids: set[str] = set()
_seen_tweet_order: deque[str] = deque()


def _sweep_expired_cooldowns() -> None:
    global _last_cooldown_sweep
    now = time.time()
    if now - _last_cooldown_sweep < _PLAYER_COOLDOWN_SEC:
        return
    expired = [k for k, t in _player_alert_time.items() if now - t > _PLAYER_COOLDOWN_SEC]
    for k in expired:
        _player_alert_count.pop(k, None)
        _player_alert_time.pop(k, None)
    _last_cooldown_sweep = now


def _is_player_on_cooldown(name: str) -> bool:
    if not name:
        return False
    _sweep_expired_cooldowns()
    key = name.lower().strip()
    now = time.time()
    if key in _player_alert_time and now - _player_alert_time[key] > _PLAYER_COOLDOWN_SEC:
        _player_alert_count.pop(key, None)
        _player_alert_time.pop(key, None)
        return False
    return _player_alert_count.get(key, 0) >= _PLAYER_MAX_ALERTS


def _record_player_alert(name: str) -> None:
    if not name:
        return
    key = name.lower().strip()
    _player_alert_count[key] = _player_alert_count.get(key, 0) + 1
    if key not in _player_alert_time:
        _player_alert_time[key] = time.time()


_ADD_RULE_URL = "https://api.twitterapi.io/oapi/tweet_filter/add_rule"
_UPDATE_RULE_URL = "https://api.twitterapi.io/oapi/tweet_filter/update_rule"
_GET_RULES_URL = "https://api.twitterapi.io/oapi/tweet_filter/get_rules"
_DELETE_RULE_URL = "https://api.twitterapi.io/oapi/tweet_filter/delete_rule"

_client: httpx.AsyncClient | None = None
_api_key: str = ""


def _headers() -> dict[str, str]:
    return {"x-api-key": _api_key}


def _is_rule_active(rule: dict[str, Any]) -> bool:
    return rule.get("is_effect") in (1, True)


async def _verify_rule_active(client: httpx.AsyncClient, headers: dict[str, str], rule_id: str) -> None:
    r = await client.get(_GET_RULES_URL, headers=headers)
    if r.status_code >= 400:
        logger.error("get_rules verification failed: status=%s body=%s", r.status_code, r.text)
    r.raise_for_status()

    rules = r.json().get("rules", [])
    rule = next(
        (
            candidate
            for candidate in rules
            if str(candidate.get("rule_id") or candidate.get("id") or "") == rule_id
        ),
        None,
    )
    if not rule:
        raise ValueError(f"rule {rule_id} missing after activation")
    if not _is_rule_active(rule):
        raise ValueError(f"rule {rule_id} did not activate (is_effect={rule.get('is_effect')!r})")

    logger.info("verified rule %s active with is_effect=%r", rule_id, rule.get("is_effect"))


async def _register_rules(client: httpx.AsyncClient) -> None:
    headers = _headers()

    cleanup_failed = False
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
        cleanup_failed = True
    except (httpx.HTTPError, ValueError):
        logger.exception("rule cleanup error")
        cleanup_failed = True

    if cleanup_failed:
        logger.warning("stale filter rules may remain active; adding new rule on top")

    phrase_rule = " OR ".join(f'"{p}"' for p in PINCH_HIT_PHRASES)
    rule_payload = {"value": phrase_rule, "tag": "phrases", "interval_seconds": 5}

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
        await _verify_rule_active(client, headers, str(rule_id))
        logger.info("activated rule %s (tag=phrases)", rule_id)
        logger.info("filter rules registered and activated: 1 rule")
    except (httpx.HTTPError, ValueError):
        logger.exception("rule registration failed")
        raise


def _normalize_ws_tweet(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize flat WS payloads to nested format. No-ops for REST/test payloads."""
    data = dict(raw)

    if "screen_name" in data and "author" not in data:
        author: dict[str, Any] = {"username": data.pop("screen_name")}
        if "display_name" in data:
            author["name"] = data.pop("display_name")
        if "user_id" in data:
            author["id"] = data.pop("user_id")
        data["author"] = author

    if "created_ms" in data and "created_at" not in data and "createdAt" not in data:
        ms = data.pop("created_ms")
        if isinstance(ms, (int, float)):
            dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            data["created_at"] = dt.isoformat()

    return data


def _get_nested_str(data: dict[str, Any], key: str, nested_key: str) -> str:
    value = data.get(key)
    if not isinstance(value, dict):
        return ""
    nested = value.get(nested_key)
    return nested if isinstance(nested, str) else ""


def _extract_reporter_handle(data: dict[str, Any]) -> str:
    # After _normalize_ws_tweet, all payloads use nested author object.
    for obj_key, field_key in (("author", "username"), ("author", "userName"), ("user", "screen_name")):
        handle = _get_nested_str(data, obj_key, field_key)
        if handle:
            return handle
    return ""


async def _handle_message(data: dict[str, Any], client: httpx.AsyncClient) -> None:
    raw_id = data.get("id")
    tweet_id = str(raw_id) if raw_id else ""

    if tweet_id and tweet_id in _seen_tweet_ids:
        logger.info("skipping duplicate tweet %s (in-memory cache)", tweet_id)
        return

    logger.debug("raw tweet payload: %s", json.dumps(data, sort_keys=True, default=str))

    text = data.get("text", "")
    created_at = data.get("created_at") or data.get("createdAt")
    reporter_handle = _extract_reporter_handle(data)
    if not created_at:
        logger.warning("tweet %s has no timestamp field", tweet_id or "<missing id>")

    if not tweet_id or not text or not reporter_handle:
        logger.warning(
            "dropping message: missing fields id=%s text=%s handle=%s",
            bool(tweet_id),
            bool(text),
            bool(reporter_handle),
        )
        return

    runtime_state.last_twitter_message_at = time.time()

    _seen_tweet_ids.add(tweet_id)
    _seen_tweet_order.append(tweet_id)
    if len(_seen_tweet_order) > _SEEN_CACHE_MAX:
        _seen_tweet_ids.discard(_seen_tweet_order.popleft())

    log_event(
        "tweet_received",
        source="twitter",
        raw_payload={"tweet_id": tweet_id, "text": text, "reporter_handle": reporter_handle},
    )

    # Debug visibility: fire every phrase match before filtering so operators can audit rejections
    tl = text.lower()
    if any(phrase in tl for phrase in PINCH_HIT_PHRASES):
        await post_unfiltered_alert(tweet_text=text, reporter_handle=reporter_handle, client=client)

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
        reporter_handle=reporter_handle,
        tweet_id=tweet_id,
        replaced_player=result["replaced_player"],
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


async def _handle_tweet_batch(event_type: str, msg: dict[str, Any], client: httpx.AsyncClient) -> None:
    tweets = msg.get("tweets", [])

    # fast_tweet may deliver a single tweet at the top level (no tweets[] array)
    if not tweets and event_type == "fast_tweet" and "id" in msg and "text" in msg:
        envelope_keys = {"event_type", "tweets", "rule_id", "rule_tag"}
        tweet_data = {k: v for k, v in msg.items() if k not in envelope_keys}
        tweets = [tweet_data]
        logger.info("fast_tweet: extracted single tweet from top-level fields (id=%s)", msg.get("id"))
    elif event_type == "fast_tweet":
        other_fields = {key: value for key, value in msg.items() if key != "tweets"}
        logger.info(
            "fast_tweet payload received: tweets_count=%s other_fields=%s",
            len(tweets) if isinstance(tweets, list) else "non-list",
            other_fields,
        )
        logger.debug("fast_tweet tweets payload: %s", json.dumps(tweets, sort_keys=True, default=str))

    if not isinstance(tweets, list):
        logger.warning("%s event has non-list tweets payload: %r", event_type, tweets)
        return

    for tweet in tweets:
        if not isinstance(tweet, dict):
            logger.warning("%s event has non-dict tweet payload: %r", event_type, tweet)
            continue
        try:
            normalized = _normalize_ws_tweet(tweet)
            await _handle_message(normalized, client)
        except Exception:
            logger.exception("failed to process WS %s tweet id=%s", event_type, tweet.get("id", "?"))


async def init_twitter() -> None:
    global _client, _api_key

    _api_key = os.environ.get("TWITTERAPI_IO_KEY", "")
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

    extra_headers = {"x-api-key": _api_key}
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
                            startup_pinged = await post_startup_ping(client)

                    elif event_type == "ping":
                        runtime_state.last_twitter_message_at = time.time()

                    elif event_type in ("tweet", "fast_tweet"):
                        await _handle_tweet_batch(event_type, msg, client)

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
