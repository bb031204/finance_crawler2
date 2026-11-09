"""Simple Qwen-led crawler: search -> decide -> read -> one paragraph per URL."""
import argparse
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
import yaml
import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from finance_agent.core import ROOT, Lock, bounded, digest, dump, now
from finance_agent.progress import log, short
from finance_agent.safety import canonical, FetchError
from qwen_agent import Qwen, ModelBudget, OutputError, PLAN_PROMPT
from web_sources import Sources

from record_files import Dataset, FIELDS, MARKETS, source_id

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

class Monitoring(Strict):
    markets: list[str] = Field(default_factory=lambda: ["CN", "US"], min_length=1)
    directions: list[str] = Field(default_factory=lambda: ["银行业", "半导体"])
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    securities: list[str] = Field(default_factory=list)

class Collection(Strict):
    mode: str = "historical"
    start_date: str | None = None
    end_date: str | None = None
    poll_interval_seconds: int = Field(default=300, ge=30)
    lookback_minutes: int = Field(default=120, ge=5)
    max_queries_per_cycle: int = Field(default=2, ge=1, le=8)
    max_candidates_per_query: int = Field(default=10, ge=1, le=20)
    max_documents_per_cycle: int = Field(default=5, ge=1, le=30)

class Search(Strict):
    provider: str = "tavily"
    api_key: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
    topic: str = "news"
    website_mode: str = "restricted"
    market_domains: dict[str, list[str]] = Field(default_factory=dict)
    exclude_domains: list[str] = Field(default_factory=list)
    rss_feeds: list[str] = Field(default_factory=list)

class Model(Strict):
    base_url: str = "http://localhost:11434"
    model: str = "qwen3:8b"
    num_ctx: int = Field(default=4096, ge=4096, le=8192)
    num_predict: int = Field(default=1000, ge=500, le=1600)
    timeout_seconds: int = Field(default=180, ge=10)
    max_calls_per_cycle: int = Field(default=30, ge=1, le=100)

class Fetch(Strict):
    proxy_url: str | None = None
    allow_fake_ip: bool = False
    use_crawl4ai: bool = True
    allow_pdf: bool = True
    respect_robots: bool = True
    timeout_seconds: int = Field(default=30, ge=5, le=120)
    max_bytes: int = 20971520
    max_redirects: int = 3
    max_text_chars: int = Field(default=60000, ge=1000, le=200000)

class Config(Strict):
    monitoring: Monitoring = Field(default_factory=Monitoring)
    collection: Collection
    search: Search = Field(default_factory=Search)
    ollama: Model = Field(default_factory=Model)
    fetch: Fetch = Field(default_factory=Fetch)
    output_dir: str = "data"

    @model_validator(mode="after")
    def valid(self):
        if set(self.monitoring.markets) - {"CN", "US"} or len(set(self.monitoring.markets)) != len(self.monitoring.markets):
            raise ValueError("markets只填写CN、US，且不能重复")
        if self.collection.mode not in {"historical", "realtime"}:
            raise ValueError("mode只能为historical或realtime")
        if self.collection.mode == "historical":
            if not self.collection.start_date or not self.collection.end_date:
                raise ValueError("历史模式必须填写起止日期")
            if date.fromisoformat(self.collection.start_date) > date.fromisoformat(self.collection.end_date):
                raise ValueError("起始日期晚于结束日期")
        elif self.collection.start_date is not None or self.collection.end_date is not None:
            raise ValueError("实时模式起止日期设为null")
        if self.collection.max_queries_per_cycle < len(self.monitoring.markets):
            raise ValueError("搜索上限至少等于启用市场数，保证每个市场都有查询")
        if self.search.topic not in {"news", "general", "finance"}:
            raise ValueError("search.topic只能为news、general或finance")
        if self.search.provider not in {"tavily", "rss"} or self.search.website_mode not in {"restricted", "unrestricted"}:
            raise ValueError("搜索来源或网站模式不正确")
        if self.search.provider == "rss" and not self.search.rss_feeds:
            raise ValueError("RSS模式需填写rss_feeds")
        if self.search.website_mode == "restricted" and any(not self.search.market_domains.get(m) for m in self.monitoring.markets):
            raise ValueError("受限模式需为每个启用市场填写网站")
        for domain in self.search.exclude_domains + [d for values in self.search.market_domains.values() for d in values]:
            if ":" in domain or "/" in domain or not domain.strip() or "*" in domain:
                raise ValueError("网站范围只填写域名，例如cls.cn")
        if urlsplit(self.ollama.base_url).scheme != "http" or urlsplit(self.ollama.base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Ollama仅使用本机HTTP地址")
        if self.fetch.proxy_url:
            proxy = urlsplit(self.fetch.proxy_url)
            if proxy.scheme != "http" or proxy.hostname not in {"localhost", "127.0.0.1", "::1"} or not proxy.port or proxy.username or proxy.password:
                raise ValueError("proxy_url需为本机HTTP代理")
        if self.fetch.allow_fake_ip and not self.fetch.proxy_url:
            raise ValueError("Fake-IP兼容需要本机代理")
        return self


class UniqueLoader(yaml.SafeLoader):
    pass

def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError("YAML存在重复配置项，请删除重复项；配置内容已隐藏")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result

UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)

