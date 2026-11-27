# PyInstaller one-file Windows build for the loopback-only AAMS dashboard.
from pathlib import Path

root = Path(SPECPATH)
datas = [
    (str(root / "ui"), "ui"),
    (str(root / "third_party_licenses"), "third_party_licenses"),
]

a = Analysis(
    [str(root / "release_entry.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name="AAMS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
