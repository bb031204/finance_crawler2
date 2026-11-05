"""Synthetic review backlog and cached-search fixtures; no real provider calls."""
import io
import time
from contextlib import redirect_stdout
from unittest.mock import patch
from finance_agent.cli import main
from finance_agent.core import digest
from finance_agent.pipeline import Engine
from test_agent import Base, FakeLLM, FakeProvider, candidate


class IdleStatusTests(Base):
    def queue_manual(self, repo, status='needs_review', count=5):
        for index in range(count):
            record = candidate()
            record['candidate_id'] = 'C_synthetic_' + status + '_' + str(index)
            repo.upsert_candidate(record, 'fixture')
            repo.set_status(record['candidate_id'], status)

    def test_only_review_backlog_completes_without_inference_and_preserves_review(self):
        repo = self.repo()
        self.queue_manual(repo)
        llm = FakeLLM()
        engine = Engine(self.cfg, self.root, repo=repo, llm=llm, provider=FakeProvider())
        try:
            output = io.StringIO()
            with redirect_stdout(output):
                report = engine.cycle(discover=False)
            self.assertEqual(report['status'], 'completed')
            self.assertEqual(report['needs_review_count'], 5)
            self.assertEqual(report['automatic_pending'], 0)
            self.assertEqual(report['error_count'], 0)
            self.assertEqual(repo.queue_counts(), {'needs_review': 5})
            self.assertEqual(llm.calls, 0)
            self.assertIn('本轮正常完成', output.getvalue())
            self.assertIn('待人工复核5篇', output.getvalue())
        finally:
            engine.close()

    def test_only_ocr_backlog_is_reported_separately(self):
        repo = self.repo()
        self.queue_manual(repo, 'needs_ocr', 1)
        engine = Engine(self.cfg, self.root, repo=repo, llm=FakeLLM(), provider=FakeProvider())
        try:
            report = engine.cycle(discover=False)
            self.assertEqual(report['status'], 'completed')
            self.assertEqual(report['needs_ocr_count'], 1)
            self.assertEqual(report['automatic_pending'], 0)
            self.assertEqual(repo.queue_counts(), {'needs_ocr': 1})
        finally:
            engine.close()

    def test_future_retry_is_still_automatic_pending_and_partial(self):
        repo = self.repo()
        record = candidate()
        repo.upsert_candidate(record, 'fixture')
        repo.set_status(record['candidate_id'], 'fetched', retry_delay=3600)
        engine = Engine(self.cfg, self.root, repo=repo, llm=FakeLLM(), provider=FakeProvider())
        try:
            report = engine.cycle(discover=False)
            self.assertEqual(report['status'], 'partial')
            self.assertEqual(report['automatic_pending'], 1)
        finally:
            engine.close()

    def test_budget_pause_stays_partial_without_automatic_rows(self):
        engine = Engine(self.cfg, self.root, repo=self.repo(), llm=FakeLLM(), provider=FakeProvider())
        def process(report):
            report['budget_pause'] = True
        try:
            with patch.object(engine, 'process', side_effect=process):
                report = engine.cycle(discover=False)
            self.assertEqual(report['status'], 'partial')
        finally:
            engine.close()

    def test_normal_cli_skipped_searches_and_old_reviews_return_zero_without_network(self):
        repo = self.repo()
        self.queue_manual(repo)
        start, end = self.cfg.collection.start_date, self.cfg.collection.end_date
        plans = [
            {'query': 'CN 银行业 公告', 'market': 'CN', 'direction': '银行业', 'reason': 'fixture'},
            {'query': 'US 半导体 财报', 'market': 'US', 'direction': '半导体', 'reason': 'fixture'},
        ]
        key = 'plan:' + digest([self.cfg.monitoring.model_dump(), start, end, 0, [], 2])
        repo.state(key, {'created': time.time(), 'queries': plans})
        for plan in plans:
            key = 'search:' + digest([self.cfg.search.model_dump(), self.cfg.monitoring.markets,
                                     plan['market'], plan['query'], start, end])
            repo.state(key, {'searched_epoch': time.time() - 60, 'returned': 0})
        llm = FakeLLM()
        class Provider(FakeProvider):
            def search(self, *args, **kwargs):
                raise AssertionError('Recent searches must not call the provider')
        engine = Engine(self.cfg, self.root, repo=repo, llm=llm, provider=Provider())
        output = io.StringIO()
        with patch('finance_agent.cli.ROOT', self.root), patch('finance_agent.cli.load', return_value=self.cfg), \
             patch('finance_agent.cli.Engine', return_value=engine), redirect_stdout(output):
            self.assertEqual(main([]), 0)
        self.assertEqual(llm.calls, 0)
        self.assertEqual(output.getvalue().count('跳过搜索：'), 2)
        self.assertIn('300秒内已执行', output.getvalue())
        self.assertIn('待人工复核5篇', output.getvalue())
        from finance_agent.database import Repository
        check = Repository(self.root / self.cfg.output.database, self.root)
        try:
            self.assertEqual(check.queue_counts(), {'needs_review': 5})
        finally:
            check.close()
