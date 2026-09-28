# Scheduler entries

The three Windows Task Scheduler tasks that start and stop Jarvis on
their own. Together they are the *entire* implementation of the "starts
at login, quits on sleep, comes back on wake" behaviour that
`USER_GUIDE.md`'s "States" section draws as a state diagram — **nothing in the
Python code observes power events at all.** No `WM_POWERBROADCAST`, no
console control handler, no `atexit`. If a wake-from-sleep leaves nothing
running, this directory is where to look, not `main.py`.

They previously existed only in the live Task Scheduler database, which
made a documented behaviour unreviewable, unreproducible on another
machine, and easy to mistake for something the code was doing.

| Task | Trigger | Action |
|---|---|---|
| `Jarvis Voice Control` | logon | `wt.exe -F -p Jarvis` |
| `Jarvis Quit On Sleep` | Kernel-Power **42** (system entering sleep) | `jarvisctl.py quit silent` |
| `Jarvis Restart On Wake` | Power-Troubleshooter **1** (resumed) | `wt.exe -F -p Jarvis`, after a 15s delay |

## Install

```powershell
.\scheduler\register_tasks.ps1          # add -WhatIf to see what it would do
```

Re-runnable: each task is registered with `/f`, so editing a template and
running it again replaces the live entry. The `{{USER}}`,
`{{JARVIS_DIR}}` and `{{WT_EXE}}` placeholders are filled in at
registration, so the committed XML isn't pinned to one machine's SID and
paths. `schtasks` insists on UTF-16 XML; the script handles that, and the
templates stay UTF-8 so they diff cleanly.

## Why the wake task has a delay

Nothing sequences the quit against the restart. The quit fires as the
machine suspends, and it *used to* block on audio — a spin-down earcon,
then Piper synthesis and playback — against an audio stack Windows is
already tearing down (see "Why the sleep quit is silent" below), so the
outgoing process is routinely still alive when the machine comes back.
It can still outlive the suspend even now, since nothing guarantees the
quit completes before Windows freezes the process. With no delay, the incoming
process hit the single-instance mutex in `main.py` and stood down,
leaving **nothing running until the next logon**. Silently, too: it exits
0, so Task Scheduler recorded a success either way.

That is fixed in two places, on purpose, because the failure is invisible
from outside and neither fix is expensive:

- `main.py`'s `_acquire_single_instance_lock` now waits out an incumbent
  for `SINGLE_INSTANCE_WAIT_SECONDS` before standing down, and logs it to
  `logs/session.jsonl` when it does.
- `Jarvis Restart On Wake` waits 15 seconds before launching at all.

## Why the sleep quit is silent

`Jarvis Quit On Sleep` runs `jarvisctl.py quit **silent**`, which skips
the spin-down earcon and the spoken goodbye and goes straight to
releasing both audio devices. Quitting by hand still gets the full
sign-off; this is opt-in for exactly one caller.

The reason is timing. Kernel-Power 42 fires once Windows has **already
begun** suspending, so whatever the quit does next is racing the
suspend. The goodbye is several seconds of blocking work — Piper
synthesis, then playback — and losing that race has a specific cost.

Reconstructed from the 2026-09-17 incident:

```
02:03:02  Windows begins suspend
02:03:04  task fires -> quit received -> blocking earcon + Piper playback
          ...process frozen mid-teardown for 12h50m...
14:52:53  Intel SST reloads DSP firmware   <-- driver reinitialising
14:52:54  Windows resumes
14:52:57  the teardown finally completes   <-- 4s into the reload
```

So the process closed suspend-invalidated stream handles into a driver
that was four seconds into reloading its firmware. The capture pipeline
came back delivering a constant 1 LSB and stayed deaf through a full
Jarvis restart; only an audio-stack reset cleared it. The one earlier
cycle where the process also survived a suspend finished its teardown
24 seconds after wake, clear of the reload, and the mic was fine.

That is n=1 with a plausible mechanism, **not proof** — Intel SST DSPs
wedge on their own, with no Python anywhere near them. But the silent
quit turns seconds of work into milliseconds for no real loss (nobody
hears a goodbye from a machine that is already asleep), so it is worth
doing whether or not it was the cause. `_shutdown()` also now closes the
TTS output stream, which it previously left to interpreter teardown, so
every audio handle is handed back deliberately before the models are
unloaded.

If the wedge recurs on a cycle that used the silent quit, Jarvis is
exonerated and it is a driver bug to report upstream.

## Caveats worth knowing

- **The lid is never observed.** The chain is lid → Windows initiates
  sleep → Kernel-Power 42 → task. A machine on modern standby, or set to
  "do nothing" on lid close, may never emit event 42, and then none of
  this fires.
- **`StartWhenAvailable` is set on the logon task**, so a missed logon
  launch can fire late — another way two instances briefly overlap, and
  another reason the mutex waits rather than exits.
- **These tasks run as the interactive user**, not SYSTEM: Jarvis needs a
  desktop session for the mic, global hotkeys and window management.
- **A paused Jarvis still holds the microphone across a suspend.**
  `_set_enabled(False)` closes the session-listening stream but
  deliberately leaves `hotkey_stream` open, because push-to-talk is
  documented to keep working while paused (see `USER_GUIDE.md`). Left
  as-is rather than quietly breaking that contract — but it does mean
  `jarvis pause` is not a way to release the capture device before
  sleeping. The sleep quit is what releases it; if that quit never
  arrives (see the lid caveat above), the device stays held for the whole
  suspend.
