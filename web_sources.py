"""Search and readable page acquisition. Financial decisions stay with Qwen."""
import json
import os
import re
from datetime import datetime, timedelta, timezone, date
from urllib.parse import urlsplit
from lxml import html as lh, etree
import httpx
from finance_agent.core import digest
from finance_agent.safety import PublicHTTP, canonical, FetchError
from finance_agent.progress import log, short


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


def visible(text):
    try:
        tree = lh.fromstring(text)
    except (ValueError, etree.ParserError):
        return " ".join(text.split())
    for node in tree.xpath("//script|//style|//nav|//footer|//header|//form|//aside|//noscript|//svg"):
        if node.getparent() is not None:
            node.drop_tree()
    return " ".join(tree.text_content().split())


def read_html(raw, url, use_crawl4ai=True, content_type=""):
    text = decode_html(raw, content_type)
    tree = lh.fromstring(text)
    title = " ".join(tree.xpath("//title/text()")).strip()
    url_ids = set(re.findall(r"\d{4,}", urlsplit(url).path))
    payloads = list(json_objects(tree))
    primary = []
    for payload in payloads:
        for path, item in walk(payload):
            identity = str(item.get("article_id", item.get("id", "")))
            headline = str(item.get("title", item.get("headline", "")))
            if (identity and identity in url_ids) or (len(headline) > 8 and title.startswith(headline)):
                primary.append((path, item))
    data = primary or [("page", payload) for payload in payloads]
    embedded = []
    clues = []
    for meta in tree.xpath("//meta[@content]"):
        name = meta.get("name") or meta.get("property") or meta.get("itemprop") or ""
        if re.search(r"date|time|publish", name, re.I):
            clues.append(name + "=" + meta.get("content", ""))
    for prefix, payload in data:
        for path, item in ([(prefix, payload)] if primary else walk(payload, prefix)):
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
                    if len(cleaned) > 80 and cleaned not in embedded:
                        embedded.append(cleaned)
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
    roots = tree.xpath("//article|//main|//*[@role='main']")
    body = max(roots, key=lambda item: len(item.text_content())) if roots else tree
    body_text = " ".join(body.text_content().split())
    # A matched article JSON supplies the main body without duplicated site navigation.
    parts = [] if primary and embedded else ([body_text] if body_text else [])
    for item in embedded:
        if any(item in previous for previous in parts):
            continue
        parts = [previous for previous in parts if previous not in item]
        parts.append(item)
    combined = "\n\n".join(parts)
    if not combined.strip():
        raise ValueError("网页正文为空")
    if combined.count("\ufffd") > max(2, len(combined) // 100):
        raise ValueError("正文存在大量无法解码字符，保留URL待检查")
    date_context = "\n".join(dict.fromkeys(clues))[:1050]
    return {"title": title, "text": combined, "date_context": date_context, "cleaning": cleaning}


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

    def search(self, query, market, start, end):
        limit = self.cfg.collection.max_candidates_per_query
        if self.cfg.search.provider == "rss":
            return self.rss(market, limit)
        key = self.cfg.search.api_key.get_secret_value().strip() or os.environ.get("TAVILY_API_KEY", "").strip()
        if not key:
            raise ValueError("请在config.yaml的search.api_key填写Tavily密钥")
        body = {"query": query, "topic": self.cfg.search.topic, "search_depth": "basic", "max_results": limit,
                "start_date": start, "end_date": (date.fromisoformat(end)+timedelta(days=1)).isoformat(),
                "include_domains": self.domains(market), "exclude_domains": self.cfg.search.exclude_domains,
                "include_answer": False, "include_raw_content": False, "auto_parameters": False}
        response = self.search_http.post("https://api.tavily.com/search", headers={"Authorization": "Bearer " + key}, json=body)
        if response.status_code != 200:
            raise ValueError("Tavily请求失败 HTTP " + str(response.status_code))
        return [{"url": canonical(item["url"]), "title": str(item.get("title", "")),
                 "snippet": str(item.get("content", ""))[:900], "origin_market": market}
                for item in response.json().get("results", [])[:limit]
                if item.get("url") and self.allowed(item["url"], market)]

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
                    results.append({"url": canonical(url), "title": text("title"),
                                    "snippet": (text("description") or text("summary"))[:900], "origin_market": market})
            if len(results) >= limit:
                break
        return results[:limit]

    def page_from_bytes(self, raw, url, origin_market, content_type="text/html", title=""):
        if raw.lstrip().startswith(b"%PDF"):
            if not self.cfg.fetch.allow_pdf:
                raise ValueError("配置未启用PDF")
            from finance_agent.pdf_layout import extract_pdf_layout
            parsed = extract_pdf_layout(raw, {"root": self.root, "ocr_enabled": False})
            if parsed["quality"]["status"] == "needs_ocr":
                raise ValueError("PDF缺少可读正文，需要OCR，当前简化版保留URL待处理")
            text = "\n\n".join(parsed["paragraphs"])
            result = {"title": title, "text": text, "date_context": text[:800], "cleaning": "PDF"}
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
        result.update(url=canonical(url), origin_market=origin_market)
        return result

    def fetch(self, candidate):
        market = candidate["origin_market"]
        if not self.allowed(candidate["url"], market):
            raise ValueError("URL超出当前市场的网站范围")
        raw, info = self.http.get(candidate["url"], allowed=self.domains(market) or None,
                                  excluded=self.cfg.search.exclude_domains)
        page = self.page_from_bytes(raw, info["url"], market, info.get("content_type", ""), candidate["title"])
        log("抓取", f"{page['cleaning']} | 正文{len(page['text'])}字符 | {short(page['title'], 65)}")
        return page
