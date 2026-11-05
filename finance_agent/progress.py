"""Console progress and derived run metrics; never serialize credentials or prompts."""
from datetime import datetime, timedelta, timezone

PURPOSE_LABELS = {"planner": "搜索规划", "relevance_filter": "相关性筛选", "extractor": "语义提取"}

def short(value, limit=100):
    return " ".join(str(value).split())[:limit]

def log(component, message):
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
    print(f"[{stamp}] [{component}] {message}", flush=True)

def log_scope(cfg, root, discover, process, start, end):
    log("任务", f"项目={root} | 模式={cfg.collection.mode} | 搜索={'开启' if discover else '关闭'} | 处理={'开启' if process else '关闭'}")
    if cfg.collection.mode == "historical":
        log("范围", f"原文发布日期={start} 至 {end}（含首尾日期）")
    else:
        log("范围", f"实时回看={cfg.collection.lookback_minutes}分钟 | 搜索日期={start} 至 {end}")
    log("范围", f"市场={','.join(cfg.monitoring.markets)} | 行业/方向={','.join(cfg.monitoring.directions)}")
    log("范围", "股票/公司=" + (", ".join(cfg.monitoring.securities) or "未指定个股，按行业和关键词查询"))
    log("范围", f"包含关键词={','.join(cfg.monitoring.include_keywords) or '无'} | 排除关键词={','.join(cfg.monitoring.exclude_keywords) or '无'}")
    for market in cfg.monitoring.markets:
        domains = cfg.search.domains_for(market, cfg.monitoring.markets)
        log("网站", f"{market} | 模式={cfg.search.website_mode or 'legacy'} | " + (", ".join(domains) or "不限制来源网站"))
    if cfg.search.exclude_domains:
        log("网站", "排除域名=" + ", ".join(cfg.search.exclude_domains))
    if cfg.search.provider == "rss":
        log("RSS", "订阅地址=" + ", ".join(cfg.search.rss_feeds))
    else:
        log("Tavily", f"配置 topic={cfg.search.topic} | depth={cfg.search.search_depth}")
    log("预算", f"查询最多{cfg.collection.max_queries_per_cycle}次/轮 | 每次最多{cfg.collection.max_candidates_per_query}条候选 | 正文最多{cfg.collection.max_documents_per_cycle}篇 | 提取最多{cfg.collection.max_extractions_per_cycle}篇")
    log("预算", f"模型最多{cfg.ollama.max_calls_per_cycle}次实际推理（含修复） | 每篇新增处理最多{cfg.fetch.max_chunks_per_document_cycle}块 | 搜索间隔{cfg.collection.poll_interval_seconds}秒")
    log("网络", f"本机代理={cfg.fetch.proxy_url or '未显式配置'} | Fake-IP兼容={cfg.fetch.allow_fake_ip} | 超时={cfg.fetch.timeout_seconds}秒 | robots检查={cfg.fetch.respect_robots}")
    log("组件配置", f"Crawl4AI清理={cfg.fetch.use_crawl4ai} | PDF={cfg.fetch.allow_pdf} | OCR={cfg.fetch.allow_ocr}")
    log("组件配置", f"Ollama={cfg.ollama.base_url} | Qwen模型={cfg.ollama.model} | 思考模式=关闭 | 上下文={cfg.ollama.num_ctx} | 输出上限={cfg.ollama.num_predict}")
    log("输出", f"数据库={root/cfg.output.database} | 事件JSONL={root/cfg.output.export_dir}")

