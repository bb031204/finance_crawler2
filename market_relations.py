"""Ground article-local relations; never manufacture a security master."""
import re
from functools import lru_cache
from finance_agent.core import digest
from interface_schema import SCHEMA_VERSION, EXTRACTION_VERSION, EventNote, IndustryImpactNote


def normalized_content_hash(text):
    return digest(re.sub(r'\s+', ' ', text).strip().encode('utf-8'))


def prompt_version():
    from pathlib import Path
    folder=Path(__file__).resolve().parent/'prompts'
    return digest({p.name:p.read_text(encoding='utf-8') for p in sorted(folder.glob('*.md'))})


@lru_cache(maxsize=1)
def code_version():
    from pathlib import Path
    root=Path(__file__).resolve().parent
    files=list(root.glob('*.py'))+list((root/'finance_agent').glob('*.py'))
    return digest({p.relative_to(root).as_posix():digest(p.read_bytes()) for p in sorted(files)})


EXTRACTION_DEPENDENCIES = (
    'qwen_agent.py', 'market_relations.py', 'interface_schema.py',
    'market_scope.py', 'record_quality.py', 'security_grounding.py', 'analysis_metadata.py',
    'numeric_qualifiers.py', 'summary_quality.py', 'financial_noise.py',
    'date_evidence.py', 'record_time.py', 'finance_agent/core.py', 'industry_dictionary.py',
)


@lru_cache(maxsize=8)
def extraction_fingerprint(root=None):
    """Cache only semantic dependencies; keep the full repository hash for audit.

    Prompt, schema, model and complete request payload remain part of Qwen.ask's
    key. Source parsing changes are reflected in that payload, not CLI hashes.
    """
    from pathlib import Path
    root=Path(root) if root is not None else Path(__file__).resolve().parent
    return digest({name:digest((root/name).read_bytes()) for name in EXTRACTION_DEPENDENCIES})


def ground_relations(note, source, raw_entities=None):
    """Invalid optional relations are omitted, with machine-readable reasons."""
    from record_quality import compact
    from security_grounding import name_pattern
    flags=list(note.get('relation_quality_flags',[]))
    raw_entities=raw_entities if raw_entities is not None else note.get('entity_details',note.get('securities',[]))
    role_cues={'customer':r'customer|client|客户|采购方','supplier':r'suppl\w*|vendor|供应|供货',
        'partner':r'partner|collaborat\w*|合作|联营','competitor':r'compet\w*|rival|竞争|对手',
        'issuer':r'issuer|issuing|发行人|发行方'}
    entities=[]
    for item in note.get('securities',[]):
        detail=dict(item,entity_type='unknown',entity_role='unknown',role_quote=None)
        matches=[r for r in raw_entities if r.get('name')==item.get('name') and r.get('ticker')==item.get('ticker')]
        for candidate in matches:
            quote=candidate.get('role_quote') or ''
            if (quote and len(quote)<=180 and compact(quote) in compact(source)
                    and item.get('name') and re.search(name_pattern(item['name']),quote,re.I)):
                detail.update(entity_type=candidate.get('entity_type','unknown'),
                    entity_role=candidate.get('entity_role','unknown'),role_quote=quote)
                role=detail['entity_role']
                if role in role_cues and not re.search(role_cues[role],quote,re.I):
                    detail['entity_role']='unknown';flags.append('entity_role_unverified')
                break
        if detail not in entities:entities.append(detail)
    events=[]; index_map={}
    for index, item in enumerate(note.get('events',[])):
        try:
            event=EventNote.model_validate(item).model_dump()
            if compact(event['quote']) not in compact(source):raise ValueError('quote')
            if event.get('subject') and compact(event['subject']) not in compact(source):raise ValueError('subject')
            # Reject unsupported high-consequence labels without replacing them with a guess.
            required={'regulation_policy':r'\b(?:regulat\w*|policy|policies|law|rules?|SEC|commission)\b|监管|政策|条例|规定|证监',
                'monetary_fiscal':r'\b(?:central bank|federal reserve|federal funds rate|policy rate|interest rate|fiscal|tax|taxes|treasury|monetary)\b|央行|利率|财政|税收|货币政策'}
            if event['event_type'] in required and not re.search(required[event['event_type']],event['quote'],re.I):
                event['event_type']='unknown';flags.append('event_type_unverified')
            # Explicit arguments must be in the evidence; semantic assignments are marked inferred.
            if event['evidence_type']=='explicit':
                for key in ['subject','action','object','key_period','key_values','conditions']:
                    if event.get(key) and compact(event[key]) not in compact(event['quote']):
                        event[key]=None;flags.append('event_argument_omitted')
            if event not in events:events.append(event)
            index_map[index]=events.index(event)
        except (ValueError,TypeError,KeyError):flags.append('event_unverified')
    impacts=[]; sector_names={x['name'].casefold() for x in note.get('industries',[])}
    for item in note.get('industry_impacts',[]):
        try:
            impact=IndustryImpactNote.model_validate(item).model_dump()
            if compact(impact['quote']) not in compact(source):raise ValueError('quote')
            if impact['industry_name_original'].casefold() not in sector_names and not impact.get('industry_key'):raise ValueError('industry')
            mechanism=impact['mechanism']
            if len(re.sub(r'\s+','',mechanism))<8 or (not re.search(r'[\u4e00-\u9fff]',mechanism) and len(re.findall(r'[A-Za-z]+',mechanism))<4):raise ValueError('mechanism_label_only')
            if re.search(r'股价|收益率|买入|卖出|涨停|price target|stock price|share price|buy rating|sell rating',impact['mechanism'],re.I):raise ValueError('forecast')
            # A claimed driver must occur in its own evidence, not merely somewhere in the article.
            for driver in [r'federal funds rate|interest rates?|borrowing costs?|利率|融资成本',
                    r'tariff\w*|关税',r'demand|orders?|需求|订单',r'production|capacity|产能|产量']:
                if re.search(driver,impact['mechanism'],re.I) and not re.search(driver,impact['quote']+' '+(impact.get('industry_scope_quote') or ''),re.I):
                    from market_scope import industry_scope_candidate
                    scope_quote=impact.get('industry_scope_quote') or ''
                    if (not impact.get('industry_key') or not scope_quote or compact(scope_quote) not in compact(source)
                            or not industry_scope_candidate(scope_quote)):
                        raise ValueError('mechanism_driver')
                    flags.append('industry_mechanism_semantic_not_literal_driver')
            if impact['event_index'] is not None:
                impact['event_index']=index_map.get(impact['event_index'])
            if impact not in impacts:impacts.append(impact)
        except (ValueError,TypeError,KeyError) as exc:
            flags.append('industry_impact_unverified')
            if str(exc) in ('quote','industry','mechanism_label_only','forecast'):
                flags.append('industry_rejection_'+str(exc))
    note.update(entity_details=entities,events=events,industry_impacts=impacts,relation_quality_flags=sorted(set(flags)))
    from market_scope import ground_market
    return ground_market(note,source)


