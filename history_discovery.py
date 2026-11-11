"""Finite, year-balanced historical discovery with durable HTTP-stage checkpoints."""
from calendar import monthrange
from copy import deepcopy
from datetime import date,timedelta
from time import monotonic
import httpx
from finance_agent.core import digest,now
from finance_agent.progress import log,short
from qwen_agent import COVERAGE_PROMPT,ModelBudget,LEGACY_COVERAGE_PROMPT,PLANNING_SCOPE_VERSION
from record_files import source_id

VERSION="year_balanced_v2"


def historical_jobs(cfg,start,end):
    first,last=date.fromisoformat(start),date.fromisoformat(end)
    years=list(range(first.year,last.year+1))
    directions=list(dict.fromkeys(cfg.monitoring.resolved_directions))
    jobs=[]
    # Every enabled year/market receives a chance before the next direction.
    for direction in directions:
        for year in years:
            left=max(first,date(year,1,1));right=min(last,date(year,12,31))
            for market in cfg.monitoring.markets:
                jobs.append(dict(phase="year",year=year,direction=direction,market=market,range=[left.isoformat(),right.isoformat()]))
    windows={year:[] for year in years}
    if cfg.search.date_window_days:
        for year in years:
            for month in range(1,13):
                left=max(first,date(year,month,1));right=min(last,date(year,month,monthrange(year,month)[1]))
                while left<=right:
                    stop=min(right,left+timedelta(days=cfg.search.date_window_days-1))
                    windows[year].append([left.isoformat(),stop.isoformat()]);left=stop+timedelta(days=1)
        # Refinement visits January in each year, then February, etc.
        window_index=0
        for slot in range(max(map(len,windows.values()),default=0)):
            for index,year in enumerate(years):
                if slot>=len(windows[year]):continue
                for market in cfg.monitoring.markets:
                    jobs.append(dict(phase="window",year=year,direction=directions[window_index%len(directions)],
                        market=market,range=windows[year][slot],source_turn=window_index))
                window_index+=1
    return jobs


def history_context(crawler,start,end):
    cfg=crawler.cfg
    inputs=[VERSION,cfg.monitoring.model_dump(),start,end,
        cfg.search.model_dump(exclude={"complete_history", "discovery_mode", "sec_edgar"})]
    scope=digest(inputs+[PLANNING_SCOPE_VERSION])
    jobs=historical_jobs(cfg,start,end)
    histories=crawler.state.setdefault('discovery',{})
    legacy=digest(inputs+[LEGACY_COVERAGE_PROMPT])
    if scope not in histories and legacy in histories:
        histories[scope]=deepcopy(histories[legacy])
        histories[scope]['migrated_from_scope']=legacy
        log('搜索断点兼容',f"沿用已完成{len(histories[scope].get('done',{}))}/{len(jobs)}个任务，不重放已提交搜索")
    progress=histories.setdefault(scope,
        {"version":VERSION,"cursor":0,"done":{},"failed":{},"plans":{},"year_stats":{},"active":None})
    return scope,jobs,progress


