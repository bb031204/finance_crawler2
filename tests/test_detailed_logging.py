"""Synthetic component and logging fixtures; all model HTTP is mocked."""
import io
import json
import threading
from contextlib import redirect_stdout
from types import SimpleNamespace
import httpx
from finance_agent.config import Search
from finance_agent.llm import OllamaClient
from finance_agent.models import Plan
from finance_agent.pipeline import Engine
from finance_agent.progress import finish_model_stats
from test_agent import Base, FakeLLM, FakeProvider, candidate


class DetailedLoggingTests(Base):
    def test_scope_logs_ranges_and_idle_components_without_secret_or_inference(self):
        cfg = self.cfg.model_copy(deep=True)
        cfg.search = Search(website_mode='restricted', market_domains={'CN': ['cninfo.com.cn'], 'US': ['sec.gov']},
                            api_key='synthetic-private-logging-key')
        llm = FakeLLM()
        engine = Engine(cfg, self.root, repo=self.repo(), llm=llm, provider=FakeProvider())
        try:
            output = io.StringIO()
            with redirect_stdout(output):
                report = engine.cycle(discover=False)
            text = output.getvalue()
            for value in ['2026-11-01 至 2026-11-05', '市场=CN,US', 'cninfo.com.cn', 'sec.gov',
                          '未指定个股', 'Crawl4AI清理=', 'Qwen模型=qwen3:8b', '实际推理0次', '本轮未执行模型检查']:
                self.assertIn(value, text)
            self.assertNotIn('synthetic-private-logging-key', text)
            self.assertNotIn('synthetic-private-logging-key', json.dumps(report))
            self.assertEqual(llm.calls, 0)
        finally:
            engine.close()

    def test_inference_retry_and_cache_count_actual_requests(self):
        posted = []
        def handler(request):
            if request.url.path.endswith('/tags'):
                return httpx.Response(200, json={'models': [{'name': 'qwen3:8b', 'digest': 'fixture_logging_model'}]})
            posted.append(request)
            content = 'bad json' if len(posted) == 1 else json.dumps({'queries': []})
            return httpx.Response(200, json={'message': {'content': content}, 'done_reason': 'stop', 'eval_count': 4})
        llm = OllamaClient(self.cfg.ollama, self.root,
                           httpx.Client(base_url='http://fixture-model', transport=httpx.MockTransport(handler)))
        try:
            output = io.StringIO()
            with redirect_stdout(output):
                llm.complete('planner', 'fixture', {}, Plan)
                llm.complete('planner', 'fixture', {}, Plan)
            report = {'components': {}}
            finish_model_stats(report, llm, 0, 0, threading.get_ident(), 0)
            stats = report['components']['qwen']
            self.assertEqual(len(posted), 2)
            self.assertEqual(stats['inference_calls'], 2)
            self.assertEqual(stats['repair_calls'], 1)
            self.assertEqual(stats['model_cache_hits'], 1)
            self.assertEqual(stats['validation_failures'], 1)
            self.assertEqual(stats['request_failures'], 0)
            self.assertEqual(stats['by_purpose']['planner'], {'calls': 2, 'cache_hits': 1})
            self.assertEqual(report['components']['ollama'], {'checks': 1, 'checks_passed': 1})
            self.assertIn('修复重试', output.getvalue())
            self.assertIn('命中已验证模型响应缓存', output.getvalue())
        finally:
            llm.close()

    def test_connection_failures_are_not_reported_as_validated_inferences(self):
        def handler(request):
            if request.url.path.endswith('/tags'):
                return httpx.Response(200, json={'models': [{'name': 'qwen3:8b', 'digest': 'fixture_logging_model'}]})
            raise httpx.ConnectTimeout('synthetic connection timeout')
        llm = OllamaClient(self.cfg.ollama, self.root,
                           httpx.Client(base_url='http://fixture-model', transport=httpx.MockTransport(handler)))
        try:
            with redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                llm.complete('extractor', 'fixture', {}, Plan)
            report = {'components': {}}
            finish_model_stats(report, llm, 0, 0, threading.get_ident(), 0)
            self.assertEqual(report['components']['qwen']['inference_calls'], 2)
            self.assertEqual(report['components']['qwen']['request_failures'], 2)
            self.assertEqual(report['components']['qwen']['validation_failures'], 0)
        finally:
            llm.close()

    def test_shared_model_stats_are_attributed_to_current_worker(self):
        llm = SimpleNamespace(calls=99, usage=[
            {'purpose': 'planner', 'cached': False, 'thread_id': 10, 'repair': False},
            {'purpose': 'extractor', 'cached': False, 'thread_id': 20, 'repair': False},
            {'purpose': 'planner', 'cached': True, 'thread_id': 10},
        ], check_history=[{'thread_id': 10, 'passed': True}, {'thread_id': 20, 'passed': True}])
        report = {'components': {}}
        finish_model_stats(report, llm, 0, 0, 10, 0)
        self.assertEqual(report['model_calls_delta'], 1)
        self.assertEqual(report['components']['qwen']['model_cache_hits'], 1)
        self.assertEqual(report['components']['ollama']['checks'], 1)

    def test_provider_candidate_dedup_and_actual_database_totals(self):
        class Provider(FakeProvider):
            def search(self, *args, **kwargs):
                record = candidate()
                return [record.copy(), record.copy()], {'response': {'results': [{}, {}, {}]}}
        engine = Engine(self.cfg, self.root, repo=self.repo(), llm=FakeLLM(), provider=Provider())
        try:
            with redirect_stdout(io.StringIO()):
                report = engine.cycle(discover=True, process=False)
            self.assertEqual(report['queries_attempted'], 1)
            self.assertEqual(report['provider_results_returned'], 3)
            self.assertEqual(report['provider_results_filtered'], 1)
            self.assertEqual(report['candidates_seen'], 2)
            self.assertEqual(report['candidates_unique'], 1)
            self.assertEqual(report['candidates_new'], 1)
            self.assertEqual(report['candidates_existing'], 1)
            self.assertEqual(report['events_inserted'], 0)
        finally:
            engine.close()

    def test_processing_counts_match_one_saved_event(self):
        repo = self.repo()
        outer = self
        class Fetcher:
            def fetch(self, record):
                doc = outer.document(repo)
                doc['candidate_id'] = record['candidate_id']
                return doc
        engine = Engine(self.cfg, self.root, repo=repo, llm=FakeLLM(), provider=FakeProvider(), fetcher=Fetcher())
        try:
            with redirect_stdout(io.StringIO()):
                report = engine.cycle()
            self.assertFalse(report['errors'])
            self.assertEqual(report['screen_accepted'], 1)
            self.assertEqual(report['download_attempted'], 1)
            self.assertEqual(report['download_parsed'], 1)
            self.assertEqual(report['events_inserted'], 1)
            self.assertEqual(report['total_events'], 1)
            self.assertEqual(report['components']['crawl4ai']['cleaned'], 0)
        finally:
            engine.close()
