"""真实桌面控件、后台 Chrome、独立缓存、TXT 导出与清理的全流程。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import json


def main():
    with TemporaryDirectory(prefix='desktop-flow-') as temp:
        root = Path(temp)
        os.environ['WEB_ARCHIVE_DATA_DIR'] = str(root/'data')
        from 桌面界面 import NovelApp
        from 网页批量保存 import CDP
        from 书籍任务 import cleanup_export, BookTask
        from 链接界面自检 import button
        import tkinter as tk
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                chapter = 1 if self.path.endswith('a.html') else 2
                link = '<a href="/book/42/b.html">下一章</a>' if chapter==1 else ''
                body = (f'<html><head><meta charset="utf-8"><meta property="og:novel:book_name" content="星河旅人">'
                        f'<title>第{chapter}章_星河旅人-示例书屋</title></head><body><h1>第{chapter}章</h1>'
                        f'<div id="content">「第{chapter}章第一句。」<br>「下一段。」</div>{link}</body></html>').encode()
                self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        app=NovelApp();app.destination.set(str(root/'txt'))
        app.delay.set('0.1');app.interval.set('0');app.scroll.set(False)
        calls=[];original=CDP.call
        def tracked(cdp,method,params=None,*args,**kwargs):
            calls.append((method,params));return original(cdp,method,params,*args,**kwargs)
        def wait(until,timeout=45):
            deadline=time.monotonic()+timeout
            while not until() and time.monotonic()<deadline:
                app.root.update();time.sleep(.03)
            assert until(), app.status.get()
        try:
            app.root.update()
            app.advanced_var.set(True);app.toggle_advanced();app.root.geometry('1000x800');app.root.update()
            assert app.root.winfo_reqheight()<=800
            assert app.reveal_btn.winfo_rooty()+app.reveal_btn.winfo_height()<=app.root.winfo_rooty()+800
            app.advanced_var.set(False);app.toggle_advanced()
            first=f'http://127.0.0.1:{server.server_port}/book/42/a.html'
            app.urls.insert('1.0',first)
            with patch.object(CDP,'call',tracked):
                app.collect_btn.invoke()
                dialog=next(w for w in app.root.winfo_children() if isinstance(w,tk.Toplevel))
                button(dialog,'开始收集').invoke()
                use=button(dialog,'使用这份清单 → 主窗口')
                wait(lambda:str(use.cget('state'))=='normal')
                use.invoke()
                assert app.title.get()=='星河旅人'
                assert len(app.urls.get('1.0','end').split())==2
                app.save_btn.invoke();wait(lambda:not app.busy)
                assert not app.task.remaining(),app.status.get()
                assert len(list(app.task.path.glob('*.mhtml')))==2
                app.export_btn.invoke();wait(lambda:not app.busy)
                assert app.result and not app.result['incomplete'],app.status.get()
                assert [p.name for p in (root/'txt').iterdir()]==['星河旅人.txt']
                text=(root/'txt/星河旅人.txt').read_text(encoding='utf-8-sig')
                assert '「第1章第一句。」\n「下一段。」' in text
                assert text.index('第1章')<text.index('第2章')
                cdp=CDP(app.browser.ensure())
                try:agent=cdp.call('Browser.getVersion')['userAgent']
                finally:cdp.close()
                assert 'HeadlessChrome' in agent,agent
                assert not any(method in {'Page.bringToFront','Target.activateTarget'} for method,_ in calls)
                assert all(params.get('background') for method,params in calls if method=='Target.createTarget')
                other=BookTask.create(['http://example.test/b'],'另一本书')
                untouched=other.path/'保留.html';untouched.write_text('不能删除')
                with patch('tkinter.messagebox.askyesno',return_value=True),patch('桌面界面.cleanup_export',side_effect=lambda r:cleanup_export(r,root/'trash')):
                    app.cleanup_btn.invoke();wait(lambda:not app.busy)
                assert app.result['cleaned'],app.status.get()
                assert not list(app.task.path.glob('*.mhtml'))
                assert untouched.exists() and (root/'txt/星河旅人.txt').exists()
                assert len(list((root/'trash').glob('*/*.mhtml')))==2
                print('PASS：真实界面收集→后台保存→书名 TXT→用户选择清理；独立任务、目录纯净、其他小说保留。')
                print('PASS：Chrome 为 headless，所有任务标签在后台创建，未调用前台激活。')
        finally:
            app.close();server.shutdown();server.server_close()


if __name__=='__main__':main()
