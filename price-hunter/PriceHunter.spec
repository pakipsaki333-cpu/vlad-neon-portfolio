# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one-file windowed PriceHunter.exe (no web stack).

Build (on Windows):  pyinstaller --clean PriceHunter.spec
"""
block_cipher = None

# curl_cffi ships a native libcurl binding the impersonation transport
# needs, and websocket-client drives the installed Chrome/Edge over CDP;
# collect both explicitly so the packaged exe really can use them.
from PyInstaller.utils.hooks import collect_all
_curl_datas, _curl_binaries, _curl_hidden = collect_all("curl_cffi")
_ws_datas, _ws_binaries, _ws_hidden = collect_all("websocket")

a = Analysis(
    ["desktop_app.py"],
    pathex=["."],
    binaries=_curl_binaries + _ws_binaries,
    datas=[("assets", "assets")] + _curl_datas + _ws_datas,
    hiddenimports=["pystray._win32", "PIL.Image", "PIL.ImageDraw",
                   "curl_cffi", "curl_cffi.requests", "websocket",
                   "websocket._core"] + _curl_hidden + _ws_hidden,
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
