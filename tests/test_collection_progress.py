import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from collection_progress import CollectionProgress, duration
from simple_crawler import Crawler
from test_simple_crawler import config, candidate, FakeQwen, FakeSources


class Clock:
    value = 0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


class ProgressTests(unittest.TestCase):
    def tracker(self):
        clock = Clock(); messages = []
        return CollectionProgress(lambda tag, text: messages.append((tag, text)), clock), clock, messages

    def finish(self, progress, clock, identity, seconds):
        progress.begin_item(identity); clock.advance(seconds); progress.finish_item(identity)

    def test_duration_and_unknown(self):
        self.assertEqual(duration(None), '暂无法估算')
        self.assertEqual(duration(-1), '00:00:00')
        self.assertEqual(duration(60.1), '00:01:01')
        self.assertEqual(duration(90061), '1天 01:01:01')

    def test_queue_eta_needs_samples_and_uses_current_remaining(self):
        progress, clock, _ = self.tracker(); progress.set_queue(str(i) for i in range(10))
        progress.set_discovery(True)
        self.assertIsNone(progress.snapshot()['queue_eta_seconds'])
        self.finish(progress, clock, '0', 10)
        self.assertIsNone(progress.snapshot()['queue_eta_seconds'])
        self.finish(progress, clock, '1', 20)
        state = progress.snapshot()
        self.assertEqual(state['processed_unique'], 2)
        self.assertEqual(state['elapsed_seconds'], 30)
        self.assertEqual(state['queue_eta_seconds'], 120)
        self.assertEqual(state['overall_eta_seconds'], 120)

    def test_discovery_work_is_not_treated_as_body_runtime(self):
        progress, clock, _ = self.tracker(); progress.set_queue(['a', 'b', 'c'])
        clock.advance(100)
        self.finish(progress, clock, 'a', 10); self.finish(progress, clock, 'b', 20)
        self.assertEqual(progress.snapshot()['elapsed_seconds'], 130)
        self.assertEqual(progress.snapshot()['queue_eta_seconds'], 15)

    def test_budget_continuation_accumulates_time_and_counts_once(self):
        progress, clock, _ = self.tracker(); progress.set_queue(['a', 'b', 'c'])
        progress.begin_item('a'); clock.advance(5); progress.finish_item('a', terminal=False)
        self.assertEqual(progress.snapshot()['processed_unique'], 0)
        self.finish(progress, clock, 'a', 10); self.finish(progress, clock, 'b', 20)
        self.assertEqual(progress.snapshot()['processed_unique'], 2)
        self.assertEqual(progress.snapshot()['queue_eta_seconds'], 17.5)
        progress.finish_item('a')
        self.assertEqual(progress.snapshot()['sample_count'], 2)

    def test_new_candidates_and_unknown_discovery_do_not_get_false_overall_eta(self):
        progress, clock, _ = self.tracker(); progress.set_queue(['a', 'b', 'c'])
        self.finish(progress, clock, 'a', 10); self.finish(progress, clock, 'b', 10)
        self.assertIsNone(progress.snapshot()['overall_eta_seconds'])
        progress.set_queue(['c', 'd', 'e'])
        self.assertEqual(progress.snapshot()['queue_eta_seconds'], 30)
        progress.set_discovery(True)
        self.assertEqual(progress.snapshot()['overall_eta_seconds'], 30)

    def test_long_current_item_does_not_show_zero_remaining_while_running(self):
        progress, clock, _ = self.tracker(); progress.set_queue(['a', 'b', 'c'])
        self.finish(progress, clock, 'a', 10); self.finish(progress, clock, 'b', 10)
        progress.begin_item('c'); clock.advance(4)
        self.assertEqual(progress.snapshot()['queue_eta_seconds'], 6)
        clock.advance(7)
        self.assertIsNone(progress.snapshot()['queue_eta_seconds'])
        self.assertEqual(progress.snapshot()['current_item_elapsed_seconds'], 11)

    def test_deferred_and_paused_work_has_no_promise_of_completion(self):
        progress, clock, _ = self.tracker(); progress.set_queue(['a'])
        progress.set_discovery(True)
        progress.begin_item('a'); clock.advance(10); progress.finish_item('a', deferred=True)
        self.assertEqual(progress.snapshot()['queue_eta_seconds'], 0)
        self.assertIsNone(progress.snapshot()['overall_eta_seconds'])
        self.assertIn('待复核', progress.snapshot()['overall_eta_basis'])
        progress.set_discovery(False, paused=True)
        self.assertIn('暂停', progress.snapshot()['overall_eta_basis'])

    def test_directory_eta_only_estimates_known_pages(self):
        progress, clock, messages = self.tracker()
        progress.phase('SEC目录', 11, 9, 2, unknown=3, catalogs=(2, 5))
        progress.phase('SEC目录', 12, 8, 4, unknown=3, catalogs=(2, 5))
        state = progress.snapshot()['phases']['SEC目录']
        self.assertEqual(state['eta_seconds'], 24)
        self.assertEqual(state['elapsed_seconds'], 6)
        self.assertEqual(state['unknown'], 3)
        self.assertIn('另有3个目录页数待展开', messages[-1][1])
        self.assertIsNone(progress.snapshot()['overall_eta_seconds'])

    def test_search_fallback_time_is_included_when_task_finishes(self):
        progress, _, _ = self.tracker()
        progress.phase('历史搜索', 0, 3, 4, units=0)
        progress.phase('历史搜索', 1, 2, 6)
        progress.phase('历史搜索', 2, 1, 8)
        self.assertEqual(progress.snapshot()['phases']['历史搜索']['eta_seconds'], 9)

    def test_fast_duplicate_skips_do_not_distort_body_speed(self):
        progress, clock, _ = self.tracker(); progress.set_queue(['old', 'a', 'b', 'c'])
        progress.finish_item('old', sample=False)
        self.finish(progress, clock, 'a', 10); self.finish(progress, clock, 'b', 10)
        self.assertEqual(progress.snapshot()['queue_eta_seconds'], 10)
        self.assertEqual(progress.snapshot()['sample_count'], 2)

    def test_heartbeat_and_interruption_close_thread(self):
        seen = threading.Event(); messages = []
        def log(tag, text):
            messages.append(tag)
            if tag == '进度心跳': seen.set()
        progress = CollectionProgress(log, interval=0.01)
        with self.assertRaises(KeyboardInterrupt):
            with progress:
                self.assertTrue(seen.wait(0.5))
                raise KeyboardInterrupt()
        self.assertFalse(progress.thread.is_alive())
        self.assertEqual(messages[-1], '运行中断')


