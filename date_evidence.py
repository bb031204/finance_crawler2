"""Publication evidence must not silently become an update or effective date."""
import re
from datetime import datetime,timezone,timedelta

VALIDATION_VERSION=3
NON_PUBLICATION=re.compile(r"last\s*(?:modified|updated|reviewed|revised)(?:\s*(?:date|time))?|dateModified|modified\s*[:=]|updated\s*[:=]|modified[_\s]*(?:date|time|at)\s*[:=]?|updated?[_\s]*(?:date|time|at)\s*[:=]?|(?:最后)?更新时间|最后更新|修改日期|修改时间|生效日期|生效时间|effective\s*(?:date|from|on)|beginning\s*with",re.I)
PUBLICATION=re.compile(r"datePublished|publish(?:ed)?(?:[_\s]*(?:date|time|at))?|pubdate|date\s*released|release[_\s]*date|发布时间|发布日期|发表时间|刊发时间|公告日期|披露日期|披露时间|ctime(?:\s*[:=]|$)",re.I)


def validate_publication_quote(quote,source_text):
    if NON_PUBLICATION.search(quote):
        raise ValueError("日期证据是修改/更新/生效时间，不能冒充原始发布时间；请另找发布证据或time=null")
    source=re.sub(r"\s+","",source_text).casefold();literal=re.sub(r"\s+","",quote).casefold()
    if not literal or literal not in source:raise ValueError("date_quote须逐字引用原网页的日期线索")
    # Prevent stripping 'Last Modified Date:' and quoting only its date value.
    safe=False
    for match in re.finditer(re.escape(literal),source):
        prefix=source[max(0,match.start()-85):match.start()]
        bad=list(NON_PUBLICATION.finditer(prefix));good=list(PUBLICATION.finditer(prefix))
        if not bad or (good and good[-1].start()>bad[-1].start()):safe=True;break
    if not safe:raise ValueError("日期所在原文上下文仅支持修改/生效时间，不能删除标签后当作发布时间")


def trusted_publication_day(page):
    """Only explicit publication metadata, never a title year or search estimate."""
    if not isinstance(page,dict) or not isinstance(page.get("date_context",""),str):return None
    days=set()
    for line in page.get("date_context","").splitlines():
        key,sep,value=line.partition("=")
        if re.search(r'(?:related|recommend|comments?|itemListElement|breadcrumb)',key,re.I):continue
        if not sep or not PUBLICATION.search(key) or NON_PUBLICATION.search(key) or any(word in key.casefold() for word in ['modified','updated','reviewed']):continue
        try:
            numeric=re.match(r"^\s*(\d{13}|\d{10})(?!\d)",value)
            if numeric:
                if page.get("origin_market")!="CN":continue
                number=int(numeric[1]);seconds=number/1000 if len(numeric[1])==13 else number
                day=datetime.fromtimestamp(seconds,timezone(timedelta(hours=8))).date().isoformat()
            else:
                stamp=re.search(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})",value)
                if not stamp:continue
                day=datetime(int(stamp[1]),int(stamp[2]),int(stamp[3])).date().isoformat()
            days.add(day)
        except (ValueError,OverflowError,OSError):continue
    return next(iter(days)) if len(days)==1 else None
