"""Exact-owned transient raw files; no raw archives, recursive deletes or full-text logs."""
import json
import re
from pathlib import Path
from datetime import datetime,timezone
from finance_agent.core import bounded,digest,dump,now
from finance_agent.safety import canonical
from record_files import source_id

POLICY_VERSION='raw-transient-1'


def allowed_page_path(root,path):
    if path.suffix!='.json':return False
    if path.parent==root/'.runtime/simple/pages':return True
    try:parts=path.relative_to(root/'.runtime/validation').parts
    except ValueError:return False
    return (bool(re.fullmatch(r'(?:history_date_binding|history_repair|output_budget|rich_language|record_title)_\d+.*',parts[0]))
        and (parts[1:-1]==('sources',) or parts[1:-1]==('.runtime','simple','pages')))


def register_page(root,path,page,identity):
    root=Path(root).resolve();path=bounded(path,root)
    if not allowed_page_path(root,path):raise ValueError('原文路径不在白名单')
    if not path.is_file():raise ValueError('原文缓存尚未写入')
    manifest=dict(source_id=identity,url=canonical(page['url']),content_hash=digest(page['text'].encode('utf-8')),
        raw_sha256=page.get('raw_sha256'),created_at=page.get('fetched_at') or now(),
        files=[dict(path=str(path.relative_to(root)),sha256=digest(path.read_bytes()))],policy_version=POLICY_VERSION)
    dump(bounded(root/'.runtime/simple/raw_manifest'/(identity+'.json'),root),manifest)
    return manifest


def cleanup_page(root,path,page,identity,reason='success',audit=None):
    root=Path(root).resolve();path=bounded(path,root)
    result=dict(source_id=identity,content_hash=digest(page['text'].encode('utf-8')),raw_sha256=page.get('raw_sha256'),
        raw_cleanup_status='not_found',raw_deleted_at=None,raw_deleted_paths=[],raw_cleanup_errors=[],raw_cleanup_reason=reason,raw_policy_version=POLICY_VERSION)
    if reason=='success' and audit is not None and audit.get('market_scope_version')=='market-proof-1':
        from market_scope import evidence_pack,validate_pack
        records=audit.get('record_paths',[])
        if not records:raise ValueError('cleanup_requires_committed_record')
        row=json.loads(bounded(root/records[0],root).read_text(encoding='utf-8'))
        proof=evidence_pack(row,audit,page['text'])
        proof_path=bounded(root/'.runtime/simple/evidence'/(identity+'.json'),root)
        dump(proof_path,proof)
        validate_pack(json.loads(proof_path.read_text(encoding='utf-8')),row,audit)
        audit['evidence_pack']={'path':str(proof_path.relative_to(root)),'sha256':digest(proof_path.read_bytes()),'schema_version':'market-proof-1'}
        dump(bounded(root/'.runtime/simple/audit'/(identity+'.json'),root),audit)
    candidates=[]
    if not allowed_page_path(root,path):raise ValueError('原文路径不在白名单')
    if path.is_file():
        value=json.loads(path.read_text(encoding='utf-8'))
        if canonical(value.get('url',''))!=canonical(page['url']) or digest(value.get('text','').encode('utf-8'))!=result['content_hash']:
            result['raw_cleanup_errors'].append(dict(path=str(path.relative_to(root)),code='ownership_mismatch'))
        else:candidates.append((path,digest(path.read_bytes())))
    # Exact old validation byte files only. Nested arbitrary/user files are never selected.
    if page.get('raw_sha256'):
        for key in {identity,source_id(page['url']),source_id((audit or {}).get('request_url') or page['url'])}:
            for ext in ['.html','.htm','.pdf','.txt']:
                item=bounded(root/'.runtime/validation'/(key+ext),root)
                if item.is_file():
                    if digest(item.read_bytes())==page['raw_sha256']:candidates.append((item,page['raw_sha256']))
                    else:result['raw_cleanup_errors'].append(dict(path=str(item.relative_to(root)),code='legacy_raw_hash_mismatch'))
        for extension in ['.html','.htm','.pdf','.txt']:
            item=bounded(path.with_suffix(extension),root)
            if item.is_file() and digest(item.read_bytes())==page['raw_sha256']:candidates.append((item,page['raw_sha256']))
    for extension in ['.txt','.ocr.txt']:
        item=bounded(path.with_suffix(extension),root)
        if item.is_file() and digest(item.read_bytes())==result['content_hash']:candidates.append((item,result['content_hash']))
    # Source-bound, validated model cache is temporary too. Never delete unbound legacy entries by guess.
    indexes=[]
    for key in {identity,source_id(page['url'])}:
        index_path=bounded(root/'.runtime/simple/model_cache_index'/(key+'.json'),root)
        if not index_path.is_file():continue
        indexes.append(index_path)
        for entry in json.loads(index_path.read_text(encoding='utf-8')):
            try:
                item=bounded(root/entry['path'],root)
                if item.parent!=root/'.runtime/simple/model_cache' or item.suffix!='.json' or not item.is_file():continue
                value=json.loads(item.read_text(encoding='utf-8'))
                if (value.get('cache_format')==2 and value.get('source_id') in {identity,source_id(page['url'])}
                        and digest(item.read_bytes())==entry['sha256']):
                    candidates.append((item,digest(item.read_bytes())))
            except (OSError,ValueError,TypeError):continue
    for item,sha in dict(candidates).items():
        relative=str(item.relative_to(root))
        try:
            if not item.is_file() or digest(item.read_bytes())!=sha:raise OSError('changed')
            item.unlink()
            if item.exists():raise OSError('still_present')
            result['raw_deleted_paths'].append(relative)
        except OSError:result['raw_cleanup_errors'].append(dict(path=relative,code='delete_failed_or_changed'))
    if result['raw_cleanup_errors']:result['raw_cleanup_status']='failed'
    elif result['raw_deleted_paths']:result['raw_cleanup_status']='deleted'
    result['raw_deleted_at']=now()
    log_path=bounded(root/'.runtime/simple/raw_cleanup'/(identity+'.json'),root)
    dump(log_path,result)
    if audit is not None:
        audit.update({k:v for k,v in result.items() if k.startswith('raw_')})
        audit['raw_deleted']=sorted(set(audit.get('raw_deleted',[])+result['raw_deleted_paths']))
        audit['cleanup_errors']=result['raw_cleanup_errors']
        dump(bounded(root/'.runtime/simple/audit'/(identity+'.json'),root),audit)
    manifest=bounded(root/'.runtime/simple/raw_manifest'/(identity+'.json'),root)
    if not result['raw_cleanup_errors']:manifest.unlink(missing_ok=True)
    if not result['raw_cleanup_errors']:
        for index_path in indexes:index_path.unlink(missing_ok=True)
    return result


