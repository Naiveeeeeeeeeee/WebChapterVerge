#!/usr/bin/env python3
"""macOS Chrome 网页批量归档。仅依赖 Python 标准库和本机 Chrome。"""
from __future__ import annotations

import argparse
import base64
from collections import deque
import csv
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import socket
import struct
import subprocess
import threading
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import build_opener, ProxyHandler
from 应用路径 import output_directory, browser_profile, remember_output

BASE = Path(__file__).resolve().parent
CHROME = next((p for p in [
    Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
    Path.home() / 'Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
] if p.exists()), Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'))
HTTP = build_opener(ProxyHandler({}))  # 本机调试连接不经过代理


class Cancelled(Exception):
    pass


def parse_urls(text):
    urls, invalid, seen = [], [], set()
    for number, line in enumerate(text.splitlines(), 1):
        raw = line.strip()
        if not raw or raw.startswith('#'):
            continue
        try:
            value = raw if '://' in raw else 'https://' + raw
            p = urlsplit(value)
            if (p.scheme.lower() not in ('http', 'https') or not p.hostname
                    or re.search(r'\s', value) or p.username or p.password):
                raise ValueError()
            port = p.port  # 校验端口
            host = p.hostname.lower()
            if ':' in host:
                host = '[' + host + ']'
            if port and (p.scheme.lower(), port) not in (('http', 80), ('https', 443)):
                host += ':' + str(port)
            value = urlunsplit((p.scheme.lower(), host, p.path or '/', p.query, p.fragment))
            if value not in seen:
                seen.add(value)
                urls.append(value)
        except ValueError:
            invalid.append(f'第 {number} 行：{raw}')
    return urls, invalid


def safe_name(title):
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', '_', title).strip(' .') or '未命名网页'
    while len(name.encode('utf-8')) > 150:
        name = name[:-1]
    return name


class CDP:
    """最小同步 WebSocket/CDP 客户端，支持分片、ping 和浏览器会话。"""
    def __init__(self, url, cancel=None):
        p = urlsplit(url)
        if p.scheme != 'ws' or p.hostname not in ('127.0.0.1', 'localhost'):
            raise RuntimeError('只允许连接本机 Chrome')
        self.sock = socket.create_connection((p.hostname, p.port), timeout=5)
        self.buffer = bytearray()
        self.serial = 0
        self.events = deque(maxlen=3000)
        self.cancel = cancel
        key = base64.b64encode(os.urandom(16)).decode()
        request = (f'GET {p.path} HTTP/1.1\r\nHost: {p.netloc}\r\n'
                   'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                   f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n')
        self.sock.sendall(request.encode('ascii'))
        header = bytearray()
        while b'\r\n\r\n' not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError('Chrome 调试连接中断')
            header.extend(chunk)
        head, tail = bytes(header).split(b'\r\n\r\n', 1)
        self.buffer.extend(tail)
        expected = base64.b64encode(hashlib.sha1(
            (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        headers = {k.strip().lower(): v.strip() for line in head.split(b'\r\n')[1:]
                   if b':' in line for k, v in [line.split(b':', 1)]}
        if b' 101 ' not in head.split(b'\r\n')[0] or headers.get(b'sec-websocket-accept') != expected:
            self.close()
            raise RuntimeError('Chrome 调试连接握手失败')

    def close(self):
        self.sock.close()

    def _send(self, data, opcode=1):
        mask = os.urandom(4)
        n = len(data)
        header = bytes([0x80 | opcode])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack('!H', n)
        else:
            header += bytes([0x80 | 127]) + struct.pack('!Q', n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(header + mask + masked)

    def _read(self, n, deadline):
        while len(self.buffer) < n:
            if self.cancel and self.cancel.is_set():
                raise Cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('等待 Chrome 响应超时')
            self.sock.settimeout(min(0.4, remaining))
            try:
                chunk = self.sock.recv(max(8192, min(n - len(self.buffer), 1024 * 1024)))
            except socket.timeout:
                continue
            if not chunk:
                raise RuntimeError('专用 Chrome 窗口已关闭，请重新打开')
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:n])
        del self.buffer[:n]
        return result

    def _message(self, deadline):
        fragments = bytearray()
        while True:
            first, second = self._read(2, deadline)
            opcode, length = first & 15, second & 127
            if length == 126:
                length = struct.unpack('!H', self._read(2, deadline))[0]
            elif length == 127:
                length = struct.unpack('!Q', self._read(8, deadline))[0]
            if length > 512 * 1024 * 1024:
                raise RuntimeError('网页归档超过单次 512 MB 限制')
            mask = self._read(4, deadline) if second & 128 else None
            data = self._read(length, deadline)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 8:
                raise RuntimeError('Chrome 已断开连接')
            if opcode == 9:
                self._send(data, 10)
                continue
            if opcode == 10:
                continue
            fragments.extend(data)
            if first & 128:
                return json.loads(fragments)

    def call(self, method, params=None, session=None, timeout=30):
        self.serial += 1
        request = {'id': self.serial, 'method': method, 'params': params or {}}
        if session:
            request['sessionId'] = session
        self.sock.settimeout(5)
        self._send(json.dumps(request).encode('utf-8'))
        deadline = time.monotonic() + timeout
        while True:
            result = self._message(deadline)
            if result.get('id') == self.serial:
                if 'error' in result:
                    raise RuntimeError(result['error'].get('message', str(result['error'])))
                return result.get('result', {})
            if 'method' in result:
                self.events.append(result)


class Browser:
    def __init__(self, profile=None, headless=True):
        self.profile = Path(profile or browser_profile()).resolve()
        self.headless = headless
        self.port = None
        self.process = None
        self.lock = threading.RLock()

    def ensure(self, headless=None):
        with self.lock:
            return self._ensure(self.headless if headless is None else headless)

    def shutdown(self):
        """只退出本工具专用配置的浏览器，不新启动进程。"""
        with self.lock:
            marker = self.profile / 'DevToolsActivePort'
            try:
                lines = marker.read_text().splitlines()
                with HTTP.open(f'http://127.0.0.1:{int(lines[0])}/json/version', timeout=1) as response:
                    info = json.load(response)
                if urlsplit(info['webSocketDebuggerUrl']).path != lines[1]:
                    return
                cdp = CDP(info['webSocketDebuggerUrl'])
                try:
                    cdp.call('Browser.close', timeout=3)
                except (OSError, RuntimeError):
                    pass
                finally:
                    cdp.close()
                if self.process:
                    try:
                        self.process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        pass
            except (OSError, ValueError, KeyError, IndexError):
                pass

    def _ensure(self, headless):
        self.profile.mkdir(parents=True, exist_ok=True)
        os.chmod(self.profile, 0o700)
        marker = self.profile / 'DevToolsActivePort'
        # 仅连接本工具的专用配置；不会读取用户日常 Chrome 的 Cookie。
        if marker.exists():
            try:
                lines = marker.read_text().splitlines()
                port = int(lines[0])
                with HTTP.open(f'http://127.0.0.1:{port}/json/version', timeout=1) as response:
                    info = json.load(response)
                if urlsplit(info['webSocketDebuggerUrl']).path == lines[1]:
                    running_headless = 'HeadlessChrome' in info.get('User-Agent', '')
                    if running_headless == headless:
                        self.port = port
                        return info['webSocketDebuggerUrl']
                    # 仅切换本工具的专用配置，保留登录状态；不操作用户日常 Chrome。
                    cdp = CDP(info['webSocketDebuggerUrl'])
                    try:
                        cdp.call('Browser.close', timeout=5)
                    except (OSError, RuntimeError):
                        pass
                    finally:
                        cdp.close()
                    for _ in range(150):
                        if not (self.profile / 'SingletonLock').is_symlink():
                            break
                        time.sleep(0.1)
                    else:
                        raise RuntimeError('专用浏览器仍在退出，请稍后重试')
            except (OSError, ValueError, KeyError, IndexError):
                pass
        if not CHROME.exists():
            raise RuntimeError('没有找到 Google Chrome，请先安装 Chrome')
        marker.unlink(missing_ok=True)
        args = [str(CHROME), f'--user-data-dir={self.profile}', '--remote-debugging-port=0',
                '--remote-debugging-address=127.0.0.1', '--no-first-run',
                '--no-default-browser-check', '--disable-background-timer-throttling']
        if headless:
            args += ['--headless=new']
        args += ['about:blank']
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(150):
            if marker.exists():
                try:
                    self.port = int(marker.read_text().splitlines()[0])
                    with HTTP.open(f'http://127.0.0.1:{self.port}/json/version', timeout=1) as response:
                        return json.load(response)['webSocketDebuggerUrl']
                except (OSError, ValueError, IndexError):
                    pass
            if self.process.poll() is not None:
                raise RuntimeError('Chrome 启动失败。请关闭本工具的专用 Chrome 窗口后重试')
            time.sleep(0.1)
        raise RuntimeError('Chrome 启动超时，请关闭本工具的专用 Chrome 后重试')


class Saver:
    def __init__(self, browser, stop=None, pause=None, log=None):
        self.browser = browser
        self.stop = stop or threading.Event()
        self.pause = pause or threading.Event()
        self.log = log or (lambda text: None)

    def wait(self, seconds=0):
        deadline = time.monotonic() + seconds
        while True:
            if self.stop.is_set():
                raise Cancelled()
            if self.pause.is_set():
                paused = time.monotonic()
                while self.pause.is_set():
                    if self.stop.wait(0.1):
                        raise Cancelled()
                deadline += time.monotonic() - paused
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.stop.wait(min(0.1, remaining))

    def save_one(self, url, destination, wait=3, scroll=True, timeout=45):
        self.wait()
        cdp = CDP(self.browser.ensure(), self.stop)
        target = None
        try:
            target = cdp.call('Target.createTarget', {'url': 'about:blank', 'background': True})['targetId']
            session = cdp.call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
            def call(method, params=None, limit=15):
                self.wait()
                return cdp.call(method, params, session, limit)
            def evaluate(js, limit=15):
                result = call('Runtime.evaluate', {'expression': js, 'returnByValue': True}, limit)
                if 'exceptionDetails' in result:
                    raise RuntimeError('网页脚本执行失败')
                return result.get('result', {}).get('value')
            call('Page.enable')
            # 在浏览器内部模拟页面活跃，维持懒加载；不会激活 macOS 窗口或抢键盘焦点。
            call('Emulation.setFocusEmulationEnabled', {'enabled': True})
            call('Network.enable', {'maxTotalBufferSize': 1000000, 'maxResourceBufferSize': 100000})
            navigation = call('Page.navigate', {'url': url}, timeout)
            if navigation.get('errorText'):
                raise RuntimeError('网页打开失败：' + navigation['errorText'])
            deadline = time.monotonic() + timeout
            while True:
                self.wait()
                try:
                    ready = evaluate('({state:document.readyState,url:location.href})', 5)
                    if ready and ready['state'] == 'complete' and ready['url'] != 'about:blank':
                        break
                except RuntimeError as exc:
                    if not any(part in str(exc) for part in ('context', 'navigat')):
                        raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f'网页在 {timeout} 秒内未加载完；可调大超时后重试')
                self.wait(0.2)
            self.wait(wait)
            if scroll:
                self.log('滚动加载图片…')
                scroll_start = time.monotonic()
                for _ in range(100):
                    position = evaluate('''(() => {
                        const d=document.scrollingElement || document.documentElement;
                        window.scrollBy(0, Math.max(500, innerHeight * 0.85));
                        return {bottom:scrollY+innerHeight>=d.scrollHeight-4,height:d.scrollHeight};
                    })()''')
                    self.wait(0.22)
                    if position and position['bottom']:
                        self.wait(0.5)
                        height = evaluate('(document.scrollingElement || document.documentElement).scrollHeight')
                        if height <= position['height'] + 4:
                            break
                    if time.monotonic() - scroll_start >= 30:
                        self.log('滚动已达 30 秒上限，将保存已加载内容')
                        break
                evaluate('window.scrollTo(0,0)')
                self.wait(max(0.5, wait))
            details = evaluate('''({title:document.title,url:location.href,
                metadata:Object.fromEntries([...document.querySelectorAll('meta[name],meta[property]')]
                    .map(m=>[m.getAttribute('property')||m.name,m.content]))})''') or {}
            if details.get('url', '').startswith(('chrome-error:', 'about:blank')):
                raise RuntimeError('浏览器显示错误页，未保存')
            for event in reversed(cdp.events):
                p = event.get('params', {})
                if (event.get('sessionId') == session and event.get('method') == 'Network.responseReceived'
                        and p.get('loaderId') == navigation.get('loaderId')
                        and p.get('frameId') == navigation.get('frameId') and p.get('type') == 'Document'):
                    status = int(p['response']['status'])
                    if status >= 400:
                        raise RuntimeError(f'服务器返回 HTTP {status}，未保存错误页')
                    break
            self.log('生成 MHTML…')
            data = call('Page.captureSnapshot', {'format': 'mhtml'}, 90).get('data', '')
            if 'multipart/related' not in data[:3000].lower():
                raise RuntimeError('Chrome 返回的内容不是有效 MHTML')
            self.wait()
            folder = Path(destination)
            folder.mkdir(parents=True, exist_ok=True)
            title = details.get('title') or urlsplit(url).hostname or '网页'
            stem = safe_name(title) + '_' + hashlib.sha256(url.encode()).hexdigest()[:8]
            path = folder / (stem + '.mhtml')
            index = 2
            # 排他创建，重名不覆盖任何已有归档。
            while True:
                try:
                    stream = path.open('xb')
                    break
                except FileExistsError:
                    path = folder / f'{stem}_{index}.mhtml'
                    index += 1
            try:
                with stream:
                    stream.write(data.encode('utf-8'))
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            from 书籍任务 import infer_book_title
            return {'url': url, 'final_url': details.get('url', url), 'title': title,
                    'book_title': infer_book_title(title, details.get('metadata')),
                    'file': path.name, 'bytes': path.stat().st_size, 'status': '成功',
                    'error': '', 'saved_at': datetime.now().astimezone().isoformat(timespec='seconds')}
        finally:
            # 用户暂停/停止时也回收本工具新建的页面，不动手动打开的登录页。
            if target:
                cdp.cancel = None
                try:
                    cdp.call('Target.closeTarget', {'targetId': target}, timeout=3)
                except Exception:
                    pass
            cdp.close()


def write_report(folder, rows, run_id):
    path = Path(folder) / f'保存记录_{run_id}.csv'
    fields = ['status', 'title', 'url', 'final_url', 'file', 'bytes', 'error', 'saved_at']
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            # 网址/标题来自网页，阻止 Excel 把它们当作公式执行。
            writer.writerow({k: "'" + str(v) if isinstance(v, str) and v.startswith(('=', '+', '-', '@'))
                             else v for k, v in row.items() if k in fields})
    temporary.replace(path)
    failures = Path(folder) / f'失败网址_{run_id}.txt'
    failed = [r['url'] for r in rows if r['status'] == '失败']
    if failed:
        failures.write_text('\n'.join(failed) + '\n', encoding='utf-8')
    else:
        failures.unlink(missing_ok=True)


def read_text(path):
    raw = Path(path).read_bytes()
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError('网址清单请控制在 5 MB 内')
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return raw.decode('gb18030')


def gui(smoke_test=False):
    from 桌面界面 import run
    run(smoke_test=smoke_test)


def main():
    parser = argparse.ArgumentParser(description='使用本机 Chrome 批量保存完整网页为 MHTML；无参数打开图形界面。')
    parser.add_argument('--urls', type=Path, help='每行一个网址的 TXT 文件')
    parser.add_argument('--output', type=Path, default=output_directory())
    parser.add_argument('--wait', type=float, default=3, help='加载后等待秒数（0–120）')
    parser.add_argument('--interval', type=float, default=1, help='页面之间间隔秒数（0–120）')
    parser.add_argument('--timeout', type=float, default=45, help='网页加载超时秒数（5–300）')
    parser.add_argument('--no-scroll', action='store_true')
    parser.add_argument('--headless', action='store_true', default=True, help='默认后台运行 Chrome')
    parser.add_argument('--visible-browser', dest='headless', action='store_false', help='显式使用可见 Chrome，默认不显示窗口')
    parser.add_argument('--profile', type=Path, help='独立 Chrome 数据目录；默认使用本工具的专用浏览器数据')
    parser.add_argument('--collect-links', nargs='+', metavar='URL', help='收集目录或章节入口后的链接，不保存正文')
    parser.add_argument('--link-report', type=Path, help='链接收集结果 JSON 的保存位置')
    parser.add_argument('--link-pattern', default='', help='正文网址匹配正则，留空使用默认规则')
    parser.add_argument('--max-pages', type=int, default=500, help='链接收集最多检查页面数，1–5000')
    parser.add_argument('--organize', type=Path, help='仅整理指定文件夹内的本地HTML/MHTML，不启动浏览器')
    parser.add_argument('--organized-output', type=Path, help='整理结果的父目录，默认放在源文件夹内的“整理结果”')
    parser.add_argument('--text-title', default='', help='小说名称，留空从网页识别')
    parser.add_argument('--full-report', action='store_true', help='开发检查：生成完整诊断目录，默认只导出书名 TXT')
    parser.add_argument('--content-selector', default='自动', help='正文区域：自动、#ID、.类名或标签')
    parser.add_argument('--keep-linebreaks', action='store_true', help='保留网页折行，不进行断行修复')
    parser.add_argument('--chapter-order', action='store_true', help='按章号排序；默认优先采用已保存的原目录顺序')
    parser.add_argument('--no-merge-pages', action='store_true', help='不将本地续页并入原章节')
    parser.add_argument('--keep-page-notices', action='store_true', help='保留正文区域里的独立分页提示')
    parser.add_argument('--order-reference', type=Path, help='旧版提取校验.json，缺少目录网页时用于恢复原顺序')
    parser.add_argument('--ui-smoke-test', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.collect_links:
        from 网页链接收集 import collect, BrowserLinkReader
        stop = threading.Event()
        result = collect(args.collect_links, BrowserLinkReader(Browser(args.profile, args.headless), stop),
                         content_pattern=args.link_pattern, max_pages=args.max_pages, stop=stop)
        report = json.dumps(result, ensure_ascii=False, indent=2)
        if args.link_report:
            args.link_report.parent.mkdir(parents=True, exist_ok=True)
            args.link_report.write_text(report, encoding='utf-8')
        print(report)
        raise SystemExit(0 if result['complete'] else 1)
    if args.organize:
        from 本地正文整理 import organize
        from 书籍任务 import export_text, text_output_directory
        try:
            operation = organize if args.full_report else export_text
            result = operation(args.organize, args.organized_output or text_output_directory(), title=args.text_title,
                               selector=args.content_selector, fix_breaks=not args.keep_linebreaks,
                               original_order=not args.chapter_order, merge_pages=not args.no_merge_pages,
                               clean_notices=not args.keep_page_notices, order_reference=args.order_reference)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if not args.urls:
        gui(smoke_test=args.ui_smoke_test)
        return
    if not (0 <= args.wait <= 120 and 0 <= args.interval <= 120 and 5 <= args.timeout <= 300):
        parser.error('等待/间隔须为 0–120 秒，超时须为 5–300 秒')
    urls, invalid = parse_urls(read_text(args.urls))
    if invalid or not urls:
        parser.error('\n'.join(invalid) or '网址清单为空')
    args.output.mkdir(parents=True, exist_ok=True)
    saver = Saver(Browser(args.profile, args.headless), log=print)
    rows = []
    run_id = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    interrupted = False
    try:
        for index, url in enumerate(urls, 1):
            print(f'[{index}/{len(urls)}] {url}', flush=True)
            try:
                row = saver.save_one(url, args.output, args.wait, not args.no_scroll, args.timeout)
            except Exception as exc:
                row = {'url': url, 'status': '失败', 'error': str(exc)}
            rows.append(row)
            write_report(args.output, rows, run_id)
            print(row['status'] + ' ' + row.get('file', row.get('error', '')), flush=True)
            if index < len(urls):
                time.sleep(args.interval)
    except KeyboardInterrupt:
        interrupted = True
        print('\n已停止，已保存文件保留。')
    raise SystemExit(130 if interrupted else 1 if any(r['status'] == '失败' for r in rows) else 0)


if __name__ == '__main__':
    main()
