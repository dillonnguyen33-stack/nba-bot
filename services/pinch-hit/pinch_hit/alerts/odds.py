import asyncio
import os
import time
from typing import TypedDict

import httpx

from pinch_hit.alerts.discord import build_odds_fields, patch_embed

# ── CONSTANTS ─────────────────────────────────────────────────────────────────

ODDS_BASE = "https://api.the-odds-api.com/v4/sports/baseball_mlb"

PROP_BOOKS: dict[str, str] = {
    "draftkings": "DK",
    "fanduel": "FD",
    "fliff": "Fliff",
    "hardrockbet": "Hard Rock",
    "bet365": "Bet365",
}

MLB_PROP_MARKETS = ["batter_hits", "batter_total_bases", "batter_rbis", "batter_home_runs"]

_CACHE_TTL = 60  # seconds


# ── TYPES ────────────────────────────────────────────────────────────────────


class OddsLine(TypedDict):
    book: str
    market: str
    line: float
    under: int


# ── CACHE ────────────────────────────────────────────────────────────────────

_cache: dict[tuple[str, str], tuple[float, dict[str, OddsLine]]] = {}


def _get_cached(player_last_name: str, market: str) -> dict[str, OddsLine] | None:
    key = (player_last_name, market)
    entry = _cache.get(key)
    if entry is None:
        return None
    fetched_at, result = entry
    if time.time() - fetched_at > _CACHE_TTL:
        del _cache[key]
        return None
    return result


def _set_cached(player_last_name: str, market: str, result: dict[str, OddsLine]) -> None:
    _cache[(player_last_name, market)] = (time.time(), result)


# ── FETCH ────────────────────────────────────────────────────────────────────


async def _fetch_player_lines(player_last_name: str, client: httpx.AsyncClient) -> dict[str, OddsLine]:
    api_key = os.environ.get("ODDS_API_KEY", "")
    if not api_key:
        return {}

    last_name_lower = player_last_name.lower()
    results: dict[str, OddsLine] = {}

    try:
        r = await client.get(f"{ODDS_BASE}/events", params={"apiKey": api_key})
        r.raise_for_status()
        events = r.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        print("[odds error] failed to fetch events list")
        return {}

    for event in events[:6]:
        event_id = event.get("id")
        if not event_id:
            continue

        for market in MLB_PROP_MARKETS:
            cached = _get_cached(player_last_name, market)
            if cached is not None:
                results.update(cached)
                continue

            try:
                r = await client.get(
                    f"{ODDS_BASE}/events/{event_id}/odds",
                    params={
                        "apiKey": api_key,
                        "markets": market,
                        "bookmakers": ",".join(PROP_BOOKS.keys()),
                        "oddsFormat": "american",
                        "regions": "us,us2",
                    },
                )
                r.raise_for_status()
                data = r.json()
            except (httpx.HTTPError, httpx.TimeoutException):
                print(f"[odds error] failed to fetch odds for event={event_id} market={market}")
                continue

            per_market_results: dict[str, OddsLine] = {}
            for bk in data.get("bookmakers", []):
                abbreviation = PROP_BOOKS.get(bk.get("key", ""))
                if not abbreviation:
                    continue
                for mkt in bk.get("markets", []):
                    for outcome in mkt.get("outcomes", []):
                        if last_name_lower not in outcome.get("description", "").lower():
                            continue
                        if outcome.get("name") != "Under":
                            continue
                        point = outcome.get("point")
                        price = outcome.get("price")
                        if point is None or price is None:
                            continue
                        market_label = market.replace("batter_", "").replace("_", " ").title()
                        key = f"{abbreviation}_{market_label}"
                        per_market_results[key] = {
                            "book": abbreviation,
                            "market": market_label,
                            "line": point,
                            "under": price,
                        }

            if per_market_results:
                _set_cached(player_last_name, market, per_market_results)
                results.update(per_market_results)

    return results


# ── FIRE-AND-FORGET ──────────────────────────────────────────────────────────


async def fetch_and_patch_odds(player_last_name: str, message_id: str, base_embed: dict) -> None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            lines_data = await _fetch_player_lines(player_last_name, client)
            if not lines_data:
                print(f"[odds] no lines found for {player_last_name}")
                return
            odds_fields = build_odds_fields(lines_data)
            patched_embed = {**base_embed, "fields": list(base_embed.get("fields", [])) + odds_fields}
            await patch_embed(message_id, patched_embed, client)
            print(f"[odds] patched message {message_id} with {len(odds_fields)} field(s) for {player_last_name}")
    except Exception as e:
        print(f"[odds error] fetch_and_patch_odds failed: {e}")


_background_tasks: set[asyncio.Task[None]] = set()


def _on_task_done(task: asyncio.Task[None]) -> None:
    _background_tasks.discard(task)
    if not task.cancelled() and task.exception():
        print(f"[odds error] background fetch failed: {task.exception()}")


def schedule_odds_fetch(player_last_name: str, message_id: str, base_embed: dict) -> None:
    """Fire-and-forget entry point. Caller does not await."""
    task = asyncio.create_task(fetch_and_patch_odds(player_last_name, message_id, base_embed))
    _background_tasks.add(task)
    task.add_done_callback(_on_task_done)
