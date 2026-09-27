import time
from types import SimpleNamespace

import pytest
from AppKit import NSBitmapImageRep, NSControlStateValueOn, NSDeviceRGBColorSpace, NSGraphicsContext, NSMenu

import menubar
from accounts import Account
from menubar import POLL_SECONDS, clock, fill, icon_share, pie, watch
from usage import Usage

HOUR = 60 * 60
SIZE = 18
# Inside the pie, a pixel into each quarter of it, (0, 0) being the top left.
UPPER_RIGHT, LOWER_RIGHT, LOWER_LEFT, UPPER_LEFT = (11, 6), (11, 11), (6, 11), (6, 6)
# On the ring, at twelve o'clock.
RING = (9, 1)


def usage(*windows: tuple, ok: bool = True, error: str | None = None, checked_at: float | None = None,
          **more) -> dict:
    """One CLI's part of the answer, each window given as (key, label, used, resets_at)."""
    return {"ok": ok, "error": error, "checked_at": checked_at, "tried_at": checked_at,
            "windows": [{"key": key, "label": label, "span": "", "used": used, "resets_at": resets_at}
                        for key, label, used, resets_at in windows], **more}


def answer(engine: str | None = "claude2") -> dict:
    """A usage report while passes open on the claude2 alias, Claude,
    Codex and Gemini having all run out: Claude's weekly window spent and its
    reading gone stale, Codex with only a weekly window, and Gemini's reading
    failed."""
    now = time.time()
    return {
        "engine": engine, "fallback_at": 95.0, "claude_back_at": now + 30 * HOUR,
        "claude": usage(("five_hour", "Session", 0.0, None), ("seven_day", "Weekly", 100.0, now + 30 * HOUR),
                        error="usage endpoint said 429", checked_at=now - 2 * HOUR),
        "accounts": [usage(("five_hour", "Session", 8.4, now + 2 * HOUR), ("seven_day", "Weekly", 34.0, now + 50 * HOUR),
                           checked_at=now, name="claude2", home="~/.claude-2", back_at=None)],
        "codex": usage(("primary", "Weekly", 60.0, now + 70 * HOUR), checked_at=now, home="~/.codex-5", back_at=None),
        "gemini": usage(ok=False, error="agy isn't signed in", back_at=None),
    }


def rows(menu: NSMenu) -> list[tuple[str, bool, bool]]:
    """A menu a row at a time: its title, indented a level for a window, whether
    it is ticked and whether it is greyed. A separator is an empty greyed row."""
    return [("  " * item.indentationLevel() + item.title(), item.state() == NSControlStateValueOn, not item.isEnabled())
            for item in menu.itemArray()]


def drawn(image) -> NSBitmapImageRep:
    """The pie drawn at a pixel a point."""
    bitmap = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, SIZE, SIZE, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(bitmap))
    image.drawInRect_(((0, 0), (SIZE, SIZE)))
    NSGraphicsContext.restoreGraphicsState()
    return bitmap


def filled(bitmap: NSBitmapImageRep, *pixels: tuple[int, int]) -> list[bool]:
    return [bitmap.colorAtX_y_(x, y).alphaComponent() > 0.5 for x, y in pixels]


class Enough(Exception):
    pass


@pytest.fixture
def waits(monkeypatch) -> list[float]:
    """Every wait watch() makes between readings, the second ending it."""
    waited = []

    def sleep(seconds):
        waited.append(seconds)
        if len(waited) == 2:
            raise Enough

    monkeypatch.setattr(menubar, "time", SimpleNamespace(sleep=sleep))
    return waited


def test_the_pie_is_the_session_window_of_the_cli_the_next_pass_opens_on():
    assert icon_share(answer()) == pytest.approx(0.084)


def test_a_cli_with_only_a_weekly_window_fills_the_pie_with_that_one():
    assert icon_share(answer(engine="codex")) == pytest.approx(0.6)


def test_the_pie_is_full_while_every_cli_is_spent_since_the_next_pass_waits():
    assert icon_share(answer(engine=None)) == 1


def test_there_is_nothing_to_fill_the_pie_with_while_usage_cant_be_read_or_a_reading_failed():
    assert icon_share(None) is None
    assert icon_share(answer(engine="gemini")) is None


def test_the_menu_lists_every_cli_with_its_windows_as_the_share_used_ticking_the_one_passes_open_on():
    report = answer()
    claude, claude2, codex = report["claude"], report["accounts"][0], report["codex"]

    assert rows(fill(NSMenu.alloc().init(), report)) == [
        ("Claude", False, False),
        ("  Session 0% used", False, False),
        (f"  Weekly 100% used · resets {clock(claude['windows'][1]['resets_at'])}", False, False),
        (f"  Last read {clock(claude['checked_at'])} and not refreshing: usage endpoint said 429", False, True),
        ("", False, True),
        ("Claude 2", True, False),
        (f"  Session 8% used · resets {clock(claude2['windows'][0]['resets_at'])}", False, False),
        (f"  Weekly 34% used · resets {clock(claude2['windows'][1]['resets_at'])}", False, False),
        ("", False, True),
        ("Codex", False, False),
        (f"  Weekly 60% used · resets {clock(codex['windows'][0]['resets_at'])}", False, False),
        ("", False, True),
        ("Gemini", False, False),
        ("  agy isn't signed in", False, True),
    ]


