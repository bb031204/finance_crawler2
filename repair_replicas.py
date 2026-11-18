"""Explicit, backed-up import of proven legacy enrichment into GLOBAL replicas.

No search/model calls, no deletion, no changes to *_original/data_enriched.
Normal collection remains strict and never silently repairs conflicting JSON.
"""
import argparse
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import zipfile
from finance_agent.core import ROOT,Lock,bounded,digest,dump,now
from record_files import Dataset,MARKETS
from record_time import utc_timestamp

CORE=('source_id','url','时间','分类','关键词','主要内容')
OUTBOX='.runtime/replica_repair/outbox.json'


def sha(raw):return hashlib.sha256(raw).hexdigest()
def read(path):return json.loads(path.read_text(encoding='utf-8-sig'))


def scan(root,output_dir='data'):
    root=Path(root).resolve();groups={}
    for market in ['GLOBAL','CN','US']:
        for path in sorted(bounded(root/output_dir/market,root).glob('*.json')):
            path=bounded(path,root);raw=path.read_bytes();row=json.loads(raw)
            Dataset.validate(market,row)
            groups.setdefault(row['source_id'],[]).append((market,path,row,raw))
    return groups


def inspect(root,output_dir='data'):
    """Plan only provenance-proven upgrades, never choose by mtime/majority."""
    root=Path(root).resolve();groups=scan(root,output_dir);operations=[];unresolved=[]
    for identity,items in groups.items():
        if len({digest(row) for _,_,row,_ in items})==1 and len({m for m,_,_,_ in items})==len(items):continue
        try:
            if len(items)!=3 or {m for m,_,_,_ in items}!=set(MARKETS):
                raise ValueError('不是一市场一份的GLOBAL三副本，不自动合并同目录文件')
            first=items[0][2]
            if any(any(row.get(k)!=first.get(k) for k in CORE) for _,_,row,_ in items):
                raise ValueError('来源、原始发布日期或正文/关键词/分类存在差异，不能自动选版本')
            evidence_path=bounded(root/'data_enriched/analysis'/(identity+'.json'),root)
            evidence=read(evidence_path)
            if evidence.get('status')!='completed' or evidence.get('canonical_market')!='GLOBAL':
                raise ValueError('缺少已完成的GLOBAL补齐证据')
            enriched_path=bounded(root/'data_enriched/GLOBAL'/items[0][1].name,root)
            enriched_raw=enriched_path.read_bytes();canonical=json.loads(enriched_raw)
            Dataset.validate('GLOBAL',canonical)
            if (canonical.get('source_id')!=identity or evidence.get('record_hash')!=digest(canonical)
                    or any(canonical.get(k)!=first.get(k) for k in CORE)):
                raise ValueError('补齐输出哈希或原始核心字段不匹配')
            output=evidence_path.parent.parent
            verified_outputs=[]
            for item in evidence.get('outputs',[]):
                p=bounded(output/item['path'],output)
                if p.is_file() and sha(p.read_bytes())==item['sha256'] and read(p)==canonical:
                    verified_outputs.append(p)
            if enriched_path not in verified_outputs:raise ValueError('补齐输出没有匹配的逐文件SHA-256')
            old=[row for _,_,row,_ in items if digest(row)==evidence.get('original_record_hash')]
            audit_path=bounded(root/'.runtime/simple/audit'/(identity+'.json'),root)
            audit=read(audit_path)
            if not old or audit.get('record_hash')!=evidence['original_record_hash']:
                raise ValueError('旧副本与原始记录哈希/审计不匹配')
            # Audits can gain cleanup bookkeeping after enrichment; compare
            # actual source evidence, not their mutable whole-file timestamp.
            for key in ['source_body_hash','raw_sha256','date_quote','date_kind','publication_validation_version']:
                value=evidence.get('legacy_producer',{}).get(key)
                if value is not None and audit.get(key)!=value:
                    raise ValueError('历史来源证据发生变化：'+key)
            for row in [r for _,_,r,_ in items]:
                if digest(row) not in {evidence['original_record_hash'],evidence['record_hash']}:
                    raise ValueError('出现既非原始也非已验证补齐的第三种版本')
                if row.get('title') is not None and row.get('title')!=canonical.get('title'):
                    raise ValueError('原文标题存在差异')
                for key in ['publication_precision','first_seen_at','fetched_at']:
                    if row.get('时间信息',{}).get(key)!=canonical.get('时间信息',{}).get(key):
                        raise ValueError('来源精度或发现/采集时刻不同：'+key)
            for item in evidence.get('original_files',[]):
                p=bounded(root/item['path'],root)
                if p.is_file() and sha(p.read_bytes())!=item['sha256']:
                    raise ValueError('只读原始输入快照缺失或发生变化')
                if not p.is_file() and not any(sha(raw)==item['sha256'] and digest(row)==evidence['original_record_hash']
                                               for _,_,row,raw in items):
                    raise ValueError('原输入已移动，现有旧副本也不匹配原始逐文件SHA-256')
            for key in ['feature_available_at','commit_verified_at','llm_inferred_at']:
                if evidence.get(key):utc_timestamp(evidence[key])
            operations.append({'source_id':identity,'row':canonical,'old_audit':audit,
                'evidence':evidence,'evidence_path':str(evidence_path.relative_to(root)),
                'evidence_sha256':sha(evidence_path.read_bytes()),'enriched_record_hash':digest(canonical),
                'targets':{str(p.relative_to(root)):sha(raw) for _,p,_,raw in items},
                'audit_path':str(audit_path.relative_to(root)),'audit_sha256':sha(audit_path.read_bytes())})
        except (ValueError,OSError,KeyError,TypeError) as error:
            unresolved.append({'source_id':identity,'paths':[str(p.relative_to(root)) for _,p,_,_ in items],
                               'reason':str(error)[:180]})
    return {'unique_records':len(groups),'repairable':len(operations),'unresolved':unresolved,'operations':operations}


