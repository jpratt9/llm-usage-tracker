import json
import time
import urllib.error

import pytest

import usage
from datetime import datetime
from zoneinfo import ZoneInfo

from accounts import Account
from usage import (CLAUDE, CODEX, GEMINI, OUT_OF_USAGE, OUT_OF_USAGE_SECONDS, Usage, UsageError, keychain_service, limit_lifts_at,
                   off_report, turned_over, windows)

# The endpoint's answer, cut down to the two windows the page shows.
LIVE = {
    "five_hour": {"utilization": 23.0, "resets_at": "2026-09-16T05:40:00.444316+00:00"},
    "seven_day": {"utilization": 40.0, "resets_at": "2026-09-22T06:00:00.444336+00:00"},
    "seven_day_opus": None,
    "extra_usage": {"utilization": 37.8},
}


def spent(five_hour: float) -> dict:
    return {**LIVE, "five_hour": {"utilization": five_hour, "resets_at": None}}


def test_reads_the_session_and_weekly_windows_and_leaves_the_rest_alone():
    found = windows(LIVE)

    assert [w["key"] for w in found] == ["five_hour", "seven_day"]
    assert [w["label"] for w in found] == ["Session", "Weekly"]
    assert [w["span"] for w in found] == ["5h", "7d"]
    assert [w["used"] for w in found] == [23.0, 40.0]
    # How much is gone, never how much is left.
    assert all("left" not in w for w in found)
    assert found[0]["resets_at"] == pytest.approx(1789537200.444316)


def test_a_window_the_answer_leaves_out_is_left_out():
    assert windows({"five_hour": None, "seven_day": {"utilization": 5}}) == [
        {"key": "seven_day", "label": "Weekly", "span": "7d", "used": 5.0, "resets_at": None}
    ]


def test_passes_run_on_claude_until_the_session_window_is_past_the_fallback():
    assert Usage(fallback_at=95, read=lambda: spent(94.9), read_codex=list).engine() == CLAUDE
    assert Usage(fallback_at=95, read=lambda: spent(95.0), read_codex=list).engine() == CODEX
    assert Usage(fallback_at=95, read=lambda: spent(99.0), read_codex=list).engine() == CODEX
    # The fallback is a setting, not a constant.
    assert Usage(fallback_at=50, read=lambda: spent(60.0), read_codex=list).engine() == CODEX


def test_the_windows_are_read_once_a_cache_and_then_again():
    reads = []

    def read():
        reads.append(1)
        return LIVE

    usage = Usage(cache_seconds=60, read=read)
    usage.report()
    usage.report()
    assert len(reads) == 1

    usage.cached[CLAUDE]["tried_at"] -= 61
    usage.report()
    assert len(reads) == 2


def test_usage_that_cant_be_read_says_so_and_passes_the_login_by():
    def read():
        raise UsageError("Claude Code isn't signed in")

    report = Usage(read=read, read_codex=lambda: [], read_gemini=lambda: []).report()

    # Nothing is known of the login, so passes open on a CLI that is known
    # good rather than on one that may have nothing left to open with.
    assert report["engine"] == CODEX
    assert report["claude"]["ok"] is False
    assert report["claude"]["error"] == "Claude Code isn't signed in"
    assert report["claude"]["windows"] == []


CODEX_WINDOWS = [
    {"key": "primary", "label": "Session", "span": "5h", "used": 61.0, "resets_at": None},
    {"key": "secondary", "label": "Weekly", "span": "7d", "used": 12.0, "resets_at": None},
]


def test_the_report_carries_both_engines_and_the_fallback_the_page_shows():
    report = Usage(fallback_at=90, read=lambda: LIVE, read_codex=lambda: CODEX_WINDOWS).report()

    assert report["fallback_at"] == 90
    assert report["claude"]["ok"] is True and len(report["claude"]["windows"]) == 2
    assert report["codex"]["ok"] is True and report["codex"]["windows"] == CODEX_WINDOWS
    # Which login a pass that falls back would run as.
    assert report["codex"]["home"] == "~/.codex-5"


def test_each_engine_is_read_on_its_own_and_one_that_cant_be_read_says_why():
    def no_limits():
        raise UsageError("no limits recorded yet")

    report = Usage(read=lambda: LIVE, read_codex=no_limits).report()

    assert report["claude"]["ok"] is True
    assert report["codex"]["ok"] is False and report["codex"]["error"] == "no limits recorded yet"
    assert report["codex"]["windows"] == []


