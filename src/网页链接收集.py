"""通过本机浏览器读取页面链接；不提取或保存正文，不调用模型服务。"""
import json
import re
import threading
import queue
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, urljoin


def canonical(url):
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("仅支持普通 HTTP/HTTPS 网址")
    return urlunsplit((p.scheme, p.netloc.lower(), p.path, p.query, ""))


def page_family(url):
    p = urlsplit(canonical(url))
    path = re.sub(r"_\d+(?=\.html?$)", "", p.path)
    path = re.sub(r"/page/\d+/?$", "/page/", path)
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in {"page", "p", "pageno", "page_no"}]
    return urlunsplit((p.scheme, p.netloc, path, urlencode(query), ""))


def page_number(url):
    p = urlsplit(url)
    match = re.search(r"_(\d+)\.html?$|/page/(\d+)/?$", p.path)
    if match:
        return int(next(x for x in match.groups() if x))
    for k, v in parse_qsl(p.query):
        if k.lower() in {"page", "p", "pageno", "page_no"} and v.isdigit():
            return int(v)
    return 1


DEFAULT_CONTENT_PATTERN = r"/(?:view/\d+(?:_\d+)?\.html?|book/[^/?#]+/[^/?#]+\.html?|chapter/[^?#]+|read/[^?#]+)"


def is_next_chapter(label):
    # 排除“上一章”、广告和包含“下一章”字样的正文标题，仅认导航按钮。
    label = re.sub(r"[\s\uE000-\uF8FF«»‹›<>→←【】\[\]（）()]", "", label)
    return bool(re.fullmatch(r"(?:下(?:一)?[章節节回]|nextchapter)", label, re.I))


def same_chapter_series(current, target):
    a, b = urlsplit(current), urlsplit(target)
    # /book/书号/章节.html：严格留在同一本书，避免跳进推荐书目。
    if a.netloc != b.netloc:
        return False
    return a.path.rsplit("/", 1)[0] == b.path.rsplit("/", 1)[0]


def collect(seeds, fetch, *, content_pattern="", max_pages=500, stop=None, progress=None, all_links=False):
    """目录入口检查目录及章内分页；章节入口另沿明确的下一章链接前进。"""
    if not 1 <= max_pages <= 5000:
        raise ValueError("检查上限应为 1–5000 页")
    pattern = re.compile(content_pattern or DEFAULT_CONTENT_PATTERN)
    seeds = list(dict.fromkeys(canonical(u) for u in seeds))
    allowed_hosts = {urlsplit(u).netloc for u in seeds}
    directories = deque(u for u in seeds if not pattern.search(u) or all_links)
    content = deque(u for u in seeds if pattern.search(u) and not all_links)
    follow_chapters = bool(content)
    directory_families = {page_family(u) for u in directories}
    known, visited, order, discovered = set(seeds), set(), [], set()
    errors, checked_directories, warnings = [], [], []
    titles = {}
    cancelled = False

    def record(url, label=""):
        # 只有实际观察到两种首页地址时才去重；目录的 _1.html 不能凭空改写。
        base = re.sub(r"_1(?=\.html?(?:\?|$))", "", url)
        if base != url and base in discovered:
            return
        alias = re.sub(r"(?=\.html?(?:\?|$))", "_1", url, count=1)
        if alias != url and alias in discovered:
            order[order.index(alias)] = url
            discovered.remove(alias)
            discovered.add(url)
            titles[url] = label
            return
        if url not in discovered:
            order.append(url)
            discovered.add(url)
            titles[url] = label

    for u in content:
        record(u)

    def links_from(data, current):
        links = []
        for link in data.get("links", []):
            try:
                url = canonical(urljoin(data.get("url") or current, link["url"]))
            except (KeyError, ValueError):
                continue
            if urlsplit(url).netloc in allowed_hosts:
                links.append((url, link.get("text", "")))
        return links

    while directories or content:
        if stop and stop.is_set():
            cancelled = True
            break
        if len(visited) >= max_pages:
            break
        is_directory = bool(directories)
        current = directories.popleft() if is_directory else content.popleft()
        if current in visited or (not is_directory and current not in discovered):
            continue
        visited.add(current)
        if progress:
            progress(f"检查 {len(visited)}/{max_pages}：{current}")
        try:
            data = fetch(current)
            if urlsplit(canonical(data.get("url") or current)).netloc not in allowed_hosts:
                raise ValueError("页面跳转到其他站点，未继续读取其链接")
            links = links_from(data, current)
        except Exception as exc:
            if stop and stop.is_set():
                cancelled = True
                visited.remove(current)
                (directories if is_directory else content).appendleft(current)
                break
            errors.append({"url": current, "error": str(exc)})
            continue
        if is_directory:
            checked_directories.append(current)
            if all_links:
                for url, label in links:
                    record(url, label)
                continue
            matches = [(u, label) for u, label in links if pattern.search(u)]
            # 部分目录顶部列出最新章节：从首次出现的第一章开始，保持主目录原顺序。
            first = next((i for i, (_, label) in enumerate(matches)
                          if re.search(r"第\s*[一1]\s*章|chapter\s+1\b", label, re.I)), None)
            if first is not None and not order:
                matches = matches[first:]
            for url, label in matches:
                record(url, label)
                if url not in known and url in discovered:
                    known.add(url)
                    content.append(url)
            for url, _ in links:
                if page_family(url) in directory_families and url not in known:
                    known.add(url)
                    directories.append(url)
        else:
            if not titles.get(current) or is_next_chapter(titles[current]):
                titles[current] = data.get("title", "")
            root = page_family(current)
            for url, label in links:
                continuation = (follow_chapters and is_next_chapter(label)
                                and same_chapter_series(current, url) and pattern.search(url))
                if page_family(url) == root or continuation:
                    record(url, label)
                    if url not in known and url in discovered:
                        known.add(url)
                        content.append(url)

    # 分页按首次遇到的文章位置聚合；不把后一篇的“下一章”混入当前篇。
    groups = {}
    for url in order:
        groups.setdefault(page_family(url), []).append(url)
    ordered = (order if all_links else
               [url for urls in groups.values() for url in sorted(urls, key=page_number)])
    pending = [u for u in list(directories) + list(content) if u not in visited]
    if not order and not errors and not cancelled:
        warnings.append("未识别到章节链接，不能判定收集完成。请检查入口、登录状态或正文网址匹配规则。")
    complete = bool(order) and not errors and not pending and not cancelled
    return {
        "urls": ordered, "titles": titles, "directories": checked_directories,
        "checked_pages": len(visited), "errors": errors, "pending": pending,
        "cancelled": cancelled, "complete": complete, "all_links": all_links,
        "warnings": warnings, "follow_chapters": follow_chapters,
        "limit_reached": bool(pending and len(visited) >= max_pages),
        "scope": "章节入口从当前章沿明确的下一章链接前进；目录入口保持目录顺序。仅检查实际链接，不保证适用于任意网站",
    }


