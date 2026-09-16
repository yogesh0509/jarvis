# Scheduler entries

The three Windows Task Scheduler tasks that start and stop Jarvis on
their own. Together they are the *entire* implementation of the "starts
at login, quits on sleep, comes back on wake" behaviour that
`USER_GUIDE.md` section 5 draws as a state diagram — **nothing in the
Python code observes power events at all.** No `WM_POWERBROADCAST`, no
console control handler, no `atexit`. If a wake-from-sleep leaves nothing
running, this directory is where to look, not `main.py`.

They previously existed only in the live Task Scheduler database, which
made a documented behaviour unreviewable, unreproducible on another
machine, and easy to mistake for something the code was doing.

| Task | Trigger | Action |
|---|---|---|
| `Jarvis Voice Control` | logon | `wt.exe -F -p Jarvis` |
| `Jarvis Quit On Sleep` | Kernel-Power **42** (system entering sleep) | `jarvisctl.py quit` |
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
machine suspends, and it deliberately blocks on audio — a spin-down
earcon, then Piper synthesis and playback — against an audio stack
Windows is already tearing down, so the outgoing process is routinely
still alive when the machine comes back. With no delay, the incoming
process hit the single-instance mutex in `main.py` and stood down,
leaving **nothing running until the next logon**. Silently, too: it exits
0, so Task Scheduler recorded a success either way.

That is fixed in two places, on purpose, because the failure is invisible
from outside and neither fix is expensive:

- `main.py`'s `_acquire_single_instance_lock` now waits out an incumbent
  for `SINGLE_INSTANCE_WAIT_SECONDS` before standing down, and logs it to
  `logs/session.jsonl` when it does.
- `Jarvis Restart On Wake` waits 15 seconds before launching at all.

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
