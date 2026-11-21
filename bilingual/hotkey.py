"""A registered Windows hotkey, without hooks or game-process injection."""
from __future__ import annotations

import ctypes
import os
import threading


class GlobalHotkey:
    """Register Ctrl+Shift+B on an owned message thread.

    The callback runs on that thread and must only enqueue UI work.
    """

    def __init__(self, callback):
        self.callback = callback
        self.active = False
        self.error = ""
        self.thread_id = None
        self._ready = threading.Event()
        self._thread = None

    def start(self):
        if os.name != "nt":
            self.error = "此系统不支持 Windows 全局快捷键"
            return False
        self._thread = threading.Thread(target=self._listen, daemon=True, name="bilingual-hotkey")
        self._thread.start()
        self._ready.wait(1)
        return self.active

    def _listen(self):
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self.thread_id = kernel32.GetCurrentThreadId()
        message = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        identifier = 0x4247
        self.active = bool(user32.RegisterHotKey(None, identifier, 0x0002 | 0x0004 | 0x4000, ord("B")))
        if not self.active:
            self.error = "Ctrl+Shift+B 已被占用，使用任务栏恢复窗口"
        self._ready.set()
        if not self.active:
            return
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == 0x0312 and message.wParam == identifier:
                    self.callback()
        finally:
            user32.UnregisterHotKey(None, identifier)
            self.active = False

    def close(self):
        if self.active and self.thread_id:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            from ctypes import wintypes
            user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
