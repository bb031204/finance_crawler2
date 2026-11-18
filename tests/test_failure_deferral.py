"""Failure deferral: classified retry limits stop deterministic failures from looping."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from finance_agent.runtime import setup
setup(Path(__file__).resolve().parents[1])
from simple_crawler import Crawler,FAILURE_DEFER_LIMITS,failure_class
from record_files import source_id
from finance_agent.core import dump
from test_simple_crawler import config,FakeQwen,FakeSources,candidate
from finance_agent.safety import FetchError


class FailureDeferralTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_deferral';parent.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.temp.name);self.cfg=config()
    def tearDown(self):self.temp.cleanup()
    def crawler(self,model=None,sources=None):return Crawler(self.cfg,self.root,sources or FakeSources(),model or FakeQwen())

    def test_failure_classes_match_reason_codes(self):
        self.assertEqual(failure_class('body_error','正文缓存超过当前max_text_chars上限；不进行日期判断'),'permanent')
        self.assertEqual(failure_class('pdf_error','PDF正文不可读取'),'permanent')
        self.assertEqual(failure_class('access_blocked','robots拒绝'),'permanent')
        self.assertEqual(failure_class('validation_error','范围外'),'permanent')
        self.assertEqual(failure_class('model_error','Qwen输出校验失败'),'model')
        self.assertEqual(failure_class('network_error','连接失败'),'transient')
        self.assertEqual(failure_class('storage_error','写入失败'),'transient')

    def test_permanent_failure_stops_after_limit(self):
        class Blocked(FakeSources):
            def fetch(self,c):raise FetchError('blocked','离线拦截夹具')
        c=self.crawler(sources=Blocked());identity=source_id(candidate('big')['url'])
        c.queue(candidate('big'))
        c.run(resume=True);c.run(resume=True)
        self.assertIn(identity,c.state['deferred'])
        self.assertNotIn(identity,c.state['pending'])
        self.assertNotIn(identity,c.active_pending())
        self.assertEqual(c.state['records'][identity]['status'],'deferred')
        self.assertEqual(c.state['deferred'][identity]['failure_attempts'],FAILURE_DEFER_LIMITS['permanent'])
        self.assertEqual(c.state['deferred'][identity]['deferred_kind'],'permanent')
        # 停试后再次运行不再消耗文档额度
        report=c.run(resume=True)
        self.assertEqual(report['considered'],0)
        c.close()

    def test_transient_failure_keeps_retrying_below_limit(self):
        c=self.crawler(sources=FakeSources(fail=True));identity=source_id(candidate('flaky')['url'])
        c.queue(candidate('flaky'))
        for _ in range(FAILURE_DEFER_LIMITS['transient']-1):
            c.run(resume=True)
        self.assertNotIn(identity,c.state['deferred'])
        self.assertIn(identity,c.state['pending'])
        self.assertEqual(c.state['pending'][identity]['failure_attempts'],FAILURE_DEFER_LIMITS['transient']-1)
        c.run(resume=True)  # 达到上限
        self.assertIn(identity,c.state['deferred'])
        self.assertEqual(c.state['deferred'][identity]['deferred_kind'],'transient')
        c.close()

    def test_oversize_body_stops_after_limit_with_reason(self):
        class Huge(FakeSources):
            def fetch(self,c):
                return {'url':c['url'],'title':c['title'],'origin_market':'CN',
                    'date_context':'发布时间 2026-11-02','text':'离线超长正文夹具'*20000}
        c=self.crawler(sources=Huge());c.cfg.fetch.max_text_chars=1000
        identity=source_id(candidate('huge')['url'])
        c.queue(candidate('huge'))
        c.run(resume=True)
        waiting=c.run(resume=True)
        self.assertEqual(waiting['considered'],0)
        self.assertEqual(len(waiting['retry_paused_ids']),1)
        report=c.run(resume=True,force=True)  # An explicit retry still observes the failure limit.
        self.assertIn(identity,c.state['deferred'])
        self.assertEqual(c.state['deferred'][identity]['deferred_reason'],'body_error')
        self.assertEqual(c.state['deferred'][identity]['failure_attempts'],2)
        self.assertEqual(report['deferred_stopped'],1)
        c.close()

    def test_queue_does_not_reactivate_deferred_candidate(self):
        c=self.crawler();identity=source_id(candidate('x')['url'])
        c.state['deferred'][identity]=dict(candidate('x'),failure_attempts=2,deferred_kind='permanent')
        c.queue(candidate('x'))
        self.assertNotIn(identity,c.state['pending'])
        c.close()

    def test_state_without_deferred_key_is_compatible(self):
        dump(self.root/'.runtime/simple/state.json',{'records':{},'pending':{},'plans':{}})
        c=self.crawler()
        self.assertEqual(c.state['deferred'],{})
        c.close()

    def test_retry_deferred_requeues_and_collects(self):
        import simple_crawler
        class Blocked(FakeSources):
            def fetch(self,c):raise FetchError('blocked','离线拦截夹具')
        blocked=self.crawler(sources=Blocked());identity=source_id(candidate('gone')['url'])
        blocked.queue(candidate('gone'));blocked.run(resume=True);blocked.run(resume=True)
        self.assertIn(identity,blocked.state['deferred']);blocked.close()
        c=self.crawler()  # 同一root读回state；环境已修复（正常sources）
        self.assertIn(identity,c.state['deferred'])
        with patch.object(simple_crawler,'ROOT',self.root),patch.object(simple_crawler,'load_config',return_value=self.cfg),patch.object(simple_crawler,'Crawler',return_value=c):
            result=simple_crawler.main(['--retry-deferred','--resume'])
        self.assertEqual(result,0)
        self.assertNotIn(identity,c.state['deferred'])
        self.assertEqual(c.state['records'][identity]['status'],'saved')
        self.assertEqual(len(list((self.root/'data/CN').glob('*.json'))),1)
        c.close()

    def test_retry_deferred_kind_filters_entries(self):
        import simple_crawler
        class Blocked(FakeSources):
            def fetch(self,c):raise FetchError('blocked','离线拦截夹具')
        permanent=source_id(candidate('perm')['url']);model=source_id(candidate('modelout')['url'])
        c=self.crawler(sources=Blocked())
        c.queue(candidate('perm'));c.run(resume=True);c.run(resume=True)
        c.state['deferred'][model]=dict(candidate('modelout'),failure_attempts=3,
            failure_kind='model',deferred_reason='model_error',deferred_kind='model',deferred_attempts=3)
        c.close()
        c2=self.crawler()
        with patch.object(simple_crawler,'ROOT',self.root),patch.object(simple_crawler,'load_config',return_value=self.cfg),patch.object(simple_crawler,'Crawler',return_value=c2):
            simple_crawler.main(['--retry-deferred-kind','model','--resume'])
        self.assertNotIn(model,c2.state['deferred'])
        self.assertIn(permanent,c2.state['deferred'])  # 指定类别外的停试条目保留
        c2.close()


if __name__=='__main__':unittest.main()
