"""Live Claude Code usage, and which CLI a pass runs on.

The numbers are the ones /usage shows: Claude Code keeps an OAuth token in the
macOS Keychain, and the dialog asks an endpoint on api.anthropic.com for the
five-hour session window and the seven-day weekly window. The token is read for
the request and never kept, printed or logged.

The page polls, so the answer is cached: one Keychain read and one request a
minute at most, however many runs are open. That endpoint rate limits, so a
reading that fails is not asked for again for RETRY_AFTER_SECONDS or, where the
refusal's Retry-After says when to come back, until right then and no later
(Usage.follow_up_at says when that is), and the windows the last good reading
found keep standing in the meantime — numbers a few minutes old say far more
than an error does. The login it rate limits is the one runs are busy on, and
there the CLI is what keeps the numbers current: every step reports the windows
of the login it runs as, off the answers it gets (see claude_step.py), and each
report stands in for a reading of the endpoint (Usage.heard).

Given `shared`, all of that is kept in a file (SHARED) that every process on
this Mac reading usage through here shares — the loop and the menu bar alike —
so between them each CLI is still read once a minute at most, a refusal's
Retry-After holds every one of them off, and the last good windows outlive a
restart.

Nothing here raises into a run or a request. A Keychain that says no, a token
that has expired or an endpoint that is down comes back as an answer carrying
the reason, and the loop carries on with Claude.

Once the session or the weekly window is spent past `fallback_at` percent,
passes run through Codex instead (see codex_step.py), and they come back to
Claude when that window resets — the answer says when, so even an endpoint that
stays down doesn't hold a run on Codex past the hour its limit lifted. Codex
stops at `fallback_at` percent of its own windows the same way, and with every
CLI spent past it a pass waits for the one back soonest. A step Claude ends by
saying it is out of usage (OUT_OF_USAGE) moves passes to Codex the same way,
until the hour that message says Claude is back, whatever the last reading said.
Codex has windows of its own, read a different way again — off the rollout files
it writes its turns to, in codex_usage.py — and this puts the two side by side
for the page.

Gemini comes after Codex, through the Antigravity CLI (see gemini_step.py). Its
windows are the same two spans, asked of agy's own /usage in gemini_usage.py, so
a pass only reaches it once Claude and Codex are spent past the fallback, and it
is passed by in turn once its own are.

Given `accounts`, the Claude accounts other zsh aliases sign in to (accounts.py)
follow Claude's own login in turn. Passes stay on the first login that isn't
exhausted — its weekly window spent, or out for longer than a session window —
and while only its session window is spent, Codex and Gemini stand in until it
is back rather than the next account. Once it is exhausted, passes rotate on to
the next account before Codex and Gemini. Each account's windows are read the
way Claude's are, off its own Keychain login.
"""

import fcntl
import hashlib
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import codex_usage
import gemini_usage
from accounts import Account
from codex_usage import CODEX_HOME

CLAUDE = "claude"
CODEX = "codex"
GEMINI = "gemini"
KEYCHAIN_SERVICE = "Claude Code-credentials"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# The two windows the page shows, in the order it shows them.
WINDOWS = (("five_hour", "Session", "5h"), ("seven_day", "Weekly", "7d"))
# Percent of a session or weekly window that moves the next pass off that CLI.
FALLBACK_AT = 95.0
CACHE_SECONDS = 60
# A reading that failed isn't asked for again this soon, unless its refusal said
# when to come back. The usage endpoint rate limits, and asking straight back is
# what gets it there.
RETRY_AFTER_SECONDS = 300
TIMEOUT_SECONDS = 15
# The file every process reading usage through here keeps its readings in (see
# Usage's `shared`).
SHARED = Path.home() / "Library" / "Caches" / "llm-usage-tracker" / "usage.json"
OFF_REASON = "not being read"
# What a CLI says when it ends a step because the plan's usage is gone: Claude's
# session, weekly or spend limit, Codex's usage limit. An API rate limit is a
# blip that passes on its own, not one of these.
OUT_OF_USAGE = re.compile(
    r"\b(?:usage|session|weekly|spend|5-hour|five-hour) limit\b"
    r"|(?<!rate )(?<!rate-)(?<!rate_)\blimit reached\b"
    r"|\bhit your \w+(?: \w+)? limit\b",
    re.I,
)
# How long Claude is left alone after saying it is out of usage without saying
# until when.
OUT_OF_USAGE_SECONDS = 15 * 60
# How long a session window lasts: a login out for longer is out for its week.
SESSION_SECONDS = 5 * 60 * 60
# When Claude says its limit lifts: "resets 11:40am (America/New_York)",
# "resets Sep 22, 2am", "will reset at 3pm".
LIFTS = re.compile(
    r"\breset(?:s)?(?: at)?\s+(?:(?P<month>[a-z]{3})[a-z]*\.? (?P<day>\d{1,2}),? (?:at )?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<half>[ap])\.?m\b\.?"
    r"(?:\s*\((?P<zone>[a-z_]+(?:/[a-z0-9_+-]+)+)\))?",
    re.I,
)
# Claude's older message puts the time straight after the words, as a Unix timestamp.
LIFTS_AT = re.compile(r"limit reached\|(\d{10})\b", re.I)
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


