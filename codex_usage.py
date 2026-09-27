"""What Codex's limits are down to, and how full a Codex session's context is.

Codex has no endpoint to ask the way Claude Code does (see usage.py). It writes
every turn to a rollout file under $CODEX_HOME/sessions/<year>/<month>/<day>/,
one JSON object a line, and the token_count events in there carry both the
rate-limit windows and the session's context window — the numbers /status
prints. So the reading is a tail of the newest rollouts rather than a request.

Two of those events come back empty:

A turn that never ran, because it was refused or hit the limit, writes a
token_count with no `info` and no windows. Only an interactive codex fills the
windows in at all; `codex exec`, which is what this loop runs, writes the event
but leaves them null. So an account only ever driven through exec has no limits
to show, and says so rather than showing nothing or a zero.

The context reading has no such gap: a turn that ran records what it was
holding, whoever started it.
"""

import json
import re
import time
from pathlib import Path

# The Codex login read: ~/.codex-5 rather than the ~/.codex an interactive codex
# would use, the account splitloop's Codex steps sign in as (its codex_step.py).
CODEX_HOME = Path.home() / ".codex-5"
# Where codex files a turn: sessions/<year>/<month>/<day>/rollout-<when>-<thread>.jsonl
ROLLOUTS = "sessions/*/*/*/rollout-*.jsonl"
# The thread a rollout is of, which its file name ends with.
THREAD_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)
# How many of the newest rollouts to look back through for a usable reading.
LOOK_BACK = 20
MINUTES_A_DAY = 1440
NO_TURNS = "no turns recorded yet"
NO_LIMITS = "no limits recorded yet"
UNREADABLE = "can't read the rollouts"


class CodexUsageError(Exception):
    """No usable reading. Carries the short line the page shows."""


def rollouts(home: Path = CODEX_HOME) -> list[Path]:
    """The newest rollout files, newest first."""
    try:
        found = sorted(home.glob(ROLLOUTS), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError as exc:
        raise CodexUsageError(UNREADABLE) from exc
    if not found:
        raise CodexUsageError(NO_TURNS)
    return found[:LOOK_BACK]


def rollout_thread(path: Path) -> str:
    """The thread a rollout is of: its file name ends with that id."""
    found = THREAD_ID.search(path.stem)
    return found.group(0) if found else ""


def read_rollout(path: Path) -> tuple[list[dict], str | None]:
    """Every token_count event in a rollout, oldest first, and its model."""
    events, model = [], None
    try:
        with path.open(encoding="utf-8", errors="replace") as lines:
            for line in lines:
                try:
                    payload = json.loads(line).get("payload") or {}
                except ValueError:
                    continue
                if payload.get("type") == "token_count":
                    events.append(payload)
                elif isinstance(payload.get("model"), str):
                    model = payload["model"]
    except OSError as exc:
        raise CodexUsageError(UNREADABLE) from exc
    return events, model


def newest(files: list[Path], usable, missing: str) -> tuple[dict, str | None]:
    """The newest token_count event `usable` accepts, and the model of its turn."""
    for path in files:
        events, model = read_rollout(path)
        for event in reversed(events):
            if usable(event):
                return event, model
    raise CodexUsageError(missing)


def span_label(minutes: int) -> str:
    return f"{minutes // 60}h" if minutes < MINUTES_A_DAY else f"{minutes // MINUTES_A_DAY}d"


def resets_at(window: dict) -> float | None:
    """When the window turns over, as a Unix timestamp. Codex gives one or the other."""
    when = window.get("resets_at")
    if isinstance(when, (int, float)):
        return float(when)
    ahead = window.get("resets_in_seconds")
    return time.time() + ahead if isinstance(ahead, (int, float)) else None


def limit_windows(limits: dict) -> list[dict]:
    """Codex's windows in the shape the page shows, shortest first. Its primary
    window is the session one and its secondary the long one, the same pair
    Claude reports."""
    found = []
    for key in ("primary", "secondary"):
        window = limits.get(key)
        used, minutes = (window or {}).get("used_percent"), (window or {}).get("window_minutes")
        if not isinstance(used, (int, float)) or not isinstance(minutes, int) or minutes <= 0:
            continue
        found.append({
            "key": key,
            "label": "Session" if minutes < MINUTES_A_DAY else "Weekly",
            "span": span_label(minutes),
            "used": float(used),
            "resets_at": resets_at(window),
        })
    return found


def has_limits(event: dict) -> bool:
    return bool(limit_windows(event.get("rate_limits") or {}))


def has_context(event: dict) -> bool:
    return bool((event.get("info") or {}).get("model_context_window"))


def read_limits(home: Path = CODEX_HOME) -> list[dict]:
    """Codex's rate-limit windows, from the newest turn that recorded any."""
    event, _ = newest(rollouts(home), has_limits, NO_LIMITS)
    return limit_windows(event["rate_limits"])


def session_context(thread: str, home: Path = CODEX_HOME) -> dict | None:
    """How full `thread`'s context window is, as its last turn left it, in the
    shape a context activity entry takes. None where the turn recorded nothing."""
    try:
        files = [path for path in rollouts(home) if rollout_thread(path) == thread]
        if not files:
            return None
        event, model = newest(files, has_context, NO_TURNS)
    except CodexUsageError:
        return None
    info = event["info"]
    last = info.get("last_token_usage") or {}
    used = last.get("total_tokens")
    return {"model": model, "window": info["model_context_window"],
            "used": int(used) if isinstance(used, (int, float)) else 0}
