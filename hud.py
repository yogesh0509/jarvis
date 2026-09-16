"""
The terminal HUD -- the single writer for everything the user sees.

Before this module, output was ~40 scattered print() calls across
main.py and tts.py, on several threads at once. That ruled out anything
that redraws (a spinner, a live meter, a pinned status band): two
threads printing into the same line interleave, and there was no way to
know where the cursor was. Everything user-facing goes through here
now, behind one lock, so the render thread and the listener thread
can't step on each other.

Layout -- a DECSTBM scroll region (ESC[{top};{bottom}r) reserves row 1
and the last three rows, so ordinary printing scrolls only the middle
band and the reserved rows stay put:

    row 1      solid state-colour band
    rows 2..H-3    scrolling transcript (the scroll region)
    row H-2    solid state-colour band
    row H-1    status line: state, mic meter, uptime, counters
    row H      solid state-colour band

The chunky colour bands are not decoration. The monitor-sync lights
behind the display mirror whatever colour is on screen, sampling the
edge zones -- so a coloured glyph in a sea of near-black moves the room
barely at all, while full-width top/bottom bands plus the per-line left
gutter drive every edge zone at once. State colour is eased toward its
target (~300ms) rather than snapped; stepped colour reads as a glitch,
eased colour reads as designed. While an utterance is being recorded
the band's brightness tracks live mic level, so the room breathes with
whatever is being said to it.

Everything degrades to plain, unstyled print() when stdout isn't a
terminal -- pythonw.exe under Task Scheduler redirects stdout to
logs/jarvis.log (see main.py's _redirect_output_if_headless), and a log
file full of escape sequences helps nobody. In that mode no render
thread starts at all.

Cost: one thread at 15fps writing a few hundred bytes. The audio
callbacks only ever hand this module a single float (feed_level) --
nothing here runs on the callback thread, because blocking there drops
frames and costs real transcription accuracy.
"""

import os
import shutil
import sys
import threading
import time
import traceback

# --- state -> colour/label ------------------------------------------
# Built around the existing "Jarvis HUD" Windows Terminal scheme: the
# Mark III hot-rod red at rest, arc-reactor cyan the moment it's
# listening to you, amber while it's thinking, gold on success. The
# red->cyan snap when the wake word lands is the single most visible
# moment in the whole system, in the terminal and in the room.
DORMANT = "dormant"
ARMED = "armed"
SESSION = "session"
RECORDING = "recording"
THINKING = "thinking"
ACTING = "acting"
ERROR = "error"
ASLEEP = "asleep"
OFFLINE = "offline"
# Dictation is a real runtime state, not a flavour of SESSION: while it's
# on, nothing said is matched against an intent at all -- it's typed. It
# used to share SESSION's cyan, so the one state where "power down" does
# NOT close the conversation looked exactly like the one where it does.
DICTATING = "dictating"

_STATES = {
    DORMANT: ((60, 16, 10), "STANDBY"),
    ARMED: ((120, 28, 16), "ARMED"),
    SESSION: ((0, 140, 190), "SESSION OPEN"),
    RECORDING: ((0, 217, 255), "LISTENING"),
    THINKING: ((255, 160, 40), "THINKING"),
    ACTING: ((255, 190, 90), "ACTING"),
    ERROR: ((255, 42, 31), "PROBLEM"),
    ASLEEP: ((26, 8, 5), "LOW POWER"),
    OFFLINE: ((70, 70, 78), "OFF DUTY"),
    DICTATING: ((120, 190, 120), "DICTATING"),
}

# Foreground accents for scrolling content, independent of the state
# colour so a reply stays readable whatever the bands are doing.
_GOLD = (255, 216, 168)
_CYAN = (0, 217, 255)
_AMBER = (255, 176, 64)
_RED = (255, 82, 68)
_DIM = (122, 96, 88)
_WHITE = (240, 230, 225)

_GLYPH = {
    "user": ("⟩", _CYAN),
    "reply": ("◂", _GOLD),
    "intent": ("▸", _AMBER),
    "system": ("◇", _DIM),
    "warn": ("⚠", _RED),
    "dim": ("·", _DIM),
}

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_METER_BLOCKS = " ▁▂▃▄▅▆▇█"
_METER_CELLS = 14

