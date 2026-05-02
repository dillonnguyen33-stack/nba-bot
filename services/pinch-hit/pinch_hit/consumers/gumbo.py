import asyncio
import os
import time
import unicodedata
from datetime import datetime, timezone
from typing import TypedDict

import httpx
from rapidfuzz.distance import Levenshtein

from pinch_hit.alerts.discord import (
    build_confirmed_embed,
    build_green_embed,
    patch_embed,
    post_blue_alert,
)
from pinch_hit.eval.logger import log_event
from pinch_hit.parsing.gumbo_event import parse_gumbo_substitution
from pinch_hit.state.repository import (
    PendingAlertRow,
    get_pending_alerts_by_team,
    update_alert_status,
)

# ── CONFIG ────────────────────────────────────────────────────────────────────

SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
FEED_URL = "https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"

LATE_INNING_THRESHOLD = int(os.environ.get("LATE_INNING_THRESHOLD", "6"))
GAME_HOURS_START = int(os.environ.get("GAME_HOURS_START", "12"))
GAME_HOURS_END = int(os.environ.get("GAME_HOURS_END", "25"))

_SCHEDULE_POLL_INTERVAL = 60
_GAME_POLL_INTERVAL = 10
_IDLE_TIMEOUT = 1800  # 30 minutes
_TERMINAL_STATUSES = {"Final", "Completed", "Postponed"}

# ── MODULE STATE ──────────────────────────────────────────────────────────────

TWITTER_DEGRADED: bool = False
_game_pool: dict[int, asyncio.Task[None]] = {}


