"""
One-off spike: does IVirtualDesktopManager::MoveWindowToDesktop actually
work on a window NOT owned by this process? Not part of the live
pipeline -- run by hand, same "re-validation tool" convention as
_bench_llm.py. Spawns a real console window, moves it to a different
real desktop, verifies three ways, and moves it back.
"""
import subprocess
import time

import psutil
import win32gui
import win32process

import virtual_desktop as vd


def harvest_desktop_guids():
    """hwnd -> guid for every visible titled window, skipping COMError/null-GUID ones."""
    mgr = vd._get_manager()
    result = {}

    def cb(h, _):
        if not (win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h)):
            return
        try:
            guid = mgr.GetWindowDesktopId(h)
        except Exception:
            return
        if str(guid) == "{00000000-0000-0000-0000-000000000000}":
            return
        result[h] = guid

    win32gui.EnumWindows(cb, None)
    return result


def main():
    mgr = vd._get_manager()

    print("Spawning a throwaway console window...")
    before = set()
    win32gui.EnumWindows(
        lambda h, _: before.add(h) if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) else None, None
    )
    # On this machine cmd.exe spawns as a WindowsTerminal.exe-owned window
    # (Windows Terminal is the default console host on Win11) -- match by
    # whichever new visible window appeared, not by the spawned PID, since
    # the PID that actually owns the hwnd is Windows Terminal's, not cmd's.
    # This is actually a BETTER test case than a bare cmd.exe would have
    # been: a real, unrelated third-party process we don't own.
    proc = subprocess.Popen("cmd.exe /k echo spike-marker", creationflags=subprocess.CREATE_NEW_CONSOLE)
    hwnd = None
    for _ in range(40):
        time.sleep(0.1)
        after = []
        win32gui.EnumWindows(
            lambda h, _: after.append(h) if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) else None, None
        )
        new_windows = [h for h in after if h not in before and "cmd.exe" in win32gui.GetWindowText(h).lower()]
        if new_windows:
            hwnd = new_windows[0]
            break
    if not hwnd:
        print("FAILED to find the spawned console window -- aborting, nothing was touched.")
        proc.kill()
        return
    owner_pid = win32process.GetWindowThreadProcessId(hwnd)[1]
    owner_exe = psutil.Process(owner_pid).name()
    print(f"Window is owned by {owner_exe} (pid={owner_pid}), NOT the spawned cmd.exe (pid={proc.pid}) -- a genuine third-party window.")
    print(f"Found console hwnd={hwnd}")

    try:
        origin_guid = mgr.GetWindowDesktopId(hwnd)
    except Exception as e:
        print(f"FAILED: GetWindowDesktopId on our own spawned window raised {e!r} -- aborting.")
        proc.kill()
        return
    print(f"Origin desktop GUID: {origin_guid}")

    guids = harvest_desktop_guids()
    other_guid = next((g for h, g in guids.items() if str(g) != str(origin_guid)), None)
    if other_guid is None:
        print("No second desktop with a harvestable window found -- cannot run the move test. Aborting.")
        proc.kill()
        return
    print(f"Target desktop GUID (harvested from an existing window): {other_guid}")

    print("\n--- Attempting MoveWindowToDesktop ---")
    try:
        mgr.MoveWindowToDesktop(hwnd, other_guid)
        print("MoveWindowToDesktop returned without raising (S_OK).")
    except Exception as e:
        print(f"RESULT: RED -- MoveWindowToDesktop raised {type(e).__name__}: {e}")
        proc.kill()
        return

    time.sleep(0.3)
    new_guid = mgr.GetWindowDesktopId(hwnd)
    on_current = mgr.IsWindowOnCurrentVirtualDesktop(hwnd)
    print(f"After move: GetWindowDesktopId={new_guid}  IsWindowOnCurrentVirtualDesktop={bool(on_current)}")

    moved_ok = str(new_guid) == str(other_guid)
    print(f"\nVerification: GUID changed to target = {moved_ok}")

    print("\nMoving it back to origin...")
    try:
        mgr.MoveWindowToDesktop(hwnd, origin_guid)
        time.sleep(0.2)
        back_guid = mgr.GetWindowDesktopId(hwnd)
        print(f"After move-back: GetWindowDesktopId={back_guid}  (should equal origin {origin_guid})")
    except Exception as e:
        print(f"Move-back raised {e!r} -- window may be stranded on the other desktop, check manually.")

    print("\nKilling the spike console...")
    proc.kill()

    print(f"\nRESULT: {'GREEN' if moved_ok else 'RED (S_OK but no-op -- worse than an error)'}")


if __name__ == "__main__":
    main()
