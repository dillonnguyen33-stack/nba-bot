import asyncio

from pinch_hit.state.db import init_db


async def main() -> None:
    await init_db()


if __name__ == "__main__":
    asyncio.run(main())
