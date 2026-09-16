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

import pytest

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


class _FakeLlama:
    """
    Stands in for a real Llama object: records every call and returns a
    canned, schema-valid JSON string. `on_call` (if given) runs before
    returning, so a test can block/raise/count from inside a call.
    """

    def __init__(
        self,
        response_json='{"action":"focus_or_launch_app","app":"chrome","direction":"none","repo":"","reply":""}',
        on_call=None,
    ):
        self.response_json = response_json
        self.on_call = on_call
        self.calls = []

    def create_chat_completion(self, messages, grammar, temperature, max_tokens):
        self.calls.append(messages)
        if self.on_call:
            self.on_call()
        return {"choices": [{"message": {"content": self.response_json}}]}


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
