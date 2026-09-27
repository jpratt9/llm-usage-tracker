import json
import subprocess

import pytest
from gemini_usage import GeminiUsageError, buckets, read_limits, resets_at, used

# What `agy -p /usage --output-format json` answers, cut to the fields read.
ENVELOPE = {
    "status": "SUCCESS",
    "command": {"name": "usage", "data": {"groups": [
        {"name": "Gemini Models", "buckets": [
            {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.5899485349655151,
             "reset_time": "2026-09-23T02:23:39Z"},
            {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.9804500937461853,
             "reset_time": "2026-09-18T04:30:23Z"},
        ]},
        # agy fronts other makers' models on a cap of their own, which says
        # nothing about the one a pass will spend.
        {"name": "Claude and GPT models", "buckets": [
            {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 1, "reset_time": "2026-09-24T23:48:12Z"},
        ]},
    ]}},
}


def answered(stdout: str, returncode: int = 0, stderr: str = ""):
    def run(command, **kwargs):
        run.command = command
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)
    return run


def test_the_gemini_groups_windows_are_read_as_how_much_is_gone():
    run = answered(json.dumps(ENVELOPE))

    assert read_limits("agy", run) == [
        {"key": "five_hour", "label": "Session", "span": "5h", "used": 2.0,
         "resets_at": resets_at("2026-09-18T04:30:23Z")},
        {"key": "seven_day", "label": "Weekly", "span": "7d", "used": 41.0,
         "resets_at": resets_at("2026-09-23T02:23:39Z")},
    ]
    # /usage runs no model turn, so nothing of the account's quota goes on reading it.
    assert run.command[:4] == ["agy", "-p", "/usage", "--output-format"]


def test_only_the_gemini_group_counts():
    assert [bucket["id"] for bucket in buckets(ENVELOPE)] == ["gemini-weekly", "gemini-5h"]
    assert buckets({"command": {"data": {"groups": []}}}) == []


def test_how_much_is_gone_is_the_share_left_turned_over():
    assert used(1) == 0.0
    assert used(0) == 100.0
    assert used(0.5899485349655151) == 41.0
    # Nothing to read, and a share outside 0..1, say nothing rather than lying.
    assert used(None) is None and used("most of it") is None
    assert used(1.5) == 0.0 and used(-1) == 100.0


def test_a_reset_agy_did_not_give_leaves_the_window_without_one():
    assert resets_at("2026-09-18T04:30:23Z") == pytest.approx(1789705823.0)
    assert resets_at(None) is None and resets_at("soon") is None


def test_agy_that_will_not_answer_says_why():
    with pytest.raises(GeminiUsageError, match="without its limits"):
        read_limits("agy", answered("agy: not signed in\n", returncode=1))
    with pytest.raises(GeminiUsageError, match="without its limit groups"):
        read_limits("agy", answered(json.dumps({"status": "SUCCESS"})))
    with pytest.raises(GeminiUsageError, match="no Gemini limits"):
        read_limits("agy", answered(json.dumps({"command": {"data": {"groups": [ENVELOPE["command"]["data"]["groups"][1]]}}})))

    def missing(command, **kwargs):
        raise FileNotFoundError("no agy on this machine")

    with pytest.raises(GeminiUsageError, match="wouldn't run"):
        read_limits("agy", missing)
