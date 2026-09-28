"""
Tests for the HUD.

The thing actually worth testing here isn't what the output looks like
-- it's that the render path runs at all when enabled. Every drawing
method writes escape sequences to sys.stdout on a background thread, so
a mistake in one of them (a bad format string, a wrong tuple shape)
would otherwise surface only as a garbled live terminal, which no test
run would ever catch. These force the module into its enabled state
against a StringIO and assert the paths execute and emit sane bytes.

The disabled path matters just as much and is easier to get wrong
silently: under pythonw.exe stdout is a log file, and escape sequences
written there are pure noise. Those tests assert nothing ANSI escapes.
"""

import io
import re
import sys
import threading
import time

import pytest

import hud


# Note: these bind hud's own _stream rather than patching sys.stdout.
# pytest re-installs its global capture between fixture setup and the
# test call, so a monkeypatched sys.stdout is silently replaced before
# the test body ever runs -- the writes land in pytest's capture buffer
# and every assertion here sees an empty string.


@pytest.fixture(autouse=True)
def _reset_hud_state():
    """
    hud is a module-level singleton, so flash/state/level leak between
    tests unless they're reset. Caught for real: a five-second flash set
    by one test was still the effective state inside the next one, which
    then quietly asserted against the wrong colour.
    """
    yield
    hud._hud._flash = None
    hud._hud._pending = None
    hud._hud.state = hud.DORMANT
    hud._hud._level = 0.0
    hud._hud._raw_level = 0.0
    hud._hud.debug = False
    hud._hud.last_render_error = None


@pytest.fixture
def captured(monkeypatch):
    """Force the HUD on, pointed at a buffer, with no render thread running."""
    buffer = io.StringIO()
    monkeypatch.setattr(hud._hud, "_stream", buffer)
    monkeypatch.setattr(hud._hud, "enabled", True)
    monkeypatch.setattr(hud._hud, "_size", (100, 30))
    yield buffer
    hud._hud._pending = None
    hud._hud._flash = None
    hud._hud.state = hud.DORMANT


@pytest.fixture
def disabled(monkeypatch):
    buffer = io.StringIO()
    monkeypatch.setattr(hud._hud, "_stream", buffer)
    monkeypatch.setattr(hud._hud, "enabled", False)
    return buffer


def test_every_content_method_renders_without_error(captured):
    hud.banner()
    hud.utterance("open chrome for me")
    hud.pending("consulting local model", spinner=True)
    hud.resolve("focus_or_launch_app   0.95")
    hud.reply("Already up on this desktop, sir.")
    hud.reply("Nothing to minimise.", ok=False)
    hud.line("system message", "system")
    out = captured.getvalue()
    assert "open chrome for me" in out
    assert "Already up on this desktop, sir." in out
    assert "\x1b[" in out


def test_raw_write_failure_is_swallowed_not_propagated(captured, monkeypatch):
    """
    Confirmed live via session.jsonl's threading.excepthook record: a
    plain out.write() call raised OSError("[WinError 1] Incorrect
    function"), and with _raw unguarded that propagated straight out of
    main.py's on_press (a bare hud.line() call, no try/except of its
    own) into `keyboard`'s single shared hook-processing thread, killing
    it outright -- push-to-talk dead for the rest of the process's life.
    hud.line() must never raise, whatever the underlying stream does.
    """

    class _ExplodingStream:
        def write(self, text):
            raise OSError(1, "Incorrect function")

        def flush(self):
            pass

    monkeypatch.setattr(hud._hud, "_stream", _ExplodingStream())
    hud.line("this should not raise", "system")
    assert hud._hud.last_render_error is not None
    assert "Incorrect function" in hud._hud.last_render_error


