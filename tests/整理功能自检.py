"""仅使用自建本地页面验证整理功能；不启动浏览器、不连接网络。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import base64
import json
import threading
import unittest

from 本地正文整理 import organize, reflow_body, OrganizeCancelled, repair_quote_layout


class OrganizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "pages"
        self.source.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def page(self, filename, url, heading, body=None, links=""):
        html = (f'<html><head><meta charset="utf-8"><base href="{url}"></head>'
                f'<body><h1>{heading}</h1>' +
                (f'<div id="nr1">{body}</div>' if body is not None else "") +
                links + "</body></html>")
        (self.source / filename).write_text(html, encoding="utf-8")

    def run_organize(self, **kwargs):
        result = organize(self.source, self.root / "out", **kwargs)
        report = json.loads((Path(result["output"]) / "提取校验.json").read_text())
        text = (Path(result["output"]) / "正文合并.txt").read_text(encoding="utf-8-sig")
        return result, report, text

    def test_original_order_includes_interleaved_specials(self):
        self.page("z.html", "https://example.test/view/1.html", "第一章", "一段正文。")
        self.page("a.html", "https://example.test/view/2.html", "第二章", "二段正文。")
        self.page("b.html", "https://example.test/view/3.html", "同人", "额外段落。")
        self.page("c.html", "https://example.test/view/4.html", "第二部（1）", "第二部正文。")
        self.page("index.html", "https://example.test/list/1.html", "目录", links=
                  '<a href="/view/4.html">最新</a><a href="/view/1.html">第一章</a>'
                  '<a href="/view/3.html">同人</a><a href="/view/2.html">第二章</a>'
                  '<a href="/view/4.html">第二部</a>')
        result, report, _ = self.run_organize()
        self.assertEqual([r["title"] for r in report["records"]], ["第一章", "同人", "第二章", "第二部（1）"])
        self.assertEqual(result["directory_files"], 1)

    def test_local_subpages_and_missing_pages(self):
        links = '<a href="/view/10_2.html">【2】</a><a href="/view/10_3.html">【3】</a>'
        self.page("z.html", "https://example.test/view/10.html", "第一章", "首段正文。", links)
        self.page("a.html", "https://example.test/view/10_2.html", "第一章（2）", "续页正文。")
        result, report, text = self.run_organize()
        self.assertEqual(result["missing_pages"], 1)
        self.assertEqual(report["missing"][0]["url"], "https://example.test/view/10_3.html")
        self.assertTrue(report["records"][1]["merged_into_previous"])
        self.assertLess(text.index("首段正文"), text.index("续页正文"))
        self.assertNotIn("第一章（2）", text)

    def test_missing_first_page(self):
        self.page("a.html", "https://example.test/view/10_2.html", "第一章（2）", "续页正文。")
        result, report, _ = self.run_organize()
        self.assertEqual(result["missing_pages"], 1)
        self.assertEqual(report["missing"][0]["page"], 1)

    def test_linebreaks_and_character_integrity(self):
        raw = "作者：测试\ue000字数：100\ue000\ue000　　句子前半\ue000后半。\ue000\ue000　　“对话。”"
        text, count = reflow_body(raw)
        self.assertEqual(text, "作者：测试\n字数：100\n句子前半后半。\n“对话。”")
        self.assertEqual(count, 1)

    def test_double_break_continuation_but_keep_dialogue(self):
        first = "这是一段用来测试网页硬折行修复的文字还没有结束"
        raw = first + "\ue000\ue000接上后半句。\ue000\ue000“另一段对话。”"
        text, count = reflow_body(raw)
        self.assertEqual(text, first + "接上后半句。\n“另一段对话。”")
        self.assertEqual(count, 1)

    def test_selector_and_keep_linebreaks(self):
        (self.source / "a.html").write_text(
            '<h1>测试</h1><article>前半<br>后半。<script>广告脚本</script></article>', encoding="utf-8")
        _, _, text = self.run_organize(fix_breaks=False)
        self.assertIn("前半\n后半。", text)
        self.assertNotIn("广告脚本", text)

    def test_encoding_and_mhtml_root(self):
        html = '<meta charset="gb18030"><h1>第一章</h1><div id="nr1">中文正常。</div>'.encode("gb18030")
        archive = (b'MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary="part"; start="<root>"\r\n\r\n'
                   b'--part\r\nContent-Type: text/html\r\nContent-ID: <frame>\r\n\r\n<h1>iframe</h1>\r\n'
                   b'--part\r\nContent-Type: text/html\r\nContent-ID: <root>\r\n'
                   b'Content-Location: https://example.test/view/1.html\r\nContent-Transfer-Encoding: base64\r\n\r\n' +
                   base64.b64encode(html) + b'\r\n--part--\r\n')
        (self.source / "a.mhtml").write_bytes(archive)
        _, report, text = self.run_organize()
        self.assertEqual(report["records"][0]["title"], "第一章")
        self.assertIn("中文正常。", text)
        self.assertNotIn("iframe", text)

    def test_duplicates_unknown_and_non_overwrite(self):
        self.page("a.html", "https://example.test/view/1.html", "第一章", "正文。")
        self.page("b.html", "https://example.test/view/1.html", "第一章", "正文。")
        (self.source / "unknown.html").write_text("<h1>标题</h1><aside>未知结构</aside>")
        first, _, _ = self.run_organize()
        second, _, _ = self.run_organize()
        self.assertNotEqual(first["output"], second["output"])
        self.assertEqual(len(first["duplicates"]), 1)
        self.assertEqual(len(first["unrecognized"]), 1)
        self.assertEqual(first["content_files"], 1)

    def test_cancel_and_bad_selector(self):
        self.page("a.html", "https://example.test/view/1.html", "第一章", "正文。")
        stop = threading.Event()
        stop.set()
        with self.assertRaises(OrganizeCancelled):
            self.run_organize(cancel=stop)
        with self.assertRaises(ValueError):
            self.run_organize(selector="#content > p")

    def test_corner_quote_dialogue_boundary_and_broken_closer(self):
        raw = "「第一句。」\ue000「第二句。 \ue000\ue000　　」「第三句。」"
        text, _ = reflow_body(raw)
        self.assertEqual(text, "「第一句。」\n「第二句。」\n「第三句。」")
        self.assertEqual(repair_quote_layout('「\n一个词」')[0], '「一个词」')
        # 反向引号或混用引号不应该让多个段落吞并。
        self.assertEqual(repair_quote_layout('」原文反向「\n叙述。\n」下一句「')[0],
                         '」原文反向「\n叙述。\n」下一句「')
        self.assertEqual(repair_quote_layout('「混用"\n叙述。\n」下一句')[0],
                         '「混用"\n叙述。\n」下一句')

    def test_remove_exact_notice_only_and_allow_opt_out(self):
        note = "本章未完，点击[ 数字分页 ]继续阅读-->>"
        narrative = "他继续阅读手中的书籍。"
        self.page("a.html", "https://example.test/view/1.html", "第一章",
                  narrative + "<br><br>" + note)
        result, report, text = self.run_organize()
        self.assertNotIn(note, text)
        self.assertIn(narrative, text)
        self.assertEqual(result["removed_notice_count"], 1)
        self.assertEqual(report["records"][0]["removed_page_notices"], [note])
        _, _, original = self.run_organize(clean_notices=False)
        self.assertIn(note, original)

    def test_first_page_alias_not_missing_or_duplicated(self):
        self.page("a.html", "https://example.test/view/1.html", "第一章", "正文。",
                  '<a href="/view/1_1.html">【1】</a><a href="/view/1_2.html">【2】</a>')
        self.page("b.html", "https://example.test/view/1_1.html", "第一章", "正文。")
        self.page("c.html", "https://example.test/view/1_2.html", "续页", "续页。")
        result, report, _ = self.run_organize()
        self.assertEqual(result["missing_pages"], 0)
        self.assertEqual(result["content_files"], 2)
        self.assertEqual(len(result["duplicates"]), 1)

    def test_remember_reference_order_after_redownload_without_directory(self):
        self.page("z.html", "https://example.test/view/1.html", "第一章", "正文。")
        self.page("a.html", "https://example.test/view/2.html", "同人", "同人正文。")
        self.page("b.html", "https://example.test/view/3.html", "第二章", "正文二。")
        reference = self.root / "reference.json"
        reference.write_text(json.dumps({"records": [
            {"url": f"https://example.test/view/{i}.html"} for i in (1, 2, 3)]}))
        _, report, _ = self.run_organize(order_reference=reference)
        self.assertEqual([r["title"] for r in report["records"]], ["第一章", "同人", "第二章"])
        _, cached, _ = self.run_organize()
        self.assertEqual([r["url"] for r in report["records"]], [r["url"] for r in cached["records"]])

    def test_quote_split_across_adjacent_pages(self):
        self.page("a.html", "https://example.test/view/1.html", "第一章", "「请稍等，",
                  '<a href="/view/1_2.html">【2】</a>')
        self.page("b.html", "https://example.test/view/1_2.html", "第一章", "马上回来。」<br><br>另起一段。")
        result, report, text = self.run_organize()
        self.assertIn("「请稍等，马上回来。」\n另起一段。", text)
        self.assertEqual(result["page_quote_joins"], 1)
        self.assertEqual(result["quote_warning_count"], 0)

    def test_do_not_join_quotes_across_missing_page(self):
        self.page("a.html", "https://example.test/view/1.html", "第一章", "「请稍等，",
                  '<a href="/view/1_2.html">【2】</a><a href="/view/1_3.html">【3】</a>')
        self.page("c.html", "https://example.test/view/1_3.html", "第一章", "马上回来。」")
        result, _, text = self.run_organize()
        self.assertNotIn("「请稍等，马上回来。」", text)
        self.assertEqual(result["page_quote_joins"], 0)
        self.assertEqual(result["missing_pages"], 1)


if __name__ == "__main__":
    unittest.main()
