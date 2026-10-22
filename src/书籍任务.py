"""书名识别、独立任务缓存、单 TXT 导出与经校验的缓存清理。"""
from collections import Counter
from datetime import datetime
import hashlib
import errno
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from 应用路径 import DATA, preferences, SETTINGS

PAGE_SUFFIXES = {'.html', '.htm', '.mhtml', '.mht'}


def safe_book_name(value):
    value = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', '_', value).strip(' .')
    while len(value.encode('utf-8')) > 160:
        value = value[:-1]
    return value


def infer_book_title(title='', metadata=None):
    metadata = {str(k).lower(): str(v).strip() for k, v in (metadata or {}).items()}
    for key in ('og:novel:book_name', 'novel:book_name', 'og:book_name', 'book_name'):
        if metadata.get(key):
            return safe_book_name(metadata[key].strip('《》 '))
    title = title.strip()
    bracket = re.search(r'《([^《》]+)》', title)
    if bracket:
        return safe_book_name(bracket[1])
    # 使用标题的分隔结构；只有章节标题时保持未知，交给用户填写，不拿第一章当书名。
    segments = re.split(r'\s*[_|｜—–-]\s*', title)
    chapter = re.compile(r'^(?:第\s*[零〇一二三四五六七八九十百千万两\d]+\s*[章回节節卷部]|chapter\s*\d+)', re.I)
    for segment in segments:
        segment = re.sub(r'\s*(?:最新章[节節].*|全文[阅閱][读讀].*|免费[阅閱][读讀].*|免費[閱阅][讀读].*)$', '', segment).strip()
        if not segment or chapter.search(segment):
            continue
        if re.search(r'(?:书屋|書屋|小说网|小說網|阅读网|閱讀網|创作网站|創作網站|^首页$|^首頁$)', segment):
            continue
        return safe_book_name(segment)
    return ''


class BookHead(HTMLParser):
    def __init__(self):
        super().__init__(); self.in_title = False; self.title = []; self.metadata = {}
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'title': self.in_title = True
        if tag == 'meta': self.metadata[attrs.get('property') or attrs.get('name', '')] = attrs.get('content', '')
    def handle_endtag(self, tag):
        if tag == 'title': self.in_title = False
    def handle_data(self, data):
        if self.in_title: self.title.append(data)


def detect_local_title(source):
    from 本地正文整理 import read_archive
    names = []
    for path in sorted(Path(source).iterdir()):
        if path.is_symlink() or path.suffix.lower() not in PAGE_SUFFIXES or not path.is_file(): continue
        try:
            html, _ = read_archive(path)
            parser = BookHead(); parser.feed(html)
            name = infer_book_title(''.join(parser.title), parser.metadata)
            if name: names.append(name)
        except (OSError, ValueError): pass
        if len(names) >= 12: break
    return Counter(names).most_common(1)[0][0] if names else ''


def text_output_directory():
    return Path(preferences().get('text_output_directory') or Path.home() / 'Documents/小说TXT').expanduser()


def remember_text_output(folder):
    DATA.mkdir(parents=True, exist_ok=True)
    settings = preferences(); settings['text_output_directory'] = str(Path(folder).expanduser().resolve())
    temporary = SETTINGS.with_suffix('.tmp')
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.chmod(0o600); temporary.replace(SETTINGS)


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class BookTask:
    def __init__(self, path, data):
        self.path, self.data = Path(path), data

    @classmethod
    def create(cls, urls, title='', root=None):
        root = Path(root or DATA / '网页缓存'); root.mkdir(parents=True, exist_ok=True)
        task_id = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
        folder = root / ((safe_book_name(title) or '待识别书名') + '_' + task_id)
        folder.mkdir()
        task = cls(folder, {'id': task_id, 'title': title, 'urls': list(dict.fromkeys(urls)), 'rows': [], 'status': '待保存'})
        task.save()
        (folder / '.正文整理顺序.json').write_text(json.dumps({'urls': task.data['urls']}, ensure_ascii=False), encoding='utf-8')
        return task

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        return cls(path, json.loads((path / '.任务.json').read_text()))

    @classmethod
    def recent(cls, root=None):
        root = Path(root or DATA / '网页缓存')
        tasks = []
        if root.exists():
            for path in sorted(root.glob('*/.任务.json'), key=lambda p: p.stat().st_mtime, reverse=True):
                try: tasks.append(cls.load(path.parent))
                except (OSError, ValueError): pass
        return tasks[:40]

    def save(self):
        tmp = self.path / '.任务.tmp'
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8'); tmp.replace(self.path / '.任务.json')

    def add(self, row):
        rows = self.data['rows']
        rows[:] = [r for r in rows if r.get('url') != row.get('url')]
        rows.append(row)
        if not self.data.get('title') and row.get('book_title'):
            self.data['title'] = row['book_title']
            target = self.path.parent / (safe_book_name(row['book_title']) + '_' + self.data['id'])
            if target != self.path:
                self.path.rename(target); self.path = target
        self.data['status'] = '已保存'
        self.save()
        # 内部清单为本地解析器提供来源 URL 和顺序；不放进用户选择的 TXT 目录。
        from 网页批量保存 import write_report
        rank = {u: i for i, u in enumerate(self.data['urls'])}
        write_report(self.path, sorted(rows, key=lambda r: rank.get(r['url'], len(rank))), '本次任务')

    def remaining(self):
        done = {r['url'] for r in self.data['rows'] if r.get('status') == '成功' and (self.path / r.get('file', '')).is_file()}
        return [u for u in self.data['urls'] if u not in done]


