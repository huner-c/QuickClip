# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for QuickClip (--onedir --windowed)."""

from __future__ import annotations

import os
from pathlib import Path
import shutil

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve()


def _locate_media_tool(tool_name: str) -> str:
    env_dir = os.environ.get("FFMPEG_BIN_DIR")
    if env_dir:
        candidate = Path(env_dir) / f"{tool_name}.exe"
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(tool_name) or shutil.which(f"{tool_name}.exe")
    if not found:
        raise FileNotFoundError(
            f"Unable to locate {tool_name}.exe on PATH or in FFMPEG_BIN_DIR."
        )
    return str(Path(found).resolve())


ffmpeg_exe = _locate_media_tool("ffmpeg")
ffprobe_exe = _locate_media_tool("ffprobe")

binaries = [
    (ffmpeg_exe, "bin"),
    (ffprobe_exe, "bin"),
]

datas = [
    (str(ROOT / "assets" / "quickclip.ico"), "assets"),
    (str(ROOT / "assets" / "quickclip.png"), "assets"),
]

hiddenimports = [
    "dxcam",
    "comtypes",
    "comtypes.stream",
    "numpy",
    "mss",
    "mss.windows",
    "keyboard",
    "keyboard._winkeyboard",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetwork",
]
hiddenimports += collect_submodules("dxcam")

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "PySide6.QtWebEngine",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DRender",
        "PySide6.QtQuick3D",
        "PySide6.QtDesigner",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="QuickClip",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "quickclip.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="QuickClip",
)