def finish_model_stats(report, llm, usage_start, check_start, thread_id, calls_before):
    records = [item for item in getattr(llm, "usage", [])[usage_start:]
               if item.get("thread_id", thread_id) == thread_id]
    checks = [item for item in getattr(llm, "check_history", [])[check_start:]
              if item.get("thread_id", thread_id) == thread_id]
    calls = [item for item in records if not item["cached"]]
    count = len(calls) if hasattr(llm, "usage") else llm.calls - calls_before
    report["model_calls_delta"] = count
    stats = {"inference_calls": count, "model_cache_hits": sum(item["cached"] for item in records),
             "repair_calls": sum(item.get("repair", False) for item in calls),
             "validation_failures": sum(item.get("status") == "validation_failed" for item in calls),
             "request_failures": sum(item.get("status") == "request_failed" for item in calls),
             "by_purpose": {}}
    for purpose in PURPOSE_LABELS:
        rows = [item for item in records if item["purpose"] == purpose]
        stats["by_purpose"][purpose] = {"calls": sum(not item["cached"] for item in rows),
                                       "cache_hits": sum(item["cached"] for item in rows)}
    report["components"]["qwen"] = stats
    report["components"]["ollama"] = {"checks": len(checks), "checks_passed": sum(item.get("passed", False) for item in checks)}

def log_summary(report):
    label = {"completed": "本轮正常完成", "partial": "本轮部分完成", "failed": "本轮运行失败"}[report["status"]]
    log("汇总", f"{label} | 用时{report['elapsed_seconds']:.2f}秒 | 错误{report['error_count']}项")
    log("搜索汇总", f"请求{report['queries_attempted']}次，成功{report['queries_executed']}次，间隔跳过{report['search_skipped_recent']}次 | 来源返回{report['provider_results_returned']}条，可用候选{report['candidates_seen']}条，去重后{report['candidates_unique']}条 | 新增入队{report['candidates_new']}条，已有/重复{report['candidates_existing']}条，范围/无效过滤{report['provider_results_filtered']+report['candidates_out_of_scope']}条")
    log("处理汇总", f"初筛{report['screened']}条，通过{report['screen_accepted']}条，拒绝{report['screen_rejected']}条 | 正文尝试{report['download_attempted']}篇，抓取解析成功{report['download_parsed']}篇，入库{report['documents_fetched']}篇，日期范围外{report['documents_out_of_range']}篇")
    log("事件汇总", f"本轮候选事件{report['events_generated']}条，实际新增入库{report['events_inserted']}条 | 库中事件共{report['total_events']}条 | 未通过证据检查{report['review_events']}条")
    qwen = report["components"]["qwen"]
    log("Qwen汇总", f"实际推理{qwen['inference_calls']}次，含修复{qwen['repair_calls']}次 | 结构/证据未通过{qwen['validation_failures']}次，连接失败{qwen['request_failures']}次 | 模型缓存{qwen['model_cache_hits']}次 | 规划缓存{report['planner_cache_hits']}次 | 已校验分块缓存{report['chunks_cached']}块")
    for purpose, counts in qwen["by_purpose"].items():
        log("Qwen分工", f"{PURPOSE_LABELS[purpose]}：推理{counts['calls']}次，模型缓存{counts['cache_hits']}次")
    checks = report["components"]["ollama"]
    log("Ollama", f"本轮模型检查{checks['checks']}次，通过{checks['checks_passed']}次" if checks["checks"] else "本轮未执行模型检查")
    crawler = report["components"]["crawl4ai"]
    pdf = report["components"]["pdf"]
    log("解析汇总", f"Crawl4AI清理成功{crawler['cleaned']}篇，回退{crawler['fallback']}篇 | PDF解析{pdf['parsed']}篇，OCR恢复{pdf['ocr_pages']}页")
    log("队列", f"自动待处理{report['automatic_pending']}篇 | 待人工复核{report['needs_review_count']}篇 | 待OCR处理{report['needs_ocr_count']}篇")
    if report["budget_pause"]:
        log("预算", "本轮达到处理预算，剩余自动任务保留，可再次运行续处理")
    if report["needs_review_count"] or report["needs_ocr_count"]:
        log("待复核", "待人工复核或OCR的记录已单独保留，普通运行仅处理自动队列。")
    log("报告", report["run_report"])
