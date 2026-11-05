import json
import os
import time
import uuid
import threading
from math import ceil
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.parse import urlsplit
from .core import ROOT,bounded,digest,dump,now
from .database import Repository
from .safety import PublicHTTP,FetchError,canonical,public_url
from .provider import TavilyProvider,RSSProvider
from .llm import OllamaClient,BudgetExceeded
from .agents import plan_queries,screen
from .fetcher import ContentFetcher,publication
from .extraction import Extractor
from .progress import log, short, log_scope, finish_model_stats, log_summary

def window(cfg):
    if cfg.collection.mode=="historical":return cfg.collection.start_date,cfg.collection.end_date
    end=datetime.now(timezone.utc)
    return (end-timedelta(minutes=cfg.collection.lookback_minutes,hours=14)).date().isoformat(),(end+timedelta(hours=14)).date().isoformat()

class Engine:
    def __init__(self,cfg,root=ROOT,repo=None,llm=None,provider=None,fetcher=None):
        self.cfg,self.root=cfg,Path(root)
        self._seen_candidate_ids = set()
        self.repo=repo or Repository(self.root/cfg.output.database,self.root)
        self.http=PublicHTTP(cfg.fetch)
        self.llm=llm or OllamaClient(cfg.ollama,self.root)
        self.owns_llm=llm is None
        self.provider=provider or (TavilyProvider(cfg.search) if cfg.search.provider=="tavily" else RSSProvider(cfg.search,self.http))
        self.fetcher=fetcher or ContentFetcher(cfg,self.http,self.root)
    def close(self):
        self.provider.close()
        self.http.close()
        self.repo.close()
        if self.owns_llm:self.llm.close()
    def discover(self,report):
        self.provider.preflight()
        queue=self.repo.queue_counts()
        if sum(queue.get(k,0) for k in ["new","approved","fetched"])>=self.cfg.collection.max_pending_candidates:
            report["budget_pause"]=True
            log("发现预算", "自动队列达到积压上限，本轮优先消化已有任务")
            return
        start,end=window(self.cfg);all_queries=[]
        for round_no in range(self.cfg.collection.max_followup_search_rounds+1):
            remaining=self.cfg.collection.max_queries_per_cycle-len(all_queries)
            if not remaining:break
            cache_key="plan:"+digest([self.cfg.monitoring.model_dump(),start,end,round_no,all_queries,remaining])
            cached=self.repo.state(cache_key)
            if cached and time.time()-cached["created"]<self.cfg.search.planner_cache_seconds:
                queries=cached["queries"]
                report["planner_cache_hits"] += 1
                log("规划缓存", f"命中查询计划，共{len(queries)}条，本轮最多执行{remaining}条")
            else:
                log("Qwen规划", f"生成第{round_no+1}轮查询，剩余查询预算{remaining}次")
                queries=plan_queries(self.llm,self.cfg,self.repo,start,end,round_no,all_queries,remaining)
                self.repo.state(cache_key,{"created":time.time(),"queries":queries})
            for query in queries[:remaining]:
                q=query["query"]
                if q in all_queries:continue
                all_queries.append(q)
                checkpoint="search:"+digest([self.cfg.search.model_dump(),self.cfg.monitoring.markets,query["market"],q,start,end])
                prior=self.repo.state(checkpoint)
                interval=self.cfg.collection.poll_interval_seconds
                if prior and time.time()-prior["searched_epoch"]<interval:
                    report["search_skipped_recent"]+=1
                    wait_seconds = max(1, ceil(interval - (time.time() - prior["searched_epoch"])))
                    report.setdefault("search_skips", []).append({
                        "market": query["market"], "query": q, "reason": "recent_query",
                        "retry_after_seconds": wait_seconds
                    })
                    log("搜索间隔", f"跳过搜索：[{query['market']}] {q} | {interval}秒内已执行 | 约{wait_seconds}秒后可再次搜索")
                    continue
                report["queries_attempted"] += 1
                domains = self.cfg.search.domains_for(query["market"], self.cfg.monitoring.markets)
                log("搜索开始", f"[{report['queries_attempted']}/{self.cfg.collection.max_queries_per_cycle}] {query['market']} | {short(q, 200)} | {start}至{end} | 最多{self.cfg.collection.max_candidates_per_query}条 | 网站={','.join(domains) or '不限制'}")
                search_started = time.perf_counter()
                items,snapshot=self.provider.search(q,start,end,self.cfg.collection.max_candidates_per_query,
                                                    market=query["market"],markets=self.cfg.monitoring.markets)
                raw_results = snapshot.get("response", {}).get("results")
                returned = len(raw_results) if isinstance(raw_results, list) else len(items)
                filtered = max(0, returned - len(items))
                report["provider_results_returned"] += returned
                report["provider_results_filtered"] += filtered
                safe_snapshot=bounded(self.root/".runtime/search_responses"/(digest(snapshot)+".json"),self.root)
                dump(safe_snapshot,snapshot)
                inserted=0;existing=0;outside=0
                for c in items:
                    self._seen_candidate_ids.add(c["candidate_id"])
                    if not self.cfg.search.allows_url(c["url"], query["market"], self.cfg.monitoring.markets):
                        outside += 1
                        continue
                    c["monitoring_direction"]=query["direction"];c["query_market"]=query["market"]
                    is_new=self.repo.candidate(c["candidate_id"]) is None
                    existing += int(not is_new)
                    self.repo.upsert_candidate(c,report["run_id"]);inserted+=int(is_new)
                report["queries_executed"]+=1;report["candidates_seen"]+=len(items);report["candidates_new"]+=inserted
                report["candidates_existing"] += existing
                report["candidates_out_of_scope"] += outside
                report["candidates_unique"] = len(self._seen_candidate_ids)
                report["search_requests"].append({"query":q,"market":query["market"],
                    "include_domains":self.cfg.search.domains_for(query["market"],self.cfg.monitoring.markets),
                    "start":start,"end":end,"returned":len(items),"provider_returned":returned,"provider_filtered":filtered,"new":inserted,"existing":existing,
                    "unique":len({c["candidate_id"] for c in items}),"out_of_scope":outside,
                    "seconds":round(time.perf_counter()-search_started, 3),
                    "snapshot":safe_snapshot.relative_to(self.root).as_posix(),
                    "coverage":"provider_visible_results_only; not exhaustive historical coverage"})
                self.repo.state(checkpoint,{"searched_epoch":time.time(),"returned":len(items)})
                log("搜索完成", f"{query['market']} | 来源返回{returned}条，可用候选{len(items)}条，新增{inserted}条，已有/重复{existing}条，过滤{filtered+outside}条 | 用时{time.perf_counter()-search_started:.2f}秒")
    def failure(self,row,status,error,report,exc=None):
        count=row["attempts"]+1
        terminal=status in {"blocked","not_found","download_invalid","needs_ocr","out_of_range","out_of_scope"}
        state=status if terminal else (row["status"] if count<=self.cfg.collection.max_retries else "needs_review")
        delay=min(900,30*(2**(count-1)))
        if isinstance(exc,FetchError) and exc.info.get("retry_after"):
            try:delay=max(delay,min(86400,int(exc.info["retry_after"])))
            except ValueError:pass
        self.repo.set_status(row["id"],state,str(error)[:700],attempt=not terminal,retry_delay=0 if terminal else delay)
        if isinstance(exc,FetchError) and exc.raw:
            path=self.root/".runtime/failures"/(digest([row["id"],now()])+".bin")
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(exc.raw)
        report["errors"].append({"candidate_id":row["id"],"url":row["url"],"stage":row["status"],"status":status,
                                  "error":str(error)[:400],"queue_status":state})
        action = f"{delay}秒后可重试" if not terminal and state != "needs_review" else "保留当前状态待处理"
        log("错误", f"待处理/复核：{row['id'][:16]} | {status} | {short(error, 180)} | 状态={state} | {action}")
        log("错误来源", short(row["url"], 240))
    def apply_website_scope(self):
        """Pause queued work outside the policy; resume its original stage when allowed."""
        rows = self.repo.db.execute(
            "SELECT id,status,payload FROM candidates WHERE status IN ('new','approved','fetched','out_of_scope')"
        ).fetchall()
        for row in rows:
            candidate = json.loads(row["payload"])
            allowed = self.cfg.search.allows_url(candidate["url"], candidate.get("query_market"),
                                                self.cfg.monitoring.markets)
            key = "website_scope:" + row["id"]
            if not allowed and row["status"] != "out_of_scope":
                # Persist the resume stage before changing status, allowing safe recovery.
                self.repo.state(key, {"resume_status": row["status"]})
                self.repo.set_status(row["id"], "out_of_scope", "来源不在当前市场的网站范围内")
            elif allowed and row["status"] == "out_of_scope":
                paused = self.repo.state(key) or {}
                stage = paused.get("resume_status", "new")
                if stage not in {"new", "approved", "fetched"}:
                    stage = "new"
                self.repo.set_status(row["id"], stage)

    def recover_dns_blocked(self, report):
        if not (self.cfg.fetch.proxy_url and self.cfg.fetch.allow_fake_ip):
            return
        rows = self.repo.db.execute(
            "SELECT id,url,payload FROM candidates WHERE status='blocked' AND error=?",
            ("禁止访问回环、私网、链路本地及特殊地址",)
        ).fetchall()
        for row in rows:
            candidate = json.loads(row["payload"])
            market = candidate.get("query_market")
            markets = self.cfg.monitoring.markets
            if not self.cfg.search.allows_url(row["url"], market, markets):
                continue
            try:
                public_url(row["url"], allowed=self.cfg.search.domains_for(market, markets) or None,
                           excluded=self.cfg.search.exclude_domains, allow_fake_ip=True)
            except FetchError:
                continue
            self.repo.state("dns_recovery:" + row["id"], {
                "at": now(), "previous_error": "禁止访问回环、私网、链路本地及特殊地址",
                "resume_status": "approved", "reason": "explicit_local_proxy_fake_ip_compatibility"
            })
            self.repo.set_status(row["id"], "approved")
            report["network_recovered"] = report.get("network_recovered", 0) + 1
            print(f"恢复DNS任务：{row['id'][:16]} | {urlsplit(row['url']).hostname}", flush=True)

    def process(self,report):
        self.recover_dns_blocked(report)
        self.apply_website_scope()
        count=self.repo.queue_counts()
        log("队列开始", f"待筛选{count.get('new',0)}条 | 待下载{count.get('approved',0)}篇 | 待提取{count.get('fetched',0)}篇 | 待复核{count.get('needs_review',0)}篇")
        if not any(count.get(x,0) for x in ["new","approved","fetched"]):
            log("处理阶段", "自动队列为空，本轮无正文下载或Qwen提取任务")
            return
        self.llm.check()
        rows=self.repo.pending("new",self.cfg.collection.max_documents_per_cycle)
        log("筛选阶段", f"本轮到期候选{len(rows)}条，每批最多{self.cfg.collection.max_candidates_per_batch}条")
        batch_size=self.cfg.collection.max_candidates_per_batch
        for offset in range(0,len(rows),batch_size):
            batch=rows[offset:offset+batch_size]
            for row in batch:self.repo.set_status(row["id"],"screening")
            try:
                log("Qwen筛选", f"候选{offset+1}至{offset+len(batch)}/{len(rows)}，正在检查标题和摘要")
                decisions=screen(self.llm,self.cfg,[json.loads(r["payload"]) for r in batch])
                for decision in decisions:
                    c=json.loads(self.repo.candidate(decision["candidate_id"])["payload"])
                    official=(c["source_name"] or "").endswith((".gov",".gov.cn")) or c["source_name"] in {
                        "www.cninfo.com.cn","www.sse.com.cn","static.sse.com.cn"}
                    if official and any(w in c["title"] for w in ["公告","政策","财报","报告","Policy","Statement"]):
                        if not decision["fetch_full_text"]:
                            decision.update(relevant=True,fetch_full_text=True,needs_review=True,
                                            reason="官方重要披露保守保留；原初筛："+decision["reason"])
                    self.repo.state("decision:"+decision["candidate_id"],decision)
                    self.repo.set_status(decision["candidate_id"],"approved" if
                        decision["relevant"] and decision["fetch_full_text"] else "rejected")
                    report["screened"]+=1
                    approved = decision["relevant"] and decision["fetch_full_text"]
                    report["screen_accepted" if approved else "screen_rejected"] += 1
                    log("筛选结果", f"{'通过' if approved else '拒绝'} | {decision['candidate_id'][:16]} | {short(c['title'])} | {short(decision['reason'], 120)}")
            except BudgetExceeded:
                for row in batch:self.repo.set_status(row["id"],"new",retry_delay=30)
                report["budget_pause"]=True;break
            except Exception as exc:
                for row in batch:self.failure(row,"screen_failed",exc,report)
        download_rows = self.repo.pending("approved",self.cfg.collection.max_documents_per_cycle)
        log("下载阶段", f"本轮到期正文{len(download_rows)}篇，上限{self.cfg.collection.max_documents_per_cycle}篇")
        for download_index, row in enumerate(download_rows, 1):
            self.repo.set_status(row["id"],"fetching")
            try:
                c=json.loads(row["payload"]);c["first_seen_at"]=row["first_seen"]
                report["download_attempted"] += 1
                log("下载开始", f"[{download_index}/{len(download_rows)}] {c.get('query_market','未标记市场')} | {row['id'][:16]} | {short(c['title'])}")
                log("来源", short(c["url"], 240))
                fetch_started = time.perf_counter()
                doc=self.fetcher.fetch(c)
                report["download_parsed"] += 1
                quality = doc["quality"]
                is_pdf = doc["raw_path"].endswith(".pdf")
                is_html = "html" in doc.get("content_type", "").lower()
                if is_pdf:
                    report["components"]["pdf"]["parsed"] += 1
                    report["components"]["pdf"]["ocr_pages"] += len(quality.get("ocr_pages", []))
                    parser = "PDF/" + quality.get("backend", "未知后端")
                elif is_html:
                    cleaned = quality.get("crawler_cleaning", False)
                    if cleaned:
                        report["components"]["crawl4ai"]["cleaned"] += 1
                    elif self.cfg.fetch.use_crawl4ai:
                        report["components"]["crawl4ai"]["fallback"] += 1
                    parser = "Crawl4AI清理" if cleaned else "lxml正文清理"
                else:
                    parser = "已导入正文"
                chars = sum(len(p["text"]) for p in doc["paragraphs"])
                log("解析完成", f"{row['id'][:16]} | {parser} | {len(doc['paragraphs'])}段/{chars}字符 | 日期={doc.get('published_date') or 'unknown'} | 质量={quality.get('status')} | 用时{time.perf_counter()-fetch_started:.2f}秒")
                start,end=window(self.cfg)
                outside=(self.cfg.collection.mode=="historical" and doc["published_date"] and not start<=doc["published_date"]<=end)
                if self.cfg.collection.mode=="realtime" and doc.get("published_at"):
                    instant=datetime.fromisoformat(doc["published_at"])
                    clock=datetime.now(timezone.utc)
                    outside=instant<clock-timedelta(minutes=self.cfg.collection.lookback_minutes) or instant>clock+timedelta(minutes=5)
                if outside:
                    report["documents_out_of_range"] += 1
                    self.failure(row,"out_of_range","正文发布时间不在配置范围内",report);continue
                self.repo.save_document(doc)
                if doc["quality"].get("status")=="needs_ocr":
                    self.repo.set_status(row["id"],"needs_ocr","正文仍有缺失文字页面")
                report["documents_fetched"]+=1
            except FetchError as exc:self.failure(row,exc.status,exc,report,exc)
            except Exception as exc:self.failure(row,"parse_failed",exc,report)
        extractor=Extractor(self.cfg,self.llm,self.repo,self.root)
        extraction_rows = self.repo.pending("fetched",self.cfg.collection.max_extractions_per_cycle)
        log("提取阶段", f"本轮到期正文{len(extraction_rows)}篇，上限{self.cfg.collection.max_extractions_per_cycle}篇")
        for extraction_index, row in enumerate(extraction_rows, 1):
            self.repo.set_status(row["id"],"extracting")
            try:
                log("提取开始", f"[{extraction_index}/{len(extraction_rows)}] {row['id'][:16]} | {short(json.loads(row['payload'])['title'])}")
                extraction_started = time.perf_counter()
                result=extractor.process(self.repo.document(row["id"]))
                report["chunks_cached"] += result.get("chunks_cached", 0)
                report["review_events"] += len(result.get("rejected_events", []))
                log("提取结果", f"{row['id'][:16]} | {result['status']} | 分块{result['completed_chunks']}/{result['total_chunks']} | 合格候选事件{len(result['events'])}条，待复核{len(result.get('rejected_events', []))}条 | 用时{time.perf_counter()-extraction_started:.2f}秒")
                for rejected in result.get("rejected_events", []):
                    log("证据待复核", f"第{rejected['chunk']+1}块，事件{rejected['event_index']+1} | {short(rejected['error'], 160)}")
                report["extractions"].append({"candidate_id":row["id"],**{k:v for k,v in result.items() if k!="events"}})
                if result["status"]=="partial_pending":
                    self.repo.set_status(row["id"],"fetched",retry_delay=1)
                report["events_generated"]+=len(result["events"])
            except BudgetExceeded:
                self.repo.set_status(row["id"],"fetched",retry_delay=30)
                report["budget_pause"]=True;break
            except Exception as exc:self.failure(row,"extraction_failed",exc,report)
    def cycle(self,discover=True,process=True):
        report={"run_id":uuid.uuid4().hex,"started_at":now(),"mode":self.cfg.collection.mode,
            "discovery_enabled":discover,"processing_enabled":process,"queries_executed":0,
            "search_skipped_recent":0,"candidates_seen":0,"candidates_new":0,"screened":0,
            "documents_fetched":0,"events_generated":0,"search_requests":[],"extractions":[],
            "errors":[],"budget_pause":False,
            "queries_attempted":0,"provider_results_returned":0,"provider_results_filtered":0,"candidates_existing":0,"candidates_out_of_scope":0,"candidates_unique":0,
            "planner_cache_hits":0,"screen_accepted":0,"screen_rejected":0,
            "download_attempted":0,"download_parsed":0,"documents_out_of_range":0,
            "chunks_cached":0,"review_events":0,
            "components":{"crawl4ai":{"cleaned":0,"fallback":0},"pdf":{"parsed":0,"ocr_pages":0}}}
        self._seen_candidate_ids = set()
        before=self.llm.calls
        usage_start = len(getattr(self.llm, "usage", []))
        check_start = len(getattr(self.llm, "check_history", []))
        thread_id = threading.get_ident()
        started = time.perf_counter()
        initial_events = self.repo.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        try:
            start, end = window(self.cfg)
            log_scope(self.cfg, self.root, discover, process, start, end)
            if discover:
                try:self.discover(report)
                except Exception as exc:report["errors"].append({"stage":"discovery","error":str(exc)[:400]})
            if process:
                try:self.process(report)
                except Exception as exc:report["errors"].append({"stage":"processing","error":str(exc)[:400]})
            if process:
                from .query import export_events
                paths = export_events(self.repo,self.root/self.cfg.output.export_dir)
                report["export_files"] = paths
                log("数据导出", f"SQLite主库已同步导出{len(paths)}个事件JSONL文件 | {self.root/self.cfg.output.export_dir}")
        finally:
            report["queue"]=self.repo.queue_counts();report["finished_at"]=now()
            finish_model_stats(report, self.llm, usage_start, check_start, thread_id, before)
            report["elapsed_seconds"] = round(time.perf_counter()-started, 3)
            report["automatic_pending"] = sum(
                report["queue"].get(status, 0)
                for status in ["new", "approved", "fetched", "screening", "fetching", "extracting"]
            )
            report["needs_review_count"] = report["queue"].get("needs_review", 0)
            report["needs_ocr_count"] = report["queue"].get("needs_ocr", 0)
            report["error_count"] = len(report["errors"])
            report["total_events"] = self.repo.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            report["events_inserted"] = report["total_events"] - initial_events
            pending = report["budget_pause"] or report["automatic_pending"] > 0
            fatal_error = any("candidate_id" not in error for error in report["errors"])
            progress = any(report[key] for key in [
                "queries_executed", "screened", "documents_fetched", "events_generated"
            ])
            if fatal_error or (report["errors"] and not progress):
                report["status"] = "failed"
            elif report["errors"] or pending:
                report["status"] = "partial"
            else:
                report["status"] = "completed"
            report["run_report"] = str(self.root / "logs" / (report["run_id"] + ".json"))
            self.repo.run(report)
            dump(Path(report["run_report"]),report)
        log_summary(report)
        print(json.dumps({k:report[k] for k in ["status","queries_executed","search_skipped_recent","candidates_new",
              "documents_fetched","events_generated","total_events","model_calls_delta","error_count",
              "automatic_pending","needs_review_count","needs_ocr_count","queue"]},ensure_ascii=False),flush=True)
        return report

