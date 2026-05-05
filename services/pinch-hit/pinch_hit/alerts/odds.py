import logging
import os
import time

import httpx

from pinch_hit.alerts import DiscordEmbed, DiscordField, OddsLine
from pinch_hit.alerts.discord import build_odds_fields, patch_embed
from pinch_hit.state.background import schedule_background

logger = logging.getLogger(__name__)

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


_cache: dict[tuple[str, str], tuple[float, dict[str, OddsLine]]] = {}


class OddsFetchError(Exception):
    pass


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


async def _fetch_player_lines(player_last_name: str, client: httpx.AsyncClient) -> dict[str, OddsLine]:
    api_key = os.environ.get("ODDS_API_KEY", "")
    if not api_key:
        logger.info("ODDS_API_KEY not set; skipping odds fetch")
        return {}

    last_name_lower = player_last_name.lower()
    results: dict[str, OddsLine] = {}

    try:
        r = await client.get(f"{ODDS_BASE}/events", params={"apiKey": api_key})
        r.raise_for_status()
        events = r.json()
    except (httpx.HTTPError, ValueError):
        logger.exception("failed to fetch odds events list")
        raise OddsFetchError("failed to fetch odds events list") from None

    markets_found: set[str] = set()
    had_fetch_error = False
    for event in events[:6]:
        if len(markets_found) >= len(MLB_PROP_MARKETS):
            break  # Got lines for all markets, stop burning API quota

        event_id = event.get("id")
        if not event_id:
            continue

        for market in MLB_PROP_MARKETS:
            cached = _get_cached(player_last_name, market)
            if cached is not None:
                results.update(cached)
                markets_found.add(market)
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
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    logger.warning("rate limited fetching odds event=%s market=%s", event_id, market)
                else:
                    logger.exception("failed to fetch odds for event=%s market=%s", event_id, market)
                had_fetch_error = True
                continue
            except (httpx.HTTPError, ValueError):
                logger.exception("failed to fetch odds for event=%s market=%s", event_id, market)
                had_fetch_error = True
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
                            "under_price": int(price),
                        }

            if per_market_results:
                _set_cached(player_last_name, market, per_market_results)
                results.update(per_market_results)
                markets_found.add(market)

    if not results and had_fetch_error:
        raise OddsFetchError("failed to fetch any odds lines")

    return results


async def fetch_and_patch_odds(player_last_name: str, message_id: str, base_embed: DiscordEmbed) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            lines_data = await _fetch_player_lines(player_last_name, client)
            odds_error = False
        except OddsFetchError:
            logger.exception("odds fetch failed for %s", player_last_name)
            lines_data = {}
            odds_error = True

        base_fields = [f for f in base_embed.get("fields", []) if f["name"] != "Odds"]

        if lines_data:
            odds_fields = build_odds_fields(lines_data)
            patched_embed: DiscordEmbed = {**base_embed, "fields": base_fields + odds_fields}
        elif odds_error:
            odds_error_field: DiscordField = {
                "name": "Odds",
                "value": "Odds unavailable (error)",
                "inline": False,
            }
            patched_embed = {**base_embed, "fields": base_fields + [odds_error_field]}
        else:
            # Remove stale "Fetching odds..." placeholder
            patched_embed = {**base_embed, "fields": base_fields}

        success = await patch_embed(message_id, patched_embed, client)
        if success and lines_data:
            logger.info(
                "patched message %s with %s odds field(s) for %s",
                message_id,
                len(odds_fields),
                player_last_name,
            )
        elif not success:
            logger.warning("odds patch failed for message %s", message_id)


def schedule_odds_fetch(player_last_name: str, message_id: str, base_embed: DiscordEmbed) -> None:
    """Fire-and-forget entry point. Caller does not await."""
    schedule_background(fetch_and_patch_odds(player_last_name, message_id, base_embed), "odds_fetch")
