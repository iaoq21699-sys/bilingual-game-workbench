# 从源码运行与构建

## 工作台

Windows x64，Python 3.10 或更新版本，带 Tcl/Tk。项目目录需可写。运行 `python main.py`，或双击 `launch.cmd`。手动主题和词句检索使用 Python 标准库，截图提色可另外安装 Pillow。

```powershell
python main.py --status
python -m unittest discover -s tests -v
```

## DirectX 游戏内插件

使用 Visual Studio 2022 的 C++ 工具、Windows SDK 和 CMake。ReShade SDK、ImGui 头文件及 SQLite 源码采用 `native/vendor` 下的版本。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File native/build.ps1
```

输出为 `native/build/bin/Release/BilingualPanel.addon64`。与 ReShade 6.8.0 Full Add-on 配套。退出游戏后由工作台面板设置安装、检查或移除。

## Windows 便携包

1. 先构建原生插件，保留仓库内带许可的 repak 和 retoc 工具。
2. 下载 [CPython 3.13.18 Windows x64 便携运行时](https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.13.18%2B20261110-x86_64-pc-windows-msvc-install_only_stripped.tar.gz)。
3. 用以下命令生成 ZIP。打包脚本校验运行时 SHA-256，保留许可和运行所需文件。

```powershell
python scripts/package_windows.py --runtime-archive .\cpython-3.13.18+20261110-x86_64-pc-windows-msvc-install_only_stripped.tar.gz
```

在新目录解压后测试启动、样本导入、搜索及插件预检。发布包基于明确的文件清单。项目运行数据、游戏文件、Oodle 与 ReShade 运行库应分别由本机提供。