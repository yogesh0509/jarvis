"""
Tests for llm_intent's background-prewarm machinery (prewarm(),
prewarm_state(), call_count(), the _lock serializing it against
infer()) -- deliberately does not import or construct a real
Llama/GGUF model, same strategy as test_llm_intent.py: _get_llm is
monkeypatched to a hand-rolled fake, exploiting the fact that the real
`from llama_cpp import Llama` import lives inside _get_llm's body and
is never reached here.
"""

import threading
import time

import numpy as np
import pytest
from llama_cpp.llama import LlamaState

import llm_intent


@pytest.fixture(autouse=True)
def _reset_llm_intent_state():
    """
    llm_intent's _llm/_grammar/_call_count/_prewarm_state are module-level
    globals, so they'd otherwise leak between tests (and between this
    file and test_llm_intent.py, which imports the same module) --
    unload() already resets exactly this set, so it doubles as the
    fixture's reset call.
    """
    llm_intent.unload()
    yield
    llm_intent.unload()


@pytest.fixture(autouse=True)
def _isolate_kv_cache_path(tmp_path, monkeypatch):
    """
    KV_CACHE_PATH defaults to a real path under this project's own
    logs/ directory -- every test in this file (existing ones included,
    once _FakeLlama grows save_state/load_state below) must be
    physically unable to write there, not merely unlikely to by
    accident of what methods a fake happens to define.
    """
    monkeypatch.setattr(llm_intent, "KV_CACHE_PATH", tmp_path / "llm_kv_cache.pkl")


def _fake_llama_state(n_tokens=4) -> LlamaState:
    """A minimal but real LlamaState -- lightweight to construct, no native backend or model weights touched."""
    return LlamaState(
        input_ids=np.arange(n_tokens, dtype=np.int32),
        scores=np.zeros((1, 8), dtype=np.float32),
        n_tokens=n_tokens,
        llama_state=b"fake-kv-bytes",
        llama_state_size=len(b"fake-kv-bytes"),
        seed=0,
    )


class _FakeLlama:
    """
    Stands in for a real Llama object: records every call and returns a
    canned, schema-valid JSON string. `on_call` (if given) runs before
    returning, so a test can block/raise/count from inside a call.

    save_state/load_state are real enough to exercise
    _save_kv_cache/_load_kv_cache's own logic (pickling, unpickling,
    calling back into this object) without a real GGUF model anywhere
    in the loop -- load_state just records what it was handed, so a
    test can assert on it directly instead of inferring success from
    timing the way the real spike (tools/_spike_kv_cache.py) had to.
    """

    def __init__(
        self,
        response_json='{"action":"focus_or_launch_app","app":"chrome","direction":"none","repo":"","reply":""}',
        on_call=None,
    ):
        self.response_json = response_json
        self.on_call = on_call
        self.calls = []
        self.loaded_states = []

    def create_chat_completion(self, messages, grammar, temperature, max_tokens):
        self.calls.append(messages)
        if self.on_call:
            self.on_call()
        return {"choices": [{"message": {"content": self.response_json}}]}

    def save_state(self):
        return _fake_llama_state()

    def load_state(self, state):
        self.loaded_states.append(state)


def test_prewarm_runs_one_inference_and_marks_warm(monkeypatch):
    fake = _FakeLlama()
    monkeypatch.setattr(llm_intent, "_get_llm", lambda: fake)

    assert llm_intent.prewarm_state() == "cold"
    assert llm_intent.prewarm() is True
    assert llm_intent.prewarm_state() == "warm"
    assert llm_intent.call_count() == 1
    assert len(fake.calls) == 1
    # Warmed with PREWARM_UTTERANCE, not anything user-specific.
    assert fake.calls[0][-1] == {"role": "user", "content": llm_intent.PREWARM_UTTERANCE}


def test_second_prewarm_after_success_is_a_noop(monkeypatch):
    fake = _FakeLlama()
    monkeypatch.setattr(llm_intent, "_get_llm", lambda: fake)

    assert llm_intent.prewarm() is True
    assert llm_intent.prewarm() is True  # already warm -- must not call the fake again
    assert len(fake.calls) == 1
    assert llm_intent.call_count() == 1


def test_prewarm_does_not_propagate_and_marks_failed(monkeypatch):
    def _boom():
        raise RuntimeError("simulated llama.cpp failure")

    fake = _FakeLlama(on_call=_boom)
    monkeypatch.setattr(llm_intent, "_get_llm", lambda: fake)

    # Must not raise -- this is meant to run on a daemon thread with
    # nothing to catch an escaping exception.
    assert llm_intent.prewarm() is False
    assert llm_intent.prewarm_state() == "failed"


def test_infer_works_normally_after_a_failed_prewarm(monkeypatch):
    def _boom():
        raise RuntimeError("simulated llama.cpp failure")

    monkeypatch.setattr(llm_intent, "_get_llm", lambda: _FakeLlama(on_call=_boom))
    assert llm_intent.prewarm() is False

    # A fresh, working fake for the real call -- prewarm failing must
    # not have left anything in a state that blocks infer() afterward.
    monkeypatch.setattr(llm_intent, "_get_llm", lambda: _FakeLlama())
    result = llm_intent.infer("open chrome")
    assert result["action"] == "focus_or_launch_app"
    assert result["app"] == "chrome"


