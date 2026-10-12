"""
Measurement spike: does echo cancellation actually let "hey jarvis"
through over system audio on *this* machine, on *this* output -- and
which way of getting it works best?

Run by hand, once per output device you play media on (switch the
Windows default output between runs -- the script measures whatever
the default is right now), ideally at your normal listening volume and
again louder. Same "kept in the repo as the record of what was learned"
convention as _spike_kv_cache.py. Results print as a table and are also
appended to logs/aec_spike.jsonl, so runs on different outputs can be
compared afterwards.

    .venv\\Scripts\\python.exe tools\\_spike_aec.py           # echo only
    .venv\\Scripts\\python.exe tools\\_spike_aec.py --talk    # you talk over it
    .venv\\Scripts\\python.exe tools\\_spike_aec.py --clip song.wav

Four capture pipelines record the same take, concurrently:

  raw          today's path before echo cancellation: MME default
               input, 16kHz mono, no processing of ours
  raw+aec3     the same stream through echo_canceller.Canceller, fed by
               loopback.py -- what main.py now runs
  wasapi-speech / wasapi-comms
               the mic driver's own processing modes (this machine's
               Intel SST array advertises SPEECH and COMMUNICATIONS,
               via the Waves APO / Intel Voca effect pack), requested
               through the WASAPI stream category -- built-in AEC that
               would need no dependency at all, if it covers the output

Echo only (default): plays a stimulus (Piper saying wake-word-laden
commands, or --clip) and nobody speaks. A pipeline passes on this output
if the playback would NOT have started a session (no VAD onset at main's
threshold), the wake score stays under main.WAKE_NEAR_MISS_FLOOR, and the
transcript is not a command.

Double talk (--talk): the stimulus loops and you say "hey jarvis, pause
the music" when prompted. A pipeline passes if the wake score clears
main.WAKE_THRESHOLD and the transcript has your words in it.

Also reported: the bulk delay between the loopback reference and its
echo in the mic (GCC-PHAT -- Bluetooth adds latency after the loopback
tap, and AEC3's own delay search only reaches about half a second), and
what the canceller costs per 80ms block.

Jarvis can stay running, but pause it first (`jarvis pause`) -- the
stimulus says "hey jarvis".
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import socket
import time

import numpy as np
import sounddevice as sd

import audio_endpoint
import echo_canceller
import loopback

SAMPLE_RATE = 16000
BLOCK = 1280  # main.FRAME_SAMPLES -- openWakeWord's native chunk
VAD_SPEECH_THRESHOLD = 0.15  # main.VAD_SPEECH_THRESHOLD
SPEECH_ONSET_FRAMES = 3  # main.SPEECH_ONSET_FRAMES
WAKE_THRESHOLD = 0.5  # main.WAKE_THRESHOLD
WAKE_NEAR_MISS_FLOOR = 0.2  # main.WAKE_NEAR_MISS_FLOOR
PRE_ROLL_SECONDS = 1.5  # also the noise-floor measurement window
TAIL_SECONDS = 0.6
TALK_SECONDS = 12.0
LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "aec_spike.jsonl"
WHISPER_DIR = Path(__file__).resolve().parent.parent / "models" / "faster-whisper-small.en"
STIMULUS_TEXT = (
    "Hey Jarvis, turn the volume down. "
    "Hey Jarvis, skip this track. "
    "Pause the music. Open Chrome for me."
)


class _RecordingAligner(echo_canceller.DelayAligner):
    """The production aligner, plus a copy of exactly what AEC3 was fed -- mic and held-back reference, frame for frame."""

    def __init__(self):
        super().__init__()
        self.mic_frames = []
        self.reference_frames = []
        self.moves = []

    def record(self, mic_frame, reference_frame, reference_active):
        super().record(mic_frame, reference_frame, reference_active)
        self.mic_frames.append(mic_frame.copy())
        self.reference_frames.append(
            reference_frame if reference_frame is not None else np.zeros_like(mic_frame)
        )

    def maybe_update(self):
        moved = super().maybe_update()
        if moved:
            self.moves.append(round(self.hold_back_ms))
        return moved


class _Take:
    """One pipeline's recording: blocks plus the monotonic time each arrived."""

    def __init__(self, name):
        self.name = name
        self.blocks: "list[np.ndarray]" = []
        self.times: "list[float]" = []
        self.error: "str | None" = None
        self.stream = None

    def add(self, block):
        self.blocks.append(block)
        self.times.append(time.monotonic())

    def window(self, start, end):
        return [b for b, t in zip(self.blocks, self.times) if start <= t <= end]


def _jarvis_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 47123), timeout=0.3):
            return True
    except OSError:
        return False


def _wasapi_default_input() -> int:
    for api in sd.query_hostapis():
        if "WASAPI" in api["name"]:
            return api["default_input_device"]
    raise RuntimeError("no WASAPI host API")


