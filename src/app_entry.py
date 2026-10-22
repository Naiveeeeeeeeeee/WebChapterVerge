"""macOS bundle entry; log startup errors without opening Terminal."""
import sys
import traceback
from 应用路径 import DATA, FROZEN


def run():
    if FROZEN:
        DATA.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = (DATA / "启动日志.log").open("a", encoding="utf-8", buffering=1)
    try:
        from 网页批量保存 import main
        main()
    except Exception:
        traceback.print_exc()
        try:
            from tkinter import messagebox
            messagebox.showerror("网页保存与整理", f"运行失败，详情已写入：\n{DATA / '启动日志.log'}")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    run()