def preserve_relations(final, notes, source):
    events=[]; impacts=[]; entities=[]; flags=[]; claims=[]
    for note in notes:
        mapping={}
        for i,event in enumerate(note.get('events',[])):
            if event not in events:events.append(event)
            mapping[i]=events.index(event)
        for item in note.get('industry_impacts',[]):
            item=dict(item)
            if item.get('event_index') is not None:item['event_index']=mapping.get(item['event_index'])
            if item not in impacts:impacts.append(item)
        for item in note.get('entity_details',[]):
            if item not in entities:entities.append(item)
        for claim in note.get('scope_evidence',[]):
            if claim not in claims:claims.append(claim)
        flags.extend(note.get('relation_quality_flags',[]))
    chosen={}
    for claim in claims:
        key=claim['scope']
        if key not in chosen or claim['confidence']>chosen[key]['confidence']:chosen[key]=claim
    final.update(market_scope=sorted(chosen),scope_evidence=[chosen[k] for k in sorted(chosen)],events=events,industry_impacts=impacts,relation_quality_flags=flags)
    return ground_relations(final,source,entities)


def relation_audit(note, page, row, model):
    active_prompt_version=getattr(model,'prompt_version',None)
    return dict(schema_version=SCHEMA_VERSION,extraction_logic_version=EXTRACTION_VERSION,
        code_version=getattr(model,'code_version',None),
        extraction_fingerprint=getattr(model,'extraction_fingerprint',None),
        llm_inferred_at=getattr(model,'inferred_by_source',{}).get(row['source_id']),
        prompt_version=active_prompt_version,content_hash=normalized_content_hash(page['text']),
        source_type=page.get('source_type') or ('sec' if 'sec.gov/' in row['url'] else 'web'),
        entity_details=note.get('entity_details',[]),events=note.get('events',[]),
        market_scope=note.get('market_scope',[]),scope_evidence=note.get('scope_evidence',[]),market_scope_version='market-proof-1',
        industry_impacts=note.get('industry_impacts',[]),industry_dictionary=note.get('industry_dictionary'),relation_quality_flags=note.get('relation_quality_flags',[]),
        event_cluster_id=None,extraction_version=digest([row['source_id'],normalized_content_hash(page['text']),
            getattr(model,'model_digest',None),active_prompt_version,getattr(model,'code_version',None),SCHEMA_VERSION,row.get('时间信息',{}).get('feature_available_at')]))
