#!/usr/bin/env python3
"""真实 Chrome 集成自检：本机测试页，不使用任何账号或外部网站。"""
import email
from email import policy
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('saver', BASE.parent / 'src/网页批量保存.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/page')
            self.end_headers()
            return
        if self.path == '/slow':
            time.sleep(8)
        status, content_type = 200, 'text/html; charset=utf-8'
        if self.path == '/missing':
            status, body = 404, '<h1>没有此页面</h1>'
        elif self.path == '/image.svg':
            content_type = 'image/svg+xml'
            body = '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="red"/></svg>'
        elif self.path == '/frame':
            body = '<html><body>嵌入页面内容</body></html>'
        else:
            body = '''<!doctype html><html><head><meta charset="utf-8"><title>本地归档/中文:测试</title></head>
            <body><h1>中文网页完整保存</h1><p id="dynamic"></p><img id="first" src="/image.svg">
            <iframe src="/frame"></iframe><div style="height:1800px">长页面</div>
            <img id="lazy" loading="lazy" src="/image.svg?lazy"><p>页尾内容</p>
            <script>setTimeout(()=>document.querySelector('#dynamic').textContent='动态加载已完成',100)</script>
            </body></html>'''
        if self.path.startswith('/image.svg'):
            content_type = 'image/svg+xml'
            body = '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="red"/></svg>'
        data = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = Path(tempfile.mkdtemp(prefix='.功能自检-', dir=BASE))
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        cls.browser = mod.Browser(cls.temp / 'profile', headless=True)
        cls.browser.ensure()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        try:
            cdp = mod.CDP(cls.browser.ensure())
            try:
                cdp.call('Browser.close', timeout=3)
            finally:
                cdp.close()
        except Exception:
            pass
        if cls.browser.process:
            try:
                cls.browser.process.wait(timeout=8)
            except Exception:
                cls.browser.process.terminate()
                cls.browser.process.wait(timeout=5)
        shutil.rmtree(cls.temp)

    def test_01_urls(self):
        urls, invalid = mod.parse_urls('EXAMPLE.com\nhttps://example.com/\n#注释\n\nhttp://localhost:80/a#b\nftp://bad\nhttps://has space\nhttp://host:99999')
        self.assertEqual(urls, ['https://example.com/', 'http://localhost/a#b'])
        self.assertEqual(len(invalid), 3)
        self.assertNotIn('/', mod.safe_name('中文/标题:*?'))

    def test_02_archive_and_offline(self):
        saver = mod.Saver(self.browser)
        row = saver.save_one(self.url + '/redirect', self.temp, wait=0.3, scroll=True, timeout=10)
        path = self.temp / row['file']
        archive = email.message_from_bytes(path.read_bytes(), policy=policy.default)
        parts = list(archive.walk())
        html = '\n'.join(part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8')
                         for part in parts if part.get_content_type() == 'text/html')
        self.assertIn('中文网页完整保存', html)
        self.assertIn('动态加载已完成', html)
        self.assertIn('嵌入页面内容', html)
        self.assertEqual(sum(p.get_content_type() == 'image/svg+xml' for p in parts), 2)
        self.assertEqual(row['final_url'], self.url + '/page')
        row2 = saver.save_one(self.url + '/redirect', self.temp, wait=0, scroll=False, timeout=10)
        self.assertNotEqual(row['file'], row2['file'])
        # 断网模拟：Chrome Network.emulateNetworkConditions，重开归档确认图片来自归档。
        cdp = mod.CDP(self.browser.ensure())
        target = cdp.call('Target.createTarget', {'url': 'about:blank'})['targetId']
        try:
            session = cdp.call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
            cdp.call('Network.enable', session=session)
            cdp.call('Network.emulateNetworkConditions', {'offline': True, 'latency': 0,
                     'downloadThroughput': 0, 'uploadThroughput': 0}, session=session)
            cdp.call('Page.navigate', {'url': path.as_uri()}, session=session)
            for _ in range(50):
                result = cdp.call('Runtime.evaluate', {'expression': '''({text:document.body?.innerText,
                  images:[...document.images].map(i=>i.complete && i.naturalWidth>0)})''', 'returnByValue': True}, session=session)
                value = result.get('result', {}).get('value', {})
                if value.get('images') and all(value['images']):
                    break
                time.sleep(0.1)
            self.assertIn('动态加载已完成', value['text'])
            self.assertEqual(value['images'], [True, True])
        finally:
            cdp.call('Target.closeTarget', {'targetId': target})
            cdp.close()
        mod.write_report(self.temp, [row, {'url': self.url + '/missing', 'status': '失败', 'error': 'HTTP 404'}], 'test')
        self.assertIn(self.url + '/missing', (self.temp / '失败网址_test.txt').read_text())

    def test_03_http_error(self):
        with self.assertRaisesRegex(RuntimeError, 'HTTP 404'):
            mod.Saver(self.browser).save_one(self.url + '/missing', self.temp, wait=0, scroll=False, timeout=10)

    def test_04_cancel_and_cleanup(self):
        stop = threading.Event()
        cdp = mod.CDP(self.browser.ensure())
        before = cdp.call('Target.getTargets')['targetInfos']
        cdp.close()
        timer = threading.Timer(0.7, stop.set)
        timer.start()
        start = time.monotonic()
        try:
            with self.assertRaises(mod.Cancelled):
                mod.Saver(self.browser, stop).save_one(self.url + '/slow', self.temp, wait=0, scroll=False)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - start, 4)
        cdp = mod.CDP(self.browser.ensure())
        after = cdp.call('Target.getTargets')['targetInfos']
        cdp.close()
        self.assertEqual(len(before), len(after))

    def test_05_real_batch_command(self):
        urls = self.temp / 'urls.txt'
        urls.write_text(self.url + '/page\n' + self.url + '/missing\n' + self.url + '/page\n')
        output = self.temp / 'batch'
        result = subprocess.run([sys.executable, str(BASE / '网页批量保存.py'), '--urls', str(urls),
                '--output', str(output), '--profile', str(self.browser.profile), '--headless',
                '--wait', '0.2', '--interval', '0', '--no-scroll'], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(len(list(output.glob('*.mhtml'))), 1)
        with next(output.glob('保存记录_*.csv')).open(encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([row['status'] for row in rows], ['成功', '失败'])
        self.assertEqual(next(output.glob('失败网址_*.txt')).read_text().strip(), self.url + '/missing')


if __name__ == '__main__':
    unittest.main(verbosity=2)