def import_document(path,cfg,repo,root=ROOT):
    root=Path(root);path=Path(path)
    path=bounded(path if path.is_absolute() else root/path,root)
    if path.stat().st_size>cfg.fetch.max_bytes:raise ValueError("导入文件超过大小限制")
    raw=path.read_bytes();source=json.loads(raw)
    url=canonical(source["source_url"]);p=urlsplit(url)
    if p.scheme not in {"http","https"} or not p.hostname or p.username or p.password:raise ValueError("导入来源URL无效")
    paragraphs=source.get("paragraphs") or [{"text":x} for x in source.get("text","").split("\n\n") if x.strip()]
    paragraphs=[dict(p,id="P"+str(i)) for i,p in enumerate(paragraphs,1) if p.get("text","").strip()]
    if not paragraphs:raise ValueError("导入文件没有正文")
    stamp=now();cid="C_"+digest(url)
    existing=repo.candidate(cid)
    first_seen=existing["first_seen"] if existing else stamp
    c={"candidate_id":cid,"url":url,"title":source["title"],"snippet":"","source_name":p.hostname,
       "published_hint":None,"search_query":"explicit_local_import","search_provider":"imported_snapshot","first_seen_at":first_seen}
    repo.upsert_candidate(c,"import_"+uuid.uuid4().hex)
    raw_path=bounded(root/cfg.output.raw_dir/(digest(raw)+".json"),root)
    raw_path.parent.mkdir(parents=True,exist_ok=True)
    if not raw_path.exists():raw_path.write_bytes(raw)
    pubraw=source.get("published_at") or source.get("published_date")
    doc={"candidate_id":cid,"version_id":"D_"+digest([url,digest(raw)]),"source_url":url,"final_url":url,
         "title":source["title"],"source_name":p.hostname,"content_type":"application/json",
         "content_hash":digest(raw),"raw_path":raw_path.relative_to(root).as_posix(),"paragraphs":paragraphs,
         "quality":source.get("quality",{"status":"imported_unverified"}),
         "published_at_raw":pubraw,**publication(pubraw),"time_evidence":"user_supplied_snapshot",
         "provider_published_hint":None,"first_seen_at":first_seen,"fetched_at":stamp,"document_available_at":stamp,
         "fetch_method":"explicit_local_import; not a live website fetch"}
    repo.save_document(doc)
    if doc["quality"].get("status")=="needs_ocr":repo.set_status(cid,"needs_ocr","导入正文缺页")
    elif cfg.collection.mode=="historical" and doc["published_date"] and not (
            cfg.collection.start_date<=doc["published_date"]<=cfg.collection.end_date):
        repo.set_status(cid,"out_of_range","导入正文已归档，但发布时间不在当前配置窗口")
    return doc
