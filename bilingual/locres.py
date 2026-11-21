"""Read Unreal locres v1-v3 without executing content or changing resources.

Based on the user's existing Subnautica reader; adds bounded reads and validation.
"""
import io
import struct
from pathlib import Path

MAGIC = bytes.fromhex("0e147475674a03fc4a15909dc3377f1b")


def read(path):
    data = Path(path).read_bytes()
    if len(data) > 128 * 1024 * 1024:
        raise ValueError("语言表超过 128 MB，暂不导入。")
    stream = io.BytesIO(data)

    def take(n):
        if n < 0 or n > len(data):
            raise ValueError("语言表长度无效。")
        value = stream.read(n)
        if len(value) != n:
            raise ValueError("语言表已截断。")
        return value

    def num(fmt):
        return struct.unpack("<" + fmt, take(struct.calcsize("<" + fmt)))[0]

    def count():
        n = num("i")
        if not 0 <= n <= min(len(data), 2_000_000):
            raise ValueError("语言表条目数量无效。")
        return n

    def string():
        n = num("i")
        if not n:
            return ""
        raw = take(-n * 2 if n < 0 else n)
        ending = b"\0\0" if n < 0 else b"\0"
        if not raw.endswith(ending):
            raise ValueError("语言表字符串缺少结束标记。")
        return raw[:-len(ending)].decode("utf-16-le" if n < 0 else "utf-8")

    if take(16) != MAGIC:
        raise ValueError("不是受支持的 locres 文件（支持 v1–v3）。")
    version = num("B")
    if version not in (1, 2, 3):
        raise ValueError(f"尚不支持 locres v{version}。")
    offset = num("q")
    if not 25 <= offset < len(data):
        raise ValueError("语言表字符串区位置无效。")
    start = stream.tell()
    stream.seek(offset)
    values = []
    for _ in range(count()):
        values.append(string())
        if version >= 2:
            num("i")
    stream.seek(start)
    declared = count() if version >= 2 else None
    entries = []
    identities = set()
    for _ in range(count()):
        nh = num("I") if version >= 2 else None
        namespace = string()
        for _ in range(count()):
            kh = num("I") if version >= 2 else None
            key, source_hash, index = string(), num("I"), num("i")
            if not 0 <= index < len(values):
                raise ValueError("语言表字符串索引越界。")
            if (namespace, key) in identities:
                raise ValueError("语言表中有重复标识。")
            identities.add((namespace, key))
            entries.append(dict(ns=namespace, key=key, hash=source_hash,
                                nh=nh, kh=kh, text=values[index]))
    if stream.tell() != offset or (declared is not None and declared != len(entries)):
        raise ValueError("语言表条目区与声明不一致。")
    return dict(version=version, entries=entries)
