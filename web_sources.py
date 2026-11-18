"""Search and readable page acquisition. Financial decisions stay with Qwen."""
import json
import os
import re
from datetime import datetime, timedelta, timezone, date
from urllib.parse import urlsplit, urljoin, quote
from lxml import html as lh, etree
import httpx
from finance_agent.core import digest, now
from finance_agent.safety import PublicHTTP, canonical, FetchError
from finance_agent.progress import log, short
from record_quality import primary_article_text
from sec_documents import SEC_DOCUMENT_VERSION,document_policy,accession_document_url,filing_documents


def decode_html(raw, content_type=""):
    declared = re.search(r"charset\s*=\s*([\w-]+)", content_type, re.I)
    choices = ["utf-8-sig"]
    if declared:
        choices.append(declared[1])
    meta = re.search(br'charset\s*=\s*["\x27]?\s*([\w-]+)', raw[:10000], re.I)
    if meta:
        choices.append(meta[1].decode("ascii"))
    choices.extend(["gb18030", "windows-1252"])
    for encoding in dict.fromkeys(choices):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def json_objects(tree):
    for script in tree.xpath("//script"):
        text = script.text or ""
        if script.get("type") not in {"application/json", "application/ld+json"} and "__NEXT_DATA__" not in text:
            continue
        try:
            yield json.loads(text)
        except (ValueError, RecursionError):
            match = re.search(r"\b__NEXT_DATA__\s*=\s*", text)
            if match:
                try:
                    yield json.JSONDecoder().raw_decode(text[match.end():])[0]
                except (ValueError, RecursionError):
                    pass


def walk(value, path="page"):
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                yield from walk(child, path + "." + str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, path + f"[{index}]")


def drop_secondary_sections(tree):
    """Drop explicitly marked comments/related widgets, never arbitrary financial prose."""
    pattern=re.compile(r'(?:^|[\s_-])(?:comments?|commentlist|related-news|related-articles|related-posts|recommended|recommendations|disqus_thread)(?:$|[\s_-])',re.I)
    for node in list(tree.xpath('//*[@id or @class]')):
        if node.getparent() is not None and pattern.search((node.get('id','')+' '+node.get('class','')).strip()):
            node.drop_tree()


def main_article_node(tree):
    roots=tree.xpath("//*[@itemprop='articleBody']") or tree.xpath('//article') or tree.xpath("//main|//*[@role='main']")
    return max(roots,key=lambda node:len(node.text_content())) if roots else tree


def visible(text):
    try:
        tree = lh.fromstring(text)
    except (ValueError, etree.ParserError):
        return " ".join(text.split())
    drop_secondary_sections(tree)
    for node in tree.xpath("//script|//style|//nav|//footer|//header|//form|//aside|//noscript|//svg"):
        if node.getparent() is not None:
            node.drop_tree()
    return " ".join(tree.text_content().split())


def unrelated_payload_path(path):
    return bool(re.search(r'(?:^|[.\[])(?:related\w*|recommend\w*|comments?\w*|authors?|breadcrumbs?|itemListElement)(?:[.\[]|$)',path,re.I))