def test_infer_increments_call_count_same_as_prewarm(monkeypatch):
    monkeypatch.setattr(llm_intent, "_get_llm", lambda: _FakeLlama())
    assert llm_intent.call_count() == 0
    llm_intent.infer("open chrome")
    assert llm_intent.call_count() == 1
    llm_intent.infer("minimize chrome")
    assert llm_intent.call_count() == 2


def test_unload_resets_call_count_and_prewarm_state(monkeypatch):
    monkeypatch.setattr(llm_intent, "_get_llm", lambda: _FakeLlama())
    llm_intent.prewarm()
    assert llm_intent.call_count() == 1
    assert llm_intent.prewarm_state() == "warm"

    llm_intent.unload()
    assert llm_intent.call_count() == 0
    assert llm_intent.prewarm_state() == "cold"


def test_prewarm_and_infer_are_mutually_exclusive(monkeypatch):
    """
    The core safety property _lock exists for: prewarm() and infer()
    must never be inside the same Llama call at once. Sequenced with
    two threading.Events rather than sleeps, so this can't pass by luck
    on a slow machine -- the only sleep here is a short one used purely
    to give a would-be bug a chance to manifest, in the direction that
    only produces a false pass under extreme scheduler delay, never a
    false failure.
    """
    order = []
    calls = []
    first_call_entered = threading.Event()
    release_first_call = threading.Event()

    class BlockingFakeLlama:
        def create_chat_completion(self, messages, grammar, temperature, max_tokens):
            index = len(calls)
            calls.append(index)
            order.append(f"call_{index}_start")
            if index == 0:
                first_call_entered.set()
                release_first_call.wait(timeout=5)
            order.append(f"call_{index}_end")
            return {"choices": [{"message": {"content": '{"action":"none","app":"none","direction":"none","repo":"","reply":""}'}}]}

    fake = BlockingFakeLlama()
    monkeypatch.setattr(llm_intent, "_get_llm", lambda: fake)

    prewarm_thread = threading.Thread(target=llm_intent.prewarm)
    prewarm_thread.start()
    assert first_call_entered.wait(timeout=5), "prewarm's call never started"

    infer_thread = threading.Thread(target=lambda: llm_intent.infer("open chrome"))
    infer_thread.start()
    time.sleep(0.3)  # give infer a chance to wrongly proceed if _lock didn't serialize it
    assert order == ["call_0_start"], f"infer's call started before prewarm's released the lock: {order}"

    release_first_call.set()
    prewarm_thread.join(timeout=5)
    infer_thread.join(timeout=5)
    assert not prewarm_thread.is_alive() and not infer_thread.is_alive()
    assert order == ["call_0_start", "call_0_end", "call_1_start", "call_1_end"]


def test_unload_during_inflight_prewarm_does_not_crash(monkeypatch):
    """
    unload() deliberately doesn't take _lock (see its docstring) -- this
    proves the accepted edge case is actually harmless: an unload()
    landing mid-prewarm doesn't raise on either side, and the in-flight
    prewarm still runs to completion against the object it already had.
    """
    entered = threading.Event()
    proceed = threading.Event()
    errors = []

    class SlowFakeLlama:
        def create_chat_completion(self, messages, grammar, temperature, max_tokens):
            entered.set()
            proceed.wait(timeout=5)
            return {"choices": [{"message": {"content": '{"action":"none","app":"none","direction":"none","repo":"","reply":""}'}}]}

    monkeypatch.setattr(llm_intent, "_get_llm", lambda: SlowFakeLlama())

    def _run():
        try:
            llm_intent.prewarm()
        except Exception as exc:  # pragma: no cover -- must never happen
            errors.append(exc)

    prewarm_thread = threading.Thread(target=_run)
    prewarm_thread.start()
    assert entered.wait(timeout=5)

    llm_intent.unload()  # must return immediately, not block on the in-flight call
    assert llm_intent.prewarm_state() == "cold"

    proceed.set()
    prewarm_thread.join(timeout=5)
    assert not prewarm_thread.is_alive()
    assert errors == []


# --- persisted KV cache (tools/_spike_kv_cache.py) ----------------------
#
# Confirmed live, against the real model: loading a saved state on a
# fresh Llama() instance turned a ~20s cold call into ~0.1-0.3s, correct
# both with the state's exact original date/time and one 47 minutes
# stale. These test the logic (fingerprinting, slimming, save/load,
# and prewarm()'s own branching between them) without needing that real
# model in the loop -- _FakeLlama's save_state/load_state are real
# enough to exercise the actual pickle round-trip.


def test_prompt_template_fingerprint_is_deterministic():
    assert llm_intent._prompt_template_fingerprint() == llm_intent._prompt_template_fingerprint()


