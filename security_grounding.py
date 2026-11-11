"""Conservative source-grounded securities; no registry lookup or price inference."""
import re

SECURITY_VALIDATION_VERSION = "security_grounding_v3_optional_codes"
CODE = r"(?:[A-Za-z]{1,6}(?:[.\-][A-Za-z0-9]{1,5})?|\d{4,6}(?:\.(?:SH|SZ|BJ|HK))?)"
EXCHANGE = r"(?:NASDAQOTH|NASDAQ|NYSE(?:\s+ARCA)?|AMEX|OTCQX|OTCQB|OTC|HKEX|SSE|SZSE|BSE|上交所|深交所|北交所)"
LABEL = r"(?:证券代码|股票代码|Stock\s+(?:Code|Symbol)|Ticker(?:\s+Symbol)?|Trading\s+Symbol)"
DECLARATION = re.compile(r"(?:(?P<exchange>"+EXCHANGE+r")\s*[:：]|"+LABEL+r"\s*[:：]?)[ \t]*(?P<ticker>"+CODE+r")(?![A-Za-z0-9]|[.\-][A-Za-z0-9])", re.I)
FINANCIAL_ABBREVIATIONS = set("AI API ADR ADS CEO CFO CPI EBITDA EBIT EPS ETF FED FOMC GAAP GDP IFRS IPO IR IRS LLC LTD PDF PPI QOQ RD REIT ROI ROE RSS SEC US USA USD UK VAT YOY YTD THE AND FOR FORM ITEM OF IN ON TO AT BY AS IS IT AN BE OR NOT ARE WAS WERE HAS HAD BUT ALL ITS NET NEW ONE TWO MAY CAN END PER".split())
FINANCIAL_ABBREVIATIONS.update({'DIY','IMF','OECD','WHO','UN','FDIC','ECB','BOJ','PBOC'})


def compact(value):
    return re.sub(r"\s+", "", str(value)).casefold()


def is_security_code(value):
    return isinstance(value, str) and bool(re.fullmatch(CODE, value.strip(), re.I))


def literal_token(value, text):
    return bool(re.search(r"(?<![A-Za-z0-9])(?<![A-Za-z0-9][.\-])"+re.escape(value)+r"(?![A-Za-z0-9]|[.\-][A-Za-z0-9])", text, re.I))


def source_initialism(value,text):
    if not re.fullmatch(r'[A-Z]{2,6}',value):return False
    parts=[re.escape(char)+r'[A-Za-z]*' for char in value]
    separator=r'\W+(?:(?:of|the|and|for|to|in)\W+)?'
    return bool(re.search(r'\b'+separator.join(parts)+r'\b',text,re.I))


def name_pattern(name):
    return r"(?<![A-Za-z0-9])"+re.escape(name)+r"(?![A-Za-z0-9])"


