"""Build a self-contained app and a single DMG; run with the build venv's Python."""
import argparse
import hashlib
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def call(*args, **kwargs):
    subprocess.run([str(x) for x in args], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, default=ROOT / ".build")
    parser.add_argument("--output", type=Path, default=ROOT / "release")
    parser.add_argument("--arch", choices=["universal2", "arm64", "x86_64"], default="universal2")
    args = parser.parse_args()
    work, output = args.work_dir.resolve(), args.output.resolve()
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    version = (ROOT / "VERSION").read_text().strip()
    target = output / f"WebArchive-{version}-macOS-{args.arch}.dmg"
    if target.exists():
        raise SystemExit(f"Release already exists; move it aside or use another output folder: {target}")
    env = dict(os.environ, WEB_ARCHIVE_ARCH=args.arch)
    call(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", work / "dist", "--workpath", work / "pyinstaller",
         ROOT / "WebArchive.spec", cwd=ROOT, env=env)
    app = work / "dist/网页保存与整理.app"
    call("codesign", "--verify", "--deep", "--strict", app)
    staging = work / "dmg-stage"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    call("ditto", app, staging / app.name)
    (staging / "Applications").symlink_to("/Applications", target_is_directory=True)
    install_text = (ROOT / "docs/安装说明.txt").read_text(encoding="utf-8")
    install_text = re.sub(r"网页保存与整理 \d+\.\d+\.\d+", f"网页保存与整理 {version}", install_text)
    install_text = install_text.replace("universal2 架构（arm64 + x86_64）", {
        "universal2": "universal2 架构（arm64 + x86_64）",
        "arm64": "Apple Silicon 架构（arm64）",
        "x86_64": "Intel 架构（x86_64）",
    }[args.arch])
    (staging / "安装说明.txt").write_text(install_text, encoding="utf-8")
    call("hdiutil", "create", "-volname", "网页保存与整理",
         "-srcfolder", staging, "-format", "UDZO", "-ov", target)
    with target.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    (output / "SHA256SUMS.txt").write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
