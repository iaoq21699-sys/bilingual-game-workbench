"""Read a Windows image's import table without executing the game."""
from pathlib import Path
import struct


def inspect_executable(path):
    path = Path(path)
    with path.open("rb") as stream:
        def read(offset, size):
            stream.seek(offset)
            value = stream.read(size)
            if len(value) != size:
                raise ValueError("游戏程序的格式不完整。")
            return value

        if read(0, 2) != b"MZ":
            raise ValueError("请选择 Windows 游戏程序（.exe）。")
        pe = struct.unpack("<I", read(60, 4))[0]
        header = read(pe, 24)
        if header[:4] != b"PE\0\0":
            raise ValueError("无法识别所选游戏程序。")
        machine, count = struct.unpack_from("<HH", header, 4)
        optional_size = struct.unpack_from("<H", header, 20)[0]
        optional = read(pe + 24, optional_size)
        magic = struct.unpack_from("<H", optional)[0]
        directory = 112 if magic == 0x20b else 96 if magic == 0x10b else None
        if directory is None or count > 96 or len(optional) < directory + 16:
            raise ValueError("不支持这个游戏程序格式。")
        sections = read(pe + 24 + optional_size, count * 40)

        def offset(rva):
            for pos in range(0, len(sections), 40):
                virtual_size, address, raw_size, pointer = struct.unpack_from("<IIII", sections, pos + 8)
                if address <= rva < address + max(virtual_size, raw_size):
                    delta = rva - address
                    if delta >= raw_size:
                        break
                    return pointer + delta
            raise ValueError("无法读取游戏程序的图形接口。")

        import_rva, import_size = struct.unpack_from("<II", optional, directory + 8)
        imports = []
        if import_rva:
            start = offset(import_rva)
            for i in range(min(import_size // 20, 4096)):
                entry = read(start + i * 20, 20)
                if not any(entry):
                    break
                name_rva = struct.unpack_from("<I", entry, 12)[0]
                stream.seek(offset(name_rva))
                name = stream.read(256).split(b"\0", 1)[0].decode("ascii", "replace").lower()
                imports.append(name)
        apis = [name.removesuffix(".dll").upper() for name in ("d3d12.dll", "d3d11.dll", "d3d10.dll", "dxgi.dll") if name in imports]
        return {"architecture": "x64" if machine == 0x8664 else "x86" if machine == 0x14c else hex(machine),
                "apis": apis, "imports": imports}
