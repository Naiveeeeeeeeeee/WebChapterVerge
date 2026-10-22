"""任务隔离、书名识别、单 TXT 与清理边界的回归测试；只使用合成页面。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from 书籍任务 import BookTask, infer_book_title, detect_local_title, export_text, cleanup_export


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.base = Path(self.tmp.name)
        self.source = self.base / 'pages'; self.source.mkdir()
        self.out = self.base / 'out'; self.trash = self.base / 'trash'
    def tearDown(self): self.tmp.cleanup()
    def page(self, name='a.html', title='第一章_星河旅人-示例书屋', body='第一段正文。', links=''):
        path = self.source / name
        path.write_text(f'<base href="https://example.test/view/{name}"><title>{title}</title><h1>第一章</h1><div id="content">{body}</div>{links}')
        return path
    def test_title_inference(self):
        self.assertEqual(infer_book_title('第1章_星河旅人-示例书屋'), '星河旅人')
        self.assertEqual(infer_book_title('chapter 1 - Star Trail - Example'), 'Star Trail')
        self.assertEqual(infer_book_title('第一章 归来'), '')
        self.assertEqual(infer_book_title('网站', {'og:novel:book_name': '星河旅人'}), '星河旅人')
    def test_each_task_is_separate_and_preserves_retry_order(self):
        a = BookTask.create(['https://example.test/a','https://example.test/b'], root=self.base/'jobs')
        b = BookTask.create(['https://example.test/c'], '另一本书', root=self.base/'jobs')
        (a.path/'a.html').write_text('first')
        a.add({'url':'https://example.test/a','file':'a.html','status':'成功','book_title':'星河旅人'})
        a.add({'url':'https://example.test/b','status':'失败','error':'示例错误'})
        self.assertIn('星河旅人', a.path.name)
        self.assertNotEqual(a.path,b.path)
        self.assertEqual(a.remaining(), ['https://example.test/b'])
        self.assertFalse((b.path/'a.html').exists())
    def test_only_named_txt_no_reports_or_timestamp_folders(self):
        self.page(body='「第一句。」<br>「第二句。」<br><br>本章未完，点击[ 数字分页 ]继续阅读--&gt;&gt;')
        self.assertEqual(detect_local_title(self.source), '星河旅人')
        result = export_text(self.source,self.out)
        self.assertEqual([p.name for p in self.out.iterdir()], ['星河旅人.txt'])
        text = Path(result['output']).read_text(encoding='utf-8-sig')
        self.assertIn('「第一句。」\n「第二句。」',text)
        self.assertNotIn('本章未完',text)
        self.assertEqual([p.name for p in self.source.iterdir()],['a.html'])
        self.assertTrue(result['cleanup_allowed'])
    def test_same_name_does_not_overwrite(self):
        self.page(); a=export_text(self.source,self.out); b=export_text(self.source,self.out)
        self.assertNotEqual(a['output'],b['output'])
        self.assertEqual(Path(b['output']).name,'星河旅人 (2).txt')
    def test_external_disk_without_hardlinks(self):
        from unittest.mock import patch
        import errno
        self.page()
        with patch('书籍任务.os.link', side_effect=OSError(errno.EOPNOTSUPP,'unsupported')):
            result=export_text(self.source,self.out)
        self.assertTrue(Path(result['output']).is_file())
        self.assertEqual(len(list(self.out.iterdir())),1)
    def test_cleanup_moves_only_exported_unchanged_pages(self):
        original=self.page(); result=export_text(self.source,self.out)
        other=self.page('unrelated.html',title='另一本书',body='保留的其他网页')
        notes=self.source/'笔记.txt'; notes.write_text('保留')
        cleaned=cleanup_export(result,self.trash)
        self.assertEqual(cleaned['count'],1)
        self.assertFalse(original.exists()); self.assertTrue(other.exists()); self.assertTrue(notes.exists())
        self.assertTrue(Path(result['output']).exists())
        self.assertTrue((Path(cleaned['trash'])/'a.html').exists())
    def test_changed_source_prevents_cleanup(self):
        p=self.page(); result=export_text(self.source,self.out);p.write_text('改过的页面')
        with self.assertRaisesRegex(ValueError,'改动'): cleanup_export(result,self.trash)
        self.assertTrue(p.exists())
    def test_missing_output_prevents_cleanup(self):
        p=self.page(); result=export_text(self.source,self.out);Path(result['output']).unlink()
        with self.assertRaises(ValueError): cleanup_export(result,self.trash)
        self.assertTrue(p.exists())
    def test_missing_page_or_failed_task_preserves_cache(self):
        self.page('1.html',links='<a href="1_2.html">2</a>')
        result=export_text(self.source,self.out)
        self.assertTrue(result['incomplete']);self.assertFalse(result['cleanup_allowed'])
        with self.assertRaises(ValueError):cleanup_export(result,self.trash)
        self.assertNotIn('详见整理报告',Path(result['output']).read_text(encoding='utf-8-sig'))
    def test_cancel_creates_no_final_output(self):
        from 本地正文整理 import OrganizeCancelled
        self.page(); stop=threading.Event();stop.set()
        with self.assertRaises(OrganizeCancelled):export_text(self.source,self.out,cancel=stop)
        self.assertFalse(self.out.exists())
    def test_unknown_name_requires_input(self):
        self.page(title='第一章')
        with self.assertRaisesRegex(ValueError,'书名'):export_text(self.source,self.out)
        result=export_text(self.source,self.out,title='手动书名')
        self.assertEqual(Path(result['output']).name,'手动书名.txt')


if __name__=='__main__':unittest.main()
