import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from finance_agent.progress import log, severity
from simple_crawler import Crawler
from test_simple_crawler import config, candidate, FakeSources, FakeQwen


class Terminal(io.StringIO):
    def isatty(self): return True


class ColorTests(unittest.TestCase):
    def test_terminal_success_warning_and_error_colors_reset(self):
        stream = Terminal()
        with patch('sys.stdout', stream), patch.dict(os.environ, {}, clear=True), patch('finance_agent.progress._enable_ansi', return_value=True):
            log('新增JSON', '本次新增累计1条（+1）')
            log('剔除', '无实质内容')
            log('错误', '网络失败')
        lines = stream.getvalue().splitlines()
        for line, code in zip(lines, ['\033[92m', '\033[93m', '\033[91m']):
            self.assertTrue(line.startswith(code)); self.assertTrue(line.endswith('\033[0m'))

    def test_redirected_log_is_plain_text_by_default(self):
        stream = io.StringIO()
        with patch('sys.stdout', stream), patch.dict(os.environ, {}, clear=True):
            log('新增JSON', '本次新增累计1条（+1）')
        self.assertNotIn('\033', stream.getvalue())

    def test_no_color_overrides_forced_color(self):
        stream = Terminal()
        with patch('sys.stdout', stream), patch.dict(os.environ, {'NO_COLOR': '1', 'FORCE_COLOR': '1'}, clear=True):
            log('错误', 'offline')
        self.assertNotIn('\033', stream.getvalue())

    def test_force_color_for_color_capable_capture(self):
        stream = io.StringIO()
        with patch('sys.stdout', stream), patch.dict(os.environ, {'FORCE_COLOR': '1'}, clear=True):
            log('剔除', 'offline')
        self.assertTrue(stream.getvalue().startswith('\033[93m'))

    def test_model_and_discovery_errors_are_red_but_screen_keep_is_neutral(self):
        for component in ['Qwen错误', '搜索失败', 'SEC目录暂停', '发现待复核', '运行失败']:
            self.assertEqual(severity(component, 'fixture'), 'error')
        self.assertEqual(severity('筛选', '跳过 | fixture'), 'warning')
        self.assertIsNone(severity('筛选', '保留 | fixture'))
        self.assertIsNone(severity('处理进度', 'fixture'))


class SaveLogTests(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parents[1]/'.runtime/test_color'
        folder.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=folder); self.root = Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def crawler(self, model=None, sources=None):
        return Crawler(config(), self.root, sources or FakeSources(), model or FakeQwen())

    def test_additions_increase_across_batches_and_reset_for_next_invocation(self):
        crawler = self.crawler(); crawler.cfg.collection.max_documents_per_cycle = 1
        for name in ['a', 'b', 'c']: crawler.queue(candidate(name))
        messages = []
        with patch('simple_crawler.log', side_effect=lambda tag, text: messages.append((tag, text))):
            report = crawler.collect(resume=True)
            crawler.queue(candidate('d')); crawler.collect(resume=True)
        additions = [text.split(' | ')[0] for tag, text in messages if tag == '新增JSON']
        self.assertEqual(additions, ['本次新增累计1条（+1）', '本次新增累计2条（+1）', '本次新增累计3条（+1）', '本次新增累计1条（+1）'])
        self.assertEqual(report['summary']['new_records'], 3)

    def test_global_replica_is_one_addition_and_reprocessing_is_update(self):
        class Model(FakeQwen):
            def summarize(self, *args): return dict(super().summarize(*args), market='GLOBAL')
        crawler = self.crawler(Model()); crawler.queue(candidate())
        messages = []
        with patch('simple_crawler.log', side_effect=lambda tag, text: messages.append((tag, text))):
            crawler.collect(resume=True); crawler.collect(reprocess=True)
        self.assertEqual(sum(tag == '新增JSON' for tag, _ in messages), 1)
        updates = [text for tag, text in messages if tag == '更新JSON']
        self.assertEqual(len(updates), 1); self.assertIn('本次新增累计0条（+0）', updates[0])
        self.assertEqual(len(list((self.root/'data').glob('*/*.json'))), 3)

    def test_audit_failure_is_error_and_never_green_addition(self):
        crawler = self.crawler(); crawler.queue(candidate())
        messages = []
        from finance_agent.core import dump
        def fail_audit(path,value):
            if Path(path).parent==self.root/'.runtime/simple/audit':raise OSError('offline audit failure')
            return dump(path,value)
        with patch('simple_crawler.log', side_effect=lambda tag, text: messages.append((tag, text))), \
             patch('record_files.dump', side_effect=fail_audit):
            report = crawler.collect(resume=True)
        self.assertFalse(any(tag in {'新增JSON', '更新JSON'} for tag, _ in messages))
        self.assertTrue(any(tag == '错误' for tag, _ in messages))
        self.assertEqual(report['summary']['new_records'], 0)

    def test_rejection_has_yellow_status_and_no_addition(self):
        crawler = self.crawler(FakeQwen(useful=False)); crawler.queue(candidate())
        messages = []
        with patch('simple_crawler.log', side_effect=lambda tag, text: messages.append((tag, text))):
            crawler.collect(resume=True)
        rejected = [(tag, text) for tag, text in messages if tag == '剔除']
        self.assertEqual(len(rejected), 1)
        self.assertEqual(severity(*rejected[0]), 'warning')
        self.assertFalse(any(tag == '新增JSON' for tag, _ in messages))


if __name__ == '__main__': unittest.main()
