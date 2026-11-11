"""Wait for one existing collector to finish, then launch main.py once with config.yaml."""
import argparse
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
import psutil
from finance_agent.core import Lock,dump,now

ROOT=Path(__file__).resolve().parent


def same_process(pid,created):
    try:
        process=psutil.Process(pid)
        return process.is_running() and abs(process.create_time()-created)<0.01
    except psutil.NoSuchProcess:return False
    except psutil.AccessDenied:return True


def matching_collectors(root):
    main=(root/'main.py').resolve();result=[]
    for process in psutil.process_iter(['pid','name']):
        if process.pid==os.getpid() or str(process.info.get('name')).lower() not in {'python.exe','pythonw.exe','python','python3'}:continue
        try:
            args=process.cmdline()
            for arg in args[1:]:
                if not str(arg).lower().endswith('main.py'):continue
                path=Path(arg);path=path if path.is_absolute() else Path(process.cwd())/path
                if path.resolve()==main:result.append(process.pid);break
        except (psutil.Error,OSError,ValueError):continue
    return result


def writer_idle(root):
    try:
        with Lock(root):return True
    except RuntimeError:return False


@contextmanager
def supervisor_lock(root):
    path=root/'.runtime/restart_supervisor.lock';path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as stream:
        if not stream.tell():stream.write(b'0');stream.flush()
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream,fcntl.LOCK_UN)


def supervise(root,target_pid,target_created,poll_seconds=10,*,alive=same_process,find=matching_collectors,
              idle=writer_idle,sleep=time.sleep,launch=subprocess.Popen):
    root=root.resolve();status_path=root/'.runtime/restart_after_current.json'
    command=[sys.executable,'-u','-B','-X','utf8',str(root/'main.py'),'--config',str(root/'config.yaml')]
    state={'requested_at':now(),'supervisor_pid':os.getpid(),'target_pid':target_pid,'target_created':target_created,
        'command':command,'config':str(root/'config.yaml'),'status':'waiting','reason':'等待原采集进程自然结束',
        'collector_pid':None,'terminal_log':None}
    def publish(**values):
        state.update(values,checked_at=now())
        for attempt in range(4):
            try:
                dump(status_path,state)
                return True
            except PermissionError:
                if attempt<3:time.sleep(0.05*(2**attempt))
        # A status reader/Windows scanner can temporarily prevent os.replace.
        # Monitoring must survive a status-only write failure; next heartbeat retries.
        print('等待任务状态文件暂时无法替换，继续监控原进程，下一心跳重试。',file=sys.stderr,flush=True)
        return False
    with supervisor_lock(root):
        publish()
        while alive(target_pid,target_created):
            sleep(poll_seconds);publish(status='waiting',reason='等待原采集进程自然结束')
        publish(status='waiting_for_writer',reason='原进程已退出，核对写入锁和其他采集器')
        while True:
            others=find(root)
            if others:
                publish(status='superseded',reason='检测到已启动的新采集器，取消额外启动以避免重复',existing_collectors=others)
                return state
            if idle(root):break
            sleep(poll_seconds);publish(status='waiting_for_writer',reason='等待现有维护/写入操作释放锁')
        stamp=time.strftime('%Y%m%d_%H%M%S',time.gmtime())
        log_path=root/'logs'/('restarted_main_'+stamp+'_'+str(os.getpid())+'.log');log_path.parent.mkdir(parents=True,exist_ok=True)
        try:
            with log_path.open('ab',buffering=0) as output:
                process=launch(command,cwd=str(root),stdout=output,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                publish(status='running',reason='已按当前config.yaml启动main.py一次',started_at=now(),collector_pid=process.pid,terminal_log=str(log_path))
                while process.poll() is None:
                    sleep(poll_seconds);publish(status='running',reason='新采集器正在运行')
                publish(status='finished' if process.returncode==0 else 'failed',reason='新采集器已退出，详见终端日志',exit_code=process.returncode,finished_at=now())
        except Exception as error:
            publish(status='failed',reason='启动/监控失败：'+type(error).__name__,finished_at=now())
            raise
    return state


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-pid',type=int,required=True)
    parser.add_argument('--target-created',type=float,required=True)
    args=parser.parse_args()
    if not (ROOT/'main.py').is_file() or not (ROOT/'config.yaml').is_file():raise ValueError('项目入口或配置不存在')
    supervise(ROOT,args.target_pid,args.target_created)


if __name__=='__main__':main()
