from __future__ import annotations

import asyncio
import json
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
from pinch_hit.consumers import twitter as twitter_mod  # noqa: E402
from pinch_hit.consumers.twitter import close_twitter, init_twitter, twitter_consumer  # noqa: E402
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


_REQUEST_TIMEOUT = 30.0


def _build_response(status: str, body: bytes) -> bytes:
    return f"HTTP/1.0 {status}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body


async def _read_http_request(reader: asyncio.StreamReader) -> tuple[str, str, int]:
    # readuntil is implicitly capped at 64 KB by asyncio.StreamReader's buffer limit.
    headers_raw = await reader.readuntil(b"\r\n\r\n")
    header_text = headers_raw.decode("iso-8859-1")
    lines = header_text.split("\r\n")
    request_line = lines[0]
    parts = request_line.split()
    if len(parts) != 3:
        raise ValueError("invalid request line")

    method, target, _version = parts
    path = target.split("?", 1)[0]

    content_length = 0
    for line in lines[1:]:
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())
            break

    return method.upper(), path, content_length


def _health_response() -> tuple[str, bytes]:
    now = time.time()
    schedule_gap = now - runtime_state.last_schedule_success_at
    schedule_unhealthy = runtime_state.schedule_api_degraded or (
        runtime_state.last_schedule_success_at > 0
        and schedule_gap > 5 * 60
    )

    if not runtime_state.twitter_degraded and not schedule_unhealthy:
        return "200 OK", b"ok"
    if schedule_unhealthy:
        return "503 Service Unavailable", f"unhealthy: schedule gap {schedule_gap:.0f}s".encode()
    return "503 Service Unavailable", b"unhealthy: twitter degraded"


async def _handle_test_tweet(reader: asyncio.StreamReader, content_length: int) -> tuple[str, bytes]:
    """Inject a fake tweet through the full pipeline (parsing → Discord)."""
    if content_length <= 0 or content_length > 4096:
        return "400 Bad Request", b"missing or oversized body"

    try:
        body = await asyncio.wait_for(reader.readexactly(content_length), timeout=_REQUEST_TIMEOUT)
    except (TimeoutError, asyncio.IncompleteReadError):
        return "400 Bad Request", b"body read failed"
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "400 Bad Request", b"invalid JSON"

    if not isinstance(data, dict) or "text" not in data:
        return "400 Bad Request", b'body must include "text" field'

    client = twitter_mod._client
    if client is None:
        return "503 Service Unavailable", b"twitter client not initialized"

    # Fill in defaults matching the WebSocket payload format
    data.setdefault("id", f"test-{int(time.time() * 1000)}")
    if "screen_name" not in data:
        data["screen_name"] = data.pop("reporter_handle", "")
    if "created_ms" not in data and "created_at" not in data and "createdAt" not in data:
        data["created_ms"] = int(time.time() * 1000)

    data = twitter_mod._normalize_ws_tweet(data)

    try:
        await twitter_mod._handle_message(data, client)
    except Exception:
        logger.exception("test-tweet handler error")
        return "500 Internal Server Error", b"internal error"

    return "200 OK", b"processed"


async def _handle_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        try:
            method, path, content_length = await asyncio.wait_for(
                _read_http_request(reader), timeout=_REQUEST_TIMEOUT
            )
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, UnicodeDecodeError, ValueError):
            logger.warning("bad HTTP request")
            writer.write(_build_response("400 Bad Request", b"bad request"))
            await writer.drain()
            return
        except TimeoutError:
            logger.warning("HTTP request timed out")
            writer.write(_build_response("408 Request Timeout", b"request timeout"))
            await writer.drain()
            return

        if method == "GET" and path == "/health":
            status, response_body = _health_response()
        elif method == "POST" and path == "/test-tweet" and os.environ.get("ENABLE_TEST_ROUTES"):
            status, response_body = await _handle_test_tweet(reader, content_length)
        else:
            status, response_body = "404 Not Found", b"not found"

        writer.write(_build_response(status, response_body))
        await writer.drain()
    except Exception:
        logger.exception("HTTP endpoint error")
        try:
            writer.write(_build_response("500 Internal Server Error", b"internal server error"))
            await writer.drain()
        except Exception:
            logger.exception("failed to write error response")
    finally:
        writer.close()
        await writer.wait_closed()


async def _health_server() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = await asyncio.start_server(_handle_http, "0.0.0.0", port)
    logger.info("HTTP server listening on port %s", port)
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

    try:
        await init_db()
        logger.info("DB initialized")

        await init_twitter()
        logger.info("Twitter initialized")

        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, lambda: _request_shutdown("SIGTERM"))
        loop.add_signal_handler(signal.SIGINT, lambda: _request_shutdown("SIGINT"))

        logger.info("starting: twitter_ws, gumbo, timeout, health, HTTP server, cleanup")

        _main_task = asyncio.gather(
            _supervise(twitter_consumer, "twitter_ws"),
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
        await close_twitter()
        await close_db()
        logger.info("shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
