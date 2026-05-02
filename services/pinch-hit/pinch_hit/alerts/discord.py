from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from pinch_hit.alerts.odds import OddsLine

# ── COLORS ────────────────────────────────────────────────────────────────────

COLOR_GREEN = 3066993   # 0x2ecc71
COLOR_RED = 15158332    # 0xe74c3c
COLOR_GREY = 9807270    # 0x95a5a6
COLOR_BLUE = 3447003    # 0x3498db

# ── CONFIG ────────────────────────────────────────────────────────────────────


def _webhook_url() -> str:
    return os.environ.get("PINCH_HIT_WEBHOOK_URL", "")


@asynccontextmanager
async def _ensure_client(client: httpx.AsyncClient | None):
    if client is None:
        async with httpx.AsyncClient(timeout=10) as c:
            yield c
    else:
        yield client


# ── EMBED BUILDERS ────────────────────────────────────────────────────────────


def build_green_embed(pinch_hitter: str, team: str, tweet_text: str, reporter: str) -> dict:
    return {
        "title": "Pinch hit detected",
        "description": tweet_text,
        "color": COLOR_GREEN,
        "fields": [
            {"name": "Player", "value": pinch_hitter, "inline": True},
            {"name": "Team", "value": team, "inline": True},
            {"name": "Reporter", "value": reporter, "inline": True},
        ],
    }


def build_confirmed_embed(base_embed: dict, gumbo_player_name: str) -> dict:
    new_embed = {**base_embed}
    new_embed["color"] = COLOR_RED
    new_embed["title"] = "CONFIRMED BY MLB -- EDGE GONE"
    confirmed_field = {"name": "Confirmed", "value": gumbo_player_name, "inline": True}
    new_embed["fields"] = [confirmed_field] + list(base_embed.get("fields", []))
    return new_embed


def build_timeout_embed(base_embed: dict) -> dict:
    new_embed = {**base_embed}
    new_embed["color"] = COLOR_GREY
    new_embed["title"] = "~~UNCONFIRMED~~"
    new_embed["fields"] = list(base_embed.get("fields", []))
    return new_embed


def build_blue_embed(pinch_hitter: str, team: str) -> dict:
    return {
        "title": "DIRECT FROM MLB -- NO EDGE",
        "color": COLOR_BLUE,
        "fields": [
            {"name": "Player", "value": pinch_hitter, "inline": True},
            {"name": "Team", "value": team, "inline": True},
        ],
    }


def build_odds_fields(lines_data: dict[str, "OddsLine"]) -> list[dict]:
    if not lines_data:
        return []

    by_market: dict[str, list[str]] = {}
    for entry in lines_data.values():
        market = entry["market"]
        price = entry["under"]
        formatted = f"{entry['book']} u{entry['line']} ({'+' if price > 0 else ''}{price})"
        by_market.setdefault(market, []).append(formatted)

    return [
        {"name": market, "value": " | ".join(entries), "inline": False}
        for market, entries in by_market.items()
    ]


# ── POST / PATCH ──────────────────────────────────────────────────────────────


async def post_initial_alert(
    pinch_hitter: str,
    team: str,
    tweet_text: str,
    reporter: str = "",
    client: httpx.AsyncClient | None = None,
) -> str | None:
    url = _webhook_url()
    if not url:
        print("[discord error] PINCH_HIT_WEBHOOK_URL not set")
        return None

    embed = build_green_embed(pinch_hitter, team, tweet_text, reporter)
    payload = {"content": "@everyone", "embeds": [embed]}

    try:
        async with _ensure_client(client) as c:
            return await _post_wait(c, url, payload)
    except (httpx.HTTPError, httpx.TimeoutException, ValueError) as e:
        print(f"[discord error] {type(e).__name__}: {e}")
        return None


async def _post_wait(client: httpx.AsyncClient, url: str, payload: dict) -> str | None:
    r = await client.post(f"{url}?wait=true", json=payload)
    r.raise_for_status()
    msg_id = r.json().get("id")
    print(f"[discord] posted initial alert, message_id={msg_id}")
    return str(msg_id) if msg_id else None


async def patch_embed(
    message_id: str,
    embed: dict,
    client: httpx.AsyncClient | None = None,
) -> None:
    url = _webhook_url()
    if not url:
        print("[discord error] PINCH_HIT_WEBHOOK_URL not set")
        return

    payload = {"embeds": [embed]}

    try:
        async with _ensure_client(client) as c:
            await _patch(c, url, message_id, payload)
    except (httpx.HTTPError, httpx.TimeoutException, ValueError) as e:
        print(f"[discord error] patch_embed {message_id}: {type(e).__name__}: {e}")


async def _patch(client: httpx.AsyncClient, url: str, message_id: str, payload: dict) -> None:
    r = await client.patch(f"{url}/messages/{message_id}", json=payload)
    r.raise_for_status()
    print(f"[discord] patched message {message_id}")


async def post_blue_alert(
    pinch_hitter: str,
    team: str,
    client: httpx.AsyncClient | None = None,
) -> None:
    url = _webhook_url()
    if not url:
        print("[discord error] PINCH_HIT_WEBHOOK_URL not set")
        return

    embed = build_blue_embed(pinch_hitter, team)
    payload = {"embeds": [embed]}

    try:
        async with _ensure_client(client) as c:
            r = await c.post(url, json=payload)
            r.raise_for_status()
        print(f"[discord] posted blue alert for {pinch_hitter}")
    except (httpx.HTTPError, httpx.TimeoutException, ValueError) as e:
        print(f"[discord error] post_blue_alert: {type(e).__name__}: {e}")
