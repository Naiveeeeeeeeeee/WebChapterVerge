"""Verify a relocated bundle against synthetic local pages; never read personal archives."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("app", type=Path)
    args = parser.parse_args()
    app = args.app.resolve()
    executable = app / "Contents/MacOS/WebArchive"
    with tempfile.TemporaryDirectory(prefix="webarchive-check-") as tmp:
        root = Path(tmp)
        env = dict(os.environ, WEB_ARCHIVE_DATA_DIR=str(root / "data"), PATH="/usr/bin:/bin")
        def run(*arguments):
            result = subprocess.run([str(executable), *map(str, arguments)], env=env,
                                    capture_output=True, text=True, timeout=60)
            if result.returncode:
                log = root / "data/启动日志.log"
                raise RuntimeError(result.stderr + (log.read_text() if log.exists() else ""))

        pages = root / "pages"
        pages.mkdir()
        (pages / "a.html").write_text(
            '<base href="https://example.test/view/1.html"><h1>第一章</h1>'
            '<div id="nr1">「第一句。」<br>「第二句。<br><br>　　」'
            '<br><br>本章未完，点击[ 数字分页 ]继续阅读--&gt;&gt;</div>'
            '<a href="/view/1_1.html">【1】</a><a href="/view/1_2.html">【2】</a>', encoding="utf-8")
        (pages / "b.html").write_text(
            '<base href="https://example.test/view/1_2.html"><h1>第一章</h1>'
            '<div id="nr1">续页正文。</div>', encoding="utf-8")
        run("--organize", pages, "--organized-output", root / "out", "--text-title", "本地测试")
        report_file = next((root / "out").glob("*/提取校验.json"))
        report = json.loads(report_file.read_text())
        text = (report_file.parent / "正文合并.txt").read_text(encoding="utf-8-sig")
        assert report["content_files"] == 2 and report["missing_pages"] == 0
        assert report["removed_notice_count"] == 1 and "「第一句。」\n「第二句。」" in text
        assert "续页正文。" in text and "本章未完" not in text
        run("--ui-smoke-test")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = '<html><meta charset="utf-8"><title>本地安装包测试</title><body>归档测试正文。</body></html>'.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        profile = root / "chrome"
        try:
            urls = root / "urls.txt"
            urls.write_text(f"http://127.0.0.1:{server.server_port}/test\n")
            run("--urls", urls, "--output", root / "archives", "--headless",
                "--profile", profile, "--wait", "0.1", "--interval", "0", "--no-scroll")
            assert len(list((root / "archives").glob("*.mhtml"))) == 1
        finally:
            # Close only the temporary profile started by this test, through its own CDP endpoint.
            marker = profile / "DevToolsActivePort"
            if marker.exists():
                import sys
                sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
                from 网页批量保存 import CDP
                lines = marker.read_text().splitlines()
                cdp = CDP(f"ws://127.0.0.1:{lines[0]}{lines[1]}")
                try:
                    cdp.call("Browser.close", timeout=5)
                except (OSError, RuntimeError):
                    pass
                finally:
                    cdp.close()
                import time
                for _ in range(50):
                    if not (profile / "SingletonLock").is_symlink():
                        break
                    time.sleep(0.1)
            server.shutdown()
            server.server_close()
        print("PASS: relocated app, GUI, offline TXT processing, and real Chrome MHTML capture.")


if __name__ == "__main__":
    main()
