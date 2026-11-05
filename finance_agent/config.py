import re
from urllib.parse import urlsplit
from datetime import date
from pathlib import Path
from typing import Literal
import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from .core import ROOT, bounded

class Strict(BaseModel):
    model_config=ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

class Monitoring(Strict):
    markets: list[Literal["CN","US"]] = Field(default_factory=lambda:["CN","US"],min_length=1)
    directions: list[str] = Field(default_factory=lambda:["银行业","半导体"],min_length=1)
    direction_aliases: dict[str,list[str]] = Field(default_factory=dict)
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    securities: list[str] = Field(default_factory=list)

class Collection(Strict):
    mode: Literal["historical","realtime"]="historical"
    start_date: str | None=None
    end_date: str | None=None
    poll_interval_seconds: int=Field(default=300,ge=30,le=86400)
    lookback_minutes: int=Field(default=120,ge=5,le=10080)
    max_queries_per_cycle: int=Field(default=2,ge=1,le=10)
    max_candidates_per_query: int=Field(default=10,ge=1,le=20)
    max_candidates_per_batch: int=Field(default=3,ge=1,le=5)
    max_documents_per_cycle: int=Field(default=5,ge=1,le=30)
    max_extractions_per_cycle: int=Field(default=5,ge=1,le=30)
    max_followup_search_rounds: int=Field(default=0,ge=0,le=2)
    max_retries: int=Field(default=2,ge=0,le=3)
    max_pending_candidates: int=Field(default=5000,ge=100,le=50000)
    @model_validator(mode="after")
    def dates(self):
        if self.mode=="historical":
            if not self.start_date or not self.end_date:
                raise ValueError("historical必须提供start_date和end_date")
            if date.fromisoformat(self.start_date)>date.fromisoformat(self.end_date):
                raise ValueError("起始日期晚于结束日期")
        elif self.start_date is not None or self.end_date is not None:
            raise ValueError("realtime模式请将start_date/end_date设为null")
        return self

class Search(Strict):
    provider: Literal["tavily","rss"]="tavily"
    api_key: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
    topic: Literal["news","general","finance"]="news"
    search_depth: Literal["basic","advanced"]="basic"
    website_mode: Literal["unrestricted", "restricted"] | None = None
    market_domains: dict[Literal["CN", "US"], list[str]] = Field(default_factory=dict)
    include_domains: list[str]=Field(default_factory=list)
    exclude_domains: list[str]=Field(default_factory=list)
    rss_feeds: list[str]=Field(default_factory=list)
    planner_cache_seconds: int=Field(default=1800,ge=0,le=86400)

    @staticmethod
    def normalize_domains(domains):
        normalized = []
        for domain in domains:
            domain = domain.strip().rstrip(".").lower()
            try:
                domain = domain.encode("idna").decode("ascii")
            except UnicodeError:
                raise ValueError("网站范围请填写域名，例如cninfo.com.cn") from None
            labels = domain.split(".")
            if len(domain) > 253 or len(labels) < 2 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in labels
            ):
                raise ValueError("网站范围仅接受域名，请去掉协议、路径、端口和通配符")
            if domain not in normalized:
                normalized.append(domain)
        return normalized

    @field_validator("include_domains", "exclude_domains")
    @classmethod
    def validate_domains(cls, domains):
        return cls.normalize_domains(domains)

    @field_validator("market_domains")
    @classmethod
    def validate_market_domains(cls, markets):
        return {market: cls.normalize_domains(domains) for market, domains in markets.items()}

    @model_validator(mode="after")
    def explicit_website_mode(self):
        if self.website_mode is not None and self.include_domains:
            raise ValueError("使用website_mode时请清空旧include_domains，限制网站请填写market_domains")
        return self

    def domains_for(self, market=None, markets=None):
        """None retains compatibility with older configs using include_domains."""
        if self.website_mode is None:
            return self.include_domains[:]
        if self.website_mode == "unrestricted":
            return []
        selected = list(dict.fromkeys(markets if markets is not None else ["CN", "US"]))
        if market in {"CN", "US"}:
            if market not in selected:
                raise ValueError("查询市场未在monitoring.markets启用")
            selected = [market]
        domains = []
        for key in selected:
            for domain in self.market_domains.get(key, []):
                if not any(domain == excluded or domain.endswith("." + excluded)
                           for excluded in self.exclude_domains) and domain not in domains:
                    domains.append(domain)
        if not domains:
            raise ValueError("restricted模式下启用市场的网站列表不能为空或全部被排除")
        return domains

    def allows_url(self, url, market=None, markets=None):
        host = (urlsplit(url).hostname or "").lower()
        if not host or any(host == domain or host.endswith("." + domain)
                           for domain in self.exclude_domains):
            return False
        try:
            domains = self.domains_for(market, markets)
        except ValueError:
            return False
        return not domains or any(host == domain or host.endswith("." + domain) for domain in domains)

