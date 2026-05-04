import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class RuntimeState:
    twitter_degraded: bool = False
    last_twitter_message_at: float = field(default_factory=time.time)
    last_schedule_success_at: float = 0.0
    schedule_api_degraded: bool = False


runtime_state = RuntimeState()

_GAME_HOURS_START = int(os.environ.get("GAME_HOURS_START", "12"))
# Default 6 spans midnight: noon UTC to 6 AM UTC (covers late west-coast games).
_GAME_HOURS_END = int(os.environ.get("GAME_HOURS_END", "6"))


def in_game_hours() -> bool:
    hour = datetime.now(timezone.utc).hour
    if _GAME_HOURS_START <= _GAME_HOURS_END:
        return _GAME_HOURS_START <= hour < _GAME_HOURS_END
    # Wraps midnight: e.g. START=12, END=6 means 12-23 and 0-5
    return hour >= _GAME_HOURS_START or hour < _GAME_HOURS_END
