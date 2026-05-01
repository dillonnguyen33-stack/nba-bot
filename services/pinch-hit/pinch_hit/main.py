import asyncio

from pinch_hit.state.db import init_db, close_db


async def main() -> None:
    await init_db()
    # Consumer and worker tasks wired here: asyncio.gather(twitter_consumer(), ...)
    print("[main] Foundation initialized — no tasks running yet")
    await asyncio.sleep(0)


if __name__ == "__main__":
    asyncio.run(main())
