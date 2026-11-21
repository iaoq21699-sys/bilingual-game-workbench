"""Chromium page debugging over handles inherited by the game we start."""
from __future__ import annotations

from collections import deque
from contextlib import ExitStack
import json
import os
from queue import Empty, Full, Queue
import subprocess
import threading
import time


_MAX_MESSAGE = 12 * 1024 * 1024


class PipeClosed(RuntimeError):
    pass


class ProtocolError(RuntimeError):
    pass


class ElectronPipe:
    def __init__(self, executable, root):
        import msvcrt

        self._inbox = Queue(maxsize=256)
        self._events = deque(maxlen=128)
        self._serial = 0
        self._stop = threading.Event()
        with ExitStack() as cleanup:
            child_in, self._output = os.pipe()
            for fd in (child_in, self._output):
                cleanup.callback(os.close, fd)
            self._input, child_out = os.pipe()
            for fd in (self._input, child_out):
                cleanup.callback(os.close, fd)
            handles = [msvcrt.get_osfhandle(fd) for fd in (child_in, child_out)]
            for handle in handles:
                os.set_handle_inheritable(handle, True)
            startup = subprocess.STARTUPINFO(lpAttributeList={"handle_list": handles})
            self.process = subprocess.Popen(
                [str(executable), "--remote-debugging-pipe", f"--remote-debugging-io-pipes={handles[0]},{handles[1]}"],
                cwd=root, close_fds=True, startupinfo=startup,
            )
            cleanup.pop_all()
        os.close(child_in)
        os.close(child_out)
        threading.Thread(target=self._read, name="electron-page-pipe", daemon=True).start()

    def _put(self, message):
        # A healthy renderer issues few requests per second. Do not buffer an
        # arbitrary number of messages if it floods or the consumer is busy.
        while not self._stop.is_set():
            try:
                self._inbox.put(message, timeout=0.25)
                return
            except Full:
                continue

    def _read(self):
        pending = bytearray()
        try:
            while not self._stop.is_set():
                chunk = os.read(self._input, 65536)
                if not chunk:
                    break
                pending.extend(chunk)
                while (end := pending.find(0)) != -1:
                    if end > _MAX_MESSAGE:
                        raise ValueError("Electron 页面消息过大。")
                    message = json.loads(pending[:end])
                    del pending[:end + 1]
                    if "id" in message or message.get("method") == "Runtime.bindingCalled":
                        self._put(message)
                if len(pending) > _MAX_MESSAGE:
                    raise ValueError("Electron 页面消息过大。")
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        finally:
            os.close(self._input)
            self._put(None)

    def _receive(self, timeout):
        try:
            message = self._inbox.get(timeout=timeout)
        except Empty:
            if self.process.poll() is not None:
                raise PipeClosed(f"游戏已退出（{self.process.returncode}）。") from None
            raise TimeoutError("等待 Electron 页面响应超时。") from None
        if message is None:
            raise PipeClosed("Electron 页面管道已断开。")
        return message

    def call(self, method, params=None, session=None, timeout=5):
        self._serial += 1
        message = dict(id=self._serial, method=method, params=params or {})
        if session is not None:
            message["sessionId"] = session
        payload = memoryview(json.dumps(message, separators=(",", ":")).encode("ascii") + b"\0")
        if len(payload) > _MAX_MESSAGE:
            raise ValueError("游戏页面的双语数据超过加载上限。")
        try:
            while payload:
                payload = payload[os.write(self._output, payload):]
        except OSError as exc:
            raise PipeClosed("Electron 页面管道已断开。") from exc
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            incoming = self._receive(remaining)
            if incoming.get("id") == self._serial:
                if "error" in incoming:
                    raise ProtocolError(incoming["error"].get("message", "Electron 页面命令失败。"))
                return incoming.get("result", {})
            if incoming.get("method") == "Runtime.bindingCalled":
                self._events.append(incoming)
        raise TimeoutError("等待 Electron 页面响应超时。")

    def next_event(self, timeout):
        if self._events:
            return self._events.popleft()
        try:
            message = self._receive(timeout)
        except TimeoutError:
            return None
        return message if message.get("method") == "Runtime.bindingCalled" else None

    def close(self):
        self._stop.set()
        os.close(self._output)