def paired_declaration(name, ticker, text):
    """Return local relation evidence, rather than accept co-occurrence in a paragraph."""
    escaped = name_pattern(name)
    # Name followed by a short security declaration: English shares and Chinese notices.
    for match in re.finditer(escaped+r"\s*[（(\[]([^）)\]\n]{1,140})[）)\]]", text, re.I):
        body=match[1].strip()
        declarations=list(DECLARATION.finditer(body))
        if any(compact(m['ticker'])==compact(ticker) for m in declarations):return match[0]
        if re.fullmatch(re.escape(ticker),body,re.I):return match[0]
        if re.fullmatch(re.escape(ticker)+r'\s*[+\-]\s*\d+(?:\.\d+)?\s*%',body,re.I):return match[0]
    for match in re.finditer(escaped+r'\s*\(([^()\n]{1,60})\)\s*\(([^()\n]{1,100})\)',text,re.I):
        if len(match[1].strip())>=3 and not match[1].isupper() and compact(match[1]) in compact(name) and any(compact(m['ticker'])==compact(ticker) for m in DECLARATION.finditer(match[2])):return match[0]
    # Explicit grammatical subject + listing predicate; no free cross-sentence window.
    predicates=[
        r'\s+has received approval for the listing of its common stock on the New York Stock Exchange under the symbol\s+',
        r'(?:,[^.!?\n]{1,120},)?\s+(?:trade|trades)\s+(?:publicly\s+)?(?:at|on)\s+(?:'+EXCHANGE+r')\s*[:：]\s*',
    ]
    for predicate in predicates:
        for match in re.finditer(escaped+predicate+r'(?P<code>'+CODE+r')(?![A-Za-z0-9]|[.\-][A-Za-z0-9])',text,re.I):
            if compact(match['code'])==compact(ticker):return match[0]
    # Explicit label directly after the name; no arbitrary intervening company names.
    for match in re.finditer(escaped+r"[ \t,，:：\-]*(?:(?:is\s+listed\s+as|trades\s+under)\s+)?(?:"+EXCHANGE+r"\s*[:：]|"+LABEL+r"\s*[:：]?)[ \t]*(?P<code>"+CODE+r")(?![A-Za-z0-9.\-])",text,re.I):
        if compact(match['code'])==compact(ticker):return match[0]
    # Common announcement header, either order; short labelled fields only.
    for match in re.finditer(r"(?:证券代码|股票代码)\s*[:：]\s*(?P<code>\d{6})[ \t\n|，,;；]*(?:证券简称|股票简称)\s*[:：]\s*"+escaped,text):
        if match['code']==ticker:return match[0]
    for match in re.finditer(r"(?:证券简称|股票简称)\s*[:：]\s*"+escaped+r"[ \t\n|，,;；]*(?:证券代码|股票代码)\s*[:：]\s*(?P<code>\d{6})(?!\d)",text):
        if match['code']==ticker:return match[0]
    # Delimited rows: verify exact cells, never a free-text cross-company paragraph.
    for line in text.splitlines():
        cells=[cell.strip() for cell in re.split(r"\||\t",line) if cell.strip()]
        if 2<=len(cells)<=4 and any(compact(c)==compact(name) for c in cells) and any(compact(c)==compact(ticker) for c in cells):
            if all(compact(c) in {compact(name),compact(ticker)} or re.fullmatch(EXCHANGE,c,re.I) for c in cells):return line
    return None


def validate_securities(entities, source_text):
    result=[]
    for item in entities:
        if not isinstance(item,dict):raise ValueError("股票实体必须为对象")
        values={key:item.get(key) for key in ['name','ticker','exchange','quote']}
        if any(value is not None and not isinstance(value,str) for value in values.values()):raise ValueError("证券属性必须是字符串或null")
        name,ticker,exchange=[(values[key] or '').strip() or None for key in ['name','ticker','exchange']]
        quote=(values['quote'] or '').strip()
        if not name and not ticker:continue
        if not quote or compact(quote) not in compact(source_text):raise ValueError("证券quote必须引用原文")
        if name and not re.search(name_pattern(name),quote,re.I):raise ValueError("证券名称必须在对应引文中出现")
        if ticker:
            if not is_security_code(ticker):raise ValueError("股票代码格式不正确，不能使用公司全名")
            if not literal_token(ticker,quote):raise ValueError("原文无该股票代码，不能补猜")
            relation=paired_declaration(name,ticker,quote) if name else None
            if name and not relation:raise ValueError("名称与代码缺少局部对应证据，不能跨公司配对")
            if not name and not any(compact(m['ticker'])==compact(ticker) for m in DECLARATION.finditer(quote)):
                raise ValueError("独立代码须有明确的证券代码声明")
            evidence=relation or quote
            if exchange and not literal_token(exchange,evidence):raise ValueError("交易所不属于该主体的代码声明")
        elif exchange:
            # An exchange alone in a wide quote does not establish an issuer relation.
            raise ValueError("交易所缺少对应证券声明，未知时应为null")
        checked=dict(name=name,ticker=ticker,exchange=exchange,quote=quote)
        if checked not in result:result.append(checked)
    return result


