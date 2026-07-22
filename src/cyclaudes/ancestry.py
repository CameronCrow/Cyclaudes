"""Process-ancestry lookup — the seam behind subtree-aware ownership (issue #23).

``ui.py``'s ownership set only ever records the PID a launcher fixture actually
spawned (``subprocess.Popen(...).pid``). On Windows, ``python`` (and any other
re-exec'ing launcher — ``.cmd``/``.bat`` wrappers, ``npx``, Java launchers,
Electron helper processes) resolves through a shim that re-execs the real
process as a *child*: the window that ends up on screen belongs to that
child's PID, never the PID we launched. Exact-PID ownership can never match
it, so the whole resolve loop times out even though the app is up and fully
visible.

This module answers exactly one question — "who is this PID's parent?" — kept
deliberately narrow and platform-isolated so ``ui.py`` can walk a PID's
ancestor chain without knowing how any particular platform gets there, and so
a test can stub :func:`parent_pid` against a fake process tree instead of a
real one. Windows implements it now via a Toolhelp32 snapshot (stdlib
``ctypes``, no new dependency); macOS is Phase 5 (``sysctl``/``libproc``
behind this same seam — :func:`parent_pid` just returns ``None`` there today,
which ``ui.py`` already treats as "cannot determine ancestry", not "assume
owned").
"""

from __future__ import annotations

import sys

__all__ = ["parent_pid"]


def parent_pid(pid: int) -> int | None:
    """The immediate parent PID of *pid*, or ``None`` if it cannot be determined.

    ``None`` covers every "don't know" case alike — the process has already
    exited, the platform call failed, or this platform has no implementation
    yet — because the caller (``ui.py``'s ancestry walk) treats "don't know"
    as "not an ancestor" and refuses ownership rather than guessing. Never
    raises: any lower-level failure is swallowed and reported as ``None``.
    """
    if sys.platform == "win32":
        return _parent_pid_win32(pid)
    return None  # no ancestry lookup on this platform yet (Phase 5: macOS)


def _parent_pid_win32(pid: int) -> int | None:
    """Windows implementation: one Toolhelp32 process snapshot, no dependency.

    ``CreateToolhelp32Snapshot`` + ``Process32First``/``Process32Next`` is the
    documented, stdlib-reachable (``ctypes``) way to read
    ``th32ParentProcessID`` for every running process, without adding
    ``psutil`` or shelling out to ``wmic``/PowerShell. Best-effort: any
    ``OSError``/``ctypes`` failure returns ``None`` rather than propagating,
    since a failed lookup must read as "cannot determine", not crash the
    ownership check it backs.
    """
    import ctypes
    from ctypes import wintypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32First.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESSENTRY32),
        ]
        kernel32.Process32First.restype = wintypes.BOOL
        kernel32.Process32Next.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESSENTRY32),
        ]
        kernel32.Process32Next.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap is None or snap == INVALID_HANDLE_VALUE:
            return None
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if not kernel32.Process32First(snap, ctypes.byref(entry)):
                return None
            while True:
                if entry.th32ProcessID == pid:
                    return entry.th32ParentProcessID
                if not kernel32.Process32Next(snap, ctypes.byref(entry)):
                    return None
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        return None  # "don't know" — the caller refuses rather than guesses
