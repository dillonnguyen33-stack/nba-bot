import asyncio
import logging
import time

import httpx

from pinch_hit.alerts.ops import post_ops_alert
from pinch_hit.eval.logger import log_event
from pinch_hit.state.runtime import in_game_hours, runtime_state

logger = logging.getLogger(__name__)

_HEARTBEAT_THRESHOLD = 60.0   # seconds — gap that triggers degraded state


async def health_monitor() -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            await asyncio.sleep(30)
            try:
                if not in_game_hours():
                    continue

                gap = time.time() - runtime_state.last_twitter_message_at
                was_degraded = runtime_state.twitter_degraded

                if gap > _HEARTBEAT_THRESHOLD and not was_degraded:
                    runtime_state.twitter_degraded = True
                    logger.warning("Twitter stream degraded gap=%.0fs", gap)
                    log_event("twitter_degraded", source="health", raw_payload={"gap_seconds": gap})
                    await post_ops_alert(
                        f"Twitter stream degraded — gap {gap:.0f}s. Switching to GUMBO-direct alerts.",
                        client,
                    )
                elif was_degraded and gap <= _HEARTBEAT_THRESHOLD:
                    runtime_state.twitter_degraded = False
                    logger.info("Twitter stream recovered")
                    log_event("twitter_recovered", source="health", raw_payload={"gap_seconds": gap})
                    await post_ops_alert("Twitter stream recovered.", client)
            except Exception:
                logger.exception("health monitor failed")
