# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one-file windowed PriceHunter.exe (no web stack).

Build (on Windows):  pyinstaller --clean PriceHunter.spec
"""
block_cipher = None

a = Analysis(
    ["desktop_app.py"],
    pathex=["."],
    binaries=[],
    datas=[("assets", "assets")],
    hiddenimports=["pystray._win32", "PIL.Image", "PIL.ImageDraw"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Desktop build intentionally excludes the web panel stack.
    excludes=["fastapi", "uvicorn", "pydantic", "starlette"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PriceHunter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # windowed: no black console behind the app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",
)