class BrowserLinkReader:
    def __init__(self, browser, stop, selector="", delay=1):
        self.browser, self.stop = browser, stop
        self.selector, self.delay = selector or "a[href]", delay

    def __call__(self, url):
        from 网页批量保存 import CDP, Saver
        waiter = Saver(self.browser, self.stop)
        waiter.wait()
        cdp = CDP(self.browser.ensure(), self.stop)
        target = None
        try:
            target = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
            session = cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]

            def call(method, params=None, timeout=15):
                waiter.wait()
                return cdp.call(method, params, session, timeout)

            def evaluate(expression):
                result = call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
                if result.get("exceptionDetails"):
                    raise ValueError("链接选择器无效或页面脚本执行失败")
                return result.get("result", {}).get("value")

            call("Page.enable")
            call("Network.enable")
            navigation = call("Page.navigate", {"url": url})
            if navigation.get("errorText"):
                raise RuntimeError(navigation["errorText"])
            import time
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                waiter.wait(0.2)
                try:
                    state = evaluate("({state:document.readyState,url:location.href})")
                    if state and state["state"] in {"interactive", "complete"} and state["url"] != "about:blank":
                        break
                except RuntimeError as exc:
                    if not any(word in str(exc).lower() for word in ("context", "navigat")):
                        raise
            else:
                raise TimeoutError("页面加载超时")
            waiter.wait(self.delay)
            # 只返回链接文字/地址与页面标题，不读取正文。
            js = r"""(() => {
                const selected = Array.from(document.querySelectorAll(SELECTOR));
                const paging = Array.from(document.querySelectorAll('a[href]')).filter(a =>
                    /^(?:[【\[（(]?\d+[】\]）)]?|下[一]?[页頁章節节回]|上一[页頁]|next(?:\s+chapter)?|previous)$/i.test(a.textContent.trim()));
                const anchors = [...new Set([...selected, ...paging])];
                return {url:location.href,title:document.title,
                    links:anchors.filter(a=>a.href).map(a=>({url:a.href,text:a.textContent.trim()}))};
            })()""".replace("SELECTOR", json.dumps(self.selector))
            # DOM 已可读时不必等待广告图片等所有资源加载完成；同时给异步链接留出稳定时间。
            deadline = time.monotonic() + 8
            earliest = time.monotonic() + 1
            previous, stable_since, result = None, time.monotonic(), None
            while time.monotonic() < deadline:
                result = evaluate(js)
                signature = json.dumps(result, ensure_ascii=False, sort_keys=True)
                if signature != previous:
                    previous, stable_since = signature, time.monotonic()
                if time.monotonic() >= earliest and time.monotonic() - stable_since >= 0.75:
                    break
                waiter.wait(0.25)
            if not result or result["url"].startswith(("chrome-error:", "about:")):
                raise RuntimeError("浏览器未打开正常网页")
            for event in reversed(cdp.events):
                p = event.get("params", {})
                if (event.get("sessionId") == session and event.get("method") == "Network.responseReceived"
                        and p.get("loaderId") == navigation.get("loaderId") and p.get("type") == "Document"):
                    if p["response"]["status"] >= 400:
                        raise RuntimeError(f"服务器返回 HTTP {p['response']['status']}")
                    break
            return result
        finally:
            if target:
                cdp.cancel = None
                try:
                    cdp.call("Target.closeTarget", {"targetId": target}, timeout=3)
                except Exception:
                    pass
            cdp.close()


