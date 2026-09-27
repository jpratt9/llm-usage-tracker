"""Gemini's usage windows, out of the Antigravity CLI's own /usage.

`agy -p /usage --output-format json` answers without running a model turn and
without this file reading a credential. Its `command.data.groups` are the model
families the account's limits are grouped by, each with a five-hour and a weekly
bucket. Passes run on Gemini, so the Gemini group is the one whose windows count
— the Claude and GPT models agy can also front have a separate cap that says
nothing about the one a pass will spend.

agy reports how much of a window is *left*. The page and the fallback read how
much is gone, so each bucket is turned over on the way out: a number that has to
be inverted before it can be compared to a limit isn't the number to keep.
"""

import json
import subprocess
from datetime import datetime

AGY = "agy"
# The model family passes run on. agy names the group in full ("Gemini Models").
GEMINI_GROUP = "Gemini"
# agy's window names, mapped onto the two the page already shows for Claude and
# Codex, so one strip reads the same way down every row.
WINDOWS = {"5h": ("five_hour", "Session", "5h"), "weekly": ("seven_day", "Weekly", "7d")}
# /usage runs no model, so it answers in about a second; the rest is room for a
# slow backend on a page that polls.
TIMEOUT_SECONDS = 60
REASON_CHARS = 300


class GeminiUsageError(Exception):
    """agy wouldn't say what is left, and this says why."""


def tail(text: str | None, chars: int = REASON_CHARS) -> str:
    """The end of what agy printed, flattened onto one line."""
    return " ".join(str(text or "").split())[-chars:]


def read_usage(agy_bin: str = AGY, run=subprocess.run) -> dict:
    """What `agy -p /usage` answered, as its own JSON envelope."""
    command = [agy_bin, "-p", "/usage", "--output-format", "json",
               "--print-timeout", f"{TIMEOUT_SECONDS}s"]
    try:
        done = run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                   timeout=TIMEOUT_SECONDS + 15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GeminiUsageError(f"agy wouldn't run: {exc}") from exc
    try:
        return json.loads(done.stdout)
    except ValueError as exc:
        raise GeminiUsageError(
            f"agy /usage exited {done.returncode} without its limits: "
            f"{tail(done.stderr or done.stdout) or 'nothing printed'}"
        ) from exc


def buckets(envelope: dict) -> list[dict]:
    """The Gemini group's limit buckets, whatever else agy's account is capped on."""
    try:
        groups = envelope["command"]["data"]["groups"]
    except (KeyError, TypeError) as exc:
        raise GeminiUsageError("agy /usage answered without its limit groups") from exc
    found = []
    for group in groups or []:
        if str((group or {}).get("name") or "").startswith(GEMINI_GROUP):
            found += [bucket for bucket in group.get("buckets") or [] if isinstance(bucket, dict)]
    return found


def used(remaining) -> float | None:
    """How much of a window is gone, from the share agy says is left."""
    try:
        return round((1 - min(max(float(remaining), 0.0), 1.0)) * 100, 1)
    except (TypeError, ValueError):
        return None


def resets_at(value) -> float | None:
    """When a window turns over, as a Unix timestamp the page can count down to."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def limit_windows(found: list[dict]) -> list[dict]:
    """agy's buckets as the windows the page shows, in the page's own order."""
    by_window = {str(bucket.get("window")): bucket for bucket in found}
    windows = []
    for name, (key, label, span) in WINDOWS.items():
        bucket = by_window.get(name)
        share = used((bucket or {}).get("remaining_fraction"))
        if share is None:
            continue
        windows.append({"key": key, "label": label, "span": span, "used": share,
                        "resets_at": resets_at(bucket.get("reset_time"))})
    return windows


def read_limits(agy_bin: str = AGY, run=subprocess.run) -> list[dict]:
    """Gemini's windows, ready for the strip and the fallback."""
    windows = limit_windows(buckets(read_usage(agy_bin, run)))
    if not windows:
        raise GeminiUsageError("agy /usage listed no Gemini limits")
    return windows