_FPS = 15.0
_EASE = 0.18  # per-frame lerp toward the target colour: ~300ms to settle
_TOP_ROWS = 1
_BOTTOM_ROWS = 3


def _fg(rgb):
    return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def _bg(rgb):
    return f"\x1b[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


_RESET = "\x1b[0m"


class _Hud:
    def __init__(self):
        self.lock = threading.RLock()
        self.enabled = False
        self.debug = False
        self.state = DORMANT
        self._colour = list(_STATES[DORMANT][0])
        self._target = list(_STATES[DORMANT][0])
        self._level = 0.0        # eased mic level, 0..1
        self._raw_level = 0.0    # written by the audio callbacks, read here
        self._started = time.time()
        self._counters = {}
        # Steady state is owned by the listener loop (armed / session /
        # recording / off duty / asleep). Transient overlays -- gold on
        # success, red on a problem, amber while the LLM is thinking --
        # ride on top as a flash and expire back to whatever the steady
        # state has become in the meantime. An earlier version had flash()
        # snapshot the current state and restore it on a timer, which
        # quietly clobbered the steady state whenever the loop changed
        # mode mid-flash (say a reply landing just as a session closed).
        self._flash = None  # (state, expiry_or_None) -- None expiry means "until cleared"
        self._pending = None     # (text, spinner_started_at) for the line being rewritten in place
        self._thread = None
        self._stop = threading.Event()
        self._size = (80, 24)
        # Bound once in init(), after main.py's headless redirect has
        # already decided what stdout is. Held rather than re-resolved
        # on every write: once this module has established its stream is
        # a real terminal, that's the stream it should keep driving,
        # not whatever happens to be bound to sys.stdout later.
        self._stream = None
        # Last render-thread failure, surfaced by `jarvis status`. The
        # loop below has to swallow exceptions -- a drawing bug must
        # never stop the listener -- but swallowing them silently
        # means a HUD that quietly freezes with no way to find out
        # why. Kept so there is something to look at.
        self.last_render_error = None

    def _out(self):
        return self._stream if self._stream is not None else sys.stdout

    # --- lifecycle --------------------------------------------------

    def init(self):
        """Safe to call when stdout is a file or missing -- just stays disabled."""
        stream = sys.stdout
        if stream is None or not hasattr(stream, "isatty"):
            return

        # Force UTF-8 with replacement before anything is written.
        #
        # This is not cosmetic. Python picks the *console codepage* for
        # stdout on Windows, which for a redirected or non-UTF-8 console
        # is cp1252 -- and cp1252 cannot encode the box drawing, arrows
        # or the "greater than or equal" sign in the boot detail lines.
        # Found by actually booting with stdout piped to a file:
        # UnicodeEncodeError out of the very first boot step, killing
        # the process outright before the listener ever started. The
        # plain-text fallback is just as exposed as the styled path, so
        # this runs before the isatty check rather than after it.
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

        try:
            if not stream.isatty():
                return
        except Exception:
            return
        if os.environ.get("JARVIS_PLAIN"):
            return
        if not _enable_vt():
            return
        self._stream = stream
        self.enabled = True
        self._size = shutil.get_terminal_size((80, 24))
        self._apply_scroll_region()
        self._thread = threading.Thread(target=self._render_loop, daemon=True)
        self._thread.start()

    def shutdown(self):
        if not self.enabled:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        with self.lock:
            self._finalize_pending()
            # Drop the scroll region and put the cursor somewhere sane,
            # otherwise the shell inherits a terminal that only scrolls
            # its middle rows for the rest of the window's life.
            out = self._out()
            out.write("\x1b[r\x1b[?25h")
            rows = self._size[1]
            out.write(f"\x1b[{rows};1H\n")
            out.flush()
        self.enabled = False

    # --- inputs from the rest of the app ----------------------------

    def feed_level(self, rms):
        """
        Called from the sounddevice input callbacks. Deliberately the
        cheapest thing that could work -- one float store, no lock, no
        allocation, no I/O. All the actual rendering happens on the
        render thread; anything that blocks an audio callback drops
        frames, and dropped frames cost transcription accuracy.
        """
        if rms > self._raw_level:
            self._raw_level = rms
        else:
            self._raw_level = self._raw_level * 0.7 + rms * 0.3

    def set_state(self, state):
        """Set the steady state. Never disturbs an in-flight flash."""
        with self.lock:
            if state in _STATES:
                self.state = state

    def flash(self, state, seconds=0.45):
        """Transient colour that expires back to the steady state."""
        with self.lock:
            if state in _STATES:
                self._flash = (state, time.time() + seconds)

    def begin_flash(self, state):
        """Open-ended flash -- held until end_flash(). Used for 'thinking'."""
        with self.lock:
            if state in _STATES:
                self._flash = (state, None)

    def end_flash(self):
        with self.lock:
            if self._flash is not None and self._flash[1] is None:
                self._flash = None

    def _effective_state(self):
        flash = self._flash
        if flash is not None:
            if flash[1] is None or time.time() < flash[1]:
                return flash[0]
            self._flash = None
        return self.state

    def set_counters(self, **values):
        with self.lock:
            self._counters.update(values)

    # --- scrolling content ------------------------------------------

    def line(self, text, kind="system"):
        """One finished line of transcript, with its glyph and left gutter."""
        glyph, colour = _GLYPH.get(kind, _GLYPH["system"])
        with self.lock:
            self._finalize_pending()
            if not self.enabled:
                self._out().write(f"{text}\n")
                self._out().flush()
                return
            self._raw(f"{self._gutter()}{_fg(colour)}{glyph}{_RESET} {_fg(_WHITE)}{text}{_RESET}\n")

    def utterance(self, text):
        """
        What was heard, printed the instant transcription finishes and
        before any matching or dispatch -- resolving can take seconds
        (the LLM fallback especially) and without an immediate echo
        that reads as having gone unresponsive.

        The old output printed the transcript again alongside the
        resolution, which was the single strongest "this is a log file"
        tell. Now a status line opens underneath it and is rewritten in
        place as the resolution lands, so each utterance occupies one
        block that evolves rather than three lines that repeat.
        """
        glyph, colour = _GLYPH["user"]
        stamp = time.strftime("%H:%M:%S")
        with self.lock:
            self._finalize_pending()
            if not self.enabled:
                self._out().write(f'Heard: "{text}"\n')
                self._out().flush()
                return
            self._raw(
                f"\n{self._gutter()}{_fg(_DIM)}{stamp}{_RESET}  "
                f"{_fg(colour)}{glyph}{_RESET} {_fg(_WHITE)}{text}{_RESET}\n"
            )

    def pending(self, text, spinner=False):
        """Open (or replace) the in-place status line under the current utterance."""
        with self.lock:
            if not self.enabled:
                if self.debug:
                    self._out().write(f"[{text}]\n")
                    self._out().flush()
                return
            self._finalize_pending(newline=False)
            self._pending = (text, time.time() if spinner else None)
            self._paint_pending()

    def resolve(self, text, kind="intent"):
        """Rewrite the open status line one last time and close it off."""
        glyph, colour = _GLYPH.get(kind, _GLYPH["intent"])
        with self.lock:
            if not self.enabled:
                self._out().write(f"{text}\n")
                self._out().flush()
                return
            self._pending = None
            self._raw(f"\r\x1b[2K{self._gutter()}{'':10}{_fg(colour)}{glyph}{_RESET} {_fg(colour)}{text}{_RESET}\n")

    def reply(self, text, ok=True):
        kind = "reply" if ok else "warn"
        glyph, colour = _GLYPH[kind]
        with self.lock:
            self._finalize_pending()
            if not self.enabled:
                self._out().write(f"{text}\n")
                self._out().flush()
                return
            self._raw(f"{self._gutter()}{'':10}{_fg(colour)}{glyph}{_RESET} {_fg(colour)}{text}{_RESET}\n")
        self.flash(ACTING if ok else ERROR)

    def note(self, text):
        """Dim aside -- only shown when debug output is on."""
        if not self.debug:
            return
        with self.lock:
            self._finalize_pending()
            if not self.enabled:
                self._out().write(f"[{text}]\n")
                self._out().flush()
                return
            self._raw(f"{self._gutter()}{'':10}{_fg(_DIM)}· {text}{_RESET}\n")

    # --- rendering internals ----------------------------------------

    def _gutter(self):
        """
        Two columns of state colour down the left edge of every content
        line. Cheap vertical colour for the left-hand light zone, and
        it doubles as a history track: a line keeps the colour of the
        state it was printed in.
        """
        return f"{_bg(tuple(int(c) for c in self._colour))}  {_RESET} "

    def _raw(self, text):
        out = self._out()
        out.write(text)
        out.flush()

    def _finalize_pending(self, newline=True):
        if self._pending is not None:
            self._pending = None
            if newline and self.enabled:
                self._raw("\n")

    def _paint_pending(self):
        """
        Rewrites the current line without scrolling -- carriage return,
        erase-line, redraw. The cursor stays on that line the whole
        time, so the spinner can tick at 15fps without touching the
        scrollback at all.
        """
        if self._pending is None:
            return
        text, spin_since = self._pending
        if spin_since is not None:
            frame = _SPINNER[int((time.time() - spin_since) * 12) % len(_SPINNER)]
            elapsed = time.time() - spin_since
            suffix = f"  {elapsed:4.1f}s"
        else:
            frame = "◍"
            suffix = ""
        # Truncated to fit on one row. This is the only line that gets
        # rewritten in place rather than terminated with a newline, so
        # if it ever wrapped, the \r\x1b[2K above would clear just the
        # last visual row and leave the rest of it stranded on screen.
        # Nothing currently sent here is anywhere near that long, but
        # the failure mode is invisible until it isn't.
        room = max(8, self._size[0] - 16 - len(suffix))
        text = text if len(text) <= room else text[: room - 1] + "…"
        self._raw(
            f"\r\x1b[2K{self._gutter()}{'':10}{_fg(_AMBER)}{frame}{_RESET} "
            f"{_fg(_AMBER)}{text}{_fg(_DIM)}{suffix}{_RESET}"
        )

    def _apply_scroll_region(self):
        cols, rows = self._size
        top = _TOP_ROWS + 1
        bottom = max(top, rows - _BOTTOM_ROWS)
        # Clear, set the region, then park the cursor at its bottom so
        # the first content line scrolls up from the right place.
        self._raw(f"\x1b[2J\x1b[{top};{bottom}r\x1b[{bottom};1H")

    def _render_loop(self):
        ticks = 0
        while not self._stop.is_set():
            try:
                ticks += 1
                # Terminal size is a syscall -- once a second is plenty
                # to catch a resize, and 15 times a second is waste.
                if ticks % int(_FPS) == 0:
                    size = shutil.get_terminal_size((80, 24))
                    if tuple(size) != tuple(self._size):
                        with self.lock:
                            self._size = size
                            self._apply_scroll_region()
                self._tick_colour()
                with self.lock:
                    self._draw_bands()
                    if self._pending is not None and self._pending[1] is not None:
                        self._paint_pending()
            except Exception:
                # A render failure must never take down the process or
                # stop the listener -- worst case the HUD goes static.
                self.last_render_error = traceback.format_exc()
            self._stop.wait(1.0 / _FPS)

    def _tick_colour(self):
        self._target = list(_STATES[self._effective_state()][0])
        for i in range(3):
            self._colour[i] += (self._target[i] - self._colour[i]) * _EASE
        self._level += (min(1.0, self._raw_level * 9.0) - self._level) * 0.35
        self._raw_level *= 0.82

    def _band_colour(self):
        """
        While recording, band brightness tracks live mic level -- this
        is what makes the room breathe with your voice. Every other
        state sits at a steady brightness.
        """
        base = [int(c) for c in self._colour]
        if self._effective_state() == RECORDING:
            scale = 0.45 + 0.55 * self._level
            return tuple(min(255, int(c * scale)) for c in base)
        return tuple(base)

    def _meter(self):
        filled = self._level * _METER_CELLS
        out = []
        for i in range(_METER_CELLS):
            frac = max(0.0, min(1.0, filled - i))
            out.append(_METER_BLOCKS[int(frac * (len(_METER_BLOCKS) - 1))])
        return "".join(out)

    def _status_text(self, cols):
        label = _STATES[self._effective_state()][1]
        up = int(time.time() - self._started)
        uptime = f"{up // 3600:d}:{(up // 60) % 60:02d}:{up % 60:02d}"
        counts = "  ".join(f"·{v} {k}" for k, v in self._counters.items() if v)
        left = f" ◉ {label:<13} {self._meter()} "
        right = f" up {uptime}   {counts} "
        pad = max(1, cols - len(left) - len(right))
        return f"{left}{' ' * pad}{right}"[:cols]

    def _draw_bands(self):
        cols, rows = self._size
        colour = self._band_colour()
        # Text on the band needs to stay readable against both the near
        # -black standby ember and the bright cyan of active listening.
        luma = 0.299 * colour[0] + 0.587 * colour[1] + 0.114 * colour[2]
        ink = (10, 4, 3) if luma > 140 else _GOLD
        band = f"{_bg(colour)}{' ' * cols}{_RESET}"
        status = f"{_bg(colour)}{_fg(ink)}{self._status_text(cols)}{_RESET}"
        # Autowrap (DECAWM) is turned OFF for the duration of this paint
        # and back on afterwards. The bands are exactly `cols` wide, so
        # the last one fills the bottom-right cell of the screen -- and
        # with autowrap on, writing that cell scrolls the display by a
        # line.
        #
        # That was corrupting the whole HUD, and in a way that took a
        # while to read: DECSC saves the cursor, the band paint scrolls
        # the screen underneath it, and DECRC then restores to a row
        # that has since moved. The next _paint_pending() erases and
        # redraws the spinner on the *wrong* line. At 15fps the display
        # walks steadily down the screen, stranding copies of the status
        # band in the scrollback with runs of blank lines between them.
        # Only ever visible while the spinner was running, because every
        # other write ends in a newline and self-corrects.
        #
        # DECSC/DECRC around the whole thing: the cursor has to leave
        # the scroll region to paint the reserved rows and must land
        # back exactly where the content stream left it.
        self._raw(
            f"\x1b7\x1b[?25l\x1b[?7l"
            f"\x1b[1;1H{band}"
            f"\x1b[{rows - 2};1H{band}"
            f"\x1b[{rows - 1};1H{status}"
            f"\x1b[{rows};1H{band}"
            f"\x1b[?7h\x1b8\x1b[?25h"
        )

    # --- boot sequence ----------------------------------------------

    def banner(self):
        if not self.enabled:
            self._out().write("Booting up the ears, sir. One moment...\n")
            self._out().flush()
            return
        gold, cyan, dim = _fg(_GOLD), _fg(_CYAN), _fg(_DIM)
        with self.lock:
            self._raw(
                f"\n{self._gutter()}{gold}   ╭{'─' * 58}╮{_RESET}\n"
                f"{self._gutter()}{gold}   │   {cyan}J · A · R · V · I · S{gold}"
                f"        {dim}mark iii · local · offline{gold}   │{_RESET}\n"
                f"{self._gutter()}{gold}   ╰{'─' * 58}╯{_RESET}\n\n"
            )

    def boot_step(self, label, detail=""):
        """
        Context manager around a startup stage: prints the label, fills
        a progress bar as the stage actually completes, and reports the
        real elapsed time. Pure theatre over latency that was being
        spent anyway -- the old boot printed flat lines and then stalled
        silently for three seconds loading Whisper.
        """
        return _BootStep(self, label, detail)