def grounded_securities(entities, source_text):
    result=[]
    for item in entities:
        if not isinstance(item,dict):continue
        item=dict(item)
        try:
            checked=validate_securities([item],source_text)
        except ValueError:
            try:
                checked=validate_securities([dict(item,exchange=None)],source_text)
            except ValueError:checked=[]
            # Keep a literal company name, but never keep its unverifiable code/exchange.
            name=item.get('name')
            if not checked and isinstance(name,str) and name.strip() and re.search(name_pattern(name.strip()),source_text,re.I):
                checked=validate_securities([dict(name=name.strip(),ticker=None,exchange=None,quote=name.strip())],source_text)
        for entity in checked:
            if entity not in result:result.append(entity)
    return result


def retain_explicit_tickers(entities, source_text, selected_text=None):
    """Recover only explicit codes selected by useful notes or tied to a selected name."""
    result=[dict(item) for item in entities]
    for match in DECLARATION.finditer(source_text):
        ticker=match['ticker']
        # Labels are case-insensitive; prose after them is not automatically a ticker.
        if ticker.isalpha() and ticker!=ticker.upper():continue
        if not is_security_code(ticker):continue
        if any(compact(item.get('ticker') or '')==compact(ticker) for item in result):continue
        attached=False
        for item in result:
            name=item.get('name')
            if name and not item.get('ticker'):
                relation=paired_declaration(name,ticker,source_text)
                if relation:
                    proposed=dict(item,ticker=ticker,exchange=match.group('exchange'),quote=relation)
                    try:checked=validate_securities([proposed],source_text)[0]
                    except ValueError:continue
                    item.update(checked);attached=True;break
        if attached:continue
        if selected_text is not None and not literal_token(ticker,selected_text):
            # Recover adjacent names only when that exact name survived summarization.
            candidates=re.findall(r"([\u4e00-\u9fff]{2,30}|[A-Z][A-Za-z&.'\-]*(?:[ \t]+[A-Z][A-Za-z&.'\-]*){0,6})\s*[（(\[]",source_text)
            candidates+=re.findall(r'(?:证券简称|股票简称)\s*[:：]\s*([\u4e00-\u9fffA-Za-z*]{2,30})',source_text)
            for name in candidates:
                relation=paired_declaration(name,ticker,source_text)
                if relation and re.search(name_pattern(name),selected_text,re.I):
                    try:result.extend(validate_securities([dict(name=name,ticker=ticker,exchange=match.group('exchange'),quote=relation)],source_text));attached=True;break
                    except ValueError:continue
            if not attached:continue
        if not attached:
            result.extend(validate_securities([dict(name=None,ticker=ticker,exchange=match.group('exchange'),quote=match[0])],source_text))
    return result


