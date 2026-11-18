"""Rebuildable, recoverable Parquet projection of committed JSON and sidecars."""
import json
import sys
import time
from pathlib import Path
from datetime import datetime,timezone
from finance_agent.core import bounded,digest,dump,now
from interface_schema import SCHEMA_VERSION,public_schema_version,EVENT_TYPES


def arrow():
    vendor=str(Path(__file__).resolve().parent/'vendor')
    if vendor not in sys.path:sys.path.insert(0,vendor)
    import pyarrow as pa
    import pyarrow.parquet as pq
    return pa,pq


def schemas():
    pa,_=arrow();s=pa.string();ls=pa.list_(s);ts=pa.timestamp('us',tz='UTC');dt=pa.date32()
    def schema(names,extra=None):
        fields={k:s for k in names.split()};fields.update(extra or {});return pa.schema(list(fields.items()))
    return {
      'article_metadata':schema('source_id url title market source_type language published_at timezone publication_precision availability_basis content_hash raw_sha256 schema_version record_schema_version prompt_version model_name model_digest code_version extraction_version event_cluster_id summary record_hash',
          {k:ts for k in ['first_seen_at','fetched_at','llm_inferred_at','extracted_at','commit_verified_at','feature_available_at']}|{k:ls for k in ['event_types','impact_scope','keywords','quality_flags']}),
      'article_entities':schema('source_id entity_id entity_name entity_type entity_role ticker exchange cik security_id evidence_type evidence_quote role_evidence_quote mapping_status',{'valid_from':dt,'valid_to':dt}),
      'article_industries':schema('source_id industry_mention_id industry_key industry_name_original evidence_type industry_standard industry_code industry_name_standard industry_version dictionary_sha256 dictionary_version evidence_basis original_evidence_quote summary_evidence_quote mapping_status evidence_quote',{'original_evidence_verified':pa.bool_()}),
      'article_events':schema('source_id event_id event_type event_subject event_action event_object key_period key_values conditions evidence_type evidence_quote'),
      'article_industry_impacts':schema('source_id impact_id event_id industry_name_original industry_key industry_scope_quote industry_standard industry_version industry_code industry_name_standard dictionary_sha256 dictionary_version original_evidence_quote summary_evidence_quote evidence_basis industry_scope_status impact_role direction mechanism horizon evidence_type evidence_quote',{'confidence':pa.float64(),'original_evidence_verified':pa.bool_()}),
      'article_market_scopes':schema('source_id scope evidence_quote rationale evidence_basis original_evidence_quote summary_evidence_quote scope_status confidence_interpretation',{'confidence':pa.float64(),'original_evidence_verified':pa.bool_()}),
      'security_master':schema('security_id company_name ticker exchange cik security_type mapping_source verification_status',{'valid_from':dt,'valid_to':dt}),
      'security_industry_history':schema('security_id industry_standard industry_code industry_name mapping_source verification_status',{'valid_from':dt,'valid_to':dt})}


def timestamp(value):
    if not value:return None
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('系统时间缺少时区')
    return result.astimezone(timezone.utc)


def snapshot(root,output_dir='data'):
    """Read only: never construct Dataset (which can recover/migrate on load)."""
    from record_files import Dataset
    if (root/'.runtime/replica_repair/outbox.json').exists():raise ValueError('副本修复尚未提交，暂缓读取投影输入')
    records={}; fingerprints={};markets={}
    for market in ['CN','US','GLOBAL']:
        folder=bounded(root/output_dir/market,root)
        if not folder.exists():continue
        for path in sorted(folder.glob('*.json')):
            raw=path.read_bytes();row=json.loads(raw);Dataset.validate(market,row)
            identity=row['source_id'];fingerprints[path]=digest(raw)
            if identity in records and records[identity]!=row:raise ValueError('同ID正式副本不一致，暂缓投影')
            records[identity]=row;markets.setdefault(identity,set()).add(market)
    result=[]
    for identity,row in sorted(records.items()):
        path=bounded(root/'.runtime/simple/audit'/(identity+'.json'),root);audit={}
        if path.is_file():
            raw=path.read_bytes();fingerprints[path]=digest(raw);value=json.loads(raw)
            if value.get('record_hash')==digest(row):audit=value
        located=markets[identity];market='GLOBAL' if audit.get('canonical_market')=='GLOBAL' or 'GLOBAL' in located or {'CN','US'}<=located else sorted(located)[0]
        result.append((market,row,audit))
    return result,fingerprints


