"""Light source grounding and post-commit raw-cache cleanup."""
import json
import re
from urllib.parse import urlsplit
from finance_agent.core import bounded,digest,dump,now
from finance_agent.safety import canonical
from record_files import source_id
from date_evidence import VALIDATION_VERSION,NON_PUBLICATION


def compact(value):
    return re.sub(r"\s+","",str(value)).casefold()


def language_hint(text):
    sample=text[:16000]
    chinese=sum("\u4e00"<=c<="\u9fff" for c in sample)
    latin=sum(c.isascii() and c.isalpha() for c in sample)
    return "zh" if chinese>=max(8,(chinese+latin)*0.12) else "en"


def primary_article_text(text,url):
    """Remove a verified publisher's related-release widget, not arbitrary prose."""
    host=(urlsplit(url).hostname or "").lower()
    if host=="prnewswire.com" or host.endswith(".prnewswire.com"):
        marker=re.search(r"×\s*Modal title\s+Also from this source\b",text,re.I)
        if marker and "More Releases From This Source" in text[marker.end():]:
            return text[:marker.start()].rstrip()
    return text


def ticker_hints(text):
    pattern=r"(?:NASDAQ|NYSE(?:\s+ARCA)?|AMEX|OTC|证券代码|股票代码|Stock\s+(?:Code|Symbol)|Ticker)\s*[:：]?\s*[A-Za-z0-9.$_-]{1,14}"
    return [text[max(0,m.start()-35):m.end()+25] for m in list(re.finditer(pattern,text,re.I))[:4]]


from security_grounding import (validate_securities, grounded_securities, retain_explicit_tickers,
    preserve_securities, SECURITY_VALIDATION_VERSION)


def prepare_record_audit(root, row, note, page, candidate, model, market, paths=()):
    """Prepare evidence before committing JSON; no deletion or data writes."""
    root=root.resolve();identity=row['source_id']
    audit_path=bounded(root/".runtime/simple/audit"/(identity+".json"),root)
    previous=json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else {}
    stamp=now()
    audit={"source_id":identity,"url":row["url"],"canonical_market":market,"origin_market":candidate.get("origin_market"),
        "source_published_at":row["时间"],"date_quote":note.get("date_quote", ""),"date_kind":note.get("date_kind"),"publication_validation_version":VALIDATION_VERSION,
        "source_body_hash":digest(page["text"].encode("utf-8")),"raw_sha256":page.get("raw_sha256"),
        "first_seen_at":candidate.get("first_seen_at"),"fetched_at":page.get("fetched_at"),
        "first_extracted_at":previous.get("first_extracted_at",stamp),"extracted_at":stamp,
        "feature_available_at":row.get("时间信息",{}).get("feature_available_at",stamp),"model":getattr(getattr(model,"cfg",None),"model",None),
        "model_digest":getattr(model,"model_digest",None),"language":language_hint(page["text"]),
        "securities":note.get("securities",[]),"industries":note.get("industries",[]),
        "impact_scope":note.get("impact_scope",[]),"analysis_schema_version":1,"security_validation_version":SECURITY_VALIDATION_VERSION,
        "security_cleanup":note.get('security_cleanup',[]),"record_hash":digest(row),
        "summary_cleanup":note.get('summary_cleanup',[]),
        "record_paths":[str(p.relative_to(root)) for p in paths],"raw_deleted":[],"cleanup_errors":[]}
    if page.get("document_url"): audit["source_document_url"] = page["document_url"]
    if page.get('sec_documents'):
        audit['source_documents']=page['sec_documents']
        audit['sec_document_version']=page['sec_document_version']
        audit['sec_document_policy']=page['sec_document_policy']
        audit['sec_index_sha256']=page.get('sec_index_sha256')
        audit['sec_unread_exhibits']=page.get('sec_unread_exhibits',[])
    audit['numeric_evidence_version']=note.get('numeric_evidence_version')
    audit['request_url']=canonical(candidate.get('url',page['url']))
    if 'title' in row:
        audit['title']=row['title']
        audit['title_origin']=page.get('title_origin','legacy_page_title')
    if page.get('article_binding_version'):audit['article_binding_version']=page['article_binding_version']
    if page.get('removed_inline_xbrl_headers'):
        audit['source_cleaning']={'removed_nonvisible_inline_xbrl_headers':page['removed_inline_xbrl_headers']}
    from market_relations import relation_audit
    audit.update(relation_audit(note,page,row,model))
    from market_scope import evidence_pack
    evidence_pack(row,audit,page['text'])  # reject broken quotes before the formal JSON transaction
    return audit