class _BootStep:
    def __init__(self, hud, label, detail):
        self.hud = hud
        self.label = label
        self.detail = detail
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.time()
        if self.hud.enabled:
            with self.hud.lock:
                self.hud._raw(
                    f"{self.hud._gutter()}{_fg(_DIM)}◇ {self.label:<22}"
                    f"{_fg(_DIM)}{'▱' * 10}  working{_RESET}"
                )
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed = time.time() - self.t0
        ok = exc_type is None
        if not self.hud.enabled:
            self.hud._out().write(f"  {self.label}: {'ok' if ok else 'FAILED'} ({elapsed:.1f}s) {self.detail}\n")
            self.hud._out().flush()
            return False
        colour = _GOLD if ok else _RED
        word = "online" if ok else "failed"
        with self.hud.lock:
            self.hud._raw(
                f"\r\x1b[2K{self.hud._gutter()}{_fg(_DIM)}◇ {_fg(_WHITE)}{self.label:<22}"
                f"{_fg(colour)}{'▰' * 10}  {word:<8}{_RESET}"
                f"{_fg(_DIM)}{elapsed:5.1f}s  {self.detail}{_RESET}\n"
            )
        return False


def _enable_vt():
    """
    Windows Terminal has VT processing on by default, but a plain
    conhost window inherited from a .cmd launcher may not -- turn it on
    explicitly rather than assuming, and report failure so the HUD can
    fall back to plain text instead of spraying escape codes.
    """
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


