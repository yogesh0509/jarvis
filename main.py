"""
Main loop: say "hey jarvis" once to open a session, then speak any
number of commands -- each is segmented by silence (VAD), transcribed,
matched, and executed, with no wake word needed in between. A stop
phrase ("power down", "stop listening", ...) closes the session back
to wake-word-armed. RIGHT CTRL push-to-talk works too, any time, via
its own independent audio stream -- a manual override that works
regardless of whether the main listener is on, off, or asleep.
Controlled from the command line via jarvisctl.py (status/pause/
resume/quit) talking to a local control socket -- no tray icon, no
GUI. See README.md for the phase-by-phase build and the design
rationale behind each piece.
"""

import queue
import socket
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path

import numpy as np
import sounddevice as sd
import keyboard
import win32api
import win32event
import winerror
from faster_whisper import WhisperModel
from openwakeword.model import Model as WakeWordModel

from intent_matcher import IntentMatcher, INTENTS
from actions import dispatch_llm_action, parse_fast_path_intent
from vad import SileroVAD
from tts import speak, is_speaking
import llm_intent
import seed_promotion

CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 47123

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80ms at 16kHz -- openWakeWord's native chunk size
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE
HOTKEY = "right ctrl"
WAKE_WORD = "hey_jarvis"
WAKE_THRESHOLD = 0.5
VAD_SPEECH_THRESHOLD = 0.15  # 0.5 -> 0.35 -> 0.2 -> 0.1 -> 0.15: 0.1 caught normal
# speaking volume reliably but proved too sensitive to background noise/room
# chatter as false speech-onset; nudged back up slightly -- still noticeably
# more sensitive than the original 0.5, just not maximally so. Revisit toward
# 0.1 if quiet speech starts getting missed again, or up toward 0.2 if it's
# still picking up too much ambient noise.

# "switch to desktop 2"/"switch to desktop two" -- both digit and spelled-out
# forms verified (in testing) to false-positive match "next desktop" seeds
# with high confidence (0.87+). Absolute desktop numbers aren't supported.
_NUMBER_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"}


def _frames(seconds: float) -> int:
    return max(1, round(seconds / FRAME_SECONDS))


SILENCE_HANG_FRAMES = _frames(0.4)  # quiet after speech ends one command -- was
# 0.8s, felt laggy for one-sentence commands; halved. Revisit upward if it starts
# cutting off mid-sentence pauses.
SPEECH_ONSET_FRAMES = _frames(0.24)  # consecutive VAD-positive frames required
# before actually committing to "recording started" -- a single frame crossing
# VAD_SPEECH_THRESHOLD isn't enough evidence of real speech. Continuous
# background noise (a fan, say) can spike above threshold for one frame even
# though it's not speech at all (confirmed directly: fan noise alone got
# transcribed as stray words like "bdk"/"boom"). Real speech sustains across
# several consecutive frames; a noise spike usually doesn't. The pre_buffer
# below already covers the actual head of the utterance regardless of exactly
# when this debounce confirms, so this costs no real audio, just ~240ms of
# certainty before starting to record.
MAX_COMMAND_FRAMES = _frames(7.2)  # hard cap per command
SESSION_IDLE_TIMEOUT_FRAMES = _frames(4 * 60)  # session auto-closes after this much silence
PRE_BUFFER_FRAMES = _frames(0.3)  # kept rolling so speech-onset isn't clipped by VAD detection lag
DEEP_SLEEP_IDLE_SECONDS = 30 * 60  # unload the heavy models after this long with nothing happening

WHISPER_MODEL_SIZE = "small.en"
# huggingface_hub's auto-download is unreliable here (see README) --
# fetched by hand into a local dir; fall back to the model-name string
# (which triggers the normal, occasionally-flaky auto-download) if the
# local copy isn't there, e.g. on a fresh machine.
WHISPER_LOCAL_DIR = Path(__file__).parent / "models" / "faster-whisper-small.en"
SINGLE_INSTANCE_MUTEX_NAME = "Global\\JarvisVoiceControlSingleInstance"


