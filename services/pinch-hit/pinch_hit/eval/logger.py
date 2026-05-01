import asyncio
import json

from pinch_hit.state.db import get_db


async def _write_row(
    event_type: str,
    source: str,
    game_pk: int | None,
    pinch_hitter: str | None,
    team_id: int | None,
    raw_payload: str | None,
) -> None:
    try:
        db = await get_db()
        await db.execute(
            """INSERT INTO evaluation_log
               (event_type, source, game_pk, pinch_hitter, team_id, raw_payload)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_type, source, game_pk, pinch_hitter, team_id, raw_payload),
        )
        await db.commit()
    except Exception as e:
        print(f"[eval log error] {e}")


def log_event(
    event_type: str,
    source: str,
    game_pk: int | None = None,
    pinch_hitter: str | None = None,
    team_id: int | None = None,
    raw_payload: dict | None = None,
) -> None:
    """Schedule a non-blocking eval log write. Never awaited by caller."""
    payload_str = json.dumps(raw_payload) if raw_payload is not None else None
    asyncio.create_task(
        _write_row(event_type, source, game_pk, pinch_hitter, team_id, payload_str)
    )


async def nightly_cleanup() -> None:
    """Delete stale rows. Wired to scheduler in Phase 4."""
    try:
        db = await get_db()
        await db.execute(
            "DELETE FROM seen_tweets WHERE seen_at < datetime('now', '-1 day')"
        )
        await db.execute(
            """DELETE FROM pending_alerts
               WHERE status IN ('confirmed', 'timeout')
               AND posted_at < datetime('now', '-7 days')"""
        )
        await db.commit()
    except Exception as e:
        print(f"[cleanup error] {e}")