def cleanup_candidate(root,identity,candidate):
    root=Path(root).resolve();path=bounded(root/candidate.get('page_path','.runtime/simple/pages/'+identity+'.json'),root)
    if not path.is_file():return None
    page=json.loads(path.read_text(encoding='utf-8'))
    if not any(canonical(page.get(key,''))==canonical(candidate['url']) for key in ['url','request_url'] if page.get(key)):return None
    return cleanup_page(root,path,page,identity,'rejected')


def reconcile_missing_pages(root):
    """Repair cleanup audit after a crash *after* unlink, using hashes only."""
    results=[];folder=root/'.runtime/simple/raw_manifest'
    for manifest_path in folder.glob('*.json') if folder.exists() else []:
        try:
            manifest_path=bounded(manifest_path,root);manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
            identity=manifest['source_id'];paths=[bounded(root/x['path'],root) for x in manifest['files']]
            if any(not allowed_page_path(root,p) for p in paths) or any(p.exists() for p in paths):continue
            audit_path=bounded(root/'.runtime/simple/audit'/(identity+'.json'),root)
            if not audit_path.exists():audit_path=bounded(root/'.runtime/simple/audit'/(source_id(manifest['url'])+'.json'),root)
            audit=json.loads(audit_path.read_text(encoding='utf-8')) if audit_path.exists() else {}
            identity=audit.get('source_id',identity)
            proof_path=bounded(root/'.runtime/simple/raw_cleanup'/(identity+'.json'),root)
            proof=json.loads(proof_path.read_text(encoding='utf-8')) if proof_path.exists() else {}
            verified=bool(audit.get('record_hash') and audit.get('record_paths') and audit.get('source_body_hash')==manifest['content_hash'])
            for rel in audit.get('record_paths',[]):
                record=bounded(root/rel,root)
                if not record.exists() or digest(json.loads(record.read_text(encoding='utf-8')))!=audit.get('record_hash'):verified=False
            if not verified and proof.get('content_hash')!=manifest['content_hash']:continue
            deleted=list(proof.get('raw_deleted_paths',[]));errors=[];indexes=[]
            for key in {manifest['source_id'],source_id(manifest['url'])}:
                index=bounded(root/'.runtime/simple/model_cache_index'/(key+'.json'),root)
                if not index.exists():continue
                indexes.append(index)
                for entry in json.loads(index.read_text(encoding='utf-8')):
                    path=bounded(root/entry['path'],root)
                    if path.parent!=root/'.runtime/simple/model_cache' or not path.is_file():continue
                    value=json.loads(path.read_text(encoding='utf-8'))
                    if value.get('cache_format')!=2 or value.get('source_id')!=key or digest(path.read_bytes())!=entry['sha256']:continue
                    try:path.unlink();deleted.append(str(path.relative_to(root)))
                    except OSError:errors.append(dict(path=str(path.relative_to(root)),code='delete_failed'))
            result=dict(source_id=identity,content_hash=manifest['content_hash'],raw_sha256=manifest.get('raw_sha256'),
                raw_cleanup_status='failed' if errors else 'deleted' if deleted else 'not_found',
                raw_deleted_at=proof.get('raw_deleted_at') or now(),raw_deleted_paths=sorted(set(deleted)),raw_cleanup_errors=errors,
                raw_cleanup_reason='post_unlink_recovery',raw_policy_version=POLICY_VERSION)
            dump(proof_path,result)
            if verified:
                audit.update({k:v for k,v in result.items() if k.startswith('raw_')})
                audit['raw_deleted']=result['raw_deleted_paths'];audit['cleanup_errors']=errors;dump(audit_path,audit)
            if not errors:
                manifest_path.unlink(missing_ok=True)
                for index in indexes:index.unlink(missing_ok=True)
            results.append(result)
        except (OSError,ValueError,TypeError,KeyError):results.append(dict(source_id=manifest_path.stem,raw_cleanup_status='failed',raw_cleanup_errors=[dict(code='cleanup_audit_recovery_failed')]))
    return results


