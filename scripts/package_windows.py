"""Build the Windows x64 ZIP with the allowlisted workbench and pinned CPython."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import runpy
import tarfile
import uuid
import zipfile

from package import NATIVE_ADDON, PROJECT, collect_files


PYTHON_SHA256 = "2e0fed09d48989f64c9305da9ff2d37a41c0d13ab2aa6283405b2b620dfbf805"
PYTHON_ROOT_FILES = {
    "LICENSE.txt", "python.exe", "pythonw.exe", "python3.dll", "python313.dll",
    "vcruntime140.dll", "vcruntime140_1.dll",
}
PYTHON_TREES = {"DLLs", "Lib", "tcl"}
PYTHON_OMIT = {"__pycache__", "ensurepip", "site-packages", "test", "tests"}
PORTABLE_ROOT_AND_SCRIPTS = {
    "LICENSE", "main.py", "launch.cmd", "启动诊断.cmd", "prepare-native-panel.cmd", "README.md",
    "START-HERE.txt", NATIVE_ADDON, "native/README.txt", "native/vendor/README.txt",
    "scripts/native_panel.py", "scripts/prepare_reshade.py",
}


def runtime_members(source: tarfile.TarFile):
    for member in source:
        path = PurePosixPath(member.name)
        if not member.isfile():
            continue
        if path.parts[:1] != ("python",) or any(p == ".." or ":" in p or "\\" in p for p in path.parts):
            raise ValueError(f"Unexpected runtime archive path: {member.name}")
        parts = path.parts[1:]
        if not parts or any(p in PYTHON_OMIT for p in parts):
            continue
        if (len(parts) == 1 and parts[0] in PYTHON_ROOT_FILES) or parts[0] in PYTHON_TREES:
            yield PurePosixPath("runtime", *parts).as_posix(), member


def package(runtime_archive: Path, output: Path) -> Path:
    if hashlib.sha256(runtime_archive.read_bytes()).hexdigest() != PYTHON_SHA256:
        raise ValueError("CPython archive SHA-256 differs from the pinned Windows x64 build.")
    version = runpy.run_path(str(PROJECT / "bilingual/__init__.py"))["__version__"]
    root = f"BilingualGameWorkbench-{version}"
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"{root}-Windows-x64.zip"
    if target.exists():
        raise FileExistsError(target)

    selected = {
        name: path
        for name, path in collect_files()
        if name in PORTABLE_ROOT_AND_SCRIPTS
        or name.startswith(("bilingual/", "docs/", "examples/", "tools/"))
        or (name.startswith("native/vendor/") and
            (Path(name).name.startswith("LICENSE") or name.endswith("/SOURCE.json")))
    }
    missing = PORTABLE_ROOT_AND_SCRIPTS - selected.keys()
    if missing:
        raise FileNotFoundError(f"Required portable files missing: {', '.join(sorted(missing))}")

    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    manifest = {}
    try:
        with zipfile.ZipFile(temporary, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            def add(name, data):
                if name in manifest:
                    raise ValueError(f"Duplicate portable path: {name}")
                archive.writestr(f"{root}/{name}", data)
                manifest[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

            for name, path in selected.items():
                add(name, path.read_bytes())
            with tarfile.open(runtime_archive, "r:gz") as source:
                for name, member in runtime_members(source):
                    stream = source.extractfile(member)
                    if stream is None:
                        raise ValueError(f"Could not read {name} from runtime archive.")
                    with stream:
                        add(name, stream.read())
            required_runtime = {"runtime/" + name for name in PYTHON_ROOT_FILES} | {
                "runtime/DLLs/_tkinter.pyd", "runtime/Lib/tkinter/__init__.py", "runtime/tcl/tcl8.6/init.tcl",
            }
            if required_runtime - manifest.keys():
                raise ValueError("The archive is missing Python or Tcl/Tk runtime files.")
            archive.writestr(
                f"{root}/RELEASE-MANIFEST.json",
                json.dumps({"version": version, "kind": "windows-x64", "files": manifest}, indent=2),
            )
        with zipfile.ZipFile(temporary) as archive:
            failure = archive.testzip()
            if failure:
                raise ValueError(f"ZIP validation failed for {failure}")
        temporary.rename(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=PROJECT / "dist")
    options = parser.parse_args()
    print(package(options.runtime_archive, options.output))
