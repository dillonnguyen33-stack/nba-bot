import logging
import os

import httpx

logger = logging.getLogger(__name__)

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
        if client is not None:
            r = await client.post(url, json={"content": message})
            r.raise_for_status()
        else:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(url, json={"content": message})
                r.raise_for_status()
    except (httpx.HTTPError, ValueError):
        logger.critical("ops post failed — ops channel is unreachable", exc_info=True)
