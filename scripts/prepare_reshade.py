"""Prepare the matching ReShade runtime from its official installer archive."""
from __future__ import annotations

import argparse
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import urllib.request
import zipfile


PROJECT = Path(__file__).resolve().parents[1]
VERSION = "6.8.0"
INSTALLER_URL = "https://reshade.me/downloads/ReShade_Setup_6.8.0_Addon.exe"
INSTALLER_SIZE = 4_318_424
INSTALLER_SHA256 = "afe4c8f13048306307983b8b3d41d5bf00a86820440b0e57dea10950e1176445"
DLL_SIZE = 5_592_064
DLL_SHA256 = "0cee63f9c9f13f3ac909c5b4903f4dbb4b719a7ab3b4f13b0deaf83c814b94f7"


def matches(target: Path) -> bool:
    return (target.is_file() and target.stat().st_size == DLL_SIZE
            and hashlib.sha256(target.read_bytes()).hexdigest() == DLL_SHA256)


def prepare(installer: Path | None) -> str:
    target = PROJECT / "tools" / "reshade-runtime" / "ReShade64.dll"
    if target.exists():
        if matches(target):
            return f"ReShade {VERSION} Full Add-on 已准备好。"
        raise FileExistsError("已有不同版本的 ReShade64.dll。请先退出游戏并检查现有运行库。")

    if installer:
        with installer.open("rb") as source:
            payload = source.read(INSTALLER_SIZE + 1)
    else:
        print(f"正在从 reshade.me 获取 ReShade {VERSION} Full Add-on…", flush=True)
        request = urllib.request.Request(INSTALLER_URL, headers={"User-Agent": "BilingualGameWorkbench"})
        with urllib.request.urlopen(request, timeout=30) as source:
            payload = source.read(INSTALLER_SIZE + 1)
    if len(payload) != INSTALLER_SIZE or hashlib.sha256(payload).hexdigest() != INSTALLER_SHA256:
        raise ValueError("安装文件校验失败。请从 ReShade 官网取得匹配版本的 Full Add-on 安装文件。")

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        image = archive.read("ReShade64.dll")
    if len(image) != DLL_SIZE or hashlib.sha256(image).hexdigest() != DLL_SHA256:
        raise ValueError("ReShade64.dll 校验失败。")

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reshade-", dir=target.parent) as folder:
        library = Path(folder) / target.name
        library.write_bytes(image)
        try:
            library.rename(target)
        except FileExistsError:
            if not matches(target):
                raise FileExistsError("其他操作已写入不同的 ReShade64.dll，请检查。") from None
    return "运行库已就绪。退出所选游戏后，可在工作台「面板设置」中保存并安装。"


def main() -> int:
    parser = argparse.ArgumentParser(description="从官网准备 DirectX 面板的 ReShade 运行库。")
    parser.add_argument("--installer", type=Path, help="已下载的官方 Full Add-on 安装文件，可离线提取")
    args = parser.parse_args()
    try:
        print(prepare(args.installer))
        return 0
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f"准备运行库失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
