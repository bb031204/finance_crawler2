"""Elapsed time and conservative live ETA; never mistake saved count for work progress."""
import math
import threading
import time
from collections import deque


def duration(seconds):
    if seconds is None: return "暂无法估算"
    seconds = max(0, math.ceil(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (f"{days}天 " if days else "") + f"{hours:02}:{minutes:02}:{seconds:02}"


def eta(samples, remaining, current_elapsed=0):
    if remaining is None: return None
    if remaining == 0: return 0.0
    if len(samples) < 2: return None
    average = sum(samples) / len(samples)
    # A long document can invalidate the early short-document average.
    if average <= 0 or current_elapsed >= average: return None
    return max(0.0, average * remaining - current_elapsed)


class CollectionProgress:
    def __init__(self, log, clock=None, interval=30):
        self.log, self.clock, self.interval = log, clock or time.monotonic, interval
        self.started = self.clock()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.samples = deque(maxlen=20)
        self.accumulated = {}
        self.finished_ids = set()
        self.deferred_ids = set()
        self.queue_ids = None
        self.current = None
        self.activity = "启动/发现候选"
        self.discovery_complete = False
        self.discovery_paused = False
        self.phases = {}
        self.ended = False

    def __enter__(self):
        self.thread = threading.Thread(target=self._heartbeat, name="collection-progress", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, error_type, error, traceback):
        self.stop.set()
        if self.thread: self.thread.join(timeout=1)
        self.emit("运行中断" if error_type else "运行耗时")

    def _heartbeat(self):
        while not self.stop.wait(self.interval): self.emit("进度心跳")

    def set_activity(self, label):
        with self.lock: self.activity = label

    def set_discovery(self, complete, paused=False):
        with self.lock:
            self.discovery_complete, self.discovery_paused = complete, paused

    def set_queue(self, identities):
        with self.lock: self.queue_ids = set(identities)

    def begin_item(self, identity):
        with self.lock:
            self.current = (identity, self.clock())
            self.activity = "正文获取/Qwen处理"

    def finish_item(self, identity, terminal=True, sample=True, deferred=False):
        with self.lock:
            if self.current and self.current[0] == identity:
                seconds = max(0, self.clock() - self.current[1])
                self.accumulated[identity] = self.accumulated.get(identity, 0) + seconds
                self.current = None
            if terminal:
                if sample and identity not in self.finished_ids and identity in self.accumulated:
                    self.samples.append(self.accumulated[identity])
                self.finished_ids.add(identity)
                if deferred: self.deferred_ids.add(identity)
                else: self.deferred_ids.discard(identity)
                if self.queue_ids is not None: self.queue_ids.discard(identity)
                self.accumulated.pop(identity, None)

    def phase(self, name, completed, remaining, elapsed, units=1, unknown=0, catalogs=None):
        with self.lock:
            phase = self.phases.setdefault(name, {"samples": deque(maxlen=20), "pending_seconds": 0.0, "elapsed_seconds": 0.0})
            phase["elapsed_seconds"] += max(0, elapsed)
            phase["pending_seconds"] += max(0, elapsed)
            if units:
                phase["samples"].append(phase["pending_seconds"] / units)
                phase["pending_seconds"] = 0.0
            phase.update(completed=completed, remaining=remaining, unknown=unknown, catalogs=list(catalogs) if catalogs else None)
            result = self._phase_snapshot(phase)
        label = f"目录{catalogs[0]}/{catalogs[1]} | " if catalogs else ""
        unit = "页" if catalogs else "任务"
        label += f"已知{unit}{completed}/{completed + remaining} | 阶段已用{duration(result['elapsed_seconds'])} | 已知部分预计剩余{duration(result['eta_seconds'])}"
        if unknown: label += f" | 另有{unknown}个目录页数待展开"
        self.log(name + "进度", label)

    def _phase_snapshot(self, phase):
        return {key: value for key, value in phase.items() if key not in {"samples", "pending_seconds"}} | {
            "eta_seconds": eta(phase["samples"], phase["remaining"]), "sample_count": len(phase["samples"])}

    def snapshot(self):
        with self.lock:
            current_elapsed = max(0, self.clock() - self.current[1]) if self.current else 0.0
            remaining = len(self.queue_ids) if self.queue_ids is not None else None
            # Partial work accumulated across model-budget continuations is already spent.
            partial = self.accumulated.get(self.current[0], 0) if self.current else 0.0
            queue_eta = eta(self.samples, remaining, current_elapsed + partial)
            reason = "按最近最多20条处理耗时估算；含筛选剔除和失败，不等于成功JSON数"
            if remaining and len(self.samples) < 2: reason = "完成样本不足2条，暂无法估算"
            elif remaining and queue_eta is None: reason = "当前任务超出平均耗时或速度不足，等待更新估算"
            if self.discovery_paused: overall_reason = "发现已暂停，需恢复后才能估计整轮剩余时间"
            elif not self.discovery_complete: overall_reason = "仍在发现候选，整轮数量尚未确定"
            elif self.deferred_ids: overall_reason = f"尚有{len(self.deferred_ids)}条待复核，其恢复时间不确定"
            else: overall_reason = reason
            return {"elapsed_seconds": max(0, self.clock() - self.started), "activity": self.activity,
                "processed_unique": len(self.finished_ids), "known_queue_remaining": remaining,
                "queue_eta_seconds": queue_eta,
                "overall_eta_seconds": queue_eta if self.discovery_complete and not self.discovery_paused and not self.deferred_ids else None,
                "deferred_records": len(self.deferred_ids),
                "discovery_complete": self.discovery_complete, "discovery_paused": self.discovery_paused,
                "current_item_elapsed_seconds": current_elapsed, "sample_count": len(self.samples),
                "queue_eta_basis": reason, "overall_eta_basis": overall_reason,
                "phases": {name: self._phase_snapshot(phase) for name, phase in self.phases.items()},
                "ended": self.ended}

    def emit(self, tag="处理进度"):
        s = self.snapshot()
        remaining = "未知" if s['known_queue_remaining'] is None else str(s['known_queue_remaining'])
        message = (f"已用{duration(s['elapsed_seconds'])} | 本次已处理{s['processed_unique']}条 | 当前已知队列剩余{remaining}条 | "
                   f"队列预计剩余{duration(s['queue_eta_seconds'])} | 整轮预计剩余{duration(s['overall_eta_seconds'])}")
        if s['overall_eta_seconds'] is None: message += "（" + s['overall_eta_basis'] + "）"
        total = s['processed_unique'] + (s['known_queue_remaining'] or 0)
        if total: message += f" | 已知候选进度{s['processed_unique'] / total:.1%}"
        if s['current_item_elapsed_seconds']: message += f" | 当前单篇已用{duration(s['current_item_elapsed_seconds'])}"
        self.log(tag, message + " | 阶段=" + s['activity'])

    def finalize(self):
        with self.lock:
            self.activity = "本次运行结束（未完成/待复核见汇总）"
            self.ended = True
