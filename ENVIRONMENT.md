# Environment quirks

Two quirks specific to the reference development machine -- not
Jarvis-the-project bugs, but things that will burn time again if
forgotten. Project overview and setup live in `README.md`; the reasoning
behind Jarvis's own design choices lives in `DECISIONS.md`.

## Known environment issue: TLS 1.3 gets reset

Something on this machine -- almost certainly antivirus HTTPS/web-shield
inspection, though never confirmed which product -- resets TLS 1.3
handshakes made by Python's TLS stack (`ConnectionResetError: [WinError
10054]`) while leaving TLS 1.2 and non-Python HTTPS clients (PowerShell's
`Invoke-WebRequest`, i.e. .NET's stack) untouched. Confirmed by hand:

- `Invoke-WebRequest -Uri https://huggingface.co` from PowerShell: works
- Python `ssl` handshake capped at `maximum_version = TLSv1_2`: works
- Python `ssl` handshake with default (TLS 1.3 offered): connection reset

Even with the TLS version capped, retries to huggingface.co from Python
stayed unreliable (worked once, failed ten times straight right after) --
this looks like TLS client fingerprinting, not just a protocol-version
block. **The practical resolution was to stop trying to make Python
download from huggingface.co at all**: every model is fetched by hand via
`Invoke-WebRequest` into its `models/<name>/` directory (see README.md's
"Fetching the models"), and every module that loads a model
(`local_embedder.py`, `llm_intent.py`, `main.py`'s Whisper loading,
`tts.py`) reads directly from that local directory, bypassing
`huggingface_hub`/`fastembed`'s auto-download entirely. There is no code
left in this repo that makes an HTTPS call at runtime, so this is now
purely a "how do I fetch the next model" note, not a live workaround
(`tls_compat.py`, a monkeypatch shim for exactly this, existed early on
for the abandoned in-Python-download approach and was removed once every
model moved to the manual-fetch pattern above).

Worth identifying which security product is doing the inspection at some
point and either updating it (several vendors have shipped TLS 1.3
handshake fixes) or excluding this dev environment -- the manual-fetch
workaround is fine indefinitely for this project specifically since every
model is a one-time download, but it'll resurface for any *other* Python
tool that needs to hit huggingface.co from this machine.

## Known environment quirk: every venv python.exe launch spawns a second process

Any script run via `D:\jarvis\.venv\Scripts\python.exe` -- even an empty
one -- spawns a child process a moment later running the *same* script
under the base interpreter (`C:\Users\agraw\...\Python312\python.exe`).
Reproduced with zero project imports, so it's not our code; almost
certainly the same security software again, doing some kind of
process-reputation check by shadow-executing new/unrecognized binaries.
The child has never been observed to reach real program logic (a log-write
smoke test only ever saw the parent's PID) but `main.py` takes a named
mutex (`_acquire_single_instance_lock`) as a hard guarantee regardless --
correct practice for an autostart background service anyway, independent
of this quirk.
