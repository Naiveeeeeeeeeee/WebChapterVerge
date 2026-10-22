# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os

root = Path(SPECPATH)
arch = os.environ.get("WEB_ARCHIVE_ARCH", "universal2")
version = (root / "VERSION").read_text().strip()
a = Analysis(
    [str(root / "src/app_entry.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[(str(root / "assets/AppIcon.png"), "assets"),
           (str(root / "docs/THIRD_PARTY_NOTICES.txt"), ".")],
    hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=[], noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="WebArchive",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=arch, codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="WebArchive")
app = BUNDLE(
    coll, name="网页保存与整理.app", icon=str(root / "assets/AppIcon.icns"),
    bundle_identifier="local.webarchive.desktop", version=version,
    info_plist={
        "CFBundleDisplayName": "网页保存与整理",
        "CFBundleShortVersionString": version,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
    },
)
