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
    def __init__(self, profile=None, headless=False):
        self.profile = Path(profile or browser_profile()).resolve()
        self.headless = headless
        self.port = None
        self.process = None

    def ensure(self):
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
                    self.port = port
                    return info['webSocketDebuggerUrl']
            except (OSError, ValueError, KeyError, IndexError):
                pass
        if not CHROME.exists():
            raise RuntimeError('没有找到 Google Chrome，请先安装 Chrome')
        marker.unlink(missing_ok=True)
        args = [str(CHROME), f'--user-data-dir={self.profile}', '--remote-debugging-port=0',
                '--remote-debugging-address=127.0.0.1', '--no-first-run',
                '--no-default-browser-check', '--disable-background-timer-throttling']
        if self.headless:
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
            target = cdp.call('Target.createTarget', {'url': 'about:blank'})['targetId']
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
            call('Network.enable', {'maxTotalBufferSize': 1000000, 'maxResourceBufferSize': 100000})
            call('Page.bringToFront')
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
            details = evaluate('({title:document.title,url:location.href})') or {}
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
            return {'url': url, 'final_url': details.get('url', url), 'title': title,
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
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    class App:
        def __init__(self):
            self.root = tk.Tk()
            self.root.title('网页批量保存 · MHTML / TXT整理')
            self.root.geometry('960x830')
            self.root.minsize(800, 680)
            self.browser = Browser()
            self.stop, self.pause = threading.Event(), threading.Event()
            self.messages = queue.Queue()
            self.busy = False
            self.failed = []
            self.rows = []
            self.destination = tk.StringVar(value=str(output_directory()))
            self.delay = tk.StringVar(value='3')
            self.interval = tk.StringVar(value='1')
            self.timeout = tk.StringVar(value='45')
            self.scroll = tk.BooleanVar(value=True)
            self.status = tk.StringVar(value='粘贴网址，每行一个；准备好后开始保存。')
            outer = ttk.Frame(self.root, padding=22)
            outer.pack(fill='both', expand=True)
            ttk.Label(outer, text='把网页，留在本地。', font=('PingFang SC', 26, 'bold')).pack(anchor='w')
            ttk.Label(outer, text='使用本机 Chrome，逐页保存为单文件 MHTML。',
                      foreground='#666666').pack(anchor='w', pady=(4, 18))
            top = ttk.Frame(outer)
            top.pack(fill='x')
            ttk.Label(top, text='网址清单', font=('PingFang SC', 13, 'bold')).pack(side='left')
            self.import_btn = ttk.Button(top, text='导入 TXT', command=self.import_urls)
            self.import_btn.pack(side='right')
            self.collect_btn = ttk.Button(top, text='自动收集目录 / 分页链接', command=self.collect_links)
            self.collect_btn.pack(side='right', padx=8)
            self.text = ScrolledText(outer, height=8, font=('Menlo', 12), wrap='none', undo=True)
            self.text.pack(fill='both', expand=True, pady=(8, 12))
            pathrow = ttk.Frame(outer)
            pathrow.pack(fill='x', pady=4)
            ttk.Label(pathrow, text='保存到').pack(side='left', padx=(0, 8))
            self.pathentry = ttk.Entry(pathrow, textvariable=self.destination)
            self.pathentry.pack(side='left', fill='x', expand=True)
            self.choose_btn = ttk.Button(pathrow, text='选择目录', command=self.choose)
            self.choose_btn.pack(side='left', padx=(8, 0))
            ttk.Button(pathrow, text='打开目录', command=self.open_folder).pack(side='left', padx=(8, 0))
            settings = ttk.Frame(outer)
            settings.pack(fill='x', pady=(10, 8))
            self.settings = []
            for label, variable in [('加载后等待（秒）', self.delay), ('网页间隔（秒）', self.interval), ('加载超时（秒）', self.timeout)]:
                ttk.Label(settings, text=label).pack(side='left')
                entry = ttk.Entry(settings, width=5, textvariable=variable)
                entry.pack(side='left', padx=(5, 14))
                self.settings.append(entry)
            self.scrollcheck = ttk.Checkbutton(outer, text='保存前自动滚动，尝试加载长页面与懒加载图片（最多 30 秒）', variable=self.scroll)
            self.scrollcheck.pack(anchor='w', pady=(0, 8))
            actions = ttk.Frame(outer)
            actions.pack(fill='x', pady=8)
            self.login_btn = ttk.Button(actions, text='① 打开浏览器 / 先登录', command=self.open_browser)
            self.login_btn.pack(side='left')
            self.start_btn = ttk.Button(actions, text='② 开始批量保存', command=self.start)
            self.start_btn.pack(side='left', padx=8)
            self.pause_btn = ttk.Button(actions, text='暂停', state='disabled', command=self.toggle_pause)
            self.pause_btn.pack(side='left')
            self.stop_btn = ttk.Button(actions, text='停止', state='disabled', command=self.cancel)
            self.stop_btn.pack(side='left', padx=8)
            self.retry_btn = ttk.Button(actions, text='重试失败项', state='disabled', command=lambda: self.start(True))
            self.retry_btn.pack(side='right')
            self.organize_btn = ttk.Button(outer, text='③ 整理已保存网页 → 合并 TXT',
                                           command=self.organize_local)
            self.organize_btn.pack(anchor='w', pady=(2, 6))
            self.progress = ttk.Progressbar(outer, mode='determinate')
            self.progress.pack(fill='x', pady=(8, 6))
            ttk.Label(outer, textvariable=self.status, wraplength=860).pack(anchor='w')
            self.log = ScrolledText(outer, height=8, state='disabled', font=('PingFang SC', 11), wrap='word')
            self.log.pack(fill='both', expand=True, pady=(10, 8))
            ttk.Label(outer, text='登录请使用工具打开的专用 Chrome；首次需重新登录，之后保留。停止会保留已保存文件。',
                      foreground='#666666', wraplength=860).pack(anchor='w')
            self.root.protocol('WM_DELETE_WINDOW', self.close)
            self.root.after(100, self.poll)
            if smoke_test:
                self.root.after(1800, self.root.destroy)
                print('主窗口已初始化，含链接收集及 TXT 整理入口。', flush=True)

        def append(self, value):
            self.log.configure(state='normal')
            self.log.insert('end', value + '\n')
            self.log.see('end')
            self.log.configure(state='disabled')

        def choose(self):
            folder = filedialog.askdirectory(initialdir=self.destination.get())
            if folder:
                self.destination.set(folder)
                try:
                    remember_output(folder)
                except OSError as exc:
                    self.append('未能记住保存目录：' + str(exc))

        def open_folder(self):
            try:
                folder = Path(self.destination.get()).expanduser().resolve()
                folder.mkdir(parents=True, exist_ok=True)
                subprocess.Popen(['open', str(folder)])
            except OSError as exc:
                messagebox.showerror('无法打开目录', str(exc))

        def import_urls(self):
            file = filedialog.askopenfilename(filetypes=[('网址清单', '*.txt'), ('所有文件', '*')])
            if file:
                try:
                    self.text.insert('end', '\n' + read_text(file))
                except (OSError, UnicodeError, ValueError) as exc:
                    messagebox.showerror('无法导入', str(exc))

        def controls(self, busy):
            self.busy = busy
            for widget in [self.import_btn, self.choose_btn, self.pathentry, self.start_btn,
                           self.login_btn, self.scrollcheck, self.organize_btn, self.collect_btn, *self.settings]:
                widget.configure(state='disabled' if busy else 'normal')
            self.text.configure(state='disabled' if busy else 'normal')
            self.pause_btn.configure(state='normal' if busy else 'disabled', text='暂停')
            self.stop_btn.configure(state='normal' if busy else 'disabled')
            self.retry_btn.configure(state='normal' if self.failed and not busy else 'disabled')

        def organize_local(self):
            from 本地正文整理 import show_dialog
            show_dialog(self.root, self.destination.get())

        def collect_links(self):
            from 网页链接收集 import show_dialog

            def apply_urls(urls):
                self.text.delete('1.0', 'end')
                self.text.insert('1.0', '\n'.join(urls))
                self.status.set(f'已放入 {len(urls)} 个链接；可开始批量保存。')

            show_dialog(self.root, self.browser, self.text.get('1.0', 'end').strip(),
                        apply_urls, self.destination.get())

        def open_browser(self):
            urls, _ = parse_urls(self.text.get('1.0', 'end'))
            self.controls(True)
            self.pause_btn.configure(state='disabled')
            self.stop_btn.configure(state='disabled')
            def work():
                try:
                    cdp = CDP(self.browser.ensure())
                    try:
                        target = cdp.call('Target.createTarget', {'url': urls[0] if urls else 'about:blank'})['targetId']
                        cdp.call('Target.activateTarget', {'targetId': target})
                    finally:
                        cdp.close()
                    self.messages.put(('log', '专用 Chrome 已打开。可在其中登录所需网站，完成后回到这里开始保存。'))
                except Exception as exc:
                    self.messages.put(('log', '浏览器打开失败：' + str(exc)))
                finally:
                    self.messages.put(('browser_done', None))
            threading.Thread(target=work, daemon=True).start()

        def start(self, retry=False):
            urls, invalid = parse_urls(self.text.get('1.0', 'end'))
            if retry:
                urls, invalid = list(self.failed), []
            if invalid:
                messagebox.showerror('请先修正网址', '\n'.join(invalid[:12]) + '\n\n每行仅填写一个 http/https 网址。')
                return
            if not urls:
                messagebox.showinfo('请填写网址', '请粘贴网址，每行一个，或导入 TXT。')
                return
            try:
                delay, interval, timeout = float(self.delay.get()), float(self.interval.get()), float(self.timeout.get())
                if not (0 <= delay <= 120 and 0 <= interval <= 120 and 5 <= timeout <= 300):
                    raise ValueError()
                folder = Path(self.destination.get()).expanduser().resolve()
                folder.mkdir(parents=True, exist_ok=True)
                # 提前检测可写性，避免浏览完才发现没有目录权限。
                probe = folder / ('.write-check-' + os.urandom(8).hex())
                with probe.open('xb'):
                    pass
                probe.unlink()
            except (ValueError, OSError):
                messagebox.showerror('检查设置', '等待/间隔填写 0–120 秒，超时填写 5–300 秒，并选择可写的保存目录。')
                return
            scroll = self.scroll.get()
            try:
                remember_output(folder)
            except OSError as exc:
                self.append('未能记住保存目录：' + str(exc))
            self.stop.clear()
            self.pause.clear()
            self.failed, self.rows = [], []
            self.controls(True)
            self.progress.configure(maximum=len(urls), value=0)
            self.status.set(f'准备保存 {len(urls)} 个网页…')
            self.append(f'开始新批次：{len(urls)} 个去重后的网址 → {folder}')
            def work():
                rows = []
                run_id = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
                saver = Saver(self.browser, self.stop, self.pause, lambda x: self.messages.put(('log', x)))
                try:
                    for i, url in enumerate(urls, 1):
                        saver.wait()
                        self.messages.put(('status', f'{i} / {len(urls)}  正在保存：{url}'))
                        try:
                            row = saver.save_one(url, folder, delay, scroll, timeout)
                            self.messages.put(('log', f'✓ {i}/{len(urls)}  {row["file"]}'))
                        except Cancelled:
                            raise
                        except Exception as exc:
                            row = {'url': url, 'status': '失败', 'error': str(exc)}
                            self.messages.put(('log', f'✗ {i}/{len(urls)}  {url}\n    {exc}'))
                        rows.append(row)
                        # 每页刷新清单，意外关闭时仍可检查已完成结果。
                        write_report(folder, rows, run_id)
                        self.messages.put(('progress', i))
                        if i < len(urls):
                            saver.wait(interval)
                except Cancelled:
                    self.messages.put(('log', '已停止。已保存文件保留，未处理网址仍留在输入框。'))
                except Exception as exc:
                    self.messages.put(('log', '任务中断：' + str(exc)))
                finally:
                    self.messages.put(('done', (rows, len(urls))))
            threading.Thread(target=work, daemon=True).start()

        def toggle_pause(self):
            if self.pause.is_set():
                self.pause.clear()
                self.pause_btn.configure(text='暂停')
                self.append('继续保存。')
            else:
                self.pause.set()
                self.pause_btn.configure(text='继续')
                self.append('已请求暂停，将在当前浏览器操作完成后暂停；页面本身仍可加载。')

        def cancel(self):
            self.stop.set()
            self.pause.clear()
            self.stop_btn.configure(state='disabled')
            self.pause_btn.configure(state='disabled')
            self.status.set('正在停止…')

        def poll(self):
            try:
                while True:
                    kind, value = self.messages.get_nowait()
                    if kind == 'log':
                        self.append(value)
                    elif kind == 'status':
                        self.status.set(value)
                    elif kind == 'progress':
                        self.progress.configure(value=value)
                    elif kind == 'browser_done':
                        self.controls(False)
                    elif kind == 'done':
                        self.rows, total = value
                        self.failed = [r['url'] for r in self.rows if r['status'] == '失败']
                        success = sum(r['status'] == '成功' for r in self.rows)
                        self.status.set(f'成功 {success} 个 · 失败 {len(self.failed)} 个 · 未处理 {total-len(self.rows)} 个')
                        self.controls(False)
            except queue.Empty:
                pass
            self.root.after(100, self.poll)

        def close(self):
            if self.busy:
                self.cancel()
                self.status.set('停止任务后关闭窗口…')
                self.root.after(150, self.close)
                return
            self.root.destroy()

    App().root.mainloop()


def main():
    parser = argparse.ArgumentParser(description='使用本机 Chrome 批量保存完整网页为 MHTML；无参数打开图形界面。')
    parser.add_argument('--urls', type=Path, help='每行一个网址的 TXT 文件')
    parser.add_argument('--output', type=Path, default=output_directory())
    parser.add_argument('--wait', type=float, default=3, help='加载后等待秒数（0–120）')
    parser.add_argument('--interval', type=float, default=1, help='页面之间间隔秒数（0–120）')
    parser.add_argument('--timeout', type=float, default=45, help='网页加载超时秒数（5–300）')
    parser.add_argument('--no-scroll', action='store_true')
    parser.add_argument('--headless', action='store_true', help='使用无界面 Chrome，首次登录建议使用图形界面')
    parser.add_argument('--profile', type=Path, help='独立 Chrome 数据目录；默认使用本工具的专用浏览器数据')
    parser.add_argument('--organize', type=Path, help='仅整理指定文件夹内的本地HTML/MHTML，不启动浏览器')
    parser.add_argument('--organized-output', type=Path, help='整理结果的父目录，默认放在源文件夹内的“整理结果”')
    parser.add_argument('--text-title', default='网页正文', help='合并TXT标题')
    parser.add_argument('--content-selector', default='自动', help='正文区域：自动、#ID、.类名或标签')
    parser.add_argument('--keep-linebreaks', action='store_true', help='保留网页折行，不进行断行修复')
    parser.add_argument('--chapter-order', action='store_true', help='按章号排序；默认优先采用已保存的原目录顺序')
    parser.add_argument('--no-merge-pages', action='store_true', help='不将本地续页并入原章节')
    parser.add_argument('--keep-page-notices', action='store_true', help='保留正文区域里的独立分页提示')
    parser.add_argument('--order-reference', type=Path, help='旧版提取校验.json，缺少目录网页时用于恢复原顺序')
    parser.add_argument('--ui-smoke-test', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.organize:
        from 本地正文整理 import organize
        try:
            result = organize(args.organize, args.organized_output, title=args.text_title,
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
