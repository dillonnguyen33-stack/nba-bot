import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from rapidfuzz.distance import Levenshtein

from pinch_hit.alerts.discord import (
    build_confirmed_embed,
    build_green_embed,
    fetch_current_embed,
    patch_embed,
    post_blue_alert,
)
from pinch_hit.alerts.ops import post_ops_alert
from pinch_hit.eval.logger import log_event
from pinch_hit.parsing.gumbo_event import parse_gumbo_substitution
from pinch_hit.parsing.names import normalize_last_name
from pinch_hit.state.repository import (
    get_pending_alerts_by_team,
    update_alert_status,
)
from pinch_hit.state import PendingAlertRow, QualifyingGame, in_game_hours, runtime_state
from pinch_hit.types import STATUS_CONFIRMED

logger = logging.getLogger(__name__)

SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
FEED_URL = "https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"

LATE_INNING_THRESHOLD = int(os.environ.get("LATE_INNING_THRESHOLD", "6"))

_SCHEDULE_POLL_INTERVAL = 60
_GAME_POLL_INTERVAL = 10
_IDLE_TIMEOUT = 1800  # 30 minutes
_TERMINAL_STATUSES = {"Final", "Completed", "Postponed"}
_SCHEDULE_OUTAGE_THRESHOLD = 5
_MAX_CONSECUTIVE_ERRORS = 10
_game_pool: dict[int, asyncio.Task[None]] = {}


