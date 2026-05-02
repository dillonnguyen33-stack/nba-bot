import asyncio
import os
import time
from datetime import datetime, timezone

import httpx

import pinch_hit.consumers.gumbo as gumbo_mod
import pinch_hit.consumers.twitter as twitter_mod
from pinch_hit.eval.logger import log_event

_HEARTBEAT_THRESHOLD = 60.0   # seconds — gap that triggers degraded state


def _in_game_hours() -> bool:
    game_hours_start = int(os.environ.get("GAME_HOURS_START", "12"))
    game_hours_end = int(os.environ.get("GAME_HOURS_END", "25"))
    hour = datetime.now(timezone.utc).hour
    if game_hours_end <= 24:
        return game_hours_start <= hour < game_hours_end
    return hour >= game_hours_start or hour < (game_hours_end - 24)


async def _post_ops(message: str, client: httpx.AsyncClient) -> None:
    url = os.environ.get("OPS_WEBHOOK_URL", "")
    if not url:
        return  # No ops webhook configured
    try:
        r = await client.post(url, json={"content": message})
        r.raise_for_status()
    except (httpx.HTTPError, ValueError) as e:
        print(f"[health error] ops post failed: {type(e).__name__}: {e}")


async def health_monitor() -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            await asyncio.sleep(30)
            try:
                if not _in_game_hours():
                    continue  # Outside game hours

                gap = time.time() - twitter_mod.last_message_at
                was_degraded = gumbo_mod.TWITTER_DEGRADED

                if gap > _HEARTBEAT_THRESHOLD and not was_degraded:
                    gumbo_mod.TWITTER_DEGRADED = True
                    print(f"[health] Twitter stream degraded — gap {gap:.0f}s")
                    log_event("twitter_degraded", source="health", raw_payload={"gap_seconds": gap})
                    await _post_ops(
                        f"Twitter stream degraded — gap {gap:.0f}s. Switching to GUMBO-direct alerts.",
                        client,
                    )
                elif was_degraded and gap <= _HEARTBEAT_THRESHOLD:
                    # Recovery: a recent message arrived while we were degraded
                    gumbo_mod.TWITTER_DEGRADED = False
                    print("[health] Twitter stream recovered")
                    log_event("twitter_recovered", source="health", raw_payload={"gap_seconds": gap})
                    await _post_ops("Twitter stream recovered.", client)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[health error] {e}")
