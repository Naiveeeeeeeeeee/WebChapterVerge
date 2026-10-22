"""面向小说的收集、后台缓存与单 TXT 导出界面。"""
from pathlib import Path
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

from 应用路径 import DATA, output_directory
from 界面主题 import setup_theme, BG, CARD, INK, MUTED, BLUE, NAV
from 书籍任务 import BookTask, detect_local_title, export_text, cleanup_export, text_output_directory, remember_text_output
from 网页批量保存 import Browser, CDP, Saver, Cancelled, parse_urls, read_text


class NovelApp:
    def __init__(self, root=None, smoke_test=False):
        self.root = root or tk.Tk()
        self.root.title('WebChapterVerge · 网页小说整理')
        self.root.geometry('1120x820'); self.root.minsize(1000, 800)
        setup_theme(self.root)
        self.browser = Browser(headless=True)
        self.events = queue.Queue(); self.stop = threading.Event(); self.pause = threading.Event()
        self.busy = False; self.closing = False; self.task = None; self.result = None; self.local_source = None
        self.title = tk.StringVar(); self.destination = tk.StringVar(value=str(text_output_directory()))
        self.status = tk.StringVar(value='粘贴目录或第一章的网址，从这里开始。')
        self.cache_status = tk.StringVar(value='尚未保存网页')
        self.delay = tk.StringVar(value='1'); self.interval = tk.StringVar(value='0.5'); self.timeout = tk.StringVar(value='45')
        self.scroll = tk.BooleanVar(value=False); self.selector = tk.StringVar(value='自动')
        self.controls = []
        self.build()
        self.refresh_tasks()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.timer = self.root.after(100, self.poll)
        if smoke_test:
            self.root.after(1500, self.close)
            print('新版桌面界面初始化成功。', flush=True)

    def build(self):
        nav = tk.Frame(self.root, bg=NAV, width=220)
        nav.pack(side='left', fill='y'); nav.pack_propagate(False)
        tk.Label(nav, text='WebChapter\nVerge', bg=NAV, fg='white', font=('Helvetica Neue', 25, 'bold'), justify='left').pack(anchor='w', padx=24, pady=(32, 8))
        tk.Label(nav, text='网页小说 · 本地 TXT', bg=NAV, fg='#a9bbd6', font=('PingFang SC', 11)).pack(anchor='w', padx=24)
        tk.Frame(nav, bg='#2b3a50', height=1).pack(fill='x', padx=24, pady=25)
        for number, title, detail in [('01', '收集章节', '目录、下一章与章内分页'), ('02', '后台保存', '每本小说独立缓存'), ('03', '导出 TXT', '书名命名 · 只输出正文')]:
            row = tk.Frame(nav, bg=NAV); row.pack(fill='x', padx=24, pady=(0, 23))
            tk.Label(row, text=number, bg=NAV, fg='#8ea4ff', font=('Helvetica Neue', 11, 'bold')).pack(anchor='w')
            tk.Label(row, text=title, bg=NAV, fg='white', font=('PingFang SC', 14, 'bold')).pack(anchor='w', pady=(4, 2))
            tk.Label(row, text=detail, bg=NAV, fg='#9eafc8', font=('PingFang SC', 10)).pack(anchor='w')
        tk.Label(nav, text='最近的小说任务', bg=NAV, fg='#a9bbd6', font=('PingFang SC', 11)).pack(anchor='w', padx=24, pady=(8, 6))
        self.task_list = tk.Listbox(nav, bg=NAV, fg='#e5edf9', selectbackground='#314668', selectforeground='white',
                                   highlightthickness=0, borderwidth=0, font=('PingFang SC', 11), height=7, exportselection=False)
        self.task_list.pack(fill='both', expand=True, padx=16)
        self.task_list.bind('<<ListboxSelect>>', self.select_task)
        tk.Label(nav, text='本地运行 · 无需 AI\nv1.1.0', bg=NAV, fg='#8497b3', font=('PingFang SC', 10), justify='left').pack(anchor='w', padx=24, pady=22)
        body = ttk.Frame(self.root, padding=(28, 20)); body.pack(side='left', fill='both', expand=True)
        header = ttk.Frame(body); header.pack(fill='x')
        ttk.Label(header, text='把章节，整理成一本书。', style='Title.TLabel').pack(side='left')
        self.login_btn = ttk.Button(header, text='网页登录', command=self.login)
        self.login_btn.pack(side='right'); self.controls.append(self.login_btn)
        ttk.Label(body, text='后台完成网页收集与保存，让你继续专注手头的工作。', style='Muted.TLabel').pack(anchor='w', pady=(5, 12))
        input_card = ttk.Frame(body, style='Card.TFrame', padding=18); input_card.pack(fill='both', expand=True)
        row = ttk.Frame(input_card, style='Card.TFrame'); row.pack(fill='x')
        ttk.Label(row, text='01   网页网址', style='CardTitle.TLabel').pack(side='left')
        self.import_btn = ttk.Button(row, text='导入清单', command=self.import_urls); self.import_btn.pack(side='right')
        self.collect_btn = ttk.Button(row, text='收集章节链接', command=self.collect, style='Primary.TButton'); self.collect_btn.pack(side='right', padx=8)
        self.controls += [self.import_btn, self.collect_btn]
        ttk.Label(input_card, text='粘贴目录、章节网址或完整清单，每行一个。', style='CardMuted.TLabel').pack(anchor='w', pady=(6, 8))
        self.urls = ScrolledText(input_card, height=4, font=('Menlo', 11), wrap='none', background='#f7f9fd', foreground=INK,
                                 insertbackground=INK, relief='flat', highlightthickness=1, highlightbackground='#e1e7f0', padx=10, pady=9, undo=True)
        self.urls.pack(fill='both', expand=True)
        self.controls.append(self.urls)
        row = ttk.Frame(input_card, style='Card.TFrame'); row.pack(fill='x', pady=(12, 0))
        self.save_btn = ttk.Button(row, text='后台保存网页', command=self.save, style='Primary.TButton'); self.save_btn.pack(side='left')
        self.pause_btn = ttk.Button(row, text='暂停', command=self.toggle_pause, state='disabled'); self.pause_btn.pack(side='left', padx=7)
        self.stop_btn = ttk.Button(row, text='停止', command=self.cancel, state='disabled'); self.stop_btn.pack(side='left')
        self.retry_btn = ttk.Button(row, text='继续 / 重试', command=lambda: self.save(retry=True), state='disabled'); self.retry_btn.pack(side='right')
        self.controls.append(self.save_btn)
        self.advanced_var = tk.BooleanVar()
        self.advanced = ttk.Frame(body, padding=(8, 5))
        toggle = ttk.Checkbutton(body, text='高级选项', variable=self.advanced_var, command=self.toggle_advanced)
        toggle.pack(anchor='w', pady=(8, 2)); self.controls.append(toggle)
        for label, variable in [('加载等待', self.delay), ('网页间隔', self.interval), ('超时秒数', self.timeout), ('正文区域', self.selector)]:
            ttk.Label(self.advanced, text=label).pack(side='left', padx=(0, 5))
            field = ttk.Entry(self.advanced, textvariable=variable, width=5 if variable != self.selector else 10)
            field.pack(side='left', padx=(0, 12)); self.controls.append(field)
        check = ttk.Checkbutton(self.advanced, text='滚动加载图片', variable=self.scroll)
        check.pack(side='left'); self.controls.append(check)
        output_card = ttk.Frame(body, style='Card.TFrame', padding=18); output_card.pack(fill='x', pady=(8, 12))
        self.output_card = output_card
        ttk.Label(output_card, text='03   导出小说 TXT', style='CardTitle.TLabel').pack(anchor='w')
        row = ttk.Frame(output_card, style='Card.TFrame'); row.pack(fill='x', pady=(12, 8))
        ttk.Label(row, text='小说名称', style='Card.TLabel', width=9).pack(side='left')
        self.name_entry = ttk.Entry(row, textvariable=self.title); self.name_entry.pack(side='left', fill='x', expand=True)
        self.controls.append(self.name_entry)
        ttk.Label(row, text='.txt', style='CardMuted.TLabel').pack(side='left', padx=(6, 0))
        row = ttk.Frame(output_card, style='Card.TFrame'); row.pack(fill='x')
        ttk.Label(row, text='输出目录', style='Card.TLabel', width=9).pack(side='left')
        field = ttk.Entry(row, textvariable=self.destination); field.pack(side='left', fill='x', expand=True); self.controls.append(field)
        choose = ttk.Button(row, text='选择', command=self.choose_output); choose.pack(side='left', padx=(8, 0)); self.controls.append(choose)
        ttk.Label(output_card, textvariable=self.cache_status, style='CardMuted.TLabel', wraplength=720).pack(anchor='w', pady=(9, 7))
        row = ttk.Frame(output_card, style='Card.TFrame'); row.pack(fill='x')
        self.export_btn = ttk.Button(row, text='导出 TXT', command=self.export, style='Primary.TButton'); self.export_btn.pack(side='left')
        local = ttk.Button(row, text='选择已有网页', command=self.choose_local); local.pack(side='left', padx=8)
        self.cleanup_btn = ttk.Button(row, text='清理本次网页', command=self.cleanup, state='disabled'); self.cleanup_btn.pack(side='right')
        self.controls += [self.export_btn, local]
        self.progress = ttk.Progressbar(body, mode='determinate'); self.progress.pack(fill='x', pady=(1, 9))
        ttk.Label(body, textvariable=self.status, wraplength=800).pack(anchor='w')
        self.log = ScrolledText(body, height=2, font=('PingFang SC', 10), wrap='word', state='disabled',
                                background=BG, foreground=MUTED, relief='flat', highlightthickness=0)
        self.log.pack(fill='x', pady=(5, 0))
        self.reveal_btn = ttk.Button(body, text='在 Finder 中查看 TXT', command=self.reveal, state='disabled')
        self.reveal_btn.pack(anchor='e')

    def toggle_advanced(self):
        if self.advanced_var.get(): self.advanced.pack(fill='x', before=self.output_card)
        else: self.advanced.pack_forget()

    def append(self, text):
        self.log.configure(state='normal'); self.log.insert('end', text + '\n'); self.log.see('end'); self.log.configure(state='disabled')

    def set_busy(self, value, saving=False):
        self.busy = value
        for item in self.controls: item.configure(state='disabled' if value else 'normal')
        self.task_list.configure(state='disabled' if value else 'normal')
        self.pause_btn.configure(state='normal' if value and saving else 'disabled', text='暂停')
        self.stop_btn.configure(state='normal' if value else 'disabled')
        self.retry_btn.configure(state='normal' if not value and self.task and self.task.remaining() else 'disabled')
        self.cleanup_btn.configure(state='normal' if not value and self.result and self.result.get('cleanup_allowed') and not self.result.get('cleaned') else 'disabled')
        self.reveal_btn.configure(state='normal' if not value and self.result else 'disabled')

    def begin(self, status, work, saving=False):
        self.stop.clear(); self.pause.clear(); self.set_busy(True, saving); self.status.set(status)
        def run():
            try: self.events.put(('finished', work()))
            except (Cancelled, __import__('本地正文整理').OrganizeCancelled): self.events.put(('cancelled', None))
            except Exception as exc: self.events.put(('error', str(exc)))
        threading.Thread(target=run, daemon=True).start()

    def choose_output(self):
        folder = filedialog.askdirectory(parent=self.root, initialdir=self.destination.get(), title='选择 TXT 输出目录')
        if folder:
            self.destination.set(folder)
            try: remember_text_output(folder)
            except OSError as exc: self.append(str(exc))

    def import_urls(self):
        file = filedialog.askopenfilename(parent=self.root, filetypes=[('网址清单', '*.txt')])
        if file:
            try: self.urls.insert('end', '\n' + read_text(file))
            except Exception as exc: messagebox.showerror('导入失败', str(exc), parent=self.root)

    def collect(self):
        from 网页链接收集 import show_dialog
        def apply(urls):
            self.urls.delete('1.0', 'end'); self.urls.insert('1.0', '\n'.join(urls))
            self.task = None; self.local_source = None; self.result = None
            self.cache_status.set('链接已就绪，点击“后台保存网页”创建独立缓存。')
            self.status.set(f'已收集 {len(urls)} 个链接。'); self.set_busy(False)
        def metadata(report):
            self.title.set(report.get('book_title', ''))
        show_dialog(self.root, self.browser, self.urls.get('1.0', 'end'), apply, DATA / '链接记录', on_result=metadata)

    def login(self):
        urls, _ = parse_urls(self.urls.get('1.0', 'end'))
        def work():
            cdp = CDP(self.browser.ensure(headless=False))
            try:
                target = cdp.call('Target.createTarget', {'url': urls[0] if urls else 'about:blank'})['targetId']
                cdp.call('Target.activateTarget', {'targetId': target})
            finally: cdp.close()
            return {'kind': 'login'}
        self.begin('正在打开专用登录窗口…', work)
        self.stop_btn.configure(state='disabled')

    def save(self, retry=False):
        urls, invalid = parse_urls(self.urls.get('1.0', 'end'))
        if retry and self.task: urls, invalid = self.task.remaining(), []
        if invalid or not urls:
            messagebox.showerror('请检查网址', '\n'.join(invalid[:8]) or '请先粘贴网址或收集章节链接。', parent=self.root); return
        try:
            delay, interval, timeout = float(self.delay.get()), float(self.interval.get()), float(self.timeout.get())
            if not (0 <= delay <= 120 and 0 <= interval <= 120 and 5 <= timeout <= 300): raise ValueError()
        except ValueError:
            messagebox.showerror('高级设置', '等待与间隔为 0–120 秒，超时为 5–300 秒。', parent=self.root); return
        if not retry or not self.task:
            name = self.title.get().strip()
            if self.task and name == self.task.data.get('title') and urls != self.task.data['urls']:
                name = ''
                self.title.set('')
            try:
                self.task = BookTask.create(urls, name)
            except OSError as exc:
                messagebox.showerror('无法创建任务缓存', str(exc), parent=self.root)
                return
        task = self.task; self.local_source = None; self.result = None
        scroll = self.scroll.get()
        self.progress.configure(maximum=len(urls), value=0)
        self.cache_status.set(f'独立任务缓存 · 本批 {len(urls)} 个网页')
        def work():
            saver = Saver(self.browser, self.stop, self.pause, lambda s: self.events.put(('log', s)))
            for index, url in enumerate(urls, 1):
                saver.wait(); self.events.put(('status', f'后台保存 {index} / {len(urls)}'))
                try: row = saver.save_one(url, task.path, delay, scroll, timeout)
                except Cancelled: raise
                except Exception as exc: row = {'url': url, 'status': '失败', 'error': str(exc)}
                task.add(row)
                self.events.put(('log', ('✓ ' + row.get('title', '') if row['status'] == '成功' else '失败：' + row['error'])))
                self.events.put(('progress', index))
                if index < len(urls): saver.wait(interval)
            return {'kind': 'saved', 'remaining': len(task.remaining())}
        self.begin(f'正在后台保存 {len(urls)} 个网页…', work, saving=True)

    def choose_local(self):
        folder = filedialog.askdirectory(parent=self.root, initialdir=str(output_directory()), title='选择同一本小说的网页文件夹')
        if not folder: return
        self.task = None; self.local_source = Path(folder); self.result = None
        self.cache_status.set('已有网页 · ' + Path(folder).name[:40])
        def work(): return {'kind': 'local', 'title': detect_local_title(folder)}
        self.begin('正在识别本地网页的书名…', work)

    def export(self):
        source = self.task.path if self.task else self.local_source
        if not source:
            self.choose_local(); return
        title, destination, selector = self.title.get().strip(), self.destination.get().strip(), self.selector.get().strip() or '自动'
        if not destination:
            messagebox.showerror('输出目录', '请选择 TXT 输出目录。', parent=self.root); return
        if not title:
            messagebox.showerror('小说名称', '未能可靠识别书名，请填写小说名称后导出。', parent=self.root); self.name_entry.focus_set(); return
        task = self.task
        def work():
            remember_text_output(destination)
            result = export_text(source, destination, title=title, task=task, selector=selector, cancel=self.stop,
                                 progress=lambda s: self.events.put(('log', s)))
            return {'kind': 'export', 'result': result}
        self.begin('正在提取正文、修复换行并导出 TXT…', work)

    def cleanup(self):
        if not self.result: return
        count = len(self.result.get('cleanup_files', []))
        if not messagebox.askyesno('清理本次网页', f'TXT 已成功导出。将本次使用的 {count} 个网页移到废纸篓？\n\nTXT 和其他小说不受影响。', parent=self.root): return
        result, task = self.result, self.task
        def work():
            info = cleanup_export(result)
            if task: task.data['export'] = result; task.data['status'] = '缓存已清理'; task.save()
            return {'kind': 'cleanup', **info}
        self.begin('正在将本次网页移到废纸篓…', work)

    def reveal(self):
        if self.result: subprocess.Popen(['open', '-R', self.result['output']])

    def refresh_tasks(self):
        self.recent = BookTask.recent(); self.task_list.delete(0, 'end')
        for task in self.recent:
            self.task_list.insert('end', task.data.get('title') or '待识别书名')

    def select_task(self, event=None):
        selection = self.task_list.curselection()
        if self.busy or not selection: return
        self.task = self.recent[selection[0]]; self.local_source = None
        self.title.set(self.task.data.get('title', '')); self.result = self.task.data.get('export')
        self.urls.delete('1.0', 'end'); self.urls.insert('1.0', '\n'.join(self.task.data['urls']))
        self.cache_status.set(self.task.data['status'] + f' · {len(self.task.data["urls"])} 个网页')
        self.status.set('已切换到选中的小说任务。'); self.set_busy(False)
        self.reveal_btn.configure(state='normal' if self.result else 'disabled')

    def toggle_pause(self):
        if self.pause.is_set(): self.pause.clear(); self.pause_btn.configure(text='暂停')
        else: self.pause.set(); self.pause_btn.configure(text='继续')

    def cancel(self):
        self.stop.set(); self.pause.clear(); self.status.set('正在停止，已保存的网页会保留。')

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'log': self.append(value); continue
                if kind == 'status': self.status.set(value); continue
                if kind == 'progress': self.progress.configure(value=value); continue
                self.set_busy(False)
                if kind == 'error': self.status.set('未完成：' + value); self.append(value)
                elif kind == 'cancelled': self.status.set('已停止。已保存的网页仍保留在本次任务中。')
                elif value['kind'] == 'login': self.status.set('登录后回到这里。开始收集或保存时会自动转为后台模式。')
                elif value['kind'] == 'local':
                    self.title.set(value['title']); self.status.set('书名已识别，可确认输出目录并导出。' if value['title'] else '请填写小说名称后导出。')
                elif value['kind'] == 'saved':
                    self.title.set(self.task.data.get('title', '') or self.title.get())
                    self.cache_status.set(f'独立任务缓存 · {len(self.task.data["urls"])} 个网页')
                    self.status.set('网页已保存。确认小说名称和输出目录后，点击“导出 TXT”。' if not value['remaining'] else f'还有 {value["remaining"]} 页未保存，可点击“继续 / 重试”。')
                elif value['kind'] == 'export':
                    self.result = value['result']; self.reveal_btn.configure(state='normal')
                    self.status.set(f'已导出 {Path(self.result["output"]).name} · {self.result["chapter_entries"]} 个章节。'
                                    + ('存在缺页或未识别文件，网页已保留。' if self.result['incomplete'] else '可选择清理本次网页。'))
                    self.set_busy(False)
                elif value['kind'] == 'cleanup':
                    self.status.set(f'已将 {value["count"]} 个网页移到废纸篓，TXT 保留。'); self.set_busy(False)
                self.refresh_tasks()
        except queue.Empty: pass
        if self.closing and not self.busy:
            self.browser.shutdown(); self.root.destroy(); return
        self.timer = self.root.after(100, self.poll)

    def close(self):
        if self.busy:
            self.closing = True; self.cancel(); return
        self.root.after_cancel(self.timer); self.browser.shutdown(); self.root.destroy()


def run(smoke_test=False):
    app = NovelApp(smoke_test=smoke_test)
    app.root.mainloop()