class UsageError(Exception):
    """The usage numbers could not be read. Never leaves this module.

    Its message goes straight onto the one-line strip, so it is short and
    plain rather than a URL and a traceback. `retry_after` is how many seconds
    the endpoint asked to be left alone for, when it said."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def access_token(service: str = KEYCHAIN_SERVICE) -> str:
    found = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-w"],
        capture_output=True,
        text=True,
    )
    if found.returncode != 0:
        raise UsageError("Claude Code isn't signed in")
    try:
        return json.loads(found.stdout)["claudeAiOauth"]["accessToken"]
    except (ValueError, KeyError, TypeError) as exc:
        raise UsageError("the Keychain login isn't readable") from exc


def fetch_usage(token: str, url: str = USAGE_URL) -> dict:
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        raise UsageError(f"usage endpoint said {exc.code}", retry_after=seconds(exc.headers)) from exc
    except (OSError, ValueError) as exc:
        raise UsageError("can't reach the usage endpoint") from exc
    if not isinstance(body, dict):
        raise UsageError("the usage endpoint answered oddly")
    # The windows are either the answer itself or wrapped in "utilization".
    inner = body.get("utilization")
    return inner if isinstance(inner, dict) else body


def seconds(headers) -> float | None:
    """How long a refusal's Retry-After asks to be left alone, when it gives a
    number of seconds."""
    try:
        return float(headers.get("Retry-After"))
    except (AttributeError, TypeError, ValueError):
        return None


def read_claude() -> dict:
    """The live windows, straight from the Keychain and the endpoint."""
    return fetch_usage(access_token())


def keychain_service(config_dir: str | None) -> str:
    """Where Claude Code keeps a login in the Keychain. Run against a
    CLAUDE_CONFIG_DIR of its own, it keeps that login apart from the default
    one, under a name ending in the first eight hex digits of the dir's sha256."""
    if not config_dir:
        return KEYCHAIN_SERVICE
    return f"{KEYCHAIN_SERVICE}-{hashlib.sha256(config_dir.encode()).hexdigest()[:8]}"


def read_account(account: Account) -> dict:
    """An alias account's live windows, off its own Keychain login."""
    return fetch_usage(access_token(keychain_service(account.config_dir)))


def windows(payload: dict) -> list[dict]:
    """The windows the page shows, skipping any the answer leaves out."""
    found = []
    for key, label, span in WINDOWS:
        window = payload.get(key)
        used = window.get("utilization") if isinstance(window, dict) else None
        if used is None:
            continue
        # How much is gone, never how much is left: a usage reading is read
        # against its limit, and an answer that has to be inverted first isn't one.
        found.append({
            "key": key,
            "label": label,
            "span": span,
            "used": float(used),
            "resets_at": resets_at(window.get("resets_at")),
        })
    return found


def heard_windows(found: dict) -> list[dict]:
    """The windows a CLI's rate_limit_event carries (see claude_step.py), the
    way the page shows them. There the share used is a fraction and the reset
    is in Unix seconds."""
    heard = []
    for key, label, span in WINDOWS:
        window = found.get(key)
        used = window.get("utilization") if isinstance(window, dict) else None
        if not isinstance(used, (int, float)):
            continue
        at = window.get("resetsAt")
        heard.append({"key": key, "label": label, "span": span, "used": float(used) * 100,
                      "resets_at": float(at) if isinstance(at, (int, float)) else None})
    return heard


def resets_at(value) -> float | None:
    """When the window resets, as a Unix timestamp the page can count down to."""
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def turned_over(window: dict, now: float | None = None) -> bool:
    """Whether the clock says a window has reset since it was read.

    A reading that can't be refreshed keeps standing (see Usage.keep), and a
    spent window that kept standing would hold a run on Codex for hours after
    its limit had lifted. The reset time the answer came with settles that
    without having to reach the endpoint again.
    """
    at = window.get("resets_at")
    return at is not None and (time.time() if now is None else now) >= at