_hud = _Hud()

init = _hud.init
shutdown = _hud.shutdown
feed_level = _hud.feed_level
set_state = _hud.set_state
flash = _hud.flash
begin_flash = _hud.begin_flash
end_flash = _hud.end_flash
set_counters = _hud.set_counters
line = _hud.line
utterance = _hud.utterance
pending = _hud.pending
resolve = _hud.resolve
reply = _hud.reply
note = _hud.note
banner = _hud.banner
boot_step = _hud.boot_step


def last_render_error():
    return _hud.last_render_error


def set_debug(on):
    _hud.debug = bool(on)


def is_debug():
    return _hud.debug


def confidence_bar(score, cells=10):
    """
    A number between 0 and 1 is something you parse; a bar is something
    you glance at. Gold at high confidence, amber in the middle, red
    where the match is a guess -- so a suspect resolution is visible
    without reading the digits.
    """
    filled = int(round(max(0.0, min(1.0, score)) * cells))
    bar = "▰" * filled + "▱" * (cells - filled)
    # Unstyled when the HUD is disabled. This function returns a string
    # that gets embedded in a line by its caller, so without this check
    # it smuggles escape sequences straight past the plain-text
    # fallback and into logs/jarvis.log under pythonw.exe.
    if not _hud.enabled:
        return bar
    colour = _GOLD if score >= 0.85 else (_AMBER if score >= 0.60 else _RED)
    return f"{_fg(colour)}{'▰' * filled}{_fg(_DIM)}{'▱' * (cells - filled)}{_RESET}"


