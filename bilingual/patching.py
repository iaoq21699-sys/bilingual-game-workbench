"""Owned-file transactions shared by all configurable game integrations."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import uuid


def inside(root, relative):
    root = Path(root).resolve()
    relative = str(relative)
    win = PureWindowsPath(relative)
    if not relative or win.is_absolute() or win.drive or ".." in win.parts:
        raise ValueError(f"资源路径必须位于游戏目录内：{relative}")
    path = (root / relative).resolve()
    if path == root or root not in path.parents:
        raise ValueError(f"资源路径超出了游戏目录：{relative}")
    return path


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with candidate.open("w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)


def _atomic_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with Path(source).open("rb") as src, temporary.open("xb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


class PatchSet:
    def __init__(self, root, profile_id, state_dir=".bilingual-workbench"):
        self.root = Path(root).resolve()
        self.profile_id = profile_id
        self.home = inside(self.root, state_dir)
        self.manifest = self.home / "state.json"

    @contextmanager
    def _lock(self):
        self.home.mkdir(parents=True, exist_ok=True)
        file = (self.home / "operation.lock").open("a+b")
        try:
            file.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise RuntimeError("另一个工作台正在修改此游戏，请等它完成。") from exc
            yield
        finally:
            file.close()

    def state(self):
        if not self.manifest.is_file():
            return {"phase": "disabled", "profile_id": self.profile_id, "files": []}
        value = json.loads(self.manifest.read_text(encoding="utf-8"))
        if value.get("profile_id") != self.profile_id or value.get("version") != 1:
            raise ValueError("此目录已有其他接入配置的安装记录，未覆盖。")
        return value

    def status(self):
        state = self.state()
        if state["phase"] in ("installing", "restoring"):
            return "上次操作中断，请先恢复原版再重新启用"
        if state["phase"] == "enabled":
            for row in state["files"]:
                target = inside(self.root, row["target"])
                if not target.is_file() or digest(target) != row["after"]:
                    return "补丁文件已变化，请检查；未自动覆盖"
            return f"双语已启用（{len(state['files'])} 个受管理文件）"
        return "原版显示 / 双语未启用"

    def verify_sources(self, fingerprints):
        for entry in fingerprints:
            path = inside(self.root, entry["path"])
            if not path.is_file():
                raise ValueError(f"原始资源已丢失：{entry['path']}")
            stat = path.stat()
            if entry.get("sha256"):
                matches = digest(path) == entry["sha256"]
            else:
                matches = stat.st_size == entry["size"] and stat.st_mtime_ns == entry["mtime_ns"]
            if matches and entry.get("index_sha256"):
                offset, length = entry["index_offset"], entry["index_size"]
                if not (0 <= offset < stat.st_size and 0 < length <= 64 * 1024 * 1024 and offset + length <= stat.st_size):
                    raise ValueError("资源索引校验范围不合法。")
                with path.open("rb") as file:
                    file.seek(offset)
                    index = file.read(length)
                    file.seek(max(0, stat.st_size - 512))
                    matches = hashlib.sha256(index + file.read()).hexdigest() == entry["index_sha256"]
            if not matches:
                raise ValueError(f"游戏资源已更新，请恢复原版后重新接入：{entry['path']}")

    def enable(self, files, fingerprints=()):
        with self._lock():
            previous = self.state()
            if previous["phase"] in ("installing", "restoring"):
                raise ValueError("上次操作未结束，请先恢复原版。")
            if previous["phase"] == "enabled":
                current = {row["target"]: row["after"] for row in previous["files"]}
                desired = {row["target"]: digest(row["source"]) for row in files}
                if current != desired:
                    raise ValueError("生成结果与已安装版本不同，请先恢复原版再重新接入。")
                # Verify both installed bytes and source resources on every launch.
                for row in previous["files"]:
                    target = inside(self.root, row["target"])
                    if not target.is_file() or digest(target) != row["after"]:
                        raise ValueError("已安装补丁被其他程序修改，未覆盖。")
                patched = set(current)
                self.verify_sources([f for f in fingerprints if f["path"] not in patched])
                return self.status()
            self.verify_sources(fingerprints)
            rows, seen = [], set()
            for item in files:
                target = inside(self.root, item["target"])
                relative = target.relative_to(self.root).as_posix()
                canonical = os.path.normcase(relative)
                if relative.casefold().startswith(".bilingual-workbench/") or canonical in seen:
                    raise ValueError("补丁包含重复或保留路径。")
                seen.add(canonical)
                source = Path(item["source"]).resolve()
                if not source.is_file():
                    raise ValueError(f"生成的补丁文件不存在：{source.name}")
                after = digest(source)
                before = digest(target) if target.is_file() else None
                if target.exists() and before is None:
                    raise ValueError(f"目标不是文件：{relative}")
                # Existing files may only be replaced when explicitly fingerprinted.
                expected = next((f for f in fingerprints if f["path"].replace("\\", "/") == relative), None)
                if before is not None and expected is None:
                    raise ValueError(f"目标已有非本工具文件，未覆盖：{relative}")
                backup = f"originals/{hashlib.sha256(relative.encode()).hexdigest()}.bin" if before else None
                if backup:
                    backup_file = inside(self.home, backup)
                    if backup_file.exists() and digest(backup_file) != before:
                        raise ValueError("原始备份与当前游戏版本不一致，请先保留旧备份并重新配置。")
                    if not backup_file.exists():
                        _atomic_copy(target, backup_file)
                rows.append(dict(target=relative, source=str(source), before=before, after=after, backup=backup))
            if not rows:
                raise ValueError("没有生成可安装的双语文件。")
            pending = dict(version=1, profile_id=self.profile_id, phase="installing", files=rows)
            save_json(self.manifest, pending)
            applied = []
            try:
                for row in rows:
                    target = inside(self.root, row["target"])
                    now = digest(target) if target.is_file() else None
                    if now != row["before"]:
                        raise ValueError(f"安装过程中目标发生变化：{row['target']}")
                    _atomic_copy(row["source"], target)
                    applied.append(row)
                    if digest(target) != row["after"]:
                        raise ValueError("补丁写入后的校验失败。")
                pending["phase"] = "enabled"
                save_json(self.manifest, pending)
            except BaseException as original_error:
                for row in reversed(applied):
                    target = inside(self.root, row["target"])
                    if not target.is_file() or digest(target) != row["after"]:
                        raise RuntimeError("安装失败且目标随后被外部修改；保留恢复记录，未删除或覆盖外部修改。") from original_error
                    self._restore_original(row)
                save_json(self.manifest, dict(previous, version=1))
                raise
            return self.status()

    def _restore_original(self, row):
        target = inside(self.root, row["target"])
        if row["before"] is None:
            target.unlink(missing_ok=True)
        else:
            backup = inside(self.home, row["backup"])
            if not backup.is_file() or digest(backup) != row["before"]:
                raise ValueError("原版备份不存在或校验失败，停止恢复。")
            _atomic_copy(backup, target)

    def disable(self):
        with self._lock():
            state = self.state()
            if state["phase"] == "disabled":
                return "当前已为原版显示。"
            eligible = []
            for row in state["files"]:
                target = inside(self.root, row["target"])
                current = digest(target) if target.is_file() else None
                if current not in (row["after"], row["before"]):
                    raise ValueError(f"文件已被其他程序改变，未覆盖：{row['target']}")
                if row["before"]:
                    backup = inside(self.home, row["backup"])
                    if not backup.is_file() or digest(backup) != row["before"]:
                        raise ValueError("原版备份校验失败，未修改游戏。")
                if current == row["after"]:
                    eligible.append(row)
            # Keep a recoverable copy of every installed file before restoring any.
            undo = self.home / "recovery" / uuid.uuid4().hex
            for i, row in enumerate(eligible):
                _atomic_copy(inside(self.root, row["target"]), undo / str(i))
            save_json(self.manifest, dict(state, phase="restoring"))
            applied = []
            try:
                for i, row in enumerate(eligible):
                    target = inside(self.root, row["target"])
                    if not target.is_file() or digest(target) != row["after"]:
                        raise ValueError(f"恢复过程中目标发生变化，未覆盖：{row['target']}")
                    self._restore_original(row)
                    applied.append((i, row))
                save_json(self.manifest, dict(state, phase="disabled"))
            except BaseException:
                for i, row in reversed(applied):
                    _atomic_copy(undo / str(i), inside(self.root, row["target"]))
                save_json(self.manifest, state)
                raise
            return "已恢复原版显示；原始备份保留。"
