import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from test_simple_crawler import config, candidate, FakeSources, FakeQwen
from simple_crawler import Crawler, source_id
from finance_agent.safety import FetchError
from qwen_agent import ModelBudget, OutputError


class SummaryTests(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parents[1] / '.runtime/test_summary'
        folder.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=folder)
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def crawler(self, model=None, sources=None):
        return Crawler(config(), self.root, sources or FakeSources(), model or FakeQwen())

    def test_mixed_outcomes_are_separate_and_persisted(self):
        class Sources(FakeSources):
            def fetch(self, item):
                if item['url'].endswith('/network'):
                    raise FetchError('network_error', 'offline failure')
                return super().fetch(item)

        class Model(FakeQwen):
            def screen(self, item, monitoring):
                return {'useful': not item['url'].endswith('/reject'), 'reason': 'fixture'}

            def summarize(self, page, monitoring):
                row = super().summarize(page, monitoring)
                if page['url'].endswith('/undated'): row['time'] = None
                if page['url'].endswith('/outside'): row['time'] = '2020-01-01'
                if page['url'].endswith('/bodyreject'): row.update(useful=False, reason='fixture')
                return row

        crawler = self.crawler(Model(), Sources())
        crawler.cfg.collection.max_documents_per_cycle = 2
        for name in ['good', 'reject', 'network', 'undated', 'outside', 'bodyreject']:
            crawler.queue(candidate(name))
        messages = []
        with patch('simple_crawler.log', side_effect=lambda tag, message: messages.append((tag, message))):
            report = crawler.collect(resume=True)
        summary = report['summary']
        self.assertEqual(summary['new_records'], 1)
        self.assertEqual(summary['updated_records'], 0)
        self.assertEqual(summary['rejected_records'], 3)
        self.assertEqual(summary['deferred_records'], 2)
        self.assertEqual(summary['deferred_reasons'], {'network_error': 1, 'publication_unconfirmed': 1})
        self.assertEqual(summary['unattempted_records'], 0)
        self.assertEqual(summary['search']['searched'], 0)
        self.assertEqual(json.loads((self.root / 'logs/latest_run.json').read_text(encoding='utf-8'))['summary'], summary)
        self.assertIn(('数据结果', '本次新增1条，更新0条（均已校验，不含GLOBAL重复副本）'), messages)

    def test_global_counts_once_and_reprocessing_counts_as_update(self):
        class GlobalModel(FakeQwen):
            def summarize(self, page, monitoring):
                return dict(super().summarize(page, monitoring), market='GLOBAL')
        crawler = self.crawler(GlobalModel())
        crawler.queue(candidate('global'))
        first = crawler.collect(resume=True)['summary']
        self.assertEqual(first['new_records'], 1)
        self.assertEqual(first['by_market'], {'GLOBAL': {'new': 1, 'updated': 0}})
        self.assertEqual(len(list((self.root / 'data').glob('*/*.json'))), 3)
        second = crawler.collect(reprocess=True)['summary']
        self.assertEqual(second['new_records'], 0)
        self.assertEqual(second['updated_records'], 1)
        self.assertEqual(second['records_before'], second['records_after'])

    def test_success_after_call_budget_is_not_counted_as_failure(self):
        class BudgetModel(FakeQwen):
            first = True
            def summarize(self, page, monitoring):
                if self.first:
                    self.first = False
                    self.calls = 30
                    raise ModelBudget('offline call limit')
                return super().summarize(page, monitoring)
        crawler = self.crawler(BudgetModel())
        crawler.queue(candidate())
        report = crawler.collect(resume=True)
        self.assertEqual(report['batches'], 2)
        self.assertEqual(report['summary']['new_records'], 1)
        self.assertEqual(report['summary']['deferred_records'], 0)
        self.assertEqual(report['summary']['components']['page_cache_hits'], 1)

    def test_single_batch_reports_unattempted_separately(self):
        crawler = self.crawler()
        crawler.cfg.collection.max_documents_per_cycle = 1
        for name in ['one', 'two', 'three']: crawler.queue(candidate(name))
        summary = crawler.collect(resume=True, one_batch=True)['summary']
        self.assertEqual(summary['new_records'], 1)
        self.assertEqual(summary['unattempted_records'], 2)
        self.assertEqual(summary['deferred_records'], 0)

    def test_duplicate_and_previous_rejection_not_new(self):
        crawler = self.crawler()
        crawler.queue(candidate())
        crawler.collect(resume=True)
        crawler.queue(candidate())
        crawler.queue(candidate('oldrejected'))
        crawler.state['records'][source_id(candidate('oldrejected')['url'])] = {'status': 'rejected', 'scope': crawler.scope}
        summary = crawler.collect(resume=True)['summary']
        self.assertEqual(summary['new_records'], 0)
        self.assertEqual(summary['skipped_reasons'], {'duplicate_saved': 1, 'previously_rejected': 1})

    def test_context_and_call_budget_have_distinct_final_reasons(self):
        class BudgetModel(FakeQwen):
            def screen(self, item, monitoring):
                if item['url'].endswith('/calls'):
                    self.calls = 30
                    raise ModelBudget('call budget')
                raise ModelBudget('context budget')
        crawler = self.crawler(BudgetModel())
        crawler.queue(candidate('calls'))
        crawler.queue(candidate('context'))
        summary = crawler.collect(resume=True)['summary']
        self.assertEqual(summary['deferred_reasons'], {'call_budget': 1, 'context_budget': 1})
        self.assertEqual(summary['deferred_records'], 2)

    def test_errors_are_classified_by_component(self):
        class Sources(FakeSources):
            def fetch(self, item):
                if item['url'].endswith('/pdf'): raise ValueError('PDF needs OCR')
                if item['url'].endswith('/blocked'): raise FetchError('blocked', 'robots.txt')
                return super().fetch(item)
        class Model(FakeQwen):
            def summarize(self, page, monitoring):
                if page['url'].endswith('/model'): raise OutputError('invalid model evidence')
                if page['url'].endswith('/ollama'): raise httpx.ConnectError('offline Ollama')
                return super().summarize(page, monitoring)
        crawler = self.crawler(Model(), Sources())
        for name in ['pdf', 'blocked', 'model', 'ollama']: crawler.queue(candidate(name))
        summary = crawler.collect(resume=True)['summary']
        self.assertEqual(summary['deferred_reasons'], {'pdf_error': 1, 'access_blocked': 1, 'model_error': 1, 'model_service_error': 1})

    def test_audit_failure_not_reported_as_verified_new_record(self):
        crawler = self.crawler()
        crawler.queue(candidate())
        from finance_agent.core import dump
        def fail_audit(path,value):
            if Path(path).parent==self.root/'.runtime/simple/audit':raise OSError('offline audit write failure')
            return dump(path,value)
        with patch('record_files.dump',side_effect=fail_audit):
            summary = crawler.collect(resume=True)['summary']
        self.assertEqual(summary['new_records'], 0)
        self.assertEqual(summary['unverified_new_records'], 0)
        self.assertTrue(crawler.dataset.outbox.is_file())
        self.assertEqual(summary['deferred_reasons'], {'storage_error': 1})

    def test_focused_search_reports_candidate_deduplication(self):
        class Sources(FakeSources):
            def search(self, *args): return [candidate('new'), candidate('new'), candidate('existing')]
        class Model(FakeQwen):
            def plan(self, *args): return [{'market': 'CN', 'query': 'offline'}]
        crawler = self.crawler(Model(), Sources())
        crawler.queue(candidate('existing')); crawler.collect(resume=True)
        from pydantic import SecretStr
        crawler.cfg.search.api_key = SecretStr('offline-test')
        summary = crawler.collect()['summary']
        self.assertEqual(summary['search'], {'searched': 1, 'found': 3, 'candidates_new': 1, 'already_saved': 1, 'already_pending': 1})
        self.assertEqual(summary['new_records'], 1)


if __name__ == '__main__': unittest.main()
