"""本地链接收集测试；--browser 另用临时 Chrome 配置访问本地 HTTP 页面。"""
import json
from pathlib import Path
import sys
import subprocess
import threading
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import urlsplit

from 网页链接收集 import collect, BrowserLinkReader

FIXTURE = {
    "/list/demo_1.html": [("/view/99.html", "最新"),
                          ("/view/10.html", "第一章"), ("/view/20.html", "同人"),
                          ("/list/demo_2.html", "下一页"), ("https://other.test/view/666.html", "广告")],
    "/list/demo_2.html": [("/view/30.html", "第二章"), ("/list/demo_1.html", "上一页")],
    "/view/10.html": [("/view/10_1.html", "【1】"), ("/view/10_2.html", "【2】"),
                      ("/view/20.html", "下一章")],
    "/view/10_2.html": [("/view/10.html", "【1】"), ("/view/10_3.html", "【3】")],
    "/view/10_3.html": [("/view/10_2.html", "上一页")],
    "/view/20.html": [],
    "/view/30.html": [],
}
EXPECTED = ["/view/10.html", "/view/10_2.html", "/view/10_3.html", "/view/20.html", "/view/30.html"]


class LinkTests(unittest.TestCase):
    def fetch(self, url):
        links = FIXTURE[urlsplit(url).path]
        return {"url": url, "links": [{"url": u, "text": t} for u, t in links]}

    def test_directory_and_chapter_pagination_original_order(self):
        result = collect(["https://example.test/list/demo_1.html"], self.fetch)
        self.assertTrue(result["complete"])
        self.assertEqual([urlsplit(u).path for u in result["urls"]], EXPECTED)
        self.assertEqual(result["checked_pages"], 7)

    def test_limit_reports_partial_not_success(self):
        result = collect(["https://example.test/list/demo_1.html"], self.fetch, max_pages=2)
        self.assertFalse(result["complete"])
        self.assertTrue(result["limit_reached"])
        self.assertEqual(len(result["pending"]), 3)

    def test_failures_retained_as_unverified_links(self):
        def fetch(url):
            if url.endswith("10_2.html"):
                raise OSError("测试连接失败")
            return self.fetch(url)
        result = collect(["https://example.test/list/demo_1.html"], fetch)
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("https://example.test/view/10_2.html", result["urls"])

    def test_stop_before_access(self):
        stop = threading.Event()
        stop.set()
        result = collect(["https://example.test/list/demo_1.html"], self.fetch, stop=stop)
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["checked_pages"], 0)

    def test_stop_during_fetch_keeps_pending(self):
        stop = threading.Event()
        def fetch(url):
            stop.set()
            raise RuntimeError("停止")
        result = collect(["https://example.test/list/demo_1.html"], fetch, stop=stop)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["checked_pages"], 0)
        self.assertEqual(len(result["pending"]), 1)
        self.assertEqual(result["errors"], [])

    def test_only_seed_links_no_crawling_or_other_hosts(self):
        result = collect(["https://example.test/list/demo_1.html"], self.fetch, all_links=True)
        self.assertEqual(result["checked_pages"], 1)
        self.assertEqual(len(result["urls"]), 4)
        self.assertTrue(all("other.test" not in u for u in result["urls"]))

    def test_custom_url_pattern_and_query_pagination(self):
        pages = {
            "https://example.test/catalog?page=1": [
                ("/story/a.html", "第一章"), ("/catalog?page=2", "下一页")],
            "https://example.test/catalog?page=2": [("/story/b.html", "第二章")],
            "https://example.test/story/a.html": [],
            "https://example.test/story/b.html": [],
        }
        result = collect(["https://example.test/catalog?page=1"],
                         lambda u: {"url": u, "links": [{"url": a, "text": b} for a, b in pages[u]]},
                         content_pattern=r"/story/")
        self.assertEqual(result["urls"], ["https://example.test/story/a.html", "https://example.test/story/b.html"])
        self.assertTrue(result["complete"])

    def test_reject_external_redirect_and_bad_options(self):
        result = collect(["https://example.test/list.html"],
                         lambda u: {"url": "https://other.test/", "links": []})
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["errors"]), 1)
        with self.assertRaises(ValueError):
            collect(["file:///tmp/test"], self.fetch)
        with self.assertRaises(ValueError):
            collect(["https://example.test/"], self.fetch, max_pages=0)


def browser_test():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from 网页批量保存 import Browser

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            links = FIXTURE.get(self.path)
            if links is None:
                self.send_error(404)
                return
            html = ('<html><meta charset="utf-8"><title>本地链接测试</title><body>' +
                    "".join(f'<a href="{u}">{t}</a>' for u, t in links) + '</body></html>').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with TemporaryDirectory() as directory:
            browser = Browser(Path(directory) / "chrome", headless=True)
            try:
                result = collect([f"http://127.0.0.1:{server.server_port}/list/demo_1.html"],
                                 BrowserLinkReader(browser, threading.Event(), delay=0.05))
                assert result["complete"], result
                assert [urlsplit(u).path for u in result["urls"]] == EXPECTED, result
                print("真实 Chrome 本地测试通过：2 个目录页、3 个章节、2 个续页，顺序及去重正确。")
            finally:
                if browser.process:
                    browser.process.terminate()
                    try:
                        browser.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        browser.process.kill()
                        browser.process.wait()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    if "--browser" in sys.argv:
        browser_test()
    else:
        unittest.main()
