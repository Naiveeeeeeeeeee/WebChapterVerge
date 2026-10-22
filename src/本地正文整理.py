"""离线HTML/MHTML正文整理；仅处理用户选择的本地文件，不调用网络或AI。"""
from pathlib import Path
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
import re
import json
import hashlib
from urllib.parse import urljoin, urlparse
from urllib.parse import urldefrag
from datetime import datetime
import csv
import time


class OrganizeCancelled(Exception):
    pass

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class Extractor(HTMLParser):
    def __init__(self, selector="#nr1"):
        super().__init__(convert_charrefs=True)
        self.selector = selector
        self.stack = []
        self.body = []
        self.heading = []
        self.title = []
        self.links = []
        self.anchor = None
        self.body_found = False
        self.skipped = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        parent = self.stack[-1] if self.stack else {}
        classes = attrs.get("class", "").split()
        skip = parent.get("skip", False) or tag in {"script", "style", "noscript"} or any(c in {"tuijian", "slide-ad"} for c in classes)
        matched = ((self.selector.startswith("#") and attrs.get("id") == self.selector[1:])
                   or (self.selector.startswith(".") and self.selector[1:] in classes)
                   or self.selector == tag)
        body = parent.get("body", False) or matched
        if matched:
            self.body_found = True
        if body and skip and not parent.get("skip", False):
            self.skipped.append({"tag": tag, "class": classes})
        frame = {"tag": tag, "body": body, "skip": skip,
                 "heading": parent.get("heading", False) or tag == "h1",
                 "title": parent.get("title", False) or tag == "title"}
        if body and not skip and tag in {"br", "p", "div", "hr"}:
            self.body.append("\ue000" if tag == "br" else "\ue001")
        if tag == "a":
            self.anchor = [attrs.get("href", ""), []]
        if tag not in VOID:
            self.stack.append(frame)

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            self.links.append(("".join(self.anchor[1]).strip(), self.anchor[0]))
            self.anchor = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                frame = self.stack[i]
                if frame["body"] and not frame["skip"] and tag in {"p", "div"}:
                    self.body.append("\ue001")
                del self.stack[i:]
                break

    def handle_data(self, data):
        if not self.stack:
            return
        frame = self.stack[-1]
        if not frame["skip"]:
            if frame["body"]:
                # HTML源码自身的换行不是可见段落；可见换行由br及块标签记录。
                self.body.append(data.replace("\r", "").replace("\n", ""))
            if frame["heading"]:
                self.heading.append(data)
            if frame["title"]:
                self.title.append(data)
        if self.anchor is not None:
            self.anchor[1].append(data)


def chinese_number(s):
    digits = dict(zip("零一二三四五六七八九", range(10)))
    if s.isdigit():
        return int(s)
    if "十" in s:
        a, b = s.split("十")
        return (digits[a] if a else 1) * 10 + (digits[b] if b else 0)
    return digits[s]


def classify(title):
    if "同人" in title:
        return "同人", 0, 0
    if "番外" in title:
        return "番外", 0, 0
    if "第二部" in title:
        numbers = re.findall(r"\d+", title.split("第二部", 1)[1])
        if not numbers:
            return "第二部", 0, 0
        return "第二部", int(numbers[0]), int(numbers[-1])
    number = re.search(r"第([零一二三四五六七八九十\d]+)章", title)
    if not number:
        return "其他", 0, 0
    n = chinese_number(number[1])
    return "第一部", n, n


def canonical_url(url):
    """站点分页中的 _1.html 与未带页码的首页等价。"""
    return re.sub(r"_1(?=\.html?$)", "", urldefrag(url)[0])


PAGE_NOTICE = re.compile(
    r"^\s*本章未完[，,。]?\s*(?:请|請)?(?:点击|點擊)\s*"
    r"[\[【（(]?\s*(?:数字分页|數字分頁)\s*[\]】）)]?\s*"
    r"(?:继续阅读|繼續閱讀)\s*[-—>＞»]*\s*$")


