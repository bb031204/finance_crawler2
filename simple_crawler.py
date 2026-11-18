"""Simple Qwen-led crawler: search -> decide -> read -> one paragraph per URL."""
import argparse
import json
import re
import time
from contextlib import nullcontext
from copy import deepcopy
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal
import yaml
import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator, field_validator
from finance_agent.core import ROOT, Lock, bounded, digest, dump, now
from finance_agent.progress import log, short
from finance_agent.safety import canonical, FetchError
from qwen_agent import Qwen, ModelBudget, OutputError, ModelServiceError, PLAN_PROMPT
from web_sources import Sources,usable_page_cache
from discovery import broad_discover
from search_topics import ALL_DIRECTIONS

from record_files import Dataset, FIELDS, MARKETS, source_id
from record_quality import finalize_record,quarantine_invalid_publication_records,prepare_record_audit,verified_record
from record_time import with_time_information
from date_evidence import trusted_publication_day,VALIDATION_VERSION
from run_summary import build_summary, error_reason, log_summary, SaveCounter, REASONS
from collection_progress import CollectionProgress
from collection_scheduling import fair_pending,bucket,retry_unchanged,set_retry_guard
from analysis_metadata import public_analysis
from interface_schema import DataInterfaceConfig,MarketInformationConfig,RawContentConfig
from crawler_control import RunControl,PauseRequested,AlreadyPaused

# 失败候选停试上限：确定性失败快速停止自动重试，瞬时失败保留更多机会。
FAILURE_DEFER_LIMITS = {"permanent": 2, "model": 3, "transient": 6}


def failure_class(reason_code, error_text=""):
    """Map a failure to its retry class; permanent failures repeat identically each run."""
    if reason_code == "body_error" and "max_text_chars" in error_text:
        return "permanent"  # 正文超上限：配置不变则每次重试必然重演
    if reason_code in {"body_error", "pdf_error", "access_blocked", "validation_error"}:
        return "permanent"  # robots/安全拦截、PDF依赖、范围校验：条件不变则失败
    if reason_code == "model_error":
        return "model"  # temp=0+固定seed下输出确定；代码/提示词变更后重试才有意义
    return "transient"  # 网络/服务/存储波动：保留多次机会


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

class Monitoring(Strict):
    markets: list[str] = Field(default_factory=lambda: ["CN", "US"], min_length=1)
    directions: list[str] | Literal["all"] = "all"
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    securities: list[str] = Field(default_factory=list)

    @field_validator("directions", mode="before")
    @classmethod
    def normalize_directions(cls, value):
        if isinstance(value, str) and value.strip().lower() == "all":
            return "all"
        if isinstance(value, list) and any(isinstance(item,str) and item.strip().lower()=="all" for item in value):
            if len(value) != 1:
                raise ValueError("全方向直接填写all；自选方向时不要混写all")
            return "all"
        return value

    @property
    def all_directions(self):
        return self.directions == "all"

    @property
    def resolved_directions(self):
        return list(ALL_DIRECTIONS) if self.all_directions else self.directions

class Collection(Strict):
    mode: str = "historical"
    start_date: str | None = None
    end_date: str | None = None
    poll_interval_seconds: int = Field(default=300, ge=30)
    lookback_minutes: int = Field(default=120, ge=5)
    max_queries_per_cycle: int = Field(default=2, ge=1, le=200)
    max_candidates_per_query: int = Field(default=10, ge=1, le=20)
    max_documents_per_cycle: int = Field(default=5, ge=1, le=30)

    @field_validator("start_date","end_date",mode="before")
    @classmethod
    def normalize_config_date(cls,value):
        if value is None:return None
        if isinstance(value,date):return value.isoformat()
        if isinstance(value,str):
            match=re.fullmatch(r"\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s*",value)
            if match:return date(int(match[1]),int(match[2]),int(match[3])).isoformat()
        return value

class SecEdgar(Strict):
    tickers: list[str] = Field(default_factory=list)
    ciks: list[str] = Field(default_factory=list)
    forms: list[str] = Field(default_factory=lambda: ["8-K", "10-Q", "10-K", "6-K", "20-F", "40-F"])
    include_amendments: bool = True
    user_agent: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
    page_size: int = Field(default=100, ge=1, le=1000)
    max_pages_per_round: int = Field(default=5, ge=1, le=200)
    max_requests_per_round: int = Field(default=10, ge=1, le=200)
    include_exhibits: bool = True
    max_exhibits_per_filing: int = Field(default=3, ge=1, le=10)

    @field_validator("tickers", mode="before")
    @classmethod
    def normalize_tickers(cls, values):
        if not isinstance(values, list): raise ValueError("SEC tickers填写列表")
        result = []
        for value in values:
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,14}", value.strip()):
                raise ValueError("SEC ticker格式不正确，例如AAPL或BRK-B")
            result.append(value.strip().upper())
        return list(dict.fromkeys(result))

    @field_validator("ciks", mode="before")
    @classmethod
    def normalize_ciks(cls, values):
        if not isinstance(values, list): raise ValueError("SEC ciks填写列表")
        result = []
        for value in values:
            text = str(value).strip()
            if not re.fullmatch(r"\d{1,10}", text) or int(text) == 0: raise ValueError("SEC CIK须为1至10位非零数字")
            result.append(text.zfill(10))
        return list(dict.fromkeys(result))

    @field_validator("forms")
    @classmethod
    def validate_forms(cls, values):
        if not values or any(not re.fullmatch(r"[A-Z0-9][A-Z0-9 /\-]{0,29}", value) for value in values):
            raise ValueError("SEC forms须填写有效申报类型列表，例如8-K、10-Q、10-K")
        return list(dict.fromkeys(values))


class Search(Strict):
    discovery_mode: Literal["search", "direct", "hybrid"] = "search"
    sec_edgar: SecEdgar = Field(default_factory=SecEdgar)
    provider: str = "tavily"
    strategy: str = "focused"
    date_window_days: int = Field(default=31, ge=0, le=366)
    rotate_sources: bool = True
    complete_history: bool = False
    undated_fallback: bool = True
    history_refinement_passes: int = Field(default=2, ge=1, le=4)
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
    service_max_attempts: int = Field(default=3, ge=1, le=5)
    service_retry_base_seconds: float = Field(default=1, ge=0, le=10)

class Fetch(Strict):
    proxy_url: str | None = None
    allow_fake_ip: bool = False
    use_crawl4ai: bool = True
    allow_pdf: bool = True
    respect_robots: bool = True
    delete_raw_after_extract: Literal[True] = True
    timeout_seconds: int = Field(default=30, ge=5, le=120)
    max_bytes: int = 20971520
    max_redirects: int = 3
    max_text_chars: int = Field(default=60000, ge=1000, le=200000)
    access_cooldown_seconds: int = Field(default=21600, ge=60, le=604800)
    robots_cache_seconds: int = Field(default=21600, ge=60, le=86400)