def preview(plan):
    return {k:v for k,v in plan.items() if k!='operations'}|{'records':[
        {'source_id':op['source_id'],'targets':list(op['targets']),'evidence_path':op['evidence_path'],
         'before_record_hash':op['old_audit']['record_hash'],'enriched_record_hash':op['enriched_record_hash']}
        for op in plan['operations']]}


def verify_backup(root,operation):
    path=bounded(root/operation['backup'],root)
    if sha(path.read_bytes())!=operation['backup_sha256']:raise ValueError('修复备份哈希不匹配')
    with zipfile.ZipFile(path) as z:
        if z.testzip() is not None:raise ValueError('修复备份ZIP校验失败')
        for name,h in operation['backup_files'].items():
            if sha(z.read(name.replace('\\','/')))!=h:raise ValueError('备份内文件SHA-256不匹配')


def resume(root,output_dir='data'):
    root=Path(root).resolve();outbox=bounded(root/OUTBOX,root);op=read(outbox)
    if op.get('output_dir')!=output_dir:raise ValueError('未完成修复的输出目录不同')
    verify_backup(root,op)
    # Validate every target before any write, including an interrupted attempt.
    for item in op['operations']:
        if item['row'].get('source_id')!=item['source_id'] or item['audit'].get('record_hash')!=digest(item['row']):
            raise ValueError('修复事务的记录/审计哈希不一致')
        for name,before in item['targets'].items():
            path=bounded(root/name,root)
            if path.parent not in [root/output_dir/m for m in MARKETS]:raise ValueError('修复目标超出市场目录')
            Dataset.validate(path.parent.name,item['row'])
            if sha(path.read_bytes())!=before and read(path) not in [item['row'],item.get('provisional_row')]:
                raise ValueError('修复期间目标发生额外修改，已停止：'+name)
        path=bounded(root/item['audit_path'],root)
        if path!=root/'.runtime/simple/audit'/(item['source_id']+'.json'):raise ValueError('修复审计路径不合法')
        if sha(path.read_bytes())!=item['audit_sha256'] and read(path) not in [item['audit'],item.get('provisional_audit')]:
            raise ValueError('修复期间审计发生额外修改，已停止')
    for item in op['operations']:
        def commit():
            for name in item['targets']:dump(bounded(root/name,root),item['row'])
            for name in item['targets']:
                if read(root/name)!=item['row']:raise ValueError('三副本读回校验失败，保留修复事务')
            dump(root/item['audit_path'],item['audit'])
            if read(root/item['audit_path'])!=item['audit']:raise ValueError('审计读回校验失败')
        commit()
        if not item.get('finalized'):
            # The imported content has now been written and read back. This
            # production version becomes available now, not in the article year.
            known=[item['row']['时间信息']['feature_available_at'],now()]
            stamp=max(utc_timestamp(t) for t in known)
            item['provisional_row']=deepcopy(item['row']);item['provisional_audit']=deepcopy(item['audit'])
            item['row']['时间信息']['feature_available_at']=stamp
            item['audit'].update(record_hash=digest(item['row']),feature_available_at=stamp,commit_verified_at=stamp)
            item['finalized']=True;dump(outbox,op)
            commit()
    result={'status':'completed','repaired_records':len(op['operations']),
        'synchronized_files':sum(len(x['targets']) for x in op['operations']),
        'backup':op['backup'],'backup_sha256':op['backup_sha256'],'finished_at':now(),
        'original_core_fields_changed':0,'model_calls':0,'search_requests':0,
        'source_ids':[x['source_id'] for x in op['operations']]}
    dump(root/'.runtime/simple/parquet_retry.json',{'status':'pending','reason':'replicas_imported',
        'at':now(),'rebuild_from':'formal_json_and_matching_sidecars'})
    dump(root/'logs/replica_repair_20261114_result.json',result)
    outbox.unlink()
    return result