def remove_page_notices(raw):
    """只删除独立且完整匹配的翻页提示，绝不按‘继续阅读’等关键词删正文。"""
    chunks = re.split(r"([\ue000\ue001]+)", raw)
    removed = []
    for i, chunk in enumerate(chunks):
        if PAGE_NOTICE.fullmatch(chunk):
            removed.append(chunk.strip())
            chunks[i] = ""
    return "".join(chunks), removed


def open_corner_quote(text, opening, closing):
    # 限于当前段末尾，不能把原文中混用/反向的引号跨段传播。
    tail = text.rsplit(opening, 1)
    return len(tail) == 2 and closing not in tail[1] and '"' not in tail[1] and "”" not in tail[1]


def repair_quote_layout(text):
    """回收掉到下一段的右引号；保留后续新对话和叙述的段落。"""
    result, changes = [], 0
    for line in text.splitlines():
        if result:
            for opening, closing in [("「", "」"), ("『", "』")]:
                if line.startswith(closing) and open_corner_quote(result[-1], opening, closing):
                    # 反向写成 」对话「 的源文本保留，避免吞并多个自然段。
                    if re.search(re.escape(closing) + r"[^「」『』]*" + re.escape(opening) + r"$", result[-1]):
                        continue
                    result[-1] += closing
                    line = line[1:]
                    changes += 1
            if (line and result[-1] in {"「", "『"}):
                result[-1] += line
                changes += 1
                continue
        if line:
            result.append(line)
    return "\n".join(result), changes


def quote_warnings(text):
    """只报告源文本疑点，不补写或替换标点。"""
    warnings = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.count("「") != line.count("」") or line.count("『") != line.count("』"):
            warnings.append({"line": number, "reason": "本段角引号数量不配对（可能为跨段引用或原文混用）"})
        elif re.match(r"^」[^「」]+「", line):
            warnings.append({"line": number, "reason": "原文疑似反向角引号"})
    return warnings


def join_local_pages(previous, following, fix_breaks=True):
    """仅在相邻页明确接续同一段角引号对话时接句；其余保留段落边界。"""
    last = previous.rsplit("\n", 1)[-1]
    first, separator, rest = following.partition("\n")
    if fix_breaks:
        for opening, closing in [("「", "」"), ("『", "』")]:
            if (open_corner_quote(last, opening, closing)
                    and re.match(r"^[^「」『』\n]{1,1000}" + re.escape(closing), first)
                    and not last.endswith(opening)):
                space = " " if re.search(r"[A-Za-z0-9]$", last) and re.match(r"[A-Za-z0-9]", first) else ""
                return previous + space + first + (separator + rest if separator else ""), 1
    return previous + "\n" + following, 0


def reflow_body(raw):
    """消除网页固定列宽折行；保留段落和独立标题，不修改非空白字符。"""
    chunks = re.split(r"([\ue000\ue001]+)", raw.replace("\xa0", " "))
    paragraphs = []
    separator = ""
    previous = ""
    previous_protected = False
    joins = 0

    def protected(text):
        return bool(
            re.match(r"^(?:作者|字数|字數|日期|时间|時間)\s*[:：]", text)
            or re.fullmatch(r"[\d\s年月日./:：\-]+", text)
            or re.fullmatch(r"(?:第[零一二三四五六七八九十百\d]+[章节部卷].{0,25}|[【\[(（].{1,35}[】\])）])", text)
            or re.fullmatch(r"[-—*=·…\s]{2,}", text)
        )

    for chunk in chunks:
        if not chunk:
            continue
        if re.fullmatch(r"[\ue000\ue001]+", chunk):
            separator += chunk
            continue
        text = chunk.strip()
        if not text:
            continue
        if re.fullmatch(r"(?:【\d+】\s*){2,}", text):
            continue
        # 网页中的全角/四空格缩进是明确的新段落标记。
        indented = bool(re.match(r"^(?:\u3000{2,}| {4,})", chunk.lstrip("\t")))
        current_protected = protected(text)
        br_count = separator.count("\ue000")
        join = bool(paragraphs and not indented and not previous_protected
                    and not current_protected and "\ue001" not in separator and br_count == 1)
        # 部分网页把每个折行都变成双br：只接回明显没有结束的长句，
        # 并保留独立对话开头和有句末标点的自然段。
        if (paragraphs and not indented and not current_protected and not previous_protected
                and "\ue001" not in separator and br_count >= 2
                and len(previous) >= 20
                and not re.search(r'[。！？!?…：:」』”’"）)\]】>]$', previous)
                and not re.match(r'^[「『“"]', text)):
            join = True
        # 网页的单个br也可能是真实的对话换段，不能把两个完整说话回合接起来。
        if (paragraphs and re.match(r'^[「『“]', text)
                and re.search(r'[。！？!?」』”]$', paragraphs[-1])):
            join = False
        if join:
            # 英文字词之间保留一个必要空格，中文折行直接接续。
            space = " " if re.search(r"[A-Za-z0-9]$", paragraphs[-1]) and re.match(r"[A-Za-z0-9]", text) else ""
            paragraphs[-1] += space + text
            joins += 1
        else:
            paragraphs.append(text)
        previous = text
        previous_protected = current_protected
        separator = ""
    repaired, _ = repair_quote_layout("\n".join(paragraphs))
    return repaired, joins