def _acquire_single_instance_lock():
    """
    Something on this machine transparently spawns a second process
    whenever the venv's python.exe runs at all (reproduces even on a
    trivial no-op script -- looks like security-software process
    verification, not anything in our code; see README). It hasn't
    been seen to run our actual logic, but a named mutex makes that
    moot: only one process ever gets past this, full stop.
    """
    mutex = win32event.CreateMutex(None, False, SINGLE_INSTANCE_MUTEX_NAME)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        print("Another copy of me is already running, sir. Standing down.")
        sys.exit(0)
    return mutex


def _redirect_output_if_headless():
    """
    pythonw.exe (used by the Task Scheduler autostart entry, so no
    console window flashes at every login) sets sys.stdout/stderr to
    None -- there's no console to attach to. Redirect to a log file in
    that case so the status output isn't just lost; leave it alone when
    running interactively (python.exe with a real console), which is
    what every test in this project so far has used.
    """
    if sys.stdout is not None:
        return
    log_path = Path(__file__).parent / "logs" / "jarvis.log"
    log_path.parent.mkdir(exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = log_file
    sys.stderr = log_file


def main():
    _redirect_output_if_headless()
    _mutex = _acquire_single_instance_lock()

    # whisper_model / matcher start unloaded and get loaded by
    # _ensure_heavy_models_loaded() below, right before the first real
    # use -- keeps startup path and the deep-sleep reload path identical.
    whisper_model = None
    matcher = None

    def _load_heavy_models():
        nonlocal whisper_model, matcher
        whisper_source = str(WHISPER_LOCAL_DIR) if WHISPER_LOCAL_DIR.is_dir() else WHISPER_MODEL_SIZE
        whisper_model = WhisperModel(whisper_source, device="cpu", compute_type="int8")
        list(whisper_model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language="en")[0])
        matcher = IntentMatcher()

    def _unload_heavy_models():
        nonlocal whisper_model, matcher
        if whisper_model is None:
            return
        whisper_model = None
        matcher = None
        # llm_intent lazy-loads itself independently (only on first actual
        # fallback use, not at startup -- most sessions may never need it),
        # but once loaded it should still be swept up by deep sleep/pause.
        llm_intent.unload()
        print("Dozing off to save memory, sir. Say the word whenever you need me.\n")

    def _ensure_heavy_models_loaded():
        if whisper_model is None:
            print("Waking the heavy circuits back up, one moment...")
            speak("Back at full power, sir.")  # started before loading, not after -- fills the wait rather than adding to it
            _load_heavy_models()

    print("Booting up the ears, sir. One moment...")
    _load_heavy_models()

    print('Tuning my ears for "hey jarvis"...')
    wake_model = WakeWordModel(wakeword_models=[WAKE_WORD], inference_framework="onnx")
    vad = SileroVAD(SAMPLE_RATE)
    # Separate instance for push-to-talk (below) -- SileroVAD carries
    # recurrent state (_h/_c) across calls, so sharing one object between
    # the two independent audio streams would corrupt both.
    hotkey_vad = SileroVAD(SAMPLE_RATE)

    print('All systems nominal. Say "hey jarvis" to open a session -- I\'ll keep listening across')
    print('as many commands as you like until you say "power down". RIGHT CTRL works too, any time.')
    print(f"Idle for {DEEP_SLEEP_IDLE_SECONDS // 60} minutes and I'll unload the heavy models to save RAM")
    print("(mic stays live for the wake word either way -- reloading takes a couple seconds).")
    print(f"Command-line control on {CONTROL_HOST}:{CONTROL_PORT} -- use jarvisctl.py status/pause/resume/quit.\n")
    speak("Online and at your service, sir.")

    audio_q: "queue.Queue[np.ndarray]" = queue.Queue()

    def audio_callback(indata, frames, time_info, status):
        audio_q.put(indata[:, 0].copy())

    def _create_stream():
        s = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=audio_callback,
            blocksize=FRAME_SAMPLES,
        )
        s.start()
        return s

    stream = _create_stream()

    state = {
        "enabled": True,
        "hotkey_active": False,
        "hotkey_frames": [],
        "stop": False,
        "last_activity": time.time(),
        "mode": "wake",
    }

    # Push-to-talk used to open a brand-new sd.InputStream on every single
    # press and tear it down on release -- the same class of Windows
    # cold-open glitch already found and fixed on the *output* side (see
    # tts.py) applies here too: a freshly-opened capture device can drop or
    # corrupt its first chunk(s), which for a short press can be the whole
    # utterance -- plausibly why push-to-talk kept reporting "didn't catch
    # actual speech" even when something was said. Opened once here instead
    # and kept running for the process's life; on_press/on_release below
    # just toggle whether the callback is actually buffering frames.
    def hotkey_callback(indata, frames, time_info, status):
        if state["hotkey_active"]:
            state["hotkey_frames"].append(indata[:, 0].copy())

    hotkey_stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        callback=hotkey_callback,
        blocksize=FRAME_SAMPLES,
    )
    hotkey_stream.start()

    def _set_enabled(enabled: bool):
        """
        A real hardware-level mic on/off -- not just an internal
        processing gate. When off, the main listening stream is fully
        closed (Windows' mic-in-use indicator reflects this correctly).
        Push-to-talk is unaffected either way: it opens its own
        independent stream on demand, so it still works to bring this
        back even with the main stream closed.
        """
        nonlocal stream
        state["enabled"] = enabled
        if enabled:
            if stream is None:
                stream = _create_stream()
            speak("Back online, sir.")
        else:
            if stream is not None:
                stream.stop()
                stream.close()
                stream = None
            _unload_heavy_models()
            # Reset the session state machine too, not just the stream --
            # otherwise pausing mid-conversation and resuming later left
            # the listener stuck in "session_recording" with the models
            # unloaded, crashing on the next transcribe attempt.
            state["mode"] = "wake"
            speak("Going dark, sir.")

    # Push-to-talk's worker thread (below) and the wake-word listener
    # both call into whisper_model/matcher/llm_intent -- serialized here
    # since neither is safe to drive from two threads at once.
    _processing_lock = threading.Lock()

    # Domain vocabulary Whisper otherwise biases away from toward more common
    # words -- evidenced repeatedly in testing: "chrome" kept transcribing as
    # "room"/"groom"/"comb" even when the rest of the same sentence came
    # through cleanly. hotwords boosts recognition of exactly these terms
    # without conditioning on it as literal prior text (unlike initial_prompt).
    # Update this list as new app/action vocabulary gets added (Phase 5's
    # registered-apps table, new query phrasing, etc.) -- covers, in order:
    # apps, desktop navigation, window actions, query phrasing, and the
    # user's actual repo names (for VS Code repo-switching in Phase 5).
    TRANSCRIBE_HOTWORDS = (
        "Chrome Discord Visual Studio Code VS Code File Explorer Settings "
        "local disk D desktop virtual desktop next desktop previous desktop "
        "minimize focus switch open close "
        "how many files folder windows "
        "bdk wallet bdk cli Hashstack djed shu programming bitcoin bitshala"
    )

    def _transcribe(audio: np.ndarray) -> str:
        # Peak-normalize before transcribing -- helps quieter (but fully
        # captured) utterances regardless of how the mic/VAD are tuned.
        peak = np.max(np.abs(audio))
        if peak > 1e-4:
            audio = audio / peak
        segments, _ = whisper_model.transcribe(
            audio,
            language="en",
            beam_size=5,
            condition_on_previous_text=False,
            vad_filter=False,
            hotwords=TRANSCRIBE_HOTWORDS,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    def _handle_transcript(text: str, via_session: bool) -> bool:
        """Returns True if this call should close an open session."""
        state["last_activity"] = time.time()

        if not text:
            print("[heard nothing intelligible]\n")
            return False

        result = matcher.match(text)

        text_lower_words = text.lower().split()
        _has_number = any(c.isdigit() for c in text) or any(w.strip(".,!?") in _NUMBER_WORDS for w in text_lower_words)
        if result.intent and result.intent.startswith("switch_desktop:") and _has_number:
            print(f'Heard: "{text}" -- sounds like a specific desktop number, sir, but I can only do next/previous for now.\n')
            speak("I can only switch to the next or previous desktop for now, sir, not a specific number.")
            return False

        # "disable_listening" is context-dependent: mid-session it just
        # closes the session (wake word opens a new one later); said via
        # push-to-talk with no session open, it's the master mic-off.
        if result.intent == "disable_listening":
            if via_session:
                print(f'Heard: "{text}" -> closing the session, sir. Say the word when you need me again.\n')
                speak("Going quiet, sir.")
                return True
            _set_enabled(False)
            print(f'Heard: "{text}" -> going quiet, sir. Hold the hotkey and say "start listening" to return.\n')
            return False
        if result.intent == "enable_listening":
            _set_enabled(True)
            print(f'Heard: "{text}" -> back online, sir.\n')
            return False

        if not state["enabled"]:
            print(f'Heard: "{text}" -- but I\'m off duty right now, sir.\n')
            speak("I'm off duty right now, sir.")
            return False

        via_llm = False
        parsed = parse_fast_path_intent(result.intent) if result.intent else None

        if parsed is None and result.intent is None:
            # Fast embedding match couldn't confidently place this --
            # fall back to the local LLM before giving up. Only reached
            # for phrasing the seed list doesn't cover, so this is the
            # occasional ~2.5-3s path, not the common case.
            parsed = llm_intent.infer(text)
            via_llm = True

        if parsed is None:
            if result.intent is not None:
                print(f'Heard: "{text}" -> matched "{result.intent}" but nothing wired up for it. My mistake.\n')
            else:
                print(f'Heard: "{text}" -- no match, sir, that\'s outside my current skill set.\n')
                speak("I'm not sure how to do that yet, sir.")
            return False

        if via_llm:
            seed_promotion.log_llm_resolution(text, parsed)
            print(f'Heard: "{text}" -> (local LLM) {parsed}')
        else:
            print(f'Heard: "{text}" -> {result.intent}  (confidence={result.score:.2f})')

        message = dispatch_llm_action(parsed)
        print(f"{message}\n")
        speak(message)
        return False

    # --- push-to-talk: its own independent stream, always available ---
    # regardless of the main listener's state (on, mic-off, or deep
    # asleep) -- this is the one manual control guaranteed to work.
    def on_press(_event):
        if state["hotkey_active"]:
            return
        state["hotkey_frames"] = []
        state["hotkey_active"] = True
        print("[all ears, sir]")

    def on_release(_event):
        if not state["hotkey_active"]:
            return
        state["hotkey_active"] = False
        if not state["hotkey_frames"]:
            print("[dead air -- didn't catch a thing]\n")
            return

        frames = state["hotkey_frames"]
        # Whisper's hotwords bias (TRANSCRIBE_HOTWORDS below) makes it
        # recognize domain vocabulary better, but on near-silent audio it
        # can hallucinate fragments of that same vocabulary as if they'd
        # been spoken -- seen directly: a push-to-talk tap with no real
        # speech transcribed as literal chunks of TRANSCRIBE_HOTWORDS
        # ("how many files folder windows bdk wallet bdk wallet ...").
        # Gate on the same VAD the wake-word session already trusts for
        # this, rather than asking Whisper to recognize its own silence.
        hotkey_vad.reset()
        if not any(hotkey_vad.speech_prob(f) >= VAD_SPEECH_THRESHOLD for f in frames):
            print("[held the key but didn't catch actual speech, sir]\n")
            return
        audio = np.concatenate(frames)

        # keyboard's own hook dispatches on_press/on_release from a single
        # shared processing thread (keyboard/_generic.py's GenericListener),
        # so running transcription+dispatch here inline used to block that
        # thread for the full duration -- up to several seconds for a cold
        # app launch or an LLM fallback. A quick follow-up press-and-release
        # during that window had its on_press (which arms the capture
        # stream) queued behind the still-running on_release, so speech
        # spoken in the gap was genuinely never captured. Offloading the
        # slow part to a worker thread lets on_release return immediately.
        def _process(audio=audio):
            try:
                with _processing_lock:
                    _ensure_heavy_models_loaded()
                    _handle_transcript(_transcribe(audio), via_session=False)
            except Exception:
                traceback.print_exc()
                print("Hit a snag processing that, sir.\n")
                speak("Sorry, sir, I stumbled on that one.")

        threading.Thread(target=_process, daemon=True).start()

    keyboard.on_press_key(HOTKEY, on_press)
    keyboard.on_release_key(HOTKEY, on_release)

    # --- wake word + session listener thread ---
    # Three modes: "wake" (watching only for the wake word),
    # "session_listening" (session open, watching for the next
    # utterance to start), "session_recording" (capturing one
    # utterance until silence ends it).
    def listener_loop():
        # state["mode"] (rather than a local var) so the control server's
        # status command can report it -- "wake" / "session_listening" /
        # "session_recording", matching the guide's state names.
        state["mode"] = "wake"
        command_frames: list[np.ndarray] = []
        silence_run = 0
        frame_count = 0
        session_silence_run = 0
        speech_onset_run = 0
        # Rolling buffer of recent frames kept while waiting for the next
        # utterance to start -- prepended once speech is detected, since
        # VAD needs a moment of evidence before it fires and a hard cut
        # right at detection tends to clip the first word.
        pre_buffer: "deque[np.ndarray]" = deque(maxlen=PRE_BUFFER_FRAMES)

        while not state["stop"]:
            try:
                chunk = audio_q.get(timeout=0.5)
            except queue.Empty:
                continue

            # Push-to-talk has its own stream now -- just don't let the
            # main listener also react to the same speech while it's held.
            # Also mute while Jarvis is talking (is_speaking(), tts.py) --
            # otherwise the session listener, always-on unlike push-to-talk,
            # hears its own spoken replies and acts on them as new commands
            # (confirmed directly: "Switching to the next desktop, sir" kept
            # re-triggering itself, causing runaway desktop switches).
            if state["hotkey_active"] or not state["enabled"] or is_speaking():
                continue

            try:
                if state["mode"] == "wake":
                    if whisper_model is not None and time.time() - state["last_activity"] > DEEP_SLEEP_IDLE_SECONDS:
                        _unload_heavy_models()
                        speak("Easing into low power mode, sir.")

                    chunk_i16 = (chunk * 32767).astype(np.int16)
                    scores = wake_model.predict(chunk_i16)
                    if scores.get(WAKE_WORD, 0.0) > WAKE_THRESHOLD:
                        _ensure_heavy_models_loaded()
                        state["last_activity"] = time.time()
                        print('[session open, sir -- go ahead, say "power down" when you\'re done]')
                        state["mode"] = "session_listening"
                        session_silence_run = 0
                        speech_onset_run = 0
                        pre_buffer.clear()
                        vad.reset()
                        wake_model.reset()
                        # openWakeWord needs a rolling window of audio to
                        # build confidence, so its score crosses threshold
                        # a beat after "hey jarvis" actually finished --
                        # anything said right on its heels with no pause
                        # could start before this mode switch lands, and
                        # come through clipped/garbled (seen directly: "open
                        # chrome for me" -> "or been grown for me"). A short
                        # spoken cue gives an unambiguous "now" to speak
                        # after -- and since is_speaking() already mutes the
                        # listener for its duration, nothing said *during*
                        # the cue is lost either, just deferred.
                        speak("Yes, sir?")

                elif state["mode"] == "session_listening":
                    pre_buffer.append(chunk)
                    prob = vad.speech_prob(chunk)
                    if prob >= VAD_SPEECH_THRESHOLD:
                        session_silence_run = 0
                        speech_onset_run += 1
                        if speech_onset_run >= SPEECH_ONSET_FRAMES:
                            state["mode"] = "session_recording"
                            command_frames = list(pre_buffer)
                            silence_run = 0
                            frame_count = len(command_frames)
                            speech_onset_run = 0
                    else:
                        speech_onset_run = 0
                        session_silence_run += 1
                        if session_silence_run >= SESSION_IDLE_TIMEOUT_FRAMES:
                            print("[quiet for a while there -- closing the session, say the word to reopen it]\n")
                            state["mode"] = "wake"
                            wake_model.reset()

                elif state["mode"] == "session_recording":
                    command_frames.append(chunk)
                    frame_count += 1

                    prob = vad.speech_prob(chunk)
                    silence_run = 0 if prob >= VAD_SPEECH_THRESHOLD else silence_run + 1

                    if silence_run >= SILENCE_HANG_FRAMES or frame_count >= MAX_COMMAND_FRAMES:
                        audio = np.concatenate(command_frames)
                        # Same lock the push-to-talk worker thread uses -- see
                        # its definition above for why.
                        with _processing_lock:
                            _ensure_heavy_models_loaded()  # defense in depth -- see _set_enabled's mode reset for the actual fix
                            text = _transcribe(audio)
                            vad.reset()
                            should_close = _handle_transcript(text, via_session=True)
                        state["mode"] = "wake" if should_close else "session_listening"
                        session_silence_run = 0
                        speech_onset_run = 0
                        pre_buffer.clear()
                        if state["mode"] == "wake":
                            wake_model.reset()
            except Exception:
                # An uncaught exception here used to kill this whole thread
                # silently -- the process kept running and `jarvis status`
                # kept reporting "Wake-word armed", but nothing was actually
                # listening anymore until a full restart (this is exactly
                # how a stray pywintypes error from one bad action call took
                # the whole listener down). Log it, say something, and reset
                # to a known-good state instead of letting the thread die.
                traceback.print_exc()
                print("Hit a snag there, sir -- picking back up.\n")
                speak("Sorry, sir, I stumbled on that one.")
                state["mode"] = "wake"
                session_silence_run = 0
                speech_onset_run = 0
                pre_buffer.clear()
                vad.reset()
                wake_model.reset()

    listener_thread = threading.Thread(target=listener_loop, daemon=True)
    listener_thread.start()

    def _shutdown():
        state["stop"] = True
        if stream is not None:
            stream.stop()
            stream.close()
        hotkey_stream.stop()
        hotkey_stream.close()
        keyboard.unhook_all()

    def _announce_and_shutdown():
        # Blocking, not fire-and-forget -- the process exits right
        # after this, so a background thread would risk never
        # actually getting to play it.
        speak("Powering down. Goodbye, sir.", blocking=True)
        _shutdown()

    # --- command-line control server: jarvisctl.py talks to this ---
    # localhost-only, fixed port; runs on the main thread and doubles
    # as the "keep the process alive" loop.
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((CONTROL_HOST, CONTROL_PORT))
    server.listen(1)
    server.settimeout(0.5)

    try:
        while not state["stop"]:
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                continue
            with conn:
                command = conn.recv(64).decode("utf-8", "ignore").strip().lower()
                if command == "status":
                    if not state["enabled"]:
                        reply = "Paused (mic off)"
                    elif state["mode"] in ("session_listening", "session_recording"):
                        reply = "Conversation open"
                    elif whisper_model is None:
                        reply = "Wake-word armed (deep sleep)"
                    else:
                        reply = "Wake-word armed (awake)"
                elif command == "pause":
                    _set_enabled(False)
                    reply = "going quiet, sir"
                elif command == "resume":
                    _set_enabled(True)
                    reply = "back online, sir"
                elif command == "quit":
                    reply = "powering down, sir"
                    conn.sendall(reply.encode())
                    _announce_and_shutdown()
                    break
                else:
                    reply = f"unrecognized command: {command!r}"
                conn.sendall(reply.encode())
    except KeyboardInterrupt:
        # Only reachable at all when run in a visible console (Ctrl+C
        # has nothing to interrupt under pythonw.exe/Task Scheduler,
        # which has no console) -- still go through the same clean
        # shutdown path as jarvisctl.py quit rather than dying mid-way.
        _announce_and_shutdown()

    server.close()
    print("Powering down. Try not to miss me.")


if __name__ == "__main__":
    main()