def apply(root,output_dir='data'):
    """Caller must hold the project writer lock; safe to resume after interruption."""
    root=Path(root).resolve();outbox=bounded(root/OUTBOX,root)
    if outbox.exists():return resume(root,output_dir)
    plan=inspect(root,output_dir)
    if plan['unresolved']:raise ValueError('存在无法用证据自动统一的冲突，未写入任何副本；请先查看预览')
    if not plan['operations']:return {'status':'unchanged','repaired_records':0}
    stamp=now();files={};operations=[]
    for original in plan['operations']:
        item=deepcopy(original)
        row=item['row'];row['时间信息']['feature_available_at']=max(utc_timestamp(row['时间信息']['feature_available_at']),stamp)
        row['时间信息']['availability_basis']='migration'
        audit=item.pop('old_audit');evidence=item.pop('evidence')
        audit.update(record_hash=digest(row),record_paths=list(item['targets']),canonical_market='GLOBAL',
            feature_available_at=stamp,replica_reconciled_at=stamp,commit_verified_at=None,
            metadata_enrichment={'evidence_path':item['evidence_path'],'evidence_sha256':item['evidence_sha256'],
                'enriched_record_hash':item['enriched_record_hash'],'original_record_hash':evidence['original_record_hash'],
                'analysis_input':evidence.get('analysis_input'),'source_verification':evidence.get('source_verification'),
                'model':evidence.get('model'),'model_digest':evidence.get('model_digest'),
                'code_version':evidence.get('code_version'),'prompt_version':evidence.get('prompt_version'),
                'llm_inferred_at':evidence.get('llm_inferred_at'),
                'prior_feature_available_at':evidence.get('feature_available_at')})
        item['audit']=audit
        for name,h in item['targets'].items():files[name]=(root/name).read_bytes()
        for name in [item['audit_path'],item['evidence_path']]:files[name]=(root/name).read_bytes()
        operations.append(item)
    backup=bounded(root/'archive'/('replica_repair_'+datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')+'.zip'),root)
    backup.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(backup,'x',zipfile.ZIP_DEFLATED) as z:
        for name,raw in files.items():z.writestr(name.replace('\\','/'),raw)
    operation={'output_dir':output_dir,'backup':str(backup.relative_to(root)),
        'backup_sha256':sha(backup.read_bytes()),'backup_files':{n:sha(b) for n,b in files.items()},'operations':operations}
    verify_backup(root,operation);dump(outbox,operation)
    return resume(root,output_dir)


def main(argv=None):
    parser=argparse.ArgumentParser(description='预览或修复有完整补齐证据的GLOBAL三副本；无联网/模型调用')
    parser.add_argument('--apply',action='store_true');parser.add_argument('--preview',action='store_true')
    args=parser.parse_args(argv)
    from simple_crawler import load_config
    cfg=load_config()
    try:
        if args.apply:
            with Lock(ROOT):result=apply(ROOT,cfg.output_dir)
        else:
            result=preview(inspect(ROOT,cfg.output_dir));dump(ROOT/'logs/replica_repair_20261114_plan.json',result)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (ValueError,OSError,RuntimeError) as error:
        print('副本修复未完成：'+str(error));return 1


if __name__=='__main__':raise SystemExit(main())
