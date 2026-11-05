import re
import json
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit
from lxml import html as lh
from .core import digest,now,bounded
from .safety import FetchError,challenge

def publication(raw):
    if not raw:return {"published_at":None,"published_date":None,"time_precision":"unknown"}
    raw=str(raw).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}",raw):
        try:datetime.fromisoformat(raw)
        except ValueError:return publication(None)
        return {"published_at":None,"published_date":raw,"time_precision":"date"}
    try:
        d=datetime.fromisoformat(raw.replace("Z","+00:00"))
        if d.tzinfo is None:return publication(None)
        return {"published_at":d.astimezone(timezone.utc).isoformat(),"published_date":d.date().isoformat(),
                "time_precision":"second"}
    except ValueError:return publication(None)

def html_body(raw,url,use_crawl4ai=True):
    doc=lh.fromstring(raw)
    title=" ".join(doc.xpath("//title/text()")).strip()
    dates=doc.xpath("//meta[@property='article:published_time' or @name='date' or @itemprop='datePublished']/@content")
    dates+=doc.xpath("//time[@datetime]/@datetime")
    date_raw=dates[0] if dates else None
    if not date_raw:
        for text in doc.xpath("//script[@type='application/ld+json']/text()"):
            try:
                obj=json.loads(text)
                items=obj if isinstance(obj,list) else [obj]
                for item in list(items):
                    if isinstance(item,dict):items.extend(item.get("@graph",[]))
                date_raw=next((x["datePublished"] for x in items if isinstance(x,dict) and x.get("datePublished")),None)
                if date_raw:break
            except (ValueError,TypeError):pass
    quality={"status":"text_parsed","crawler_cleaning":False}
    if use_crawl4ai:
        try:
            from crawl4ai.content_scraping_strategy import LXMLWebScrapingStrategy
            scraped=LXMLWebScrapingStrategy().scrap(url,raw.decode("utf-8",errors="replace"),
                word_count_threshold=1,excluded_tags=["script","style","nav","footer","header","form"])
            if not isinstance(scraped,dict):scraped=scraped.model_dump()
            clean=scraped.get("cleaned_html")
            if clean:
                doc=lh.fromstring(clean);quality["crawler_cleaning"]=True
        except Exception as exc:
            quality["crawler_cleaning_error"]=type(exc).__name__+": "+str(exc)[:150]
    for node in doc.xpath("//script|//style|//nav|//footer|//header|//form|//noscript"):
        node.drop_tree()
    roots=doc.xpath("//article|//main|//*[@role='main']")
    body=max(roots,key=lambda x:len(x.text_content())) if roots else doc
    nodes=body.xpath(".//p|.//h1|.//h2|.//h3|.//li|.//table")
    texts=[]
    for node in nodes:
        if node.xpath("ancestor::table") or node.xpath("ancestor::li"):continue
        text=re.sub(r"\s+"," ",node.text_content()).strip()
        if text and (not texts or text!=texts[-1]):texts.append(text)
    if not texts:
        text=re.sub(r"\s+"," ",body.text_content()).strip()
        if text:texts=[text]
    return title,date_raw,[{"id":"P"+str(i),"text":t,"kind":"paragraph"} for i,t in enumerate(texts,1)],quality

class ContentFetcher:
    def __init__(self,cfg,http,root):
        self.cfg,self.http,self.root=cfg,http,Path(root)
    def fetch(self,candidate):
        market = candidate.get("query_market")
        markets = self.cfg.monitoring.markets
        if not self.cfg.search.allows_url(candidate["url"], market, markets):
            raise FetchError("out_of_scope", "来源不在当前市场的网站范围内")
        domains = self.cfg.search.domains_for(market, markets)
        raw,info=self.http.get(candidate["url"], allowed=domains or None,
                               excluded=self.cfg.search.exclude_domains)
        return self.parse(candidate,raw,info)
    def parse(self,candidate,raw,info):
        from .runtime import setup
        setup(self.root)
        if challenge(raw):raise FetchError("blocked","正文为验证页",raw,info)
        ctype=info.get("content_type","").lower()
        pdf=urlsplit(candidate["url"]).path.lower().endswith(".pdf") or "application/pdf" in ctype or raw.lstrip().startswith(b"%PDF")
        if pdf:
            if not self.cfg.fetch.allow_pdf:raise FetchError("download_invalid","PDF开关已关闭",raw,info)
            if not raw.lstrip().startswith(b"%PDF"):raise FetchError("download_invalid","PDF地址没有返回PDF",raw,info)
            try:
                from .pdf_layout import extract_pdf_layout
                value=extract_pdf_layout(raw,{"root":self.root,"ocr_enabled":self.cfg.fetch.allow_ocr,
                                             "ocr_max_pages":self.cfg.fetch.ocr_max_pages})
            except Exception as exc:raise FetchError("parse_failed",str(exc)[:350],raw,info) from None
            paragraphs=[dict(id="P"+str(i),text=t,**value["paragraph_metadata"][i-1])
                        for i,t in enumerate(value["paragraphs"],1)]
            quality=value["quality"];title=candidate["title"];date_raw=None;ext="pdf"
        else:
            if "html" not in ctype and not raw.lstrip().startswith(b"<"):
                raise FetchError("download_invalid","仅支持HTML与PDF正文",raw,info)
            try:title,date_raw,paragraphs,quality=html_body(raw,info["url"],self.cfg.fetch.use_crawl4ai)
            except Exception as exc:raise FetchError("parse_failed",type(exc).__name__,raw,info) from None
            ext="html"
        if not paragraphs or sum(len(p["text"]) for p in paragraphs)<30:
            raise FetchError("empty_content","未得到足够正文",raw,info)
        content_hash=digest(raw)
        path=bounded(self.root/self.cfg.output.raw_dir/(content_hash+"."+ext),self.root)
        path.parent.mkdir(parents=True,exist_ok=True)
        if not path.exists():path.write_bytes(raw)
        elif digest(path.read_bytes())!=content_hash:raise ValueError("原件哈希碰撞或被修改")
        # A provider date remains a hint; never promote it to verified publication time.
        pub=publication(date_raw)
        return {"candidate_id":candidate["candidate_id"],"version_id":"D_"+digest([candidate["url"],content_hash,paragraphs,quality,pub]),
                "source_url":candidate["url"],"final_url":info["url"],"title":title or candidate["title"],
                "source_name":candidate["source_name"],"directions":[candidate["monitoring_direction"]] if candidate.get("monitoring_direction") else [],"content_type":ctype,"content_hash":content_hash,
                "raw_path":path.relative_to(self.root).as_posix(),"paragraphs":paragraphs,"quality":quality,
                "published_at_raw":date_raw,"provider_published_hint":candidate.get("published_hint"),
                **pub,"first_seen_at":candidate["first_seen_at"],"fetched_at":now(),
                "document_available_at":now(),"fetch_method":info.get("transport","public_http"),
                "time_evidence":"source_metadata" if date_raw else "unknown"}