def export_text(source, destination, *, title='', task=None, cancel=None, progress=None, **options):
    from 本地正文整理 import organize, OrganizeCancelled
    source, destination = Path(source).expanduser().resolve(), Path(destination).expanduser().resolve()
    name = safe_book_name(title.strip() or detect_local_title(source))
    if not name:
        raise ValueError('未能可靠识别书名，请在“小说名称”里填写后再导出。')
    snapshots = {p.name: {'path': str(p), 'sha256': file_hash(p)} for p in source.iterdir()
                 if p.is_file() and not p.is_symlink() and p.suffix.lower() in PAGE_SUFFIXES}
    if not snapshots: raise ValueError('这个文件夹里还没有可导出的网页。')
    with tempfile.TemporaryDirectory(prefix='chapter-export-') as tmp:
        summary = organize(source, tmp, title=name, cancel=cancel, progress=progress,
                           pure_text=True, write_order_cache=False, **options)
        report = json.loads((Path(summary['output']) / '提取校验.json').read_text())
        if cancel and cancel.is_set(): raise OrganizeCancelled('导出已取消')
        content = (Path(summary['output']) / '正文合并.txt').read_bytes()
        destination.mkdir(parents=True, exist_ok=True)
        # 先完整写入临时文件，再以不覆盖的硬链接原子发布；不存在“成功了一半”的最终 TXT。
        fd, temp_path = tempfile.mkstemp(prefix='.chapter-export-', dir=destination)
        temporary = Path(temp_path)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            index = 1
            while True:
                target = destination / (name + (f' ({index})' if index > 1 else '') + '.txt')
                try:
                    os.link(temporary, target)
                    break
                except FileExistsError:
                    index += 1
                except OSError as exc:
                    # exFAT 等外置磁盘不支持硬链接，改用排他创建，仍不覆盖旧 TXT。
                    if exc.errno not in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP}:
                        raise
                    try:
                        stream = target.open('xb')
                    except FileExistsError:
                        index += 1
                        continue
                    try:
                        with stream:
                            stream.write(content); stream.flush(); os.fsync(stream.fileno())
                    except BaseException:
                        target.unlink(missing_ok=True)
                        raise
                    break
            if file_hash(target) != hashlib.sha256(content).hexdigest():
                target.unlink(); raise OSError('导出文件校验失败')
        finally:
            temporary.unlink(missing_ok=True)
    incomplete = bool(summary['errors'] or summary['unrecognized'] or summary['missing_pages'] or (task and task.remaining()))
    # 只有正文校验通过的源网页可以清理。目录、未识别文件、失败页及用户新增文件均保留。
    used = {Path(r['source']).name for r in report['records']}
    cleanup_files = [snapshots[n] for n in used if n in snapshots]
    result = dict(summary, title=name, output=str(target), output_sha256=file_hash(target),
                  source=str(source), cleanup_files=cleanup_files, cleanup_allowed=not incomplete,
                  incomplete=incomplete, cleaned=False)
    if task:
        task.data.update(title=name, status='TXT 已导出', export=result); task.save()
    return result


def cleanup_export(result, trash_root=None):
    """调用方在用户点击清理后调用；只移动已校验源网页，永不递归删除源目录。"""
    if not result.get('cleanup_allowed') or result.get('cleaned'):
        raise ValueError('这次导出存在缺页、失败或已清理，保留网页以便补存。')
    output = Path(result['output'])
    if not output.is_file() or file_hash(output) != result['output_sha256']:
        raise ValueError('导出的 TXT 已移动、删除或改动，请确认文件后重新导出；本次不清理网页。')
    source = Path(result['source']).resolve()
    candidates = []
    for item in result['cleanup_files']:
        path = Path(item['path'])
        if path.is_symlink() or path.parent.resolve() != source or path.suffix.lower() not in PAGE_SUFFIXES:
            raise ValueError('网页路径发生变化，已停止清理。')
        if not path.is_file() or file_hash(path) != item['sha256']:
            raise ValueError('网页文件已改动，已停止清理。')
        candidates.append(path)
    trash = Path(trash_root or Path.home() / '.Trash') / ('网页缓存_' + uuid.uuid4().hex[:12])
    trash.mkdir(parents=True, exist_ok=False)
    moved = []
    try:
        for path in candidates:
            target = trash / path.name
            shutil.move(str(path), str(target)); moved.append((path, target))
    except Exception:
        for original, target in reversed(moved):
            if target.exists() and not original.exists(): shutil.move(str(target), str(original))
        raise
    result['cleaned'] = True
    return {'count': len(moved), 'trash': str(trash)}
