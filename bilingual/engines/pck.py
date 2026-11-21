"""Bounded, read-only Godot PCK v2/v3 access and small v2 overlay writing.

Format reference: Godot 4.5 core/io/file_access_pack.cpp. Encryption and sparse
bundles deliberately fail closed; this module never rewrites the source pack.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import struct


MAGIC = b"GDPC"
MAX_ENTRY = 128 * 1024 * 1024


def resource_path(value: str) -> str:
    value = value.removeprefix("res://")
    parts = PurePosixPath(value)
    if (not value or value != parts.as_posix() or parts.is_absolute() or
            any(part in {".", ".."} for part in parts.parts) or
            any(char in value for char in "\\:\0")):
        raise ValueError(f"不安全的 PCK 资源路径：{value!r}")
    return value


@dataclass(frozen=True)
class Entry:
    offset: int
    size: int
    md5: bytes
    flags: int


class Archive:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.size = self.path.stat().st_size
        self.entries: dict[str, Entry] = {}
        with self.path.open("rb") as file:
            def take(size):
                value = file.read(size)
                if len(value) != size:
                    raise ValueError("PCK 文件已截断。")
                return value

            if take(4) != MAGIC:
                raise ValueError("不是独立 Godot PCK 文件。")
            self.version, major, minor, patch, flags = struct.unpack("<5I", take(20))
            self.engine_version = (major, minor, patch)
            if self.version not in (2, 3):
                raise ValueError(f"暂不支持 PCK v{self.version}，仅支持 v2/v3。")
            if flags & ~2:
                raise ValueError("加密或稀疏 PCK 暂不支持；不会尝试绕过。")
            file_base = struct.unpack("<Q", take(8))[0]
            if self.version == 3:
                directory = struct.unpack("<Q", take(8))[0]
                if not 40 <= directory < self.size:
                    raise ValueError("PCK 目录位置越界。")
                file.seek(directory)
            else:
                take(64)
            count = struct.unpack("<I", take(4))[0]
            if count > min(1_000_000, self.size // 40):
                raise ValueError("PCK 资源数量无效。")
            for _ in range(count):
                length = struct.unpack("<I", take(4))[0]
                if not 0 < length <= 65536:
                    raise ValueError("PCK 路径长度无效。")
                raw_path = take(length).decode("utf-8").rstrip("\0")
                name = resource_path(raw_path)
                offset, size = struct.unpack("<QQ", take(16))
                digest = take(16)
                entry_flags = struct.unpack("<I", take(4))[0]
                if entry_flags & ~3:
                    raise ValueError(f"不支持的 PCK 条目标记：{name}")
                if entry_flags & 2:
                    continue
                absolute = file_base + offset
                if absolute > self.size or size > self.size - absolute:
                    raise ValueError(f"PCK 资源数据越界：{name}")
                if name in self.entries:
                    raise ValueError(f"PCK 含重复资源路径：{name}")
                self.entries[name] = Entry(absolute, size, digest, entry_flags)

    def read(self, name: str) -> bytes:
        name = resource_path(name)
        entry = self.entries[name]
        if entry.flags & 1:
            raise ValueError(f"资源已加密，无法读取：{name}")
        if entry.size > MAX_ENTRY:
            raise ValueError(f"单个文本资源超过 128 MB：{name}")
        with self.path.open("rb") as file:
            file.seek(entry.offset)
            value = file.read(entry.size)
        if len(value) != entry.size or hashlib.md5(value).digest() != entry.md5:
            raise ValueError(f"PCK 资源校验失败：{name}")
        return value


def write(path, files: dict[str, bytes], engine_version=(4, 0, 0)) -> None:
    """Write a new overlay (never an in-place edit), then verify every entry."""
    if not files:
        raise ValueError("没有可写入 PCK 的资源。")
    prepared = []
    for name, content in sorted(files.items()):
        name = resource_path(name)
        if not isinstance(content, bytes) or len(content) > MAX_ENTRY:
            raise ValueError(f"PCK 资源不是字节或超出限制：{name}")
        encoded = name.encode("utf-8")
        encoded += b"\0" * (-len(encoded) % 4)
        prepared.append((name, encoded, content))
    directory_size = 4 + sum(4 + len(encoded) + 36 for _, encoded, _ in prepared)
    file_base = (96 + directory_size + 15) & ~15
    offset = 0
    directory = bytearray(struct.pack("<I", len(prepared)))
    for name, encoded, content in prepared:
        directory.extend(struct.pack("<I", len(encoded)) + encoded)
        directory.extend(struct.pack("<QQ", offset, len(content)))
        directory.extend(hashlib.md5(content).digest() + struct.pack("<I", 0))
        offset = (offset + len(content) + 15) & ~15
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as file:
        file.write(MAGIC + struct.pack("<5IQ", 2, *engine_version, 0, file_base) + bytes(64))
        file.write(directory)
        file.write(bytes(file_base - file.tell()))
        for _, _, content in prepared:
            file.write(content)
            file.write(bytes(-file.tell() % 16))
    archive = Archive(destination)
    if set(archive.entries) != set(files):
        raise ValueError("生成的 PCK 资源清单不一致。")
    for name, original in files.items():
        if archive.read(name) != original:
            raise ValueError(f"生成的 PCK 回读不一致：{name}")
