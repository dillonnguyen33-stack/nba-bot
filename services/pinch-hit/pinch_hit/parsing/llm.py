import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_OPENAI_URL = "https://api.openai.com/v1/chat/completions"

_SYSTEM_PROMPT = """You extract player names from MLB pinch-hit tweets.

Return JSON with:
- "pinch_hitter": name of player coming in to pinch hit (or null)
- "replaced_player": name of player being replaced (or null)

Rules:
- Extract names exactly as written in the tweet
- A single last name is valid (e.g., "Osuna")
- "PH" and "ph" mean "pinch hit"
- If this is NOT about a current pinch-hit event, return nulls

Examples:
"Osuna is in to pinch hit for Foscue" → {"pinch_hitter":"Osuna","replaced_player":"Foscue"}
"PH: Osuna for Foscue" → {"pinch_hitter":"Osuna","replaced_player":"Foscue"}
"Rangers send Osuna to pinch hit" → {"pinch_hitter":"Osuna","replaced_player":null}
"Pinch hitting for Foscue is Duran" → {"pinch_hitter":"Duran","replaced_player":"Foscue"}
"Osuna pinch-hits for Foscue" → {"pinch_hitter":"Osuna","replaced_player":"Foscue"}
"Schwarber hit a pinch-hit homer last night" → {"pinch_hitter":null,"replaced_player":null}"""

_llm_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient | None:
    global _llm_client
    if _llm_client is not None:
        return _llm_client
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return None
    _llm_client = httpx.AsyncClient(
        timeout=httpx.Timeout(5.0, connect=1.0),
        headers={"Authorization": f"Bearer {api_key}"},
    )
    return _llm_client


async def close_llm_client() -> None:
    global _llm_client
    if _llm_client is not None:
        try:
            await _llm_client.aclose()
        except Exception:
            logger.warning("error closing LLM client", exc_info=True)
        _llm_client = None


PlayerPair = tuple[str | None, str | None]


async def extract_players_llm(text: str) -> PlayerPair:
    client = _get_client()
    if client is None:
        return None, None

    payload = {
        "model": "gpt-4o-mini",
        "temperature": 0,
        "max_tokens": 80,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "player_extraction",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "pinch_hitter": {"type": ["string", "null"]},
                        "replaced_player": {"type": ["string", "null"]},
                    },
                    "required": ["pinch_hitter", "replaced_player"],
                    "additionalProperties": False,
                },
            },
        },
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }

    try:
        r = await client.post(_OPENAI_URL, json=payload)
        r.raise_for_status()
    except httpx.HTTPError:
        logger.warning("LLM extraction failed for: %s", text[:80], exc_info=True)
        return None, None

    try:
        content = r.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError):
        logger.warning("LLM returned unparseable response for: %s", text[:80])
        return None, None

    hitter = result.get("pinch_hitter") or None
    replaced = result.get("replaced_player") or None

    logger.info("LLM extracted: hitter=%s replaced=%s from: %s", hitter, replaced, text[:80])
    return hitter, replaced