def natural_key(text):
    return [(1, int(x)) if x.isdigit() else (0, x.casefold())
            for x in re.split(r"(\d+)", str(text))]


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.selectors = set()
        self.base = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.selectors.add(tag)
        if attrs.get("id"):
            self.selectors.add("#" + attrs["id"])
        self.selectors.update("." + x for x in attrs.get("class", "").split())
        if tag == "base" and attrs.get("href"):
            self.base = attrs["href"]


def read_archive(path, source_url=""):
    raw = path.read_bytes()
    charset = None
    if path.suffix.lower() in {".mhtml", ".mht"}:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        html_parts = [p for p in message.walk() if p.get_content_type() == "text/html"]
        if not html_parts:
            raise ValueError("归档中没有HTML页面")
        # multipart/related的start参数可以指定根HTML；其余HTML可能是iframe。
        start = message.get_param("start")
        part = next((p for p in html_parts if start and p.get("Content-ID") == start), html_parts[0])
        source_url = str(part.get("Content-Location") or message.get("Snapshot-Content-Location") or source_url)
        charset = part.get_content_charset()
        raw = part.get_payload(decode=True)
    meta = re.search(rb'charset\s*=\s*["\']?([\w-]+)', raw[:16000], re.I)
    choices = list(dict.fromkeys(x for x in
                   (charset, meta[1].decode("ascii") if meta else None, "utf-8-sig", "gb18030") if x))
    for encoding in choices:
        try:
            return raw.decode(encoding), source_url
        except (UnicodeError, LookupError):
            continue
    raise ValueError("无法无损识别文字编码")


def clean_lines(raw):
    return "\n".join(x.strip() for x in raw.replace("\ue000", "\n").replace("\ue001", "\n")
                     .replace("\xa0", " ").splitlines()
                     if x.strip() and not re.fullmatch(r"(?:【\d+】\s*){2,}", x.strip()))


