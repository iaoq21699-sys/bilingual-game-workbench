"""Single desktop instance; authenticated JSON-only local launch requests."""
from __future__ import annotations
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import threading

from .patching import save_json


class Instance:
    def __init__(self, data_dir):
        self.directory = Path(data_dir)
        self.file = None
        self.server = None
        self.stop = threading.Event()
        self.metadata = self.directory / "instance.json"
        self.token = None

    def claim(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.file = (self.directory / "instance.lock").open("a+b")
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.file.close()
                self.file = None
                return False
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.server.settimeout(.5)
        self.token = secrets.token_hex(32)
        save_json(self.metadata, dict(port=self.server.getsockname()[1], token=self.token, pid=os.getpid()))
        return True

    def send(self, game_id=None):
        info = json.loads(self.metadata.read_text(encoding="utf-8"))
        request = dict(token=info["token"], type="launch" if game_id else "show", game_id=game_id)
        with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=3) as channel:
            channel.sendall(json.dumps(request).encode() + b"\n")
            if channel.recv(32) != b"OK\n":
                raise RuntimeError("已有工作台未接受请求，请在它的窗口中启动游戏。")

    def start(self, callback):
        def serve():
            while not self.stop.is_set():
                try:
                    channel, _ = self.server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    with channel:
                        channel.settimeout(2)
                        payload = b""
                        while b"\n" not in payload and len(payload) < 4096:
                            block = channel.recv(min(1024, 4096-len(payload)))
                            if not block:
                                break
                            payload += block
                        message = json.loads(payload)
                        if not isinstance(message, dict) or not isinstance(message.get("token"), str):
                            continue
                        if not hmac.compare_digest(message["token"], self.token):
                            continue
                        if message.get("type") not in ("show", "launch"):
                            continue
                        if message["type"] == "launch" and not isinstance(message.get("game_id"), str):
                            continue
                        callback(message)
                        channel.sendall(b"OK\n")
                except (OSError, ValueError, TypeError):
                    continue
        threading.Thread(target=serve, name="bilingual-launch-requests", daemon=True).start()

    def close(self):
        self.stop.set()
        if self.server:
            self.server.close()
        if self.file:
            self.file.close()
            self.file = None