def _on_game_done(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception():
        logger.error("game poller crashed", exc_info=task.exception())
        from pinch_hit.state.background import schedule_background
        schedule_background(
            post_ops_alert(f"Game poller crashed unexpectedly: {task.exception()}"),
            "game_crash_ops_alert",
        )


def _match_name(gumbo_name: str, pending_normalized: str, max_distance: int = 2) -> bool:
    gumbo_last = normalize_last_name(gumbo_name)
    if not gumbo_last or not pending_normalized:
        return False
    if gumbo_last == pending_normalized:
        return True
    return Levenshtein.distance(gumbo_last, pending_normalized, score_cutoff=max_distance) <= max_distance


def _match_alert(gumbo_name: str, alert: PendingAlertRow) -> bool:
    return _match_name(gumbo_name, alert["pinch_hitter_normalized"])


async def _handle_substitution(
    play_event: dict[str, Any], play: dict[str, Any], game_pk: int,
    home_team_id: int, away_team_id: int,
    home_team_name: str, away_team_name: str,
    client: httpx.AsyncClient,
) -> None:
    description = play_event.get("details", {}).get("description")
    if not description:
        return
    result = parse_gumbo_substitution(description)
    if result is None:
        return

    gumbo_pinch_hitter, gumbo_replaced = result
    is_top = play.get("about", {}).get("isTopInning", False)
    batting_team_id = away_team_id if is_top else home_team_id
    batting_team_name = away_team_name if is_top else home_team_name
    log_event(
        "mlb_substitution",
        source="gumbo",
        game_pk=game_pk,
        pinch_hitter=gumbo_pinch_hitter,
        team_id=batting_team_id,
        raw_payload={"replaced": gumbo_replaced, "description": description},
    )
    alerts = await get_pending_alerts_by_team(batting_team_id)
    matched = next((a for a in alerts if _match_alert(gumbo_pinch_hitter, a)), None)

    if matched:
        current = await fetch_current_embed(matched["discord_message_id"], client)
        current = current or build_green_embed(matched["pinch_hitter_raw"], "", "", "")
        confirmed = build_confirmed_embed(current, gumbo_pinch_hitter)
        patched = await patch_embed(matched["discord_message_id"], confirmed, client)
        if not patched:
            logger.error("Discord PATCH failed for alert %s; leaving DB pending", matched["id"])
            return
        try:
            await update_alert_status(matched["id"], STATUS_CONFIRMED, game_pk=game_pk)
        except Exception:
            # Discord shows CONFIRMED but DB still says pending — timeout watcher
            # may later turn it grey.
            logger.critical("DB update failed AFTER Discord PATCH for alert %s", matched["id"], exc_info=True)
            await post_ops_alert(
                f"CRITICAL: Alert {matched['id']} shows CONFIRMED on Discord but DB update failed. May revert to UNCONFIRMED on timeout.",
                client,
            )
            return
        log_event(
            "alert_confirmed",
            source="gumbo",
            game_pk=game_pk,
            pinch_hitter=gumbo_pinch_hitter,
            team_id=batting_team_id,
            raw_payload={"replaced": gumbo_replaced, "alert_id": matched["id"]},
        )
        logger.info("confirmed: %s game=%s", gumbo_pinch_hitter, game_pk)
    elif runtime_state.twitter_degraded:
        try:
            posted = await post_blue_alert(gumbo_pinch_hitter, batting_team_name, client)
            if posted:
                log_event(
                    "fallback_alert",
                    source="gumbo",
                    game_pk=game_pk,
                    pinch_hitter=gumbo_pinch_hitter,
                    team_id=batting_team_id,
                    raw_payload={"replaced": gumbo_replaced},
                )
                logger.info("fallback blue alert: %s game=%s", gumbo_pinch_hitter, game_pk)
            else:
                logger.critical(
                    "FALLBACK ALERT FAILED: %s game=%s — user received NO alert",
                    gumbo_pinch_hitter, game_pk,
                )
                await post_ops_alert(
                    f"CRITICAL: Fallback alert failed for {gumbo_pinch_hitter} game {game_pk}. User received no alert.",
                    client,
                )
        except Exception:
            logger.critical("FALLBACK ALERT EXCEPTION: %s game=%s — user received NO alert", gumbo_pinch_hitter, game_pk, exc_info=True)
            await post_ops_alert(
                f"CRITICAL: Fallback alert exception for {gumbo_pinch_hitter} game {game_pk}. User received no alert.",
                client,
            )
    else:
        log_event(
            "unmatched_substitution",
            source="gumbo",
            game_pk=game_pk,
            pinch_hitter=gumbo_pinch_hitter,
            team_id=batting_team_id,
            raw_payload={"replaced": gumbo_replaced},
        )
        logger.info("unmatched substitution: %s game=%s", gumbo_pinch_hitter, game_pk)


async def _poll_game(
    game_pk: int, home_team_id: int, away_team_id: int,
    home_team_name: str, away_team_name: str,
    client: httpx.AsyncClient,
) -> None:
    last_play_count = 0
    seen_events: set[str] = set()
    last_event_at = time.time()
    consecutive_errors = 0

    while True:
        try:
            r = await client.get(FEED_URL.format(game_pk=game_pk))
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError):
            consecutive_errors += 1
            logger.exception("game %s poll failed consecutive_errors=%s", game_pk, consecutive_errors)
            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                logger.error("game %s giving up after %s consecutive errors", game_pk, consecutive_errors)
                await post_ops_alert(
                    f"Game {game_pk} poller gave up after {consecutive_errors} consecutive errors. Game is now unmonitored.",
                    client,
                )
                break
            await asyncio.sleep(_GAME_POLL_INTERVAL)
            continue

        consecutive_errors = 0

        abstract_state = (
            data.get("gameData", {}).get("status", {}).get("abstractGameState", "")
        )
        if abstract_state in _TERMINAL_STATUSES:
            logger.info("game %s ended status=%s", game_pk, abstract_state)
            break

        all_plays = data.get("liveData", {}).get("plays", {}).get("allPlays", [])
        for play in all_plays[last_play_count:]:
            for pe in play.get("playEvents", []):
                if pe.get("type") == "action" and pe.get("isSubstitution"):
                    event_key = f"{play.get('about', {}).get('atBatIndex', 0)}_{pe.get('index', 0)}"
                    if event_key in seen_events:
                        continue
                    seen_events.add(event_key)
                    last_event_at = time.time()
                    try:
                        await _handle_substitution(
                            pe, play, game_pk,
                            home_team_id, away_team_id,
                            home_team_name, away_team_name,
                            client,
                        )
                    except Exception:
                        logger.exception("substitution handler failed game=%s", game_pk)
                        from pinch_hit.state.background import schedule_background
                        schedule_background(
                            post_ops_alert(f"Substitution handler failed for game {game_pk}. A confirmation may have been missed."),
                            "substitution_handler_error_ops",
                        )

        last_play_count = len(all_plays)

        if time.time() - last_event_at > _IDLE_TIMEOUT:
            logger.info("game %s idle timeout", game_pk)
            break

        await asyncio.sleep(_GAME_POLL_INTERVAL)

    _game_pool.pop(game_pk, None)
    logger.info("game %s subscriber ended", game_pk)


