"""Discover packaged Unreal localization and build a reversible bilingual patch.

Probe reads archive indexes and selected metadata. Build only writes its workdir;
installation and game startup belong to the application's shared lifecycle layer.
Supported resources are unencrypted .pak locres versions 1-3, not arbitrary assets.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import uuid


MAGIC = bytes.fromhex("0e147475674a03fc4a15909dc3377f1b")
PATCH_NAME = "zzz_BilingualWorkbench_100_P"
PAK_VERSIONS = ["V0", "V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8A", "V8B", "V9", "V10", "V11"]
SUPPORTED_UE = {"UE4_25", "UE4_26", "UE4_27", "UE5_0", "UE5_1", "UE5_2", "UE5_3", "UE5_4", "UE5_5", "UE5_6", "UE5_7"}
PARAMETERS = re.compile(r"(?<!\{)\{[^{}]+\}(?!\})|%(?:\d+\$)?[-+#0 ]*\d*(?:\.\d+)?[sdifuxX]|%\d+")


def _safe_relative(value: str) -> str:
    value = str(value).replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(p in {"..", "."} for p in path.parts) or ":" in value:
        raise ValueError(f"不安全的相对资源路径：{value}")
    return path.as_posix()


def _under(root: Path, value: str) -> Path:
    result = (root / _safe_relative(value)).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError("资源路径位于游戏目录之外。")
    return result


def _tool(tools_dir: Path, name: str) -> Path:
    result = Path(tools_dir) / (name + (".exe" if os.name == "nt" else ""))
    if not result.is_file():
        raise FileNotFoundError(f"缺少工具 {name}：请在工具目录配置后重试。")
    return result.resolve()


def _run(tool: Path, *args: str, timeout=90) -> bytes:
    result = subprocess.run([str(tool), *map(str, args)], capture_output=True, timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), cwd=tool.parent)
    if result.returncode:
        error = result.stderr.decode("utf-8", errors="replace").strip()[-1200:]
        raise RuntimeError(f"{tool.stem} 无法读取或生成该资源：{error or '工具未提供错误详情'}")
    return result.stdout


def _text(raw: bytes) -> str:
    return raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")


def _get(repak: Path, archive: Path, name: str) -> bytes:
    data = _run(repak, "get", archive, _safe_relative(name))
    if len(data) > 128 * 1024 * 1024:
        raise ValueError("单个语言资源超过 128 MB，停止自动处理。")
    return data


def _read_locres(raw: bytes) -> dict:
    stream = io.BytesIO(raw)

    def read(size):
        if size < 0 or size > len(raw):
            raise ValueError("语言表长度异常。")
        value = stream.read(size)
        if len(value) != size:
            raise ValueError("语言表不完整。")
        return value

    def number(fmt):
        return struct.unpack("<" + fmt, read(struct.calcsize("<" + fmt)))[0]

    def count():
        value = number("i")
        if not 0 <= value <= len(raw):
            raise ValueError("语言表计数异常。")
        return value

    def string():
        size = number("i")
        if not size:
            return ""
        value = read(-size * 2 if size < 0 else size)
        return value.decode("utf-16-le" if size < 0 else "utf-8").removesuffix("\0")

    if read(16) != MAGIC:
        raise ValueError("不是受支持的 Unreal locres 文件。")
    version = number("B")
    if version not in {1, 2, 3}:
        raise ValueError(f"暂不支持 locres v{version}，需要额外格式适配。")
    offset = number("q")
    start = stream.tell()
    if not start <= offset < len(raw):
        raise ValueError("语言表字符串偏移异常。")
    stream.seek(offset)
    values = []
    for _ in range(count()):
        values.append(string())
        if version >= 2:
            number("i")
    stream.seek(start)
    declared = count() if version >= 2 else None
    entries = []
    seen = set()
    for _ in range(count()):
        namespace_hash = number("I") if version >= 2 else None
        namespace = string()
        for _ in range(count()):
            key_hash = number("I") if version >= 2 else None
            key, source_hash, index = string(), number("I"), number("i")
            if not 0 <= index < len(values):
                raise ValueError("语言表字符串索引异常。")
            identity = (namespace, key)
            if identity in seen:
                raise ValueError(f"语言表存在重复文本标识：{identity}")
            seen.add(identity)
            entries.append(dict(ns=namespace, key=key, nh=namespace_hash, kh=key_hash, hash=source_hash, text=values[index]))
    if stream.tell() != offset or (declared is not None and declared != len(entries)):
        raise ValueError("语言表记录计数不一致。")
    return {"version": version, "entries": entries}


def _write_locres(data: dict) -> bytes:
    stream = io.BytesIO()

    def number(fmt, value):
        stream.write(struct.pack("<" + fmt, value))

    def string(value):
        raw = value.encode("utf-8") + b"\0" if value.isascii() else value.encode("utf-16-le") + b"\0\0"
        number("i", len(raw) if value.isascii() else -len(raw) // 2)
        stream.write(raw)

    version = data["version"]
    stream.write(MAGIC)
    number("B", version)
    number("q", 0)
    if version >= 2:
        number("i", len(data["entries"]))
    groups = {}
    for item in data["entries"]:
        groups.setdefault(item["ns"], []).append(item)
    number("i", len(groups))
    indices, values, counts = {}, [], []
    for namespace, items in groups.items():
        if version >= 2:
            number("I", items[0]["nh"])
        string(namespace)
        number("i", len(items))
        for item in items:
            if version >= 2:
                number("I", item["kh"])
            string(item["key"])
            number("I", item["hash"])
            text = item["text"]
            if text not in indices:
                indices[text] = len(values)
                values.append(text)
                counts.append(0)
            index = indices[text]
            counts[index] += 1
            number("i", index)
    offset = stream.tell()
    number("i", len(values))
    for value, references in zip(values, counts):
        string(value)
        if version >= 2:
            number("i", references)
    stream.seek(17)
    number("q", offset)
    result = stream.getvalue()
    # Group ordering may differ; identity/content/hash preservation is what matters.
    expected = {(e["ns"], e["key"]): e for e in data["entries"]}
    actual = {(e["ns"], e["key"]): e for e in _read_locres(result)["entries"]}
    if expected != actual:
        raise ValueError("生成语言表后回读不一致，未生成安装包。")
    return result


def _locale_kind(locale: str) -> str | None:
    value = locale.lower().replace("_", "-")
    if value in {"english", "eng"} or re.fullmatch(r"en(?:-[a-z]+)?", value):
        return "en"
    if value in {"zh-hans", "zh-cn", "zh-sg", "zh", "chinesesimplified", "simplifiedchinese", "schinese", "chs"}:
        return "zh"
    return None


def _pak_dirs(root: Path) -> list[Path]:
    paths = []
    if root.name.lower() == "paks" and root.is_dir():
        paths.append(root)
    for pattern in ("Content/Paks", "*/Content/Paks", "*/*/Content/Paks"):
        paths.extend(p for p in root.glob(pattern) if p.is_dir())
    return sorted(set(p.resolve() for p in paths))


def _info(repak: Path, path: Path) -> dict:
    value = _text(_run(repak, "info", path))
    version = re.search(r"^version:\s*(\S+)", value, re.M)
    mount = re.search(r"^mount point:\s*(.+)", value, re.M)
    return {"version": version.group(1) if version else None,
            "mount": mount.group(1).strip() if mount else None,
            "encrypted": "encrypted index: true" in value.lower()}


def _iostore_version(retoc: Path, path: Path) -> tuple[str | None, dict]:
    info = _text(_run(retoc, "info", path))
    toc = re.search(r"^\s*version:\s*(\w+)", info, re.M)
    header = re.search(r"container_header_version:\s*Some\((\w+)\)", info)
    toc, header = toc.group(1) if toc else "", header.group(1) if header else ""
    known = {("PartitionSize", "Initial"): "UE4_27",
             ("PerfectHashWithOverflow", "LocalizedPackages"): "UE5_0",
             ("PerfectHashWithOverflow", "NoExportInfo"): "UE5_3",
             ("OnDemandMetaData", "NoExportInfo"): "UE5_4",
             ("ReplaceIoChunkHashWithIoHash", "SoftPackageReferences"): "UE5_5"}
    return known.get((toc, header)), {"toc_version": toc, "container_header_version": header}


def probe(root: Path, tools_dir: Path) -> dict:
    requested = Path(root).expanduser().resolve()
    found = _pak_dirs(requested)
    if not found:
        return None
    repak = _tool(tools_dir, "repak")
    paks = found[0]
    base = paks.parents[2]
    if paks.parent.parent == requested and requested.name.lower() != "content":
        base = requested.parent
    warnings = []
    if len(found) > 1:
        warnings.append("所选目录包含多个游戏资源目录，本次使用 " + str(paks))
    archives = sorted(paks.glob("*.pak"))
    # Existing third-party overrides are not silently folded into the game corpus.
    foreign_mods = list(paks.glob("*/*.pak"))
    if foreign_mods:
        warnings.append("检测到子目录中的其他模组；自动配对仅使用 Paks 根目录的资源，已有模组可能影响显示。")
    members = {}
    archive_info = {}
    for archive in archives:
        if archive.name.startswith(PATCH_NAME):
            continue
        relative = archive.relative_to(base).as_posix()
        try:
            names = _text(_run(repak, "list", archive)).splitlines()
        except RuntimeError as exc:
            warnings.append(f"未读取 {archive.name}：{exc}")
            continue
        relevant = [name for name in names if name.lower().endswith((".locres", "/defaultgame.ini"))]
        if not relevant:
            continue
        archive_info[relative] = _info(repak, archive)
        for name in relevant:
            name = _safe_relative(name)
            if name in members:
                warnings.append(f"多个包包含同一资源 {name}；请确认 pairs 中选择的来源包。")
            members[name] = relative
    groups = {}
    for name, archive in members.items():
        path = PurePosixPath(name)
        if path.suffix.lower() != ".locres" or len(path.parts) < 3:
            continue
        culture = path.parent.name
        kind = _locale_kind(culture)
        if kind:
            resource = path.parent.parent / path.name
            groups.setdefault(str(resource), {}).setdefault(kind, []).append((culture, name, archive))
    pairs = []
    for label, languages in sorted(groups.items()):
        if "en" not in languages or "zh" not in languages:
            warnings.append(f"{label} 未同时包含英文和简体中文，未纳入自动双语补丁。")
            continue
        english = sorted(languages["en"], key=lambda x: (x[0].lower() not in {"en", "en-us"}, x[0]))[0]
        chinese = sorted(languages["zh"], key=lambda x: (x[0].lower() != "zh-hans", x[0]))[0]
        pairs.append({"archive": chinese[2], "english_archive": english[2], "chinese_archive": chinese[2],
                      "english": english[1], "chinese": chinese[1], "target": chinese[1],
                      "namespace": PurePosixPath(label).stem,
                      "source_locale": english[0], "target_locale": chinese[0]})
    if not pairs:
        return dict(pairs=[], warnings=[
            "已找到 Content/Paks，但未找到可自动配对的中英文 locres。", *warnings[:4]])
    game_pairs = [p for p in pairs if not p["target"].startswith("Engine/")]
    primary = (game_pairs or pairs)[0]
    versions = [v["version"] for v in archive_info.values() if v["version"] in PAK_VERSIONS]
    pak_version = max(versions, key=PAK_VERSIONS.index) if versions else None
    if not pak_version:
        raise ValueError("无法确认原资源的 pak 版本，未猜测打包格式。")
    title = base.name
    project_version = ""
    for member, archive in members.items():
        if member.lower().endswith("/defaultgame.ini") and not member.startswith("Engine/"):
            config = _text(_get(repak, _under(base, archive), member))
            display = re.search(r'^ProjectDisplayedTitle=(?:INVTEXT\()?"([^"\r\n]+)"', config, re.M)
            name = re.search(r"^ProjectName=(.+)$", config, re.M)
            version = re.search(r"^ProjectVersion=(.+)$", config, re.M)
            title = display.group(1) if display else name.group(1).strip() if name else title
            project_version = version.group(1).strip() if version else ""
            break
    executables = sorted(p.relative_to(base).as_posix() for p in base.glob("*.exe")
                         if not re.search(r"unins|crash|prereq|setup|install", p.name, re.I))
    executables += sorted(p.relative_to(base).as_posix() for p in (paks.parent.parent / "Binaries/Win64").glob("*.exe")
                          if "shipping" in p.name.lower())
    utocs = sorted(p for p in paks.glob("*.utoc") if p.stem != "global" and not p.name.startswith(PATCH_NAME))
    ue_version = None
    container = {}
    if utocs:
        try:
            ue_version, container = _iostore_version(_tool(tools_dir, "retoc"), utocs[0])
        except (RuntimeError, FileNotFoundError) as exc:
            warnings.append(str(exc))
        if ue_version is None:
            warnings.append("IoStore 容器版本不能唯一映射到引擎版本，请设置 options.ue_version 后生成。")
    if warnings and any("多个包包含同一资源" in warning for warning in warnings):
        warnings.append("来源有重复时，生成前须明确 options.accept_duplicate_sources=true，并核对 pairs 选择。")
    return {"engine": "unreal", "root": str(base), "title": title, "executables": executables,
            "launch_args": ["-culture=" + primary["target_locale"]],
            "window_executable": next((exe for exe in executables if "shipping" in exe.lower()), executables[0] if executables else ""),
            "pairs": pairs, "source_locale": primary["source_locale"], "target_locale": primary["target_locale"],
            "details": {"archive_count": len(archives), "paired_resources": len(pairs), "project_version": project_version,
                        "source_archives": archive_info, "container": container,
                        "engine_version_basis": "由容器格式映射得到的兼容打包参数；不是游戏 EXE 版本声明。"},
            "warnings": warnings,
            "options": {"pak_version": pak_version, "needs_iostore": bool(utocs), "ue_version": ue_version,
                        "pak_directory": paks.relative_to(base).as_posix(), "mount_point": "../../../",
                        "language_order": "zh-en", "layout": "auto", "patch_name": PATCH_NAME}}


def _combine(english: str, chinese: str, options: dict) -> tuple[str, str | None]:
    if not english.strip():
        return chinese, "missing_english"
    if not chinese.strip():
        return english, "missing_chinese"
    if english.strip() == chinese.strip():
        return chinese, "identical"
    if set(PARAMETERS.findall(english)) != set(PARAMETERS.findall(chinese)):
        return chinese, "placeholder_mismatch"
    # Commands and plural/select expressions require resource-specific formatting.
    if re.search(r"\}\s*\|(?:plural|select|gender)\s*\(|^\s*!\w+|\{metadata\b|\[bar=", english, re.I | re.M):
        return chinese, "complex_format_preserved"
    layout = options.get("layout", "auto")
    if layout == "newline":
        layout = "lines"
    multiline = layout == "lines" or (layout == "auto" and (len(english) > 52 or "\n" in english or "\n" in chinese))
    separator = "\n" if multiline else " / "
    parts = (english, chinese) if options.get("order", options.get("language_order")) == "en-zh" else (chinese, english)
    return separator.join(parts), None


def _category(namespace: str, resource: str) -> str:
    if resource.startswith("Engine/"):
        return "引擎与系统文本"
    value = namespace.lower()
    if re.search(r"dialog|subtitle|speech|voice|audiolog", value):
        return "对话与字幕"
    if re.search(r"item|weapon|armor|equipment|skill|pictos|inventory", value):
        return "物品与技能"
    if re.search(r"quest|journal|databank|lore|narrative", value):
        return "任务与资料"
    return "界面与游戏文本"


def _fingerprint(root: Path, path: Path) -> dict:
    stat = path.stat()
    result = {"path": path.relative_to(root).as_posix(), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    if stat.st_size <= 16 * 1024 * 1024:
        result["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif path.suffix.lower() == ".pak":
        with path.open("rb") as handle:
            handle.seek(max(0, stat.st_size - 512))
            tail = handle.read()
            magic = struct.pack("<I", 0x5A6F12E1)
            for position in range(len(tail) - 24):
                if tail[position:position + 4] != magic:
                    continue
                version, offset, size = struct.unpack_from("<iqq", tail, position + 4)
                if 1 <= version <= 11 and 0 <= offset <= stat.st_size and 0 < size <= 64 * 1024 * 1024 and offset + size <= stat.st_size:
                    handle.seek(offset)
                    result.update(index_offset=offset, index_size=size,
                                  index_sha256=hashlib.sha256(handle.read(size) + tail).hexdigest())
                    break
    return result


def build(root: Path, profile: dict, workdir: Path, tools_dir: Path) -> dict:
    base = Path(root).expanduser().resolve()
    selected_root = Path(profile.get("root", base)).resolve()
    if selected_root != base:
        # Accept the enclosing directory originally selected in the UI.
        if not selected_root.is_relative_to(base):
            raise ValueError("适配配置指向另一游戏目录。")
        base = selected_root
    work = Path(workdir).expanduser().resolve()
    if work == base or work.is_relative_to(base):
        raise ValueError("生成目录必须位于游戏目录之外；生成步骤不会修改游戏。")
    options = dict(profile.get("options", {}))
    options["language_order"] = options.get("order", options.get("language_order", "zh-en"))
    if options.get("layout") == "newline":
        options["layout"] = "lines"
    if any("多个包包含同一资源" in str(w) for w in profile.get("warnings", [])) and not options.get("accept_duplicate_sources"):
        raise ValueError("发现同名语言资源的多个来源，请核对 pairs 后明确接受来源选择。")
    if options.get("pak_version") not in PAK_VERSIONS:
        raise ValueError("请选择受支持的 pak_version。")
    if options.get("needs_iostore") and options.get("ue_version") not in SUPPORTED_UE:
        raise ValueError("此游戏使用 IoStore，请设置已确认的 ue_version（如 UE5_4）。")
    if options.get("mount_point", "../../../") != "../../../":
        raise ValueError("当前自动生成器仅支持规范化 ../../../ 挂载点。")
    if options.get("layout", "auto") not in {"auto", "lines", "inline"}:
        raise ValueError("layout 必须是 auto、lines 或 inline。")
    if options.get("language_order", "zh-en") not in {"zh-en", "en-zh"}:
        raise ValueError("language_order 必须是 zh-en 或 en-zh。")
    pairs = profile.get("pairs", [])
    if not pairs:
        raise ValueError("未选择可配对的语言资源。")
    repak = _tool(tools_dir, "repak")
    retoc = _tool(tools_dir, "retoc") if options.get("needs_iostore") else None
    run = work / ("unreal-" + uuid.uuid4().hex)
    content = run / "content"
    content.mkdir(parents=True)
    output = run / "patch"
    output.mkdir()
    entries, resources, issues = [], [], []
    counts = Counter()
    fingerprints = {}
    seen_targets = set()
    for pair in pairs:
        target = _safe_relative(pair["target"])
        if target in seen_targets or not target.lower().endswith(".locres"):
            raise ValueError("重复或非 locres 的输出资源路径。")
        seen_targets.add(target)
        english_archive = _under(base, pair.get("english_archive", pair["archive"]))
        chinese_archive = _under(base, pair.get("chinese_archive", pair["archive"]))
        for archive in {english_archive, chinese_archive}:
            if archive.suffix.lower() != ".pak":
                raise ValueError("此适配器仅从 pak 读取语言资源。")
            if archive not in fingerprints:
                fingerprints[archive] = _fingerprint(base, archive)
        english = _read_locres(_get(repak, english_archive, pair["english"]))
        chinese = _read_locres(_get(repak, chinese_archive, pair["chinese"]))
        if english["version"] != chinese["version"]:
            raise ValueError("同组中英文 locres 格式版本不同，需专门适配。")
        en = {(e["ns"], e["key"]): e for e in english["entries"]}
        zh = {(e["ns"], e["key"]): e for e in chinese["entries"]}
        merged = []
        changed = 0
        for identity in dict.fromkeys([*zh, *en]):
            a, b = en.get(identity), zh.get(identity)
            origin = b or a
            original = dict(origin)
            en_text, zh_text = a["text"] if a else "", b["text"] if b else ""
            if a and b and a["hash"] != b["hash"]:
                value, issue = zh_text, "source_hash_mismatch"
            else:
                value, issue = _combine(en_text, zh_text, options)
            original["text"] = value
            merged.append(original)
            changed += int(value != (b["text"] if b else a["text"]))
            counts[issue or "bilingual"] += 1
            if issue and issue != "identical":
                issues.append({"resource": target, "namespace": origin["ns"], "key": origin["key"], "reason": issue})
            category = _category(origin["ns"], target)
            note = {"missing_chinese": "原资源未提供中文，保留英文，不自动编造译文。",
                    "missing_english": "原资源未提供英文，保留中文。",
                    "placeholder_mismatch": "中英文动态参数不一致，游戏显示保留原中文；配对可供检索。",
                    "source_hash_mismatch": "中英文源文哈希不同，游戏显示保留原中文，配对需复核。",
                    "complex_format_preserved": "包含复数、命令或特殊控制格式，游戏显示保留原中文。"}.get(issue, "")
            entries.append({"key": target + "::" + origin["ns"] + "::" + origin["key"],
                            "namespace": origin["ns"], "category": category, "en": en_text, "zh": zh_text,
                            "speaker": "", "source": "本机游戏语言资源 · " + target,
                            "note": note, "phrases": [], "spoiler": category in {"对话与字幕", "任务与资料"},
                            "source_hash": origin["hash"]})
        result = {"version": chinese["version"], "entries": merged}
        destination = content / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_write_locres(result))
        resources.append({"target": target, "entries": len(merged), "changed": changed,
                          "source_locale": pair.get("source_locale"), "target_locale": pair.get("target_locale")})
    patch_name = str(options.get("patch_name", PATCH_NAME))
    if not re.fullmatch(r"[A-Za-z0-9_-]+_P", patch_name):
        raise ValueError("补丁名只能包含英文字母、数字、下划线、横线，并以 _P 结尾。")
    legacy = run / (patch_name + ".pak")
    _run(repak, "pack", content, legacy, "--version", options["pak_version"], "--mount-point", "../../../")
    if retoc:
        _run(retoc, "to-zen", legacy, output / (patch_name + ".utoc"), "--version", options["ue_version"], timeout=180)
        # Locres are loose/non-asset pak files. Retoc emits only an empty companion
        # pak for these inputs; retain the localization pak alongside its IO pair.
        shutil.copyfile(legacy, output / legacy.name)
        expected_suffixes = (".pak", ".utoc", ".ucas")
    else:
        legacy.replace(output / legacy.name)
        expected_suffixes = (".pak",)
    final_pak = output / (patch_name + ".pak")
    for resource in resources:
        extracted = _get(repak, final_pak, resource["target"])
        if extracted != (content / resource["target"]).read_bytes():
            raise ValueError("双语补丁回读验证失败，未交付安装文件。")
    pak_directory = _safe_relative(options["pak_directory"])
    _under(base, pak_directory)
    files = []
    for suffix in expected_suffixes:
        source = output / (patch_name + suffix)
        if not source.is_file():
            raise ValueError(f"生成器缺少输出文件：{source.name}")
        files.append({"source": str(source), "target": pak_directory + "/" + source.name})
    # Re-check source metadata after extraction: never publish a mixed-version build.
    for source, fingerprint in fingerprints.items():
        stat = source.stat()
        if stat.st_size != fingerprint["size"] or stat.st_mtime_ns != fingerprint["mtime_ns"]:
            raise RuntimeError("生成期间游戏资源发生变化，请等待游戏更新完成后重新生成。")
    report = {"engine": "unreal", "title": profile.get("title", base.name), "root": str(base),
              "entries": len(entries), "resources": resources, "counts": dict(counts), "issues": issues,
              "options": options, "roundtrip_verified": True,
              "limitations": ["仅覆盖选定语言表，不包含图片中的文字或未本地化资产。",
                              "补丁资源已回读校验；游戏中的加载、字体、换行和控件布局仍需实机确认。",
                              "没有逐句情境词组解释时，面板应显示通用词典释义及未批注状态。"]}
    (run / "build-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"entries": entries, "files": files, "fingerprints": list(fingerprints.values()), "report": report}
