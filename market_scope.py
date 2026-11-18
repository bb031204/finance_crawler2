"""Typed market scope and bounded proof. A model claim is never human approval."""
import math
import re
from finance_agent.core import digest, now

VERSION = 'market-proof-1'
SCOPES = ('US_EQUITY', 'NASDAQ_LISTED', 'NYSE_LISTED')


def industry_scope_candidate(quote):
    """Necessary collective economic cues, never company identity or human approval."""
    broad=r'\b(industry|industries|sector|manufacturers|producers|lenders|banks|retailers|automakers|utilities|airlines|carriers|insurers|companies|firms|competitors|suppliers)\b|行业|产业|多家公司|制造商|银行业'
    effect=r'\b(demand|supply|shortages?|regulat\w*|tariffs?|tax\w*|competition|competitive|prices?|pricing|costs?|production|sales|grow\w*|declin\w*|reduc\w*|increas\w*|decreas\w*|requir\w*|restrict\w*|disrupt\w*|capacity|outlook|adopt\w*|adapt\w*|shift\w*|rate\w*|investment|financing|credit|technology|innovation|transition)\b|需求|供给|供需|成本|监管|关税|转型|竞争|利率|技术'
    return bool(re.search(broad,quote,re.I) and re.search(effect,quote,re.I))


def normalized(value):
    return re.sub(r'\s+', ' ', value).strip()


def broad_scope(scope, quote):
    """Conservative necessary cues; not a semantic truth or calibrated probability."""
    central = r'Federal Reserve|FOMC|美联储|联邦公开市场'
    monetary = r'federal funds|monetary policy|interest rates?|target range|balance sheet|reinvest\w*|taper\w*|国债|利率|缩表|再投资|货币政策'
    extra = r'(?:(?:U\.?S\.?|United States|American|美国).{0,120}(?:tariffs?|trade policy|fiscal|Treasury|bond yields?|credit markets?|government shutdown|recession|employment|payroll|retail sales|关税|财政|经济衰退|就业|国债))|(?:global|worldwide|全球).{0,80}(?:financial markets?|stock markets?|equity markets?|金融市场|股市)'
    equity = r'(?:U\.?S\.?|United States|American)\s+(?:stock|equity|financial)\s+markets?|Wall Street|美国股市|美股'
    macro = r'(?:U\.?S\.?|United States|American|美国).{0,80}(?:inflation|consumer prices|gross domestic product|unemployment|nonfarm|economic growth|Treasury yields|通胀|失业率|国内生产总值)'
    if scope == 'US_EQUITY':
        return bool(re.search(equity+'|'+macro+'|'+extra, quote, re.I) or (re.search(central,quote,re.I) and re.search(monetary,quote,re.I)))
    venue = r'NASDAQ|纳斯达克' if scope == 'NASDAQ_LISTED' else r'NYSE|New York Stock Exchange|纽约证券交易所'
    broad = r'listed (?:companies|stocks|securities)|all (?:companies|stocks|securities)|listing (?:rules|standards|requirements)|上市公司|上市规则|所有股票'
    return bool(re.search(venue, quote, re.I) and re.search(broad, quote, re.I))