def test_pending_is_rewritten_in_place_not_appended(captured):
    """
    The whole point of the in-place status line: the transcript is
    echoed once, and the line under it is erased and redrawn as the
    resolution lands. A carriage return plus erase-line is what makes
    that not scroll -- if either goes missing the display starts
    accumulating duplicate half-finished lines again.
    """
    hud.utterance("run ls command")
    hud.pending("consulting local model", spinner=True)
    hud.resolve("count_files_in_folder")
    out = captured.getvalue()
    assert out.count("run ls command") == 1
    assert "\r\x1b[2K" in out


def test_draw_bands_paints_reserved_rows_and_restores_the_cursor(captured):
    hud._hud._draw_bands()
    out = captured.getvalue()
    assert "\x1b7" in out and "\x1b8" in out          # DECSC / DECRC
    assert "\x1b[1;1H" in out                          # top band
    assert "\x1b[30;1H" in out                         # bottom band, rows=30


def test_scroll_region_reserves_top_and_bottom(captured):
    hud._hud._apply_scroll_region()
    # rows=30, one row reserved at the top and three at the bottom.
    assert "\x1b[2;27r" in captured.getvalue()


def test_flash_expires_back_to_the_steady_state():
    hud._hud.state = hud.SESSION
    hud.flash(hud.ACTING, seconds=0.0)
    assert hud._hud._effective_state() == hud.SESSION


def test_flash_does_not_clobber_a_steady_state_set_while_it_is_up():
    """
    The regression that motivated splitting steady state from flash: a
    reply landing just as the session closed used to restore the state
    captured *before* the reply, silently reverting the close.
    """
    hud._hud.state = hud.SESSION
    hud.begin_flash(hud.THINKING)
    hud.set_state(hud.ARMED)
    assert hud._hud._effective_state() == hud.THINKING
    hud.end_flash()
    assert hud._hud._effective_state() == hud.ARMED


def test_end_flash_leaves_a_timed_flash_alone():
    hud._hud.state = hud.ARMED
    hud.flash(hud.ERROR, seconds=5.0)
    hud.end_flash()
    assert hud._hud._effective_state() == hud.ERROR


def test_confidence_bar_colour_tracks_the_score():
    strong = hud.confidence_bar(0.95)
    weak = hud.confidence_bar(0.30)
    assert strong != weak
    assert strong.count("▰") == 10   # full at 0.95 -> rounds to 10 cells
    assert weak.count("▰") == 3


def test_meter_is_empty_when_silent_and_fills_when_loud():
    hud._hud._level = 0.0
    assert hud._hud._meter().strip() == ""
    hud._hud._level = 1.0
    assert "█" in hud._hud._meter()


def test_feed_level_never_allocates_or_raises_on_extremes():
    for value in (0.0, 1.0, 1e-9, 42.0):
        hud.feed_level(value)


def test_recording_band_brightness_tracks_level():
    """The room-lighting effect: band brightness follows the mic, not a constant."""
    hud._hud.state = hud.RECORDING
    hud._hud._colour = list(hud._STATES[hud.RECORDING][0])
    hud._hud._level = 0.0
    quiet = hud._hud._band_colour()
    hud._hud._level = 1.0
    loud = hud._hud._band_colour()
    assert sum(loud) > sum(quiet)


def test_disabled_hud_emits_no_escape_sequences(disabled):
    hud.banner()
    hud.utterance("open chrome")
    # confidence_bar returns a string its caller embeds in a line, so it
    # has to respect the disabled mode too -- it was smuggling colour
    # past the plain-text fallback and into logs/jarvis.log.
    hud.resolve(f"focus_or_launch_app  {hud.confidence_bar(0.95)}")
    hud.reply("Done.")
    hud.line("something", "system")
    out = disabled.getvalue()
    assert "\x1b" not in out
    assert "open chrome" in out


def test_confidence_bar_is_unstyled_when_disabled(disabled):
    assert "\x1b" not in hud.confidence_bar(0.95)
    assert "\x1b" not in hud.confidence_bar(0.2)