def _on_game_done(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception():
        print(f"[gumbo error] game poller crashed: {task.exception()}")


# ── NAME MATCHING ─────────────────────────────────────────────────────────────


def _normalize_last_name(full_name: str) -> str:
    last = full_name.strip().split()[-1]
    decomposed = unicodedata.normalize("NFD", last)
    ascii_bytes = decomposed.encode("ascii", "ignore")
    return ascii_bytes.decode("ascii").lower()


def _match_name(gumbo_name: str, pending_normalized: str, max_distance: int = 2) -> bool:
    gumbo_last = _normalize_last_name(gumbo_name)
    if gumbo_last == pending_normalized:
        return True
    return Levenshtein.distance(gumbo_last, pending_normalized) <= max_distance


def _match_alert(gumbo_name: str, alert: PendingAlertRow) -> bool:
    pending_normalized = alert["pinch_hitter_normalized"].split()[-1]
    return _match_name(gumbo_name, pending_normalized)


# ── DISCORD MESSAGE FETCH ─────────────────────────────────────────────────────


async def _fetch_current_embed(message_id: str, client: httpx.AsyncClient) -> dict | None:
    url = os.environ.get("PINCH_HIT_WEBHOOK_URL", "")
    try:
        r = await client.get(f"{url}/messages/{message_id}")
        r.raise_for_status()
        return r.json()["embeds"][0]
    except (httpx.HTTPError, ValueError, IndexError) as e:
        print(f"[gumbo error] fetch embed {message_id}: {type(e).__name__}: {e}")
        return None


# ── SUBSTITUTION HANDLER ──────────────────────────────────────────────────────


async def _handle_substitution(
    play_event: dict, play: dict, game_pk: int,
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
    alerts = await get_pending_alerts_by_team(batting_team_id)
    matched = next((a for a in alerts if _match_alert(gumbo_pinch_hitter, a)), None)

    if matched:
        try:
            current = await _fetch_current_embed(matched["discord_message_id"], client)
            current = current or build_green_embed(matched["pinch_hitter_raw"], "", "", "")
            confirmed = build_confirmed_embed(current, gumbo_pinch_hitter)
            await patch_embed(matched["discord_message_id"], confirmed, client)
            await update_alert_status(matched["id"], "confirmed", game_pk=game_pk)
        except Exception as e:
            print(f"[gumbo error] confirm update failed for alert {matched['id']}: {e}")
        else:
            log_event(
                "confirmed_substitution",
                source="gumbo",
                game_pk=game_pk,
                pinch_hitter=gumbo_pinch_hitter,
                team_id=batting_team_id,
                raw_payload={"replaced": gumbo_replaced, "alert_id": matched["id"]},
            )
            print(f"[gumbo] confirmed: {gumbo_pinch_hitter} (game {game_pk})")
    elif TWITTER_DEGRADED:
        await post_blue_alert(gumbo_pinch_hitter, batting_team_name, client)
        log_event(
            "unmatched_substitution",
            source="gumbo",
            game_pk=game_pk,
            pinch_hitter=gumbo_pinch_hitter,
            team_id=batting_team_id,
            raw_payload={"replaced": gumbo_replaced, "fallback": True},
        )
        print(f"[gumbo] fallback blue alert: {gumbo_pinch_hitter} (game {game_pk})")
    else:
        log_event(
            "unmatched_substitution",
            source="gumbo",
            game_pk=game_pk,
            pinch_hitter=gumbo_pinch_hitter,
            team_id=batting_team_id,
            raw_payload={"replaced": gumbo_replaced},
        )
        print(f"[gumbo] unmatched substitution: {gumbo_pinch_hitter} (game {game_pk})")


# ── PER-GAME POLLER ───────────────────────────────────────────────────────────


async def _poll_game(
    game_pk: int, home_team_id: int, away_team_id: int,
    home_team_name: str, away_team_name: str,
) -> None:
    last_play_count = 0
    seen_events: set[str] = set()
    last_event_at = time.time()

    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                r = await client.get(FEED_URL.format(game_pk=game_pk))
                r.raise_for_status()
                data = r.json()

                abstract_state = (
                    data.get("gameData", {}).get("status", {}).get("abstractGameState", "")
                )
                if abstract_state in _TERMINAL_STATUSES:
                    print(f"[gumbo] game {game_pk} ended ({abstract_state})")
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
                            except Exception as e:
                                print(f"[gumbo error] substitution handler game {game_pk}: {e}")

                last_play_count = len(all_plays)
            except Exception as e:
                print(f"[gumbo error] game {game_pk}: {e}")
                await asyncio.sleep(_GAME_POLL_INTERVAL)
                continue

            if time.time() - last_event_at > _IDLE_TIMEOUT:
                print(f"[gumbo] game {game_pk} idle timeout")
                break

            await asyncio.sleep(_GAME_POLL_INTERVAL)

    _game_pool.pop(game_pk, None)
    print(f"[gumbo] game {game_pk} subscriber ended")


# ── SCHEDULE POLLER ───────────────────────────────────────────────────────────


class QualifyingGame(TypedDict):
    game_pk: int
    home_team_id: int
    away_team_id: int
    home_team_name: str
    away_team_name: str
    inning: int


async def _fetch_qualifying_games(client: httpx.AsyncClient) -> list[QualifyingGame]:
    try:
        r = await client.get(
            SCHEDULE_URL,
            params={"sportId": 1, "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "hydrate": "linescore"},
        )
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        print(f"[gumbo error] schedule fetch: {type(e).__name__}: {e}")
        return []

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


async def schedule_poller() -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                hour = datetime.now(timezone.utc).hour
                if GAME_HOURS_END <= 24:
                    in_window = GAME_HOURS_START <= hour < GAME_HOURS_END
                else:
                    in_window = hour >= GAME_HOURS_START or hour < (GAME_HOURS_END - 24)
                if not in_window:
                    await asyncio.sleep(_SCHEDULE_POLL_INTERVAL)
                    continue

                games = await _fetch_qualifying_games(client)
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
                            )
                        )
                        task.add_done_callback(_on_game_done)
                        _game_pool[game_pk] = task
                        print(f"[gumbo] spawned subscriber for game {game_pk} (inning {inning})")

                # Prune completed tasks that didn't self-remove (e.g. exception exit)
                done_pks = [gp for gp, task in list(_game_pool.items()) if task.done()]
                for gp in done_pks:
                    _game_pool.pop(gp, None)

                await asyncio.sleep(_SCHEDULE_POLL_INTERVAL)
            except Exception as e:
                print(f"[gumbo error] schedule poller: {e}")
                await asyncio.sleep(_SCHEDULE_POLL_INTERVAL)
