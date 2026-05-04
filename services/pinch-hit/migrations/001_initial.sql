CREATE TABLE IF NOT EXISTS pending_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_message_id TEXT NOT NULL,
    pinch_hitter_raw TEXT NOT NULL,
    pinch_hitter_normalized TEXT NOT NULL,
    replaced_player TEXT,
    team_id INTEGER NOT NULL,
    game_pk INTEGER,
    tweet_id TEXT NOT NULL,
    posted_at TIMESTAMP NOT NULL,
    confirmed_at TIMESTAMP,
    status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'timeout')),
    UNIQUE(tweet_id)
);

CREATE INDEX IF NOT EXISTS idx_pending ON pending_alerts(status, posted_at);
CREATE INDEX IF NOT EXISTS idx_match ON pending_alerts(team_id, pinch_hitter_normalized, status);

CREATE TABLE IF NOT EXISTS seen_tweets (
    tweet_id TEXT PRIMARY KEY,
    seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS evaluation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    game_pk INTEGER,
    pinch_hitter TEXT,
    team_id INTEGER,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    raw_payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_eval_time ON evaluation_log(timestamp);