def bound_article_payloads(tree,payloads,url,title):
    """Prefer URL/ID evidence; a different explicit article URL defeats a title match."""
    ids={value for value in re.findall(r'\d{4,}',urlsplit(url).path) if not 1900<=int(value)<=2099}
    compact=lambda value:re.sub(r'\s+','',value).casefold()
    headings=tree.xpath('//h1')
    names=[' '.join(headings[0].itertext()).strip()] if headings else [title,re.split(r'\s+[|｜]\s+',title,maxsplit=1)[0]]
    names={compact(name) for name in names if name}
    strong=[];fallback=[]
    for payload in payloads:
        for path,item in walk(payload):
            if unrelated_payload_path(path):continue
            identity=str(item.get('article_id',item.get('id','')))
            headline=str(item.get('title',item.get('headline','')))
            kinds=item.get('@type',[]);kinds=kinds if isinstance(kinds,list) else [kinds]
            article=any(any(word in str(kind).lower() for word in ['article','blogposting','socialmediapost']) for kind in kinds)
            article=article or any(isinstance(item.get(key),str) and len(item[key])>80 for key in ['articleBody','content','body','text'])
            links=[]
            for key in ['url','@id','mainEntityOfPage']:
                value=item.get(key)
                if isinstance(value,dict):value=value.get('@id') or value.get('url')
                if isinstance(value,str) and value:
                    link=urljoin(url,value)
                    if urlsplit(link).scheme in {'http','https'}:links.append(link)
            linked=any(canonical(link)==canonical(url) for link in links)
            if links and not linked:continue
            if (linked and article) or (identity and identity in ids):strong.append((path,item))
            elif article and len(compact(headline))>=4 and compact(headline) in names:fallback.append((path,item))
    return strong or fallback


def usable_page_cache(page,url,sec=None):
    if not isinstance(page,dict) or not isinstance(page.get('text'),str) or not page['text'].strip():return False
    linked=canonical(page.get('url',''))==canonical(url) or page.get('request_url')==canonical(url)
    if not linked:return False
    # Older versioned caches may contain unrelated cards matched by a title prefix.
    if page.get('article_binding_version') is not None and page.get('article_binding_version',0)<4:return False
    if page.get('sec_filing_date') and page.get('document_url'):
        if page.get('sec_document_version')!=SEC_DOCUMENT_VERSION:return False
        if sec is not None and page.get('sec_document_policy')!=document_policy(sec):return False
    return not any(unrelated_payload_path(line.partition('=')[0]) for line in page.get('date_context','').splitlines())