def limit_lifts_at(text: str, now: float | None = None) -> float | None:
    """When a CLI that said it is out of usage says it is back, as a Unix
    timestamp, or None when the message doesn't say.

    A bare time is the next one of it, in the zone the message names or else
    this machine's, except a time that has only just gone by: that is the limit
    lifting as the message was read, not the same time tomorrow.
    """
    now = time.time() if now is None else now
    if stamp := LIFTS_AT.search(text):
        return float(stamp.group(1))
    found = LIFTS.search(text)
    if not found:
        return None
    try:
        zone = ZoneInfo(found["zone"]) if found["zone"] else None
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    # A named zone's wall clock, or this machine's, whose own rules .timestamp() applies.
    here = datetime.fromtimestamp(now, zone)
    hour = int(found["hour"]) % 12 + (12 if found["half"].lower() == "p" else 0)
    try:
        at = here.replace(hour=hour, minute=int(found["minute"] or 0), second=0, microsecond=0)
        if found["month"]:
            at = at.replace(month=MONTHS.index(found["month"].lower()) + 1, day=int(found["day"]))
            if at.timestamp() < now - 24 * 60 * 60:
                at = at.replace(year=at.year + 1)
        elif at.timestamp() < now - 60 * 60:
            at += timedelta(days=1)
    except ValueError:
        return None
    return at.timestamp()


def read_codex() -> list[dict]:
    """Codex's windows, from the rollouts it writes its turns to."""
    try:
        return codex_usage.read_limits()
    except codex_usage.CodexUsageError as exc:
        raise UsageError(str(exc)) from exc


def read_gemini() -> list[dict]:
    """Gemini's windows, out of the Antigravity CLI's own /usage."""
    try:
        return gemini_usage.read_limits()
    except gemini_usage.GeminiUsageError as exc:
        raise UsageError(str(exc)) from exc


def off_report() -> dict:
    """What /api/usage answers where nothing reads usage, as in tests."""
    off = {"ok": False, "error": OFF_REASON, "checked_at": None, "tried_at": None, "windows": []}
    return {"engine": CLAUDE, "fallback_at": None, "claude_back_at": None, "claude": off, "accounts": [],
            "codex": {**off, "home": tilde(CODEX_HOME), "back_at": None},
            "gemini": {**off, "back_at": None}}


def tilde(path: Path) -> str:
    home = Path.home()
    return f"~/{path.relative_to(home)}" if home in path.parents else str(path)


class SharedTable:
    """One of Usage's tables, kept in the `shared` file. It is read afresh on
    every look, so what another process read or heard counts here at once, and
    written back under a lock, so two processes writing at once don't drop each
    other's change. A missing or garbled file reads as empty and a write that
    fails is let go: the worst either costs is another reading."""

    def __init__(self, path: Path, name: str):
        self.path = path
        self.name = name

    def tables(self) -> dict:
        try:
            found = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}

    def load(self) -> dict:
        found = self.tables().get(self.name)
        return found if isinstance(found, dict) else {}

    def get(self, key: str, default=None):
        return self.load().get(key, default)

    def __getitem__(self, key: str):
        return self.load()[key]

    def __setitem__(self, key: str, value) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path.with_suffix(".lock"), "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                tables = self.tables()
                tables[self.name] = {**self.load(), key: value}
                # Written whole beside it and swapped in, so a reader never
                # finds half a file.
                spare = self.path.with_suffix(".tmp")
                spare.write_text(json.dumps(tables), encoding="utf-8")
                spare.replace(self.path)
        except OSError:
            pass


