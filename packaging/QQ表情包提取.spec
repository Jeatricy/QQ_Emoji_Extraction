# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：QQ 表情包提取与去重。

生成单文件 exe，内置 CustomTkinter 的主题与字体资源。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

# spec 里的相对路径以本文件所在目录为基准，脚本在上一级。
PROJECT_ROOT = Path(SPECPATH).parent
SCRIPT = str(PROJECT_ROOT / "qq_emoji_tool.py")

# customtkinter 在运行时读取 assets/themes/*.json 与 assets/fonts/*，
# 这些不是 .py 模块，必须显式收集，否则打包后启动即报错。
datas = collect_data_files("customtkinter")

a = Analysis(
    [SCRIPT],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=["PIL._tkinter_finder"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 只排除确定用不到的大块头；不再剔除标准库，避免省体积换来运行期报错。
    excludes=["numpy", "pandas", "matplotlib", "scipy", "pytest", "IPython"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="QQ表情包提取",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
