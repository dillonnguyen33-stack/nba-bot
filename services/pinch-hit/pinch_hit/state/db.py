import os
from pathlib import Path

import aiosqlite

_db: aiosqlite.Connection | None = None


async def init_db() -> None:
    global _db
    if _db is not None:
        raise RuntimeError("DB already initialized")
    db_path = os.getenv("DB_PATH", "/data/pinch-hit.db")
    print(f"[db init] path={db_path}")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        _db = await aiosqlite.connect(db_path)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
        await _apply_migrations(_db)
    except Exception as e:
        print(f"[db init error] {e}")
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
    migrations_dir = Path(__file__).parent.parent.parent / "migrations"
    migration_file = migrations_dir / "001_initial.sql"
    try:
        sql = migration_file.read_text()
        await db.executescript(sql)
    except Exception as e:
        print(f"[migration error] {migration_file}: {e}")
        raise
