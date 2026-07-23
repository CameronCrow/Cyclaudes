"""Cheap, enumeration-free window queries — the ctypes seam behind issue #36.

Window *resolution* has to go through touchpoint's UIA enumeration
(``touchpoint.windows()``), which is ~8s on a busy desktop (worse — ~30s — when
a UI-thread-blocked app like Logix Designer is open), because each top-level
window costs cross-process UIA property reads. But two questions ``ui.py`` asks a
lot do **not** need UIA at all, only raw Win32:

- **"Is *this* window still alive?"** — the WindowGone-vs-EmptyTree decision on an
  empty scoped read. A ``user32.IsWindow`` + ``GetWindowThreadProcessId`` check is
  sub-millisecond and needs no enumeration; the old path re-walked every
  top-level window just to notice ours had vanished, so a settle loop waiting on
  a gone/empty tree paid the full ~8s enumeration *every poll*.
- **"Does any owned process own a visible top-level window yet?"** — the launch
  gate. A pure-``ctypes`` ``EnumWindows`` sweep of visible windows' PIDs is ~1ms,
  so the ``app_session`` launch-wait can cheaply decide "not up yet" and skip the
  expensive ``owned_window()`` UIA resolve until a window has actually appeared,
  instead of enumerating on every 0.25s poll.

Kept platform-isolated behind this seam in the same spirit as
:mod:`cyclaudes.ancestry`: Windows implements both off stdlib ``ctypes`` (no new
dependency, no shelling out); every other platform returns the "cannot determine"
sentinel (``None``), which callers treat as "fall back to the enumeration path" —
never as a definite answer. Neither function raises: any lower-level failure is
swallowed and reported as ``None`` so a performance shortcut can only ever *fail
open* to the correct-but-slower path, never crash the check it backs. (macOS is
Phase 5: the same two answers off ``CoreGraphics``/AX behind this seam.)
"""

from __future__ import annotations

import sys

__all__ = ["window_is_live", "visible_window_pids"]


def window_is_live(hwnd, expect_pid: int | None = None) -> bool | None:
    """Whether ``hwnd`` is still a live window (optionally owned by ``expect_pid``).

    Returns ``True``/``False`` on Windows when it can be decided, and ``None``
    when it cannot be determined cheaply — ``hwnd`` is unknown (``None``), this
    is not Windows, or the ``ctypes`` call failed — so the caller falls back to
    the enumeration-based liveness check rather than trusting a guess.

    When ``expect_pid`` is given, a live ``hwnd`` whose owning PID no longer
    matches counts as **not** live: an HWND can be recycled to a different
    process after the original window is destroyed, and that must read as
    "our window is gone", not "a window with this handle exists".
    """
    if sys.platform != "win32" or hwnd is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL

        h = wintypes.HWND(int(hwnd))
        if not user32.IsWindow(h):
            return False
        if expect_pid is None:
            return True

        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value == 0:
            return False  # destroyed between the IsWindow check and here
        return pid.value == int(expect_pid)
    except Exception:
        return None  # "don't know" — caller falls back to enumeration


def visible_window_pids() -> set[int] | None:
    """PIDs owning any *visible* top-level window, via a pure-``ctypes`` sweep.

    ``None`` means the set could not be determined cheaply (non-Windows, or the
    ``ctypes`` call failed) — the launch gate treats that as "can't tell, do the
    real resolve", so it can only ever skip provably-unnecessary work, never
    hide a window that is actually ready. Visible-only (``IsWindowVisible``) so a
    process's invisible message-only/helper windows don't read as "its UI is up".
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        pids: set[int] = set()

        def _collect(hwnd, _lparam):
            try:
                if user32.IsWindowVisible(hwnd):
                    pid = wintypes.DWORD(0)
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value:
                        pids.add(pid.value)
            except Exception:
                pass  # skip a single bad window; keep sweeping
            return True  # keep enumerating

        if not user32.EnumWindows(WNDENUMPROC(_collect), 0):
            # EnumWindows returns 0 only on a real failure here (our callback
            # always returns TRUE). Return what we gathered if anything, else
            # signal "couldn't determine".
            return pids or None
        return pids
    except Exception:
        return None
