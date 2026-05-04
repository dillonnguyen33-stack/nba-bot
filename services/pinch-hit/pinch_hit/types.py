from typing import Literal

AlertStatus = Literal["pending", "confirmed", "timeout"]

STATUS_PENDING: AlertStatus = "pending"
STATUS_CONFIRMED: AlertStatus = "confirmed"
STATUS_TIMEOUT: AlertStatus = "timeout"

EventType = Literal[
    "alert_fired",
    "alert_confirmed",
    "fallback_alert",
    "mlb_substitution",
    "tweet_parsed",
    "tweet_received",
    "tweet_rejected",
    "unmatched_substitution",
    "twitter_degraded",
    "alert_timeout",
    "twitter_recovered",
    "schedule_api_outage",
    "schedule_api_recovered",
]
