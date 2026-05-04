CREATE TABLE evaluation_log_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'alert_fired',
        'alert_confirmed',
        'fallback_alert',
        'mlb_substitution',
        'tweet_parsed',
        'tweet_received',
        'tweet_rejected',
        'unmatched_substitution',
        'confirmed_substitution',
        'twitter_degraded',
        'alert_timeout',
        'twitter_recovered',
        'schedule_api_outage',
        'schedule_api_recovered'
    )),
    source TEXT NOT NULL,
    game_pk INTEGER,
    pinch_hitter TEXT,
    team_id INTEGER,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    raw_payload TEXT
);

INSERT INTO evaluation_log_new
    (id, event_type, source, game_pk, pinch_hitter, team_id, timestamp, raw_payload)
SELECT id, event_type, source, game_pk, pinch_hitter, team_id, timestamp, raw_payload
FROM evaluation_log;

DROP TABLE evaluation_log;

ALTER TABLE evaluation_log_new RENAME TO evaluation_log;

CREATE INDEX IF NOT EXISTS idx_eval_time ON evaluation_log(timestamp);
