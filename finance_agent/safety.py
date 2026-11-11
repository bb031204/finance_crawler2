import ipaddress
import re
import socket
import time
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode,urljoin
from urllib.robotparser import RobotFileParser
import httpx

class FetchError(RuntimeError):
    def __init__(self,status,message,raw=b"",info=None):
        super().__init__(message)
        self.status,self.raw,self.info=status,raw,info or {}

def canonical(url):
    p=urlsplit(url)
    return urlunsplit((p.scheme.lower(),p.netloc.lower(),p.path or "/",
        urlencode([(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True)
                   if not k.lower().startswith("utm_") and k.lower() not in {"spm","fbclid"}]),""))

def public_url(url,allowed=None,resolver=None,excluded=None,allow_fake_ip=False):
    p=urlsplit(url)
    if p.scheme not in {"http","https"} or not p.hostname or p.username or p.password or p.port not in {None,80,443}:
        raise FetchError("blocked","只允许标准公网HTTP/HTTPS地址")
    if excluded and any(p.hostname==d or p.hostname.endswith("."+d) for d in excluded):
        raise FetchError("blocked", "链接位于排除的网站范围")
    if allowed and not any(p.hostname==d or p.hostname.endswith("."+d) for d in allowed):
        raise FetchError("blocked","链接不在指定域名范围")
    try:
        addresses=(resolver or socket.getaddrinfo)(p.hostname,p.port or (443 if p.scheme=="https" else 80),type=socket.SOCK_STREAM)
        ips = sorted({ipaddress.ip_address(address[4][0]) for address in addresses}, key=str)
        # Only explicit local-proxy Fake-IP compatibility may accept these benchmark ranges.
        fake_networks = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("2001:2::/48"))
        fake_ips = [ip for ip in ips if any(ip.version == net.version and ip in net for net in fake_networks)]
        try:
            ipaddress.ip_address(p.hostname)
            hostname_is_literal = True
        except ValueError:
            hostname_is_literal = False
        fake_domain = (not hostname_is_literal and "." in p.hostname
                       and not p.hostname.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")))
        unsafe = [ip for ip in ips if not ip.is_global and not (
            allow_fake_ip and fake_domain and ip in fake_ips
        )]
        info = {"host": p.hostname, "addresses": [str(ip) for ip in ips]}
        if not ips or unsafe:
            only_fake = fake_domain and bool(ips) and all(ip in fake_ips or ip.is_global for ip in ips)
            if only_fake:
                raise FetchError("network_error",
                    f"DNS返回Fake-IP：{p.hostname} -> {','.join(map(str, fake_ips))}；请配置fetch.proxy_url和allow_fake_ip", info=info)
            raise FetchError("blocked", f"DNS包含非公网地址：{p.hostname} -> {','.join(map(str, unsafe))}", info=info)
    except socket.gaierror as exc:
        raise FetchError("network_error","DNS解析失败") from exc
    return canonical(url)

def challenge(raw):
    text=raw[:24000].decode("utf-8",errors="ignore")
    return bool(("acw_sc__v2" in text and "<script" in text.lower()) or re.search(
        r"<title[^>]*>[^<]*(验证码|安全验证|访问受限|拒绝访问|access denied|just a moment|登录)",text,re.I))

class PublicHTTP:
    def __init__(self,cfg):
        self.cfg=cfg
        self.client=httpx.Client(timeout=cfg.timeout_seconds,follow_redirects=False,
                                 proxy=cfg.proxy_url,trust_env=cfg.proxy_url is None,
                                 headers={"User-Agent":"FinanceResearchAgent/1.0","Accept":"*/*"})
        self.robots={}
        self.last={}
        self.blocked=set()
    def close(self):self.client.close()
    def get(self,url,robots=True,allowed=None,excluded=None,headers=None):
        for hop in range(self.cfg.max_redirects+1):
            url=public_url(url,allowed,excluded=excluded,allow_fake_ip=self.cfg.allow_fake_ip)
            origin=urlsplit(url).netloc.lower()
            if origin in self.blocked:raise FetchError("blocked","本轮该主机已返回访问限制")
            if robots and self.cfg.respect_robots:
                self.check_robots(url,allowed=allowed,excluded=excluded,headers=headers)
            time.sleep(max(0,1-(time.monotonic()-self.last.get(origin,0))))
            self.last[origin]=time.monotonic()
            try:
                with self.client.stream("GET",url,headers=headers) as r:
                    info={"url":url,"status_code":r.status_code,"content_type":r.headers.get("content-type","")}
                    if r.status_code in {401,403}:
                        self.blocked.add(origin)
                        raise FetchError("blocked",f"HTTP {r.status_code}",info=info)
                    if r.status_code==429:
                        raise FetchError("rate_limited","HTTP 429",info=dict(info,retry_after=r.headers.get("retry-after")))
                    if 300<=r.status_code<400:
                        target=r.headers.get("location")
                        if not target:raise FetchError("download_invalid","重定向缺少Location",info=info)
                        url=urljoin(url,target)
                        continue
                    if r.status_code==404:raise FetchError("not_found","HTTP 404",info=info)
                    if r.status_code>=500:raise FetchError("network_error",f"HTTP {r.status_code}",info=info)
                    if r.status_code!=200:raise FetchError("download_invalid",f"HTTP {r.status_code}",info=info)
                    parts=[];size=0;body_started=time.monotonic()
                    for block in r.iter_bytes():
                        if time.monotonic()-body_started>self.cfg.timeout_seconds:
                            raise FetchError("network_error","正文总读取时间超过预算",info=info)
                        size+=len(block)
                        if size>self.cfg.max_bytes:raise FetchError("download_invalid","响应超过大小限制",info=info)
                        parts.append(block)
                    raw=b"".join(parts)
                if challenge(raw):
                    self.blocked.add(origin)
                    raise FetchError("blocked","HTTP内容为验证或登录页",raw,info)
                if not raw:raise FetchError("empty_content","空响应",info=info)
                return raw,info
            except httpx.HTTPError as exc:
                raise FetchError("network_error",type(exc).__name__) from None
        raise FetchError("download_invalid","重定向次数超限")
    def check_robots(self,url,allowed=None,excluded=None,headers=None):
        p=urlsplit(url);origin=p.scheme+"://"+p.netloc
        if origin not in self.robots:
            try:
                raw,_=self.get(origin+"/robots.txt",robots=False,allowed=allowed,excluded=excluded,headers=headers)
                parser=RobotFileParser();parser.parse(raw.decode("utf-8",errors="replace").splitlines())
                self.robots[origin]=parser
            except FetchError as exc:
                if exc.status=="not_found":self.robots[origin]=None
                else:raise
        parser=self.robots[origin]
        agent = (headers or {}).get("User-Agent", "FinanceResearchAgent")
        if parser is not None and not parser.can_fetch(agent,url):
            raise FetchError("blocked","robots.txt不允许该抓取路径")
