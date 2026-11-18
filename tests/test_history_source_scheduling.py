import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import httpx

from finance_agent.core import dump
from finance_agent.safety import PublicHTTP, FetchError
from source_scheduling import SourceAccess
from simple_crawler import Crawler
from qwen_agent import Qwen
from test_simple_crawler import config, candidate, FakeSources, FakeQwen
from test_preflight_pipeline import OfflineQwen, PageSources


class HistorySourceSchedulingTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_history_source';parent.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.tmp.name);self.cfg=config()
        self.clock=1000
    def tearDown(self):self.tmp.cleanup()
    def access(self):return SourceAccess(self.root,self.cfg.fetch,clock=lambda:self.clock)
    def http(self,robots=None,hosts=None):return SimpleNamespace(restrictions=hosts or {},robots_evidence=robots or {})

    def test_host_pause_is_exact_survives_restart_and_expires(self):
        a=self.access();a.capture(self.http(hosts={'blocked.example.com':{'reason':'http_403','observed_at':1000}}))
        self.assertTrue(self.access().paused('https://blocked.example.com/a'))
        self.assertIsNone(a.paused('https://other.example.com/a'))
        self.clock+=self.cfg.fetch.access_cooldown_seconds
        self.assertIsNone(self.access().paused('https://blocked.example.com/a'))

    def test_robots_pauses_only_disallowed_paths(self):
        a=self.access();a.capture(self.http(robots={'https://example.com':{'text':'User-agent: *\nDisallow: /articles/\nAllow: /articles/public/',
            'agent':'FinanceResearchAgent','observed_at':1000}}))
        # RobotFileParser uses its own rule ordering; assert its actual policy,
        # never invent a broader / host restriction from a single rejection.
        self.assertTrue(self.access().paused('https://example.com/articles/private'))
        self.assertIsNone(a.paused('https://example.com/news/a'))
        self.assertIsNone(a.paused('https://sub.example.com/articles/private'))
        self.clock+=self.cfg.fetch.robots_cache_seconds
        self.assertIsNone(a.paused('https://example.com/articles/private'))

    def test_policy_is_invalidated_when_proxy_changes(self):
        a=self.access();a.capture(self.http(hosts={'example.com':{'reason':'http_403','observed_at':1000}}))
        self.cfg.fetch.proxy_url='http://127.0.0.1:7897'
        self.assertIsNone(self.access().paused('https://example.com/a'))

    def test_missing_robots_does_not_pause(self):
        a=self.access();a.capture(self.http(robots={'https://example.com':{'text':None,'agent':'test','observed_at':1000}}))
        self.assertIsNone(a.paused('https://example.com/a'))

    def test_expired_observations_do_not_cause_repeated_sidecar_writes(self):
        a=self.access();http=self.http(hosts={'example.com':{'reason':'http_403','observed_at':1000}})
        a.capture(http);self.clock+=self.cfg.fetch.access_cooldown_seconds
        with patch('source_scheduling.dump') as writer:
            a.capture(http);a.capture(http)
        writer.assert_not_called()
        self.assertIsNone(a.paused('https://example.com/a'))

    def test_http_records_real_robots_rules_and_host_denial(self):
        h=PublicHTTP(self.cfg.fetch);h.client.close()
        def handler(request):
            if request.url.path=='/robots.txt':return httpx.Response(200,text='User-agent: *\nDisallow: /private/')
            return httpx.Response(403)
        h.client=httpx.Client(transport=httpx.MockTransport(handler))
        with patch('finance_agent.safety.public_url',side_effect=lambda url,*a,**k:url),patch('finance_agent.safety.time.sleep'):
            with self.assertRaises(FetchError):h.get('https://example.com/private/a')
            self.assertFalse(h.restrictions)
            with self.assertRaises(FetchError):h.get('https://example.com/public/a')
        a=SourceAccess(self.root,self.cfg.fetch);a.capture(h)
        self.assertTrue(a.paused('https://example.com/private/a'))
        self.assertEqual(a.paused('https://example.com/public/a')['reason'],'http_403')
        self.assertEqual(h.robots_evidence['https://example.com']['text'],'User-agent: *\nDisallow: /private/')
        h.close()

    def test_cooldown_skips_many_candidates_without_spending_budget(self):
        class Sources(FakeSources):
            def __init__(self):
                super().__init__();self.http=SimpleNamespace(restrictions={},robots_evidence={});self.attempts=[]
            def fetch(self,item):
                self.attempts.append(item['url'])
                if 'blocked.example.com' in item['url']:
                    self.http.restrictions['blocked.example.com']={'reason':'http_403','observed_at':time.time()}
                    raise FetchError('blocked','HTTP 403')
                return super().fetch(item)
        sources=Sources();c=Crawler(self.cfg,self.root,sources,FakeQwen())
        for n in range(4):c.queue(dict(candidate(str(n)),url='https://blocked.example.com/'+str(n)))
        c.queue(candidate('good'))
        r=c.collect(resume=True)
        self.assertEqual(len(sources.attempts),2);self.assertEqual(r['saved'],1)
        self.assertEqual(r['considered'],2);self.assertEqual(len(r['source_paused_ids']),4)
        self.assertEqual(r['summary']['source_paused_records'],4)
        self.assertEqual(r['summary']['unattempted_records'],0)
        for identity in r['source_paused_ids']:
            self.assertNotIn('failure_attempts',c.state['pending'][identity])
        c.close()
        # A restart reads the small policy sidecar and leaves the main queue intact.
        sources=Sources();c=Crawler(self.cfg,self.root,sources,FakeQwen());r=c.collect(resume=True)
        self.assertEqual(r['considered'],0);self.assertEqual(r['model_calls'],0)
        self.assertEqual(r['summary']['source_paused_records'],4);self.assertFalse(sources.attempts)
        self.assertEqual(len(c.state['pending']),4);c.close()

    def test_valid_cached_body_can_be_read_during_host_pause(self):
        c=Crawler(self.cfg,self.root,FakeSources(),FakeQwen());item=candidate()
        c.queue(item);identity=next(iter(c.state['pending']))
        dump(self.root/'.runtime/simple/pages'/(identity+'.json'),c.sources.fetch(item))
        c.source_access.capture(self.http(hosts={'example.com':{'reason':'http_403','observed_at':time.time()}}))
        c.sources.fetched=0;r=c.collect(resume=True)
        self.assertEqual(r['saved'],1);self.assertEqual(c.sources.fetched,0)
        self.assertFalse(r['source_paused_ids']);c.close()

    def test_historical_context_reaches_initial_screen_without_api_change(self):
        class Model(FakeQwen):
            def screen(self,payload,monitoring):
                self.payload=payload;return super().screen(payload,monitoring)
        model=Model(useful=False);c=Crawler(self.cfg,self.root,FakeSources(),model)
        c.queue(candidate());c.collect(resume=True)
        ctx=model.payload['collection_context']
        self.assertEqual(ctx['mode'],'historical');self.assertEqual(ctx['date_range'],['2026-11-01','2026-11-05'])
        self.assertIsNone(ctx['published_at']);c.close()

    def test_verified_source_date_and_excerpt_reach_delayed_screen(self):
        class Model(OfflineQwen):
            def screen(self,payload,monitoring):self.payload=payload;return {'useful':False,'reason':'offline test'}
        model=Model(self.cfg.ollama,self.root)
        c=Crawler(self.cfg,self.root,PageSources(body='Example Bank reported operating changes. '*200),model)
        c.queue(candidate());r=c.collect(resume=True)
        self.assertEqual(r['saved'],0)
        self.assertEqual(model.payload['collection_context']['published_at'],'2026-11-02')
        self.assertTrue(model.payload['body_excerpt']);self.assertLessEqual(len(model.payload['body_excerpt']),900)
        c.close()

    def test_model_screen_request_includes_collection_context(self):
        model=Qwen(self.cfg.ollama,self.root);payload=dict(candidate(),collection_context={'mode':'historical'})
        with patch.object(model,'ask',return_value={'action':'fetch','reason':'valid'}) as ask:
            model.screen(payload,self.cfg.monitoring)
        self.assertEqual(ask.call_args.args[2]['collection_context']['mode'],'historical');model.close()


if __name__=='__main__':unittest.main()
