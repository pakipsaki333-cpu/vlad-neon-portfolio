# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one-file windowed PriceHunter.exe (no web stack).

Build (on Windows):  pyinstaller --clean PriceHunter.spec
"""
block_cipher = None

# curl_cffi ships a native libcurl binding the browser transport needs;
# collect it explicitly so the packaged exe really can use it.
from PyInstaller.utils.hooks import collect_all
_curl_datas, _curl_binaries, _curl_hidden = collect_all("curl_cffi")

a = Analysis(
    ["desktop_app.py"],
    pathex=["."],
    binaries=_curl_binaries,
    datas=[("assets", "assets")] + _curl_datas,
    hiddenimports=["pystray._win32", "PIL.Image", "PIL.ImageDraw",
                   "curl_cffi", "curl_cffi.requests"] + _curl_hidden,
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
    upx=False,              # UPX-packed exes draw AV false positives
    console=False,          # windowed: no black console behind the app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",
)