def broad_historical_discover(crawler,report,start,end):
    cfg=crawler.cfg;scope,jobs,progress=history_context(crawler,start,end)
    report.update(candidates_new=0,already_saved=0,already_pending=0,planning_previous_calls=0)
    info={"strategy":"historical_balanced","scope":scope,"cursor_start":progress["cursor"],
        "tasks_total":len(jobs),"industry_market_pairs":len(cfg.monitoring.resolved_directions)*len(cfg.monitoring.markets),
        "budget":cfg.collection.max_queries_per_cycle,"paused":False,"planning_rollovers":0}
    report["discovery"]=info
    directions=list(cfg.monitoring.resolved_directions);covered=set();domains_seen=set();retried=set();errors_in_row=0
    def plan_with_budget(method,*args):
        rolls=0
        while True:
            try:return method(*args)
            except ModelBudget:
                if crawler.model.calls<cfg.ollama.max_calls_per_cycle or rolls>=2:raise
                report["planning_previous_calls"]+=crawler.model.calls;crawler.model.calls=0
                info["planning_rollovers"]+=1;rolls+=1
                log("规划续批","本批模型额度已到；保留搜索进度，开启下一本地规划批次，不重放已完成搜索")
    while report["searched"]<cfg.collection.max_queries_per_cycle:
        active=progress.get("active")
        eligible=[int(k) for k,v in progress["failed"].items() if v["attempts"]<3 and k not in retried]
        if active:
            index=active["index"];stage=active["stage"]
        elif eligible:
            index=min(eligible);stage=progress["failed"][str(index)]["stage"];retried.add(str(index))
        elif progress["cursor"]<len(jobs):
            index=progress["cursor"];stage="dated"
        else:
            break
        job=jobs[index];key=str(index);market=job["market"];direction=job["direction"]
        step_started = monotonic(); done_before = len(progress["done"])
        progress["active"]={"index":index,"stage":stage};crawler.persist()
        allowed=[d for d in crawler.sources.domains(market) if not any(d==x or d.endswith("."+x) for x in cfg.search.exclude_domains)]
        domains=allowed
        if job["phase"]=="window" and allowed and cfg.search.rotate_sources:
            domains=[allowed[job["source_turn"]%len(allowed)]]
        group=(directions.index(direction)//2)*2;plan_key=str(group)
        http_attempt=False
        try:
            if cfg.search.website_mode=="restricted" and not domains:
                raise ValueError("当前市场所有网站均被排除，未放宽访问范围")
            if plan_key not in progress["plans"]:
                try:
                    rows=plan_with_budget(crawler.model.coverage_plan,cfg.monitoring,directions[group:group+2],start,end)
                    progress["plans"][plan_key]=rows;crawler.persist()
                except (ValueError,httpx.HTTPError) as error:
                    if not hasattr(crawler.model,"plan"):raise
                    progress["plans"][plan_key]=[]
                    log("规划回退","组合规划未通过，按当前行业/市场单独重规划："+short(error,100))
            choices=[r["query"] for r in progress["plans"][plan_key] if r["market"]==market and r["direction"]==direction][:2]
            if not choices and hasattr(crawler.model,"plan"):
                single=cfg.monitoring.model_copy(update={"markets":[market],"directions":[direction]})
                rows=plan_with_budget(crawler.model.plan,single,1,start,end)
                valid=[dict(r,direction=direction) for r in rows if r["market"]==market]
                progress["plans"][plan_key].extend(valid);crawler.persist()
                choices=[r["query"] for r in valid]
            if not choices:raise ValueError("规划遗漏当前行业/市场，保留任务待重试")
            query=choices[(0 if job["phase"]=="year" else 1)%len(choices)]+" "+str(job["year"])
            left,right=job["range"]
            log("搜索覆盖",f"[{report['searched']+1}/{cfg.collection.max_queries_per_cycle}] {market} | {job['year']}年 | {direction} | {left}至{right} | {stage} | 来源={','.join(domains) or '不限网站'}")
            log("搜索",query);report["searched"]+=1;http_attempt=True
            stats=progress["year_stats"].setdefault(str(job["year"]),{"searches":0,"returned":0,"new":0,"failures":0})
            stats["searches"]+=1
            results=crawler.sources.search(query,market,left,right,domains=domains,strict_dates=stage=="dated")
            report["found"]+=len(results)
            report["queries"].append(dict(job,query=query,domains=domains,stage=stage,returned=len(results)))
            stats["returned"]+=len(results)
            new=saved=pending=0
            for candidate in results:
                identity=source_id(candidate["url"])
                if identity in crawler.dataset.rows:saved+=1;continue
                old=crawler.state["pending"].get(identity,{})
                if old:pending+=1
                else:new+=1
                candidate=dict(old,**candidate)
                candidate.update(collection_scope=crawler.scope,search_range=job["range"],search_year=job["year"],
                    discovery_scope=scope,search_stage=stage)
                crawler.queue(candidate)
            report["candidates_new"]+=new;report["already_saved"]+=saved;report["already_pending"]+=pending
            stats["new"]+=new;covered.add(direction);domains_seen.update(domains);errors_in_row=0
            log("搜索结果",f"返回{len(results)}条 | 新增{new} | 已保存忽略{saved} | 已有候选{pending}")
            if stage=="dated" and not results and cfg.search.undated_fallback:
                progress["active"]={"index":index,"stage":"undated"}
                log("日期回退","严格日期检索为空，单独保留宽检索任务；来源日期仍须由正文确认")
            else:
                progress["done"][key]={"year":job["year"],"range":job["range"],"at":now()}
                progress["failed"].pop(key,None);progress["active"]=None
                progress["cursor"]=max(progress["cursor"],index+1)
        except (ValueError,httpx.HTTPError,ModelBudget) as error:
            message=short(error,220);old=progress["failed"].get(key,{})
            progress["failed"][key]={"attempts":old.get("attempts",0)+1,"stage":stage,"reason":message,"at":now()}
            report["errors"].append({"phase":"discovery","url":None,"year":job["year"],"reason":message})
            errors_in_row+=1;retried.add(key);progress["active"]=None
            if http_attempt:
                stats["failures"]=stats.get("failures",0)+1
            progress["cursor"]=max(progress["cursor"],index+1)
            log("发现待复核",message)
            if errors_in_row>=3 or any(x in message for x in ["HTTP 401","HTTP 402","HTTP 403","HTTP 429","HTTP 432"]):
                info["paused"]=True;info["pause_reason"]="search_or_planning_errors";crawler.persist();break
        crawler.persist()
        tracker = getattr(crawler, "_progress", None)
        if tracker:
            tracker.phase("历史搜索", len(progress["done"]), len(jobs) - len(progress["done"]),
                monotonic() - step_started, units=len(progress["done"]) - done_before)
    complete=len(progress["done"])==len(jobs)
    info.update(cursor_end=progress["cursor"],tasks_done=len(progress["done"]),tasks_remaining=len(jobs)-len(progress["done"]),
        failed_tasks=len(progress["failed"]),complete=complete,year_stats=deepcopy(progress["year_stats"]),
        directions=sorted(covered),domains=sorted(domains_seen))
    if not complete and report["searched"]<cfg.collection.max_queries_per_cycle and not info["paused"]:
        info.update(paused=True,pause_reason="failed_tasks_need_review")
    progress["complete"]=complete;crawler.persist()
    log("历史发现",f"本轮搜索{report['searched']}次 | 年份覆盖={','.join(sorted(progress['year_stats'])) or '尚无'} | 搜索任务{len(progress['done'])}/{len(jobs)} | {'计划已遍历' if complete else '仍未完成，断点已保存'}")
