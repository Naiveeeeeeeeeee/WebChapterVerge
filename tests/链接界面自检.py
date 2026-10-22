"""点击真实 Tk 控件验证网址输入、误填恢复和提示；--browser 使用本机 Chrome 读取本地页面。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from 网页链接收集 import show_dialog


def widgets(parent):
    for child in parent.winfo_children():
        yield child
        yield from widgets(child)


def named(window, name):
    return next(w for w in widgets(window) if w.winfo_name() == name)


def button(window, text):
    return next(w for w in widgets(window) if isinstance(w, (ttk.Button, ttk.Checkbutton)) and w.cget('text') == text)


class DialogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='link-ui-test-')
        self.root = tk.Tk()
        self.root.geometry('300x100')
        self.root.update()
        self.applied = []
        self.window = show_dialog(self.root, None, '', self.applied.extend, Path(self.tmp.name))
        self.window.update()
        self.errors = []
        self.error_patch = patch('tkinter.messagebox.showerror', side_effect=lambda title, msg, **kw: self.errors.append((title, msg)))
        self.error_patch.start()
        self.calls = []
        def fetch(url):
            self.calls.append(url)
            return {'url': url, 'links': [{'url': '/book/42/b.html', 'text': '下一章'}] if url.endswith('/a.html') else []}
        self.reader_patch = patch('网页链接收集.BrowserLinkReader', return_value=fetch)
        self.reader_patch.start()

    def tearDown(self):
        self.reader_patch.stop()
        self.error_patch.stop()
        # 销毁测试窗口前取消定时轮询，避免留给下一组 Tk 测试。
        for task in self.root.tk.call('after', 'info'):
            self.root.tk.call('after', 'cancel', task)
        self.root.destroy()
        self.tmp.cleanup()

    def put(self, name, text):
        field = named(self.window, name)
        if isinstance(field, tk.Text):
            field.delete('1.0', 'end'); field.insert('1.0', text)
        else:
            field.delete(0, 'end'); field.insert(0, text)

    def finish(self):
        button(self.window, '开始收集').invoke()
        deadline = time.monotonic() + 5
        use = button(self.window, '使用这份清单 → 主窗口')
        while str(use.cget('state')) == 'disabled' and not self.errors and time.monotonic() < deadline:
            self.root.update(); time.sleep(0.01)
        self.assertEqual(self.errors, [])
        self.assertEqual(str(use.cget('state')), 'normal')
        actual = named(self.window, 'collected_urls').get('1.0', 'end').split()
        self.assertEqual(actual, ['https://example.test/book/42/a.html', 'https://example.test/book/42/b.html'])
        use.invoke()
        self.assertEqual(self.applied, actual)
        report = next(Path(self.tmp.name).glob('链接清单/收集报告_*.json'))
        self.assertTrue(json.loads(report.read_text())['complete'])

    def test_normal_paste_and_click(self):
        self.put('entry_urls', 'https://example.test/book/42/a.html')
        self.finish()

    def test_screenshot_misplaced_url_recovers_and_continues(self):
        button(self.window, '高级设置（一般不用改）').invoke()
        self.put('content_pattern', 'https://example.test/book/42/a.html')
        self.finish()

    def test_same_url_in_both_fields_is_not_a_filter(self):
        self.put('entry_urls', 'https://example.test/book/42/a.html')
        self.put('content_pattern', 'https://example.test/book/42/a.html')
        self.finish()
        self.assertEqual(len(self.calls), 2)

    def test_empty_url_names_the_correct_field(self):
        button(self.window, '开始收集').invoke()
        self.assertIn('顶部白色', self.errors[0][1])
        self.assertNotIn('5000', self.errors[0][1])
        self.assertEqual(self.calls, [])

    def test_bad_limit_names_only_the_limit(self):
        self.put('entry_urls', 'https://example.test/book/42/a.html')
        self.put('maximum_pages', 'abc')
        button(self.window, '开始收集').invoke()
        self.assertIn('最多检查页数', self.errors[0][1])
        self.assertEqual(self.calls, [])

    def test_bad_regex_is_reported(self):
        self.put('entry_urls', 'https://example.test/book/42/a.html')
        self.put('content_pattern', '[')
        button(self.window, '开始收集').invoke()
        self.assertIn('正则', self.errors[0][1])
        self.assertEqual(self.calls, [])

    def test_real_regex_is_preserved(self):
        self.put('entry_urls', 'https://example.test/book/42/a.html')
        rule = r'https://example.test/book/42/.*\.html'
        self.put('content_pattern', rule)
        button(self.window, '开始收集').invoke()
        self.assertEqual(named(self.window, 'content_pattern').get(), rule)
        deadline = time.monotonic() + 5
        while str(button(self.window, '开始收集').cget('state')) == 'disabled' and time.monotonic() < deadline:
            self.root.update(); time.sleep(0.01)

    def test_default_and_expanded_layout(self):
        self.assertFalse(named(self.window, 'advanced').winfo_ismapped())
        self.assertEqual(named(self.window, 'entry_urls').cget('background'), 'white')
        for expand in (False, True):
            if expand: button(self.window, '高级设置（一般不用改）').invoke()
            self.window.update()
            self.assertLessEqual(self.window.winfo_reqheight(), self.window.winfo_height())
            start = button(self.window, '开始收集')
            self.assertLess(start.winfo_rooty() + start.winfo_height(), self.window.winfo_rooty() + self.window.winfo_height() + 2)


def browser_test():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from 网页批量保存 import Browser
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            html = '<html><meta charset="utf-8"><title>本地测试</title><body>'
            if self.path == '/book/42/a.html': html += '<a href="/book/42/b.html">下一章</a>'
            html += '</body></html>'
            data = html.encode()
            self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='link-ui-browser-') as tmp:
        root = tk.Tk(); root.geometry('300x100'); root.update()
        browser = Browser(Path(tmp) / 'chrome', headless=True)
        try:
            for misplaced in [False, True]:
                applied = []
                window = show_dialog(root, browser, '', applied.extend, Path(tmp) / str(misplaced))
                window.update()
                url = f'http://127.0.0.1:{server.server_port}/book/42/a.html'
                if misplaced:
                    button(window, '高级设置（一般不用改）').invoke()
                    named(window, 'content_pattern').insert(0, url)
                else: named(window, 'entry_urls').insert('1.0', url)
                button(window, '开始收集').invoke()
                use = button(window, '使用这份清单 → 主窗口')
                deadline = time.monotonic() + 40
                while str(use.cget('state')) == 'disabled' and time.monotonic() < deadline:
                    root.update(); time.sleep(0.03)
                assert str(use.cget('state')) == 'normal', 'GUI 收集未完成'
                assert not named(window, 'content_pattern').get()
                use.invoke()
                assert applied == [url, url.replace('/a.html', '/b.html')], applied
                for task in root.tk.call('after', 'info'): root.tk.call('after', 'cancel', task)
            print('真实界面 + Chrome：正确输入、误填规则栏均完成两章收集、结果展示和清单应用。')
        finally:
            root.destroy()
            if browser.process:
                browser.process.terminate()
                try: browser.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    browser.process.kill(); browser.process.wait()
            server.shutdown(); server.server_close()


if __name__ == '__main__':
    if '--browser' in sys.argv: browser_test()
    else: unittest.main()
