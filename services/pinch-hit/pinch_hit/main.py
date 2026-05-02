import asyncio
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pinch_hit.consumers.twitter as twitter_mod
from pinch_hit.consumers.gumbo import schedule_poller
from pinch_hit.consumers.twitter import twitter_consumer
from pinch_hit.state.db import close_db, init_db
from pinch_hit.state.repository import nightly_cleanup
from pinch_hit.workers.health import health_monitor
from pinch_hit.workers.timeout import timeout_watcher

_main_task: asyncio.Task[None] | None = None


# -- SUPERVISOR ----------------------------------------------------------------


async def _supervise(coro_fn, name: str) -> None:
    """Restart coro_fn on unhandled exception. Never returns until cancelled."""
    while True:
        try:
            await coro_fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[{name} error] crashed: {e} -- restarting in 5s")
            await asyncio.sleep(5)


# -- HEALTH ENDPOINT ----------------------------------------------------------


def _in_game_hours() -> bool:
    game_hours_start = int(os.environ.get("GAME_HOURS_START", "12"))
    game_hours_end = int(os.environ.get("GAME_HOURS_END", "25"))
    hour = datetime.now(timezone.utc).hour
    if game_hours_end <= 24:
        return game_hours_start <= hour < game_hours_end
    return hour >= game_hours_start or hour < (game_hours_end - 24)


async def _handle_health(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    now = time.time()
    gap = now - twitter_mod.last_message_at
    in_window = _in_game_hours()

    if not in_window or gap <= 120:
        status = "200 OK"
        body = b"ok"
    else:
        status = "503 Service Unavailable"
        body = f"unhealthy: twitter gap {gap:.0f}s".encode()

    response = f"HTTP/1.0 {status}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
    writer.write(response)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _health_server() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = await asyncio.start_server(_handle_health, "0.0.0.0", port)
    print(f"[main] /health listening on port {port}")
    async with server:
        await server.serve_forever()


# -- NIGHTLY CLEANUP ----------------------------------------------------------


def _seconds_until_midnight_et() -> float:
    ET = ZoneInfo("America/New_York")
    now = datetime.now(ET)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now).total_seconds()


async def _nightly_cleanup_loop() -> None:
    while True:
        secs = _seconds_until_midnight_et()
        await asyncio.sleep(secs)
        try:
            await nightly_cleanup()
            print("[cleanup] nightly cleanup complete")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[cleanup error] {e}")


# -- SHUTDOWN ------------------------------------------------------------------


def _request_shutdown(sig_name: str) -> None:
    print(f"[main] received {sig_name}, shutting down")
    if _main_task is not None:
        _main_task.cancel()


# -- ENTRY POINT ---------------------------------------------------------------


async def main() -> None:
    global _main_task

    await init_db()
    print("[main] DB initialized")

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: _request_shutdown("SIGTERM"))
    loop.add_signal_handler(signal.SIGINT, lambda: _request_shutdown("SIGINT"))

    print("[main] starting: twitter, gumbo, timeout, health, /health server, cleanup")

    _main_task = asyncio.create_task(
        asyncio.gather(
            _supervise(twitter_consumer, "twitter"),
            _supervise(schedule_poller, "gumbo"),
            _supervise(timeout_watcher, "timeout"),
            _supervise(health_monitor, "health"),
            _health_server(),
            _supervise(_nightly_cleanup_loop, "cleanup"),
        )
    )

    try:
        await _main_task
    except asyncio.CancelledError:
        print("[main] cancelled -- draining in-flight work (5s)")
        await asyncio.sleep(5)
    finally:
        await close_db()
        print("[main] shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
