import os
import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)
_db: aiosqlite.Connection | None = None


async def init_db() -> None:
    global _db
    if _db is not None:
        raise RuntimeError("DB already initialized")
    db_path = os.environ.get("DB_PATH", "/data/pinch-hit.db")
    logger.info("DB init path=%s", db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        _db = await aiosqlite.connect(db_path)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
        await _apply_migrations(_db)
    except Exception:
        logger.exception("DB init failed")
        _db = None
        raise


async def get_db() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("DB not initialized — call init_db() first")
    return _db


async def close_db() -> None:
    global _db
    if _db:
        await _db.close()
        _db = None


async def _apply_migrations(db: aiosqlite.Connection) -> None:
    await db.execute("CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY)")
    await db.commit()

    migrations_dir = Path(__file__).parent.parent.parent / "migrations"
    for migration_file in sorted(migrations_dir.glob("*.sql")):
        async with db.execute(
            "SELECT 1 FROM _migrations WHERE name = ?", (migration_file.name,)
        ) as cur:
            if await cur.fetchone():
                continue
        try:
            sql = migration_file.read_text()
            await db.executescript(sql)
            await db.execute(
                "INSERT INTO _migrations (name) VALUES (?)", (migration_file.name,)
            )
            await db.commit()
            logger.info("applied migration %s", migration_file.name)
        except Exception:
            logger.exception("migration failed: %s", migration_file)
            raise
