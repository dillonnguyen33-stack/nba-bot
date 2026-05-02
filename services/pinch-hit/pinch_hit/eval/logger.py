import asyncio
import json
from typing import Any, Literal

from pinch_hit.state.db import get_db

EventType = Literal[
    "alert_fired",
    "tweet_rejected",
    "unmatched_substitution",
    "confirmed_substitution",
    "twitter_degraded",
    "alert_timeout",
    "twitter_recovered",
]

_background_tasks: set[asyncio.Task[None]] = set()


def _on_task_done(task: asyncio.Task[None]) -> None:
    _background_tasks.discard(task)
    if not task.cancelled() and task.exception():
        print(f"[eval log error] background write failed: {task.exception()}")


async def _write_row(
    event_type: EventType,
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
    event_type: EventType,
    source: str,
    game_pk: int | None = None,
    pinch_hitter: str | None = None,
    team_id: int | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget — caller never awaits the write."""
    payload_str = json.dumps(raw_payload) if raw_payload is not None else None
    task = asyncio.create_task(
        _write_row(event_type, source, game_pk, pinch_hitter, team_id, payload_str)
    )
    _background_tasks.add(task)
    task.add_done_callback(_on_task_done)
