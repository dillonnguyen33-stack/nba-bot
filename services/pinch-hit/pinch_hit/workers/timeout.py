import asyncio
import logging
import os

import httpx

from pinch_hit.alerts import DiscordEmbed
from pinch_hit.alerts.discord import build_timeout_embed, fetch_current_embed, patch_embed
from pinch_hit.eval.logger import log_event
from pinch_hit.state.repository import bulk_update_alerts_timeout, get_expired_pending_alerts

logger = logging.getLogger(__name__)


async def timeout_watcher() -> None:
    timeout_minutes = int(os.environ.get("TIMEOUT_MINUTES", "5"))
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            await asyncio.sleep(30)
            try:
                expired = await get_expired_pending_alerts(timeout_minutes)
                timed_out_ids: list[int] = []
                for alert in expired:
                    try:
                        current = await fetch_current_embed(alert["discord_message_id"], client)
                        fallback: DiscordEmbed = {"title": alert["pinch_hitter_raw"], "color": 0}
                        grey = build_timeout_embed(current or fallback)
                        patched = await patch_embed(alert["discord_message_id"], grey, client)
                        if not patched:
                            logger.error("Discord timeout PATCH failed for alert %s", alert["id"])
                            continue
                        log_event(
                            "alert_timeout",
                            source="timeout",
                            pinch_hitter=alert["pinch_hitter_raw"],
                            team_id=alert["team_id"],
                            raw_payload={"alert_id": alert["id"]},
                        )
                        timed_out_ids.append(alert["id"])
                    except Exception:
                        logger.exception("timeout failed for alert %s", alert["id"])
                if timed_out_ids:
                    await bulk_update_alerts_timeout(timed_out_ids)
                    logger.info("timed out %s alert(s)", len(timed_out_ids))
            except Exception:
                logger.exception("timeout watcher failed")