def test_every_cli_in_the_menu_has_a_pie_of_its_own():
    menu = fill(NSMenu.alloc().init(), answer())
    names = [item for item in menu.itemArray() if item.indentationLevel() == 0 and not item.isSeparatorItem()]

    assert [item.title() for item in names] == ["Claude", "Claude 2", "Codex", "Gemini"]
    assert all(item.image() is not None and item.image().isTemplate() for item in names)


def test_the_menu_says_the_next_pass_waits_and_ticks_nothing_while_every_cli_is_spent():
    menu = rows(fill(NSMenu.alloc().init(), answer(engine=None)))

    assert menu[:3] == [("Every CLI's usage is spent: the next pass waits", False, True), ("", False, True),
                        ("Claude", False, False)]
    assert not any(ticked for _, ticked, _ in menu)


def test_each_answer_replaces_the_menu_and_one_that_never_came_says_why():
    menu = fill(NSMenu.alloc().init(), answer())

    assert rows(fill(menu, None)) == [("Couldn't read the usage windows: menubar.log says why", False, True)]


def test_the_pie_fills_clockwise_from_twelve_oclock():
    quarter = drawn(pie(0.25, SIZE))
    half = drawn(pie(0.5, SIZE))

    assert filled(quarter, UPPER_RIGHT, LOWER_RIGHT, LOWER_LEFT, UPPER_LEFT) == [True, False, False, False]
    assert filled(half, UPPER_RIGHT, LOWER_RIGHT, LOWER_LEFT, UPPER_LEFT) == [True, True, False, False]


def test_a_full_pie_is_filled_all_round_and_an_empty_one_is_its_ring_alone():
    assert filled(drawn(pie(1.0, SIZE)), UPPER_RIGHT, LOWER_RIGHT, LOWER_LEFT, UPPER_LEFT) == [True] * 4
    assert filled(drawn(pie(0, SIZE)), RING, UPPER_RIGHT, LOWER_RIGHT, LOWER_LEFT, UPPER_LEFT) == [True] + [False] * 4


def test_the_pie_with_nothing_to_show_is_a_faint_ring_and_every_pie_takes_the_menu_bars_color():
    faint = drawn(pie(None, SIZE))

    assert 0 < faint.colorAtX_y_(*RING).alphaComponent() < 0.5
    assert filled(faint, UPPER_RIGHT, LOWER_RIGHT, LOWER_LEFT, UPPER_LEFT) == [False] * 4
    assert pie(None, SIZE).isTemplate() and pie(0.5, SIZE).isTemplate()


def test_the_menu_shows_what_it_reads_itself_with_nothing_else_running():
    usage = Usage(read=lambda: {"five_hour": {"utilization": 12.0, "resets_at": None},
                                "seven_day": {"utilization": 30.0, "resets_at": None}},
                  read_codex=list, read_gemini=list,
                  accounts=lambda: [Account("claude2", ("claude",), {"CLAUDE_CONFIG_DIR": "/tmp/claude-2"})],
                  read_account=lambda account: {"five_hour": {"utilization": 50.0, "resets_at": None}})
    report = usage.report()

    assert icon_share(report) == pytest.approx(0.12)
    assert rows(fill(NSMenu.alloc().init(), report)) == [
        ("Claude", True, False),
        ("  Session 12% used", False, False),
        ("  Weekly 30% used", False, False),
        ("", False, True),
        ("Claude 2", False, False),
        ("  Session 50% used", False, False),
        ("", False, True),
        ("Codex", False, False),
        ("", False, True),
        ("Gemini", False, False),
    ]


def test_watch_hands_each_reading_to_the_menu_bar_and_waits_between_readings(waits):
    shown = []

    with pytest.raises(Enough):
        watch(lambda: {"engine": "claude"}, shown.append)

    assert shown == [{"engine": "claude"}] * 2
    assert waits == [POLL_SECONDS] * 2


def test_a_reading_that_blows_up_shows_as_nothing_read_and_the_watching_carries_on(waits, capsys):
    readings = iter([RuntimeError("the Keychain fell over"), {"engine": "claude"}])
    shown = []

    def read():
        found = next(readings)
        if isinstance(found, Exception):
            raise found
        return found

    with pytest.raises(Enough):
        watch(read, shown.append)

    assert shown == [None, {"engine": "claude"}]
    assert "the Keychain fell over" in capsys.readouterr().err
