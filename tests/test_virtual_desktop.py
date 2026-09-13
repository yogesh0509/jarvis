"""
Regression test for the COM thread-affinity fix (virtual_desktop.py):
the manager used to be a single global, created on whichever thread
called first -- a raw comtypes pointer used from a second thread
silently returned wrong answers. Now it's thread-local. This confirms
a manager obtained on one (now-dead) thread doesn't poison the result
seen by later threads calling in fresh.
"""

import threading

import win32gui

import virtual_desktop as vd


def test_manager_is_thread_local_not_shared():
    assert not hasattr(vd._thread_local, "manager")
    vd._get_manager()
    assert hasattr(vd._thread_local, "manager")

    result = {}

    def other_thread():
        # A brand-new thread has never touched _thread_local -- must
        # get its own manager, not silently reuse (or choke on) this
        # thread's.
        result["has_manager_before"] = hasattr(vd._thread_local, "manager")
        vd._get_manager()
        result["has_manager_after"] = hasattr(vd._thread_local, "manager")

    t = threading.Thread(target=other_thread)
    t.start()
    t.join()

    assert result["has_manager_before"] is False
    assert result["has_manager_after"] is True


def test_current_desktop_check_agrees_across_fresh_threads():
    # Regression for the actual bug: same window, queried from several
    # independent fresh threads (mirroring push-to-talk spawning a new
    # thread per press) -- and from the main thread -- must all agree.
    hwnd = win32gui.GetForegroundWindow() or win32gui.GetDesktopWindow()

    results = []

    def worker():
        results.append(vd.is_window_on_current_desktop(hwnd))

    for _ in range(3):
        t = threading.Thread(target=worker)
        t.start()
        t.join()

    results.append(vd.is_window_on_current_desktop(hwnd))

    assert len(set(results)) == 1, f"disagreement across threads: {results}"