class Config(Strict):
    data_interface: DataInterfaceConfig = Field(default_factory=DataInterfaceConfig)
    market_information: MarketInformationConfig = Field(default_factory=MarketInformationConfig)
    raw_content: RawContentConfig = Field(default_factory=RawContentConfig)
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
        if self.search.strategy not in {"focused", "broad"}:
            raise ValueError("search.strategy只能为focused或broad")
        if self.search.provider not in {"tavily", "rss"} or self.search.website_mode not in {"restricted", "unrestricted"}:
            raise ValueError("搜索来源或网站模式不正确")
        if self.search.discovery_mode != "direct" and self.search.provider == "rss" and not self.search.rss_feeds:
            raise ValueError("RSS模式需填写rss_feeds")
        if self.search.discovery_mode in {"direct", "hybrid"}:
            if "US" not in self.monitoring.markets or (self.search.discovery_mode == "direct" and self.monitoring.markets != ["US"]):
                raise ValueError("直接目录目前仅支持US；CN+US请使用hybrid，CN继续搜索")
            agent = self.search.sec_edgar.user_agent.get_secret_value()
            if len(agent) > 250 or not re.search(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", agent) or any(ch in agent for ch in "\r\n"):
                raise ValueError("SEC user_agent须包含项目名称和真实联系邮箱；配置内容已隐藏")
            if self.search.website_mode == "restricted" and not any("sec.gov" == d or "sec.gov".endswith("." + d) for d in self.search.market_domains.get("US", [])):
                raise ValueError("SEC直接发现需在US网站范围中允许sec.gov（覆盖www.sec.gov及data.sec.gov）")
            if any("sec.gov" == d or "sec.gov".endswith("." + d) for d in self.search.exclude_domains):
                raise ValueError("SEC直接发现与排除sec.gov冲突，请调整来源范围")
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
    directory=bounded(root/cfg.data_interface.directory,root)
    if directory==root.resolve() or any(part.startswith('.') for part in Path(cfg.data_interface.directory).parts):
        raise ValueError('结构化目录须为项目内普通子目录')
    return cfg




def collection_scope_digest(cfg):
    """Scope digest shared by Crawler and reconcile_state; keep both callers identical."""
    scope_inputs = [VALIDATION_VERSION, cfg.monitoring.model_dump(), cfg.collection.start_date, cfg.collection.end_date,
        cfg.search.website_mode, cfg.search.market_domains, cfg.search.exclude_domains]
    if cfg.search.discovery_mode != "search":
        from sec_discovery import selection
        scope_inputs.append([cfg.search.discovery_mode, selection(cfg.search.sec_edgar)])
    return digest(scope_inputs)


def control_config_digest(cfg):
    value=cfg.model_dump(mode='json')
    # The new default extends the old plan by appending tasks, never reindexes
    # committed queries. Keep existing paused descriptors compatible.
    if value['search'].get('history_refinement_passes')==2:
        value['search'].pop('history_refinement_passes')
    return digest(value)


class Crawler:
    def __init__(self, cfg, root=ROOT, sources=None, model=None):
        self.cfg, self.root = cfg, root
        self.sources = sources or Sources(cfg, root)
        self.model = model or Qwen(cfg.ollama, root, query_limit=cfg.collection.max_queries_per_cycle, industry_settings=cfg.market_information.industry_dictionary,market_min_confidence=cfg.market_information.scope_min_confidence)
        self.state_path = root / ".runtime/simple/state.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {"records": {}, "pending": {}, "plans": {}}
        self.state.setdefault("deferred", {})
        self.dataset = Dataset(root, cfg.output_dir)
        from structured_export import ProjectionScheduler
        self.projection=ProjectionScheduler(root,cfg.output_dir,cfg.data_interface)
        from raw_cleanup import cleanup_transients
        cleanup=cleanup_transients(root,cfg.raw_content.pending_cache_ttl_hours)
        if cleanup['deleted'] or cleanup['failed']:log('原文缓存',f"TTL/提交清理{cleanup['deleted']}条 | 失败{cleanup['failed']}条 | 不重写历史正式JSON")
        self.scope = collection_scope_digest(cfg)
        self._publication_days={}
        self._progress = None
        self._save_counter = None
        self.control = None
        from source_scheduling import SourceAccess
        self.source_access=SourceAccess(root,cfg.fetch,cfg.search.sec_edgar.user_agent.get_secret_value())

    def close(self):
        self.sources.close()
        self.model.close()

    def attach_control(self,control):
        self.control=control
        self.model.pause_check=control.check
        self.sources.pause_check=control.check
        http=getattr(self.sources,'http',None)
        if http is not None:http.pause_check=control.check

    def pause_checkpoint(self):
        if self.control:self.control.check()

    def persist(self):
        dump(self.state_path, self.state,compact=True)

    def screen_candidate(self,candidate,start,end,publication=None,page=None):
        payload={key:candidate[key] for key in ['url','title','snippet','origin_market']}
        payload['collection_context']={'mode':self.cfg.collection.mode,'date_range':[start,end],
            'published_at':publication.get('time') if publication else None,
            'judgment_basis':'information_at_source_publication_not_today'}
        if page is not None:
            payload['body_excerpt']=page['text'][:900]
        return self.model.screen(payload,self.cfg.monitoring)

    def capture_access(self):
        try:
            self.source_access.capture(getattr(self.sources,'http',None))
            self._access_warning=False
        except OSError:
            if not getattr(self,'_access_warning',False):
                log('来源缓存暂缓','来源规则已在本进程生效，调度缓存写入失败，将重试；不覆盖文章状态')
            self._access_warning=True

    def source_pause(self,identity,candidate,force=False):
        pause=self.source_access.paused(candidate['url'])
        if not pause:return None
        if not force:
            if identity in self.dataset.rows:return None
            previous=self.state['records'].get(identity,{})
            if previous.get('status')=='saved' and previous.get('record_id') in self.dataset.rows:return None
            try:
                path=bounded(self.root/candidate.get('page_path','.runtime/simple/pages/'+identity+'.json'),self.root)
                if path.is_file():
                    page=json.loads(path.read_text(encoding='utf-8'))
                    if usable_page_cache(page,candidate['url'],self.cfg.search.sec_edgar):return None
            except (OSError,ValueError):pass
        return pause

    def window(self):
        if self.cfg.collection.mode == "historical":
            return self.cfg.collection.start_date, self.cfg.collection.end_date
        end = datetime.now(timezone.utc)
        return (end-timedelta(minutes=self.cfg.collection.lookback_minutes, hours=14)).date().isoformat(), (end+timedelta(hours=14)).date().isoformat()

    def queue(self, candidate):
        if self.cfg.search.discovery_mode != "search":
            from sec_discovery import filing_url
            candidate = dict(candidate, url=filing_url(candidate["url"]))
        identity = source_id(candidate["url"])
        if identity in self.state["deferred"]:
            return  # 已停试的失败候选不自动重新入队；修复后用--retry-deferred恢复
        candidate.setdefault("collection_scope",self.scope)
        self._publication_days.pop(identity,None)
        previous=self.state["pending"].get(identity,{})
        if "first_seen_at" in previous:
            first_seen=previous["first_seen_at"]
        elif identity in self.dataset.rows:
            first_seen=self.dataset.rows[identity][1].get("时间信息",{}).get("first_seen_at")
        else:first_seen=now()
        candidate.setdefault("first_seen_at",first_seen)
        self.state["pending"][identity] = candidate

    def pending_scope(self,identity,candidate):
        if candidate.get("origin_market") not in self.cfg.monitoring.markets:return False,"market"
        if not self.sources.allowed(candidate["url"],candidate["origin_market"]):return False,"website"
        scope=candidate.get("collection_scope")
        if scope and scope!=self.scope:return False,"old_scope"
        if identity not in self._publication_days:
            try:
                path=bounded(self.root/candidate.get("page_path",".runtime/simple/pages/"+identity+".json"),self.root)
            except ValueError:return False,"unsafe_cache_path"
            try:
                page=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None
                self._publication_days[identity]=trusted_publication_day(page) if page and usable_page_cache(page,candidate['url'],self.cfg.search.sec_edgar) else None
            except (OSError,ValueError,TypeError):self._publication_days[identity]=None
        day=self._publication_days[identity];start,end=self.window()
        if day and not start<=day<=end:return False,"publication_out_of_range"
        if not scope and not day:return False,"legacy_scope_unknown"
        return True,None

    def active_pending(self):
        return {i:c for i,c in self.state["pending"].items() if self.pending_scope(i,c)[0]}

    def restore_changed_failures(self):
        """Only guarded failures whose evidence or relevant policy changed."""
        restored=0
        for identity,item in list(self.state['deferred'].items()):
            if (not item.get('retry_guard') or not self.pending_scope(identity,item)[0]
                    or retry_unchanged(self.root,item,self.cfg,self.model)):
                continue
            candidate={k:v for k,v in item.items() if not k.startswith('deferred_')}
            candidate['retry_history']=(candidate.get('retry_history',[])+[candidate['retry_guard']])[-3:]
            candidate.pop('retry_guard',None);candidate['failure_attempts']=0
            self.state['pending'][identity]=candidate;self.state['deferred'].pop(identity)
            self.state['records'][identity]={'status':'pending','reason':'证据或相关处理条件已变化，恢复尝试','at':now()}
            restored+=1
        return restored

    def saved_pages(self):
        seen=set()
        for path in (self.root/".runtime/simple/pages").glob("*.json"):
            page=json.loads(path.read_text(encoding="utf-8"))
            origin=next((market for market in self.cfg.monitoring.markets if self.sources.allowed(page["url"],market)),None)
            if not origin:continue
            identity=source_id(page["url"]);seen.add(identity)
            yield {"url":page["url"],"title":page["title"],"snippet":page["text"][:450],"origin_market":origin,
                "page_path":str(path.relative_to(self.root)),"reprocess":True}
        # Successful source caches may have been removed; fetch the original URL again.
        for identity,(market,row) in self.dataset.rows.items():
            if identity in seen:continue
            origin=next((key for key in self.cfg.monitoring.markets if self.sources.allowed(row["url"],key)),None)
            if not origin:continue
            yield {"url":row["url"],"title":row.get('title') or " / ".join(row["关键词"][:4]),"snippet":row["主要内容"][:450],
                "title_is_source":bool(row.get('title')),"origin_market":origin,"reprocess":True}

    def search_discover(self, report, start, end):
        self.pause_checkpoint()
        if self.cfg.search.provider == "tavily":
            import os
            if not self.cfg.search.api_key.get_secret_value().strip() and not os.environ.get("TAVILY_API_KEY", "").strip():
                raise ValueError("请在config.yaml填写search.api_key")
        if self.cfg.search.strategy == "broad" and self.cfg.search.provider == "tavily":
            broad_discover(self, report, start, end)
        else:
            plan_key = digest([self.cfg.monitoring.model_dump(), start, end, self.cfg.collection.max_queries_per_cycle, PLAN_PROMPT])
            cached = self.state["plans"].get(plan_key)
            if self.cfg.search.provider=='rss':
                # A feed is not a query engine. Pull once per market, then let
                # the normal publication/useful/evidence checks do their job.
                plan_key=digest(['rss',self.cfg.search.rss_feeds,self.cfg.monitoring.markets,start,end])
                queries=[{'market':market,'query':'RSS订阅'} for market in self.cfg.monitoring.markets]
            elif cached and time.time() - cached["at"] < 1800:
                queries = cached["queries"]
                log("规划", "复用本地搜索计划")
            else:
                queries = self.model.plan(self.cfg.monitoring, self.cfg.collection.max_queries_per_cycle, start, end)
                self.state["plans"][plan_key] = {"at": time.time(), "queries": queries}
                self.persist()
            for query_number, item in enumerate(queries, 1):
                self.pause_checkpoint()
                query_key=digest([plan_key,item,start,end])
                if self.control and query_key in self.control.progress.get('query_done',[]):
                    log('搜索续跑','跳过本次任务已提交的搜索请求');continue
                query_started = time.monotonic()
                log("搜索", f"{item['market']} | {item['query']} | 最多{self.cfg.collection.max_candidates_per_query}条")
                results = self.sources.search(item["query"], item["market"], start, end)
                report["queries"].append({"market": item["market"], "query": item["query"], "returned": len(results)})
                report["searched"] += 1
                report["found"] += len(results)
                log("搜索", f"返回{len(results)}条，Qwen将逐条判断用途")
                for candidate in results:
                    identity = source_id(candidate["url"])
                    key = "already_saved" if identity in self.dataset.rows else "already_pending" if identity in self.state["pending"] or identity in self.state["deferred"] else "candidates_new"
                    report[key] += 1
                    self.queue(candidate)
                self.persist()
                if self.control:self.control.progress.setdefault('query_done',[]).append(query_key)
                if self._progress:
                    self._progress.phase("本轮搜索", query_number, len(queries) - query_number, time.monotonic() - query_started)

    def run(self, resume=False, reprocess=False, force=False, exclude_ids=None, batch_number=None):
        self.dataset.recover_pending()
        if (not getattr(self.model,'model_digest',None) and callable(getattr(self.model,'check',None))
                and any(c.get('retry_guard',{}).get('kind')=='model' and self.pending_scope(i,c)[0]
                        for i,c in list(self.state['pending'].items())+list(self.state['deferred'].items()))):
            # One cheap tags lookup detects changed weights under the same name.
            # An unavailable service leaves guards intact, never an article failure.
            try:self.model.check()
            except ModelServiceError:log('模型版本暂缓','暂时无法核对本机权重版本；保留模型失败等待条件，其他可用任务继续')
        recovered_transactions=self.dataset.recovery_count;self.dataset.recovery_count=0
        baseline_ids = set(self.dataset.rows)
        save_counter = self._save_counter or SaveCounter(baseline_ids)
        report = {"started_at": now(), "searched": 0, "found": 0, "considered": 0,
                  "downloaded": 0, "raw_deleted": 0, "saved": 0, "skipped": 0, "pending": 0, "errors": [], "budget_pause": False, "attempted_ids": [], "budget_kind": None, "budget_item": None, "deferred_stopped": 0, "service_pause": False}
        report.update(item_results=[], candidates_new=0, already_saved=0, already_pending=0, cleanup_errors=0, page_cache_hits=0,
            screen_calls_avoided=0,transaction_recovered=recovered_transactions)
        report.update(preflight_checked=0,preflight_passed=0,preflight_rejected=0,preflight_deferred=0)
        report.update(source_paused_ids=[],source_paused_hosts={},source_policy_probes=0)
        report.update(retry_paused_ids=[],retry_paused_reasons={},retry_reactivated=self.restore_changed_failures())
        usage_before=dict(getattr(self.model,'usage',{}))
        def outcome(identity, status, reason_code=None, record_id=None):
            item = {"source_id": identity, "outcome": status, "reason_code": reason_code}
            if record_id is not None: item["record_id"] = record_id
            report["item_results"].append(item)
            if self.control and reason_code!='call_budget' and status!='source_paused':
                self.control.completed(identity)
            if status in {"rejected", "skipped", "deferred_stopped"}:
                detail = self.state["records"].get(identity, {}).get("reason", "") if status in {"rejected", "deferred_stopped"} else ""
                label = "剔除" if status == "rejected" else "跳过" if status == "skipped" else "停试"
                log(label, f"{identity} | {REASONS.get(reason_code, reason_code)}" + (" | " + short(detail, 140) if detail else ""))
            if status=='rejected':
                from raw_cleanup import cleanup_candidate
                try:
                    result=cleanup_candidate(self.root,identity,candidate)
                    if result and result['raw_cleanup_status']=='failed':log('原文清理错误','拒绝记录缓存清理失败，已记审计待重试')
                except (OSError,ValueError,KeyError):log('原文清理错误','拒绝记录缓存归属未核实或删除失败，留待TTL检查')
            if self._progress:
                self._progress.finish_item(identity, terminal=reason_code != "call_budget", sample=status not in {"skipped","source_paused"},
                    deferred=status in {"deferred","source_paused"} and reason_code != "call_budget")
                self._progress.emit()
        self.model.calls = 0
        self.model.cache_hits = 0
        # Historical conversion is explicit (--upgrade-records), never an incidental collection write.
        migration = dict(migrated=0,global_synced=0,time_upgraded=0,analysis_upgraded=0,backup=None)
        report["quarantined_dates"]=[]
        report["maintenance"] = {key: migration.get(key, 0) for key in ["migrated", "global_synced", "time_upgraded", "analysis_upgraded"]}
        report["maintenance"]["quarantined"] = len(report["quarantined_dates"])
        report['maintenance'].update(summary_cleaned=0,summary_quarantined=0)
        # Existing formal records are read-only unless a dedicated maintenance command is requested.
        if report["quarantined_dates"]:log("日期隔离",f"{len(report['quarantined_dates'])}条旧记录日期依据不合格；已备份并转待复核")
        if migration["migrated"]:
            log("迁移", f"已将{migration['migrated']}条记录转为独立JSON，旧JSONL已备份")
        if migration.get("global_synced"):
            log("GLOBAL同步",f"{migration['global_synced']}条公共信息已同步至CN、US")
        if migration.get("time_upgraded"):
            log("时间信息",f"已为{migration['time_upgraded']}条旧记录补充时间信息；未知历史时刻保留为空")
        if migration.get("analysis_upgraded"):
            log("分析字段", f"已备份并升级{migration['analysis_upgraded']}条记录，恢复可核验元数据；缺乏依据的字段留空")
        start, end = self.window()
        report.update(range=[start, end], markets=self.cfg.monitoring.markets, directions=self.cfg.monitoring.directions, queries=[])
        if batch_number in {None, 1}:
            direction_label = f"全部行业（{len(self.cfg.monitoring.resolved_directions)}个搜索主题轮转）" if self.cfg.monitoring.all_directions else ",".join(self.cfg.monitoring.directions)
            log("范围", f"{start}至{end} | 市场={','.join(self.cfg.monitoring.markets)} | 方向={direction_label} | 股票/公司={','.join(self.cfg.monitoring.securities) or '按方向查询'}")
            for market in self.cfg.monitoring.markets:
                log("网站", f"{market} | " + (", ".join(self.sources.domains(market)) or "不限制网站"))
            log("配置", f"Qwen={self.cfg.ollama.model} | Ollama={self.cfg.ollama.base_url} | 输出={self.root/self.cfg.output_dir}")
            log("发现模式", self.cfg.search.discovery_mode + " | search=搜索；direct=SEC目录；hybrid=两者结合")
            if self.cfg.search.discovery_mode != "search":
                sec = self.cfg.search.sec_edgar
                targets = ",".join(sec.tickers + sec.ciks) or "日期范围内全部SEC申报主体（不等同全部上市美股）"
                log("SEC范围", f"{targets} | 类型={','.join(sec.forms)} | 每页{sec.page_size}条 | 每轮最多{sec.max_pages_per_round}页/{sec.max_requests_per_round}次目录请求")
        if reprocess:
            for candidate in self.saved_pages():
                self.queue(candidate)
        elif not resume:
            if self._progress:
                self._progress.set_activity("发现候选/读取目录")
                self._progress.set_discovery(False)
            mode = self.cfg.search.discovery_mode
            channels=getattr(self,'_discovery_channels',{})
            if mode in {"search", "hybrid"}:
                prior=channels.get('search')
                if (prior and prior.get('complete') and prior.get('strategy')=='historical_balanced'
                        and self.cfg.collection.mode=='historical'):
                    from history_discovery import historical_jobs
                    if prior.get('tasks_total')!=len(historical_jobs(self.cfg,start,end)):
                        prior=None  # Newly appended coverage tasks; keep the committed prefix.
                if prior and (prior.get('complete') or prior.get('paused')):
                    report['discovery']=deepcopy(prior)
                else:
                    try:
                        self.search_discover(report, start, end)
                    except ModelServiceError as error:
                        report['service_pause']=True
                        report['errors'].append({'phase':'planning','url':None,'reason':str(error),'reason_code':'model_service_error'})
                        discovery=report.setdefault('discovery',{})
                        progress=self.state.get('discovery',{}).get(discovery.get('scope'),{})
                        remaining=max(1,discovery.get('tasks_total',1)-len(progress.get('done',{})))
                        discovery.update(complete=False,paused=True,tasks_remaining=remaining,pause_reason='model_service_unavailable')
                        log('Ollama暂停',str(error)+'；保留当前搜索规划断点')
                    except (ValueError,httpx.HTTPError,ModelBudget) as error:
                        if mode!='hybrid':raise
                        report['errors'].append({'phase':'discovery','url':None,'reason':short(error,220)})
                        report['discovery']={'complete':False,'paused':True,'tasks_remaining':1,'pause_reason':'search_unavailable'}
                        log('搜索渠道暂停',short(error,160)+'；继续可用的SEC渠道，不重复请求失败搜索')
                    report.setdefault('discovery',{'complete':True,'paused':False,'tasks_remaining':0})
                channels['search']=deepcopy(report['discovery'])
            if mode in {"direct", "hybrid"}:
                from sec_discovery import direct_discover, combine_discovery
                prior=channels.get('direct')
                if prior and (prior.get('complete') or prior.get('paused')):
                    report['direct_discovery']=deepcopy(prior)
                    for key in ['requests','pages','rows','candidates_new','already_saved','already_pending','already_rejected']:
                        report['direct_discovery'][key]=0
                else:direct_discover(self, report, start, end)
                channels['direct']=deepcopy(report['direct_discovery'])
                combine_discovery(report, mode)
            self._discovery_channels=channels
            if self.control:self.control.progress['channels']=deepcopy(channels)
        if self._progress:
            discovery = report.get("discovery", {})
            if not resume or reprocess:
                self._progress.set_discovery(discovery.get("complete", True), discovery.get("paused", False))
            elif batch_number in {None, 1}:
                self._progress.set_discovery(True)
            self._progress.set_queue(set(self.active_pending()) - set(exclude_ids or []))
            self._progress.set_activity("处理当前队列")
            self._progress.emit()
        existing_filing_urls = set()
        self.capture_access()
        if self.cfg.search.discovery_mode != "search":
            from sec_discovery import filing_url
            existing_filing_urls = {filing_url(row["url"]) for _, row in self.dataset.rows.values()}
        active=self.active_pending()
        scheduling=self.state.setdefault('scheduling',{}).setdefault(self.scope,{'last_bucket':None})
        eligible={i:c for i,c in active.items() if not exclude_ids or i not in exclude_ids}
        report['queue_scheduling']={'strategy':'year_source_type_round_robin','eligible':len(eligible),
            'buckets':len({bucket(c) for c in eligible.values()}),
            'long_documents':sum(bucket(c)[3]=='long' for c in eligible.values())}
        if batch_number in {None,1}:
            log('队列调度',f"年份/来源/长短文公平轮转 | 可调度{len(eligible)}条 | 长文{report['queue_scheduling']['long_documents']}条；长文保留份额，不挤占全部短新闻处理")
        for identity, candidate, turn in fair_pending(eligible,scheduling.get('last_bucket')):
            self.pause_checkpoint()
            if report.get('service_pause'):
                break
            if exclude_ids and identity in exclude_ids:
                continue
            pause=self.source_pause(identity,candidate,force)
            if pause:
                scheduling['last_bucket']=turn
                report['source_paused_ids'].append(identity)
                host=pause['host'];report['source_paused_hosts'][host]=report['source_paused_hosts'].get(host,0)+1
                if self._progress:self._progress.finish_item(identity,deferred=True,sample=False)
                continue
            if report["considered"] >= self.cfg.collection.max_documents_per_cycle:
                break
            previous = self.state["records"].get(identity, {})
            saved_identity=identity if identity in self.dataset.rows else None
            alias=previous.get('record_id')
            if (not saved_identity and previous.get('status')=='saved' and alias in self.dataset.rows
                    and previous.get('request_url')==canonical(candidate['url'])
                    and previous.get('resolved_url')==canonical(self.dataset.rows[alias][1]['url'])):
                saved_identity=alias
            already_saved = saved_identity is not None or candidate["url"] in existing_filing_urls
            audit_incomplete=saved_identity is not None and not verified_record(self.root,self.dataset.rows[saved_identity][1])
            if audit_incomplete:
                candidate['reprocess']=True
                log('保存恢复','已有JSON缺少匹配审计，复用正文/模型缓存补齐，不按重复记录跳过')
            if not force and not reprocess and not candidate.get("reprocess") and (already_saved or (previous.get("status") == "rejected" and previous.get("scope") == self.scope)):
                if saved_identity is not None:
                    try:
                        path=bounded(self.root/candidate.get('page_path','.runtime/simple/pages/'+identity+'.json'),self.root)
                        page=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None
                        row=self.dataset.rows[saved_identity][1]
                        audit_path=self.root/'.runtime/simple/audit'/(saved_identity+'.json')
                        evidence=json.loads(audit_path.read_text(encoding='utf-8')) if audit_path.is_file() else {}
                        if page and usable_page_cache(page,candidate['url'],self.cfg.search.sec_edgar) and evidence.get('record_hash')==digest(row) and evidence.get('source_body_hash')==digest(page['text'].encode('utf-8')):
                            audit=finalize_record(self.root,self.dataset,row,{},page,candidate,self.model,path,self.cfg.fetch.delete_raw_after_extract)
                            report['raw_deleted']+=len(audit['raw_deleted'])-len(evidence.get('raw_deleted',[]))
                    except (OSError,ValueError) as error:
                        report['cleanup_errors']+=1;log('原文保留','已提交记录的后续清理未完成：'+short(error,100))
                self.state["pending"].pop(identity, None)
                report["skipped"] += 1
                outcome(identity, "skipped", "duplicate_saved" if already_saved else "previously_rejected")
                scheduling['last_bucket']=turn
                continue
            if (not force and not reprocess and not candidate.get('reprocess')
                    and retry_unchanged(self.root,candidate,self.cfg,self.model)):
                scheduling['last_bucket']=turn
                report['retry_paused_ids'].append(identity)
                kind=candidate['retry_guard']['kind']
                report['retry_paused_reasons'][kind]=report['retry_paused_reasons'].get(kind,0)+1
                if self._progress:self._progress.finish_item(identity,deferred=True,sample=False)
                continue
            if candidate.get('retry_guard'):
                changed=not retry_unchanged(self.root,candidate,self.cfg,self.model)
                candidate['retry_history']=(candidate.get('retry_history',[])+[candidate.pop('retry_guard')])[-3:]
                if changed:candidate['failure_attempts']=0
            scheduling['last_bucket']=turn
            report["considered"] += 1
            report["attempted_ids"].append(identity)
            log("候选", f"[{report['considered']}/{self.cfg.collection.max_documents_per_cycle}] {short(candidate['title'], 75)} | {candidate['url']}")
            if self._progress: self._progress.begin_item(identity)
            stage = "validate"
            try:
                if candidate["origin_market"] not in self.cfg.monitoring.markets or not self.sources.allowed(candidate["url"], candidate["origin_market"]):
                    raise ValueError("候选不在当前启用市场或网站范围")
                page_path = bounded(self.root / candidate.get('page_path','.runtime/simple/pages/'+identity+'.json'),self.root)
                cached_page=None
                if page_path.is_file() and (not force or (resume and previous.get('budget_resume'))):
                    try:
                        value=json.loads(page_path.read_text(encoding='utf-8'))
                        if usable_page_cache(value,candidate['url'],self.cfg.search.sec_edgar):cached_page=value
                    except (OSError,ValueError):pass
                official_filing=False
                if candidate.get('discovery_source')=='sec_edgar' and candidate.get('sec_filing'):
                    from sec_discovery import filing_identity,matches
                    filing=candidate['sec_filing']
                    official_filing=(matches(filing,self.cfg.search.sec_edgar,start,end) and
                        canonical(candidate['url'])==filing_identity(filing['cik'],filing['accession']))
                stage = "screen"
                delayed_screen=False
                if candidate.get('reprocess') or cached_page is not None or official_filing:
                    decision={'useful':True,'reason':'直接由Qwen正文判断用途；复用可读缓存/核对过的SEC目录，省略重复标题初筛'}
                    report['screen_calls_avoided']+=1
                elif callable(getattr(self.model,'preflight',None)):
                    delayed_screen=True
                    decision={'useful':True,'reason':'先检查正文、来源日期和范围，通过后再做相关性初筛与提炼'}
                else:decision=self.screen_candidate(candidate,start,end)
                log("前置待检" if delayed_screen else "筛选", ("进入检查" if delayed_screen else "保留" if decision["useful"] else "跳过") + " | " + decision["reason"])
                if not decision["useful"]:
                    self.state["records"][identity] = {"status": "rejected", "reason": decision["reason"], "scope": self.scope, "at": now()}
                    self.state["pending"].pop(identity, None)
                    report["skipped"] += 1
                    outcome(identity, "rejected", "screen_rejected")
                    self.persist()
                    continue
                stage = "fetch"
                if cached_page is not None:
                    page = cached_page
                    report["page_cache_hits"] += 1
                    log("正文缓存", f"复用{len(page['text'])}字符")
                else:
                    page = self.sources.fetch(candidate)
                    self.capture_access()
                    report["downloaded"] += 1
                    dump(page_path, page)
                    from raw_cleanup import register_page
                    register_page(self.root,page_path,page,identity)
                candidate["page_path"] = str(page_path.relative_to(self.root))
                page=dict(page,collection_context={'mode':self.cfg.collection.mode,'date_range':[start,end],
                    'judgment_basis':'information_at_source_publication_not_today'})
                self.persist()
                resolved_id=source_id(canonical(page['url']))
                if (not force and not reprocess and not candidate.get('reprocess') and resolved_id in self.dataset.rows
                        and verified_record(self.root,self.dataset.rows[resolved_id][1])):
                    self.state['records'][identity]={'status':'saved','record_id':resolved_id,
                        'request_url':canonical(candidate['url']),'resolved_url':canonical(page['url']),'at':now()}
                    self.state['pending'].pop(identity,None);report['skipped']+=1
                    outcome(identity,'skipped','duplicate_saved');self.persist()
                    log('前置检查','重定向已对应正式记录，跳过日期判断与正文提炼')
                    continue
                if len(page['text'])>self.cfg.fetch.max_text_chars:
                    raise ValueError('正文缓存超过当前max_text_chars上限；不进行日期判断或正文提炼')
                published_day=trusted_publication_day(page)
                if published_day and not start<=published_day<=end:
                    self._publication_days[identity]=published_day
                    self.state["records"][identity]={"status":"inactive","reason":"正文明确发布元数据在当前日期范围之外；原任务保留", "at":now()}
                    report["skipped"]+=1;self.persist()
                    outcome(identity, "rejected", "date_out_of_range")
                    log("日期隔离",published_day+"不在当前范围，不对长文调用Qwen")
                    continue
                prepared_article=None
                if callable(getattr(self.model,'preflight',None)):
                    stage='preflight';report['preflight_checked']+=1
                    calls_before=self.model.calls
                    prepared_article=self.model.preflight(page,self.cfg.monitoring,start,end)
                    status=prepared_article['status']
                    if status!='ready':
                        rejected=status=='rejected'
                        report['preflight_rejected' if rejected else 'preflight_deferred']+=1
                        self.state['records'][identity]={'status':'rejected' if rejected else 'pending',
                            'reason':prepared_article['reason'],'scope':self.scope,'at':now()}
                        if rejected:self.state['pending'].pop(identity,None);report['skipped']+=1
                        outcome(identity,'rejected' if rejected else 'deferred',prepared_article['reason_code'])
                        log('前置检查',('跳过本轮' if rejected else '保留待复核')+' | '+prepared_article['reason']+' | 正文提炼0次')
                        self.persist();continue
                    report['preflight_passed']+=1
                    log('前置检查',f"通过 | 来源日期={prepared_article['publication']['time']} | 预计正文{len(prepared_article['chunks'])}片段 | 日期检查调用{self.model.calls-calls_before}次")
                    if delayed_screen:
                        if len(prepared_article['chunks'])==1:
                            report['screen_calls_avoided']+=1
                            log('相关性初筛','单片短文由同次正文提炼判断useful，省略独立模型调用；日期和事实校验继续执行')
                        else:
                            stage='screen'
                            decision=self.screen_candidate(candidate,start,end,prepared_article['publication'],prepared_article['page'])
                            log('相关性初筛',('保留' if decision['useful'] else '跳过')+' | '+decision['reason'])
                            if not decision['useful']:
                                self.state['records'][identity]={'status':'rejected','reason':decision['reason'],'scope':self.scope,'at':now()}
                                self.state['pending'].pop(identity,None);report['skipped']+=1
                                outcome(identity,'rejected','screen_rejected');self.persist();continue
                    candidate['estimated_read_chunks']=len(prepared_article['chunks'])
                stage = "summarize"
                note = self.model.summarize(page, self.cfg.monitoring,prepared=prepared_article) if prepared_article is not None else self.model.summarize(page,self.cfg.monitoring)
                note['impact_scope']=[x for x in note.get('impact_scope',[]) if x in self.cfg.market_information.allowed_scopes]
                if not note["useful"]:
                    self.state["records"][identity] = {"status": "rejected", "reason": note["reason"], "scope": self.scope, "at": now()}
                    self.state["pending"].pop(identity, None)
                    report["skipped"] += 1
                    outcome(identity, "rejected", "content_rejected")
                elif not note["time"]:
                    self.state["records"][identity] = {"status": "pending", "reason": note.get("reason","Qwen未确认来源发布日期"), "at": now()}
                    self.state["pending"].pop(identity, None)
                    self.state["pending"][identity] = candidate
                    outcome(identity, "deferred", "publication_unconfirmed")
                    log("待处理", "原始发布日期尚未确认，正文提炼未完成，未写入正式数据")
                elif not start <= note["time"][:10] <= end:
                    self.state["records"][identity] = {"status": "rejected", "reason": "来源日期不在配置范围", "scope": self.scope, "at": now()}
                    self.state["pending"].pop(identity, None)
                    report["skipped"] += 1
                    outcome(identity, "rejected", "date_out_of_range")
                    log("日期", note["time"] + "超出配置范围")
                else:
                    stage = "save"
                    url = canonical(page["url"])
                    raw_title=page.get('source_title',page.get('title'))
                    if 'source_title' not in page and page.get('cleaning')=='PDF' and not candidate.get('title_is_source',True):raw_title=None
                    title=raw_title.strip() if isinstance(raw_title,str) and raw_title.strip() else None
                    row = {"source_id": source_id(url), "url": url, "title":title,"时间": note["time"], "分类": note["category"],
                           "关键词": list(dict.fromkeys(note["keywords"])), "主要内容": note["content"].strip()}
                    row.update(public_analysis(note))
                    row = with_time_information(row,candidate,page)
                    prepared=prepare_record_audit(self.root,row,note,page,candidate,self.model,note['market'])
                    saved_path = self.dataset.save(note["market"], row, note["industry"],audit_update=prepared)
                    try:
                        audit = finalize_record(self.root,self.dataset,row,note,page,candidate,self.model,page_path,self.cfg.fetch.delete_raw_after_extract)
                    except OSError as error:
                        if not verified_record(self.root,row) or not all(json.loads(path.read_text(encoding='utf-8'))==row for path in self.dataset.replicas[row['source_id']].values()):raise
                        audit=json.loads((self.root/'.runtime/simple/audit'/(row['source_id']+'.json')).read_text(encoding='utf-8'))
                        audit.setdefault('cleanup_errors',[]).append({'path':str(page_path.relative_to(self.root)),'reason':str(error)[:160]})
                        log('保存后处理','正式JSON与审计已提交，清理日志待核对；不丢弃合格结果或重复提炼')
                    report["raw_deleted"] += len(audit["raw_deleted"])
                    report["cleanup_errors"] += len(audit["cleanup_errors"])
                    if audit["raw_deleted"]:log("原文清理",f"已校验正式文件与审计，删除{len(audit['raw_deleted'])}个原文缓存")
                    if audit["cleanup_errors"]:log("原文保留",str(audit["cleanup_errors"])[:200])
                    self.state["records"][identity] = {"status": "saved", "reason": "Qwen已整理", "at": now(),
                        'record_id':row['source_id'],'request_url':canonical(candidate['url']),'resolved_url':row['url']}
                    self.state["pending"].pop(identity, None)
                    report["saved"] += 1
                    outcome(identity, "saved", record_id=row["source_id"])
                    added, selected_count = save_counter.record(row["source_id"])
                    log("新增JSON" if added else "更新JSON", f"本次新增累计{selected_count}条（+{1 if added else 0}） | "
                        f"{saved_path.relative_to(self.root)} | {len(row['主要内容'])}字" + ("" if added else " | 已有记录更新，不重复计数"))
                self.persist()
            except ModelBudget as error:
                if stage=='preflight':report['preflight_deferred']+=1
                report["budget_pause"] = True
                report["budget_item"] = identity
                report["budget_kind"] = "calls" if self.model.calls >= self.cfg.ollama.max_calls_per_cycle else "context"
                outcome(identity, "deferred", "call_budget" if report["budget_kind"] == "calls" else "context_budget")
                self.state["records"][identity] = {"status":"pending", "reason":str(error)[:200], "budget_resume":True, "at":now()}
                log("预算", str(error))
                self.persist()
                break
            except ModelServiceError as error:
                # An unavailable shared model is not evidence that this article
                # is invalid. Stop this run instead of exhausting the whole queue.
                if stage=='preflight':report['preflight_deferred']+=1
                candidate['service_failures']=candidate.get('service_failures',0)+1
                self.state['pending'][identity]=candidate
                self.state['records'][identity]={'status':'pending','reason':str(error),'at':now()}
                report['service_pause']=True
                report['errors'].append({'url':candidate['url'],'reason':str(error),'phase':stage,'reason_code':'model_service_error'})
                outcome(identity,'deferred','model_service_error')
                self.persist()
                log('Ollama暂停',str(error)+'；本轮停止模型处理，已完成结果保留')
                break
            except (ValueError, KeyError, httpx.HTTPError, FetchError, OSError) as error:
                if stage=='fetch':self.capture_access()
                restriction=self.source_access.paused(candidate['url']) if stage=='fetch' and isinstance(error,FetchError) and error.status=='blocked' else None
                if restriction:
                    # A source policy is not an article defect. Even the first
                    # observed denial keeps the original failure budget intact.
                    report['source_paused_ids'].append(identity)
                    report['source_policy_probes']+=1
                    host=restriction['host'];report['source_paused_hosts'][host]=report['source_paused_hosts'].get(host,0)+1
                    self.state['records'][identity]={'status':'pending','reason':'来源暂缓：'+restriction['reason'],'at':now()}
                    report['errors'].append({'url':candidate['url'],'reason':str(error)[:220],'phase':stage,'reason_code':'access_blocked'})
                    outcome(identity,'source_paused','source_cooldown')
                    log('来源暂缓',host+' | '+restriction['reason']+'；已记录来源规则，保留候选，不累计文章失败次数')
                    continue
                if stage=='preflight':report['preflight_deferred']+=1
                reason_code = error_reason(error, stage)
                kind = failure_class(reason_code, str(error))
                limit = FAILURE_DEFER_LIMITS.get(kind, 4)
                attempts = int(candidate.get('failure_attempts', 0)) + 1
                try:set_retry_guard(self.root,candidate,self.cfg,self.model,reason_code,error)
                except (OSError,ValueError):pass  # The original article error remains authoritative.
                candidate['failure_attempts'] = attempts
                candidate['failure_kind'] = kind
                candidate['failure_reasons'] = (candidate.get('failure_reasons', []) + [reason_code])[-5:]
                self.state["pending"].pop(identity, None)
                self.state["records"][identity] = {"status": "pending", "reason": str(error)[:200], "at": now()}
                if attempts >= limit:
                    # 停试：保留完整候选与失败历史，不再自动重试；修复后--retry-deferred重新入队。
                    self.state["deferred"][identity] = dict(candidate, deferred_reason=reason_code,
                        deferred_kind=kind, deferred_attempts=attempts, deferred_at=now())
                    self.state["records"][identity] = {"status": "deferred",
                        "reason": f"失败达停试上限（{kind}类{attempts}次）：{str(error)[:140]}", "at": now()}
                    report["deferred_stopped"] += 1
                    outcome(identity, "deferred_stopped", reason_code)
                    report["errors"].append({"url": candidate["url"], "reason": str(error)[:220], "phase": stage, "reason_code": reason_code})
                    log("停试", f"{identity} | {REASONS.get(reason_code, reason_code)}第{attempts}次 | 保留候选与失败历史，不再自动重试")
                else:
                    self.state["pending"][identity] = candidate
                    outcome(identity, "deferred", reason_code)
                    report["errors"].append({"url": candidate["url"], "reason": str(error)[:220], "phase": stage, "reason_code": reason_code})
                    log("错误", f"{identity} | {short(error, 180)} | 保留待复核（{attempts}/{limit}）")
                self.persist()
        self.persist()
        if report['retry_paused_ids']:
            log('条件等待汇总',f"{len(report['retry_paused_ids'])}条证据/相关处理条件未变，暂缓重复提炼或超长下载；不消耗文档和模型额度；可用--force --resume明确重试")
        report['model_usage']={k:round(v-usage_before.get(k,0),3) for k,v in getattr(self.model,'usage',{}).items()}
        if report['source_paused_ids']:
            log('来源暂缓',f"保留{len(report['source_paused_ids'])}条，其中首次访问确认{report['source_policy_probes']}条，其余不占文档/Qwen额度；均不累计文章失败 | {report['source_paused_hosts']}")
        from raw_cleanup import cleanup_transients
        cleanup_transients(self.root,self.cfg.raw_content.pending_cache_ttl_hours,due_only=True)
        self.projection.mark(report['saved']+self.dataset.recovery_count)
        projection=self.projection.flush(force=batch_number is None)
        report['structured_export']=projection
        if projection['status']=='completed':log('Parquet','投影完成 | '+str(projection.get('rows',{})))
        elif projection['status']=='buffered':log('Parquet',f"合并更新 | 待投影{projection['pending_records']}条 | 正常结束时统一提交")
        elif projection['status']=='pending':log('错误','Parquet导出待重试；摘要已保存，原文仍清理，可从JSON/审计重建')
        self.dataset.ensure_dirs()
        report['transaction_recovered']+=self.dataset.recovery_count;self.dataset.recovery_count=0
        active=self.active_pending();inactive=len(self.state["pending"])-len(active)
        report.update(status="partial" if active or report.get("discovery",{}).get("complete") is False else "completed", finished_at=now(), pending=len(active),inactive_pending=inactive,model_calls=self.model.calls+report.get("planning_previous_calls",0),
                      model_cache_hits=self.model.cache_hits, total_records=len(self.dataset.rows))
        report["summary"] = build_summary([report], baseline_ids, self.dataset, active, inactive)
        if self._progress: report["progress"] = self._progress.snapshot()
        dump(self.root / "logs/latest_run.json", report)
        log("批次汇总" if batch_number else "汇总", f"搜索{report['searched']}次，返回{report['found']}条，检查{report['considered']}篇，保存{report['saved']}篇，跳过{report['skipped']}条，待处理{report['pending']}条")
        log("组件", f"Qwen实际调用{report['model_calls']}次，模型缓存{report['model_cache_hits']}次 | 数据共{report['total_records']}篇 | 详情logs/latest_run.json")
        if report["deferred_stopped"]:
            log("停试汇总", f"{report['deferred_stopped']}条候选达到失败上限，保留在state.deferred不再自动重试；修复后可用--retry-deferred重新入队")
        if batch_number is None: log_summary(report["summary"], log)
        return report


    def collect(self, resume=False, reprocess=False, force=False, one_batch=False):
        with CollectionProgress(log) as progress:
            self._progress = progress
            self._save_counter = SaveCounter(self.dataset.rows)
            try:
                return self._collect(resume, reprocess, force, one_batch)
            finally:
                self._progress = None
                self._save_counter = None

    def _collect(self, resume=False, reprocess=False, force=False, one_batch=False):
        """Drain the active scope; optionally continue a finite historical search plan."""
        baseline_ids = set(self.dataset.rows)
        self._sec_cycle_scope = None
        restored=self.control.progress if self.control else {}
        self._discovery_channels=deepcopy(restored.get('channels',{}))
        seen, continuations, batches = set(restored.get('seen',[])), dict(restored.get('continuations',{})), []
        totals = {key:0 for key in ["searched", "found", "considered", "downloaded", "raw_deleted", "saved", "skipped", "model_calls", "model_cache_hits",'screen_calls_avoided','transaction_recovered','preflight_checked','preflight_passed','preflight_rejected','preflight_deferred','deferred_stopped','source_policy_probes','retry_reactivated']}
        started = now()
        first = True;discover_again=False;last_discovery=None;last_direct=None;last_search=None
        while first or discover_again or any(identity not in seen for identity in self.active_pending()):
            self.pause_checkpoint()
            number = len(batches) + 1
            log("自动批次", f"第{number}批 | 每批最多{self.cfg.collection.max_documents_per_cycle}篇 | " +
                ("执行/续跑历史搜索并处理队列" if (first or discover_again) and not resume and not reprocess else "继续队列，不新增搜索"))
            batch = self.run(resume=resume if first else not discover_again, reprocess=reprocess if first else False,
                             force=force, exclude_ids=seen, batch_number=number)
            discover_again=False
            if "discovery" in batch:last_discovery=batch["discovery"]
            if "direct_discovery" in batch:last_direct=batch["direct_discovery"]
            if "search_discovery" in batch:last_search=batch["search_discovery"]
            batches.append(batch)
            for key in totals:
                totals[key] += batch[key]
            attempted = set(batch["attempted_ids"])
            seen.update(attempted)
            seen.update(batch.get('source_paused_ids',[]))
            seen.update(batch.get('retry_paused_ids',[]))
            if batch.get('service_pause'):
                break
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
                    self._progress.finish_item(identity, deferred=True)
                    log("待复核", reason)
            elif batch["budget_kind"] == "context":
                log("待复核", "当前记录上下文不足，本次保留并继续其他记录，不反复重试")
            if self.control:self.control.progress['continuations']=dict(continuations)
            first = False
            if one_batch:
                break
            # A malfunctioning worker must not cause an endless outer loop.
            if not attempted and batch["pending"] and any(identity not in seen for identity in self.active_pending()):
                log("停止", "本批没有处理进展，已保留队列")
                break
            if (not resume and not reprocess and self.cfg.search.complete_history and last_discovery
                    and not last_discovery.get("complete",True) and not last_discovery.get("paused")
                    and not any(i not in seen for i in self.active_pending())):
                discover_again=True
        remaining = set(self.active_pending())
        projection=self.projection.flush(force=True)
        deferred = len(remaining & seen)
        unprocessed = len(remaining - seen)
        report = dict(batches[0])
        report['structured_export']=projection
        report.update(totals)
        report['retry_paused_ids']=list(dict.fromkeys(i for batch in batches for i in batch.get('retry_paused_ids',[])))
        report['model_usage']={key:round(sum(batch.get('model_usage',{}).get(key,0) for batch in batches),3)
            for key in ['requests','prompt_tokens','completion_tokens','seconds']}
        discovery_incomplete=bool(last_discovery and not last_discovery.get("complete",True))
        report.update(started_at=started, finished_at=now(), status="partial" if remaining or discovery_incomplete else "completed",
            batches=len(batches), pending=len(remaining), deferred=deferred, unprocessed=unprocessed,
            total_records=len(self.dataset.rows), errors=[error for batch in batches for error in batch["errors"]],
            budget_pause=any(batch["budget_pause"] for batch in batches), budget_continuations=continuations,
            service_pause=any(batch.get('service_pause',False) for batch in batches),
            attempted_ids=list(dict.fromkeys(identity for batch in batches for identity in batch["attempted_ids"])),
            batch_reports=batches, one_batch=one_batch)
        report.update(inactive_pending=len(self.state["pending"])-len(remaining),discovery_incomplete=discovery_incomplete,
            queries=[q for batch in batches for q in batch.get("queries",[])])
        if last_discovery:report["discovery"]=last_discovery
        if last_direct:
            report["direct_discovery"] = dict(last_direct)
            for key in ["requests", "pages", "rows", "candidates_new", "already_saved", "already_pending", "already_rejected"]:
                report["direct_discovery"][key] = sum(batch.get("direct_discovery", {}).get(key, 0) for batch in batches)
        if last_search: report["search_discovery"] = last_search
        report["inactive_reasons"]=dict(Counter(self.pending_scope(i,c)[1] for i,c in self.state["pending"].items() if not self.pending_scope(i,c)[0]))
        start,end=self.window();coverage={}
        for year in range(int(start[:4]),int(end[:4])+1):
            stats=(last_discovery or {}).get("year_stats",{}).get(str(year),{})
            direct_stats=(last_discovery or {}).get("direct_year_stats",{}).get(str(year),{})
            coverage[str(year)]={"searches":stats.get("searches",0),"returned":stats.get("returned",0),
                "catalog_rows":direct_stats.get("catalog_rows",0),"catalog_candidates":direct_stats.get("candidates",0),
                "records":{m:sum(start<=r["时间"][:10]<=end and r["时间"].startswith(str(year)) for r in self.dataset.market_rows(m)) for m in self.cfg.monitoring.markets}}
        report["year_coverage"]=coverage
        report["years_without_records"]=[year for year,entry in coverage.items() if not any(entry['records'].values())]
        report["quality_status"]="yearly_gaps" if report["years_without_records"] else "records_in_each_year"
        report["item_results"] = [item for batch in batches for item in batch["item_results"]]
        report['source_paused_ids']=list(dict.fromkeys(i for batch in batches for i in batch.get('source_paused_ids',[]) if i in remaining))
        report['source_paused_hosts']=dict(Counter(urlsplit(self.state['pending'][i]['url']).netloc.lower() for i in report['source_paused_ids']))
        report["summary"] = build_summary(batches, baseline_ids, self.dataset, remaining, report["inactive_pending"])
        for key in ["candidates_new", "already_saved", "already_pending", "cleanup_errors", "page_cache_hits"]:
            report[key] = sum(batch.get(key, 0) for batch in batches)
        report.pop("budget_item", None)
        report.pop("budget_kind", None)
        self._progress.set_queue(remaining - seen)
        for identity in remaining & seen:
            self._progress.finish_item(identity, deferred=True, sample=False)
        self._progress.finalize()
        report["progress"] = self._progress.snapshot()
        dump(self.root / "logs/latest_run.json", report)
        log("最终汇总", f"{report['status']} | 自动处理{len(batches)}批 | {start}至{end} | 市场={','.join(self.cfg.monitoring.markets)}")
        log_summary(report["summary"], log)
        if last_direct:
            direct = report["direct_discovery"]
            log("SEC目录总计", f"请求{direct['requests']}次，处理{direct['pages']}页，日期/类型匹配{direct['rows']}条；新增候选{direct['candidates_new']}条、"
                f"已保存{direct['already_saved']}条、已有候选{direct['already_pending']}条、此前剔除{direct['already_rejected']}条；"
                + ("目录计划已完成" if direct['complete'] else "目录计划未完成"))
        log("市场可用记录",f"CN={len(self.dataset.market_rows('CN'))}，US={len(self.dataset.market_rows('US'))}（均包含GLOBAL，跨市场合并按source_id去重）")
        for year,entry in coverage.items():
            log("年度覆盖",f"{year} | 累计搜索{entry['searches']}次，返回{entry['returned']}条 | 目录匹配{entry['catalog_rows']}条、候选{entry['catalog_candidates']}条 | 当前范围JSON={entry['records']}")
        if discovery_incomplete:
            remaining_label = last_discovery.get('tasks_remaining')
            remaining_label = "待解析公司目录" if remaining_label is None else str(remaining_label) + "个发现任务"
            log("历史发现未完成",f"尚余{remaining_label}（公司分卷可能继续展开）；下次main.py从断点继续，不能将本轮结束当作历史穷尽")
        if report["years_without_records"]:
            log("数据缺口","以下年份仍无正式JSON："+",".join(report['years_without_records'])+"；搜索任务结束不等于这些年份没有信息")
        log("队列隔离",f"当前范围外保留{report['inactive_pending']}条，不占用本次模型/文档预算 | {report['inactive_reasons']}")
        log("队列", f"未尝试{unprocessed}条，待复核/来源暂缓{deferred}条，累计停试{totals['deferred_stopped']}条 | 数据共{len(self.dataset.rows)}篇")
        log("组件总计", f"Qwen实际调用{totals['model_calls']}次，缓存{totals['model_cache_hits']}次 | 详情logs/latest_run.json")
        return report


def print_history_preview(cfg):
    if cfg.collection.mode != "historical": raise ValueError("历史计划预览需historical模式")
    result = {"range": [cfg.collection.start_date, cfg.collection.end_date], "markets": cfg.monitoring.markets,
              "discovery_mode": cfg.search.discovery_mode, "complete_history": cfg.search.complete_history}
    if cfg.search.discovery_mode in {"search", "hybrid"}:
        if cfg.search.strategy != "broad" or cfg.search.provider != "tavily":
            raise ValueError("搜索计划预览需broad+tavily；目录单独预览可使用direct模式")
        from history_discovery import historical_jobs
        jobs = historical_jobs(cfg, cfg.collection.start_date, cfg.collection.end_date)
        result.update(tasks=len(jobs), phases=dict(Counter(j['phase'] for j in jobs)),
            years=dict(Counter(j['year'] for j in jobs)), max_http_queries_per_round=cfg.collection.max_queries_per_cycle,
            first_queries=jobs[:len(set(j['year'] for j in jobs))*len(cfg.monitoring.markets)])
    if cfg.search.discovery_mode in {"direct", "hybrid"}:
        from sec_discovery import preview
        result["sec_edgar"] = preview(cfg, cfg.collection.start_date, cfg.collection.end_date)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def controlled_collection(cfg,args):
    options={key:getattr(args,key) for key in ['resume','reprocess_saved','force','once']}
    # Compare parsed settings, so comments and line endings do not break resume.
    # SecretStr fields stay masked; no credentials are stored in the descriptor.
    config_hash=control_config_digest(cfg)
    session=nullcontext(None) if args.doctor else RunControl(ROOT,args.config,config_hash,options,args.continue_paused)
    with session as control:
        if control:
            for key,value in control.options.items():setattr(args,key,value)
        crawler=Crawler(cfg)
        if control:crawler.attach_control(control)
        try:
            if args.retry_deferred:
                deferred=crawler.state.get('deferred',{});requeued=0
                for identity,item in list(deferred.items()):
                    if args.retry_deferred_kind and item.get('deferred_kind')!=args.retry_deferred_kind:continue
                    if identity in crawler.dataset.rows:
                        deferred.pop(identity,None);continue
                    crawler.state['pending'][identity]={k:v for k,v in item.items() if not k.startswith(('deferred_','failure_')) and k!='retry_guard'}
                    crawler.state['records'][identity]={'status':'pending','reason':'用户修复环境/配置后重新入队（--retry-deferred）','at':now()}
                    deferred.pop(identity,None);requeued+=1
                if requeued:crawler.persist()
                log('停试恢复',f'重新入队{requeued}条停试候选，失败计数清零，随后正常运行')
            if args.retry_failed_searches:
                if cfg.search.discovery_mode=='direct':raise ValueError('SEC目录失败直接再次运行main.py重试；--retry-failed-searches仅用于搜索通道')
                if cfg.collection.mode!='historical' or cfg.search.strategy!='broad':raise ValueError('失败搜索重试需historical+broad模式')
                from history_discovery import history_context
                _,_,progress=history_context(crawler,*crawler.window())
                for failure in progress['failed'].values():failure['attempts']=0
                crawler.persist()
            if args.doctor:
                print(json.dumps(crawler.model.check(),ensure_ascii=False));return 0
            while True:
                control.check()
                crawler.collect(args.resume,args.reprocess_saved,args.force,one_batch=args.once)
                control.check()
                if cfg.collection.mode!='realtime' or args.once or args.resume or args.reprocess_saved:return 0
                control.reset_round()
                log('等待',f'{cfg.collection.poll_interval_seconds}秒后开始下一轮，可运行pause_crawler.py暂停')
                control.wait(cfg.collection.poll_interval_seconds)
        except (PauseRequested,KeyboardInterrupt) as error:
            crawler.persist()
            crawler.projection.flush(force=True)
            control.status='paused'
            log('采集暂停','已保存搜索/目录/候选断点和可用缓存，释放写入锁后可运行resume_crawler.py恢复')
            return 130 if isinstance(error,KeyboardInterrupt) else 0
        finally:
            crawler.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Qwen主导的信息采集：筛选、阅读、段落摘要，每篇JSON包含来源与特征可用时间")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--validate-config", action="store_true")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument('--export-parquet',action='store_true',help='只读正式JSON及匹配审计，重建7张Parquet；不调用模型或搜索')
    parser.add_argument('--cleanup-raw',action='store_true',help='持锁清理已提交/拒绝/超过TTL的精确归属原文，清理旧错误全文日志')
    parser.add_argument("--upgrade-records", action="store_true", help="备份并升级已有JSON分析字段，不搜索、不调用Qwen")
    parser.add_argument("--repair-mappings", action="store_true", help="备份并修复旧记录的证券代码/配对元数据，不采集或调用Qwen")
    parser.add_argument('--clean-summaries',action='store_true',help='持锁、备份后清理已有摘要的确定套话，不联网、不调用Qwen')
    parser.add_argument("--preview-history",action="store_true",help="只预览有限历史搜索计划，不联网、不调用模型")
    parser.add_argument("--retry-failed-searches",action="store_true",help="修复网络/配置后重置当前历史搜索失败计数并正常运行")
    parser.add_argument("--retry-deferred",action="store_true",help="把达到失败停试上限的候选重新入队（修复配置/代码/网络后使用），随后正常运行")
    parser.add_argument("--retry-deferred-kind",choices=["permanent","model","transient"],help="只重新入队指定失败类别的停试候选；默认全部")
    parser.add_argument("--reconcile-state",action="store_true",help="对账state.json与正式库：默认只读预览分歧；配合--apply才修复state并从archive恢复缺失JSON")
    parser.add_argument("--apply",action="store_true",help="与--reconcile-state连用：实际执行修复（写state、恢复文件、重建Parquet）")
    parser.add_argument("--requeue-lost",action="store_true",help="与--reconcile-state --apply连用：把无备份丢失记录的URL重新入队采集")
    parser.add_argument("--once", action="store_true", help="只处理一批；不加此参数时自动继续后续批次")
    parser.add_argument("--resume", "--extract-only", dest="resume", action="store_true")
    parser.add_argument("--reprocess-saved", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument('--continue-paused',action='store_true',help='恢复已暂停任务的原运行方式（包含未完成的搜索），由resume_crawler.py使用')
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.validate_config:
            print(json.dumps(cfg.model_dump(), ensure_ascii=False, indent=2))
            return 0
        if args.preview_history:
            return print_history_preview(cfg)
        with Lock(ROOT):
            if args.export_parquet:
                from structured_export import export_or_defer
                result=export_or_defer(ROOT,cfg.output_dir,cfg.data_interface.directory)
                log('Parquet',json.dumps(result,ensure_ascii=False));return 0 if result['status']=='completed' else 1
            if args.cleanup_raw:
                from raw_cleanup import cleanup_transients
                result=cleanup_transients(ROOT,cfg.raw_content.pending_cache_ttl_hours)
                log('原文清理',f"已删除{result['deleted']}条 | 失败{result['failed']}条 | 旧错误日志脱敏{result['sanitized_error_logs']}条")
                return 1 if result['failed'] else 0
            if args.clean_summaries:
                from summary_maintenance import clean_saved_summaries
                result=clean_saved_summaries(Dataset(ROOT,cfg.output_dir))
                dump(ROOT/'logs/latest_summary_cleanup.json',result)
                log('摘要去噪',json.dumps(result,ensure_ascii=False))
                return 0
            if args.repair_mappings:
                from mapping_maintenance import repair_saved_mappings
                result=repair_saved_mappings(Dataset(ROOT,cfg.output_dir))
                result['finished_at']=now()
                dump(ROOT/'logs/mapping_repair.json',result)
                log('映射修复',json.dumps(result,ensure_ascii=False))
                return 0
            if args.upgrade_records:
                class NoModel:
                    def industry(self, row): return "Unspecified"
                dataset = Dataset(ROOT, cfg.output_dir)
                migration = dataset.migrate(NoModel())
                result = dict(migration, unique_records=len(dataset.rows),
                              fields=sorted(FIELDS), finished_at=now())
                dump(ROOT/"logs/analysis_schema_upgrade.json", result)
                log("分析字段升级", json.dumps(result, ensure_ascii=False))
                return 0
            if args.reconcile_state:
                from reconcile_state import reconcile_state
                result = reconcile_state(ROOT, cfg, apply=args.apply, requeue_lost=args.requeue_lost)
                dump(ROOT/"logs/state_reconcile.json", result)
                log("state对账", json.dumps({key: result[key] for key in ["apply", "library_records", "state_saved",
                    "fixed_state_count", "restored_count", "lost_count", "conflict_count", "audit_orphan_count"]}, ensure_ascii=False))
                return 0 if result["status"] == "ok" else 1
            return controlled_collection(cfg,args)
    except AlreadyPaused as error:
        log('采集暂停',str(error));return 0
    except KeyboardInterrupt:
        log("停止", "已停止，保存的数据与待处理任务已保留。")
        return 130
    except Exception as error:
        log("运行失败", str(error))
        return 1
