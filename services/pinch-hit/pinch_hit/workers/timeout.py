import asyncio
import os

import httpx

from pinch_hit.alerts.discord import build_timeout_embed, patch_embed
from pinch_hit.eval.logger import log_event
from pinch_hit.state.repository import bulk_update_alerts_timeout, get_expired_pending_alerts


async def _fetch_current_embed(message_id: str, client: httpx.AsyncClient) -> dict | None:
    # Duplicated from gumbo.py — each module owns its Discord fetch.
    url = os.environ.get("PINCH_HIT_WEBHOOK_URL", "")
    try:
        r = await client.get(f"{url}/messages/{message_id}")
        r.raise_for_status()
        return r.json()["embeds"][0]
    except (httpx.HTTPError, ValueError, IndexError) as e:
        print(f"[timeout error] fetch embed {message_id}: {type(e).__name__}: {e}")
        return None


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
                        current = await _fetch_current_embed(alert["discord_message_id"], client)
                        grey = build_timeout_embed(current or {})
                        await patch_embed(alert["discord_message_id"], grey, client)
                        log_event(
                            "alert_timeout",
                            source="timeout",
                            pinch_hitter=alert["pinch_hitter_raw"],
                            team_id=alert["team_id"],
                            raw_payload={"alert_id": alert["id"]},
                        )
                        timed_out_ids.append(alert["id"])
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        print(f"[timeout error] alert {alert['id']}: {e}")
                if timed_out_ids:
                    await bulk_update_alerts_timeout(timed_out_ids)
                    print(f"[timeout] timed out {len(timed_out_ids)} alert(s)")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[timeout error] {e}")