def cleanup_transients(root,ttl_hours=24,clock=None,due_only=False):
    """Called under project writer lock. No inference/network and no formal JSON rewrites."""
    if not 0<ttl_hours<=24:raise ValueError('原文TTL须大于0且不超过24小时')
    root=Path(root).resolve();clock=clock or datetime.now(timezone.utc)
    checkpoint=root/'.runtime/simple/raw_sweep_checkpoint.json'
    if due_only and checkpoint.is_file():
        previous=json.loads(checkpoint.read_text(encoding='utf-8'))
        if (clock-datetime.fromisoformat(previous['at'])).total_seconds()<300:
            return dict(deleted=0,failed=0,skipped_until_next_sweep=True)
    state_path=root/'.runtime/simple/state.json'
    state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    folder=root/'.runtime/simple/pages';results=[]
    pages=list(folder.glob('*.json')) if folder.exists() else []
    validation=root/'.runtime/validation'
    if validation.exists():
        pages.extend(validation.glob('*/.runtime/simple/pages/*.json'));pages.extend(validation.glob('*/sources/*.json'))
    for path in sorted(pages):
        try:
            path=bounded(path,root)
            if not allowed_page_path(root,path):continue
            page=json.loads(path.read_text(encoding='utf-8'));url=canonical(page['url']);identity=path.stem
            if not isinstance(page['text'],str):continue
            candidate=state.get('pending',{}).get(identity,{})
            owned=identity==source_id(url) or candidate.get('page_path')==str(path.relative_to(root))
            if not owned:continue
            audit_path=bounded(root/'.runtime/simple/audit'/(identity+'.json'),root)
            audit=json.loads(audit_path.read_text(encoding='utf-8')) if audit_path.is_file() else {}
            if not audit:
                audit_path=bounded(root/'.runtime/simple/audit'/(source_id(url)+'.json'),root)
                audit=json.loads(audit_path.read_text(encoding='utf-8')) if audit_path.is_file() else {}
            # Success can be rechecked from every recorded replica, never from a saved flag alone.
            committed=bool(audit.get('record_hash') and audit.get('record_paths'))
            for rel in audit.get('record_paths',[]):
                file=bounded(root/rel,root)
                if not file.is_file() or digest(json.loads(file.read_text(encoding='utf-8')))!=audit['record_hash']:committed=False
            committed=committed and audit.get('source_body_hash')==digest(page['text'].encode('utf-8'))
            created=page.get('fetched_at')
            stamp=datetime.fromisoformat(created.replace('Z','+00:00')) if created else datetime.fromtimestamp(path.stat().st_mtime,timezone.utc)
            if stamp.tzinfo is None:stamp=datetime.fromtimestamp(path.stat().st_mtime,timezone.utc)
            expired=(clock-stamp.astimezone(timezone.utc)).total_seconds()>=ttl_hours*3600
            rejected=state.get('records',{}).get(identity,{}).get('status') in {'rejected','inactive'}
            if committed or expired or rejected:
                results.append(cleanup_page(root,path,page,audit['source_id'] if committed else identity,'success' if committed else 'rejected' if rejected else 'ttl',audit if committed else None))
        except (OSError,ValueError,KeyError,TypeError):
            results.append(dict(source_id=path.stem,raw_cleanup_status='failed',raw_cleanup_errors=[dict(code='unreadable_or_unverified')]))
    results.extend(reconcile_missing_pages(root))
    sanitized=0
    errors=root/'.runtime/simple/model_errors'
    for path in errors.glob('*.json') if errors.exists() else []:
        if not re.fullmatch(r'[a-f0-9]{64}_[01]',path.stem):continue
        try:path=bounded(path,root);value=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):continue
        if isinstance(value,dict) and 'response' in value and 'purpose' in value and 'error' in value:
            dump(path,dict(purpose=value['purpose'],error='legacy_error_redacted',response_hash=digest(value['response']),at=now(),raw_policy_version=POLICY_VERSION));sanitized+=1
    report=dict(policy_version=POLICY_VERSION,ttl_hours=ttl_hours,checked_at=now(),results=results,
        deleted=sum(x['raw_cleanup_status']=='deleted' for x in results),failed=sum(x['raw_cleanup_status']=='failed' for x in results),sanitized_error_logs=sanitized)
    dump(root/'logs/latest_raw_cleanup.json',report)
    dump(checkpoint,dict(at=clock.isoformat(),policy_version=POLICY_VERSION))
    return report
