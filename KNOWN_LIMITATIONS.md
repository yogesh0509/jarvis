# Known limitations / open items

Things that are known to be incomplete, provisional, or deliberately
deferred -- not bugs waiting to be fixed by accident, but real gaps worth
knowing about before relying on or extending the relevant area. See
`DECISIONS.md` for the reasoning behind the design these limitations sit
on top of.

- **No UI Automation / Accessibility API integration.** Everything this
  project knows about a window's content comes from its title string
  (`app_titles.py`) -- it can say what VS Code file or Chrome tab you're
  on, but it can't read Chrome's actual URL, a document's text, or
  anything a title doesn't already carry. UIA is reachable from this
  codebase's existing dependencies with no new install (raw `comtypes`
  already used by `virtual_desktop.py` can load `UIAutomationCore.dll`
  directly), but was deliberately not pursued: `IUIAutomationElement`
  exposes on the order of 80 methods where `IVirtualDesktopManager` has
  3, and a wrong hand-written vtable declaration doesn't raise a
  catchable exception the way a COM error does -- it calls whatever
  function pointer happens to sit at that offset, a hard crash in the
  one process that must not lose its listener thread over it. Revisit
  only if a concrete need survives everything title-based context
  already covers, and only via the `uiautomation` PyPI package (a
  maintained, tested vtable binding) rather than hand-declared
  `COMMETHOD`s.
- **`logs/session.jsonl` is never rotated or pruned.** One line per
  event, so realistically kilobytes per day -- but it grows without
  bound across a long-running install. Delete it whenever; nothing reads
  it back.
- **The HUD's pinned bands depend on the terminal honouring DECSTBM
  scroll regions.** Windows Terminal does, and there's a fallback to
  plain text when stdout isn't a terminal at all -- but a terminal that
  accepts VT sequences while ignoring `ESC[{top};{bottom}r` would scroll
  the reserved rows away rather than degrading cleanly. `python hud.py`
  renders the whole display standalone, which is the fastest way to
  check a given terminal before wiring anything else to it.

- **The session listener blocks synchronously while handling one
  command.** Transcription + action dispatch (up to ~5s for a cold app
  launch that has to wait for a window to appear; a warm LLM fallback is
  a few seconds, a cold one -- first call in a process, before the
  background prewarm finishes, or after the pre-LLM gate's rejection
  path -- more like 13-15s post-shrink, see DECISIONS.md's "Latency"
  section) runs inline in the same thread that watches for the next
  utterance. Nothing is lost during this window -- incoming audio still
  queues (`audio_q`, unbounded) and gets processed once the thread frees
  up, though nothing currently trims that backlog if a very long dispatch
  lets it grow -- but Jarvis is genuinely unresponsive-feeling for that
  stretch. Fixing this properly means the session state machine tracking
  an in-flight command while still watching for the next utterance
  concurrently; not done, since it is a real restructuring of the state
  machine, not a contained fix.
- **"Hey jarvis" occasionally not registering for a stretch right after
  boot** was reported once; the persistent push-to-talk stream fix
  addresses one plausible contributor (the same device cold-open glitch
  class), but this wasn't confirmed as the full explanation. If it
  recurs, the next step is adding wake-score logging around startup to
  see what's actually happening, rather than another guess.
- **`VAD_SPEECH_THRESHOLD` (0.15) and the onset debounce (3 frames) are
  provisional** -- reasonable given the evidence so far, but the exact
  cause of at least one false-positive ("bdk"/"boom" transcribed from
  what was assumed to be background fan noise) was never independently
  confirmed. Revisit either value if quiet speech starts getting missed,
  or if background noise still occasionally gets treated as speech.