def show_dialog(parent, browser, initial, apply_urls, destination):
    import tkinter as tk
    from tkinter import ttk, messagebox
    from tkinter.scrolledtext import ScrolledText
    from 网页批量保存 import parse_urls
    window = tk.Toplevel(parent)
    window.title("自动收集网页链接")
    window.geometry("860x760")
    window.minsize(760, 700)
    window.transient(parent)
    window.grab_set()
    outer = ttk.Frame(window, padding=18)
    outer.pack(fill="both", expand=True)
    ttk.Label(outer, text="输入目录或章节网址，自动收集后续链接。", font=("PingFang SC", 17, "bold")).pack(anchor="w")
    seeds = ScrolledText(outer, height=3, wrap="word")
    seeds.pack(fill="x", pady=8)
    seeds.insert("1.0", initial)
    pattern, selector = tk.StringVar(), tk.StringVar()
    maximum = tk.StringVar(value="500")
    all_var = tk.BooleanVar()
    controls = [seeds]
    for label, variable in [("正文网址匹配（正则，可留空）", pattern),
                            ("目录链接选择器（可留空）", selector),
                            ("最多检查页面数", maximum)]:
        row = ttk.Frame(outer)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, width=29).pack(side="left")
        entry = ttk.Entry(row, textvariable=variable)
        entry.pack(side="left", fill="x", expand=True)
        controls.append(entry)
    toggle = ttk.Checkbutton(outer, text="只列出输入页面上的同站点链接，不跟进分页", variable=all_var)
    toggle.pack(anchor="w", pady=6)
    controls.append(toggle)
    ttk.Label(outer, text="章节入口：从当前章沿“下一章”继续；目录入口：按目录顺序。两种方式均检查章内数字分页。\n"
              "支持 /book/书号/章节.html、/view/、/chapter/、/read/；其他格式可填写匹配规则。\n"
              "需要登录时先在主窗口打开专用浏览器登录。收集后可编辑清单，再放入保存列表。",
              foreground="#666666", wraplength=800).pack(anchor="w")
    status = tk.StringVar(value="只收集链接；不保存正文，不调用 AI。")
    ttk.Label(outer, textvariable=status, wraplength=800).pack(anchor="w", pady=8)
    output = ScrolledText(outer, height=12, wrap="none", undo=True)
    output.pack(fill="both", expand=True)
    actions = ttk.Frame(outer)
    actions.pack(fill="x", pady=10)
    stop, events = threading.Event(), queue.Queue()
    state = {"busy": False, "result": None, "closing": False}

    def start():
        urls, invalid = parse_urls(seeds.get("1.0", "end"))
        try:
            limit = int(maximum.get())
            if invalid or not urls or not 1 <= limit <= 5000:
                raise ValueError("请填写有效入口网址，检查上限为 1–5000 页")
            re.compile(pattern.get() or DEFAULT_CONTENT_PATTERN)
        except (ValueError, re.error) as exc:
            messagebox.showerror("检查设置", str(exc), parent=window)
            return
        options = dict(content_pattern=pattern.get(), max_pages=limit, all_links=all_var.get())
        reader = BrowserLinkReader(browser, stop, selector.get().strip())
        stop.clear()
        state.update(busy=True, result=None)
        detail_button.configure(state="disabled")
        output.delete("1.0", "end")
        for widget in controls + [start_button, use_button]:
            widget.configure(state="disabled")
        stop_button.configure(state="normal")

        def work():
            try:
                result = collect(urls, reader, stop=stop,
                                 progress=lambda s: events.put(("status", s)), **options)
                events.put(("result", result))
            except Exception as exc:
                events.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def apply():
        urls, invalid = parse_urls(output.get("1.0", "end"))
        if invalid or not urls:
            messagebox.showerror("检查清单", "请保留每行一个有效网址。", parent=window)
            return
        # 保留可复查的链接清单和范围/失败记录，不包含正文。
        folder = Path(destination).expanduser() / "链接清单"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            (folder / f"网址_{stamp}.txt").write_text("\n".join(urls)+"\n", encoding="utf-8-sig")
            report = dict(state["result"] or {}, selected_urls=urls)
            (folder / f"收集报告_{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            # 把用户审阅后的顺序交给后续本地整理，不因下载失败重试而改变顺序。
            if not report.get("all_links"):
                cache = Path(destination).expanduser() / ".正文整理顺序.json"
                cache.write_text(json.dumps({"urls": urls, "source": f"链接清单/网址_{stamp}.txt"},
                                            ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("保存清单失败", str(exc), parent=window)
            return
        apply_urls(urls)
        window.destroy()

    def close():
        if state["busy"]:
            state["closing"] = True
            stop.set()
            status.set("正在停止并关闭…")
        else:
            window.destroy()

    def details():
        result = state["result"]
        if not result:
            return
        dialog = tk.Toplevel(window)
        dialog.title("链接检查详情")
        dialog.geometry("780x520")
        dialog.transient(window)
        box = ScrolledText(dialog, wrap="word", padx=12, pady=12)
        box.pack(fill="both", expand=True)
        lines = [result["scope"], ""] + result.get("warnings", [])
        for error in result["errors"]:
            lines.extend(["失败网址：" + error["url"], "原因：" + error["error"], ""])
        if result["pending"]:
            lines.extend(["尚未检查的网址：", *result["pending"]])
        if not result["errors"] and not result["pending"] and not result.get("warnings"):
            lines.append("当前规则范围内没有失败或待检查页面。")
        box.insert("1.0", "\n".join(lines))
        box.configure(state="disabled")
        dialog.grab_set()
        def dismiss():
            dialog.destroy()
            window.grab_set()
        dialog.protocol("WM_DELETE_WINDOW", dismiss)

    start_button = ttk.Button(actions, text="开始收集", command=start)
    start_button.pack(side="left")
    stop_button = ttk.Button(actions, text="停止", command=stop.set, state="disabled")
    stop_button.pack(side="left", padx=8)
    detail_button = ttk.Button(actions, text="查看检查详情", command=details, state="disabled")
    detail_button.pack(side="left")
    use_button = ttk.Button(actions, text="使用这份清单 → 主窗口", command=apply, state="disabled")
    use_button.pack(side="right")

    def poll():
        try:
            while True:
                kind, data = events.get_nowait()
                if kind == "status":
                    status.set(data)
                    continue
                state["busy"] = False
                stop_button.configure(state="disabled")
                for widget in controls + [start_button]:
                    widget.configure(state="normal")
                if kind == "result":
                    state["result"] = data
                    detail_button.configure(state="normal")
                    output.insert("1.0", "\n".join(data["urls"]))
                    use_button.configure(state="normal" if data["urls"] else "disabled")
                    outcome = "已完成规则范围内的检查" if data["complete"] else "尚未全部检查"
                    status.set(f"{outcome}：{len(data['urls'])} 个链接，检查 {data['checked_pages']} 页；"
                               f"失败 {len(data['errors'])} 页，待检查 {len(data['pending'])} 页。可编辑下方清单。")
                    if not data["urls"] and not data["errors"]:
                        status.set("未识别到符合规则的链接。请检查入口页面、登录状态，或调整网址匹配规则／链接选择器。")
                    if data["errors"]:
                        status.set(status.get() + " 首个失败原因：" + data["errors"][0]["error"][:160])
                else:
                    status.set("收集失败：" + data)
                if state["closing"]:
                    window.destroy()
                    return
        except queue.Empty:
            pass
        window.after(100, poll)

    window.protocol("WM_DELETE_WINDOW", close)
    window.after(100, poll)
    return window