async def _fetch_qualifying_games(client: httpx.AsyncClient) -> list[QualifyingGame]:
    r = await client.get(
        SCHEDULE_URL,
        params={"sportId": 1, "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "hydrate": "linescore"},
    )
    r.raise_for_status()
    data = r.json()

    results: list[QualifyingGame] = []
    for series in data.get("dates", []):
        for g in series.get("games", []):
            state = g.get("status", {}).get("abstractGameState", "")
            if state != "Live":
                continue
            inning = g.get("linescore", {}).get("currentInning", 0)
            if inning < LATE_INNING_THRESHOLD:
                continue
            home = g.get("teams", {}).get("home", {})
            away = g.get("teams", {}).get("away", {})
            results.append({
                "game_pk": g["gamePk"],
                "home_team_id": home.get("team", {}).get("id", 0),
                "away_team_id": away.get("team", {}).get("id", 0),
                "home_team_name": home.get("team", {}).get("name", ""),
                "away_team_name": away.get("team", {}).get("name", ""),
                "inning": inning,
            })
    return results


async def _cancel_game_pool() -> None:
    for task in _game_pool.values():
        task.cancel()
    if _game_pool:
        await asyncio.gather(*_game_pool.values(), return_exceptions=True)
    _game_pool.clear()


_MAX_CONSECUTIVE_OUTER_FAILURES = 5


async def schedule_poller() -> None:
    consecutive_schedule_failures = 0
    consecutive_outer_failures = 0
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                try:
                    if not in_game_hours():
                        await asyncio.sleep(_SCHEDULE_POLL_INTERVAL)
                        continue

                    try:
                        games = await _fetch_qualifying_games(client)
                    except (httpx.HTTPError, ValueError, KeyError):
                        consecutive_schedule_failures += 1
                        logger.exception(
                            "schedule fetch failed consecutive_failures=%s",
                            consecutive_schedule_failures,
                        )
                        if consecutive_schedule_failures == _SCHEDULE_OUTAGE_THRESHOLD:
                            runtime_state.schedule_api_degraded = True
                            log_event(
                                "schedule_api_outage",
                                source="gumbo",
                                raw_payload={"consecutive_failures": consecutive_schedule_failures},
                            )
                            await post_ops_alert(
                                f"MLB schedule API has failed {consecutive_schedule_failures} consecutive polls.",
                                client,
                            )
                        await asyncio.sleep(_SCHEDULE_POLL_INTERVAL)
                        continue

                    if consecutive_schedule_failures >= _SCHEDULE_OUTAGE_THRESHOLD:
                        log_event(
                            "schedule_api_recovered",
                            source="gumbo",
                            raw_payload={"previous_consecutive_failures": consecutive_schedule_failures},
                        )
                        await post_ops_alert("MLB schedule API recovered.", client)
                    consecutive_schedule_failures = 0
                    runtime_state.last_schedule_success_at = time.time()
                    runtime_state.schedule_api_degraded = False

                    for game in games:
                        game_pk = game["game_pk"]
                        inning = game["inning"]

                        existing = _game_pool.get(game_pk)
                        if existing is None or existing.done():
                            _game_pool.pop(game_pk, None)
                            task = asyncio.create_task(
                                _poll_game(
                                    game_pk,
                                    game["home_team_id"], game["away_team_id"],
                                    game["home_team_name"], game["away_team_name"],
                                    client,
                                )
                            )
                            task.add_done_callback(_on_game_done)
                            _game_pool[game_pk] = task
                            logger.info("spawned subscriber for game %s inning=%s", game_pk, inning)

                    # Prune completed tasks that didn't self-remove (e.g. exception exit)
                    done_pks = [gp for gp, task in list(_game_pool.items()) if task.done()]
                    for gp in done_pks:
                        _game_pool.pop(gp, None)

                    consecutive_outer_failures = 0
                    await asyncio.sleep(_SCHEDULE_POLL_INTERVAL)
                except Exception:
                    consecutive_outer_failures += 1
                    logger.exception(
                        "schedule poller failed consecutive_outer_failures=%s",
                        consecutive_outer_failures,
                    )
                    if consecutive_outer_failures >= _MAX_CONSECUTIVE_OUTER_FAILURES:
                        raise
                    await asyncio.sleep(_SCHEDULE_POLL_INTERVAL)
    finally:
        await _cancel_game_pool()
