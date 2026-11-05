"""Discovery scheduler and persistent processing queue share one serialized model client."""
import threading
import time
from .pipeline import Engine

def run(cfg,root,stage="all"):
    stop=threading.Event()
    worker=Engine(cfg,root)
    worker.provider.preflight() if stage in {"all","discover"} else None
    worker.repo.recover()
    worker.recover_dns_blocked({})
    worker.apply_website_scope()
    worker.llm.budget_window_seconds=cfg.collection.poll_interval_seconds
    errors=[]
    def discovery():
        engine=Engine(cfg,root,llm=worker.llm)
        try:
            while not stop.is_set():
                started=time.monotonic()
                engine.cycle(discover=True,process=False)
                stop.wait(max(1,cfg.collection.poll_interval_seconds-(time.monotonic()-started)))
        except Exception as exc:
            errors.append(str(exc));stop.set()
        finally:engine.close()
    thread=None
    try:
        if stage in {"all","discover"}:
            thread=threading.Thread(target=discovery,name="financial-discovery",daemon=True);thread.start()
        if stage in {"all","process"}:
            while not stop.is_set():
                counts=worker.repo.queue_counts()
                if any(counts.get(k,0) for k in ["new","approved","fetched"]):
                    worker.cycle(discover=False,process=True)
                    stop.wait(5)
                else:stop.wait(2)
        else:
            while not stop.wait(1):pass
    except KeyboardInterrupt:
        print("收到停止信号，保留已提交队列和分块缓存。",flush=True)
    finally:
        stop.set()
        if thread:thread.join(timeout=cfg.ollama.timeout_seconds+cfg.fetch.timeout_seconds+5)
        if thread and thread.is_alive():worker.owns_llm=False
        worker.close()
    if errors:raise RuntimeError(errors[0])
    return 0