def quote_candidates(source, maximum):
    """Literal sentence/window choices, covering the text without inventing a quotation."""
    result=[]
    def add(value):
        value=value.strip()
        if 8<=len(value)<=maximum and value not in result:result.append(value)
    for match in re.finditer(r'.+?(?:[.!?。！？](?=\s|$)|\n|$)',source,re.S):add(match[0])
    for offset in range(0,len(source),max(4,maximum//2)):
        start=offset
        if start and source[start-1].isascii() and source[start-1].isalnum() and source[start].isascii() and source[start].isalnum():
            while start<len(source) and source[start].isascii() and source[start].isalnum():start+=1
        end=min(len(source),start+maximum)
        if end<len(source) and source[end-1].isascii() and source[end-1].isalnum() and source[end].isascii() and source[end].isalnum():
            while end>start and source[end-1].isascii() and source[end-1].isalnum():end-=1
        add(source[start:end])
    return result


def constrain_market_schema(schema, source, evidence_definition):
    """Company-only inputs cannot decode a typed whole-market claim at all."""
    quotes=quote_candidates(source,350)
    selected=[q for q in quotes if any(broad_scope(s,q) for s in SCOPES)]
    scopes=[s for s in SCOPES if any(broad_scope(s,q) for q in selected)]
    if not scopes:
        schema['properties']['market_scope']['maxItems']=0
        schema['properties']['scope_evidence']['maxItems']=0
    else:
        schema['properties']['market_scope']['items']={'enum':scopes}
        evidence_definition['properties']['scope']={'enum':scopes}
        evidence_definition['properties']['evidence_quote']={'type':'string','enum':selected}
    return schema


def ground_market(note, source, basis='original_article_quote_verified_before_raw_cleanup', minimum=.75):
    flags = list(note.get('relation_quality_flags', [])); claims = []
    text = normalized(source); summary = normalized(note.get('content', ''))
    offered = note.get('market_scope', [])
    evidence = note.get('scope_evidence', [])
    if not isinstance(offered, list) or len(set(offered)) != len(offered) or not set(offered) <= set(SCOPES):
        offered = []; flags.append('market_scope_invalid')
    for scope in offered:
        rows = [r for r in evidence if isinstance(r, dict) and r.get('scope') == scope]
        if len(rows) != 1:
            flags.append('market_scope_evidence_missing_or_ambiguous'); continue
        row = rows[0]; quote = row.get('evidence_quote'); score = row.get('confidence'); rationale = row.get('rationale')
        if (not isinstance(quote, str) or not 8 <= len(quote) <= 350 or normalized(quote) not in text
                or not isinstance(rationale, str) or not 8 <= len(rationale) <= 300
                or isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score)
                or not minimum <= score <= 1 or not broad_scope(scope, quote)):
            flags.append('market_scope_evidence_unverified'); continue
        claims.append(dict(row, evidence_basis=basis,
            original_evidence_verified=basis == 'original_article_quote_verified_before_raw_cleanup',
            original_evidence_quote=quote if basis.startswith('original_article') else None,
            summary_evidence_quote=quote if normalized(quote) in summary else None,
            scope_status='automated_evidence_gate_passed_not_human_reviewed',
            confidence_interpretation='uncalibrated_model_self_assessment'))
    note.update(market_scope=[r['scope'] for r in claims], scope_evidence=claims,
                relation_quality_flags=sorted(set(flags)))
    return note


def evidence_pack(row, audit, source):
    """Save bounded quotes before raw cleanup, binding them to the exact record and body."""
    if audit.get('record_hash') != digest(row) or audit.get('source_body_hash') != digest(source.encode('utf-8')):
        raise ValueError('evidence_pack_source_binding_failed')
    evidence = []
    for field in ('entity_details', 'industries', 'events', 'industry_impacts', 'scope_evidence', 'impact_scope_evidence'):
        for index, item in enumerate(audit.get(field, [])):
            for key in ('quote', 'role_quote', 'industry_scope_quote', 'evidence_quote'):
                quote = item.get(key)
                if not quote: continue
                if not isinstance(quote, str) or len(quote) > 400 or normalized(quote) not in normalized(source):
                    raise ValueError('evidence_pack_quote_not_in_source:'+field+':'+key)
                evidence.append(dict(field=field, index=index, key=key, quote=quote,
                    quote_sha256=digest(quote.encode('utf-8'))))
    return dict(schema_version=VERSION, source_id=row['source_id'], url=row['url'],
        source_document_url=audit.get('source_document_url'), source_documents=audit.get('source_documents', []),
        record_hash=digest(row), source_body_hash=audit['source_body_hash'], raw_sha256=audit.get('raw_sha256'),
        title=row.get('title'), source_published_at=row['时间'], publication_quote=audit.get('date_quote'),
        model_digest=audit.get('model_digest'), prompt_version=audit.get('prompt_version'), code_version=audit.get('code_version'),
        llm_inferred_at=audit.get('llm_inferred_at'), evidence=evidence, created_at=now(),
        limitations='Bounded source quotes and hashes permit claim replay, not full-text reconstruction or human semantic approval.')


def validate_pack(pack, row, audit):
    if (pack.get('source_id') != row['source_id'] or pack.get('record_hash') != digest(row)
            or pack.get('source_body_hash') != audit.get('source_body_hash')
            or pack.get('schema_version') != VERSION):
        raise ValueError('evidence_pack_binding_invalid')
    for item in pack['evidence']:
        if digest(item['quote'].encode('utf-8')) != item['quote_sha256']:
            raise ValueError('evidence_quote_hash_changed')
    expected = [(f, i, k, x[k]) for f in ('entity_details','industries','events','industry_impacts','scope_evidence','impact_scope_evidence')
                for i, x in enumerate(audit.get(f, [])) for k in ('quote','role_quote','industry_scope_quote','evidence_quote') if x.get(k)]
    if expected != [(x['field'],x['index'],x['key'],x['quote']) for x in pack['evidence']]:
        raise ValueError('evidence_pack_incomplete')
    return True
