import asyncio
import logging
import time

import httpx

from pinch_hit.alerts.ops import post_ops_alert
from pinch_hit.eval.logger import log_event
from pinch_hit.state.runtime import in_game_hours, runtime_state

logger = logging.getLogger(__name__)

_HEARTBEAT_THRESHOLD = 300.0   # seconds — gap that triggers degraded state
_MAX_CONSECUTIVE_FAILURES = 5


async def health_monitor() -> None:
    consecutive_failures = 0
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
                    logger.warning("Twitter webhook degraded gap=%.0fs", gap)
                    log_event("twitter_degraded", source="health", raw_payload={"gap_seconds": gap})
                    await post_ops_alert(
                        f"Twitter webhook degraded — gap {gap:.0f}s. Switching to GUMBO-direct alerts.",
                        client,
                    )
                elif was_degraded and gap <= _HEARTBEAT_THRESHOLD:
                    runtime_state.twitter_degraded = False
                    logger.info("Twitter webhook recovered")
                    log_event("twitter_recovered", source="health", raw_payload={"gap_seconds": gap})
                    await post_ops_alert("Twitter webhook recovered.", client)
                consecutive_failures = 0
            except Exception:
                consecutive_failures += 1
                logger.exception("health monitor failed consecutive_failures=%s", consecutive_failures)
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    raise
