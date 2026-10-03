"""Tests for bot.py's correction detection — no network, no Discord.

Run:  python test_bot.py
"""
import bot

SHOT = {"actionType": "2pt", "personId": 1, "playerNameI": "J. Brunson"}
REB = {"actionType": "rebound", "personId": 7, "playerNameI": "M. Robinson"}


def test_assist_added_removed_and_switched():
    with_ast = dict(SHOT, assistPersonId=5, assistPlayerNameInitial="M. Bridges")
    other_ast = dict(SHOT, assistPersonId=6, assistPlayerNameInitial="J. Hart")
    assert bot.diff_plays(SHOT, with_ast) == [("ast", None, 5)]
    assert bot.diff_plays(with_ast, SHOT) == [("ast", 5, None)]
    assert bot.diff_plays(with_ast, other_ast) == [("ast", 5, 6)]
    assert bot.classify_correction("ast", None, 5) == ("added", "assist added")
    assert bot.classify_correction("ast", 5, None) == ("removed", "assist removed")
    assert bot.classify_correction("ast", 5, 6) == ("mixup", "assist mixup")


def test_unchanged_play_reports_nothing():
    assert bot.diff_plays(SHOT, dict(SHOT)) == []
    assert bot.diff_plays(REB, dict(REB, reboundTotal=4)) == []


def test_rebound_switched_between_players():
    new = dict(REB, personId=8, playerNameI="K. Towns")
    assert bot.diff_plays(REB, new) == [("reb", 7, 8)]
    assert bot.classify_correction("reb", 7, 8) == ("mixup", "rebound mixup")
    assert bot.credited_name(REB, "reb") == "M. Robinson"
    assert bot.credited_name(new, "reb") == "K. Towns"


def test_team_rebound_given_to_or_taken_from_a_player():
    team = {"actionType": "rebound", "personId": 0, "teamTricode": "NYK"}
    assert bot.diff_plays(team, REB) == [("reb", None, 7)]
    assert bot.diff_plays(REB, team) == [("reb", 7, None)]
    assert bot.classify_correction("reb", None, 7) == ("added", "rebound added")
    assert bot.classify_correction("reb", 7, None) == ("removed", "rebound removed")
    assert bot.credited_name(team, "reb") is None


def test_shooter_changing_on_a_shot_is_not_a_rebound_correction():
    assert bot.diff_plays(SHOT, dict(SHOT, personId=2)) == []


def test_changes_on_the_next_poll_or_inside_the_window_are_filtered():
    assert bot.too_fast(elapsed=10.4, polls_seen=1)      # the "10s" alerts
    assert bot.too_fast(elapsed=12.9, polls_seen=1)      # slow poll cycle, still next poll
    assert bot.too_fast(elapsed=14.0, polls_seen=2)      # inside the minimum window
    assert not bot.too_fast(elapsed=21.0, polls_seen=2)
    assert not bot.too_fast(elapsed=161.0, polls_seen=15)  # the 2m 41s alert


def test_deleted_play_needs_several_missing_polls():
    plays = {str(i): dict(SHOT, actionNumber=i) for i in range(10, 40)}
    snap = {pid: (play, 0, 5) for pid, play in plays.items()}
    snap["2"] = (REB, 0, 5)
    missing = {}
    assert bot.find_deleted_plays(snap, plays, missing, 100) == []
    assert bot.find_deleted_plays(snap, plays, missing, 110) == []
    assert bot.find_deleted_plays(snap, plays, missing, 120) == ["2"]
    assert missing["2"] == (3, 100)  # remembers when it first went missing
    assert bot.credits_lost_if_deleted(REB) == [("reb", 7)]


def test_play_that_comes_back_is_not_deleted():
    plays = {str(i): dict(SHOT, actionNumber=i) for i in range(10, 40)}
    snap = {pid: (play, 0, 5) for pid, play in plays.items()}
    snap["2"] = (REB, 0, 5)
    missing = {}
    bot.find_deleted_plays(snap, plays, missing, 100)
    bot.find_deleted_plays(snap, plays, missing, 110)
    assert missing["2"][0] == 2
    assert bot.find_deleted_plays(snap, dict(plays, **{"2": REB}), missing, 120) == []
    assert missing == {}


def test_truncated_feed_does_not_count_as_deletions():
    snap = {str(i): (dict(REB, actionNumber=i), 0, 5) for i in range(100)}
    half = {str(i): snap[str(i)][0] for i in range(50)}
    missing = {}
    for now in (100, 110, 120, 130):
        assert bot.find_deleted_plays(snap, half, missing, now) == []
    assert missing == {}


def test_deleted_plain_shot_or_team_rebound_is_ignored():
    team = {"actionType": "rebound", "personId": 0}
    kept = {str(i): dict(REB, actionNumber=i) for i in range(10, 40)}
    snap = {pid: (play, 0, 5) for pid, play in kept.items()}
    snap.update({"1": (SHOT, 0, 5), "2": (team, 0, 5)})
    missing = {}
    for now in (100, 110, 120):
        deleted = bot.find_deleted_plays(snap, kept, missing, now)
    assert deleted == [] and missing == {}


def test_deleted_assisted_shot_loses_the_assist():
    with_ast = dict(SHOT, assistPersonId=5, assistPlayerNameInitial="M. Bridges")
    assert bot.credits_lost_if_deleted(with_ast) == [("ast", 5)]
    assert bot.credits_lost_if_deleted(SHOT) == []


def test_rebound_alert_text():
    sent = {}

    class _Resp:
        def raise_for_status(self):
            pass

    def fake_post(url, json=None, timeout=None):
        sent.update(json)
        return _Resp()

    real_post = bot.requests.post
    bot.requests.post = fake_post
    try:
        new = dict(REB, personId=8, playerNameI="K. Towns", period=3, clock="PT08M36.00S",
                   actionNumber=385, description="K. Towns REBOUND (Off:1 Def:5)")
        bot.post_to_discord({"gameCode": "20260613/NYKSAS"}, new, REB,
                            "mixup", "rebound mixup", "reb", 7, 8, 161)
    finally:
        bot.requests.post = real_post
    text = sent["embeds"][0]["description"]
    assert "**K. Towns** — rebound mixup" in text
    assert "Taken from: **M. Robinson**" in text and "Given to: **K. Towns**" in text
    assert "2m 41s" in text


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} tests passed.")
