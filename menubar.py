#!/usr/bin/env python3
"""LLM usage in the menu bar.

The icon is a pie of the session window of the CLI the next pass opens on: the
share of it used, filled clockwise from twelve o'clock. Clicking it opens a menu
of every CLI, in the order passes fall back through them, the one passes open on
ticked, each with a pie of its own and its session and weekly windows as the
share used and when they reset. With every CLI spent the pie is full, since the
next pass waits. With nothing to show, a reading that failed, it is a faint ring.

The numbers are read here, by usage.py: Claude's login and each alias account's
off the Keychain and the endpoint /usage asks, Codex's off the rollout files it
writes and Gemini's off agy's /usage. They are kept in usage.SHARED, which
clear_backlog.py reads usage through too, so the menu and its page match and
cost one reading between them. Nothing here needs clear_backlog.py running.

    python3 menubar.py

It runs as the launch agent com.john.llm-usage-tracker, apart from
clear_backlog.py, so either restarts without the other.
"""

import sys
import threading
import time
import traceback
from functools import partial
from typing import Callable

from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory, NSBezierPath, NSColor,
                    NSControlStateValueOn, NSImage, NSInsetRect, NSMakePoint, NSMenu, NSMenuItem, NSMidX,
                    NSMidY, NSStatusBar, NSVariableStatusItemLength, NSWidth)
from PyObjCTools import AppHelper

from accounts import account_label, claude_aliases
from usage import CLAUDE, CODEX, GEMINI, SHARED, Usage

# Usage moves slowly, and usage.py reads each CLI once a minute at most anyway.
POLL_SECONDS = 30
# Points. The menu bar's own icons are about this big, and a menu row's a little smaller.
ICON_SIZE = 18
ROW_ICON_SIZE = 14


def clock(at: float) -> str:
    """"11:40 AM": the wall-clock time of a Unix timestamp, or "Tue 2:00 AM"
    once it is a day or more away, like a weekly window's reset."""
    far = at - time.time() >= 24 * 60 * 60
    return time.strftime("%a %-I:%M %p" if far else "%-I:%M %p", time.localtime(at))


def engine_name(engine: str) -> str:
    """How a CLI is named in the menu, an alias account such as claude2 as "Claude 2"."""
    return {CLAUDE: "Claude", CODEX: "Codex", GEMINI: "Gemini"}.get(engine) or account_label(engine)


def engines(report: dict) -> list[tuple[str, dict]]:
    """Every CLI with its usage, in the order the menu lists them: Claude's own
    login, each alias account, Codex, then Gemini. One the answer doesn't carry
    is left out."""
    found = [(CLAUDE, report.get("claude")), *((account["name"], account) for account in report.get("accounts") or []),
             (CODEX, report.get("codex")), (GEMINI, report.get("gemini"))]
    return [(engine, usage) for engine, usage in found if usage]


def session(usage: dict) -> dict | None:
    """The window a CLI's pie shows: its five-hour session window, or its only
    window where it has none (Codex can read just a weekly one). None while
    nothing has been read."""
    windows = usage["windows"]
    return next((window for window in windows if window["key"] == "five_hour"), windows[0] if windows else None)


def share(usage: dict | None) -> float | None:
    """How much of a CLI's pie is filled, 0 to 1, or None with nothing read to fill it."""
    window = session(usage) if usage else None
    return None if window is None else min(max(window["used"], 0), 100) / 100


def icon_share(report: dict | None) -> float | None:
    """How much of the menu bar's pie is filled: the session window of the CLI
    the next pass opens on. Full while every CLI is spent, since the next pass
    waits, and None while usage can't be read."""
    if report is None:
        return None
    if report["engine"] is None:
        return 1.0
    return share(dict(engines(report)).get(report["engine"]))


def window_line(window: dict) -> str:
    """"Session 8% used · resets 4:00 PM": a window as the share used, never the share left."""
    resets = f" · resets {clock(window['resets_at'])}" if window["resets_at"] is not None else ""
    return f"{window['label']} {window['used']:.0f}% used{resets}"


def why(usage: dict) -> str:
    """Why a CLI's windows are missing or old: the reason its reading failed and,
    while the last good windows stand in, when those were read."""
    if not usage["ok"]:
        return usage["error"]
    return f"Last read {clock(usage['checked_at'])} and not refreshing: {usage['error']}"