class Fetch(Strict):
    use_crawl4ai: bool=True
    proxy_url: str | None = None
    allow_fake_ip: bool = False
    allow_pdf: bool=True
    allow_ocr: bool=False
    ocr_max_pages: int=Field(default=8,ge=1,le=32)
    timeout_seconds: int=Field(default=30,ge=5,le=120)
    max_bytes: int=Field(default=20*1024*1024,ge=1024,le=50*1024*1024)
    max_redirects: int=Field(default=3,ge=0,le=5)
    max_chunks_per_document_cycle: int=Field(default=8,ge=1,le=32)
    respect_robots: bool=True

    @model_validator(mode="after")
    def local_proxy(self):
        if self.proxy_url:
            try:
                proxy = urlsplit(self.proxy_url)
                valid = (proxy.scheme == "http" and proxy.hostname in {"127.0.0.1", "::1", "localhost"}
                         and proxy.port is not None and 1 <= proxy.port <= 65535
                         and not proxy.username and not proxy.password
                         and proxy.path in {"", "/"} and not proxy.query and not proxy.fragment)
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("proxy_url请填写本机HTTP代理地址，例如http://127.0.0.1:7897")
        if self.allow_fake_ip and not self.proxy_url:
            raise ValueError("allow_fake_ip需要显式配置可信的本机proxy_url")
        return self

class Ollama(Strict):
    base_url: str="http://localhost:11434"
    model: str="qwen3:8b"
    num_ctx: int=Field(default=4096,ge=2048,le=8192)
    num_predict: int=Field(default=1200,ge=256,le=2048)
    temperature: float=Field(default=0.0,ge=0,le=0.3)
    timeout_seconds: int=Field(default=180,ge=10,le=600)
    max_calls_per_cycle: int=Field(default=30,ge=1,le=200)
    @model_validator(mode="after")
    def local(self):
        from urllib.parse import urlsplit
        u=urlsplit(self.base_url)
        if u.scheme!="http" or u.hostname not in {"localhost","127.0.0.1","::1"} or u.username or u.password:
            raise ValueError("Ollama仅允许本机HTTP地址")
        if self.num_predict>=self.num_ctx-512:
            raise ValueError("必须为输入预留上下文")
        return self

class Output(Strict):
    database: str="financial_dataset/events.sqlite3"
    raw_dir: str="financial_dataset/raw"
    export_dir: str="financial_dataset/events"
    security_master: str="data/security_master.json"

class Config(Strict):
    monitoring: Monitoring=Field(default_factory=Monitoring)
    collection: Collection
    search: Search=Field(default_factory=Search)
    fetch: Fetch=Field(default_factory=Fetch)
    ollama: Ollama=Field(default_factory=Ollama)
    output: Output=Field(default_factory=Output)
    @model_validator(mode="after")
    def nonempty(self):
        if any(k not in self.monitoring.directions for k in self.monitoring.direction_aliases):
            raise ValueError("direction_aliases只能定义已配置方向")
        if any(not s.strip() for s in self.monitoring.directions):
            raise ValueError("方向词不能为空")
        if self.search.provider=="rss" and not self.search.rss_feeds:
            raise ValueError("rss需要rss_feeds")
        if self.search.website_mode == "restricted":
            for market in self.monitoring.markets:
                self.search.domains_for(market, self.monitoring.markets)
        return self

class Loader(yaml.SafeLoader):
    pass

def mapping(loader,node,deep=False):
    d={}
    for k,v in node.value:
        key=loader.construct_object(k,deep=deep)
        if key in d:raise ValueError("重复配置键："+str(key))
        d[key]=loader.construct_object(v,deep=deep)
    return d
Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,mapping)

def load(path=None,root=ROOT):
    root=Path(root).resolve()
    path=Path(path) if path else Path("config.yaml")
    path=bounded(path if path.is_absolute() else root/path,root)
    try:
        raw=yaml.load(path.read_text(encoding="utf-8-sig"),Loader=Loader)
    except yaml.YAMLError as exc:
        mark=getattr(exc,"problem_mark",None)
        location=f"（第{mark.line+1}行）" if mark is not None else ""
        raise ValueError("YAML格式错误"+location+"；请检查引号、缩进和标点，配置内容已隐藏。") from None
    cfg=Config.model_validate(raw)
    for value in cfg.output.model_dump().values():
        bounded(root/value,root)
    return cfg
