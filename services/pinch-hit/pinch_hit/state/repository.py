from typing import Literal, TypedDict

from pinch_hit.state.db import get_db

AlertStatus = Literal["pending", "confirmed", "timeout"]

STATUS_CONFIRMED: AlertStatus = "confirmed"


class PendingAlertRow(TypedDict):
    id: int
    discord_message_id: str
    pinch_hitter_raw: str
    pinch_hitter_normalized: str
    replaced_player: str | None
    team_id: int
    game_pk: int | None
    tweet_id: str
    posted_at: str
    confirmed_at: str | None
    status: AlertStatus


async def insert_pending_alert(
    discord_message_id: str,
    pinch_hitter_raw: str,
    pinch_hitter_normalized: str,
    team_id: int,
    tweet_id: str,
    replaced_player: str | None = None,
    game_pk: int | None = None,
) -> int:
    db = await get_db()
    cursor = await db.execute(
        """INSERT INTO pending_alerts
           (discord_message_id, pinch_hitter_raw, pinch_hitter_normalized,
            replaced_player, team_id, game_pk, tweet_id, posted_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), 'pending')""",
        (discord_message_id, pinch_hitter_raw, pinch_hitter_normalized,
         replaced_player, team_id, game_pk, tweet_id),
    )
    await db.commit()
    if cursor.lastrowid is None:
        raise RuntimeError("insert_pending_alert: no row ID returned")
    return cursor.lastrowid


async def get_pending_alerts_by_team(team_id: int) -> list[PendingAlertRow]:
    db = await get_db()
    async with db.execute(
        "SELECT * FROM pending_alerts WHERE status = 'pending' AND team_id = ?",
        (team_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def update_alert_status(alert_id: int, status: AlertStatus, game_pk: int | None = None) -> None:
    db = await get_db()
    if status == STATUS_CONFIRMED:
        await db.execute(
            "UPDATE pending_alerts SET status = ?, confirmed_at = datetime('now'), game_pk = COALESCE(?, game_pk) WHERE id = ?",
            (status, game_pk, alert_id),
        )
    else:
        await db.execute(
            "UPDATE pending_alerts SET status = ? WHERE id = ?",
            (status, alert_id),
        )
    await db.commit()


async def get_expired_pending_alerts(timeout_minutes: int) -> list[PendingAlertRow]:
    db = await get_db()
    async with db.execute(
        """SELECT * FROM pending_alerts
           WHERE status = 'pending'
           AND posted_at < datetime('now', ? || ' minutes')""",
        (f"-{timeout_minutes}",),
    ) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def bulk_update_alerts_timeout(alert_ids: list[int]) -> None:
    if not alert_ids:
        return
    db = await get_db()
    placeholders = ",".join("?" * len(alert_ids))
    await db.execute(
        f"UPDATE pending_alerts SET status = 'timeout' WHERE id IN ({placeholders})",
        alert_ids,
    )
    await db.commit()


async def insert_seen_tweet(tweet_id: str) -> None:
    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO seen_tweets (tweet_id) VALUES (?)",
        (tweet_id,),
    )
    await db.commit()


async def is_tweet_seen(tweet_id: str) -> bool:
    db = await get_db()
    async with db.execute(
        "SELECT 1 FROM seen_tweets WHERE tweet_id = ?", (tweet_id,)
    ) as cursor:
        return await cursor.fetchone() is not None


async def nightly_cleanup() -> None:
    """Prevent unbounded DB growth on the Railway persistent volume."""
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
