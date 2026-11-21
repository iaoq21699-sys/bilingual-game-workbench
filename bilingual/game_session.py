"""Observe an exact Windows game executable and return focus to its window.

This module only queries process metadata and top-level windows. It does not
read process memory, inject code, hook a renderer, launch a process, or assume
that a launcher's children belong to the game.

UI integration:
    session = GameSession(actual_game_executable)
    session.capture_foreground()  # Immediately before showing the panel.
    # On panel hide, while handling that user's hotkey:
    session.restore_focus()       # False means no verified window/focus denied.

The configured executable must be the renderer/game executable, not a store or
bootstrap launcher. set_process(launcher_pid) intentionally refuses a different
image path. A separately launched game is discovered by its exact image path.

Coordinates are Win32 screen coordinates in the calling thread's DPI context.
This module never changes the application's DPI awareness. An ordinary desktop
panel cannot promise to render over exclusive fullscreen; use borderless or
windowed mode. Foreground activation remains subject to Windows restrictions.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import threading


def _canonical(path) -> str:
    value = os.path.expandvars(os.path.expanduser(os.fspath(path)))
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    try:
        value = str(Path(value).resolve())
    except OSError:
        value = os.path.abspath(value)
    return os.path.normcase(os.path.normpath(value))


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _rect(value):
    return (int(value.left), int(value.top), int(value.right), int(value.bottom))


class _Windows:
    """All pointer-sized API signatures are explicit for 32/64-bit Python."""

    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        signatures = (
            (self.kernel, "OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            (self.kernel, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
            (self.kernel, "QueryFullProcessImageNameW", [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            (self.kernel, "GetProcessTimes", [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)], wintypes.BOOL),
            (self.kernel, "K32EnumProcesses", [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            (self.user, "EnumWindows", [self.callback_type, wintypes.LPARAM], wintypes.BOOL),
            (self.user, "GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            (self.user, "GetForegroundWindow", [], wintypes.HWND),
            (self.user, "GetWindow", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
            (self.user, "IsWindow", [wintypes.HWND], wintypes.BOOL),
            (self.user, "IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
            (self.user, "IsIconic", [wintypes.HWND], wintypes.BOOL),
            (self.user, "GetWindowRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            (self.user, "GetClientRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            (self.user, "ClientToScreen", [wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            (self.user, "GetWindowTextW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.user, "MonitorFromWindow", [wintypes.HWND, wintypes.DWORD], wintypes.HANDLE),
            (self.user, "GetMonitorInfoW", [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)], wintypes.BOOL),
            (self.user, "ShowWindowAsync", [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            (self.user, "SetForegroundWindow", [wintypes.HWND], wintypes.BOOL),
        )
        for library, name, args, result in signatures:
            function = getattr(library, name)
            function.argtypes, function.restype = args, result
        self.window_style = getattr(self.user, "GetWindowLongPtrW", self.user.GetWindowLongW)
        self.window_style.argtypes = [wintypes.HWND, ctypes.c_int]
        self.window_style.restype = ctypes.c_ssize_t

    def image(self, pid):
        handle = self.kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if self.kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                return _canonical(buffer.value)
            return None
        finally:
            self.kernel.CloseHandle(handle)

    def pids(self):
        capacity = 1024
        while capacity <= 65536:
            buffer = (wintypes.DWORD * capacity)()
            used = wintypes.DWORD()
            if not self.kernel.K32EnumProcesses(buffer, ctypes.sizeof(buffer), ctypes.byref(used)):
                return []
            if used.value < ctypes.sizeof(buffer):
                return list(buffer[:used.value // ctypes.sizeof(wintypes.DWORD)])
            capacity *= 2
        return []

    def identity(self, pid, expected_image):
        """Query image and creation time on one handle, avoiding PID-reuse races."""
        handle = self.kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                return None
            image = _canonical(buffer.value)
            if image != expected_image:
                return None
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not self.kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
                return None
            started = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
            if not started:
                return None
            return {"pid": int(pid), "exe_path": image, "process_start_filetime": str(started)}
        finally:
            self.kernel.CloseHandle(handle)

    def pid(self, hwnd):
        pid = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def windows(self):
        result = []

        @self.callback_type
        def collect(hwnd, _):
            result.append(int(hwnd))
            return True

        self.user.EnumWindows(collect, 0)
        return result


class GameSession:
    """Path-verified game process/window tracking; methods are thread-safe.

    No background thread is created. Poll window_info() from the application as
    needed (typically 0.5–1 second). is_running() also covers a game that has not
    created a visible window yet. Access-denied processes remain unverified.
    """

    def __init__(self, exe_path):
        if not os.fspath(exe_path).strip():
            raise ValueError("需要实际游戏可执行文件路径。")
        self.exe_path = _canonical(exe_path)
        self.supported = os.name == "nt"
        self.last_error = "" if self.supported else "游戏窗口跟踪目前仅支持 Windows。"
        self._api = _Windows() if self.supported else None
        self._lock = threading.RLock()
        self._preferred_pid = None
        self._last_window = None  # (HWND, PID), revalidated before every use.

    def _matches_pid(self, pid):
        return bool(self._api and isinstance(pid, int) and 0 < pid <= 0xFFFFFFFF
                    and self._api.image(pid) == self.exe_path)

    def _verified_window(self, hwnd, expected_pid=None):
        if not self._api or not hwnd or not self._api.user.IsWindow(hwnd):
            return False
        pid = self._api.pid(hwnd)
        return (expected_pid is None or pid == expected_pid) and self._matches_pid(pid)

    def set_process(self, pid) -> bool:
        """Prefer a launched PID only if its image exactly matches exe_path.

        Rejects launcher PIDs, dead PIDs, inaccessible PIDs, and invalid values.
        Other exact-path instances can still be discovered after this PID exits.
        """
        with self._lock:
            if isinstance(pid, bool) or not self._matches_pid(pid):
                self.last_error = "进程路径与所配置游戏不匹配，或进程已结束/无法验证。"
                return False
            self._preferred_pid = pid
            self.last_error = ""
            return True

    def process_ids(self) -> list[int]:
        with self._lock:
            if not self._api:
                return []
            matches = [pid for pid in self._api.pids() if self._matches_pid(pid)]
            if self._preferred_pid in matches:
                matches.remove(self._preferred_pid)
                matches.insert(0, self._preferred_pid)
            return matches

    def is_running(self) -> bool:
        with self._lock:
            if self._preferred_pid and self._matches_pid(self._preferred_pid):
                return True
            return bool(self.process_ids())

    def process_identity(self, pid=None):
        """Return exact image/PID/creation FILETIME, or None; never starts a game.

        A remembered PID is re-queried every time. Without one, at most one PID
        inventory is taken. Creation FILETIME distinguishes a reused PID even
        when the new process runs the same executable.
        """
        with self._lock:
            if not self._api:
                return None
            if pid is not None:
                if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= 0xFFFFFFFF:
                    return None
                return self._api.identity(pid, self.exe_path)
            if self._preferred_pid:
                identity = self._api.identity(self._preferred_pid, self.exe_path)
                if identity:
                    return identity
            for candidate in self._api.pids():
                identity = self._api.identity(candidate, self.exe_path)
                if identity:
                    self._preferred_pid = candidate
                    return identity
            return None

    def _find_window(self):
        if not self._api:
            return None
        if self._last_window and self._verified_window(*self._last_window):
            hwnd = self._last_window[0]
            if self._api.user.IsWindowVisible(hwnd):
                return hwnd
        foreground = self._api.user.GetForegroundWindow()
        if self._verified_window(foreground):
            return int(foreground)
        candidates, image_cache = [], {}
        for hwnd in self._api.windows():
            if not self._api.user.IsWindowVisible(hwnd):
                continue
            if self._api.user.GetWindow(hwnd, 4):  # GW_OWNER: exclude owned auxiliaries.
                continue
            if self._api.window_style(hwnd, -20) & 0x80:  # WS_EX_TOOLWINDOW
                continue
            pid = self._api.pid(hwnd)
            if pid not in image_cache:
                image_cache[pid] = self._matches_pid(pid)
            if not image_cache[pid]:
                continue
            bounds = wintypes.RECT()
            if not self._api.user.GetWindowRect(hwnd, ctypes.byref(bounds)):
                continue
            area = max(0, bounds.right - bounds.left) * max(0, bounds.bottom - bounds.top)
            if area:
                candidates.append((pid == self._preferred_pid, area, hwnd))
        return max(candidates)[2] if candidates else None

    @property
    def hwnd(self):
        """Current verified game HWND, or None. It is never a launcher fallback."""
        with self._lock:
            return self._find_window()

    def is_foreground(self) -> bool:
        with self._lock:
            return bool(self._api and self._verified_window(self._api.user.GetForegroundWindow()))

    def capture_foreground(self) -> bool:
        """Remember the foreground only when it belongs to this exact game.

        Calling this while the panel/desktop/another application is in front
        leaves the last verified game window unchanged.
        """
        with self._lock:
            if not self._api:
                return False
            hwnd = self._api.user.GetForegroundWindow()
            if not self._verified_window(hwnd):
                return False
            self._last_window = (int(hwnd), self._api.pid(hwnd))
            self.last_error = ""
            return True

    def window_info(self):
        """Return HWND/PID/title and rectangles, or None when not verified.

        rect: outer window; client_rect: content area in screen coordinates;
        screen_rect: monitor bounds; work_area: monitor minus taskbar. Rectangles
        are (left, top, right, bottom), possibly negative on secondary monitors.
        No field claims to detect exclusive fullscreen.
        """
        with self._lock:
            hwnd = self._find_window()
            if not hwnd or not self._verified_window(hwnd):
                return None
            api = self._api
            bounds = wintypes.RECT()
            if not api.user.GetWindowRect(hwnd, ctypes.byref(bounds)):
                return None
            client = wintypes.RECT()
            client_bounds = None
            if api.user.GetClientRect(hwnd, ctypes.byref(client)):
                origin, end = wintypes.POINT(client.left, client.top), wintypes.POINT(client.right, client.bottom)
                if api.user.ClientToScreen(hwnd, ctypes.byref(origin)) and api.user.ClientToScreen(hwnd, ctypes.byref(end)):
                    client_bounds = (int(origin.x), int(origin.y), int(end.x), int(end.y))
            monitor = api.user.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
            monitor_info = _MonitorInfo()
            monitor_info.cbSize = ctypes.sizeof(monitor_info)
            has_monitor = bool(monitor and api.user.GetMonitorInfoW(monitor, ctypes.byref(monitor_info)))
            title = ctypes.create_unicode_buffer(1024)
            api.user.GetWindowTextW(hwnd, title, len(title))
            return dict(hwnd=int(hwnd), pid=api.pid(hwnd), title=title.value,
                        exe_path=self.exe_path, rect=_rect(bounds), client_rect=client_bounds,
                        screen_rect=_rect(monitor_info.rcMonitor) if has_monitor else None,
                        work_area=_rect(monitor_info.rcWork) if has_monitor else None,
                        minimized=bool(api.user.IsIconic(hwnd)),
                        foreground=int(api.user.GetForegroundWindow() or 0) == int(hwnd))

    def window_rect(self):
        info = self.window_info()
        return info["rect"] if info else None

    def screen_rect(self):
        info = self.window_info()
        return info["screen_rect"] if info else None

    def restore_focus(self) -> bool:
        """Return focus only to a currently path-verified game window.

        No desktop/last-app/launcher fallback and no foreground-lock bypass.
        False is a normal result when the game exits or Windows denies focus.
        """
        with self._lock:
            hwnd = self._find_window()
            if not hwnd or not self._verified_window(hwnd):
                self.last_error = "未找到已验证的游戏窗口；未切换到其他应用。"
                return False
            if self._api.user.IsIconic(hwnd):
                self._api.user.ShowWindowAsync(hwnd, 9)  # SW_RESTORE, no blocking SendMessage.
            # Revalidate after the restore request in case the process exited.
            if not self._verified_window(hwnd):
                self.last_error = "游戏窗口已关闭，无法返回。"
                return False
            restored = bool(self._api.user.SetForegroundWindow(hwnd))
            if restored:
                self._last_window = (hwnd, self._api.pid(hwnd))
                self.last_error = ""
            else:
                self.last_error = "Windows 未允许切回游戏窗口，请点击游戏或使用 Alt+Tab。"
            return restored

    def bring_to_front(self) -> bool:
        return self.restore_focus()


def _self_test():
    """Read-only live-process checks; no game launch or focus changes."""
    import json
    import sys

    session = GameSession(sys.executable)
    if not session.supported:
        print(json.dumps({"skipped": "Windows only"}))
        return
    results = {}
    results["own_pid_verified"] = session.set_process(os.getpid())
    results["own_process_found"] = os.getpid() in session.process_ids()
    results["own_process_running"] = session.is_running()
    results["invalid_pid_rejected"] = not session.set_process(0)
    results["boolean_pid_rejected"] = not session.set_process(True)
    wrong = GameSession(Path(sys.executable).with_name("__not_this_game__.exe"))
    results["wrong_executable_rejected"] = not wrong.set_process(os.getpid())
    results["wrong_executable_has_no_window"] = wrong.window_info() is None
    results["wrong_executable_is_not_foreground"] = not wrong.capture_foreground()
    print(json.dumps(results, ensure_ascii=False))
    if not all(results.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--self-test"]:
        _self_test()
    else:
        raise SystemExit("Usage: python -m bilingual.game_session --self-test")