def organize(source, destination=None, *, title="网页正文", selector="自动",
             fix_breaks=True, original_order=True, merge_pages=True, clean_notices=True,
             order_reference=None, progress=None, cancel=None, write_order_cache=True, pure_text=False):
    """将本地页面整理到新的结果目录，返回统计和输出路径。不会打开浏览器。"""
    started = time.perf_counter()
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise ValueError("请选择已保存网页所在的文件夹")
    if selector != "自动" and not re.fullmatch(r"[#.]?[\w-]+", selector):
        raise ValueError("正文区域只支持“自动”、#元素ID、.类名或article/main等标签")
    paths = sorted((p for p in source.iterdir() if p.is_file() and
                    p.suffix.lower() in {".mhtml", ".mht", ".html", ".htm"}), key=lambda p: natural_key(p.name))
    if not paths:
        raise ValueError("文件夹内没有MHTML或HTML文件")

    def check():
        if cancel and cancel.is_set():
            raise OrganizeCancelled("整理已取消")

    def emit(message):
        if progress:
            progress(message)

    # 用保存记录恢复普通HTML的来源网址及最初输入顺序；不读浏览器资料。
    manifest = {}
    for csv_path in sorted(source.glob("保存记录_*.csv")):
        try:
            with csv_path.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    filename = row.get("file") or row.get("文件名")
                    url = row.get("final_url") or row.get("最终网址") or row.get("url") or row.get("原网址")
                    if filename and url:
                        manifest[Path(filename).name] = (url, len(manifest))
        except (OSError, UnicodeError, csv.Error):
            continue

    records, potential_directories, errors = [], [], []
    priorities = ["#nr1", "#chaptercontent", "#chapter-content", "#content",
                  ".chapter-content", ".read-content", ".page-content", "article", "main"]
    for i, path in enumerate(paths, 1):
        check()
        emit(f"读取 {i}/{len(paths)}：{path.name}")
        try:
            html, url = read_archive(path, manifest.get(path.name, ("", 0))[0])
            elements = Elements()
            elements.feed(html)
            url = canonical_url(url or elements.base or path.as_uri())
            chosen = next((s for s in priorities if s in elements.selectors), "#__missing__") if selector == "自动" else selector
            parser = Extractor(chosen)
            parser.feed(html)
            links = [{"title": label, "url": canonical_url(urljoin(url, href))}
                     for label, href in parser.links if href]
            heading = "".join(parser.heading).strip() or "".join(parser.title).strip() or path.stem
            raw = "".join(parser.body)
            original_baseline = clean_lines(raw)
            raw, removed_notices = remove_page_notices(raw) if clean_notices else (raw, [])
            baseline = clean_lines(raw)
            if not parser.body_found or not baseline:
                potential_directories.append({"source": str(path), "url": url, "links": links})
                continue
            body, joins = reflow_body(raw) if fix_breaks else (baseline, 0)
            if "\ufffd" in body:
                raise ValueError("正文含有无法恢复的替换字符，请检查原网页")
            if re.sub(r"\s+", "", baseline) != re.sub(r"\s+", "", body):
                raise ValueError("换行整理的文字一致性检查未通过")
            try:
                category, start, end = classify(heading)
            except (ValueError, KeyError):
                category, start, end = "其他", 0, 0
            records.append({
                "title": heading, "category": category, "start": start, "end": end,
                "url": url, "source": str(path), "selector": chosen, "body": body,
                "links": links, "characters": len(body),
                "joined_layout_breaks": joins, "removed_line_breaks": len(baseline.splitlines()) - len(body.splitlines()),
                "sha256": hashlib.sha256(body.encode()).hexdigest(),
                "text_sha256": hashlib.sha256(re.sub(r"\s+", "", body).encode()).hexdigest(),
                "original_text_sha256": hashlib.sha256(re.sub(r"\s+", "", original_baseline).encode()).hexdigest(),
                "removed_page_notices": removed_notices,
                "quote_warnings": quote_warnings(body),
                "manifest_order": manifest.get(path.name, ("", len(paths) + i))[1],
                "mtime": path.stat().st_mtime_ns,
            })
        except (OSError, ValueError, TypeError, LookupError) as exc:
            errors.append({"file": path.name, "error": str(exc)})
    if not records:
        raise ValueError("没有识别到正文。可填写正文区域，例如 #content、.read-content 或 article。" +
                         (f"\n首个错误：{errors[0]['error']}" if errors else ""))

    # 相同网址的重复保存选用最新文件；不同网址同文仅提示，不擅自删掉。
    by_url, duplicates = {}, []
    for r in sorted(records, key=lambda r: r["mtime"]):
        if r["url"] in by_url:
            duplicates.append({"kept": r["source"], "skipped": by_url[r["url"]]["source"], "url": r["url"]})
        by_url[r["url"]] = r
    records = list(by_url.values())
    by_hash, identical_text = {}, []
    for r in records:
        if r["text_sha256"] in by_hash:
            identical_text.append([by_hash[r["text_sha256"]], r["title"]])
        by_hash[r["text_sha256"]] = r["title"]

    directories, skipped = [], []
    for d in potential_directories:
        d["links"] = [link for link in d["links"] if link["url"] in by_url]
        if d["links"]:
            directories.append(d)
        else:
            skipped.append({"file": Path(d["source"]).name, "reason": "未识别到正文，也没有指向本批正文的目录链接"})

    original_urls = []
    first_chapter = next((r["url"] for r in records if r["category"] == "第一部" and r["start"] == 1), None)
    for d in sorted(directories, key=lambda d: natural_key(d["url"])):
        links = [link["url"] for link in d["links"]]
        if first_chapter in links and not original_urls:
            links = links[links.index(first_chapter):]  # 从第一章起，跳过顶部最新章节。
        for url in links:
            if url not in original_urls:
                original_urls.append(url)
    cache = source / ".正文整理顺序.json"
    reference_path = Path(order_reference).expanduser().resolve() if order_reference else cache
    order_source = "本地目录页" if original_urls else "保存记录/文件名（未找到目录）"
    if order_reference or (not original_urls and cache.exists()):
        reference = json.loads(reference_path.read_text(encoding="utf-8-sig"))
        reference_urls = reference.get("urls")
        if reference_urls is None:
            reference_urls = [r["url"] for r in reference["records"]
                              if not r.get("merged_into_previous", False)]
        if not isinstance(reference_urls, list) or not all(isinstance(u, str) for u in reference_urls):
            raise ValueError("顺序参考必须是包含网址顺序的整理校验JSON")
        original_urls = list(dict.fromkeys(canonical_url(u) for u in reference_urls))
        order_source = str(reference_path)
    rank = {url: i for i, url in enumerate(original_urls)}

    missing = {}
    for r in records:
        root = re.sub(r"_\d+(?=\.html?$)", "", r["url"])
        pattern = re.escape(root.rsplit(".", 1)[0]) + r"_(\d+)\.html?"
        r["root_url"] = root if root in by_url else r["url"]
        number = re.fullmatch(pattern, r["url"])
        r["page_number"] = int(number[1]) if number else 1
        for link in r["links"]:
            match = re.fullmatch(pattern, link["url"])
            if match and link["url"] not in by_url:
                missing[link["url"]] = {"title": r["title"], "page": int(match[1]), "url": link["url"]}
        if number and root not in by_url:
            missing[root] = {"title": r["title"], "page": 1, "url": root}
    categories = {"第一部": 0, "第二部": 1, "番外": 2, "同人": 3, "其他": 4}

    def sort_key(r):
        base = by_url.get(r["root_url"], r) if merge_pages else r
        page = r["page_number"] if merge_pages else 1
        if original_order:
            return (rank.get(base["url"], len(rank)), base["manifest_order"],
                    natural_key(base["source"]), page)
        return (categories[base["category"]], base["start"], natural_key(base["title"]), page)

    records.sort(key=sort_key)
    for r in records:
        r["merged_into_previous"] = bool(merge_pages and r["root_url"] != r["url"])
    check()
    output_parent = Path(destination).expanduser().resolve() if destination else source / "整理结果"
    folder = output_parent / datetime.now().strftime("整理_%Y%m%d_%H%M%S_%f")
    folder.mkdir(parents=True, exist_ok=False)
    title = title.strip() or "网页正文"
    parts = [title]
    if (missing or errors or skipped) and not pure_text:
        parts.append("本文件只包含已保存并成功提取的正文；缺页或未识别文件详见整理报告.txt。")
    index = [title + " · 目录", "按本地原目录排序" if original_order else "按章号排序", ""]
    section = None
    chapter_count = 0
    chapter_bodies = []
    page_quote_joins = 0
    for i, r in enumerate(records, 1):
        label = f"{r['title']}〔续页 {r['page_number']}〕" if r["merged_into_previous"] else r["title"]
        index.append(f"{i:03d}. {label}")
        if not r["merged_into_previous"]:
            chapter_count += 1
            if r["category"] in {"第一部", "第二部"} and section != r["category"]:
                section = r["category"]
                parts.append(section)
            parts.append(label)
            parts.append(r["body"])
            chapter_bodies.append({"title": label, "root_url": r["root_url"],
                                   "part_index": len(parts)-1, "source": r["source"]})
        else:
            previous = records[i-2]
            if (i > 1 and previous["root_url"] == r["root_url"]
                    and previous["page_number"] + 1 == r["page_number"]):
                parts[-1], count = join_local_pages(parts[-1], r["body"], fix_breaks)
                page_quote_joins += count
            else:
                parts[-1] += "\n\n" + r["body"]
    chapter_warnings = [
        {"title": c["title"], "root_url": c["root_url"], "source": c["source"],
         "warnings": quote_warnings(parts[c["part_index"]])} for c in chapter_bodies]
    (folder / "正文合并.txt").write_text("\n\n".join(parts) + "\n", encoding="utf-8-sig")
    (folder / "章节目录.txt").write_text("\n".join(index) + "\n", encoding="utf-8-sig")
    missing_list = sorted(missing.values(), key=lambda p: (natural_key(p["url"])))
    (folder / "缺失分页链接.txt").write_text("\n".join(p["url"] for p in missing_list), encoding="utf-8-sig")
    missing_text = [f"{p['title']} — 网页第{p['page']}页\n{p['url']}" for p in missing_list]
    (folder / "缺失分页清单.txt").write_text("\n\n".join(missing_text) or "未发现可识别的缺失分页链接。", encoding="utf-8-sig")
    elapsed = round(time.perf_counter() - started, 3)
    summary = {
        "output": str(folder), "input_files": len(paths), "content_files": len(records),
        "directory_files": len(directories), "missing_pages": len(missing),
        "errors": errors, "unrecognized": skipped, "duplicates": duplicates, "identical_text": identical_text,
        "removed_line_breaks": sum(r["removed_line_breaks"] for r in records),
        "elapsed_seconds": elapsed, "offline": True, "text_verified": True,
        "order": "原目录优先" if original_order else "章号",
        "order_source": order_source, "chapter_entries": chapter_count,
        "removed_notice_count": sum(len(r["removed_page_notices"]) for r in records),
        "quote_warning_count": sum(len(c["warnings"]) for c in chapter_warnings),
        "page_quote_joins": page_quote_joins,
    }
    report = dict(summary, records=[{k: v for k, v in r.items() if k not in {"body", "links"}} for r in records],
                  missing=missing_list, chapter_quote_warnings=chapter_warnings)
    (folder / "提取校验.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    messages = [
        f"读取文件：{len(paths)}；提取正文：{len(records)}；目录页：{len(directories)}。",
        f"排序：{summary['order']}。未被目录引用的页面按保存记录/文件名排列；请查看章节目录。",
        f"顺序依据：{order_source}。",
        f"删除独立分页提示：{summary['removed_notice_count']}处；删除内容逐页记录在提取校验.json。",
        f"角引号疑点：{summary['quote_warning_count']}处，保留原文标点，详见引号检查.txt。",
        f"接回明确跨页的角引号对话：{page_quote_joins}处。",
        f"减少排版断行：{summary['removed_line_breaks']}；清除指定分页提示后，换行修复的非空白字符一致性校验通过。",
        f"检测到缺失分页：{len(missing)}；出错文件：{len(errors)}；未识别文件：{len(skipped)}。",
        f"相同网址重复保存：{len(duplicates)}；不同网址同文提示：{len(identical_text)}。",
        f"本机耗时：{elapsed}秒；未调用AI或网络。编码：UTF-8 BOM，换行：LF。",
        "分页检测支持当前页面列出的同编号 _2.html 等链接；无缺页提示不代表已经取得全文。",
        "自动正文区域和换行采用规则识别；换站点后可更换正文区域，复杂排版需人工复核。",
    ]
    messages += [f"读取失败：{e['file']} — {e['error']}" for e in errors]
    messages += [f"未识别：{e['file']} — {e['reason']}" for e in skipped]
    (folder / "整理报告.txt").write_text("\n".join(messages) + "\n", encoding="utf-8-sig")
    quote_notes = [f"{c['title']} · 合并后本章正文第{w['line']}行：{w['reason']}\n{c['root_url']}"
                   for c in chapter_warnings for w in c["warnings"]]
    (folder / "引号检查.txt").write_text(
        "\n\n".join(quote_notes) or "未发现角引号配对疑点。", encoding="utf-8-sig")
    if original_order and original_urls and write_order_cache:
        cache.write_text(json.dumps({"urls": original_urls, "source": order_source},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
    emit(f"完成：{len(records)}个正文页面，减少{summary['removed_line_breaks']}处折行；缺失{len(missing)}个分页。")
    return summary


def show_dialog(parent, initial_source):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from tkinter.scrolledtext import ScrolledText
    import threading
    import queue
    import subprocess

    window = tk.Toplevel(parent)
    window.title("整理已保存网页")
    window.geometry("800x760")
    window.minsize(740, 720)
    window.transient(parent)
    window.grab_set()
    frame = ttk.Frame(window, padding=22)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="把已保存网页，整理成文本。", font=("PingFang SC", 21, "bold")).pack(anchor="w")
    ttk.Label(frame, text="提取正文、沿用目录顺序、修复排版换行，并检查缺失分页。",
              foreground="#666666").pack(anchor="w", pady=(6, 18))
    source_var = tk.StringVar(value=str(initial_source))
    title_var = tk.StringVar(value="网页正文")
    selector_var = tk.StringVar(value="自动")
    reference_var = tk.StringVar()
    fix_var, order_var, pages_var = (tk.BooleanVar(value=True) for _ in range(3))
    notices_var = tk.BooleanVar(value=True)
    controls = []
    row = ttk.Frame(frame)
    row.pack(fill="x")
    ttk.Label(row, text="网页文件夹").pack(side="left", padx=(0, 8))
    entry = ttk.Entry(row, textvariable=source_var)
    entry.pack(side="left", fill="x", expand=True)
    controls.append(entry)

    def choose():
        folder = filedialog.askdirectory(parent=window, initialdir=source_var.get())
        if folder:
            source_var.set(folder)

    button = ttk.Button(row, text="选择文件夹", command=choose)
    button.pack(side="left", padx=(8, 0))
    controls.append(button)
    for label, variable in [("合并文本标题", title_var), ("正文区域", selector_var)]:
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(12, 0))
        ttk.Label(row, text=label, width=12).pack(side="left")
        entry = ttk.Entry(row, textvariable=variable)
        entry.pack(side="left", fill="x", expand=True)
        controls.append(entry)
    ttk.Label(frame, text="正文区域一般保持“自动”；未识别时可填写 #content、.read-content 或 article。",
              foreground="#666666", wraplength=720).pack(anchor="w", pady=(6, 12))
    row = ttk.Frame(frame)
    row.pack(fill="x", pady=(0, 6))
    ttk.Label(row, text="顺序参考（可选）").pack(side="left")
    entry = ttk.Entry(row, textvariable=reference_var)
    entry.pack(side="left", fill="x", expand=True, padx=8)
    controls.append(entry)

    def choose_reference():
        filename = filedialog.askopenfilename(
            parent=window, title="选择之前整理结果中的提取校验.json",
            filetypes=[("整理校验文件", "*.json")])
        if filename:
            reference_var.set(filename)

    button = ttk.Button(row, text="选择", command=choose_reference)
    button.pack(side="right")
    controls.append(button)
    ttk.Label(frame, text="缺少目录网页时可选旧版提取校验.json；顺序会记住，补存后仍按原目录排列。",
              foreground="#666666", wraplength=720).pack(anchor="w", pady=(0, 8))
    for text, variable in [
        ("优先按已保存目录顺序排列（同人、番外保留原位置）", order_var),
        ("修复网页折行和「」引号断行，保留自然段和对话", fix_var),
        ("将已保存的章内续页接到对应章节后面", pages_var),
        ("删除独立的“本章未完，点击数字分页继续阅读”提示", notices_var),
    ]:
        control = ttk.Checkbutton(frame, text=text, variable=variable)
        control.pack(anchor="w", pady=3)
        controls.append(control)
    ttk.Label(frame, text="结果另存为“整理结果/时间”文件夹，原网页和以往结果均保留。",
              foreground="#666666").pack(anchor="w", pady=(10, 8))
    actions = ttk.Frame(frame)
    actions.pack(fill="x", pady=8)
    status_var = tk.StringVar(value="准备好后开始整理。")
    ttk.Label(frame, textvariable=status_var, wraplength=720).pack(anchor="w")
    log = ScrolledText(frame, height=7, state="disabled", wrap="word", font=("PingFang SC", 11))
    log.pack(fill="both", expand=True, pady=(10, 0))
    events = queue.Queue()
    stop = threading.Event()
    state = {"busy": False, "output": "", "closing": False}

    def append(message):
        log.configure(state="normal")
        log.insert("end", message + "\n")
        log.see("end")
        log.configure(state="disabled")

    def open_output():
        if state["output"]:
            subprocess.Popen(["open", state["output"]])

    def start():
        folder = Path(source_var.get()).expanduser()
        if not folder.is_dir():
            messagebox.showerror("检查文件夹", "请选择存在的网页文件夹。", parent=window)
            return
        settings = dict(title=title_var.get(), selector=selector_var.get().strip() or "自动",
                        fix_breaks=fix_var.get(), original_order=order_var.get(), merge_pages=pages_var.get(),
                        clean_notices=notices_var.get(), order_reference=reference_var.get().strip() or None)
        state["busy"] = True
        stop.clear()
        for control in controls + [start_button]:
            control.configure(state="disabled")
        cancel_button.configure(state="normal")
        result_button.configure(state="disabled")
        status_var.set("正在整理…")

        def work():
            try:
                result = organize(folder, **settings, cancel=stop,
                                  progress=lambda message: events.put(("log", message)))
                events.put(("done", result))
            except OrganizeCancelled:
                events.put(("cancelled", None))
            except Exception as exc:
                events.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def cancel():
        stop.set()
        cancel_button.configure(state="disabled")
        status_var.set("正在停止…")

    start_button = ttk.Button(actions, text="开始整理并导出 TXT", command=start)
    start_button.pack(side="left")
    cancel_button = ttk.Button(actions, text="停止", command=cancel, state="disabled")
    cancel_button.pack(side="left", padx=8)
    result_button = ttk.Button(actions, text="打开整理结果", command=open_output, state="disabled")
    result_button.pack(side="right")

    def close():
        if state["busy"]:
            state["closing"] = True
            cancel()
        else:
            window.destroy()

    def poll():
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == "log":
                    append(value)
                    continue
                state["busy"] = False
                for control in controls + [start_button]:
                    control.configure(state="normal")
                cancel_button.configure(state="disabled")
                if kind == "done":
                    state["output"] = value["output"]
                    result_button.configure(state="normal")
                    status_var.set(f"已整理 {value['content_files']} 页 · 减少 {value['removed_line_breaks']} 处折行"
                                   f" · 清除提示 {value['removed_notice_count']} 处 · 缺页 {value['missing_pages']}")
                    append("输出目录：" + value["output"])
                elif kind == "cancelled":
                    status_var.set("已停止整理。")
                else:
                    status_var.set("整理未完成，请查看错误。")
                    append(value)
                if state["closing"]:
                    window.destroy()
                    return
        except queue.Empty:
            pass
        window.after(100, poll)

    window.protocol("WM_DELETE_WINDOW", close)
    window.after(100, poll)
    return window