def _open_streams(canceller, takes):
    raw, aec, speech, comms = takes
    cost = []

    def raw_callback(indata, frames, time_info, status):
        try:
            block = indata[:, 0].copy()
            raw.add(block)
            t0 = time.perf_counter()
            aec.add(canceller.process(block))
            cost.append(time.perf_counter() - t0)
        except Exception as exc:  # never let it reach PortAudio
            aec.error = repr(exc)

    raw.stream = sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK, callback=raw_callback
    )

    for take, category in ((speech, sd._lib.eAudioCategorySpeech), (comms, sd._lib.eAudioCategoryCommunications)):
        try:
            settings = sd.WasapiSettings(auto_convert=True)
            # sounddevice doesn't expose the stream category; PortAudio's
            # PaWasapiStreamInfo has it, and the struct is right there.
            settings._streaminfo.streamCategory = category

            def callback(indata, frames, time_info, status, take=take):
                try:
                    take.add(indata[:, 0].copy())
                except Exception:
                    pass

            take.stream = sd.InputStream(
                device=_wasapi_default_input(),
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=BLOCK,
                callback=callback,
                extra_settings=settings,
            )
        except Exception as exc:
            take.error = f"{type(exc).__name__}: {exc}"

    for take in takes:
        if take.stream is not None:
            take.stream.start()
    return cost


def _stimulus(clip: "str | None") -> "tuple[np.ndarray, int]":
    if clip:
        from scipy.io import wavfile

        rate, data = wavfile.read(clip)
        data = data.astype(np.float32)
        if data.ndim > 1:
            data = data.mean(axis=1)
        if np.abs(data).max() > 1.0:
            data /= 32768.0
        return data, rate
    import tts

    voice = tts._get_voice()
    chunks = list(voice.synthesize(STIMULUS_TEXT, syn_config=tts._SYN_CONFIG))
    audio = np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32)
    return audio, chunks[0].sample_rate



