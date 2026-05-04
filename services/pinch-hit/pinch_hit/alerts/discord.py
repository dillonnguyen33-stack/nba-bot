from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx

from pinch_hit.alerts import DiscordEmbed, DiscordField, OddsLine

logger = logging.getLogger(__name__)

COLOR_GREEN = 3066993   # 0x2ecc71
COLOR_RED = 15158332    # 0xe74c3c
COLOR_GREY = 9807270    # 0x95a5a6
COLOR_BLUE = 3447003    # 0x3498db
_DISCORD_RETRIES = 3


def _webhook_url() -> str:
    return os.environ.get("PINCH_HIT_WEBHOOK_URL", "")


@asynccontextmanager
async def _ensure_client(client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    if client is None:
        async with httpx.AsyncClient(timeout=10) as c:
            yield c
    else:
        yield client


def build_green_embed(
    pinch_hitter: str,
    team: str,
    tweet_text: str,
    reporter: str,
    include_odds_placeholder: bool = False,
) -> DiscordEmbed:
    fields: list[DiscordField] = [
        {"name": "Player", "value": pinch_hitter, "inline": True},
        {"name": "Team", "value": team, "inline": True},
        {"name": "Reporter", "value": reporter, "inline": True},
    ]
    if include_odds_placeholder:
        fields.append({"name": "Odds", "value": "Fetching odds...", "inline": False})
    return {
        "title": "Pinch hit detected",
        "description": tweet_text,
        "color": COLOR_GREEN,
        "fields": fields,
    }


def build_confirmed_embed(base_embed: DiscordEmbed, gumbo_player_name: str) -> DiscordEmbed:
    confirmed_field: DiscordField = {"name": "Confirmed", "value": gumbo_player_name, "inline": True}
    return {
        **base_embed,
        "color": COLOR_RED,
        "title": "CONFIRMED BY MLB -- EDGE GONE",
        "fields": [confirmed_field, *base_embed.get("fields", [])],
    }


def build_timeout_embed(base_embed: DiscordEmbed) -> DiscordEmbed:
    return {
        **base_embed,
        "color": COLOR_GREY,
        "title": "~~UNCONFIRMED~~",
    }


def build_blue_embed(pinch_hitter: str, team: str) -> DiscordEmbed:
    return {
        "title": "DIRECT FROM MLB -- NO EDGE",
        "color": COLOR_BLUE,
        "fields": [
            {"name": "Player", "value": pinch_hitter, "inline": True},
            {"name": "Team", "value": team, "inline": True},
        ],
    }


def build_odds_fields(lines_data: dict[str, OddsLine]) -> list[DiscordField]:
    if not lines_data:
        return []

    by_market: dict[str, list[str]] = {}
    for entry in lines_data.values():
        market = entry["market"]
        price = entry["under_price"]
        formatted = f"{entry['book']} u{entry['line']} ({'+' if price > 0 else ''}{price})"
        by_market.setdefault(market, []).append(formatted)

    return [
        {"name": market, "value": " | ".join(entries), "inline": False}
        for market, entries in by_market.items()
    ]


async def post_initial_alert(
    pinch_hitter: str,
    team: str,
    tweet_text: str,
    reporter: str = "",
    client: httpx.AsyncClient | None = None,
) -> str | None:
    url = _webhook_url()
    if not url:
        logger.error("PINCH_HIT_WEBHOOK_URL not set")
        return None

    embed = build_green_embed(
        pinch_hitter,
        team,
        tweet_text,
        reporter,
        include_odds_placeholder=bool(os.environ.get("ODDS_API_KEY", "")),
    )
    payload = {"content": "@everyone", "embeds": [embed]}

    try:
        async with _ensure_client(client) as c:
            return await _post_wait(c, url, payload)
    except (httpx.HTTPError, ValueError):
        logger.exception("post_initial_alert failed")
        return None


async def _post_wait(client: httpx.AsyncClient, url: str, payload: dict[str, Any]) -> str | None:
    r = await _request_with_retries(client, "POST", f"{url}?wait=true", json=payload)
    msg_id = r.json().get("id")
    logger.info("posted initial alert message_id=%s", msg_id)
    return str(msg_id) if msg_id else None


async def patch_embed(
    message_id: str,
    embed: DiscordEmbed,
    client: httpx.AsyncClient | None = None,
) -> bool:
    url = _webhook_url()
    if not url:
        logger.error("PINCH_HIT_WEBHOOK_URL not set")
        return False

    payload = {"embeds": [embed]}

    try:
        async with _ensure_client(client) as c:
            await _patch(c, url, message_id, payload)
        return True
    except (httpx.HTTPError, ValueError):
        logger.exception("patch_embed failed message_id=%s", message_id)
        return False


async def delete_message(
    message_id: str,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Returns True when the Discord message is gone or was accepted for deletion."""
    url = _webhook_url()
    if not url:
        logger.error("PINCH_HIT_WEBHOOK_URL not set")
        return False

    try:
        async with _ensure_client(client) as c:
            await _request_with_retries(c, "DELETE", f"{url}/messages/{message_id}")
        logger.info("deleted message %s", message_id)
        return True
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            logger.info("message %s already deleted", message_id)
            return True
        logger.exception("delete_message failed message_id=%s", message_id)
        return False
    except (httpx.HTTPError, ValueError):
        logger.exception("delete_message failed message_id=%s", message_id)
        return False


async def _patch(client: httpx.AsyncClient, url: str, message_id: str, payload: dict[str, Any]) -> None:
    await _request_with_retries(client, "PATCH", f"{url}/messages/{message_id}", json=payload)
    logger.info("patched message %s", message_id)


async def fetch_current_embed(
    message_id: str, client: httpx.AsyncClient | None = None,
) -> DiscordEmbed | None:
    url = _webhook_url()
    if not url:
        logger.error("PINCH_HIT_WEBHOOK_URL not set")
        return None
    try:
        async with _ensure_client(client) as c:
            r = await _request_with_retries(c, "GET", f"{url}/messages/{message_id}")
            return cast(DiscordEmbed, r.json()["embeds"][0])
    except (httpx.HTTPError, ValueError, IndexError, KeyError):
        logger.exception("fetch embed failed message_id=%s", message_id)
        return None


async def post_blue_alert(
    pinch_hitter: str,
    team: str,
    client: httpx.AsyncClient | None = None,
) -> bool:
    url = _webhook_url()
    if not url:
        logger.error("PINCH_HIT_WEBHOOK_URL not set")
        return False

    embed = build_blue_embed(pinch_hitter, team)
    payload = {"embeds": [embed]}

    try:
        async with _ensure_client(client) as c:
            await _request_with_retries(c, "POST", url, json=payload)
        logger.info("posted blue alert for %s", pinch_hitter)
        return True
    except (httpx.HTTPError, ValueError):
        logger.exception("post_blue_alert failed")
        return False


def _retry_after_seconds(response: httpx.Response) -> float | None:
    retry_after = response.headers.get("Retry-After") or response.headers.get("X-RateLimit-Reset-After")
    if not retry_after:
        return None
    try:
        return max(float(retry_after), 0.0)
    except ValueError:
        return None


_MAX_RETRY_SLEEP = 30.0


async def _request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    for attempt in range(1, _DISCORD_RETRIES + 1):
        response = await client.request(method, url, **kwargs)

        is_rate_limited = response.status_code == 429
        is_server_error = response.status_code >= 500

        if not is_rate_limited and not is_server_error:
            response.raise_for_status()
            return response

        if attempt == _DISCORD_RETRIES:
            response.raise_for_status()

        if is_rate_limited:
            sleep_for = _retry_after_seconds(response) or min(2 ** (attempt - 1), 4)
        else:
            sleep_for = min(2 ** (attempt - 1), 4)

        sleep_for = min(sleep_for, _MAX_RETRY_SLEEP)
        logger.warning(
            "Discord %s %s; retrying in %.2fs attempt=%s",
            response.status_code,
            "rate limited" if is_rate_limited else "server error",
            sleep_for,
            attempt,
        )
        await asyncio.sleep(sleep_for)

    # Unreachable: loop always returns or raises. Guard against _DISCORD_RETRIES=0.
    raise RuntimeError("_DISCORD_RETRIES must be >= 1")
