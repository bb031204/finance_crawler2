"""Cooperative pause/continue; no process suspension, signals or forced termination."""
import json
import os
from pathlib import Path
import time
from uuid import uuid4
from finance_agent.core import Lock, dump, now


class PauseRequested(BaseException):
    """Do not classify a user pause as a model, network or article failure."""


class AlreadyPaused(Exception):
    pass


def read(path):
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def busy(root):
    try:
        with Lock(root):
            return False
    except RuntimeError:
        return True


class RunControl:
    def __init__(self, root, config_path, config_hash, options, continuing=False):
        self.root=Path(root).resolve();self.directory=self.root/'.runtime/control'
        self.path=self.directory/'run.json';self.request_path=self.directory/'pause.json'
        self.config_path=Path(config_path).resolve();self.config_hash=config_hash
        self.options=dict(options);self.continuing=continuing
        self.progress={'seen':[],'continuations':{},'channels':{},'query_done':[]}
        self.status='running';self.run_id=uuid4().hex
        self._seen=set()

    def __enter__(self):
        self.owner=Lock(self.directory/'active');self.owner.__enter__()
        try:
            with Lock(self.directory/'commands'):
                previous=read(self.path)
                if previous.get('status')=='paused' and not self.continuing:
                    raise AlreadyPaused('采集已暂停，请运行 resume_crawler.py 恢复；不会自动重放任务')
                if self.continuing and previous.get('status') in {'paused','running','pause_requested','failed'}:
                    if previous.get('config_hash')!=self.config_hash:
                        raise ValueError('config.yaml与暂停时不同，未恢复旧任务；请还原原配置后再次恢复')
                    self.options=previous['options']
                    self.progress=previous.get('progress',self.progress)
                if set(self.options)!={'resume','reprocess_saved','force','once'} or any(type(v) is not bool for v in self.options.values()):
                    raise ValueError('暂停描述中的运行选项无效，未启动采集')
                self._seen=set(self.progress.get('seen',[]))
                self.descriptor={'run_id':self.run_id,'pid':os.getpid(),'started_at':now(),
                    'config_path':str(self.config_path),'config_hash':self.config_hash,
                    'options':self.options,'status':'running','progress':self.progress}
                dump(self.path,self.descriptor,compact=True)
                self.request_path.unlink(missing_ok=True)
            return self
        except BaseException:
            self.owner.__exit__(None,None,None)
            raise

    def check(self):
        request=read(self.request_path)
        if request.get('run_id')==self.run_id:
            raise PauseRequested()

    def completed(self, identity):
        if identity not in self._seen:
            self._seen.add(identity)
            self.progress['seen'].append(identity)

    def reset_round(self):
        self.progress={'seen':[],'continuations':{},'channels':{},'query_done':[]};self._seen=set()

    def wait(self, seconds):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            self.check();time.sleep(min(0.5,max(0,end-time.monotonic())))
        self.check()

    def __exit__(self, kind, value, traceback):
        try:
            if self.status=='running':
                if kind in {PauseRequested,KeyboardInterrupt}:
                    self.status='paused'
                elif kind is not None:
                    self.status='failed'
                else:
                    self.status='paused' if read(self.request_path).get('run_id')==self.run_id else 'finished'
            with Lock(self.directory/'commands'):
                dump(self.path,dict(self.descriptor,status=self.status,finished_at=now(),progress=self.progress),compact=True)
        finally:
            self.owner.__exit__(kind,value,traceback)


def request_pause(root):
    root=Path(root).resolve();directory=root/'.runtime/control'
    with Lock(directory/'commands'):
        record=read(directory/'run.json')
        if not busy(directory/'active'):
            if busy(root):
                return {'status':'unsupported','message':'当前写入进程未载入暂停控制；旧版采集请先在原终端Ctrl+C正常停止一次，再用新版main.py启动'}
            return {'status':'paused' if record.get('status')=='paused' else 'idle',
                    'message':'已经暂停' if record.get('status')=='paused' else '当前没有运行中的采集，无需暂停'}
        if not record.get('run_id'):
            return {'status':'unsupported','message':'采集正在初始化，请稍后重新请求暂停'}
        dump(directory/'pause.json',{'run_id':record['run_id'],'requested_at':now()})
        dump(directory/'run.json',dict(record,status='pause_requested'),compact=True)
        return {'status':'pause_requested','run_id':record['run_id'],
                'message':'已请求暂停；等待当前请求完成并提交断点，不强杀进程'}


def wait_paused(root, run_id, timeout=300):
    root=Path(root);directory=root/'.runtime/control';end=time.monotonic()+timeout
    while time.monotonic()<end:
        record=read(directory/'run.json')
        if record.get('run_id')!=run_id:
            return {'status':'changed','message':'运行实例已变化，请核对新终端状态'}
        if not busy(directory/'active') and not busy(root):
            return {'status':record.get('status','unknown'),
                    'message':'已暂停，断点已保存，可以运行 resume_crawler.py' if record.get('status')=='paused' else '采集已退出，状态：'+record.get('status','unknown')}
        time.sleep(0.5)
    return {'status':'waiting','message':'暂停请求仍有效，当前请求/提交尚未结束；未强制终止，可稍后查看原终端'}
