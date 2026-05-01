import asyncio

from pinch_hit.state.db import init_db


async def main() -> None:
    await init_db()
    # Wire consumer/worker tasks here when twitter_consumer is implemented


if __name__ == "__main__":
    asyncio.run(main())
