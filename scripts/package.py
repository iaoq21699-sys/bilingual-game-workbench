"""Create separate source and screenshot-guide ZIPs from narrow allowlists.

Usage: python scripts/package.py [--kind source|demo33] [--version 0.9.0] [--overwrite]
The source ZIP excludes docs/demo33. Neither ZIP includes local game resources.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
import zipfile


PROJECT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "main.py", "launch.cmd", "scripts/package.py", "scripts/native_panel.py", "docs/THIRD_PARTY.txt",
    "tools/repak.exe", "tools/retoc.exe", "tools/repak-LICENSE-MIT",
    "tools/repak-LICENSE-APACHE", "tools/retoc-LICENSE",
)
NATIVE_REQUIRED = (
    "native/README.txt", "native/CMakeLists.txt", "native/build.ps1", "native/Overlay.cpp", "native/WindowInput.hpp", "native/RuntimeStatus.hpp",
    "native/shared/Corpus.cpp", "native/shared/Corpus.hpp", "native/shared/NoteWriter.cpp",
    "native/shared/NoteWriter.hpp", "tests/native_note_writer.cpp", "native/vendor/README.txt",
    "native/vendor/reshade/LICENSE.md", "native/vendor/reshade/SOURCE.json",
    "native/vendor/reshade/include/reshade.hpp",
    "native/vendor/reshade/include/reshade_api.hpp",
    "native/vendor/reshade/include/reshade_api_device.hpp",
    "native/vendor/reshade/include/reshade_api_format.hpp",
    "native/vendor/reshade/include/reshade_api_pipeline.hpp",
    "native/vendor/reshade/include/reshade_api_resource.hpp",
    "native/vendor/reshade/include/reshade_events.hpp",
    "native/vendor/reshade/include/reshade_overlay.hpp",
    "native/vendor/imgui/imgui.h", "native/vendor/imgui/imconfig.h",
    "native/vendor/imgui/LICENSE.txt", "native/vendor/imgui/SOURCE.json",
    "native/vendor/sqlite/sqlite3.c", "native/vendor/sqlite/sqlite3.h",
    "native/vendor/sqlite/sqlite3ext.h", "native/vendor/sqlite/LICENSE.txt",
    "native/vendor/sqlite/SOURCE.json",
)
# The sole optional build product is our own add-on, never the ReShade runtime.
NATIVE_ADDON = "native/build/bin/Release/BilingualPanel.addon64"
OPTIONAL = ("README.txt", "README.md", "THIRD_PARTY.txt", "THIRD_PARTY.md", "PROJECT_STATE.txt", "启动诊断.cmd",
            "docs/game-candidates-2016-2026.html", "docs/game-candidates-2016-2026.csv",
            "START-HERE.txt", "prepare-native-panel.cmd", "scripts/prepare_reshade.py", "scripts/package_windows.py")
TREES = {"bilingual": {".py", ".js", ".css"}, "tests": {".py"}, "docs": {".txt", ".md"}, "examples": {".json"}}
EXCLUDED_PARTS = {"data", "game", "games", "logs", "__pycache__", "cache", "backups", "userdata", "evidence", "validation", "dist", "build", ".git", ".bilingual-workbench"}
EXECUTABLES = {"tools/repak.exe", "tools/retoc.exe"}
DEMO_EXTENSIONS = {".md", ".txt", ".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".webp"}


def collect_files(project: Path = PROJECT) -> list[tuple[str, Path]]:
    """Return allowed paths only; do not create files or execute bundled tools."""
    project = Path(project).resolve()
    selected = set(REQUIRED) | set(NATIVE_REQUIRED)
    selected.update(name for name in OPTIONAL if (project / name).is_file())
    if (project / NATIVE_ADDON).is_file():
        selected.add(NATIVE_ADDON)
    if not any((project / name).is_file() for name in ("README.txt", "README.md")):
        raise FileNotFoundError("Missing README.txt or README.md")
    for folder, extensions in TREES.items():
        source = project / folder
        if not source.is_dir():
            raise FileNotFoundError(f"Missing source directory: {folder}")
        for path in source.rglob("*"):
            relative = path.relative_to(project)
            if len(relative.parts) >= 2 and relative.parts[0].casefold() == "docs" and relative.parts[1].casefold() in {"demo33", "demo-lis"}:
                continue
            if any(part.casefold() in EXCLUDED_PARTS for part in relative.parts):
                continue
            if path.is_file() and path.suffix.lower() in extensions:
                selected.add(relative.as_posix())
    files = []
    for name in sorted(selected):
        path = project / name
        resolved = path.resolve()
        if not resolved.is_relative_to(project) or not path.is_file():
            raise ValueError(f"Missing file or path outside project: {name}")
        # Check real destinations as well, so a link in docs/source cannot
        # smuggle private data or the local-only runtime into a release.
        actual = resolved.relative_to(project)
        actual_parts = tuple(part.casefold() for part in actual.parts)
        if actual_parts[:2] == ("tools", "reshade-runtime"):
            raise ValueError(f"ReShade runtime cache must never be packaged: {name}")
        if any(part in EXCLUDED_PARTS for part in actual_parts):
            if name != NATIVE_ADDON or actual.as_posix() != NATIVE_ADDON:
                raise ValueError(f"Private data or build output cannot be packaged: {name}")
        if path.suffix.lower() == ".dll" or "oo2core" in path.name.lower():
            raise ValueError(f"Proprietary local decompressor cannot be packaged: {name}")
        if path.suffix.lower() == ".exe" and name not in EXECUTABLES:
            raise ValueError(f"Executable is not allowlisted: {name}")
        if path.suffix.lower() == ".addon64" and name != NATIVE_ADDON:
            raise ValueError(f"Native add-on is not allowlisted: {name}")
        if name.startswith("examples/"):
            # Shareable profiles use a relocatable root, never this user's game path.
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(value, dict) and "root" in value and value["root"] not in (".", "", None):
                raise ValueError(f"Example contains a machine-specific root: {name}")
        files.append((name, path))
    return files


def collect_demo_files(project: Path = PROJECT) -> list[tuple[str, Path]]:
    """Select only the authored guide and screenshots, with index.html at ZIP root."""
    project = Path(project).resolve()
    source = (project / "docs/demo33").resolve()
    if not source.is_relative_to(project) or not source.is_dir():
        raise ValueError("Screenshot guide must be inside project/docs/demo33")
    files = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part.casefold() in EXCLUDED_PARTS for part in relative.parts):
            continue
        if not path.is_file() or path.suffix.lower() not in DEMO_EXTENSIONS:
            continue
        if not path.resolve().is_relative_to(source):
            raise ValueError(f"Guide path escapes docs/demo33: {relative.as_posix()}")
        files.append((relative.as_posix(), path))
    return files


def package(version: str = "0.11.2", overwrite: bool = False, project: Path = PROJECT, kind: str = "source") -> Path:
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*(?:-[A-Za-z0-9]+)?", version):
        raise ValueError("Use a simple release version such as 0.2 or 0.2-beta")
    project = Path(project).resolve()
    if kind not in {"source", "demo33"}:
        raise ValueError("Package kind must be source or demo33")
    files = collect_demo_files(project) if kind == "demo33" else collect_files(project)
    if kind == "demo33" and "index.html" not in {name for name, _ in files}:
        raise FileNotFoundError("Screenshot guide is not ready: docs/demo33/index.html is missing")
    destination_dir = project / "dist"
    destination_dir.mkdir(exist_ok=True)
    if not destination_dir.resolve().is_relative_to(project):
        raise ValueError("Release output must remain inside the project")
    filename = (f"Expedition33-BilingualGuide-{version}.zip" if kind == "demo33"
                else f"BilingualGameWorkbench-{version}-source.zip")
    destination = destination_dir / filename
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Release already exists: {destination.name}; use --overwrite intentionally")
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        manifest = []
        with zipfile.ZipFile(temporary, "x", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, path in files:
                content = path.read_bytes()
                archive.writestr(name, content)
                manifest.append({"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
            archive.writestr("RELEASE-MANIFEST.json", json.dumps({"version": version, "kind": kind, "files": manifest}, ensure_ascii=False, indent=2))
        with zipfile.ZipFile(temporary) as archive:
            problem = archive.testzip()
            if problem:
                raise ValueError(f"ZIP integrity check failed: {problem}")
            expected = {name for name, _ in files} | {"RELEASE-MANIFEST.json"}
            if set(archive.namelist()) != expected:
                raise ValueError("ZIP contents differ from the allowlist")
        if destination.exists() and not overwrite:
            raise FileExistsError(f"Release appeared during packaging: {destination.name}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="0.11.2")
    parser.add_argument("--kind", choices=("source", "demo33"), default="source")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--list", action="store_true", help="Print selected paths without creating a ZIP")
    args = parser.parse_args()
    if args.list:
        files = collect_demo_files() if args.kind == "demo33" else collect_files()
        for name, _ in files:
            print(name)
    else:
        print(package(args.version, args.overwrite, kind=args.kind))


if __name__ == "__main__":
    main()
