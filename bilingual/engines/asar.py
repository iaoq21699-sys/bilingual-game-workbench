"""Bounded, read-only inventory of ordinary Electron ASAR resources.

This reader never extracts files to disk, follows archive links, executes JavaScript,
or changes the package. It is intended for explicitly identified language data.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import struct

from ..importers import _unique_object

MAX_HEADER = 32 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_DEPTH = 48


@dataclass(frozen=True)
class Entry:
    size: int
    offset: int | None


def _signature(stat):
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


class Archive:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.signature = _signature(self.path.stat())
        self.entries: dict[str, Entry] = {}
        self.links_skipped = 0
        self._nodes = 0
        with self.path.open("rb") as stream:
            prefix = stream.read(16)
            if len(prefix) != 16:
                raise ValueError("ASAR 文件头不完整。")
            first, header_size, payload_size, json_size = struct.unpack("<IIII", prefix)
            if (first != 4 or not 8 <= header_size <= MAX_HEADER or
                    payload_size + 4 != header_size or
                    json_size <= 0 or 4 + ((json_size + 3) & ~3) != payload_size):
                raise ValueError("ASAR 文件头格式或大小不受支持。")
            self.data_offset = 8 + header_size
            if self.data_offset > self.signature[0]:
                raise ValueError("ASAR 文件头超过文件范围。")
            raw = stream.read(json_size)
            if len(raw) != json_size:
                raise ValueError("ASAR 目录索引不完整。")
            try:
                header = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            except (ValueError, RecursionError) as exc:
                raise ValueError("ASAR 目录索引无法读取。") from exc
            if not isinstance(header, dict) or not isinstance(header.get("files"), dict):
                raise ValueError("ASAR 缺少有效的文件目录。")
            self._walk(header["files"], (), 0)
        self.check_unchanged()

    def _walk(self, files, parents, depth):
        if depth > MAX_DEPTH:
            raise ValueError("ASAR 文件目录层级过深。")
        for name, node in files.items():
            self._nodes += 1
            if self._nodes > MAX_ENTRIES:
                raise ValueError("ASAR 目录条目过多。")
            if (not isinstance(name, str) or not name or name in (".", "..") or
                    any(c in name for c in ("/", "\\", "\0")) or not isinstance(node, dict)):
                raise ValueError("ASAR 含无效文件路径或目录记录。")
            parts = (*parents, name)
            if "link" in node:
                self.links_skipped += 1
                continue
            if "files" in node:
                if not isinstance(node["files"], dict):
                    raise ValueError("ASAR 目录记录不完整。")
                self._walk(node["files"], parts, depth + 1)
                continue
            size = node.get("size")
            if type(size) is not int or size < 0:
                raise ValueError("ASAR 文件大小无效。")
            offset = None
            if not node.get("unpacked", False):
                value = node.get("offset")
                if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 20:
                    raise ValueError("ASAR 文件偏移无效。")
                offset = int(value)
                if self.data_offset + offset + size > self.signature[0]:
                    raise ValueError("ASAR 文件条目超出包范围。")
            self.entries["/".join(parts)] = Entry(size, offset)

    def check_unchanged(self):
        if _signature(self.path.stat()) != self.signature:
            raise ValueError("语言资源包在读取期间发生更新，请更新完成后重试。")

    def read(self, name, limit=8 * 1024 * 1024):
        """Read only an explicitly selected packed data resource, with a size limit."""
        entry = self.entries.get(name)
        if entry is None or entry.offset is None:
            raise ValueError("ASAR 中没有可直接读取的所选数据文件。")
        if entry.size > limit:
            raise ValueError("所选 ASAR 数据文件超过大小限制。")
        self.check_unchanged()
        with self.path.open("rb") as stream:
            stream.seek(self.data_offset + entry.offset)
            data = stream.read(entry.size)
        self.check_unchanged()
        if len(data) != entry.size:
            raise ValueError("ASAR 数据文件不完整。")
        return data

    def json(self, name, limit=8 * 1024 * 1024):
        try:
            return json.loads(self.read(name, limit).decode("utf-8-sig"),
                              object_pairs_hook=_unique_object)
        except (UnicodeError, RecursionError) as exc:
            raise ValueError("ASAR JSON 语言资源编码或结构不受支持。") from exc