if __name__ == "__main__":
    # Live preview, so the display can be looked at (and the room
    # lights behind it judged) without booting the whole stack:
    #     .venv\Scripts\python.exe hud.py
    # Cycles every state with a synthetic mic level so the recording
    # band's brightness modulation is visible.
    import math
    import random

    init()
    banner()
    for stage, detail in (
        ("acoustic frontend", "silero vad · 16000Hz"),
        ("transcription core", "whisper small.en · int8"),
        ("intent lattice", ""),
        ("wake sentinel", '"hey jarvis" ≥ 0.50'),
    ):
        with boot_step(stage, detail):
            time.sleep(0.4)
    line("preview · ctrl-c to stop", "dim")

    demo = [
        (ARMED, "open chrome for me", "focus_or_launch_app", 0.95, "Already up on this desktop, sir."),
        (SESSION, "move to the previous window", "switch_desktop", 0.87, "Moving previous."),
        (SESSION, "run the ls command", None, 0.0, "No File Explorer window is focused, sir."),
        (SESSION, "okay close this terminal", "minimize_app", 0.62, "Out of your way, sir."),
    ]
    try:
        start = time.time()
        while True:
            for state, said, intent, score, answer in demo:
                set_state(state)
                for _ in range(14):
                    feed_level(0.02)
                    time.sleep(0.05)
                set_state(RECORDING)
                utterance(said)
                for i in range(26):
                    feed_level(abs(math.sin(i / 3.0)) * random.uniform(0.06, 0.16))
                    time.sleep(0.05)
                set_state(SESSION)
                if intent is None:
                    begin_flash(THINKING)
                    pending("consulting local model", spinner=True)
                    time.sleep(2.2)
                    end_flash()
                    resolve("count_files_in_folder · terminal   local llm  2.2s")
                    reply(answer, ok=False)
                else:
                    resolve(f"{intent}   {score:.2f} {confidence_bar(score)}")
                    reply(answer)
                set_counters(cmds=int(time.time() - start) // 3, llm=1)
                time.sleep(0.8)
    except KeyboardInterrupt:
        shutdown()
