"""可写数据放在用户目录，应用安装包保持只读。"""
import json
import os
from pathlib import Path
import sys

FROZEN = bool(getattr(sys, "frozen", False))
SOURCE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("WEB_ARCHIVE_DATA_DIR",
                          str(Path.home() / "Library/Application Support/WebArchive")))
SETTINGS = DATA / "settings.json"


def preferences():
    try:
        value = json.loads(SETTINGS.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def output_directory():
    if not FROZEN:
        return SOURCE / "已保存网页"
    return Path(preferences().get("output_directory") or
                Path.home() / "Documents/网页保存与整理/已保存网页").expanduser()


def browser_profile():
    if not FROZEN and not os.environ.get('WEB_ARCHIVE_DATA_DIR'):
        return SOURCE / "浏览器数据"
    return Path(preferences().get("browser_profile") or DATA / "浏览器数据").expanduser()


def remember_output(folder):
    if not FROZEN:
        return
    DATA.mkdir(parents=True, exist_ok=True)
    settings = preferences()
    settings["output_directory"] = str(folder)
    temporary = SETTINGS.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(SETTINGS)
