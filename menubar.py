#!/usr/bin/env python3
"""LLM usage in the menu bar.

The icon is a pie of the session window of the CLI the next pass opens on: the
share of it used, filled clockwise from twelve o'clock. Clicking it opens a menu
of every CLI, in the order passes fall back through them, the one passes open on
ticked, each with a pie of its own and its session and weekly windows as the
share used and when they reset. With every CLI spent the pie is full, since the
next pass waits. With nothing to show, a reading that failed, it is a faint ring.

"Show in the menu bar", at the foot of the menu, changes which CLI the icon is
the pie of: the one the next pass opens on, as it is by default, or any one CLI
whatever passes do. The choice is kept in settings.json beside this file.

The numbers are read here, by usage.py: Claude's login and each alias account's
off the Keychain and the endpoint /usage asks, Codex's off the rollout files it
writes and Gemini's off agy's /usage. They are kept in usage.SHARED, which
clear_backlog.py reads usage through too, so the menu and its page match and
cost one reading between them. Nothing here needs clear_backlog.py running.

    python3 menubar.py

It runs as the launch agent com.john.llm-usage-tracker, apart from
clear_backlog.py, so either restarts without the other.
"""

import json
import sys
import threading
import time
import traceback
from functools import partial
from pathlib import Path
from typing import Callable

from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory, NSBezierPath, NSColor,
                    NSControlStateValueOn, NSImage, NSInsetRect, NSMakePoint, NSMenu, NSMenuItem, NSMidX,
                    NSMidY, NSStatusBar, NSVariableStatusItemLength, NSWidth)
from Foundation import NSObject
from PyObjCTools import AppHelper

from accounts import account_label, claude_aliases
from usage import CLAUDE, CODEX, GEMINI, SHARED, Usage

HERE = Path(__file__).resolve().parent
# Which CLI the icon is the pie of, as "Show in the menu bar" last set it.
SETTINGS = HERE / "settings.json"
# The icon's default: whichever CLI the next pass opens on.
AUTO = "auto"
# Usage moves slowly, and usage.py reads each CLI once a minute at most anyway.
# A refusal that said when to come back is followed up right then instead.
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


def shown_in(report: dict, shown: str) -> str:
    """Which the icon is the pie of: `shown`, or AUTO where that CLI isn't in
    the answer any more (an alias taken out of ~/.zshrc)."""
    return shown if shown in dict(engines(report)) else AUTO


def icon_share(report: dict | None, shown: str = AUTO) -> float | None:
    """How much of the menu bar's pie is filled: the session window of the CLI
    `shown`, or by default of the CLI the next pass opens on, which is full
    while every CLI is spent, since the next pass waits. None while usage
    can't be read or that CLI's reading failed."""
    if report is None:
        return None
    usages = dict(engines(report))
    if shown_in(report, shown) != AUTO:
        return share(usages[shown])
    if report["engine"] is None:
        return 1.0
    return share(usages.get(report["engine"]))


def load_shown(path: Path = SETTINGS) -> str:
    """The CLI the icon was last set to show, or AUTO where it never was."""
    try:
        shown = json.loads(path.read_text(encoding="utf-8")).get("shown")
    except (OSError, ValueError, AttributeError):
        return AUTO
    return shown if isinstance(shown, str) else AUTO


def save_shown(shown: str, path: Path = SETTINGS) -> None:
    path.write_text(json.dumps({"shown": shown}), encoding="utf-8")


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


def choices(report: dict, shown: str, target) -> NSMenuItem:
    """The "Show in the menu bar" row: a submenu of what the icon can be the
    pie of, the CLI the next pass opens on and then each CLI, ticked on the
    one it is. Choosing one sends it to `target`'s choose:."""
    submenu = NSMenu.alloc().init()
    submenu.setAutoenablesItems_(False)
    shown = shown_in(report, shown)
    for key, title in [(AUTO, "The CLI the next pass opens on"),
                       *((engine, engine_name(engine)) for engine, _ in engines(report))]:
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, "choose:", "")
        item.setTarget_(target)
        item.setRepresentedObject_(key)
        if key == shown:
            item.setState_(NSControlStateValueOn)
        submenu.addItem_(item)
    item = row("Show in the menu bar")
    item.setSubmenu_(submenu)
    return item


def fill(menu: NSMenu, report: dict | None, shown: str = AUTO, target=None) -> NSMenu:
    """Makes the menu say what `report` does: a row per CLI with its pie,
    ticked for the one the next pass opens on, and under it a row per window,
    then why, where its reading failed or has stopped refreshing. Last comes
    the choice of what the icon shows, `shown` ticked."""
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
    menu.addItem_(NSMenuItem.separatorItem())
    menu.addItem_(choices(report, shown, target))
    return menu


class Picker(NSObject):
    """What the "Show in the menu bar" items call when one is chosen. AppKit
    sends a selector to an Objective-C object rather than calling a Python
    function, so this hands the choice on to `chosen`."""

    def choose_(self, item):
        self.chosen(str(item.representedObject()))


class UsageBar:
    """The pie in the menu bar and the menu it opens. AppKit's, so only ever
    touched on the main thread."""

    def __init__(self, settings: Path = SETTINGS) -> None:
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.menu = NSMenu.alloc().init()
        # Every row is there to be read, so none is greyed for having no action.
        self.menu.setAutoenablesItems_(False)
        self.item.setMenu_(self.menu)
        self.item.button().setImage_(pie(None, ICON_SIZE))
        self.settings = settings
        self.shown = load_shown(settings)
        self.report: dict | None = None
        self.picker = Picker.alloc().init()
        self.picker.chosen = self.choose

    def show(self, report: dict | None) -> None:
        self.report = report
        self.item.button().setImage_(pie(icon_share(report, self.shown), ICON_SIZE))
        fill(self.menu, report, self.shown, self.picker)

    def choose(self, shown: str) -> None:
        """"Show in the menu bar" was set to `shown`: kept for the next start
        and drawn now, not at the next reading."""
        self.shown = shown
        save_shown(shown, self.settings)
        self.show(self.report)


def pause(follow_up_at: float | None) -> float:
    """How long to wait for the next reading: POLL_SECONDS, or only until the
    hour a refusal said to come back where that is sooner."""
    if follow_up_at is None:
        return POLL_SECONDS
    return min(POLL_SECONDS, max(follow_up_at - time.time(), 0))


def watch(read: Callable[[], dict], show: Callable[[dict | None], None],
          follow_up: Callable[[], float | None] = lambda: None) -> None:
    """Reads usage every POLL_SECONDS for as long as the app runs, or sooner
    where `follow_up` names the hour a refusal said to come back, and hands
    each report to `show`, one reading at a time so slow ones never pile up. A
    reading that blows up is shown as None, its traceback left in the log,
    rather than ending the watching."""
    while True:
        try:
            report = read()
        except Exception:
            traceback.print_exc()
            report = None
        show(report)
        time.sleep(pause(follow_up()))


def main() -> int:
    # No Dock icon and no app menu: only the status item.
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    bar = UsageBar()
    usage = Usage(accounts=claude_aliases, shared=SHARED)
    # The reading happens off the main thread, so a slow one never holds the
    # menu bar up, and each report is drawn back on it.
    threading.Thread(target=watch, args=(usage.report, partial(AppHelper.callAfter, bar.show), usage.follow_up_at),
                     daemon=True).start()
    # Ctrl+C quits one started by hand. A crash ends the process for launchd to
    # start again, rather than asking in a panel whether to carry on.
    AppHelper.runEventLoop(installInterrupt=True, unexpectedErrorAlert=lambda: False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