def test_where_nothing_reads_usage_the_page_is_told_so_and_passes_run_on_claude():
    report = off_report()

    assert report["engine"] == CLAUDE
    assert report["fallback_at"] is None
    assert report["claude"]["ok"] is False and report["claude"]["error"] == "not being read"


class FakeRun:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class FakeResponse:
    """What urlopen answers with, as a context manager json.load can read."""

    def __init__(self, body: str):
        self.body = body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return self.body


def test_the_token_comes_out_of_the_keychain_and_nothing_else(monkeypatch):
    asked = []

    def run(command, **kwargs):
        asked.append(command)
        return FakeRun(0, json.dumps({"claudeAiOauth": {"accessToken": "sk-oauth-token"}}))

    monkeypatch.setattr(usage.subprocess, "run", run)

    assert usage.access_token() == "sk-oauth-token"
    assert asked == [["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"]]


def test_a_keychain_that_says_no_or_holds_something_else_is_a_reason_not_a_crash(monkeypatch):
    monkeypatch.setattr(usage.subprocess, "run", lambda *a, **k: FakeRun(44, stderr="not found"))
    with pytest.raises(UsageError, match="isn't signed in"):
        usage.access_token()

    monkeypatch.setattr(usage.subprocess, "run", lambda *a, **k: FakeRun(0, '{"other": 1}'))
    with pytest.raises(UsageError, match="Keychain login isn't readable"):
        usage.access_token()


def test_the_endpoint_is_asked_as_the_usage_dialog_asks_it(monkeypatch):
    sent = []

    def urlopen(request, timeout=None):
        sent.append(request)
        return FakeResponse(json.dumps(LIVE))

    monkeypatch.setattr(usage.urllib.request, "urlopen", urlopen)

    assert usage.fetch_usage("sk-oauth-token") == LIVE
    request = sent[0]
    assert request.full_url == usage.USAGE_URL
    assert request.get_header("Authorization") == "Bearer sk-oauth-token"
    assert request.get_header("Anthropic-beta") == "oauth-2025-04-20"


def test_windows_wrapped_in_utilization_are_unwrapped(monkeypatch):
    monkeypatch.setattr(usage.urllib.request, "urlopen",
                        lambda request, timeout=None: FakeResponse(json.dumps({"utilization": LIVE})))

    assert usage.fetch_usage("sk-oauth-token") == LIVE


