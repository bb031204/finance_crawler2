"""Per-invocation, source-ID-deduplicated collection accounting."""
from collections import Counter
import httpx
from finance_agent.safety import FetchError
from record_files import source_key


REASONS = {
    "duplicate_saved": "已有正式记录", "previously_rejected": "此前已筛选剔除",
    "screen_rejected": "搜索初筛剔除", "content_rejected": "正文判断无实质用途",
    "date_out_of_range": "来源日期超出范围", "publication_unconfirmed": "来源发布日期未确认",
    "call_budget": "Qwen调用预算耗尽", "context_budget": "Qwen上下文不足",
    "access_blocked": "访问限制/安全检查拦截", "network_error": "下载或网络失败",
    "pdf_error": "PDF正文不可读取", "body_error": "正文解析失败/过长",
    "model_error": "Qwen输出或语义证据校验失败", "model_service_error": "Ollama服务或连接失败",
    "storage_error": "文件写入/审计校验失败", "validation_error": "其他校验失败",
}


class SaveCounter:
    """Count verified additions once across batches; existing IDs are updates."""
    def __init__(self, baseline_ids):
        self.baseline_ids = set(baseline_ids)
        self.new_ids = set()

    def record(self, identity):
        added = identity not in self.baseline_ids and identity not in self.new_ids
        if added: self.new_ids.add(identity)
        return added, len(self.new_ids)


def error_reason(error, stage):
    """Use execution stage first; avoid treating every HTTP/model error as a download."""
    if stage == "save":
        return "storage_error"
    if stage in {"screen", "summarize", "preflight"}:
        return "model_service_error" if isinstance(error, httpx.HTTPError) else "model_error"
    if isinstance(error, FetchError):
        return "access_blocked" if error.status == "blocked" else "network_error"
    if isinstance(error, httpx.HTTPError):
        return "network_error"
    if stage == "fetch":
        return "pdf_error" if "PDF" in str(error).upper() else "body_error"
    return "validation_error"


def build_summary(batches, baseline_ids, dataset, active_pending, inactive_pending):
    # A continuation can fail in one batch and succeed later. Count its final fate once.
    final = {}
    verified = set()
    for batch in batches:
        for item in batch.get("item_results", []):
            final[item["source_id"]] = item
            if item["outcome"] == "saved":
                verified.add(item.get("record_id", item["source_id"]))
    verified &= set(dataset.rows)
    new_ids = verified - baseline_ids
    updated_ids = verified & baseline_ids
    rejected = Counter(item["reason_code"] for item in final.values() if item["outcome"] == "rejected")
    skipped = Counter(item["reason_code"] for item in final.values() if item["outcome"] == "skipped")
    deferred = Counter(final[i]["reason_code"] for i in active_pending if i in final and final[i]["outcome"] == "deferred")
    # Records can be inactive while retained in the queue (e.g. out-of-range dates).
    deferred_ids = {i for i in active_pending if i in final and final[i]["outcome"] == "deferred"}
    by_market, by_category, by_source = {}, {}, {}
    for identity in sorted(verified):
        market, row = dataset.rows[identity]
        kind = "new" if identity in new_ids else "updated"
        for mapping, key in [(by_market, market), (by_category, row["分类"]), (by_source, source_key(row["url"]))]:
            mapping.setdefault(key, {"new": 0, "updated": 0})[kind] += 1
    candidate_totals = {key: sum(batch.get(key, 0) for batch in batches) for key in ["candidates_new", "already_saved", "already_pending"]}
    direct_totals = {key: sum(batch.get("direct_discovery", {}).get(key, 0) for batch in batches)
                     for key in ["requests", "pages", "rows", "candidates_new", "already_saved", "already_pending", "already_rejected"]}
    return {
        "counting_basis": "本次调用；按source_id去重；仅正式文件及审计校验成功计入保存；GLOBAL副本不重复计数",
        "records_before": len(baseline_ids), "records_after": len(dataset.rows),
        "net_change": len(dataset.rows) - len(baseline_ids),
        "new_records": len(new_ids), "updated_records": len(updated_ids),
        "unverified_new_records": len(set(dataset.rows) - baseline_ids - verified),
        "verified_saved_records": len(verified),
        "rejected_records": sum(rejected.values()), "rejected_reasons": dict(rejected),
        "skipped_records": sum(skipped.values()), "skipped_reasons": dict(skipped),
        "deferred_records": len(deferred_ids), "deferred_reasons": dict(deferred),
        "active_pending": len(active_pending), "unattempted_records": len(set(active_pending) - set(final)),
        "inactive_pending": inactive_pending,
        "by_market": by_market, "by_category": by_category, "by_source": by_source,
        "maintenance": {key: sum(batch.get("maintenance", {}).get(key, 0) for batch in batches)
                        for key in ["migrated", "global_synced", "time_upgraded", "quarantined", "analysis_upgraded",'summary_cleaned','summary_quarantined']},
        "search": dict({key: sum(batch.get(key, 0) for batch in batches) for key in ["searched", "found"]},
                       **{key: value - direct_totals[key] for key, value in candidate_totals.items()}),
        "candidates": candidate_totals,
        "components": {key: sum(batch.get(key, 0) for batch in batches)
                       for key in ["considered", "downloaded", "page_cache_hits", "raw_deleted", "model_calls", "model_cache_hits",'screen_calls_avoided','transaction_recovered','preflight_checked','preflight_passed','preflight_rejected','preflight_deferred']},
        "error_attempts_by_phase": dict(Counter(error.get("phase", "unknown") for batch in batches for error in batch["errors"])),
        "cleanup_error_attempts": sum(batch.get("cleanup_errors", 0) for batch in batches),
        "direct_discovery": direct_totals,
    }


