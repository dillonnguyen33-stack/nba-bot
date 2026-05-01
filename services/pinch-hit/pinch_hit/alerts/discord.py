import os

import httpx


def _webhook_url() -> str:
    return os.environ.get("PINCH_HIT_WEBHOOK_URL", "")


async def post_initial_alert(
    pinch_hitter: str,
    team: str,
    tweet_text: str,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """
    Posts plain-text pinch-hit alert to Discord webhook and returns message ID.
    Returns None on failure — caller skips DB insert.
    """
    url = _webhook_url()
    if not url:
        print("[discord error] PINCH_HIT_WEBHOOK_URL not set")
        return None
    content = f"[PINCH HIT] {pinch_hitter} ({team})\n{tweet_text}"
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=10) as c:
                return await _post(c, url, content)
        return await _post(client, url, content)
    except (httpx.HTTPError, httpx.TimeoutException, ValueError) as e:
        print(f"[discord error] {type(e).__name__}: {e}")
        return None


async def _post(client: httpx.AsyncClient, url: str, content: str) -> str | None:
    r = await client.post(
        f"{url}?wait=true",
        json={"content": content},
    )
    r.raise_for_status()
    data = r.json()
    msg_id = data.get("id")
    return str(msg_id) if msg_id else None
