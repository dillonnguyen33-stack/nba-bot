import os
from pathlib import Path

import aiosqlite

_db: aiosqlite.Connection | None = None


async def init_db() -> None:
    global _db
    db_path = os.getenv("DB_PATH", "/data/pinch-hit.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(db_path)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA foreign_keys=ON")
    await _apply_migrations(_db)


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
    migrations_dir = Path(__file__).parent.parent.parent / "migrations"
    sql = (migrations_dir / "001_initial.sql").read_text()
    await db.executescript(sql)
