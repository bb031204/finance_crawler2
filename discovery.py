"""Budgeted rotating discovery; Qwen writes queries, Python controls coverage."""
from datetime import date, timedelta
from copy import deepcopy
from time import monotonic
import httpx
from finance_agent.core import digest
from finance_agent.progress import log, short
from qwen_agent import COVERAGE_PROMPT, ModelBudget, OutputError, LEGACY_COVERAGE_PROMPT, PLANNING_SCOPE_VERSION
from record_files import source_id


def date_windows(start, end, days):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError("搜索起始日期不能晚于结束日期")
    if days == 0:
        return [(start, end)]
    windows = []
    while last >= first:
        left = max(first, last-timedelta(days=days-1))
        windows.append((left.isoformat(), last.isoformat()))
        last = left-timedelta(days=1)
    return windows


def broad_discover(crawler, report, start, end):
    if crawler.cfg.collection.mode=="historical":
        from history_discovery import broad_historical_discover
        return broad_historical_discover(crawler,report,start,end)
    cfg = crawler.cfg
    directions = list(dict.fromkeys(d.strip() for d in cfg.monitoring.resolved_directions if d.strip())) or ["综合金融"]
    markets = cfg.monitoring.markets
    pairs = [(direction, market) for direction in directions for market in markets]
    windows = date_windows(start, end, cfg.search.date_window_days)
    inputs=[cfg.monitoring.model_dump(),directions,start,end,cfg.search.model_dump(exclude={"discovery_mode", "sec_edgar"})]
    scope = digest(inputs+[PLANNING_SCOPE_VERSION])
    histories=crawler.state.setdefault('discovery',{})
    legacy=digest(inputs+[LEGACY_COVERAGE_PROMPT])
    if scope not in histories and legacy in histories:
        histories[scope]=deepcopy(histories[legacy]);histories[scope]['migrated_from_scope']=legacy
    progress = histories.setdefault(scope,{"cursor":0})
    cursor = max(0,int(progress["cursor"]))
    plans, covered_directions, covered_domains = {}, set(), set()
    report.update(candidates_new=0, already_saved=0, already_pending=0,
        discovery={"strategy":"broad", "cursor_start":cursor, "date_windows":len(windows),
            "industry_market_pairs":len(pairs), "budget":cfg.collection.max_queries_per_cycle, "paused":False})
    errors_in_row = 0
    for offset in range(cfg.collection.max_queries_per_cycle):
        step_started = monotonic()
        position = cursor + offset
        pair_index = position % len(pairs)
        turn = position // len(pairs)
        direction, market = pairs[pair_index]
        left, right = windows[turn % len(windows)]
        domains = crawler.sources.domains(market)
        domains = [domain for domain in domains if not any(domain==excluded or domain.endswith("."+excluded) for excluded in cfg.search.exclude_domains)]
        if cfg.search.website_mode == "restricted" and not domains:
            log("搜索跳过", market + "全部来源均被排除")
            progress["cursor"] = position+1; crawler.persist(); continue
        target = [domains[(pair_index//len(markets)+turn//len(windows))%len(domains)]] if domains and cfg.search.rotate_sources else domains
        group_start = (pair_index//len(markets)//2)*2
        if group_start not in plans:
            group = directions[group_start:group_start+2]
            try:
                rows = crawler.model.coverage_plan(cfg.monitoring,group,start,end)
                plans[group_start] = {}
                for row in rows:
                    options=plans[group_start].setdefault((row["direction"],row["market"]),[])
                    if row["query"] not in options:options.append(row["query"])
            except ModelBudget as error:
                report["discovery"]["paused"] = True
                log("搜索规划暂停", str(error) + "；已保存轮转位置，先处理已有候选")
                break
            except (ValueError, httpx.HTTPError) as error:
                plans[group_start] = None
                report["errors"].append({"phase":"planning","url":None,"reason":short(error,220)})
                log("搜索规划失败",short(error,180))
        if plans[group_start] is None:
            progress["cursor"] = position+1; crawler.persist(); continue
        options = plans[group_start].get((direction,market),[])
        if not options:
            try:
                single=cfg.monitoring.model_copy(update={'directions':[direction],'markets':[market]})
                options=[row['query'] for row in crawler.model.plan(single,1,start,end) if row['market']==market]
                if not options:raise ValueError('单任务规划没有返回当前市场的搜索词')
                plans[group_start][(direction,market)]=options
            except ModelBudget as error:
                report['discovery']['paused']=True
                log('搜索规划暂停',str(error)+'；保留当前任务，下次续跑');break
            except (ValueError,httpx.HTTPError) as error:
                report['errors'].append({'phase':'planning','url':None,'reason':short(error,220)})
                log('搜索规划失败',short(error,180));progress['cursor']=position+1;crawler.persist();continue
        query = options[turn % len(options)]
        log("搜索覆盖",f"[{offset+1}/{cfg.collection.max_queries_per_cycle}] {market} | {direction} | {left}至{right} | 来源={','.join(target) or '不限网站'}")
        log("搜索",query)
        try:
            report["searched"] += 1
            results = crawler.sources.search(query,market,left,right,domains=target)
            report["found"] += len(results)
            report["queries"].append({"market":market,"direction":direction,"query":query,"range":[left,right],"domains":target,"returned":len(results)})
            new = saved = pending = 0
            for candidate in results:
                identity = source_id(candidate["url"])
                if identity in crawler.dataset.rows:
                    saved += 1; continue
                if identity in crawler.state["pending"]:
                    pending += 1
                    candidate = dict(crawler.state["pending"][identity],**candidate)
                else:
                    new += 1
                crawler.queue(candidate)
            report["candidates_new"] += new
            report["already_saved"] += saved
            report["already_pending"] += pending
            covered_directions.add(direction);covered_domains.update(target)
            log("搜索结果",f"返回{len(results)}条 | 新增线索{new} | 已保存忽略{saved} | 队列已有{pending}")
            errors_in_row = 0
        except (ValueError,httpx.HTTPError) as error:
            errors_in_row += 1
            message = short(error,220)
            report["queries"].append({"market":market,"direction":direction,"query":query,"range":[left,right],"domains":target,"returned":0,"error":message})
            report["errors"].append({"phase":"search","url":None,"query":query,"reason":message})
            log("搜索失败",message)
            progress["cursor"] = position+1; crawler.persist()
            if errors_in_row >= 3 or any(code in message for code in ["HTTP 401","HTTP 403","HTTP 429","HTTP 432"]):
                report["discovery"]["paused"] = True
                log("搜索暂停","搜索服务连续失败或额度/授权受限，先处理已有队列")
                break
        progress["cursor"] = position+1
        crawler.persist()
        tracker = getattr(crawler, "_progress", None)
        if tracker:
            tracker.phase("本轮搜索", offset + 1, cfg.collection.max_queries_per_cycle - offset - 1,
                monotonic() - step_started)
    report["discovery"].update(cursor_end=progress["cursor"],directions=sorted(covered_directions),domains=sorted(covered_domains))
    log("发现汇总",f"搜索{report['searched']}次，覆盖{len(covered_directions)}个行业、{len(covered_domains)}个来源；新增{report['candidates_new']}条线索。下次从轮转位置{progress['cursor']}继续")