def _analyse(take, start, end, whisper):
    from openwakeword.model import Model as WakeWordModel

    from vad import SileroVAD

    if take.error and not take.blocks:
        return {"pipeline": take.name, "error": take.error}
    floor_blocks = take.window(start - PRE_ROLL_SECONDS + 0.5, start)
    blocks = take.window(start, end)
    if not blocks:
        return {"pipeline": take.name, "error": take.error or "no audio captured"}
    audio = np.concatenate(blocks)

    vad, run, onsets, max_prob = SileroVAD(SAMPLE_RATE), 0, 0, 0.0
    wake = WakeWordModel(wakeword_models=["hey_jarvis"], inference_framework="onnx")
    wake_max = 0.0
    for block in blocks:
        prob = vad.speech_prob(block)
        max_prob = max(max_prob, prob)
        run = run + 1 if prob >= VAD_SPEECH_THRESHOLD else 0
        if run == SPEECH_ONSET_FRAMES:
            onsets += 1
        score = wake.predict((np.clip(block, -1, 1) * 32767).astype(np.int16)).get("hey_jarvis", 0.0)
        wake_max = max(wake_max, float(score))

    peak = float(np.abs(audio).max()) or 1.0
    segments, _info = whisper.transcribe(
        audio * min(8.0, 0.9 / peak), language="en", beam_size=5, temperature=0.0,
        condition_on_previous_text=False, vad_filter=False, without_timestamps=True,
    )
    transcript = " ".join(s.text.strip() for s in segments).strip()
    return {
        "pipeline": take.name,
        "floor_dbfs": round(echo_canceller.dbfs(np.concatenate(floor_blocks)), 1) if floor_blocks else None,
        "level_dbfs": round(echo_canceller.dbfs(audio), 1),
        "vad_onsets": onsets,
        "vad_max": round(max_prob, 2),
        "wake_max": round(wake_max, 3),
        "transcript": transcript,
        "error": take.error,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--talk", action="store_true", help="loop the stimulus and talk over it")
    parser.add_argument("--clip", help="WAV file to play instead of the Piper stimulus")
    parser.add_argument("--label", default="", help="free-text note stored with the result (e.g. 'loud')")
    args = parser.parse_args()

    if _jarvis_running():
        print("note: Jarvis is running -- `jarvis pause` it first, the stimulus says \"hey jarvis\".\n")

    endpoint_id, endpoint, form_factor = loopback.default_render_endpoint()
    volume = audio_endpoint.get_output_volume()
    print(f"output: {endpoint} ({loopback.FORM_FACTORS.get(form_factor, form_factor)}), "
          f"volume {round(volume * 100) if volume is not None else '?'}%")

    stimulus, rate = _stimulus(args.clip)
    if args.talk:
        repeats = max(1, int(np.ceil(TALK_SECONDS * rate / len(stimulus))))
        stimulus = np.tile(stimulus, repeats)

    print("loading whisper...")
    from faster_whisper import WhisperModel

    whisper = WhisperModel(str(WHISPER_DIR) if WHISPER_DIR.is_dir() else "small.en", device="cpu", compute_type="int8", cpu_threads=4)

    aligner = _RecordingAligner()
    canceller = echo_canceller.Canceller(echo_canceller._LivekitApm, loopback, aligner=aligner)
    canceller.open()
    loopback.start()
    takes = [_Take("raw"), _Take("raw+aec3"), _Take("wasapi-speech"), _Take("wasapi-comms")]
    cost = _open_streams(canceller, takes)
    try:
        time.sleep(PRE_ROLL_SECONDS)
        if args.talk:
            print("\n>>> in 3 seconds, say: \"hey jarvis, pause the music\" (normal voice, normal distance)")
        start = time.monotonic()
        sd.play(stimulus, rate)
        if args.talk:
            time.sleep(3.0)
            print(">>> now")
        sd.wait()
        end = time.monotonic()
        time.sleep(TAIL_SECONDS)
        # Read while the reference is still flowing -- once loopback
        # stops, coverage() rightly reports "unhealthy".
        coverage = canceller.coverage()
        stats = canceller.stats()
    finally:
        for take in takes:
            if take.stream is not None:
                take.stream.stop()
                take.stream.close()
        loopback.stop()
    canceller.close()

    print("analysing...\n")
    results = [_analyse(take, start, end + TAIL_SECONDS, whisper) for take in takes]

    # What AEC3 was actually handed, frame for frame -- the residual
    # delay it had to find on its own after the aligner's hold-back.
    # (The whole take, not just the playback window: the aligner may
    # have moved partway, and either way the echo is what dominates.)
    residual_ms, delay_strength = echo_canceller.gcc_phat_lag_ms(
        np.concatenate(aligner.mic_frames), np.concatenate(aligner.reference_frames)
    ) if aligner.mic_frames else (float("nan"), 0.0)
    delay_ms = residual_ms + aligner.hold_back_ms

    mode = "double talk" if args.talk else "echo only"
    print(f"=== {mode} · {endpoint} · {Path(args.clip).name if args.clip else 'piper stimulus'} ===")
    print(f"{'pipeline':<15}{'floor':>8}{'level':>8}{'onsets':>8}{'vad max':>9}{'wake max':>10}  verdict  transcript")
    for r in results:
        if "level_dbfs" not in r:
            print(f"{r['pipeline']:<15}  could not record: {r['error']}")
            continue
        if args.talk:
            verdict = "PASS" if r["wake_max"] >= WAKE_THRESHOLD else "FAIL"
        else:
            verdict = "PASS" if r["vad_onsets"] == 0 and r["wake_max"] < WAKE_NEAR_MISS_FLOOR else "FAIL"
        r["verdict"] = verdict
        print(
            f"{r['pipeline']:<15}{r['floor_dbfs'] if r['floor_dbfs'] is not None else '-':>8}{r['level_dbfs']:>8}"
            f"{r['vad_onsets']:>8}{r['vad_max']:>9}{r['wake_max']:>10}  {verdict:<7}  {r['transcript']!r}"
        )
    per_block = 1000 * float(np.mean(cost)) if cost else float("nan")
    print(f"\naec3: coverage {coverage[1]} · residual {stats['residual_dbfs']} dBFS · floor {stats['floor_dbfs']} dBFS "
          f"· {per_block:.2f} ms per 80ms block")
    print(f"echo delay: {delay_ms:.0f} ms total, {residual_ms:.0f} ms left for AEC3 after holding back "
          f"{aligner.hold_back_ms:.0f} ms (peak strength {delay_strength:.1f}; under ~{echo_canceller.DELAY_MIN_STRENGTH:.0f} "
          f"means no clear echo) · hold-back moves: {aligner.moves or 'none'}")
    if not args.talk:
        print("\nverdict: PASS = playback alone would not have opened a session or woken Jarvis.")
    else:
        print("\nverdict: PASS = your \"hey jarvis\" was heard over the playback (check the transcript too).")

    LOG_PATH.parent.mkdir(exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": time.time(),
            "mode": mode,
            "label": args.label,
            "endpoint": endpoint,
            "endpoint_id": endpoint_id,
            "form_factor": loopback.FORM_FACTORS.get(form_factor, form_factor),
            "volume": volume,
            "clip": args.clip,
            "results": results,
            "aec_coverage": coverage[1],
            "aec_stats": stats,
            "aec_ms_per_block": round(per_block, 3),
            "delay_ms": round(delay_ms, 1),
            "residual_delay_ms": round(residual_ms, 1),
            "delay_strength": round(delay_strength, 2),
            "aligner_moves": aligner.moves,
        }) + "\n")
    print(f"(appended to {LOG_PATH})")


if __name__ == "__main__":
    main()