class ProgressIntegrationTests(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parents[1]/'.runtime/test_progress'
        folder.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=folder); self.root = Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()

    def test_main_collection_preserves_samples_across_batches_and_saves_snapshot(self):
        clock = Clock()
        class Model(FakeQwen):
            def screen(self, *args): clock.advance(2); return super().screen(*args)
            def summarize(self, *args): clock.advance(8); return super().summarize(*args)
        crawler = Crawler(config(), self.root, FakeSources(), Model())
        crawler.cfg.collection.max_documents_per_cycle = 1
        for i in range(3): crawler.queue(candidate(str(i)))
        with patch('simple_crawler.CollectionProgress', side_effect=lambda log: CollectionProgress(log, clock)):
            report = crawler.collect(resume=True)
        self.assertEqual(report['progress']['elapsed_seconds'], 30)
        self.assertEqual(report['progress']['processed_unique'], 3)
        self.assertEqual(report['progress']['overall_eta_seconds'], 0)
        self.assertEqual(report['batch_reports'][1]['progress']['queue_eta_seconds'], 10)
        self.assertEqual(json.loads((self.root/'logs/latest_run.json').read_text(encoding='utf-8'))['progress'], report['progress'])
        self.assertIsNone(crawler._progress)

    def test_missing_date_keeps_overall_eta_unknown(self):
        crawler = Crawler(config(), self.root, FakeSources(), FakeQwen(missing=True))
        crawler.queue(candidate())
        report = crawler.collect(resume=True)
        self.assertEqual(report['progress']['deferred_records'], 1)
        self.assertIsNone(report['progress']['overall_eta_seconds'])

    def test_directory_remaining_pages_are_visible_in_report(self):
        from test_sec_discovery import sec_config, SecSources, SecModel, quarter_jobs, master, filing
        cfg = sec_config()
        sources = SecSources({quarter_jobs('2019-01-01', '2019-01-31')[0]['url']: master([filing(i) for i in range(1, 6)])})
        crawler = Crawler(cfg, self.root, sources, SecModel())
        report = crawler.collect()
        phase = report['progress']['phases']['SEC目录']
        self.assertEqual(phase['completed'], 1)
        self.assertEqual(phase['remaining'], 2)
        self.assertFalse(report['progress']['discovery_complete'])
        self.assertIsNone(report['progress']['overall_eta_seconds'])


if __name__ == '__main__': unittest.main()
