import asyncio
import json
import os
import time
from typing import Any

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from pinch_hit.alerts.discord import build_green_embed, post_initial_alert
from pinch_hit.alerts.odds import schedule_odds_fetch
from pinch_hit.config.reporters import REPORTERS
from pinch_hit.eval.logger import log_event
from pinch_hit.parsing.tweet import (
    PINCH_HIT_PHRASES,
    REPORTER_BY_HANDLE,
    build_player_team_map,
    process_tweet,
    refresh_if_stale,
)
from pinch_hit.state.repository import insert_pending_alert, insert_seen_tweet, is_tweet_seen

# ── CONFIG ────────────────────────────────────────────────────────────────────

# twitterapi.io uses X-API-Key header auth, not Bearer token.
_RULES_URL = "https://api.twitterapi.io/twitter/tweet/search/stream/rule"
_STREAM_WSS = "wss://stream.twitterapi.io/v1/tweet/search"

_MAX_RULE_CHARS = 512

# ── MODULE STATE ──────────────────────────────────────────────────────────────

last_message_at: float = 0.0


def _headers() -> dict[str, str]:
    return {"X-API-Key": os.environ.get("TWITTERAPI_IO_KEY", "")}


# ── FILTER RULE REGISTRATION ──────────────────────────────────────────────────


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
            await client.delete(_RULES_URL, headers=headers, json={"ids": ids})
            print(f"[twitter] deleted {len(ids)} existing rule(s)")
    except (httpx.HTTPError, ValueError) as e:
        print(f"[twitter] rule cleanup error: {type(e).__name__}: {e}")

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
        print(f"[twitter] filter rules registered: {r.json()}")
    except (httpx.HTTPError, ValueError) as e:
        print(f"[twitter] rule registration failed: {type(e).__name__}: {e}")
        raise  # Fatal — cannot consume stream without rules


# ── MESSAGE HANDLER ───────────────────────────────────────────────────────────


def _extract_reporter_handle(data: dict[str, Any], tweet: dict[str, Any]) -> str:
    """Extract author username with fallback chain."""
    users = (data.get("includes") or {}).get("users", [])
    if users:
        username = users[0].get("username", "")
        if username:
            return username
    return tweet.get("author", {}).get("username", "")


async def _handle_message(raw: str, client: httpx.AsyncClient) -> None:
    global last_message_at
    last_message_at = time.time()

    try:
        data: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        return

    # twitterapi.io may wrap tweet in an envelope; adapt if format differs
    tweet = data.get("data") or data
    tweet_id = str(tweet.get("id", ""))
    text = tweet.get("text", "")
    created_at = tweet.get("created_at")
    reporter_handle = _extract_reporter_handle(data, tweet)

    if not tweet_id or not text or not reporter_handle:
        return

    await refresh_if_stale()

    result = await process_tweet(
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

    # Mark seen before Discord POST to prevent duplicate processing on race
    try:
        await insert_seen_tweet(tweet_id)
    except Exception as e:
        print(f"[twitter error] failed to mark tweet seen {tweet_id}: {e}")
        return

    reporter_info = REPORTER_BY_HANDLE.get(reporter_handle.lower())
    reporter_display = reporter_info["handle"] if reporter_info else reporter_handle

    message_id = await post_initial_alert(
        pinch_hitter=result["pinch_hitter_raw"],
        team=result["team"],
        tweet_text=text,
        reporter=reporter_display,
        client=client,
    )

    if not message_id:
        print(f"[twitter] Discord post failed for tweet {tweet_id} — skipping DB insert")
        return

    base_embed = build_green_embed(
        pinch_hitter=result["pinch_hitter_raw"],
        team=result["team"],
        tweet_text=text,
        reporter=reporter_display,
    )
    schedule_odds_fetch(
        player_last_name=result["pinch_hitter_raw"].split()[-1],
        message_id=message_id,
        base_embed=base_embed,
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
    except Exception as e:
        print(f"[twitter error] DB insert failed for tweet {tweet_id} (Discord alert orphaned): {e}")
        return

    log_event(
        "alert_fired",
        source="twitter",
        pinch_hitter=result["pinch_hitter_raw"],
        team_id=result["team_id"],
        raw_payload={"tweet_id": tweet_id, "text": text},
    )

    print(f"[twitter] alert fired: {result['pinch_hitter_raw']} ({result['team']})")


# ── CONSUMER LOOP ─────────────────────────────────────────────────────────────


async def twitter_consumer() -> None:
    """
    Persistent WebSocket consumer. Runs forever as an asyncio task.
    Uses websockets built-in reconnection with exponential backoff.
    """
    await build_player_team_map()

    async with httpx.AsyncClient(timeout=15) as client:
        await _register_rules(client)

        async for ws in connect(
            _STREAM_WSS,
            additional_headers=_headers(),
            open_timeout=10,
            ping_interval=30,
        ):
            try:
                print("[twitter] WebSocket connected")
                async for message in ws:
                    await _handle_message(str(message), client)
            except ConnectionClosed:
                print("[twitter] connection lost, reconnecting...")
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[twitter error] unexpected error in message loop: {e}")
                await asyncio.sleep(1)
                continue
