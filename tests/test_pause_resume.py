import json
from pathlib import Path
import tempfile
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import httpx

from crawler_control import RunControl,PauseRequested,AlreadyPaused,request_pause,wait_paused,busy,read
from finance_agent.core import Lock,dump
from qwen_agent import Qwen,Decision
from simple_crawler import Crawler
from test_simple_crawler import config,candidate,FakeSources,FakeQwen

OPTIONS={'resume':False,'reprocess_saved':False,'force':False,'once':False}

class PauseResumeTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_pause_resume';parent.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def control(self,continuing=False,hash='config-one',options=None):
        return RunControl(self.root,self.root/'config.yaml',hash,options or OPTIONS,continuing)

    def test_pause_ack_waits_for_locks_and_resume_restores_options(self):
        with Lock(self.root),self.control(options=dict(OPTIONS,once=True)) as control:
            control.completed('already-failed');control.progress['query_done']=['committed-query']
            result=request_pause(self.root)
            self.assertEqual(result['status'],'pause_requested')
            with self.assertRaises(PauseRequested):control.check()
            self.assertEqual(wait_paused(self.root,control.run_id,timeout=.01)['status'],'waiting')
            control.status='paused'
        self.assertEqual(wait_paused(self.root,result['run_id'],timeout=.1)['status'],'paused')
        with self.control(continuing=True) as restored:
            self.assertTrue(restored.options['once']);self.assertFalse(restored.options['resume'])
            self.assertEqual(restored.progress['seen'],['already-failed'])
            self.assertEqual(restored.progress['query_done'],['committed-query']);restored.check()
        self.assertEqual(read(self.root/'.runtime/control/run.json')['status'],'finished')

    def test_ordinary_start_cannot_override_pause(self):
        with self.control() as control:control.status='paused'
        with self.assertRaises(AlreadyPaused):
            with self.control():pass
        self.assertFalse(busy(self.root/'.runtime/control/active'))

    def test_config_change_keeps_old_pause_descriptor(self):
        with self.control() as control:control.status='paused'
        before=(self.root/'.runtime/control/run.json').read_bytes()
        with self.assertRaises(ValueError):
            with self.control(continuing=True,hash='changed'):pass
        self.assertEqual(before,(self.root/'.runtime/control/run.json').read_bytes())

    def test_idle_pause_is_noop(self):
        self.assertEqual(request_pause(self.root)['status'],'idle')
        self.assertFalse((self.root/'.runtime/control/pause.json').exists())

    def test_legacy_writer_is_not_signaled(self):
        with Lock(self.root):self.assertEqual(request_pause(self.root)['status'],'unsupported')
        self.assertFalse((self.root/'.runtime/control/pause.json').exists())

    def test_old_request_and_duplicate_owner(self):
        with self.control() as control:
            dump(control.request_path,{'run_id':'stale-run'});control.check()
            with self.assertRaises(RuntimeError):
                with self.control():pass
            self.assertEqual(read(control.path)['run_id'],control.run_id)

    def test_real_child_process_acknowledges_only_after_checkpoint(self):
        script="""
import sys,time
from pathlib import Path
from crawler_control import RunControl,PauseRequested
from finance_agent.core import Lock,dump
root=Path(sys.argv[1])
with Lock(root),RunControl(root,root/'config.yaml','fixture',dict(resume=False,reprocess_saved=False,force=False,once=False)) as control:
    dump(root/'ready.json',{'ready':True})
    try:
        while True:control.check();time.sleep(.02)
    except PauseRequested:
        dump(root/'checkpoint.json',{'committed':True});control.status='paused'
"""
        process=subprocess.Popen([sys.executable,'-B','-X','utf8','-c',script,str(self.root)],
            cwd=Path(__file__).resolve().parents[1],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            deadline=time.monotonic()+10
            while not (self.root/'ready.json').exists() and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue((self.root/'ready.json').exists())
            request=request_pause(self.root)
            self.assertEqual(request['status'],'pause_requested')
            self.assertEqual(wait_paused(self.root,request['run_id'],5)['status'],'paused')
            self.assertEqual(read(self.root/'checkpoint.json'),{'committed':True})
            self.assertEqual(process.wait(timeout=5),0)
        finally:
            if process.poll() is None:process.terminate();process.wait(timeout=5)

    def test_current_model_call_is_cached_before_pause_then_reused(self):
        cfg=config();requests=[]
        def handler(request):
            requests.append(request);request_pause(self.root)
            return httpx.Response(200,json={'message':{'content':json.dumps({'action':'fetch','reason':'fixture'})}})
        model=Qwen(cfg.ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler)))
        model.model_digest='offline-pause-test'
        with self.control() as control:
            model.pause_check=control.check
            model.ask('fixture','fixture',{'source_id':'one'},Decision)
            with self.assertRaises(PauseRequested):model.ask('fixture','fixture',{'source_id':'two'},Decision)
            control.status='paused'
        self.assertEqual(len(requests),1)
        with self.control(continuing=True) as control:
            model.pause_check=control.check
            model.ask('fixture','fixture',{'source_id':'one'},Decision)
            self.assertEqual(len(requests),1);self.assertEqual(model.cache_hits,1)
        model.close()

    def test_main_queue_pause_and_resume_without_duplicate_json(self):
        import simple_crawler
        cfg=config()
        class Model(FakeQwen):
            def summarize(inner,page,monitoring):
                result=super().summarize(page,monitoring);request_pause(self.root);return result
        c=Crawler(cfg,self.root,FakeSources(),Model())
        for n in range(3):c.queue(candidate(str(n)))
        c.persist()
        with patch.object(simple_crawler,'ROOT',self.root),patch.object(simple_crawler,'load_config',return_value=cfg),patch.object(simple_crawler,'Crawler',return_value=c):
            self.assertEqual(simple_crawler.main(['--resume']),0)
        self.assertEqual(read(self.root/'.runtime/control/run.json')['status'],'paused')
        self.assertEqual(len(c.dataset.rows),1);self.assertEqual(len(c.state['pending']),2)
        self.assertTrue(all('failure_attempts' not in x for x in c.state['pending'].values()))
        c2=Crawler(cfg,self.root,FakeSources(),FakeQwen())
        with patch.object(simple_crawler,'ROOT',self.root),patch.object(simple_crawler,'load_config',return_value=cfg),patch.object(simple_crawler,'Crawler',return_value=c2):
            self.assertEqual(simple_crawler.main(['--continue-paused']),0)
        self.assertEqual(len(c2.dataset.rows),3);self.assertEqual(c2.sources.fetched,2)
        self.assertFalse(c2.state['pending'])

    def test_mid_discovery_resume_does_not_repeat_committed_search(self):
        import simple_crawler
        from pydantic import SecretStr
        cfg=config();cfg.search.strategy='targeted';cfg.search.api_key=SecretStr('offline-test')
        class Model(FakeQwen):
            def plan(self,*args):return [{'market':'CN','query':'query-one'},{'market':'CN','query':'query-two'}]
        class Sources(FakeSources):
            def __init__(inner):super().__init__();inner.queries=[]
            def search(inner,query,*args):
                if callable(getattr(inner,'pause_check',None)):inner.pause_check()
                inner.queries.append(query)
                if query=='query-one':request_pause(self.root)
                return [candidate(query)]
        s=Sources();c=Crawler(cfg,self.root,s,Model())
        with patch.object(simple_crawler,'ROOT',self.root),patch.object(simple_crawler,'load_config',return_value=cfg),patch.object(simple_crawler,'Crawler',return_value=c):
            self.assertEqual(simple_crawler.main([]),0)
        self.assertEqual(s.queries,['query-one']);self.assertEqual(len(c.state['pending']),1)
        c2=Crawler(cfg,self.root,s,Model())
        with patch.object(simple_crawler,'ROOT',self.root),patch.object(simple_crawler,'load_config',return_value=cfg),patch.object(simple_crawler,'Crawler',return_value=c2):
            self.assertEqual(simple_crawler.main(['--continue-paused']),0)
        self.assertEqual(s.queries,['query-one','query-two']);self.assertEqual(len(c2.dataset.rows),2)

if __name__=='__main__':unittest.main()
