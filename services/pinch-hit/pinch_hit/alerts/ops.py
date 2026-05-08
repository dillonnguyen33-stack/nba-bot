import logging
import os

import httpx

from pinch_hit.alerts.discord import ensure_client

logger = logging.getLogger(__name__)

# Warn once — ops alerts are frequent during normal operation
_warned_no_url = False


async def post_ops_alert(message: str, client: httpx.AsyncClient | None = None) -> None:
    global _warned_no_url
    url = os.environ.get("OPS_WEBHOOK_URL", "")
    if not url:
        if not _warned_no_url:
            logger.warning("OPS_WEBHOOK_URL not set; ops alerts disabled")
            _warned_no_url = True
        return
    try:
        async with ensure_client(client) as c:
            r = await c.post(url, json={"content": message})
            r.raise_for_status()
    except (httpx.HTTPError, ValueError):
        logger.critical("ops post failed — ops channel is unreachable", exc_info=True)