def test_note_is_silent_unless_debug_is_on(disabled):
    hud.set_debug(False)
    hud.note("internal detail")
    assert disabled.getvalue() == ""
    hud.set_debug(True)
    hud.note("internal detail")
    assert "internal detail" in disabled.getvalue()
    hud.set_debug(False)


def test_boot_step_reports_failure_without_swallowing_the_exception(captured):
    with pytest.raises(ValueError):
        with hud.boot_step("transcription core", "detail"):
            raise ValueError("boom")
    assert "failed" in captured.getvalue()


def test_init_declines_when_stdout_is_not_a_terminal(monkeypatch):
    """Under pythonw.exe, stdout is logs/jarvis.log -- ANSI must not go there."""
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(hud._hud, "enabled", False)
    monkeypatch.setattr(hud._hud, "_stream", None)
    hud._hud.init()
    assert hud._hud.enabled is False
    assert hud._hud._stream is None


def test_render_loop_survives_a_burst_of_real_work(monkeypatch):
    """
    Runs the actual render thread for a moment against a buffer while
    states, levels and terminal sizes all change underneath it.

    _render_loop has to swallow its own exceptions -- a drawing bug must
    never stop the listener -- which means a broken format string in
    _draw_bands or _status_text would otherwise produce a silently
    frozen HUD and a green test suite. last_render_error exists so that
    failure has somewhere to show up, and this asserts it stays empty.
    """
    buffer = io.StringIO()
    monkeypatch.setattr(hud._hud, "_stream", buffer)
    monkeypatch.setattr(hud._hud, "enabled", True)
    monkeypatch.setattr(hud._hud, "last_render_error", None)
    hud._hud._stop.clear()
    thread = threading.Thread(target=hud._hud._render_loop, daemon=True)
    thread.start()
    try:
        for size in ((100, 30), (40, 12), (200, 60), (20, 6)):
            hud._hud._size = size
            for state in (hud.ARMED, hud.SESSION, hud.RECORDING, hud.THINKING, hud.OFFLINE):
                hud.set_state(state)
                hud.feed_level(0.2)
                hud.set_counters(cmds=7, llm=2)
                time.sleep(0.02)
        hud.pending("consulting local model", spinner=True)
        time.sleep(0.15)
    finally:
        hud._hud._stop.set()
        thread.join(timeout=1.0)
        hud._hud._stop.clear()
    assert hud._hud.last_render_error is None, hud._hud.last_render_error
    assert buffer.getvalue()


def test_bands_disable_autowrap_while_painting(captured):
    """
    The bands are exactly `cols` wide, so the last one fills the
    bottom-right cell -- and with autowrap on, writing that cell scrolls
    the display. DECSC then restores the cursor to a row that has moved,
    and the spinner's next in-place redraw lands on the wrong line. At
    15fps the whole HUD walks down the screen, stranding copies of the
    status band in the scrollback.
    """
    hud._hud._draw_bands()
    out = captured.getvalue()
    assert "\x1b[?7l" in out, "autowrap must be disabled before painting the bands"
    assert "\x1b[?7h" in out, "autowrap must be restored afterwards"
    # Order matters: save cursor, disable wrap, paint, re-enable wrap,
    # restore cursor. Restoring before re-enabling would leave the
    # content stream running with wrap off.
    assert (
        out.index("\x1b7")
        < out.index("\x1b[?7l")
        < out.index("\x1b[1;1H")
        < out.index("\x1b[?7h")
        < out.index("\x1b8")
    )


def test_pending_line_is_truncated_to_one_row(captured):
    """
    The pending line is the only one rewritten in place rather than
    ended with a newline, so a wrap would leave half of it stranded --
    \r + erase-line only clears the last visual row.
    """
    hud._hud._size = (60, 30)
    hud.pending("x" * 500, spinner=True)
    painted = captured.getvalue().rsplit("\r\x1b[2K", 1)[-1]
    visible = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", painted)
    assert len(visible) <= 60
