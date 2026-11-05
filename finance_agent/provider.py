import os
from datetime import datetime,timezone,date,timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
import httpx
from lxml import etree
from .core import digest,now
from .safety import canonical,FetchError,PublicHTTP

def tavily_api_key(cfg):
    """Prefer the local config; never serialize this credential."""
    return cfg.api_key.get_secret_value().strip() or os.environ.get("TAVILY_API_KEY", "").strip()

class TavilyProvider:
    def __init__(self,cfg,http=None):
        self.cfg=cfg
        self.key=tavily_api_key(cfg)
        self.http=http or httpx.Client(timeout=30,trust_env=False)
    def preflight(self):
        if not self.key:raise ValueError("未配置Tavily密钥；请在config.yaml的search.api_key填写，或设置TAVILY_API_KEY环境变量。未执行联网搜索。")
    def close(self):self.http.close()
    def search(self,query,start,end,limit,*,market=None,markets=None):
        self.preflight()
        if not 1<=limit<=20:raise ValueError("Tavily每次最多20条")
        body={"query":query,"topic":self.cfg.topic,"search_depth":self.cfg.search_depth,
              "max_results":limit,"start_date":start,"end_date":(date.fromisoformat(end)+timedelta(days=1)).isoformat(),
              "include_published_date":True,"filter_by_published_date":False,
              "include_answer":False,"include_raw_content":False,
              "include_domains":self.cfg.domains_for(market, markets),"exclude_domains":self.cfg.exclude_domains,
              "auto_parameters":False}
        try:
            r=self.http.post("https://api.tavily.com/search",headers={"Authorization":"Bearer "+self.key},json=body)
            if r.status_code==429:raise FetchError("rate_limited","Tavily HTTP 429")
            if r.status_code in {401,403}:raise FetchError("blocked","Tavily凭证或权限无效")
            r.raise_for_status();payload=r.json()
        except httpx.HTTPError as exc:raise FetchError("network_error","Tavily "+type(exc).__name__) from None
        out=[]
        for item in payload.get("results",[])[:limit]:
            if not item.get("url"):continue
            url=canonical(item["url"])
            if urlsplit(url).scheme not in {"http","https"}:continue
            if not self.cfg.allows_url(url, market, markets):continue
            out.append({"candidate_id":"C_"+digest(url),"url":url,"title":str(item.get("title",""))[:500],
                        "snippet":str(item.get("content",""))[:1500],
                        "published_hint":item.get("published_date"),"date_basis":"provider_estimate",
                        "search_query":query,"search_provider":"tavily","first_seen_at":now(),
                        "source_name":urlsplit(url).hostname})
        return out,{"request":body,"response":payload,"searched_at":now()}

class RSSProvider:
    def __init__(self,cfg,http):
        self.cfg,self.http=cfg,http
    def preflight(self):
        if not self.cfg.rss_feeds:raise ValueError("rss_feeds为空")
    def close(self):pass
    def search(self,query,start,end,limit,*,market=None,markets=None):
        self.preflight();out=[];snapshots=[]
        domains = self.cfg.domains_for(market, markets)
        for feed in self.cfg.rss_feeds:
            if not self.cfg.allows_url(feed, market, markets):
                continue
            raw,info=self.http.get(feed, allowed=domains or None, excluded=self.cfg.exclude_domains)
            root=etree.fromstring(raw,parser=etree.XMLParser(resolve_entities=False,no_network=True))
            snapshots.append({"feed":feed,"sha256":digest(raw),"info":info})
            for item in root.xpath("//*[local-name()='item' or local-name()='entry']"):
                def text(name):
                    return "".join(item.xpath("./*[local-name()='"+name+"']//text()")).strip()
                url=text("link")
                links=item.xpath("./*[local-name()='link']/@href")
                if links:url=links[0]
                if not url:continue
                hint=text("pubDate") or text("published") or text("updated")
                try:
                    hint=parsedate_to_datetime(hint).astimezone(timezone.utc).isoformat()
                except (ValueError,TypeError,OverflowError):pass
                if hint and len(hint)>=10 and hint[:10].count("-")==2 and not start<=hint[:10]<=end:continue
                url=canonical(url)
                if not self.cfg.allows_url(url, market, markets):continue
                out.append({"candidate_id":"C_"+digest(url),"url":url,"title":text("title")[:500],
                            "snippet":(text("description") or text("summary"))[:1500],
                            "published_hint":hint or None,"date_basis":"feed_claim",
                            "search_query":query,"search_provider":"rss","first_seen_at":now(),
                            "source_name":urlsplit(url).hostname})
                if len(out)>=limit:return out,{"feeds":snapshots,"searched_at":now(),"note":"RSS为当前条目，不保证历史覆盖"}
        return out,{"feeds":snapshots,"searched_at":now(),"note":"RSS为当前条目，不保证历史覆盖"}
