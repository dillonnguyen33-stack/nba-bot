from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal, cast

import httpx

from pinch_hit.alerts import DiscordEmbed, DiscordField

logger = logging.getLogger(__name__)

COLOR_GREEN = 3066993   # 0x2ecc71
COLOR_RED = 15158332    # 0xe74c3c
COLOR_GREY = 9807270    # 0x95a5a6
_DISCORD_RETRIES = 3

_DISCORD_MD_CHARS = re.compile(r'([*_~|>\[\]()\\])')


def _escape_md(text: str) -> str:
    return _DISCORD_MD_CHARS.sub(r'\\\1', text)


def _get_webhook_url(env_var: str) -> str:
    return os.environ.get(env_var, "")


def _webhook_url() -> str:
    return _get_webhook_url("PINCH_HIT_WEBHOOK_URL")


def _official_webhook_url() -> str:
    return _get_webhook_url("OFFICIAL_PLAYS_WEBHOOK_URL")


@asynccontextmanager
async def ensure_client(client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    if client is None:
        async with httpx.AsyncClient(timeout=10) as c:
            yield c
    else:
        yield client


def build_green_embed(
    pinch_hitter: str,
    team: str,
    tweet_text: str,
    reporter_handle: str,
    tweet_id: str = "",
    replaced_player: str | None = None,
) -> DiscordEmbed:
    tweet_url = f"https://x.com/{reporter_handle}/status/{tweet_id}" if tweet_id and reporter_handle else ""

    safe_hitter = _escape_md(pinch_hitter)
    safe_text = _escape_md(tweet_text)
    safe_handle = _escape_md(reporter_handle)

    if replaced_player:
        player_line = f"**{safe_hitter}** will pinch hit for **{_escape_md(replaced_player)}**"
    else:
        player_line = f"**{safe_hitter}** is being called to pinch hit"

    quote = f"@{safe_handle}:\n*{safe_text}*"
    if tweet_url:
        quote += f"\n[View Tweet]({tweet_url})"

    description = (
        f"Twitter source — pre-event pinch hit\n\n"
        f"{player_line}\n\n"
        f"{quote}\n\n"
        f"Lines: not found on tracked books"
    )

    return {
        "title": f"Pinch Hit Alert — {team}",
        "description": description,
        "color": COLOR_GREEN,
        "footer": {"text": "General Alert"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_confirmed_embed(base_embed: DiscordEmbed, gumbo_player_name: str) -> DiscordEmbed:
    confirmed_field: DiscordField = {"name": "Confirmed", "value": gumbo_player_name, "inline": True}
    return {
        **base_embed,
        "color": COLOR_RED,
        "title": "FIXED",
        "fields": [confirmed_field],
    }


def build_timeout_embed(base_embed: DiscordEmbed) -> DiscordEmbed:
    return {
        **base_embed,
        "color": COLOR_GREY,
        "title": "UNCONFIRMED",
    }



async def post_startup_ping(client: httpx.AsyncClient | None = None) -> bool:
    # Goes to the debug channel so operators see the bot is alive without cluttering alerts
    url = _webhook_url()
    if not url:
        logger.error("PINCH_HIT_WEBHOOK_URL not set")
        return False

    embed: DiscordEmbed = {
        "title": "Bot Online",
        "description": "Pinch hit scanner is live and monitoring for plays.",
        "color": COLOR_GREEN,
    }
    payload = {"embeds": [embed]}

    try:
        async with ensure_client(client) as c:
            await _request_with_retries(c, "POST", url, json=payload)
        logger.info("startup ping sent")
        return True
    except (httpx.HTTPError, ValueError):
        logger.exception("startup ping failed")
        return False


async def post_unfiltered_alert(
    tweet_text: str,
    reporter_handle: str,
    client: httpx.AsyncClient | None = None,
) -> None:
    url = _webhook_url()
    if not url:
        logger.debug("PINCH_HIT_WEBHOOK_URL not set; skipping unfiltered alert")
        return

    embed: DiscordEmbed = {
        "title": "Unfiltered Tweet",
        "description": tweet_text,
        "color": COLOR_GREY,
        "fields": [{"name": "Reporter", "value": f"@{reporter_handle}", "inline": True}],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload = {"embeds": [embed]}

    try:
        async with ensure_client(client) as c:
            await _request_with_retries(c, "POST", url, json=payload)
    except (httpx.HTTPError, ValueError):
        logger.exception("post_unfiltered_alert failed")


async def post_initial_alert(
    pinch_hitter: str,
    team: str,
    tweet_text: str,
    reporter_handle: str = "",
    tweet_id: str = "",
    replaced_player: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    url = _official_webhook_url()
    if not url:
        logger.error("OFFICIAL_PLAYS_WEBHOOK_URL not set")
        return None

    embed = build_green_embed(
        pinch_hitter, team, tweet_text, reporter_handle,
        tweet_id=tweet_id, replaced_player=replaced_player,
    )
    payload = {"content": "@everyone", "embeds": [embed]}

    try:
        async with ensure_client(client) as c:
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
    url = _official_webhook_url()
    if not url:
        logger.error("OFFICIAL_PLAYS_WEBHOOK_URL not set")
        return False

    payload = {"embeds": [embed]}

    try:
        async with ensure_client(client) as c:
            await _patch(c, url, message_id, payload)
        return True
    except (httpx.HTTPError, ValueError):
        logger.exception("patch_embed failed message_id=%s", message_id)
        return False


async def delete_message(
    message_id: str,
    client: httpx.AsyncClient | None = None,
) -> bool:
    url = _official_webhook_url()
    if not url:
        logger.error("OFFICIAL_PLAYS_WEBHOOK_URL not set")
        return False

    try:
        async with ensure_client(client) as c:
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
    url = _official_webhook_url()
    if not url:
        logger.error("OFFICIAL_PLAYS_WEBHOOK_URL not set")
        return None
    try:
        async with ensure_client(client) as c:
            r = await _request_with_retries(c, "GET", f"{url}/messages/{message_id}")
            embeds = r.json().get("embeds", [])
            if not embeds:
                return None
            return cast(DiscordEmbed, embeds[0])
    except (httpx.HTTPError, ValueError):
        logger.exception("fetch embed failed message_id=%s", message_id)
        return None


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
    method: Literal["GET", "POST", "PATCH", "DELETE"],
    url: str,
    json: dict[str, Any] | None = None,
) -> httpx.Response:
    for attempt in range(1, _DISCORD_RETRIES + 1):
        response = await client.request(method, url, json=json)

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

    raise RuntimeError("_DISCORD_RETRIES must be >= 1")