def project(records):
    tables={name:[] for name in schemas()}
    for market,row,audit in records:
        identity=row['source_id'];times=row.get('时间信息',{});events=audit.get('events',[])
        extracted=timestamp(audit.get('extracted_at'));commit=timestamp(audit.get('commit_verified_at'))
        feature=timestamp(times.get('feature_available_at'))
        inferred=timestamp(audit.get('llm_inferred_at'))
        known=[timestamp(times.get(k)) for k in ['first_seen_at','fetched_at']]+[inferred,extracted,commit]
        if feature:feature=max([feature]+[x for x in known if x])
        metadata=dict(source_id=identity,url=row['url'],title=row.get('title'),market=market,
            timezone=None if len(row['时间'])==10 else ('UTC' if row['时间'].endswith('Z') else row['时间'][-6:]),
            source_type=audit.get('source_type'),language=audit.get('language'),published_at=row['时间'],
            publication_precision=times.get('publication_precision','date' if len(row['时间'])==10 else 'unknown'),
            first_seen_at=timestamp(times.get('first_seen_at')),fetched_at=timestamp(times.get('fetched_at')),
            llm_inferred_at=inferred,extracted_at=extracted,commit_verified_at=commit,feature_available_at=feature,
            availability_basis=times.get('availability_basis','unknown'),event_types=sorted({x['event_type'] for x in events}),
            impact_scope=row.get('影响范围',[]),content_hash=audit.get('content_hash'),raw_sha256=audit.get('raw_sha256'),
            schema_version=audit.get('schema_version',SCHEMA_VERSION),record_schema_version=public_schema_version(row),
            prompt_version=audit.get('prompt_version'),model_name=audit.get('model'),model_digest=audit.get('model_digest'),code_version=audit.get('code_version'),
            extraction_version=audit.get('extraction_version'),event_cluster_id=None,summary=row['主要内容'],
            keywords=row['关键词'],record_hash=digest(row),quality_flags=audit.get('relation_quality_flags',[]))
        tables['article_metadata'].append(metadata)
        if not audit:metadata['quality_flags']=sorted(set(metadata['quality_flags']+['matching_audit_unavailable']))
        if audit.get('schema_version')!=SCHEMA_VERSION:metadata['quality_flags']=sorted(set(metadata['quality_flags']+['legacy_relations_unavailable']))
        evidence=audit.get('entity_details') or audit.get('securities',[])
        for entity in row.get('股票实体',[]):
            name,ticker,exchange=entity['名称'],entity['股票代码'],entity['交易所']
            detail=next((x for x in evidence if x.get('name')==name and x.get('ticker')==ticker and x.get('exchange')==exchange),{})
            entity_id='unresolved:'+digest([identity,name,ticker,exchange])[:24]
            tables['article_entities'].append(dict(source_id=identity,entity_id=entity_id,entity_name=name,
                entity_type=detail.get('entity_type','unknown'),entity_role=detail.get('entity_role','unknown'),
                ticker=ticker,exchange=exchange,cik=None,security_id=None,evidence_type='explicit' if detail.get('quote') else 'unresolved',
                evidence_quote=(detail.get('quote') or '')[:350] or None,role_evidence_quote=detail.get('role_quote'),
                mapping_status='unresolved',valid_from=None,valid_to=None))
        industry_details=[]
        for item in row.get('涉及行业',[]):
            matched=[x for x in audit.get('industries',[]) if item['名称'] in (x.get('name'),x.get('industry_name_standard'))]
            industry_details.extend(matched or [dict(name=item['名称'],basis=item['依据'])])
        # A grounded impact can be the sole mention of a code. Preserve that relation rather
        # than requiring Qwen to repeat an identical mention in a second output array.
        for impact in audit.get('industry_impacts',[]):
            if impact.get('industry_code') and impact.get('industry_standard'):
                industry_details.append(dict(impact,name=impact['industry_name_original'],
                    basis=impact['evidence_type'],mapping_status='dictionary_selected_not_stock_membership'))
        seen_industries=set()
        for detail in industry_details:
            identity_key=(detail.get('industry_standard'),detail.get('industry_version'),detail.get('industry_code'),detail['name'])
            if identity_key in seen_industries:continue
            seen_industries.add(identity_key)
            tables['article_industries'].append(dict(source_id=identity,industry_mention_id='industry:'+digest([identity,list(identity_key)])[:24],
                industry_name_original=detail['name'],evidence_type=detail['basis'],industry_standard=detail.get('industry_standard'),industry_code=detail.get('industry_code'),
                industry_key=detail.get('industry_key'),
                industry_name_standard=detail.get('industry_name_standard'),**{k:detail.get(k) for k in ['industry_version','dictionary_sha256','dictionary_version','evidence_basis','original_evidence_quote','summary_evidence_quote','original_evidence_verified']},mapping_status=detail.get('mapping_status','unresolved'),evidence_quote=detail.get('quote')))
        for item in audit.get('scope_evidence',[]):
            tables['article_market_scopes'].append(dict(source_id=identity,**{k:item.get(k) for k in ['scope','evidence_quote','rationale','confidence','evidence_basis','original_evidence_quote','summary_evidence_quote','original_evidence_verified','scope_status','confidence_interpretation']}))
        ids=[]
        for item in events:
            if item['event_type'] not in EVENT_TYPES:raise ValueError('未知事件类型')
            event_id='event:'+digest([identity,item])[:24];ids.append(event_id)
            tables['article_events'].append(dict(source_id=identity,event_id=event_id,event_type=item['event_type'],
                **{'event_'+k:item.get(k) for k in ['subject','action','object']},
                **{k:item.get(k) for k in ['key_period','key_values','conditions','evidence_type']},evidence_quote=item['quote']))
        for item in audit.get('industry_impacts',[]):
            index=item.get('event_index')
            if index is not None and not 0<=index<len(ids):raise ValueError('影响机制事件外键不存在')
            tables['article_industry_impacts'].append(dict(source_id=identity,impact_id='impact:'+digest([identity,item])[:24],
                event_id=ids[index] if index is not None else None,industry_name_original=item['industry_name_original'],
                **{k:item.get(k) for k in ['industry_key','industry_scope_quote','industry_standard','industry_version','industry_code','industry_name_standard','dictionary_sha256','dictionary_version','original_evidence_quote','summary_evidence_quote','evidence_basis','industry_scope_status','original_evidence_verified']},
                **{k:item.get(k) for k in ['impact_role','direction','mechanism','horizon','evidence_type','confidence']},evidence_quote=item['quote']))
    validate_tables(tables)
    return tables