def read_html(raw, url, use_crawl4ai=True, content_type=""):
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", decode_html(raw, content_type), count=1, flags=re.I)
    tree = lh.fromstring(text)
    hidden_headers=0
    host=(urlsplit(url).hostname or '').lower()
    if (host=='sec.gov' or host.endswith('.sec.gov')) and '/Archives/edgar/' in urlsplit(url).path:
        # Inline XBRL's ix:header contains non-visible resources/hidden facts,
        # not the article body. Keep every visible nonfraction/nonnumeric node.
        for node in list(tree.iter()):
            if isinstance(node.tag,str) and node.tag.lower() in {'ix:header','{http://www.xbrl.org/2013/inlinexbrl}header'}:
                if node.getparent() is not None:node.drop_tree();hidden_headers+=1
        if hidden_headers:
            text=lh.tostring(tree,encoding='unicode')
    # SVG accessibility <title> nodes (e.g. a lock icon) are not the document title.
    title = " ".join(tree.xpath("/html/head/title/text()")).strip()
    payloads = list(json_objects(tree))
    primary = bound_article_payloads(tree,payloads,url,title)
    drop_secondary_sections(tree)
    headings=main_article_node(tree).xpath('.//h1')
    heading=' '.join(headings[0].itertext()).strip() if headings else ''
    bound_titles=[item.get('headline') or item.get('title') for _,item in primary]
    bound_title=next((value.strip() for value in bound_titles if isinstance(value,str) and value.strip()),'')
    source_title=heading or bound_title or title or None
    title_origin='heading' if heading else 'bound_metadata' if bound_title else 'html_title' if title else 'unknown'
    data = primary
    embedded = []
    descriptions = []
    clues = []
    for meta in tree.xpath("//meta[@content]"):
        name = meta.get("name") or meta.get("property") or meta.get("itemprop") or ""
        if re.search(r"date|time|publish", name, re.I):
            clues.append(name + "=" + meta.get("content", ""))
    for node in main_article_node(tree).xpath('.//time[@datetime]'):
        label=' '.join([node.get('itemprop',''),node.get('class',''),' '.join(node.itertext()).strip()])
        if re.search(r'last\s*(?:updated|modified)|dateModified|updated|modified|更新|修改',label,re.I):key='dateModified'
        elif re.search(r'datePublished|publish|pubdate|发布|发表|刊发',label,re.I):key='datePublished'
        else:key='article_time_datetime'
        clues.append(key+'='+node.get('datetime')+' | '+label.strip())
    # Fed press releases expose a visible publisher-specific date outside generic meta tags.
    # Bind only this known publication node in the main document; never borrow a footer date.
    host=(urlsplit(url).hostname or '').lower()
    if host=='federalreserve.gov' or host.endswith('.federalreserve.gov'):
        dates=[node for node in tree.iter() if 'article__time' in (node.get('class') or '').split()]
        if len(dates)==1:
            value=' '.join(dates[0].itertext()).strip()
            if re.fullmatch(r'[A-Za-z]+\s+\d{1,2},?\s+\d{4}',value):clues.insert(0,'datePublished='+value)
    for prefix, payload in data:
        for path, item in [(prefix, payload)]:
            for key, value in item.items():
                if isinstance(value, (str, int, float)) and re.search(r"date|time|publish|created", key, re.I):
                    clue = path + "." + key + "=" + str(value)
                    if isinstance(value, (int, float)) and 946684800 <= value <= 4102444800000:
                        seconds = value / 1000 if value > 4102444800 else value
                        try:
                            stamp = datetime.fromtimestamp(seconds, timezone.utc)
                            clue += " | UTC=" + stamp.isoformat() + " | UTC+08=" + stamp.astimezone(timezone(timedelta(hours=8))).isoformat()
                        except (ValueError, OSError, OverflowError):
                            pass
                    clues.append(clue)
                if isinstance(value, str) and len(value) > 80 and key.lower() in {"content", "articlebody", "body", "text", "description"}:
                    cleaned = visible(value)
                    if len(cleaned) > 80:
                        bucket = descriptions if key.lower() == "description" else embedded
                        if cleaned not in bucket:
                            bucket.append(cleaned)
    original_visible = visible(text)
    for match in list(re.finditer(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?", original_visible[:1800]))[:3]:
        clues.append(original_visible[max(0, match.start()-30):match.end()+30])
    cleaning = "lxml"
    if use_crawl4ai:
        try:
            from crawl4ai.content_scraping_strategy import LXMLWebScrapingStrategy
            result = LXMLWebScrapingStrategy().scrap(url, text, word_count_threshold=1,
                excluded_tags=["script", "style", "nav", "footer", "header", "form", "aside"])
            if not isinstance(result, dict):
                result = result.model_dump()
            if result.get("cleaned_html"):
                tree = lh.fromstring(result["cleaned_html"])
                cleaning = "Crawl4AI"
        except Exception as error:
            log("正文", "Crawl4AI回退：" + type(error).__name__)
    for node in tree.xpath("//script|//style|//nav|//footer|//header|//form|//aside|//noscript|//svg"):
        if node.getparent() is not None:
            node.drop_tree()
    drop_secondary_sections(tree)
    body = main_article_node(tree)
    body_text = " ".join(body.text_content().split())
    # A matched article JSON supplies the main body without duplicated site navigation.
    parts = [] if primary and embedded else ([body_text] if body_text else [])
    for item in embedded:
        if any(item in previous for previous in parts):
            continue
        parts = [previous for previous in parts if previous not in item]
        parts.append(item)
    if not parts and descriptions:
        parts = descriptions
    combined = primary_article_text("\n\n".join(parts),url)
    if not combined.strip():
        raise ValueError("网页正文为空")
    if combined.count("\ufffd") > max(2, len(combined) // 100):
        raise ValueError("正文存在大量无法解码字符，保留URL待检查")
    date_context = "\n".join(dict.fromkeys(clues))[:1050]
    return {"title": title, "source_title":source_title,'title_origin':title_origin,"text": combined, "date_context": date_context, "cleaning": cleaning,
        'removed_inline_xbrl_headers':hidden_headers,
        'article_binding_version':4,'article_binding':'matched_payload' if primary else 'visible_only'}


class Sources:
    def __init__(self, cfg, root):
        self.cfg, self.root = cfg, root
        self.http = PublicHTTP(cfg.fetch)
        self.search_http = httpx.Client(timeout=30, trust_env=False)

    def close(self):
        self.http.close()
        self.search_http.close()

    def domains(self, market):
        return self.cfg.search.market_domains.get(market, []) if self.cfg.search.website_mode == "restricted" else []

    def allowed(self, url, market):
        host = urlsplit(url).hostname or ""
        if any(host == d or host.endswith("." + d) for d in self.cfg.search.exclude_domains):
            return False
        domains = self.domains(market)
        return not domains or any(host == d or host.endswith("." + d) for d in domains)

    def sec_get(self, url):
        """SEC contact header is sent only to SEC, using the existing proxy/robots policy."""
        host = urlsplit(url).hostname or ""
        if not (host == "sec.gov" or host.endswith(".sec.gov")) or not self.allowed(url, "US"):
            raise ValueError("SEC链接超出已配置网站范围")
        agent = self.cfg.search.sec_edgar.user_agent.get_secret_value().strip()
        if not agent: raise ValueError("SEC访问需在search.sec_edgar.user_agent填写真实联系信息")
        return self.http.get(url, allowed=["sec.gov"], excluded=self.cfg.search.exclude_domains,
                             headers={"User-Agent": agent})

    def candidate_url(self, url):
        url = canonical(url)
        if self.cfg.search.discovery_mode != "search":
            from sec_discovery import filing_url
            return filing_url(url)
        return url

    def sec_page(self, candidate):
        """Use one accession's main document and bounded financial exhibits as one source."""
        from sec_discovery import filing_identity,filing_url
        match = re.fullmatch(r"/Archives/edgar/data/(\d+)/(\d{10}-\d{2}-\d{6})-index\.html", urlsplit(candidate["url"]).path)
        if not match: raise ValueError("SEC申报索引地址格式异常")
        cik, accession = match.groups()
        index_url = filing_identity(cik, accession)
        raw, index_info = self.sec_get(index_url)
        if index_info.get('url') and filing_url(index_info['url'])!=index_url:
            raise ValueError('SEC申报索引重定向到另一申报，保留待复核')
        tree = lh.fromstring(raw)
        # The date belongs to this exact accession, never to a search hit or another filing.
        def labeled(label):
            values = tree.xpath("//*[contains(concat(' ',normalize-space(@class),' '),' infoHead ')][normalize-space(.)=$label]/following-sibling::*[1]", label=label)
            return " ".join(values[0].itertext()).strip() if values else ""
        filing_date = labeled("Filing Date")
        date.fromisoformat(filing_date)
        expected = candidate.get("sec_filing", {})
        if expected.get("filing_date") and expected["filing_date"] != filing_date:
            raise ValueError("SEC目录与申报索引日期不一致，保留待复核")
        main,exhibits=filing_documents(tree,expected.get('form'))
        document_url=accession_document_url(index_url,main.href,cik,accession)
        # Cross-check primaryDocument when the company API supplied it.
        primary = expected.get("primary_document")
        if primary and document_url != accession_document_url(index_url,urljoin(document_url,quote(primary,safe='/')),cik,accession):
            raise ValueError("SEC公司目录与申报索引的主文档不一致，保留待复核")
        sec=self.cfg.search.sec_edgar
        selected=[];seen={document_url}
        if sec.include_exhibits:
            for exhibit in exhibits:
                url=accession_document_url(index_url,exhibit.href,cik,accession)
                if url not in seen:
                    selected.append((exhibit,url));seen.add(url)
        unread=selected[sec.max_exhibits_per_filing:]
        selected=selected[:sec.max_exhibits_per_filing]
        texts=[];provenance=[];page=None;byte_count=len(raw);offset=0
        for document,url in [(main,document_url)]+selected:
            body,info=self.sec_get(url)
            if info.get('url') and accession_document_url(index_url,info['url'],cik,accession)!=url:
                raise ValueError('SEC文档重定向与申报索引不一致，保留待复核')
            byte_count+=len(body)
            if byte_count>self.cfg.fetch.max_bytes:
                raise ValueError('SEC主文档及附件合计超过max_bytes，保留待处理')
            part=self.page_from_bytes(body,url,'US',info.get('content_type',''),candidate['title'],
                candidate.get('title_is_source',candidate.get('discovery_source')!='sec_edgar') if page is None else False)
            if page is None:page=part
            separator='\n\n' if texts else ''
            offset+=len(separator)
            texts.append(separator+part['text'])
            provenance.append(dict(url=url,type=document.kind,description=document.description,
                raw_sha256=part['raw_sha256'],text_sha256=digest(part['text'].encode('utf-8')),
                text_start=offset,text_end=offset+len(part['text'])))
            offset+=len(part['text'])
            if offset>self.cfg.fetch.max_text_chars:
                raise ValueError('SEC主文档及附件正文合计超过max_text_chars，保留待处理')
        page.update(text=''.join(texts),sec_documents=provenance,sec_document_version=SEC_DOCUMENT_VERSION,
            sec_document_policy=document_policy(sec),sec_unread_exhibits=[{'url':url,'type':doc.kind} for doc,url in unread],
            sec_index_sha256=digest(raw))
        page.update(url=index_url, request_url=canonical(candidate['url']), document_url=document_url, sec_filing_date=filing_date,
                    date_context="SEC filing datePublished=" + filing_date, fetched_at=now())
        log("抓取", f"SEC主文档 | {main.kind} | 附件{len(selected)}份，超本次上限{len(unread)}份 | 官方申报日期={filing_date} | 正文{len(page['text'])}字符")
        return page

    def search(self, query, market, start, end, domains=None,strict_dates=None):
        if callable(getattr(self,'pause_check',None)):self.pause_check()
        limit = self.cfg.collection.max_candidates_per_query
        if self.cfg.search.provider == "rss":
            return self.rss(market, limit)
        key = self.cfg.search.api_key.get_secret_value().strip() or os.environ.get("TAVILY_API_KEY", "").strip()
        if not key:
            raise ValueError("请在config.yaml的search.api_key填写Tavily密钥")
        scope = self.domains(market) if domains is None else domains
        if self.cfg.search.website_mode == "restricted" and any(domain not in self.domains(market) for domain in scope):
            raise ValueError("搜索子范围超出当前市场配置")
        if self.cfg.search.website_mode == "restricted" and not scope:
            raise ValueError("受限模式搜索来源不能为空")
        body = {"query": query, "topic": self.cfg.search.topic, "search_depth": "basic", "max_results": limit,
                "start_date": start, "end_date": (date.fromisoformat(end)+timedelta(days=1)).isoformat(),
                "include_domains": scope, "exclude_domains": self.cfg.search.exclude_domains,
                "include_answer": False, "include_raw_content": False, "auto_parameters": False}
        body.update(include_published_date=True,filter_by_published_date=bool(strict_dates))
        response = self.search_http.post("https://api.tavily.com/search", headers={"Authorization": "Bearer " + key}, json=body)
        if response.status_code != 200:
            raise ValueError("Tavily请求失败 HTTP " + str(response.status_code))
        return [{"url": self.candidate_url(item["url"]), "title": str(item.get("title", "")),
                 "snippet": str(item.get("content", ""))[:900], "origin_market": market,
                 "search_published_at":item.get("published_date")}
                for item in response.json().get("results", [])[:limit]
                if item.get("url") and self.allowed(item["url"], market) and (not scope or any((urlsplit(item["url"]).hostname or "")==d or (urlsplit(item["url"]).hostname or "").endswith("."+d) for d in scope))]

    def rss(self, market, limit):
        results = []
        for feed in self.cfg.search.rss_feeds:
            if not self.allowed(feed, market):
                continue
            raw, _ = self.http.get(feed, allowed=self.domains(market) or None, excluded=self.cfg.search.exclude_domains)
            tree = etree.fromstring(raw, parser=etree.XMLParser(resolve_entities=False, no_network=True))
            for item in tree.xpath("//*[local-name()='item' or local-name()='entry']"):
                def text(name):
                    return "".join(item.xpath("./*[local-name()='"+name+"']//text()")).strip()
                links = item.xpath("./*[local-name()='link']/@href")
                url = links[0] if links else text("link")
                if url and self.allowed(url, market):
                    results.append({"url": self.candidate_url(url), "title": text("title"),
                                    "snippet": (text("description") or text("summary"))[:900], "origin_market": market})
            if len(results) >= limit:
                break
        return results[:limit]

    def page_from_bytes(self, raw, url, origin_market, content_type="text/html", title="",title_is_source=True):
        listing_title=title.strip() if isinstance(title,str) else ''
        if raw.lstrip().startswith(b"%PDF"):
            if not self.cfg.fetch.allow_pdf:
                raise ValueError("配置未启用PDF")
            from finance_agent.pdf_layout import extract_pdf_layout
            parsed = extract_pdf_layout(raw, {"root": self.root, "ocr_enabled": False})
            if parsed["quality"]["status"] == "needs_ocr":
                raise ValueError("PDF缺少可读正文，需要OCR，当前简化版保留URL待处理")
            text = "\n\n".join(parsed["paragraphs"])
            result = {"title": title, "text": text, "date_context": text if len(text)<=600 else text[:250]+"\n[中间正文另行分段阅读]\n"+text[-350:], "cleaning": "PDF"}
            result.update(source_title=listing_title if title_is_source and listing_title else None,
                title_origin='source_listing' if title_is_source and listing_title else 'unknown')
        else:
            if urlsplit(url).path.lower().endswith(".pdf"):
                raise ValueError("PDF链接返回的内容并非PDF")
            try:
                result = read_html(raw, url, self.cfg.fetch.use_crawl4ai, content_type)
            except (etree.ParserError, etree.XMLSyntaxError, UnicodeError):
                raise ValueError("网页内容无法解析，保留URL待处理") from None
            result["title"] = result["title"] or title
        if len(result["text"]) > self.cfg.fetch.max_text_chars:
            raise ValueError("文章超过max_text_chars上限，保留链接，不截断后冒充完整摘要")
        result.update(url=canonical(url), origin_market=origin_market, raw_sha256=digest(raw))
        return result

    def fetch(self, candidate):
        if callable(getattr(self,'pause_check',None)):self.pause_check()
        market = candidate["origin_market"]
        if not self.allowed(candidate["url"], market):
            raise ValueError("URL超出当前市场的网站范围")
        if (urlsplit(candidate["url"]).hostname in {"www.sec.gov","sec.gov"} and
                re.fullmatch(r"/Archives/edgar/data/\d+/\d{10}-\d{2}-\d{6}-index\.html", urlsplit(candidate["url"]).path)):
            return self.sec_page(candidate)
        host = urlsplit(candidate["url"]).hostname or ""
        if (host == "sec.gov" or host.endswith(".sec.gov")) and self.cfg.search.sec_edgar.user_agent.get_secret_value().strip():
            raw, info = self.sec_get(candidate["url"])
        else:
            raw, info = self.http.get(candidate["url"], allowed=self.domains(market) or None,
                                      excluded=self.cfg.search.exclude_domains)
        page = self.page_from_bytes(raw, info["url"], market, info.get("content_type", ""), candidate["title"],candidate.get('title_is_source',True))
        page['request_url']=canonical(candidate['url'])
        page["fetched_at"] = now()
        log("抓取", f"{page['cleaning']} | 正文{len(page['text'])}字符 | {short(page['title'], 65)}")
        return page