def test_an_endpoint_that_wont_answer_is_a_reason_not_a_crash(monkeypatch):
    def refuse(request, timeout=None):
        raise urllib.error.HTTPError(usage.USAGE_URL, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(usage.urllib.request, "urlopen", refuse)
    with pytest.raises(UsageError, match="usage endpoint said 401"):
        usage.fetch_usage("stale-token")

    monkeypatch.setattr(usage.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out")))
    with pytest.raises(UsageError, match="can't reach the usage endpoint"):
        usage.fetch_usage("sk-oauth-token")


def test_a_refusal_says_how_long_the_endpoint_asked_to_be_left_alone(monkeypatch):
    def refuse(request, timeout=None):
        raise urllib.error.HTTPError(usage.USAGE_URL, 429, "Too Many Requests", {"Retry-After": "3218"}, None)

    monkeypatch.setattr(usage.urllib.request, "urlopen", refuse)
    with pytest.raises(UsageError, match="usage endpoint said 429") as refused:
        usage.fetch_usage("sk-oauth-token")
    assert refused.value.retry_after == 3218


def test_a_login_its_cli_reports_on_shows_what_the_cli_said_while_the_endpoint_is_left_alone(monkeypatch):
    now = [READ_AT]
    monkeypatch.setattr(time, "time", lambda: now[0])
    asked = []

    def refuse(account):
        asked.append(account.name)
        raise UsageError("usage endpoint said 429", retry_after=3218)

    accounts = [Account("claude2", ("claude",), {"CLAUDE_CONFIG_DIR": "/opt/claude-2"})]
    usage = Usage(read=lambda: LIVE, read_codex=lambda: [], read_gemini=lambda: [], accounts=lambda: accounts,
                  read_account=refuse)
    assert usage.windows_for("claude2")["error"] == "usage endpoint said 429"

    # A step on claude2 reports its windows the way the CLI does: the share used
    # as a fraction, the reset in Unix seconds, and a window the page doesn't show.
    usage.heard("claude2", {"five_hour": {"utilization": 0.39, "resetsAt": 1789777200},
                            "seven_day": {"utilization": 0.82, "resetsAt": 1790244000},
                            "seven_day_overage_included": {"utilization": 0.1, "resetsAt": 1790244000}})
    usage.heard("claude2", {"five_hour": {}})
    found = usage.windows_for("claude2")
    assert found["ok"] and found["error"] is None and found["checked_at"] == READ_AT
    assert [(w["key"], w["label"], w["span"], round(w["used"], 2), w["resets_at"]) for w in found["windows"]] == [
        ("five_hour", "Session", "5h", 39.0, 1789777200.0), ("seven_day", "Weekly", "7d", 82.0, 1790244000.0),
    ]

    # The endpoint isn't asked again until the hour its Retry-After named.
    now[0] = READ_AT + 3000
    usage.windows_for("claude2")
    assert asked == ["claude2"]
    now[0] = READ_AT + 3300
    assert usage.windows_for("claude2")["windows"][0]["used"] == pytest.approx(39.0)
    assert asked == ["claude2", "claude2"]


def test_a_reading_that_failed_is_not_asked_for_again_straight_away():
    """The endpoint rate limits; asking again on the next poll is what gets it there."""
    tries = []

    def rate_limited():
        tries.append(1)
        raise UsageError("usage endpoint said 429")

    usage = Usage(cache_seconds=60, retry_after=300, read=rate_limited)
    usage.report()
    usage.cached[CLAUDE]["tried_at"] -= 61  # a normal cache would have asked again by now
    usage.report()
    assert len(tries) == 1

    usage.cached[CLAUDE]["tried_at"] -= 300
    usage.report()
    assert len(tries) == 2


def test_windows_that_cant_be_refreshed_keep_standing_with_the_reason(monkeypatch):
    answers = [LIVE, UsageError("usage endpoint said 429")]

    def flaky():
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    # An hour before LIVE's session window resets, with the clock still running:
    # a window the clock has passed is dropped, whatever today's date is.
    real = time.time
    behind = real() - (windows(LIVE)[0]["resets_at"] - 3600)
    monkeypatch.setattr(time, "time", lambda: real() - behind)
    usage = Usage(read=flaky)
    good = usage.report()["claude"]
    usage.cached[CLAUDE]["tried_at"] -= 61

    stale = usage.report()["claude"]

    # The numbers are minutes old, which says far more than an error does.
    assert stale["windows"] == good["windows"]
    assert stale["ok"] is True
    assert "429" in stale["error"]
    assert stale["checked_at"] == good["checked_at"]  # when they were read, not when it last tried
    assert stale["tried_at"] > good["tried_at"]


def test_a_first_reading_that_fails_has_no_windows_to_stand_on():
    def refused():
        raise UsageError("Claude Code isn't signed in")

    claude = Usage(read=refused).report()["claude"]

    assert claude["ok"] is False and claude["windows"] == [] and claude["checked_at"] is None


def test_a_window_counts_as_reset_once_the_clock_passes_what_it_said():
    at = 1_789_537_200.0
    assert turned_over({"resets_at": at}, now=at - 1) is False
    assert turned_over({"resets_at": at}, now=at) is True
    # A window that never said when it resets can't be called either way.
    assert turned_over({"resets_at": None}, now=at) is False


def test_a_run_on_codex_comes_back_to_claude_when_the_window_resets(monkeypatch):
    spent_at = {"five_hour": {"utilization": 96.0, "resets_at": "2026-09-16T05:40:00+00:00"},
                "seven_day": {"utilization": 40.0, "resets_at": "2026-09-22T06:00:00+00:00"}}
    reset = 1_789_537_200.0  # what that five_hour resets_at comes to
    answers = [spent_at, UsageError("usage endpoint said 429")]

    def flaky():
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    now = [reset - 600]
    monkeypatch.setattr(time, "time", lambda: now[0])
    usage = Usage(fallback_at=95, read=flaky, read_codex=list)
    assert usage.engine() == CODEX

    # The endpoint is down, so the spent reading is all there is — until the
    # clock passes the hour it said the window lifts.
    now[0] = reset + 1

    assert usage.engine() == CLAUDE
    claude = usage.report()["claude"]
    # The window that turned over is gone; the weekly one still stands.
    assert [w["key"] for w in claude["windows"]] == ["seven_day"]
    assert "429" in claude["error"]


NEW_YORK = ZoneInfo("America/New_York")
# When the spend-limit message below was read.
READ_AT = datetime(2026, 9, 16, 10, 53, 36, tzinfo=NEW_YORK).timestamp()
SPENT = ("You've hit your monthly spend limit · raise it at claude.ai/settings/usage?from=cc_cli_limit_message · "
         "your session limit resets 11:40am (America/New_York)")


def new_york(*when: int) -> float:
    return datetime(*when, tzinfo=NEW_YORK).timestamp()


@pytest.mark.parametrize("message", [
    SPENT,
    "You've hit your session limit · resets 11:40am (America/New_York)",
    "5-hour limit reached ∙ resets 3pm",
    "Claude AI usage limit reached|1789545600",
    "You've hit your usage limit. Upgrade to Pro or try again later.",
])
def test_a_cli_out_of_usage_is_told_apart(message):
    assert OUT_OF_USAGE.search(message)


@pytest.mark.parametrize("message", [
    "API Error: 429 rate_limit_error",
    "Rate limit reached for requests",
    "claude exited with code 1 before finishing.",
])
def test_a_rate_limit_or_a_crash_is_not_being_out_of_usage(message):
    assert not OUT_OF_USAGE.search(message)


def test_the_hour_claude_says_it_is_back_is_read_off_its_message():
    assert limit_lifts_at(SPENT, READ_AT) == new_york(2026, 9, 16, 11, 40)
    assert limit_lifts_at("Your limit will reset at 3pm (America/New_York).", READ_AT) == new_york(2026, 9, 16, 15)
    assert limit_lifts_at("You've hit your weekly limit · resets Sep 22, 2am (America/New_York)",
                          READ_AT) == new_york(2026, 9, 22, 2)
    assert limit_lifts_at("Claude AI usage limit reached|1789545600", READ_AT) == 1789545600
    # A time long gone is tomorrow's; one that only just went by is the limit lifting now.
    assert limit_lifts_at("resets 9am (America/New_York)", READ_AT) == new_york(2026, 9, 17, 9)
    assert limit_lifts_at("resets 10:40am (America/New_York)", READ_AT) == new_york(2026, 9, 16, 10, 40)
    assert limit_lifts_at("resets 12am (America/New_York)", READ_AT) == new_york(2026, 9, 17, 0)


def test_a_message_that_doesnt_say_when_gives_no_hour():
    assert limit_lifts_at("You've hit your usage limit. Try again later.", READ_AT) is None
    assert limit_lifts_at("resets Feb 30, 2am (America/New_York)", READ_AT) is None
    # A zone nobody has heard of falls back on this machine's clock rather than failing.
    assert limit_lifts_at("resets 3pm (Nowhere/Special)", READ_AT) is not None


def test_claude_saying_it_is_out_moves_passes_to_codex_until_the_hour_it_named(monkeypatch):
    now = [READ_AT]
    monkeypatch.setattr(time, "time", lambda: now[0])
    usage = Usage(read=lambda: spent(10.0), read_codex=lambda: [])
    back = new_york(2026, 9, 16, 11, 40)

    usage.claude_out(back)
    assert usage.engine() == CODEX
    report = usage.report()
    assert report["engine"] == CODEX and report["claude_back_at"] == back

    now[0] = back
    assert usage.engine() == CLAUDE
    assert usage.back_at() is None


def test_claude_out_without_a_time_it_is_back_holds_off_for_a_while(monkeypatch):
    now = [READ_AT]
    monkeypatch.setattr(time, "time", lambda: now[0])
    usage = Usage(read=lambda: spent(10.0), read_codex=lambda: [])

    usage.claude_out(None)
    assert usage.back_at() == READ_AT + OUT_OF_USAGE_SECONDS
    # An hour already gone says nothing about now either.
    usage.claude_out(READ_AT - 60)
    assert usage.back_at() == READ_AT + OUT_OF_USAGE_SECONDS
    now[0] = READ_AT + OUT_OF_USAGE_SECONDS
    assert usage.engine() == CLAUDE


def test_a_spent_session_window_says_claude_is_back_at_its_reset(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: READ_AT)
    reading = {**LIVE, "five_hour": {"utilization": 97.0, "resets_at": "2026-09-16T15:40:00+00:00"}}
    usage = Usage(fallback_at=95, read=lambda: reading, read_codex=lambda: [])

    assert usage.back_at() == new_york(2026, 9, 16, 11, 40)
    assert Usage(fallback_at=95, read=lambda: spent(40.0), read_codex=lambda: []).back_at() is None
    assert off_report()["claude_back_at"] is None


def test_an_alias_account_is_read_off_its_own_keychain_login(monkeypatch):
    asked = []
    monkeypatch.setattr(usage, "access_token", lambda service: asked.append(service) or "token")
    monkeypatch.setattr(usage, "fetch_usage", lambda token: LIVE)

    assert usage.read_account(Account("claude2", ("claude",), {"CLAUDE_CONFIG_DIR": "/Users/john/.claude-2"})) == LIVE
    assert asked == ["Claude Code-credentials-c3642f9b"]
    assert keychain_service(None) == "Claude Code-credentials"


def test_passes_try_codex_and_gemini_before_each_alias_account(monkeypatch):
    now = [READ_AT]
    monkeypatch.setattr(time, "time", lambda: now[0])
    accounts = [Account("claude2", ("claude",), {"CLAUDE_CONFIG_DIR": "/opt/claude-2"}),
                Account("claude3", ("claude",), {})]
    readings = {"claude2": spent(10.0), "claude3": spent(10.0)}
    usage = Usage(fallback_at=95, read=lambda: spent(97.0), read_codex=lambda: [], read_gemini=lambda: [],
                  accounts=lambda: accounts, read_account=lambda account: readings[account.name])
    back = new_york(2026, 9, 16, 11, 40)

    assert usage.engines() == [CLAUDE, CODEX, GEMINI, "claude2", "claude3"]
    # Claude's own session window is spent, so passes open on Codex rather than the next Claude account.
    assert usage.engine() == CODEX
    usage.claude_out(back, CODEX)
    assert usage.engine() == GEMINI
    # Only with Codex and Gemini both out do they move on to the first alias account.
    usage.claude_out(back, GEMINI)
    assert usage.engine() == "claude2"
    assert usage.account("claude2") is accounts[0] and usage.account(CLAUDE) is None
    # claude2 being out moves them on to claude3 while Codex and Gemini still are.
    usage.claude_out(back + 3600, "claude2")
    assert usage.engine() == "claude3"
    report = usage.report()
    assert report["engine"] == "claude3"
    assert [(a["name"], a["home"], a["back_at"], a["windows"][0]["used"]) for a in report["accounts"]] == [
        ("claude2", "/opt/claude-2", back + 3600, 10.0), ("claude3", None, None, 10.0),
    ]
    assert usage.is_claude_out("claude2") and not usage.is_claude_out()

    # Codex is back before claude2 is, and passes go to it ahead of claude3.
    now[0] = back
    assert usage.engine() == CODEX


def test_a_claude_login_out_for_its_week_rotates_passes_to_the_next_one_before_codex_and_gemini(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: READ_AT)
    accounts = [Account("claude2", ("claude",), {}), Account("claude3", ("claude",), {})]
    week_spent = {**LIVE, "five_hour": {"utilization": 0.0, "resets_at": None},
                  "seven_day": {"utilization": 100.0, "resets_at": None}}
    readings = {"claude2": spent(10.0), "claude3": spent(10.0)}
    usage = Usage(fallback_at=95, cache_seconds=0, read=lambda: week_spent, read_codex=lambda: [],
                  read_gemini=lambda: [], accounts=lambda: accounts, read_account=lambda account: readings[account.name])

    # Claude's own login is out for days, so passes go to claude2 though Codex and Gemini have room.
    assert usage.engines() == [CLAUDE, "claude2", CODEX, GEMINI, "claude3"]
    assert usage.engine() == "claude2"
    # claude2's session running out is over within hours: Codex stands in for it rather than claude3.
    readings["claude2"] = spent(97.0)
    assert usage.engine() == CODEX
    # claude2 saying it is out until tomorrow is its week gone too, so passes rotate on to claude3.
    usage.claude_out(READ_AT + 24 * 60 * 60, "claude2")
    assert usage.engines() == [CLAUDE, "claude2", "claude3", CODEX, GEMINI]
    assert usage.engine() == "claude3"


def test_an_alias_that_is_gone_says_so_instead_of_windows():
    usage = Usage(read=lambda: LIVE, read_codex=lambda: [], accounts=lambda: [])

    assert usage.windows_for("claude2")["error"] == "there's no claude2 alias anymore"
    assert off_report()["accounts"] == []


def test_a_spent_weekly_window_moves_passes_on_and_codex_stops_at_the_fallback_too(monkeypatch):
    now = [READ_AT]
    monkeypatch.setattr(time, "time", lambda: now[0])
    weekly_reset = new_york(2026, 9, 22, 2)
    claude = {**LIVE, "five_hour": {"utilization": 10.0, "resets_at": None},
              "seven_day": {"utilization": 100.0, "resets_at": datetime.fromtimestamp(weekly_reset, NEW_YORK).isoformat()}}
    codex = {"used": 96.0}
    usage = Usage(fallback_at=95, cache_seconds=0, read=lambda: claude, read_codex=lambda: [
        {"key": "secondary", "label": "Weekly", "span": "7d", "used": codex["used"], "resets_at": new_york(2026, 9, 20, 9)},
    ], read_gemini=lambda: [
        {"key": "seven_day", "label": "Weekly", "span": "7d", "used": 100.0, "resets_at": new_york(2026, 9, 21, 9)},
    ])

    # Claude's session window has room, but its weekly one is spent, and so are
    # Codex's and Gemini's: nothing takes the pass.
    assert not usage.takes_passes(CLAUDE) and not usage.takes_passes(CODEX) and not usage.takes_passes(GEMINI)
    assert usage.engine() is None
    assert usage.back_at() == weekly_reset
    report = usage.report()
    assert report["engine"] is None and report["codex"]["back_at"] == new_york(2026, 9, 20, 9)

    codex["used"] = 94.0
    assert usage.engine() == CODEX and usage.back_at(CODEX) is None


def test_a_spent_codex_is_back_at_its_reset_though_its_last_turn_still_reads_spent(monkeypatch):
    now = [READ_AT]
    monkeypatch.setattr(time, "time", lambda: now[0])
    reset = new_york(2026, 9, 16, 13)
    # Codex's reading is off its last turn, and none runs on it while it reads
    # spent, so it says 100% for as long as nothing else moves it.
    usage = Usage(fallback_at=95, read=lambda: spent(99.0), read_codex=lambda: [
        {"key": "primary", "label": "Session", "span": "5h", "used": 100.0, "resets_at": reset},
    ], read_gemini=lambda: [
        {"key": "seven_day", "label": "Weekly", "span": "7d", "used": 100.0, "resets_at": new_york(2026, 9, 21, 9)},
    ])
    assert usage.engine() is None and usage.back_at(CODEX) == reset

    now[0] = reset + 1
    assert usage.engine() == CODEX and usage.back_at(CODEX) is None


def test_an_engine_is_back_once_everything_holding_it_off_is_over(monkeypatch):
    now = [READ_AT]
    monkeypatch.setattr(time, "time", lambda: now[0])
    session, weekly = new_york(2026, 9, 16, 13), new_york(2026, 9, 22, 2)
    reading = {"five_hour": {"utilization": 97.0, "resets_at": datetime.fromtimestamp(session, NEW_YORK).isoformat()},
               "seven_day": {"utilization": 99.0, "resets_at": datetime.fromtimestamp(weekly, NEW_YORK).isoformat()}}
    usage = Usage(fallback_at=95, read=lambda: reading, read_codex=lambda: [])

    assert usage.back_at() == weekly
    usage.claude_out(new_york(2026, 9, 23, 9))
    assert usage.back_at() == new_york(2026, 9, 23, 9)
    # A spent window that doesn't say when it resets leaves nothing to count down to.
    reading["five_hour"]["resets_at"] = None
    assert Usage(fallback_at=95, read=lambda: reading, read_codex=lambda: []).back_at() is None


def test_a_login_nothing_can_be_read_of_is_passed_by_rather_than_guessed_at(monkeypatch):
    now = [READ_AT]
    monkeypatch.setattr(time, "time", lambda: now[0])
    accounts = [Account("claude2", ("claude",), {}), Account("claude3", ("claude",), {})]
    readings = {"claude2": spent(10.0), "claude3": spent(10.0)}
    refused = [True]

    def read():
        if refused[0]:
            raise UsageError("usage endpoint said 429")
        return LIVE

    usage = Usage(fallback_at=95, cache_seconds=0, read=read, read_codex=lambda: [], read_gemini=lambda: [],
                  accounts=lambda: accounts, read_account=lambda account: readings[account.name])

    # Nothing has ever come back for Claude's own login, so how much of it is
    # gone is a guess. Passes rotate on to the next login rather than opening
    # on it to be told what the refusal already said.
    assert usage.unreadable(CLAUDE) and not usage.takes_passes(CLAUDE)
    assert usage.engines() == [CLAUDE, "claude2", CODEX, GEMINI, "claude3"]
    assert usage.engine() == "claude2"
    assert usage.claude()["error"] == "usage endpoint said 429"

    # It is back as soon as a reading comes back; nothing has to be reset.
    refused[0] = False
    now[0] = READ_AT + usage.retry_after
    assert not usage.unreadable(CLAUDE) and usage.engine() == CLAUDE


def test_a_login_its_cli_reported_on_is_known_though_the_endpoint_never_answered(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: READ_AT)

    def refused():
        raise UsageError("usage endpoint said 429", retry_after=3218)

    usage = Usage(fallback_at=95, read=refused, read_codex=lambda: [], read_gemini=lambda: [], accounts=lambda: [])
    assert usage.unreadable(CLAUDE)

    # A step's own report is a reading as good as the endpoint's (`heard`):
    # the share used as a fraction, the reset in Unix seconds.
    usage.heard(CLAUDE, {"five_hour": {"utilization": 0.12, "resetsAt": READ_AT + 3600}})
    assert not usage.unreadable(CLAUDE) and usage.engine() == CLAUDE


def test_with_nothing_readable_at_all_a_pass_opens_rather_than_waiting_on_a_number(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: READ_AT)

    def refused(*args):
        raise UsageError("can't reach the usage endpoint")

    usage = Usage(fallback_at=95, cache_seconds=0, read=refused, read_codex=refused, read_gemini=refused,
                  accounts=lambda: [])

    # Not knowing passes an engine by only while there is one that is known
    # good; with none, the loop tries rather than waiting for the endpoint.
    assert all(usage.unreadable(engine) for engine in (CLAUDE, CODEX, GEMINI))
    assert usage.engine() == CLAUDE


# A reading whose windows never turn over, so a kept one keeps standing.
STEADY = {"five_hour": {"utilization": 23.0, "resets_at": None}, "seven_day": {"utilization": 40.0, "resets_at": None}}


def test_processes_sharing_a_file_share_one_reading(tmp_path):
    reads = []

    def read():
        reads.append(1)
        return STEADY

    shared = tmp_path / "usage.json"
    loop, menu_bar = Usage(read=read, shared=shared), Usage(read=read, shared=shared)

    assert loop.claude()["windows"] == menu_bar.claude()["windows"]
    assert len(reads) == 1


def test_the_last_good_reading_outlives_a_restart_with_the_reason_it_isnt_refreshing(tmp_path):
    shared = tmp_path / "usage.json"
    Usage(read=lambda: STEADY, shared=shared).claude()

    def refused():
        raise UsageError("usage endpoint said 401")

    claude = Usage(cache_seconds=0, read=refused, shared=shared).claude()

    assert [window["used"] for window in claude["windows"]] == [23.0, 40.0]
    assert claude["ok"] and claude["error"] == "usage endpoint said 401"


def test_a_refusal_one_process_is_given_holds_every_process_sharing_the_file_off(tmp_path):
    shared = tmp_path / "usage.json"
    asked = []

    def rate_limited():
        asked.append(1)
        raise UsageError("usage endpoint said 429", retry_after=600)

    Usage(cache_seconds=0, retry_after=0, read=rate_limited, shared=shared).claude()
    Usage(cache_seconds=0, retry_after=0, read=rate_limited, shared=shared).claude()

    assert len(asked) == 1


def test_a_login_one_process_hears_is_out_is_out_for_every_process_sharing_the_file(tmp_path):
    shared = tmp_path / "usage.json"
    loop = Usage(read=lambda: STEADY, read_codex=list, read_gemini=list, shared=shared)
    menu_bar = Usage(read=lambda: STEADY, read_codex=list, read_gemini=list, shared=shared)

    loop.claude_out(time.time() + 60 * 60)

    assert menu_bar.engine() == CODEX


def test_a_garbled_shared_file_reads_as_nothing_kept_and_is_written_whole_again(tmp_path):
    shared = tmp_path / "usage.json"
    shared.write_text("{not json")

    assert Usage(read=lambda: STEADY, shared=shared).claude()["windows"][0]["used"] == 23.0
    assert json.loads(shared.read_text())["cached"]["claude"]["windows"][0]["used"] == 23.0