def test_prompt_template_fingerprint_changes_with_the_template(monkeypatch):
    before = llm_intent._prompt_template_fingerprint()
    monkeypatch.setattr(llm_intent, "SYSTEM_PROMPT_TEMPLATE", llm_intent.SYSTEM_PROMPT_TEMPLATE + " edited")
    after = llm_intent._prompt_template_fingerprint()
    assert before != after


def test_slim_llama_state_shrinks_scores_and_keeps_everything_else():
    state = _fake_llama_state(n_tokens=7)
    slim = llm_intent._slim_llama_state(state)
    assert slim.scores.shape == (1, state.scores.shape[1])
    assert slim.n_tokens == state.n_tokens
    assert slim.llama_state == state.llama_state
    assert slim.llama_state_size == state.llama_state_size
    assert np.array_equal(slim.input_ids, state.input_ids)


def test_save_then_load_kv_cache_round_trips(monkeypatch):
    fake = _FakeLlama()
    assert not llm_intent.KV_CACHE_PATH.exists()

    llm_intent._save_kv_cache(fake)

    assert llm_intent.KV_CACHE_PATH.exists()
    loaded_ok = llm_intent._load_kv_cache(fake)
    assert loaded_ok is True
    assert len(fake.loaded_states) == 1
    assert fake.loaded_states[0].n_tokens == 4  # _fake_llama_state's default


def test_load_kv_cache_on_a_missing_file_returns_false_without_raising():
    assert not llm_intent.KV_CACHE_PATH.exists()
    assert llm_intent._load_kv_cache(_FakeLlama()) is False


def test_load_kv_cache_rejects_a_stale_template_fingerprint(monkeypatch):
    fake = _FakeLlama()
    llm_intent._save_kv_cache(fake)

    # Simulate SYSTEM_PROMPT_TEMPLATE having changed since the cache was
    # written -- the one scenario the real spike never tested, and the
    # one this project's own "never trust a single layer" habit says
    # not to just assume is safe.
    monkeypatch.setattr(llm_intent, "SYSTEM_PROMPT_TEMPLATE", llm_intent.SYSTEM_PROMPT_TEMPLATE + " edited")

    assert llm_intent._load_kv_cache(fake) is False
    assert fake.loaded_states == [], "a stale cache must never reach load_state() at all"


def test_load_kv_cache_on_a_corrupt_file_returns_false_without_raising():
    llm_intent.KV_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    llm_intent.KV_CACHE_PATH.write_bytes(b"not a valid pickle")
    assert llm_intent._load_kv_cache(_FakeLlama()) is False


def test_save_kv_cache_never_raises_even_if_save_state_itself_fails():
    class ExplodingLlama:
        def save_state(self):
            raise RuntimeError("simulated llama.cpp failure")

    llm_intent._save_kv_cache(ExplodingLlama())  # must not raise
    assert not llm_intent.KV_CACHE_PATH.exists()


def test_prewarm_saves_a_cache_on_a_cold_run(monkeypatch):
    fake = _FakeLlama()
    monkeypatch.setattr(llm_intent, "_get_llm", lambda: fake)

    assert llm_intent.prewarm() is True

    assert llm_intent.KV_CACHE_PATH.exists()
    assert len(fake.calls) == 1  # no cache yet -- ran the real inference


def test_prewarm_loads_the_cache_instead_of_running_inference_when_one_exists(monkeypatch):
    fake = _FakeLlama()
    monkeypatch.setattr(llm_intent, "_get_llm", lambda: fake)
    llm_intent.prewarm()  # first run: cold, saves a cache
    llm_intent.unload()  # simulates a fresh process -- resets state, cache file survives on disk

    second_fake = _FakeLlama()
    monkeypatch.setattr(llm_intent, "_get_llm", lambda: second_fake)
    assert llm_intent.prewarm() is True

    assert second_fake.calls == [], "a valid cache exists -- must not run a real inference at all"
    assert len(second_fake.loaded_states) == 1


def test_prewarm_from_cache_reports_call_count_as_already_warm(monkeypatch):
    """
    main.py's llm_cold field (call_count() == 0 right before a call) is
    what a real dispatch after this checks -- loading from cache must
    leave that check reporting "not cold" the same as a real prewarm
    inference would have, or every session's first real fallback would
    be mislabeled cold in the log despite genuinely not paying that cost.
    """
    fake = _FakeLlama()
    monkeypatch.setattr(llm_intent, "_get_llm", lambda: fake)
    llm_intent.prewarm()
    llm_intent.unload()

    assert llm_intent.call_count() == 0
    monkeypatch.setattr(llm_intent, "_get_llm", lambda: _FakeLlama())
    llm_intent.prewarm()

    assert llm_intent.call_count() >= 1


def test_prewarm_falls_back_to_real_inference_when_the_cache_is_corrupt(monkeypatch):
    llm_intent.KV_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    llm_intent.KV_CACHE_PATH.write_bytes(b"garbage, not a pickle")
    fake = _FakeLlama()
    monkeypatch.setattr(llm_intent, "_get_llm", lambda: fake)

    assert llm_intent.prewarm() is True

    assert len(fake.calls) == 1, "corrupt cache must fall back to a real inference, not fail prewarm entirely"
