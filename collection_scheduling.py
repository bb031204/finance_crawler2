"""Fair work ordering and evidence/configuration-bound deterministic retry holds."""
from collections import defaultdict, deque
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from finance_agent.core import bounded, digest, now
from record_files import source_id


LONG_FORMS={'10-K','10-Q','20-F','40-F'}


def bucket(candidate):
    filing=candidate.get('sec_filing') or {}
    # search_year is a scheduling hint only, never publication evidence.
    year=str(filing.get('filing_date',''))[:4] or str(candidate.get('search_year') or 'unknown')
    if len(year)!=4 or not year.isdigit():year='unknown'
    form=str(filing.get('form','')).removesuffix('/A')
    long=form in LONG_FORMS or candidate.get('estimated_read_chunks',0)>4
    long=long or candidate.get('retry_guard',{}).get('kind')=='oversize'
    return (year,candidate.get('origin_market',''),(urlsplit(candidate['url']).hostname or '').lower(),
            'long' if long else 'standard')


def fair_pending(pending,last_bucket=None):
    """Round-robin years and source/type buckets; retain FIFO within a bucket.

    Return a token with every item so a batch of one can persist its next turn.
    No candidate is removed, re-dated, filtered for a ticker or marked complete.
    """
    queues=defaultdict(deque)
    for identity,candidate in pending.items():queues[bucket(candidate)].append((identity,candidate))
    by_year=defaultdict(list)
    for key in queues:by_year[key[:2]].append(key)
    for keys in by_year.values():keys.sort(key=lambda k:(k[3]=='long',k[2]))
    years=sorted(by_year)
    order=[by_year[y][slot] for slot in range(max((len(v) for v in by_year.values()),default=0))
           for y in years if slot<len(by_year[y])]
    if last_bucket and tuple(last_bucket) in order:
        cut=order.index(tuple(last_bucket))+1;order=order[cut:]+order[:cut]
    while order:
        next_order=[]
        for key in order:
            identity,candidate=queues[key].popleft()
            yield identity,candidate,list(key)
            if queues[key]:next_order.append(key)
        order=next_order


@lru_cache(maxsize=1)
def source_parser_version():
    root=Path(__file__).resolve().parent
    return digest({name:digest((root/name).read_bytes()) for name in
        ['web_sources.py','sec_documents.py','finance_agent/pdf_layout.py','finance_agent/pdf_ocr.py']})


def evidence_signature(root,candidate,previous=None):
    """A cache change may unlock retry; TTL eviction alone must not unlock it."""
    path=bounded(Path(root)/candidate.get('page_path','.runtime/simple/pages/'+source_id(candidate['url'])+'.json'),root)
    if path.is_file():
        stat=path.stat()
        return [str(path.relative_to(root)),stat.st_mtime_ns,stat.st_size]
    return previous


def retry_condition(root,candidate,cfg,model,kind,previous=None):
    previous=previous or {}
    evidence=evidence_signature(root,candidate,previous.get('evidence'))
    if kind=='oversize':
        policy=[cfg.fetch.max_text_chars,source_parser_version(),
                cfg.search.sec_edgar.include_exhibits,cfg.search.sec_edgar.max_exhibits_per_filing]
    else:
        from market_relations import extraction_fingerprint
        policy=[getattr(model,'extraction_fingerprint',None) or extraction_fingerprint(),
                getattr(model,'prompt_version',None),cfg.ollama.model_dump(),
                getattr(model,'model_digest',None) or previous.get('model_digest'),cfg.monitoring.model_dump()]
    value=dict(kind=kind,evidence=evidence,model_digest=getattr(model,'model_digest',None) or previous.get('model_digest'))
    value['fingerprint']=digest([candidate['url'],kind,evidence,policy])
    return value


def set_retry_guard(root,candidate,cfg,model,reason_code,error):
    kind='oversize' if 'max_text_chars' in str(error) else 'model' if reason_code=='model_error' else None
    if kind is None:return
    guard=retry_condition(root,candidate,cfg,model,kind)
    candidate['retry_guard']=dict(guard,at=now(),reason_code=reason_code)


def retry_unchanged(root,candidate,cfg,model):
    guard=candidate.get('retry_guard')
    if not guard:return False
    try:return retry_condition(root,candidate,cfg,model,guard['kind'],guard)['fingerprint']==guard.get('fingerprint')
    except (OSError,ValueError,KeyError):return False