def pie(filled: float | None, size: float) -> NSImage:
    """A ring around a pie `filled` of the way round, 0 to 1, clockwise from
    twelve o'clock; the ring alone, faint, for None. A template image, so the
    menu bar tints it for light and dark like its own icons."""

    def draw(rect) -> bool:
        ring = NSBezierPath.bezierPathWithOvalInRect_(NSInsetRect(rect, 1, 1))
        ring.setLineWidth_(1.5)
        NSColor.colorWithWhite_alpha_(0, 0.35 if filled is None else 1).setStroke()
        ring.stroke()
        if filled:
            disc = NSInsetRect(rect, 3.5, 3.5)
            if filled >= 1:
                # An arc from twelve round to twelve again is no arc at all.
                wedge = NSBezierPath.bezierPathWithOvalInRect_(disc)
            else:
                center = NSMakePoint(NSMidX(disc), NSMidY(disc))
                wedge = NSBezierPath.bezierPath()
                wedge.moveToPoint_(center)
                wedge.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                    center, NSWidth(disc) / 2, 90, 90 - 360 * filled, True)
                wedge.closePath()
            NSColor.blackColor().setFill()
            wedge.fill()
        return True

    image = NSImage.imageWithSize_flipped_drawingHandler_((size, size), False, draw)
    image.setTemplate_(True)
    return image


def row(title: str, level: int = 0, enabled: bool = True) -> NSMenuItem:
    """A menu row to read, not choose. One that isn't enabled is greyed: a
    reason, where numbers should have been."""
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
    item.setIndentationLevel_(level)
    item.setEnabled_(enabled)
    return item


def fill(menu: NSMenu, report: dict | None) -> NSMenu:
    """Makes the menu say what `report` does: a row per CLI with its pie,
    ticked for the one the next pass opens on, and under it a row per window,
    then why, where its reading failed or has stopped refreshing."""
    menu.removeAllItems()
    if report is None:
        menu.addItem_(row("Couldn't read the usage windows: menubar.log says why", enabled=False))
        return menu
    if report["engine"] is None:
        menu.addItem_(row("Every CLI's usage is spent: the next pass waits", enabled=False))
        menu.addItem_(NSMenuItem.separatorItem())
    for n, (engine, usage) in enumerate(engines(report)):
        if n:
            menu.addItem_(NSMenuItem.separatorItem())
        name = row(engine_name(engine))
        name.setImage_(pie(share(usage), ROW_ICON_SIZE))
        if engine == report["engine"]:
            name.setState_(NSControlStateValueOn)
        menu.addItem_(name)
        for window in usage["windows"]:
            menu.addItem_(row(window_line(window), level=1))
        if usage["error"]:
            menu.addItem_(row(why(usage), level=1, enabled=False))
    return menu


class UsageBar:
    """The pie in the menu bar and the menu it opens. AppKit's, so only ever
    touched on the main thread."""

    def __init__(self) -> None:
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.menu = NSMenu.alloc().init()
        # Every row is there to be read, so none is greyed for having no action.
        self.menu.setAutoenablesItems_(False)
        self.item.setMenu_(self.menu)
        self.item.button().setImage_(pie(None, ICON_SIZE))

    def show(self, report: dict | None) -> None:
        self.item.button().setImage_(pie(icon_share(report), ICON_SIZE))
        fill(self.menu, report)


def watch(read: Callable[[], dict], show: Callable[[dict | None], None]) -> None:
    """Reads usage every POLL_SECONDS for as long as the app runs and hands each
    report to `show`, one reading at a time so slow ones never pile up. A
    reading that blows up is shown as None, its traceback left in the log,
    rather than ending the watching."""
    while True:
        try:
            report = read()
        except Exception:
            traceback.print_exc()
            report = None
        show(report)
        time.sleep(POLL_SECONDS)


def main() -> int:
    # No Dock icon and no app menu: only the status item.
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    bar = UsageBar()
    usage = Usage(accounts=claude_aliases, shared=SHARED)
    # The reading happens off the main thread, so a slow one never holds the
    # menu bar up, and each report is drawn back on it.
    threading.Thread(target=watch, args=(usage.report, partial(AppHelper.callAfter, bar.show)), daemon=True).start()
    # Ctrl+C quits one started by hand. A crash ends the process for launchd to
    # start again, rather than asking in a panel whether to carry on.
    AppHelper.runEventLoop(installInterrupt=True, unexpectedErrorAlert=lambda: False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