def verified_record(root,row):
    path=bounded(root/'.runtime/simple/audit'/(row['source_id']+'.json'),root)
    try:return json.loads(path.read_text(encoding='utf-8')).get('record_hash')==digest(row)
    except (OSError,ValueError):return False


def finalize_record(root, dataset, row, note, page, candidate, model, page_path, delete_raw=True):
    """Delete only known source files after the recoverable data/audit commit is verified."""
    root=root.resolve();identity=row['source_id'];paths=dataset.replicas.get(identity,{})
    if not paths:raise ValueError('正式记录尚未写入，不能清理原文')
    for path in paths.values():
        if json.loads(path.read_text(encoding='utf-8'))!=row:raise ValueError('正式副本校验失败，保留原文')
    audit_path=bounded(root/'.runtime/simple/audit'/(identity+'.json'),root)
    audit=json.loads(audit_path.read_text(encoding='utf-8')) if audit_path.is_file() else {}
    if audit.get('record_hash')!=digest(row) or audit.get('source_body_hash')!=digest(page['text'].encode('utf-8')):
        audit=prepare_record_audit(root,row,note,page,candidate,model,dataset.rows[identity][0],paths.values())
        dump(audit_path,audit)
    if json.loads(audit_path.read_text(encoding="utf-8"))["record_hash"]!=digest(row):raise ValueError("审计写入失败，保留原文")
    if not delete_raw:raise ValueError('提炼成功后不允许长期保留原文')
    from market_scope import evidence_pack,validate_pack
    proof_path=bounded(root/'.runtime/simple/evidence'/(identity+'.json'),root)
    proof=evidence_pack(row,audit,page['text'])
    dump(proof_path,proof)
    loaded=json.loads(proof_path.read_text(encoding='utf-8'))
    validate_pack(loaded,row,audit)
    audit['evidence_pack']={'path':str(proof_path.relative_to(root)),'sha256':digest(proof_path.read_bytes()),'schema_version':loaded['schema_version']}
    dump(audit_path,audit)
    if json.loads(audit_path.read_text(encoding='utf-8')).get('evidence_pack')!=audit['evidence_pack']:raise ValueError('evidence_pack_readback_failed')
    from raw_cleanup import cleanup_page
    cleanup_page(root,page_path,page,identity,'success',audit)
    return audit


def quarantine_invalid_publication_records(crawler):
    """Recheck known bad old evidence, preserving every original JSON for review."""
    repaired=[]
    for identity,(market,row) in list(crawler.dataset.rows.items()):
        path=bounded(crawler.root/".runtime/simple/audit"/(identity+".json"),crawler.root)
        if not path.is_file():continue
        audit=json.loads(path.read_text(encoding="utf-8"))
        hashes={digest(row),digest({k:v for k,v in row.items() if k!="时间信息"})}
        if audit.get("record_hash") not in hashes or not NON_PUBLICATION.search(audit.get("date_quote","")):continue
        reason="原日期依据为修改/更新/生效时间，未证明原始发布时间；保留副本并重新核验"
        target=crawler.dataset.quarantine(identity,reason)
        origin=next((m for m in crawler.cfg.monitoring.markets if crawler.sources.allowed(row["url"],m)),None)
        if origin:
            crawler.queue({"url":row["url"],"title":row.get('title') or " / ".join(row["关键词"][:4]),"title_is_source":bool(row.get('title')),"snippet":"需重新核对原始发布日期", "origin_market":origin,"reprocess":True,
                "first_seen_at":row.get("时间信息",{}).get("first_seen_at")})
        crawler.state["records"][identity]={"status":"needs_review","reason":reason,"at":now()}
        repaired.append({"source_id":identity,"path":str(target.relative_to(crawler.root)),"reason":reason})
    if repaired:crawler.persist()
    return repaired