def validate_tables(tables):
    articles={x['source_id'] for x in tables['article_metadata']}
    keys={'article_metadata':['source_id'],'article_entities':['source_id','entity_id'],
        'article_industries':['source_id','industry_mention_id'],'article_events':['source_id','event_id'],
        'article_industry_impacts':['source_id','impact_id'],'article_market_scopes':['source_id','scope'],'security_master':['security_id'],
        'security_industry_history':['security_id','industry_standard','industry_code','valid_from']}
    events={(x['source_id'],x['event_id']) for x in tables['article_events']}
    industries={(x['source_id'],x['industry_name_original']) for x in tables['article_industries']}
    industry_codes={(x['source_id'],x.get('industry_standard'),x.get('industry_version'),x.get('industry_code'))
        for x in tables['article_industries'] if x.get('industry_standard') and x.get('industry_code')}
    for name,rows in tables.items():
        primary=[tuple(x.get(k) for k in keys[name]) for x in rows]
        if len(primary)!=len(set(primary)):raise ValueError(name+'主键重复')
        if name.startswith('article_') and any(x['source_id'] not in articles for x in rows):raise ValueError('文章外键缺失')
    for row in tables['article_industry_impacts']:
        if row['event_id'] and (row['source_id'],row['event_id']) not in events:raise ValueError('事件外键缺失')
        if row.get('industry_standard') and row.get('industry_code'):
            if (row['source_id'],row['industry_standard'],row.get('industry_version'),row['industry_code']) not in industry_codes:
                raise ValueError('行业代码外键缺失')
        elif (row['source_id'],row['industry_name_original']) not in industries:raise ValueError('行业外键缺失')


def recover_export(root):
    path=root/'.runtime/simple/parquet_outbox.json'
    if not path.exists():return False
    op=json.loads(path.read_text(encoding='utf-8'))
    for item in op['files']:
        src=bounded(root/item['staged'],root);dst=bounded(root/item['target'],root)
        if src.parent!=root/'.runtime/simple/parquet_stage' or dst.name not in {x+'.parquet' for x in schemas()}:raise ValueError('投影事务路径不合法')
        if src.is_file():
            if digest(src.read_bytes())!=item['sha256']:raise ValueError('投影暂存哈希不符')
            dst.parent.mkdir(parents=True,exist_ok=True);src.replace(dst)
        if not dst.is_file() or digest(dst.read_bytes())!=item['sha256']:raise ValueError('投影恢复校验失败')
    dump(bounded(root/op['manifest_path'],root),op['manifest'])
    path.unlink();return True


class ProjectionError(RuntimeError):
    """Bounded stage/table diagnostics; never include a source row or response."""
    def __init__(self,stage,table,cause):
        super().__init__('Parquet '+stage+' failed')
        self.stage,self.table,self.cause=stage,table,cause


