"""Process-ancestry lookup — the seam behind subtree-aware ownership (issue #23).

``ui.py``'s ownership set only ever records the PID a launcher fixture actually
spawned (``subprocess.Popen(...).pid``). On Windows, ``python`` (and any other
re-exec'ing launcher — ``.cmd``/``.bat`` wrappers, ``npx``, Java launchers,
Electron helper processes) resolves through a shim that re-execs the real
process as a *child*: the window that ends up on screen belongs to that
child's PID, never the PID we launched. Exact-PID ownership can never match
it, so the whole resolve loop times out even though the app is up and fully
visible.

This module answers two closely-related questions — "who is this PID's parent?"
(:func:`parent_pid`, ancestry.py's original job for ``ui.py``) and "which PIDs
descend from this one?" (:func:`descendant_pids`, added for issue #29's
subtree-aware force-kill) — kept deliberately narrow and platform-isolated so
callers can reason about a process tree without knowing how any particular
platform reads it, and so a test can stub these against a fake process tree
instead of a real one. Windows implements both now off a single Toolhelp32
snapshot (stdlib ``ctypes``, no new dependency, no shelling out to
``taskkill``); macOS is Phase 5 (``sysctl``/``libproc`` behind this same seam
— both functions just return the empty answer there today, which callers
already treat as "cannot determine ancestry", not "assume owned").

Both public functions share :func:`_process_table_win32`, which reads the
whole process table into a single ``{pid: parent_pid}`` mapping in one
snapshot. Deriving a descendant set from that one internally-consistent
mapping — rather than re-snapshotting per hop — is what makes the subtree walk
safe against PID reuse racing the enumeration: a PID is only ever treated as a
descendant if it chains up to the root *within the same snapshot*.
"""

from __future__ import annotations

import sys

__all__ = ["parent_pid", "descendant_pids"]

#: Max hops walked when deriving a descendant set, mirroring ``ui.py``'s
#: ``_MAX_ANCESTRY_DEPTH`` discipline: a corrupt or PID-reuse-induced
#: cyclic-looking table can never spin the walk. The ``seen`` set already
#: makes the walk cycle-safe; this is a second, blunt backstop.
_MAX_WALK = 4096


def parent_pid(pid: int) -> int | None:
    """The immediate parent PID of *pid*, or ``None`` if it cannot be determined.

    ``None`` covers every "don't know" case alike — the process has already
    exited, the platform call failed, or this platform has no implementation
    yet — because the caller (``ui.py``'s ancestry walk) treats "don't know"
    as "not an ancestor" and refuses ownership rather than guessing. Never
    raises: any lower-level failure is swallowed and reported as ``None``.
    """
    if sys.platform == "win32":
        table = _process_table_win32()
        return None if table is None else table.get(pid)
    return None  # no ancestry lookup on this platform yet (Phase 5: macOS)


def descendant_pids(pid: int) -> set[int]:
    """Every PID descending from *pid* — children, grandchildren, and deeper.

    Built from a single process-table snapshot so the whole set is internally
    consistent: a PID is included only if it chains up to *pid* within that one
    snapshot, which is what guards the subtree force-kill (issue #29) against
    killing an unrelated PID that briefly reused a descendant's number. Excludes
    *pid* itself — the launched process is killed through its own ``Popen``
    handle, this answers only "what else did it spawn". Never raises and never
    returns ``None``: any lower-level failure (or a platform with no
    implementation yet) yields an empty set, so a best-effort force-kill that
    leans on it simply falls back to killing the launched PID alone rather than
    crashing the finalizer it runs from.
    """
    if sys.platform == "win32":
        table = _process_table_win32()
        if table is None:
            return set()
        return _descendants_from_table(pid, table)
    return set()  # no descendant lookup on this platform yet (Phase 5: macOS)


def _descendants_from_table(pid: int, table: dict[int, int]) -> set[int]:
    """Derive *pid*'s descendant set from a ``{pid: parent_pid}`` mapping.

    Inverts the mapping into parent → children once, then walks outward from
    *pid*. Bounded and cycle-safe in the same spirit as ``ui.py``'s ancestor
    walk: a ``seen`` set means a table made cyclic by PID reuse can never
    revisit a PID, and :data:`_MAX_WALK` caps the total work regardless. Pure
    and platform-independent, so a test can exercise the walk against a fake
    table without a real snapshot.
    """
    children: dict[int, list[int]] = {}
    for child, parent in table.items():
        children.setdefault(parent, []).append(child)

    descendants: set[int] = set()
    seen = {pid}
    stack = [pid]
    steps = 0
    while stack and steps < _MAX_WALK:
        steps += 1
        for child in children.get(stack.pop(), ()):  # type: ignore[arg-type]
            if child in seen:
                continue  # PID reuse can make the table look cyclic; don't loop
            seen.add(child)
            descendants.add(child)
            stack.append(child)
    return descendants


def _process_table_win32() -> dict[int, int] | None:
    """One Toolhelp32 snapshot as a ``{pid: parent_pid}`` mapping, or ``None``.

    ``CreateToolhelp32Snapshot`` + ``Process32First``/``Process32Next`` is the
    documented, stdlib-reachable (``ctypes``) way to read
    ``th32ParentProcessID`` for every running process, without adding
    ``psutil`` or shelling out to ``wmic``/PowerShell. Reading the *whole*
    table in one pass (rather than scanning for a single PID) is what lets
    :func:`parent_pid` and :func:`descendant_pids` share one internally
    consistent snapshot. Best-effort: any ``OSError``/``ctypes`` failure
    returns ``None`` rather than propagating, since a failed read must read as
    "cannot determine", not crash the ownership / teardown checks it backs.
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
            table: dict[int, int] = {}
            while True:
                table[entry.th32ProcessID] = entry.th32ParentProcessID
                if not kernel32.Process32Next(snap, ctypes.byref(entry)):
                    break
            return table
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        return None  # "don't know" — the caller refuses rather than guesses