def validate_summary_securities(note, source_text):
    """Clean unsupported optional stock labels; ordinary acronyms never gate an article."""
    content=note['content']
    actions=note.setdefault('security_cleanup',[]);removed=set()
    raw_codes={item.get('ticker') for item in note.get('securities',[]) if item.get('ticker') and is_security_code(item.get('ticker'))}
    names={item.get('name') for item in note.get('securities',[]) if isinstance(item.get('name'),str) and item.get('name')}
    names.update(word for word in note.get('keywords',[]) if len(word)>1 and not word.isupper() and re.search(name_pattern(word),source_text,re.I))
    names.update(match[1] for match in re.finditer(r"([A-Z][A-Za-z&.'\-]*(?:[ \t]+[A-Z][A-Za-z&.'\-]*){0,6})\s*[（(]",content)
        if re.search(name_pattern(match[1]),source_text,re.I))
    def record(code,reason):
        removed.add(code)
        action={'code':code,'reason':reason}
        if action not in actions:actions.append(action)
    for name in names:
        pattern=name_pattern(name)+r"\s*[（(\[]([^）)\]\n]{1,100})[）)\]]"
        def clean_pair(match):
            body=match[1].strip()
            declarations=list(DECLARATION.finditer(body));codes=[m['ticker'] for m in declarations]
            initials=''.join(word[0] for word in re.findall(r'[A-Za-z]+',name)).upper()
            if is_security_code(body) and (body in raw_codes or (body.upper() not in FINANCIAL_ABBREVIATIONS and body.upper()!=initials)):codes.append(body)
            for ticker in codes:
                relation=paired_declaration(name,ticker,source_text)
                if not relation or any(item.group('exchange') and not literal_token(item.group('exchange'),relation) for item in declarations):
                    record(ticker,'unsupported_company_code_label_removed')
                    return name
            return match[0]
        content=re.sub(pattern,clean_pair,content,flags=re.I)
    def clean_declaration(match):
        if literal_token(match['ticker'],source_text):return match[0]
        record(match['ticker'],'unsupported_security_declaration_removed');return ''
    content=DECLARATION.sub(clean_declaration,content)
    def clean_cashtag(match):
        if literal_token(match[1],source_text):return match[0]
        record(match[1],'unsupported_cashtag_removed');return ''
    content=re.sub(r'(?<!\w)\$([A-Z]{1,6}(?:[.\-][A-Z0-9]{1,5})?)(?!\w)',clean_cashtag,content)
    removed.update(code for code in raw_codes if not literal_token(code,source_text))
    # Softly remove unsupported code-shaped keywords, never reject otherwise useful prose.
    removed.update(word for word in note['keywords'] if re.fullmatch(r'[A-Z]{2,6}(?:[.\-][A-Z0-9]{1,5})?',word)
        and word not in FINANCIAL_ABBREVIATIONS and not literal_token(word,source_text) and not source_initialism(word,source_text))
    note['keywords']=[word for word in note['keywords'] if word not in removed]
    if not note['keywords']:note['keywords']=[name for name in names if re.search(name_pattern(name),source_text,re.I)]
    if not note['keywords']:
        note['keywords']=[value for value in [note.get('category'),note.get('industry')] if isinstance(value,str) and value.strip()][:2]
    note['content']=re.sub(r'[ \t]{2,}',' ',content).strip()
    if not note['keywords']:raise ValueError('缺少有效内容关键词')
    return note


def preserve_securities(final, notes, source_text, language):
    cleanups=[]
    for note in notes+[final]:
        for action in note.get('security_cleanup',[]):
            if action not in cleanups:cleanups.append(action)
    final['security_cleanup']=cleanups
    entities=grounded_securities([entity for note in notes+[final] for entity in note.get('securities',[])],source_text)
    selected='\n'.join(note.get('content','')+'\n'+'\n'.join(note.get('keywords',[])) for note in notes+[final])
    entities=retain_explicit_tickers(entities,source_text,selected)
    unique={}
    for entity in entities:
        key=(compact(entity.get('name')),compact(entity.get('ticker')))
        unique.setdefault(key,entity)
    # A fuller verified entity supersedes a same-name entry without a code.
    named_codes={compact(item['name']) for item in unique.values() if item['name'] and item['ticker']}
    final['securities']=[item for item in unique.values() if item['ticker'] or compact(item['name']) not in named_codes]
    missing=[]
    for item in final['securities']:
        for value in [item['name'],item['ticker']]:
            if value and value not in final['keywords']:final['keywords'].append(value)
        if item['ticker'] and not literal_token(item['ticker'],final['content']):
            missing.append(item['name']+' ('+item['ticker']+')' if item['name'] else item['ticker'])
    if missing:final['content']+=(' 原文提及的证券包括'+'、'.join(missing)+'。') if language=='zh' else (' Securities explicitly mentioned in the source include '+', '.join(missing)+'.')
    return validate_summary_securities(final,source_text)
