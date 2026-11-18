import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from qwen_agent import Qwen, Decision, ModelServiceError, OutputError
from simple_crawler import Crawler
from test_simple_crawler import config, candidate, FakeSources, FakeQwen
from run_summary import error_reason


class ModelServiceRetryTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_model_service_retry'
        parent.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent)
        self.root=Path(self.tmp.name)
        self.cfg=config()
        self.cfg.ollama.service_retry_base_seconds=0
    def tearDown(self):
        self.tmp.cleanup()

    def model(self, handler):
        client=httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler))
        model=Qwen(self.cfg.ollama,self.root,client)
        model.model_digest='offline-service-test'
        self.addCleanup(model.close)
        return model

    def test_500_then_success_retries_identical_payload(self):
        requests=[]
        def handle(request):
            requests.append(json.loads(request.content))
            if len(requests)<3:return httpx.Response(500,json={'error':'test'})
            return httpx.Response(200,json={'message':{'content':json.dumps({'action':'fetch','reason':'valid'})}})
        model=self.model(handle)
        with patch('qwen_agent.time.sleep') as sleep:
            model.ask('test','offline',{},Decision)
        self.assertEqual(model.calls,3)
        self.assertEqual(requests[0],requests[1]);self.assertEqual(requests[1],requests[2])
        self.assertEqual(sleep.call_count,2)
        self.assertFalse(list((self.root/'.runtime/simple/model_errors').glob('*')))

    def test_transport_exhaustion_stays_service_error(self):
        def handle(request):raise httpx.ReadTimeout('private request body',request=request)
        model=self.model(handle)
        with self.assertRaises(ModelServiceError) as error:model.ask('test','offline',{},Decision)
        self.assertEqual(model.calls,3)
        self.assertEqual(error_reason(error.exception,'summarize'),'model_service_error')
        self.assertNotIn('private request body',str(error.exception))

    def test_nonretryable_status_stops_immediately(self):
        model=self.model(lambda request:httpx.Response(400,json={'error':'test'}))
        with self.assertRaises(ModelServiceError) as error:model.ask('test','offline',{},Decision)
        self.assertFalse(error.exception.retryable);self.assertEqual(model.calls,1)

    def test_retry_after_and_inference_budget_are_bounded(self):
        model=self.model(lambda request:httpx.Response(429,headers={'Retry-After':'999'}))
        model.cfg.max_calls_per_cycle=2
        with patch('qwen_agent.time.sleep') as sleep, self.assertRaises(ModelServiceError):
            model.ask('test','offline',{},Decision)
        self.assertEqual(model.calls,2);sleep.assert_called_once_with(30)

    def test_output_repair_keeps_separate_two_attempt_limit(self):
        model=self.model(lambda request:httpx.Response(200,json={'message':{'content':'invalid'}}))
        with self.assertRaises(OutputError):model.ask('test','offline',{},Decision)
        self.assertEqual(model.calls,2)

    def test_error_envelope_is_not_an_article_validation_failure(self):
        model=self.model(lambda request:httpx.Response(200,json={'error':'private diagnostic'}))
        with self.assertRaises(ModelServiceError) as error:model.ask('test','offline',{},Decision)
        self.assertEqual(model.calls,3);self.assertNotIn('private diagnostic',str(error.exception))

    def test_model_check_retries_without_using_inference_budget(self):
        calls=[]
        def handle(request):
            calls.append(request)
            if len(calls)==1:raise httpx.ConnectError('offline',request=request)
            return httpx.Response(200,json={'models':[{'name':'qwen3:8b','digest':'offline'}]})
        model=self.model(handle)
        model.model_digest=None
        self.assertTrue(model.check()['ready']);self.assertEqual(model.calls,0)
        self.assertEqual(len(calls),2)

    def test_missing_model_is_a_configuration_service_failure(self):
        model=self.model(lambda request:httpx.Response(200,json={'models':[]}))
        model.model_digest=None
        with self.assertRaises(ModelServiceError) as error:model.ask('test','offline',{},Decision)
        self.assertFalse(error.exception.retryable);self.assertEqual(model.calls,0)

    def test_service_pause_keeps_candidates_and_failure_count(self):
        class Unavailable(FakeQwen):
            def screen(self, row, monitoring):raise ModelServiceError('offline unavailable')
        model=Unavailable();crawler=Crawler(self.cfg,self.root,FakeSources(),model)
        self.addCleanup(crawler.close)
        crawler.queue(candidate('first'));crawler.queue(candidate('second'))
        first_id=next(iter(crawler.state['pending']))
        crawler.state['pending'][first_id]['failure_attempts']=2
        report=crawler.collect(resume=True)
        self.assertTrue(report['service_pause']);self.assertEqual(report['batches'],1)
        self.assertEqual(len(crawler.state['pending']),2);self.assertFalse(crawler.state['deferred'])
        self.assertEqual(crawler.state['pending'][first_id]['failure_attempts'],2)
        self.assertEqual(report['errors'][0]['reason_code'],'model_service_error')
        crawler.model=FakeQwen()
        self.assertEqual(crawler.collect(resume=True)['saved'],2)

    def test_planning_outage_preserves_history_and_does_not_consume_search(self):
        from pydantic import SecretStr
        from test_broad_discovery import WideModel,WideSources
        class Unavailable(WideModel):
            def coverage_plan(self,*args):raise ModelServiceError('offline planning unavailable')
            def plan(self,*args):raise ModelServiceError('offline planning unavailable')
        for mode in ['historical_broad','realtime_broad','focused']:
            with self.subTest(mode=mode):
                cfg=self.cfg.model_copy(deep=True)
                cfg.monitoring.directions=['Banking']
                cfg.collection.max_queries_per_cycle=1
                cfg.search.strategy='focused' if mode=='focused' else 'broad'
                cfg.search.api_key=SecretStr('offline-fixture')
                cfg.search.undated_fallback=False
                if mode=='realtime_broad':
                    cfg.collection.mode='realtime';cfg.collection.start_date=None;cfg.collection.end_date=None
                root=self.root/mode;root.mkdir()
                sources=WideSources(cfg,empty=True)
                crawler=Crawler(cfg,root,sources,Unavailable())
                try:
                    crawler.queue(candidate('existing'))
                    report=crawler.collect()
                    self.assertTrue(report['service_pause'])
                    self.assertEqual(report['considered'],0);self.assertEqual(report['searched'],0)
                    self.assertEqual(len(sources.requests),0);self.assertEqual(len(crawler.state['pending']),1)
                    if mode!='focused':
                        progress=next(iter(crawler.state['discovery'].values()))
                        self.assertEqual(progress['cursor'],0);self.assertFalse(progress.get('failed'))
                    if mode=='historical_broad':
                        self.assertEqual(progress['active']['index'],0)
                        crawler.model=WideModel()
                        recovered=crawler.collect()
                        self.assertEqual(recovered['searched'],1);self.assertEqual(recovered['saved'],1)
                        self.assertFalse(recovered['service_pause'])
                finally:crawler.close()


if __name__=='__main__':
    unittest.main()