def log_summary(summary, log):
    s = summary
    log("数据结果", f"本次新增{s['new_records']}条，更新{s['updated_records']}条（均已校验，不含GLOBAL重复副本）")
    log("数据累计", f"运行前{s['records_before']}条 → 运行后{s['records_after']}条，净变化{s['net_change']:+d}条；迁移/隔离单独统计")
    if s['components'].get('preflight_checked'):
        c=s['components']
        log('前置检查汇总',f"检查{c['preflight_checked']}篇，通过{c['preflight_passed']}篇，提前跳过{c['preflight_rejected']}篇，待复核{c['preflight_deferred']}篇；未通过者不执行正文提炼")
    if s['components'].get('screen_calls_avoided') or s['components'].get('transaction_recovered'):
        log('效率汇总',f"省略重复标题初筛{s['components'].get('screen_calls_avoided',0)}次；恢复未完成保存事务{s['components'].get('transaction_recovered',0)}次；正文用途/日期校验继续执行")
    if s['unverified_new_records']:
        log("保存未完成", f"{s['unverified_new_records']}条新记录已写入，但后续校验/审计未成功，不计入成功新增，须复核")
    for label, count, reasons in [("筛选剔除", s['rejected_records'], s['rejected_reasons']),
                                  ("重复跳过", s['skipped_records'], s['skipped_reasons']),
                                  ("待复核", s['deferred_records'], s['deferred_reasons'])]:
        detail = "，".join(f"{REASONS.get(key, key)}{value}条" for key, value in sorted(reasons.items()))
        log(label, f"{count}条" + (" | " + detail if detail else ""))
    log("剩余队列", f"当前范围{s['active_pending']}条，其中未尝试{s['unattempted_records']}条、待复核{s['deferred_records']}条；范围外隔离保留{s['inactive_pending']}条")
    search = s['search']
    log("搜索总计", f"请求{search['searched']}次，返回{search['found']}条（返回含重复）")
    candidates = s['candidates']
    log("候选总计", f"搜索与目录合计新增候选{candidates['candidates_new']}条、命中已有记录{candidates['already_saved']}次、命中已有候选{candidates['already_pending']}次")
    c = s['components']
    log("处理总计", f"候选处理{c['considered']}次（含预算续处理），成功获取正文{c['downloaded']}次、复用正文缓存{c['page_cache_hits']}次；清理原文缓存{c['raw_deleted']}个")
    for label, mapping in [("保存市场分布", s['by_market']), ("保存分类分布", s['by_category']), ("保存来源分布", s['by_source'])]:
        log(label, "；".join(f"{key}: 新增{value['new']}、更新{value['updated']}" for key, value in sorted(mapping.items())) or "本次无成功保存")
    maintenance = s['maintenance']
    log("维护总计", f"旧格式迁移{maintenance['migrated']}条，时间补齐{maintenance['time_upgraded']}条，分析字段升级{maintenance['analysis_upgraded']}条，GLOBAL同步{maintenance['global_synced']}条，日期隔离{maintenance['quarantined']}条；均不算本次新增采集")
    if maintenance.get('summary_cleaned') or maintenance.get('summary_quarantined'):
        log('摘要去噪汇总',f"已有摘要清理{maintenance.get('summary_cleaned',0)}条，仅披露包装隔离{maintenance.get('summary_quarantined',0)}条；不算新增采集")
    if s['error_attempts_by_phase']:
        phases = {"screen": "Qwen初筛", "summarize": "Qwen整理", "fetch": "正文获取", "save": "保存/审计", "search": "搜索", "discovery": "历史发现", "sec_discovery": "SEC目录发现", "planning": "搜索规划", "validate": "范围校验"}
        log("异常尝试", "，".join(f"{phases.get(key, key)}{value}次" for key, value in sorted(s['error_attempts_by_phase'].items())) + "（按尝试次数，重试后成功不再计为待复核）")
    if s['cleanup_error_attempts']:
        log("原文清理异常", f"{s['cleanup_error_attempts']}次，原文保留，不影响已校验数据的保存计数")