def export_parquet(root,output_dir='data',directory='structured'):
    root=Path(root).resolve();target=bounded(root/directory,root)
    if target==root or any(x.startswith('.') for x in Path(directory).parts):raise ValueError('结构化目录须为项目内普通子目录')
    recover_export(root)
    try:records,fingerprints=snapshot(root,output_dir);tables=project(records);_,pq=arrow()
    except Exception as error:raise ProjectionError('snapshot_projection',None,error) from error
    stage=root/'.runtime/simple/parquet_stage';stage.mkdir(parents=True,exist_ok=True)
    files=[]
    for name,schema in schemas().items():
        import pyarrow as pa
        phase='table_schema'
        try:
            table=pa.Table.from_pylist(tables[name],schema=schema);path=stage/(name+'.parquet')
            phase='table_write';pq.write_table(table,path,compression='zstd')
            phase='table_verify'
            if not pq.read_table(path).equals(table):raise ValueError('Parquet读回不一致')
        except Exception as error:raise ProjectionError(phase,name,error) from error
        files.append(dict(staged=str(path.relative_to(root)),target=str((target/path.name).relative_to(root)),sha256=digest(path.read_bytes())))
    if any(not p.exists() or digest(p.read_bytes())!=h for p,h in fingerprints.items()):raise ValueError('正式输入在导出期间变动，请重试')
    from market_relations import code_version
    manifest=dict(schema_version=SCHEMA_VERSION,export_code_version=code_version(),
        availability_protocol='observed_feature_availability_not_publication_backfill',
        generation_id=digest([[(x['source_id'],x['record_hash']) for x in tables['article_metadata']],files]),
        exported_at=now(),rows={k:len(v) for k,v in tables.items()},
        empty_reasons={k:('requires_verified_historical_master' if k.startswith('security_') else 'no_verified_relations_in_snapshot') for k,v in tables.items() if not v},
        files={Path(x['target']).name:x['sha256'] for x in files},input_snapshot_hash=digest(sorted((str(p.relative_to(root)),h) for p,h in fingerprints.items())))
    dump(root/'.runtime/simple/parquet_outbox.json',dict(files=files,manifest_path=str((target/'manifest.json').relative_to(root)),manifest=manifest))
    recover_export(root)
    (root/'.runtime/simple/parquet_retry.json').unlink(missing_ok=True)
    return manifest


def export_or_defer(root,output_dir='data',directory='structured'):
    try:return dict(status='completed',**export_parquet(root,output_dir,directory))
    except Exception as error:
        cause=error.cause if isinstance(error,ProjectionError) else error
        result=dict(status='pending',error_type=type(cause).__name__,error_code='dependency_missing' if isinstance(cause,ImportError) else 'validation_failed' if isinstance(cause,ValueError) else 'io_or_export_failure',
            error_stage=getattr(error,'stage','transaction'),error_table=getattr(error,'table',None),
            error_fingerprint=digest([type(cause).__name__,str(cause)]),errno=getattr(cause,'errno',None),
            at=now(),rebuild_from='formal_json_and_matching_sidecars')
        dump(Path(root)/'.runtime/simple/parquet_retry.json',result)
        return result


class ProjectionScheduler:
    """Coalesce updates in one collector process; the durable retry marker survives exit."""
    def __init__(self,root,output_dir,cfg):
        self.root=Path(root);self.output_dir=output_dir;self.cfg=cfg
        self.dirty=0;self.last_attempt=None;self.last_result=None

    def mark(self,count):self.dirty+=count

    def flush(self,force=False):
        if not self.cfg.export_parquet:return {'status':'disabled'}
        manifest=bounded(self.root/self.cfg.directory/'manifest.json',self.root)
        pending=(self.root/'.runtime/simple/parquet_retry.json').exists() or (self.root/'.runtime/simple/parquet_outbox.json').exists()
        if not self.dirty and not pending and manifest.exists():return {'status':'unchanged'}
        current=time.monotonic()
        due=self.last_attempt is None or current-self.last_attempt>=self.cfg.export_interval_seconds
        failed=self.last_result and self.last_result.get('status')=='pending'
        if not force and not due and (failed or self.dirty<self.cfg.export_every_records):
            return dict(status='buffered',pending_records=self.dirty)
        self.last_attempt=current
        result=export_or_defer(self.root,self.output_dir,self.cfg.directory)
        self.last_result=result
        if result['status']=='completed':self.dirty=0
        return result


def read_export(root,directory='structured'):
    """Readers must verify the manifest, rejecting an interrupted generation."""
    root=Path(root).resolve();target=bounded(root/directory,root);pa,pq=arrow()
    manifest=json.loads((target/'manifest.json').read_text(encoding='utf-8'))
    buffers={}
    for name,sha in manifest['files'].items():
        data=(target/name).read_bytes()
        if digest(data)!=sha:raise ValueError('Parquet代际不完整，请先恢复导出')
        buffers[name]=data
    tables={name:pq.read_table(pa.BufferReader(buffers[name+'.parquet'])).to_pylist() for name in schemas()}
    validate_tables(tables);return tables