class Usage:
    """Every engine's usage windows, each read at most once every `cache_seconds`.

    One of these is shared by every run in a process: the loop asks it which
    CLI to open a pass on, and the page asks it what to show. `accounts` gives
    the alias accounts as they stand when asked. Given `shared`, it shares what
    it reads and hears with every other process given the same file.
    """

    def __init__(self, fallback_at: float = FALLBACK_AT, cache_seconds: float = CACHE_SECONDS,
                 read=read_claude, read_codex=read_codex, retry_after: float = RETRY_AFTER_SECONDS,
                 accounts: Callable[[], list[Account]] = list, read_account=read_account,
                 read_gemini=read_gemini, shared: Path | None = None):
        self.fallback_at = fallback_at
        self.cache_seconds = cache_seconds
        self.retry_after = retry_after
        # Each engine's windows, however it has to be asked for them.
        self.reads = {CLAUDE: lambda: windows(read()), CODEX: read_codex, GEMINI: read_gemini}
        self.accounts = accounts
        self.read_account = read_account
        self.lock = threading.Lock()
        # Each engine's last reading.
        self.cached = SharedTable(shared, "cached") if shared else {}
        # Until when each Claude login said it is out of usage.
        self.out_until = SharedTable(shared, "out_until") if shared else {}
        # Until when the endpoint asked to be left alone about each engine.
        self.quiet_until = SharedTable(shared, "quiet_until") if shared else {}

    def windows_for(self, engine: str) -> dict:
        """One engine's windows, from the cache while the last reading stands
        or while the endpoint has asked not to be asked."""
        with self.lock:
            found = self.cached.get(engine)
            if found and time.time() < self.asks_again_at(engine, found):
                return found
            fresh = self.keep(found, self.read_windows(engine))
            self.cached[engine] = fresh
            return fresh

    def asks_again_at(self, engine: str, found: dict) -> float:
        """When an engine is next asked for its windows: once its last reading
        is `hold` old, never while a refusal's Retry-After holds it off, and,
        where the reading failed with one, the moment it said to come back
        rather than whenever `retry_after` would have."""
        quiet = self.quiet_until.get(engine, 0)
        if found["error"] is not None and quiet >= found["tried_at"]:
            return quiet
        return max(found["tried_at"] + self.hold(found), quiet)

    def follow_up_at(self) -> float | None:
        """The soonest hour a refusal still holding an engine off said to come
        back, so whatever polls can read again right then instead of at its
        next poll. None while no refusal is holding one off."""
        now = time.time()
        engines = [CLAUDE, *(account.name for account in self.accounts()), CODEX, GEMINI]
        return min((at for engine in engines if (at := self.quiet_until.get(engine, 0)) > now), default=None)

    def heard(self, engine: str, found: dict) -> None:
        """A step's CLI reported the windows of the login it runs as (see
        claude_step.py). They come off the answers it is getting, so they are a
        reading as good as the endpoint's, and they stand in for one."""
        heard = heard_windows(found)
        if not heard:
            return
        now = time.time()
        with self.lock:
            self.cached[engine] = {"ok": True, "error": None, "checked_at": now, "tried_at": now, "windows": heard}

    def hold(self, found: dict) -> float:
        """How long a reading stands before the engine is asked again. One that
        failed stands longer, so a rate limit isn't answered by asking again."""
        return self.cache_seconds if found["error"] is None else self.retry_after

    def keep(self, found: dict | None, fresh: dict) -> dict:
        """A failed reading keeps the windows the last good one found rather
        than emptying the strip: numbers a few minutes old say far more than an
        error does. The reason still comes along, so the page can say they are
        no longer being refreshed."""
        if fresh["ok"] or not (found and found["windows"]):
            return fresh
        # A window that has reset since it was read says nothing about now, so
        # it is dropped rather than stood on: that is what puts a run back on
        # Claude at the reset even while the endpoint is unreachable.
        standing = [window for window in found["windows"] if not turned_over(window)]
        if not standing:
            return fresh
        return {**found, "windows": standing, "error": fresh["error"], "tried_at": fresh["tried_at"]}

    def read_windows(self, engine: str) -> dict:
        """Ask that engine for its windows; a reading that fails is an answer too."""
        now = time.time()
        try:
            found = self.reads[engine]() if engine in self.reads else self.account_windows(engine)
        except UsageError as exc:
            if exc.retry_after:
                self.quiet_until[engine] = now + exc.retry_after
            return {"ok": False, "error": str(exc), "checked_at": None, "tried_at": now, "windows": []}
        return {"ok": True, "error": None, "checked_at": now, "tried_at": now, "windows": found}

    def account_windows(self, engine: str) -> list[dict]:
        account = self.account(engine)
        if account is None:
            raise UsageError(f"there's no {engine} alias anymore")
        return windows(self.read_account(account))

    def account(self, engine: str | None) -> Account | None:
        """The alias account an engine names; None for Claude's own login and Codex."""
        return next((account for account in self.accounts() if account.name == engine), None)

    def engines(self) -> list[str]:
        """Every CLI passes can open on, in the order they are tried: the Claude
        logins — Claude's own, then each alias account — up to the first that
        isn't exhausted, then Codex and Gemini, then the logins after it. That
        login's session running out is over within hours, so Codex and Gemini
        stand in until it is back rather than the next account; a login that is
        exhausted is out for days, so passes rotate on to the next one first."""
        logins = [CLAUDE, *(account.name for account in self.accounts())]
        current = next((n for n, login in enumerate(logins) if not self.exhausted(login)), len(logins) - 1)
        return [*logins[:current + 1], CODEX, GEMINI, *logins[current + 1:]]

    def exhausted(self, login: str) -> bool:
        """Whether a Claude login is out for longer than a session window could
        hold it: its weekly window is spent, it said it is out until later than
        a session window lasts, or nothing can be read of it at all.

        A login nothing is known of is out for as long as that lasts, which is
        no shorter than a week's window: passes rotate on to the next login
        rather than dropping to Codex and Gemini over it.
        """
        return (self.out_until.get(login, 0) - time.time() > SESSION_SECONDS
                or self.unreadable(login)
                or any(window["key"] == "seven_day" for window in self.spent_windows(login)))

    def claude(self) -> dict:
        return self.windows_for(CLAUDE)

    def codex(self) -> dict:
        return {**self.windows_for(CODEX), "home": tilde(CODEX_HOME)}

    def gemini(self) -> dict:
        return self.windows_for(GEMINI)

    def spent_windows(self, engine: str) -> list[dict]:
        """An engine's session and weekly windows spent past the fallback, less
        any the clock says has reset since it was read. Codex's reading is its
        last turn's, and a Codex that reads spent is given no turn to read again,
        so a reset that reading can't show is the only way it is ever back."""
        found = self.windows_for(engine)
        return [window for window in found["windows"]
                if window["used"] >= self.fallback_at and not turned_over(window)] if found["ok"] else []

    def claude_out(self, until: float | None, engine: str = CLAUDE) -> None:
        """A Claude login ended a step saying it is out of usage: passes don't
        open on it until `until`, the hour it said it is back, whatever its
        windows read. A message that didn't say, or said a time already gone,
        holds it off for OUT_OF_USAGE_SECONDS."""
        now = time.time()
        self.out_until[engine] = until if until is not None and until > now else now + OUT_OF_USAGE_SECONDS

    def is_claude_out(self, engine: str = CLAUDE) -> bool:
        return time.time() < self.out_until.get(engine, 0)

    def back_at(self, engine: str = CLAUDE) -> float | None:
        """When an engine takes passes again, while it can't: once the hour a
        Claude login said it is back has come and every window it has spent
        past the fallback has reset. None when one of those doesn't say when."""
        times = [window["resets_at"] for window in self.spent_windows(engine)]
        if self.is_claude_out(engine):
            times.append(self.out_until[engine])
        return max(times) if times and None not in times else None

    def unreadable(self, engine: str) -> bool:
        """Whether nothing has ever been read of an engine's usage: no reading
        of the endpoint came back, and no step reported its windows either.

        A reading that failed after a good one still has that one's numbers
        standing behind it (`keep`), and those are what the engine is judged
        on. With none at all there is nothing to judge it by, and how much of a
        login is gone is not a thing to guess at: the endpoint refusing one
        with 429 is how a login that is being rate limited reads, and opening
        passes on it anyway spends a step to be told what the refusal said.
        """
        return self.windows_for(engine)["checked_at"] is None

    def takes_passes(self, engine: str) -> bool:
        """Whether an engine can open a pass: something is known of its usage,
        it hasn't said it is out, and neither its session nor its weekly window
        is spent past the fallback."""
        return (not self.unreadable(engine) and not self.is_claude_out(engine)
                and not self.spent_windows(engine))

    def engine(self) -> str | None:
        """Which CLI the next pass opens on: the first in `engines` that takes
        passes, or None while none does. A window the clock says has reset is
        no reason to pass an engine by, so a run that moved on comes back at
        the top of the window that moved it. An engine having said it is out
        of usage moves it all the same.

        With nothing known of any of them — every reading refused or the
        endpoint down since this process started — the first whose usage can't
        be read is tried rather than waiting on a number nobody can get. Not
        knowing only passes an engine by while there is one that is known good.
        """
        chain = self.engines()
        known = next((engine for engine in chain if self.takes_passes(engine)), None)
        if known is not None:
            return known
        return next((engine for engine in chain
                     if self.unreadable(engine) and not self.is_claude_out(engine)), None)

    def report(self) -> dict:
        """What /api/usage answers."""
        return {"engine": self.engine(), "fallback_at": self.fallback_at, "claude_back_at": self.back_at(),
                "claude": self.claude(), "accounts": [self.account_report(account) for account in self.accounts()],
                "codex": {**self.codex(), "back_at": self.back_at(CODEX)},
                "gemini": {**self.gemini(), "back_at": self.back_at(GEMINI)}}

    def account_report(self, account: Account) -> dict:
        """An alias account's windows for the page, with the config dir it signs
        in with and, while it can't take passes, when it can again."""
        home = tilde(Path(account.config_dir)) if account.config_dir else None
        return {**self.windows_for(account.name), "name": account.name, "home": home,
                "back_at": self.back_at(account.name)}
