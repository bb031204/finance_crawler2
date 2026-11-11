"""Backed-up metadata-only correction of old securities, with recoverable audit writes."""
import json
import zipfile
from datetime import datetime,timezone,timedelta
from finance_agent.core import bounded,digest,now
from security_grounding import grounded_securities,is_security_code,SECURITY_VALIDATION_VERSION


def entity_key(item, public=False):
    fields=['名称','股票代码','交易所'] if public else ['name','ticker','exchange']
    return tuple(item.get(key) or None for key in fields)


def plan_mapping_repairs(dataset):
    operations=[];unverified=[]
    for identity,(market,row) in dataset.rows.items():
        if '股票实体' not in row:continue
        audit_path=bounded(dataset.root/'.runtime/simple/audit'/(identity+'.json'),dataset.root)
        audit=json.loads(audit_path.read_text(encoding='utf-8')) if audit_path.is_file() else {}
        matched=audit.get('record_hash')==digest(row)
        evidence={entity_key(item):item for item in audit.get('securities',[]) if isinstance(item,dict)} if matched else {}
        entities=[];checked_evidence=[];changes=[]
        for original in row['股票实体']:
            item=dict(original);old_key=entity_key(item,True)
            previous=evidence.get(old_key)
            if previous:
                verified=grounded_securities([previous],previous.get('quote',''))
                if verified:
                    checked=verified[0]
                    item=dict(zip(['名称','股票代码','交易所'],entity_key(checked)))
                    checked_evidence.append(checked)
                elif item.get('名称'):
                    item['股票代码']=None;item['交易所']=None
                else:item=None
            elif item['股票代码'] and not is_security_code(item['股票代码']):
                if item['名称']:item['股票代码']=None;item['交易所']=None
                else:item=None
            elif item['股票代码']:
                unverified.append({'source_id':identity,'entity':original,'reason':'无匹配的旧审计实体引文，未按常识重映射'})
            if item!=original:changes.append({'before':original,'after':item})
            if item is not None and item not in entities:entities.append(item)
        if entities!=row['股票实体']:
            if identity not in dataset.industries:raise ValueError('无法保留原文件产业与路径，停止映射修复：'+identity)
            updated=dict(row,股票实体=entities)
            operations.append({'source_id':identity,'market':market,'previous_row':row,'row':updated,
                'audit_path':audit_path,'audit':audit,'audit_matched':matched,'checked_evidence':checked_evidence,'changes':changes})
    return operations,unverified


def repair_saved_mappings(dataset):
    """Caller must hold the writer lock. Never searches, reads a model, or removes raw sources."""
    operations,unverified=plan_mapping_repairs(dataset)
    report={'version':SECURITY_VALIDATION_VERSION,'unique_records':len(dataset.rows),'changed_records':0,
        'changed_files':0,'changed_entities':0,'unverified_entities':unverified,'changes':[],'backup':None,
        'text_reprocessed':False}
    if not operations:return report
    originals={}
    for operation in operations:
        for path in dataset.replicas[operation['source_id']].values():originals[path.relative_to(dataset.root).as_posix()]=path.read_bytes()
        path=operation['audit_path']
        if path.is_file():originals[path.relative_to(dataset.root).as_posix()]=path.read_bytes()
    folder=bounded(dataset.root/'archive',dataset.root);folder.mkdir(exist_ok=True)
    filename='mapping_repair_'+datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d_%H%M%S_%f')+'.zip'
    backup=folder/filename
    with zipfile.ZipFile(backup,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,body in originals.items():archive.writestr(name,body)
        archive.writestr('manifest.json',json.dumps({name:digest(body) for name,body in originals.items()}))
    with zipfile.ZipFile(backup) as archive:
        for name,body in originals.items():
            if archive.read(name)!=body:raise ValueError('股票映射修复备份校验失败，未修改原件')
    report['backup']=str(backup)
    for operation in operations:
        identity=operation['source_id'];stamp=now()
        row=dict(operation['row'])
        row['时间信息']=dict(row['时间信息'],feature_available_at=stamp,availability_basis='migration')
        audit=dict(operation['audit']) if operation['audit_matched'] else {}
        audit.update(source_id=identity,url=row['url'],record_hash=digest(row),source_published_at=row['时间'],feature_available_at=stamp,
            mapping_repair={'version':SECURITY_VALIDATION_VERSION,'at':stamp,'previous_record_hash':digest(operation['previous_row']),
                'evidence_basis':'matching_legacy_audit_quotes' if operation['audit_matched'] else 'invalid_code_format_only',
                'changes':operation['changes'],'text_reprocessed':False})
        if operation['audit_matched']:audit['securities']=operation['checked_evidence']
        dataset.save(operation['market'],row,dataset.industries[identity],audit_update=audit)
        report['changed_records']+=1;report['changed_files']+=len(dataset.replicas[identity]);report['changed_entities']+=len(operation['changes'])
        report['changes'].append({'source_id':identity,'changes':operation['changes'],'paths':[str(p.relative_to(dataset.root)) for p in dataset.replicas[identity].values()]})
    return report