def load_config(path=None, root=ROOT):
    path = bounded(path or root / "config.yaml", root)
    try:
        value = yaml.load(path.read_text(encoding="utf-8-sig"), Loader=UniqueLoader)
    except yaml.YAMLError:
        raise ValueError("YAML格式错误，请检查缩进、冒号和引号；配置内容已隐藏") from None
    cfg = Config.model_validate(value)
    bounded(root / cfg.output_dir, root)
    return cfg




class Crawler:
    def __init__(self, cfg, root=ROOT, sources=None, model=None):
        self.cfg, self.root = cfg, root
        self.sources = sources or Sources(cfg, root)
        self.model = model or Qwen(cfg.ollama, root)
        self.state_path = root / ".runtime/simple/state.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {"records": {}, "pending": {}, "plans": {}}
        self.dataset = Dataset(root, cfg.output_dir)
        self.scope = digest([cfg.monitoring.model_dump(), cfg.collection.start_date, cfg.collection.end_date, cfg.search.website_mode, cfg.search.market_domains, cfg.search.exclude_domains])

    def close(self):
        self.sources.close()
        self.model.close()

    def persist(self):
        dump(self.state_path, self.state)

    def window(self):
        if self.cfg.collection.mode == "historical":
            return self.cfg.collection.start_date, self.cfg.collection.end_date
        end = datetime.now(timezone.utc)
        return (end-timedelta(minutes=self.cfg.collection.lookback_minutes, hours=14)).date().isoformat(), (end+timedelta(hours=14)).date().isoformat()

    def queue(self, candidate):
        identity = source_id(candidate["url"])
        self.state["pending"][identity] = candidate

    def saved_pages(self):
        for path in (self.root / ".runtime/simple/pages").glob("*.json"):
            page = json.loads(path.read_text(encoding="utf-8"))
            yield {"url": page["url"], "title": page["title"], "snippet": page["text"][:450],
                   "origin_market": page["origin_market"], "page_path": str(path.relative_to(self.root))}

    def run(self, resume=False, reprocess=False, force=False, exclude_ids=None, batch_number=None):
        report = {"started_at": now(), "searched": 0, "found": 0, "considered": 0,
                  "downloaded": 0, "saved": 0, "skipped": 0, "pending": 0, "errors": [], "budget_pause": False, "attempted_ids": [], "budget_kind": None, "budget_item": None}
        self.model.calls = 0
        self.model.cache_hits = 0
        migration = self.dataset.migrate(self.model)
        if migration["migrated"]:
            log("迁移", f"已将{migration['migrated']}条记录转为独立JSON，旧JSONL已备份")
        start, end = self.window()
        report.update(range=[start, end], markets=self.cfg.monitoring.markets, directions=self.cfg.monitoring.directions, queries=[])
        if batch_number in {None, 1}:
            log("范围", f"{start}至{end} | 市场={','.join(self.cfg.monitoring.markets)} | 方向={','.join(self.cfg.monitoring.directions)} | 股票/公司={','.join(self.cfg.monitoring.securities) or '按方向查询'}")
            for market in self.cfg.monitoring.markets:
                log("网站", f"{market} | " + (", ".join(self.sources.domains(market)) or "不限制网站"))
            log("配置", f"Qwen={self.cfg.ollama.model} | Ollama={self.cfg.ollama.base_url} | 输出={self.root/self.cfg.output_dir}")
        if reprocess:
            for candidate in self.saved_pages():
                self.queue(candidate)
        elif not resume:
            if self.cfg.search.provider == "tavily":
                import os
                if not self.cfg.search.api_key.get_secret_value().strip() and not os.environ.get("TAVILY_API_KEY", "").strip():
                    raise ValueError("请在config.yaml填写search.api_key")
            plan_key = digest([self.cfg.monitoring.model_dump(), start, end, self.cfg.collection.max_queries_per_cycle, PLAN_PROMPT])
            cached = self.state["plans"].get(plan_key)
            if cached and time.time() - cached["at"] < 1800:
                queries = cached["queries"]
                log("规划", "复用本地搜索计划")
            else:
                queries = self.model.plan(self.cfg.monitoring, self.cfg.collection.max_queries_per_cycle)
                self.state["plans"][plan_key] = {"at": time.time(), "queries": queries}
                self.persist()
            for item in queries:
                log("搜索", f"{item['market']} | {item['query']} | 最多{self.cfg.collection.max_candidates_per_query}条")
                results = self.sources.search(item["query"], item["market"], start, end)
                report["queries"].append({"market": item["market"], "query": item["query"], "returned": len(results)})
                report["searched"] += 1
                report["found"] += len(results)
                log("搜索", f"返回{len(results)}条，Qwen将逐条判断用途")
                for candidate in results:
                    self.queue(candidate)
                self.persist()
        for identity, candidate in list(self.state["pending"].items()):
            if exclude_ids and identity in exclude_ids:
                continue
            if report["considered"] >= self.cfg.collection.max_documents_per_cycle:
                break
            previous = self.state["records"].get(identity, {})
            if not force and not reprocess and (identity in self.dataset.rows or (previous.get("status") == "rejected" and previous.get("scope") == self.scope)):
                self.state["pending"].pop(identity, None)
                report["skipped"] += 1
                continue
            report["considered"] += 1
            report["attempted_ids"].append(identity)
            log("候选", f"[{report['considered']}/{self.cfg.collection.max_documents_per_cycle}] {short(candidate['title'], 75)} | {candidate['url']}")
            try:
                if candidate["origin_market"] not in self.cfg.monitoring.markets or not self.sources.allowed(candidate["url"], candidate["origin_market"]):
                    raise ValueError("候选不在当前启用市场或网站范围")
                decision = self.model.screen({key: candidate[key] for key in ["url", "title", "snippet", "origin_market"]}, self.cfg.monitoring)
                log("筛选", ("保留" if decision["useful"] else "跳过") + " | " + decision["reason"])
                if not decision["useful"]:
                    self.state["records"][identity] = {"status": "rejected", "reason": decision["reason"], "scope": self.scope, "at": now()}
                    self.state["pending"].pop(identity, None)
                    report["skipped"] += 1
                    self.persist()
                    continue
                page_path = bounded(self.root / candidate.get("page_path", ".runtime/simple/pages/" + identity + ".json"), self.root)
                if page_path.exists() and (not force or (resume and previous.get("budget_resume"))):
                    page = json.loads(page_path.read_text(encoding="utf-8"))
                    log("正文缓存", f"复用{len(page['text'])}字符")
                else:
                    page = self.sources.fetch(candidate)
                    report["downloaded"] += 1
                    dump(page_path, page)
                candidate["page_path"] = str(page_path.relative_to(self.root))
                self.persist()
                note = self.model.summarize(page, self.cfg.monitoring)
                if not note["useful"]:
                    self.state["records"][identity] = {"status": "rejected", "reason": note["reason"], "scope": self.scope, "at": now()}
                    self.state["pending"].pop(identity, None)
                    report["skipped"] += 1
                elif not note["time"]:
                    self.state["records"][identity] = {"status": "pending", "reason": "Qwen未确认来源发布日期", "at": now()}
                    self.state["pending"].pop(identity, None)
                    self.state["pending"][identity] = candidate
                    log("待处理", "文章有用，但来源发布日期尚未确认，未写入正式数据")
                elif not start <= note["time"][:10] <= end:
                    self.state["records"][identity] = {"status": "rejected", "reason": "来源日期不在配置范围", "scope": self.scope, "at": now()}
                    self.state["pending"].pop(identity, None)
                    report["skipped"] += 1
                    log("日期", note["time"] + "超出配置范围")
                else:
                    url = canonical(page["url"])
                    row = {"source_id": source_id(url), "url": url, "时间": note["time"], "分类": note["category"],
                           "关键词": list(dict.fromkeys(note["keywords"])), "主要内容": note["content"].strip()}
                    saved_path = self.dataset.save(note["market"], row, note["industry"])
                    self.state["records"][identity] = {"status": "saved", "reason": "Qwen已整理", "at": now()}
                    self.state["pending"].pop(identity, None)
                    report["saved"] += 1
                    log("保存", f"{saved_path.relative_to(self.root)} | {len(row['主要内容'])}字")
                self.persist()
            except ModelBudget as error:
                report["budget_pause"] = True
                report["budget_item"] = identity
                report["budget_kind"] = "calls" if self.model.calls >= self.cfg.ollama.max_calls_per_cycle else "context"
                self.state["records"][identity] = {"status":"pending", "reason":str(error)[:200], "budget_resume":True, "at":now()}
                log("预算", str(error))
                self.persist()
                break
            except (ValueError, KeyError, httpx.HTTPError, FetchError) as error:
                self.state["records"][identity] = {"status": "pending", "reason": str(error)[:200], "at": now()}
                self.state["pending"].pop(identity, None)
                self.state["pending"][identity] = candidate
                report["errors"].append({"url": candidate["url"], "reason": str(error)[:220]})
                log("待处理", short(error, 180))
                self.persist()
        self.persist()
        self.dataset.ensure_dirs()
        report.update(status="partial" if self.state["pending"] else "completed", finished_at=now(), pending=len(self.state["pending"]), model_calls=self.model.calls,
                      model_cache_hits=self.model.cache_hits, total_records=len(self.dataset.rows))
        dump(self.root / "logs/latest_run.json", report)
        log("批次汇总" if batch_number else "汇总", f"搜索{report['searched']}次，返回{report['found']}条，检查{report['considered']}篇，保存{report['saved']}篇，跳过{report['skipped']}条，待处理{report['pending']}条")
        log("组件", f"Qwen实际调用{report['model_calls']}次，模型缓存{report['model_cache_hits']}次 | 数据共{report['total_records']}篇 | 详情logs/latest_run.json")
        return report


    def collect(self, resume=False, reprocess=False, force=False, one_batch=False):
        """Discover once and automatically visit every queued candidate, with bounded continuation."""
        seen, continuations, batches = set(), {}, []
        totals = {key:0 for key in ["searched", "found", "considered", "downloaded", "saved", "skipped", "model_calls", "model_cache_hits"]}
        started = now()
        first = True
        while first or any(identity not in seen for identity in self.state["pending"]):
            number = len(batches) + 1
            log("自动批次", f"第{number}批 | 每批最多{self.cfg.collection.max_documents_per_cycle}篇 | " +
                ("执行搜索并处理队列" if first and not resume and not reprocess else "继续队列，不新增搜索"))
            batch = self.run(resume=resume if first else True, reprocess=reprocess if first else False,
                             force=force, exclude_ids=seen, batch_number=number)
            batches.append(batch)
            for key in totals:
                totals[key] += batch[key]
            attempted = set(batch["attempted_ids"])
            seen.update(attempted)
            if not one_batch and batch["budget_kind"] == "calls":
                identity = batch["budget_item"]
                used = continuations.get(identity, 0)
                if used < 3:
                    continuations[identity] = used + 1
                    seen.discard(identity)
                    log("自动续处理", f"本批Qwen调用上限已到，下一批复用缓存继续长文（{used+1}/3），不重新搜索")
                else:
                    reason = "同一记录已自动跨批续处理3次，仍未完成；保留缓存，继续其他记录"
                    self.state["records"][identity] = {"status":"pending", "reason":reason, "at":now()}
                    self.persist()
                    log("待复核", reason)
            elif batch["budget_kind"] == "context":
                log("待复核", "当前记录上下文不足，本次保留并继续其他记录，不反复重试")
            first = False
            if one_batch:
                break
            # A malfunctioning worker must not cause an endless outer loop.
            if not attempted and batch["pending"] and any(identity not in seen for identity in self.state["pending"]):
                log("停止", "本批没有处理进展，已保留队列")
                break
        remaining = set(self.state["pending"])
        deferred = len(remaining & seen)
        unprocessed = len(remaining - seen)
        report = dict(batches[0])
        report.update(totals)
        report.update(started_at=started, finished_at=now(), status="partial" if remaining else "completed",
            batches=len(batches), pending=len(remaining), deferred=deferred, unprocessed=unprocessed,
            total_records=len(self.dataset.rows), errors=[error for batch in batches for error in batch["errors"]],
            budget_pause=any(batch["budget_pause"] for batch in batches), budget_continuations=continuations,
            attempted_ids=list(dict.fromkeys(identity for batch in batches for identity in batch["attempted_ids"])),
            batch_reports=batches, one_batch=one_batch)
        report.pop("budget_item", None)
        report.pop("budget_kind", None)
        dump(self.root / "logs/latest_run.json", report)
        log("最终汇总", f"自动处理{len(batches)}批，搜索{totals['searched']}次，返回{totals['found']}条，保存{totals['saved']}篇，跳过{totals['skipped']}条")
        log("队列", f"未尝试{unprocessed}条，失败/日期不足等待复核{deferred}条 | 数据共{len(self.dataset.rows)}篇")
        log("组件总计", f"Qwen实际调用{totals['model_calls']}次，缓存{totals['model_cache_hits']}次 | 详情logs/latest_run.json")
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Qwen主导的信息采集：筛选、阅读、段落摘要，每篇输出独立的六字段JSON")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--validate-config", action="store_true")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--once", action="store_true", help="只处理一批；不加此参数时自动继续后续批次")
    parser.add_argument("--resume", "--extract-only", dest="resume", action="store_true")
    parser.add_argument("--reprocess-saved", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.validate_config:
            print(json.dumps(cfg.model_dump(), ensure_ascii=False, indent=2))
            return 0
        with Lock(ROOT):
            crawler = Crawler(cfg)
            try:
                if args.doctor:
                    print(json.dumps(crawler.model.check(), ensure_ascii=False))
                    return 0
                while True:
                    crawler.collect(args.resume, args.reprocess_saved, args.force, one_batch=args.once)
                    if cfg.collection.mode != "realtime" or args.once or args.resume or args.reprocess_saved:
                        return 0
                    log("等待", f"{cfg.collection.poll_interval_seconds}秒后开始下一轮，Ctrl+C停止")
                    time.sleep(cfg.collection.poll_interval_seconds)
            finally:
                crawler.close()
    except KeyboardInterrupt:
        print("已停止，保存的数据与待处理任务已保留。")
        return 130
    except Exception as error:
        print("运行失败：" + str(error))
        return 1
