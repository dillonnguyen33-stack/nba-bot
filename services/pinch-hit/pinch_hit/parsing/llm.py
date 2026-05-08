import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_OPENAI_URL = "https://api.openai.com/v1/chat/completions"

_SYSTEM_PROMPT = """You analyze MLB tweets that mention pinch hitting.

Determine if the tweet is announcing a substitution happening RIGHT NOW, and if so, extract player names.

VALID = a player IS ABOUT TO or IS CURRENTLY pinch hitting (the substitution is being announced).
INVALID = what happened AFTER a pinch hit (result, stats), a question about a past event, a celebration of a completed at-bat, or a historical reference.

Return JSON with:
- "is_current_event": true only if this announces a substitution happening now
- "pinch_hitter": name of player coming in (or null)
- "replaced_player": name of player being replaced (or null)

Rules:
- If is_current_event is false, pinch_hitter and replaced_player MUST be null
- Extract names exactly as written in the tweet
- A single last name is valid (e.g., "Osuna")
- "PH" and "ph" mean "pinch hit"

VALID — announcing a current substitution:
"Osuna is in to pinch hit for Foscue" → {"is_current_event":true,"pinch_hitter":"Osuna","replaced_player":"Foscue"}
"PH: Osuna for Foscue" → {"is_current_event":true,"pinch_hitter":"Osuna","replaced_player":"Foscue"}
"Rangers send Osuna to pinch hit" → {"is_current_event":true,"pinch_hitter":"Osuna","replaced_player":null}
"Pinch hitting for Foscue is Duran" → {"is_current_event":true,"pinch_hitter":"Duran","replaced_player":"Foscue"}
"Osuna pinch-hits for Foscue" → {"is_current_event":true,"pinch_hitter":"Osuna","replaced_player":"Foscue"}
"Simpson coming in to PH for Garcia" → {"is_current_event":true,"pinch_hitter":"Simpson","replaced_player":"Garcia"}
"Osuna to pinch hit here in the 7th" → {"is_current_event":true,"pinch_hitter":"Osuna","replaced_player":null}
"Simpson will pinch hit for Garcia" → {"is_current_event":true,"pinch_hitter":"Simpson","replaced_player":"Garcia"}

INVALID — result, celebration, question, history, or commentary:
"Schwarber hit a pinch-hit homer last night" → {"is_current_event":false,"pinch_hitter":null,"replaced_player":null}
"CHANDLER SIMPSON PINCH HIT 2 RUN RBI SINGLE. LETS GOOOOOOOO!!!!!!" → {"is_current_event":false,"pinch_hitter":null,"replaced_player":null}
"Alek Thomas launched a pinch-hit two-run homer in the eighth inning" → {"is_current_event":false,"pinch_hitter":null,"replaced_player":null}
"Wtf did they just have Felix Reyes PH for Bryce Harper?" → {"is_current_event":false,"pinch_hitter":null,"replaced_player":null}
"Pinch-hit grand slam by Osuna! Dodgers take the lead!" → {"is_current_event":false,"pinch_hitter":null,"replaced_player":null}
"Osuna pinch-hit RBI double ties it at 3" → {"is_current_event":false,"pinch_hitter":null,"replaced_player":null}
"Incredible pinch-hit walk-off homer by Reyes!" → {"is_current_event":false,"pinch_hitter":null,"replaced_player":null}
"Thomas has been the best pinch hitter all season, batting .385" → {"is_current_event":false,"pinch_hitter":null,"replaced_player":null}
"That might be the best pinch hit I've ever seen" → {"is_current_event":false,"pinch_hitter":null,"replaced_player":null}
"One of the greatest pinch-hit moments in World Series history" → {"is_current_event":false,"pinch_hitter":null,"replaced_player":null}"""

_llm_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient | None:
    global _llm_client
    if _llm_client is not None:
        return _llm_client
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return None
    _llm_client = httpx.AsyncClient(
        timeout=httpx.Timeout(3.0, connect=1.0),
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


from pinch_hit.parsing import PlayerPair


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
                        "is_current_event": {"type": "boolean"},
                        "pinch_hitter": {"type": ["string", "null"]},
                        "replaced_player": {"type": ["string", "null"]},
                    },
                    "required": ["is_current_event", "pinch_hitter", "replaced_player"],
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

    is_current = result.get("is_current_event", False)
    hitter = result.get("pinch_hitter") or None
    replaced = result.get("replaced_player") or None

    if not is_current:
        if hitter:
            logger.info("LLM rejected non-current event: hitter=%s text=%s", hitter, text[:80])
        return None, None

    logger.info("LLM extracted: hitter=%s replaced=%s from: %s", hitter, replaced, text[:80])
    return hitter, replaced
