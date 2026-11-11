import json
import tempfile
import unittest
from finance_agent.core import dump
from pathlib import Path
from unittest.mock import patch
from run_after_current import supervise,same_process


class RestartSupervisorTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_restart_supervisor';parent.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.tmp.name)
        self.launched=[];self.sleeps=[]
    def tearDown(self):self.tmp.cleanup()
    def launch(self,command,**kwargs):
        self.launched.append((command,kwargs));test=self
        class Process:
            pid=12345;returncode=None;polls=0
            def poll(self):
                self.polls+=1
                if self.polls>1:self.returncode=0
                return self.returncode
        return Process()
    def read(self):return json.loads((self.root/'.runtime/restart_after_current.json').read_text(encoding='utf-8'))

    def test_waits_for_original_exit_and_starts_exactly_once(self):
        alive=iter([True,True,False])
        def sleep(value):
            self.sleeps.append(value)
            if len(self.sleeps)<=2:self.assertEqual(len(self.launched),0)
        result=supervise(self.root,222,100.0,alive=lambda *_:next(alive),find=lambda _:[],idle=lambda _:True,sleep=sleep,launch=self.launch)
        self.assertEqual(len(self.launched),1);self.assertEqual(result['status'],'finished')
        command,kwargs=self.launched[0];self.assertEqual(command[-2:],['--config',str(self.root/'config.yaml')])
        self.assertEqual(command[-3],str(self.root/'main.py'));self.assertEqual(kwargs['cwd'],str(self.root))
        self.assertEqual(self.read()['collector_pid'],12345)

    def test_existing_new_collector_prevents_duplicate_launch(self):
        result=supervise(self.root,222,100.0,alive=lambda *_:False,find=lambda _:[333],idle=lambda _:True,sleep=lambda _:None,launch=self.launch)
        self.assertEqual(result['status'],'superseded');self.assertEqual(self.launched,[])

    def test_writer_lock_must_be_free_before_start(self):
        ready=iter([False,False,True])
        def sleep(value):
            self.sleeps.append(value)
            if len(self.sleeps)<=2:self.assertFalse(self.launched)
        supervise(self.root,222,100.0,alive=lambda *_:False,find=lambda _:[],idle=lambda _:next(ready),sleep=sleep,launch=self.launch)
        self.assertEqual(len(self.launched),1)

    def test_nonzero_exit_is_reported_and_never_auto_restarted_again(self):
        def launch(*args,**kwargs):
            self.launched.append(args)
            class Process:
                pid=12345;returncode=1
                def poll(self):return 1
            return Process()
        result=supervise(self.root,222,100.0,alive=lambda *_:False,find=lambda _:[],idle=lambda _:True,sleep=lambda _:None,launch=launch)
        self.assertEqual(result['status'],'failed');self.assertEqual(result['exit_code'],1);self.assertEqual(len(self.launched),1)

    def test_pid_reuse_is_not_mistaken_for_original_process(self):
        class Process:
            def is_running(self):return True
            def create_time(self):return 200.0
        with patch('run_after_current.psutil.Process',return_value=Process()):self.assertFalse(same_process(222,100.0))

    def test_transient_status_permission_error_is_retried(self):
        writes=0
        def flaky(path,value):
            nonlocal writes
            writes+=1
            if writes<=2:raise PermissionError('Windows reader temporarily holds target')
            return dump(path,value)
        with patch('run_after_current.dump',side_effect=flaky),patch('run_after_current.time.sleep'):
            result=supervise(self.root,222,100.0,alive=lambda *_:False,find=lambda _:[],idle=lambda _:True,sleep=lambda _:None,launch=self.launch)
        self.assertEqual(result['status'],'finished');self.assertEqual(len(self.launched),1)
        self.assertEqual(self.read()['status'],'finished')

    def test_status_failure_does_not_stop_waiting_or_start_early(self):
        waiting=iter([True,False]);writes=0
        def flaky(path,value):
            nonlocal writes
            writes+=1
            if writes<=4:raise PermissionError('all first-heartbeat retries blocked')
            return dump(path,value)
        def sleep(_):
            if writes==4:self.assertFalse(self.launched)
        with patch('run_after_current.dump',side_effect=flaky),patch('run_after_current.time.sleep'):
            result=supervise(self.root,222,100.0,alive=lambda *_:next(waiting),find=lambda _:[],idle=lambda _:True,sleep=sleep,launch=self.launch)
        self.assertEqual(result['status'],'finished');self.assertEqual(len(self.launched),1)


if __name__=='__main__':unittest.main()
