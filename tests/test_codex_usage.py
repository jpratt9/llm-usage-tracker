import json
import time
from pathlib import Path

import pytest

from codex_usage import (NO_LIMITS, NO_TURNS, CodexUsageError, limit_windows, read_limits, rollout_thread,
                         session_context, span_label)

THREAD = "01a0a7dd-fabe-7b30-82f6-18b7e4b9523b"
LIMITS = {
    "limit_id": "premium",
    "plan_type": "pro",
    "credits": {"has_credits": False, "unlimited": False, "balance": None},
    "primary": {"used_percent": 61.0, "window_minutes": 300, "resets_at": 1_789_537_200},
    "secondary": {"used_percent": 12.0, "window_minutes": 10_080, "resets_in_seconds": 3_600},
}
# What codex records when a turn never ran: the envelope is there, the numbers aren't.
REFUSED = {"limit_id": "premium", "plan_type": None, "primary": None, "secondary": None,
           "credits": {"has_credits": False, "unlimited": False, "balance": None}}
INFO = {
    "model_context_window": 272_000,
    "last_token_usage": {"total_tokens": 68_000},
    "total_token_usage": {"input_tokens": 120_000, "cached_input_tokens": 90_000, "output_tokens": 4_000},
}


def write_rollout(home: Path, thread: str, events: list[dict], model: str = "gpt-5.6-terra",
                  day: str = "2026/09/15", when: str = "2026-09-15T21-38-57") -> Path:
    """A rollout file shaped like the ones codex writes, with `events` in it."""
    folder = home / "sessions" / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-{when}-{thread}.jsonl"
    lines = [{"type": "session_meta", "payload": {"session_id": thread, "source": "exec"}},
             {"type": "turn_context", "payload": {"turn_id": "t1", "model": model}}]
    lines += [{"type": "event_msg", "payload": event} for event in events]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def token_count(info=None, limits=None) -> dict:
    return {"type": "token_count", "info": info, "rate_limits": limits}


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / ".codex-5"


def test_a_window_is_named_by_how_long_it_runs():
    assert span_label(300) == "5h"
    assert span_label(60) == "1h"
    assert span_label(10_080) == "7d"
    assert span_label(1_440) == "1d"


def test_the_windows_come_out_shortest_first_as_a_share_used(home):
    write_rollout(home, THREAD, [token_count(INFO, LIMITS)])

    session, weekly = read_limits(home)

    assert (session["key"], session["label"], session["span"]) == ("primary", "Session", "5h")
    assert session["used"] == 61.0 and session["resets_at"] == 1_789_537_200
    assert (weekly["label"], weekly["span"], weekly["used"]) == ("Weekly", "7d", 12.0)
    # How much is gone, never how much is left.
    assert all("left" not in window for window in (session, weekly))


def test_a_window_that_counts_down_instead_of_naming_a_time_is_turned_into_one():
    [weekly] = limit_windows({"secondary": {"used_percent": 5, "window_minutes": 10_080,
                                            "resets_in_seconds": 600}})

    assert weekly["resets_at"] == pytest.approx(time.time() + 600, abs=5)


def test_a_turn_that_never_ran_records_no_windows_and_is_passed_over(home):
    write_rollout(home, THREAD, [token_count(None, REFUSED)])

    with pytest.raises(CodexUsageError, match=NO_LIMITS):
        read_limits(home)


def test_the_newest_turn_that_did_record_windows_is_the_one_read(home):
    write_rollout(home, THREAD, [token_count(INFO, LIMITS)], when="2026-09-15T09-00-00")
    later = write_rollout(home, "01a0a7dd-fabe-7b30-82f6-000000000002",
                          [token_count(None, REFUSED)], when="2026-09-15T21-00-00")
    later.touch()

    # The newer rollout has no windows, so the older one still answers.
    assert [window["used"] for window in read_limits(home)] == [61.0, 12.0]


def test_a_codex_that_has_never_run_says_so_rather_than_showing_nothing(home):
    home.mkdir(parents=True)

    with pytest.raises(CodexUsageError, match=NO_TURNS):
        read_limits(home)


def test_a_rollout_is_matched_to_its_thread_by_the_id_its_name_ends_with():
    assert rollout_thread(Path(f"rollout-2026-09-15T21-38-57-{THREAD}.jsonl")) == THREAD
    assert rollout_thread(Path("rollout-nothing.jsonl")) == ""


def test_a_threads_context_is_what_its_last_turn_was_holding(home):
    write_rollout(home, THREAD, [token_count(INFO, LIMITS)])

    assert session_context(THREAD, home) == {"model": "gpt-5.6-terra", "window": 272_000, "used": 68_000}


def test_another_threads_context_is_never_read_as_this_ones(home):
    write_rollout(home, THREAD, [token_count(INFO, LIMITS)])

    assert session_context("01a0a7dd-fabe-7b30-82f6-000000000009", home) is None


def test_a_turn_that_recorded_no_context_leaves_the_reading_alone(home):
    write_rollout(home, THREAD, [token_count(None, REFUSED)])

    assert session_context(THREAD, home) is None
