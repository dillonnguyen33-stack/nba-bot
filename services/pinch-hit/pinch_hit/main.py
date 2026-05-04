from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def _load_env() -> None:
    """Load .env file from project root if it exists. No-op on Railway."""
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value and len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_env()

from pinch_hit.consumers.gumbo import schedule_poller  # noqa: E402
from pinch_hit.consumers.twitter import twitter_consumer  # noqa: E402
from pinch_hit.logging_config import configure_logging  # noqa: E402
from pinch_hit.state.background import drain_background_tasks  # noqa: E402
from pinch_hit.state.db import close_db, init_db  # noqa: E402
from pinch_hit.state.repository import nightly_cleanup  # noqa: E402
from pinch_hit.state.runtime import runtime_state  # noqa: E402
from pinch_hit.workers.health import health_monitor  # noqa: E402
from pinch_hit.workers.timeout import timeout_watcher  # noqa: E402

configure_logging()
logger = logging.getLogger(__name__)

_main_task: asyncio.Future[Any] | None = None


async def _supervise(coro_fn: "Callable[[], Coroutine[Any, Any, None]]", name: str) -> None:
    """Restart coro_fn on unhandled exception. Never returns until cancelled."""
    while True:
        try:
            await coro_fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s crashed; restarting in 5s", name)
            await asyncio.sleep(5)


async def _handle_health(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await reader.read(4096)  # consume HTTP request to prevent pipeline stall

        now = time.time()
        schedule_gap = now - runtime_state.last_schedule_success_at
        schedule_unhealthy = runtime_state.schedule_api_degraded or (
            runtime_state.last_schedule_success_at > 0
            and schedule_gap > 5 * 60
        )

        if not runtime_state.twitter_degraded and not schedule_unhealthy:
            status = "200 OK"
            body = b"ok"
        elif schedule_unhealthy:
            status = "503 Service Unavailable"
            body = f"unhealthy: schedule gap {schedule_gap:.0f}s".encode()
        else:
            status = "503 Service Unavailable"
            body = b"unhealthy: twitter degraded"

        response = f"HTTP/1.0 {status}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
        writer.write(response)
        await writer.drain()
    except Exception:
        logger.exception("health endpoint error")
        try:
            body = b"health endpoint error"
            response = b"HTTP/1.0 500 Internal Server Error\r\nContent-Length: %d\r\n\r\n%s" % (
                len(body),
                body,
            )
            writer.write(response)
            await writer.drain()
        except Exception:
            logger.exception("failed to write health endpoint error response")
    finally:
        writer.close()
        await writer.wait_closed()


async def _health_server() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = await asyncio.start_server(_handle_health, "0.0.0.0", port)
    logger.info("/health listening on port %s", port)
    async with server:
        await server.serve_forever()


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
            logger.info("nightly cleanup complete")
        except Exception:
            logger.exception("nightly cleanup failed")
        await asyncio.sleep(60)  # guard against tight retry if _supervise restarts


def _request_shutdown(sig_name: str) -> None:
    logger.info("received %s, shutting down", sig_name)
    if _main_task is not None:
        _main_task.cancel()


async def main() -> None:
    global _main_task

    # Fail fast on missing required env vars
    for var in ("TWITTERAPI_IO_KEY", "PINCH_HIT_WEBHOOK_URL"):
        if not os.environ.get(var):
            raise RuntimeError(f"{var} must be set")

    await init_db()
    logger.info("DB initialized")

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: _request_shutdown("SIGTERM"))
    loop.add_signal_handler(signal.SIGINT, lambda: _request_shutdown("SIGINT"))

    logger.info("starting: twitter, gumbo, timeout, health, /health server, cleanup")

    _main_task = asyncio.gather(
        _supervise(twitter_consumer, "twitter"),
        _supervise(schedule_poller, "gumbo"),
        _supervise(timeout_watcher, "timeout"),
        _supervise(health_monitor, "health"),
        _supervise(_health_server, "health_server"),
        _supervise(_nightly_cleanup_loop, "cleanup"),
    )

    try:
        await _main_task
    except asyncio.CancelledError:
        logger.info("cancelled; draining in-flight background tasks")
        await drain_background_tasks(timeout=5.0)
    finally:
        await close_db()
        logger.info("shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
