# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onefile: dist/SmartFileOrganizer.exe."""

from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

project_root = Path(SPECPATH).resolve()
icon_override = os.environ.get("SFO_ICON")
icon_path = Path(icon_override) if icon_override else project_root / "assets" / "app.ico"
icon = str(icon_path) if icon_path.exists() else None
version_file = project_root / "version_info.txt"
version = str(version_file) if version_file.exists() else None

hiddenimports: list[str] = []
try:
    import watchdog  # noqa: F401
except ImportError:
    pass
else:
    # watchdog es opcional. Si está instalado en el entorno de compilación,
    # se incluyen todos sus observadores y utilidades.
    hiddenimports += collect_submodules("watchdog")

a = Analysis(
    [str(project_root / "main.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "setuptools", "pip"],
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
    name="SmartFileOrganizer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
    version=version,
)
