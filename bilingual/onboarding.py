"""Presentation model for read-only game preflight results."""
ENGINE_LABELS = {"electron": "Electron · ASAR", "unreal": "Unreal · PAK",
                 "godot": "Godot · PCK", "loose_json": "松散 JSON", "profile": "接入配置"}


def describe(probe):
    kind = probe.get("engine")
    reason = probe.get("diagnostic")
    hints = [src for src, _ in probe.get("issues", []) if src in ENGINE_LABELS]
    engine = ENGINE_LABELS.get(kind or next(iter(hints), None), "游戏接入")
    if probe.get("connectable") is False:
        guidance = {
            "missing_exe": ("没有找到游戏启动程序",
                            "请选择游戏本身的安装目录，不要选 Steam 游戏库根目录或单独的资源文件夹。"),
            "invalid_profile": ("接入配置无法使用",
                                 "目录中的 bilingual-game.json 无法通过检查。请核对游戏版本与配置，或导入一份匹配的配置。"),
            "resource_issue": ("找到了资源线索，但无法自动接入",
                                 "先检查下方的资源格式与工具提示。只有中英文本时可以仅导入文本，不需要修改游戏。"),
            "unsupported": ("没有识别到支持的语言资源",
                               "检查是否选对游戏安装目录。没有受支持的资源时，可导入已有接入配置或中英文本。"),
        }
        title, body = guidance.get(reason, ("识别到引擎，但暂无可用的中英配对",
                                           "可导入官方导出的语言文件或已有双语文本。仅导入的语料可在桌面检索，不会启用游戏内显示。"))
    elif kind == "electron":
        title, body = ("先建词句库，再启用内置会话",
                       "接入语料不改写原包。需要 F8 时，在面板设置点「保存并启用」，再从工作台启动。")
    else:
        title, body = ("先验证双语显示，再配置 F8",
                       "双语资源、桌面检索与游戏内 F8 是不同功能。先验证一条游戏文本，再按需开启 F8。")
    return title, body, engine
