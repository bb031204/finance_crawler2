"""SEC filing catalogs: resumable, bounded discovery without a search engine or LLM."""
import json
import re
import gzip
from math import ceil
from time import monotonic
from io import BytesIO
from copy import deepcopy
from datetime import date
from urllib.parse import urlsplit, urljoin, parse_qs
import httpx
from finance_agent.core import bounded, digest, dump, now
from finance_agent.progress import log, short
from record_files import source_id

VERSION = "sec_catalog_v2"
ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")


def filing_identity(cik, accession):
    cik = str(int(cik))
    if not 0 < int(cik) < 10**10 or not ACCESSION.fullmatch(accession):
        raise ValueError("SEC申报编号格式异常，保留目录断点")
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}-index.html"


def filing_url(url):
    """Fold SEC document/index/iXBRL links to one accession URL for hybrid deduplication."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or parts.hostname not in {"www.sec.gov", "sec.gov"}: return url
    if parts.path in {"/ix", "/ix.xhtml", "/ixviewer/doc/action"}:
        document = parse_qs(parts.query).get("doc", [None])[0]
        if not document: return url
        parts = urlsplit(urljoin("https://www.sec.gov", document))
        if parts.hostname != "www.sec.gov": return url
    match = re.fullmatch(r"/Archives/edgar/data/(\d+)/(\d{18})/[^?#]+", parts.path)
    if match:
        cik, number = match.groups()
        return filing_identity(cik, number[:10] + "-" + number[10:12] + "-" + number[12:])
    match = re.fullmatch(r"/Archives/edgar/data/(\d+)/(\d{10}-\d{2}-\d{6})(?:-index\.html|\.txt)", parts.path)
    return filing_identity(*match.groups()) if match else url


def quarter_jobs(start, end):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    jobs = []
    # Give each year a chance before visiting the next quarter.
    for quarter in range(1, 5):
        for year in range(first.year, last.year + 1):
            if (year, quarter) < (first.year, (first.month - 1)//3 + 1) or (year, quarter) > (last.year, (last.month - 1)//3 + 1):
                continue
            jobs.append({"kind": "quarter", "year": year, "quarter": quarter,
                         "url": f"https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/master.gz"})
    return jobs


def selected_jobs(ciks):
    return [{"kind": "company", "cik": cik, "url": f"https://data.sec.gov/submissions/CIK{cik}.json"} for cik in ciks]


def resolve_current_ticker(mapping, ticker):
    """Resolve a unique current issuer; class aliases require an actual mapping row."""
    rows=list(mapping.values()) if isinstance(mapping,dict) else []
    choices=[row for row in rows if isinstance(row,dict) and str(row.get('ticker','')).upper()==ticker]
    if not choices and re.fullmatch(r'[A-Z]{1,6}[.\-][A-Z]{1,2}',ticker):
        alias=ticker.replace('.','-')
        choices=[row for row in rows if isinstance(row,dict) and str(row.get('ticker','')).upper().replace('.','-')==alias]
    ciks={str(int(row['cik_str'])).zfill(10) for row in choices}
    if len(ciks)!=1:raise ValueError('SEC当前ticker映射未唯一匹配：'+ticker+'；可改用明确CIK，不会回退为全市场')
    cik=next(iter(ciks))
    if not 0<int(cik)<10**10:raise ValueError('SEC映射返回无效CIK')
    return cik


def selection(sec):
    # Budgets/page size/contact changes do not reset the catalog cursor.
    return {"tickers": sec.tickers, "ciks": sec.ciks, "forms": sec.forms, "include_amendments": sec.include_amendments}


def preview(cfg, start, end):
    sec = cfg.search.sec_edgar
    selected = bool(sec.tickers or sec.ciks)
    jobs = selected_jobs(sec.ciks) if selected else quarter_jobs(start, end)
    return {"source": "sec_edgar", "range": [start, end], "selection": selection(sec),
            "universe": "selected_filers" if selected else "all_filers_in_quarter_indexes",
            "quarter_tasks": None if selected else len(jobs), "requires_ticker_resolution": bool(sec.tickers),
            "historical_company_files": "由SEC公司主目录返回后展开，预览不联网",
            "page_size": sec.page_size, "max_pages_per_round": sec.max_pages_per_round,
            "max_requests_per_round": sec.max_requests_per_round, "first_tasks": jobs[:12]}


def parse_master(raw):
    # SEC's compressed quarterly catalog avoids raising the normal page-download limit.
    # Bound expansion too; never decompress an unbounded gzip response.
    if raw.startswith(b"\x1f\x8b"):
        try:
            with gzip.GzipFile(fileobj=BytesIO(raw)) as stream: raw = stream.read(128 * 1024 * 1024 + 1)
        except (OSError, EOFError) as error: raise ValueError("SEC压缩索引损坏，未推进断点") from None
    if len(raw) > 128 * 1024 * 1024: raise ValueError("SEC目录解压后超过128MB上限，保留任务待复核")
    try: text = raw.decode("utf-8-sig")
    except UnicodeDecodeError: text = raw.decode("latin1")
    lines = text.splitlines()
    header = next((i for i, line in enumerate(lines) if line.strip() == "CIK|Company Name|Form Type|Date Filed|Filename"), None)
    if header is None: raise ValueError("SEC季度索引缺少表头，不能当作空结果完成")
    rows = []
    for line in lines[header + 1:]:
        if not line.strip() or set(line.strip()) == {"-"}: continue
        parts = line.split("|")
        if len(parts) != 5: raise ValueError("SEC季度索引行格式异常，未推进断点")
        cik, name, form, day, filename = parts
        match = re.fullmatch(r"edgar/data/(\d+)/(\d{10}-\d{2}-\d{6})\.txt", filename)
        if not match or int(match[1]) != int(cik): raise ValueError("SEC季度索引路径与CIK不对应")
        date.fromisoformat(day)
        filing_identity(cik, match[2])
        rows.append({"cik": str(int(cik)).zfill(10), "accession": match[2], "company": name,
                     "form": form, "filing_date": day, "primary_document": ""})
    return rows


def parse_submissions(data, cik, company=""):
    if not isinstance(data, dict): raise ValueError("SEC公司目录并非JSON对象")
    if "cik" in data and int(data["cik"]) != int(cik): raise ValueError("SEC返回的公司CIK与请求不一致")
    columns = data.get("filings", {}).get("recent", {}) if "filings" in data else data
    if not isinstance(columns, dict) or not isinstance(columns.get("accessionNumber"), list):
        raise ValueError("SEC公司目录缺少申报列表，不能当作空结果完成")
    count = len(columns["accessionNumber"])
    for key in ["filingDate", "form", "primaryDocument"]:
        if not isinstance(columns.get(key), list) or len(columns[key]) != count:
            raise ValueError("SEC公司目录字段长度不一致，未推进断点")
    rows = []
    for i in range(count):
        accession, day = columns["accessionNumber"][i], columns["filingDate"][i]
        filing_identity(cik, accession); date.fromisoformat(day)
        rows.append({"cik": cik, "accession": accession, "filing_date": day, "form": columns["form"][i],
                     "company": data.get("name", company), "primary_document": columns["primaryDocument"][i]})
    return rows


def matches(row, sec, start, end):
    form = row["form"]
    return start <= row["filing_date"] <= end and (form in sec.forms or
        (sec.include_amendments and form.endswith("/A") and form[:-2] in sec.forms))


def child_jobs(data, cik, start, end):
    children = []
    for item in data.get("filings", {}).get("files", []):
        name = item.get("name", "")
        if not re.fullmatch(r"CIK" + cik + r"-submissions-\d+\.json", name):
            raise ValueError("SEC历史分卷文件名异常，未请求未知地址")
        left, right = item.get("filingFrom"), item.get("filingTo")
        if left and right:
            date.fromisoformat(left); date.fromisoformat(right)
            if right < start or left > end: continue
        children.append({"kind": "history", "cik": cik, "company": data.get("name", ""),
                         "url": "https://data.sec.gov/submissions/" + name})
    return children


def direct_discover(crawler, report, start, end):
    cfg, sec = crawler.cfg, crawler.cfg.search.sec_edgar
    scope = digest([VERSION, start, end, selection(sec), cfg.search.website_mode,
                    cfg.search.market_domains.get("US", []), cfg.search.exclude_domains])
    progress = crawler.state.setdefault("sec_discovery", {}).setdefault(scope,
        {"jobs": None, "cursor": 0, "generation": now(), "pages_scanned": 0, "year_stats": {}})
    # A realtime invocation checks the current catalog again; within one invocation it resumes pages.
    if cfg.collection.mode == "realtime" and getattr(crawler, "_sec_cycle_scope", None) != scope:
        progress.update(jobs=None, cursor=0, generation=now(), pages_scanned=0, year_stats={})
        crawler._sec_cycle_scope = scope
    info = {"source": "sec_edgar", "scope": scope, "requests": 0, "pages": 0, "rows": 0,
            "candidates_new": 0, "already_saved": 0, "already_pending": 0, "already_rejected": 0,
            "paused": False, "complete": False}
    report["direct_discovery"] = info
    existing_filings = {filing_url(row["url"]) for _, row in crawler.dataset.rows.values()}
    cache_root = bounded(crawler.root / ".runtime/simple/sec_catalogs", crawler.root)

    def cached_json(url, parser):
        path = cache_root / (digest([scope, url]) + ".json")
        if path.is_file():
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("generation") == progress["generation"] and cached.get("url") == url:
                return cached["value"]
        if info["requests"] >= sec.max_requests_per_round: return None
        info["requests"] += 1
        raw, _ = crawler.sources.sec_get(url)
        value = parser(raw)
        dump(path, {"url": url, "generation": progress["generation"], "fetched_at": now(), "value": value})
        return value

    try:
        if progress["jobs"] is None:
            ciks = list(sec.ciks)
            if sec.tickers:
                mapping = cached_json("https://www.sec.gov/files/company_tickers.json", lambda raw: json.loads(raw))
                if mapping is not None:
                    if cfg.collection.mode=='historical':
                        log('历史主体提示','ticker仅按SEC当前表解析采集主体，未验证历史代码有效期；历史身份请使用经核验CIK/证券主表，不将当前ticker倒填原文')
                    for ticker in sec.tickers:
                        ciks.append(resolve_current_ticker(mapping,ticker))
                else:
                    ciks = None
            if ciks is not None:
                progress["jobs"] = selected_jobs(sorted(set(ciks))) if (sec.tickers or sec.ciks) else quarter_jobs(start, end)
                crawler.persist()
        jobs = progress["jobs"]
        while jobs is not None and info["pages"] < sec.max_pages_per_round:
            unfinished = [i for i in range(len(jobs)) if not jobs[i].get("done")]
            if not unfinished: break
            index = next((i for i in unfinished if i >= progress["cursor"]), unfinished[0])
            job = jobs[index]
            page_started = monotonic()
            def parse_catalog(raw):
                if job["kind"] == "quarter": rows, children = parse_master(raw), []
                else:
                    data = json.loads(raw)
                    rows = parse_submissions(data, job["cik"], job.get("company", ""))
                    children = child_jobs(data, job["cik"], start, end) if job["kind"] == "company" else []
                return {"rows": [row for row in rows if matches(row, sec, start, end)], "total_rows": len(rows), "children": children}
            catalog = cached_json(job["url"], parse_catalog)
            if catalog is None: break
            known = {j["url"] for j in jobs}
            for child in catalog["children"]:
                if child["url"] not in known: jobs.append(child); known.add(child["url"])
            offset = job.get("offset", 0)
            rows = catalog["rows"][offset:offset + sec.page_size]
            log("SEC目录", f"{job['kind']} | {job.get('cik', str(job.get('year', '')) + 'Q' + str(job.get('quarter', '')))} | "
                f"第{offset//sec.page_size+1}页 | 日期/类型命中{len(catalog['rows'])}/{catalog['total_rows']}条 | 本页{len(rows)}条")
            for row in rows:
                info["rows"] += 1
                year = row["filing_date"][:4]
                stats = progress["year_stats"].setdefault(year, {"catalog_rows": 0, "candidates": 0})
                stats["catalog_rows"] += 1
                url = filing_identity(row["cik"], row["accession"]); identity = source_id(url)
                if not crawler.sources.allowed(url, "US"): raise ValueError("SEC申报正文被当前网站范围排除，未扩大来源权限")
                if url in existing_filings: info["already_saved"] += 1; continue
                prior = crawler.state["records"].get(identity, {})
                if prior.get("status") == "rejected" and prior.get("scope") == crawler.scope:
                    info["already_rejected"] += 1; continue
                old = crawler.state["pending"].get(identity, {})
                info["already_pending" if old else "candidates_new"] += 1
                candidate = dict(old, url=url, title=f"{row['company']} — {row['form']} — {row['filing_date']}",
                    snippet=f"SEC {row['form']} filing. CIK {row['cik']}. Official filing date {row['filing_date']}.",
                    origin_market="US", collection_scope=crawler.scope, discovery_source="sec_edgar", sec_filing=row,
                    search_range=[start, end])
                crawler.queue(candidate); stats["candidates"] += 1
            job["offset"] = offset + len(rows)
            job["done"] = job["offset"] >= len(catalog["rows"])
            job["matched_rows"] = len(catalog["rows"]); job["catalog_rows"] = catalog["total_rows"]
            progress["cursor"] = (index + 1) % len(jobs)
            progress["pages_scanned"] += 1; info["pages"] += 1
            # Queue additions and catalog offsets commit in one state-file replacement.
            crawler.persist()
            tracker = getattr(crawler, "_progress", None)
            if tracker:
                remaining_pages = sum(ceil(max(0, j["matched_rows"] - j.get("offset", 0)) / sec.page_size)
                                      for j in jobs if "matched_rows" in j and not j.get("done"))
                unknown = sum("matched_rows" not in j for j in jobs)
                tracker.phase("SEC目录", progress["pages_scanned"], remaining_pages, monotonic() - page_started,
                    unknown=unknown, catalogs=(sum(bool(j.get("done")) for j in jobs), len(jobs)))
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, httpx.HTTPError) as error:
        info.update(paused=True, pause_reason="sec_catalog_error")
        report["errors"].append({"phase": "sec_discovery", "url": None, "reason": short(error, 220)})
        log("SEC目录暂停", short(error, 180) + "；保留断点，下次main.py重试，未标记目录完成")
    jobs = progress["jobs"]
    info.update(tasks_total=len(jobs) if jobs is not None else None,
        tasks_remaining=sum(not j.get("done") for j in jobs) if jobs is not None else None,
        complete=jobs is not None and all(j.get("done") for j in jobs),
        pages_scanned=progress["pages_scanned"], year_stats=deepcopy(progress["year_stats"]))
    progress["complete"] = info["complete"]; crawler.persist()
    for key in ["candidates_new", "already_saved", "already_pending"]: report[key] += info[key]
    log("SEC发现汇总", f"本轮目录请求{info['requests']}次、处理{info['pages']}页、候选{info['rows']}条；新增线索{info['candidates_new']}条、"
        f"已保存{info['already_saved']}条、队列已有{info['already_pending']}条、此前剔除{info['already_rejected']}条 | "
        + ("所选目录已遍历" if info['complete'] else "目录未完成，断点已保存"))
    return info


def combine_discovery(report, mode):
    """Preserve independent progress and prevent one finished channel hiding another."""
    search = deepcopy(report.get("discovery"))
    direct = report["direct_discovery"]
    if search is not None: report["search_discovery"] = search
    channels={'direct':direct}
    if search is not None:channels['search']=search
    runnable=[key for key,value in channels.items() if not value.get('complete',True) and not value.get('paused')]
    report["discovery"] = {"strategy": mode, "complete": direct["complete"] and (search is None or search.get("complete", True)),
        "paused": not runnable and any(not value.get('complete',True) and value.get('paused') for value in channels.values()),
        'runnable_channels':runnable,'channel_paused':{key:bool(value.get('paused')) for key,value in channels.items()},
        "tasks_remaining": None if direct["tasks_remaining"] is None else direct["tasks_remaining"] + (search or {}).get("tasks_remaining", 0),
        "year_stats": (search or {}).get("year_stats", {}), "direct_year_stats": direct["year_stats"]}
