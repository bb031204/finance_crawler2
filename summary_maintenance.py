"""Backed-up, writer-locked cleanup of known noise in existing summaries."""
import json
import zipfile
from datetime import datetime,timezone,timedelta
from finance_agent.core import bounded,digest,now
from financial_noise import trim_financial_noise,only_filing_wrapper,NOISE_VERSION


def load_summary_snapshot(root,directory='data'):
    """Read-only inventory; unlike Dataset construction this never recovers an outbox."""
    from types import SimpleNamespace
    from record_files import Dataset,original_time,safe_part,source_key
    root=root.resolve();folder=bounded(root/directory,root)
    view=SimpleNamespace(root=root,rows={},replicas={},industries={})
    for market in ['GLOBAL','CN','US']:
        for path in sorted((folder/market).glob('*.json')):
            row=json.loads(path.read_text(encoding='utf-8'));Dataset.validate(market,row);identity=row['source_id']
            if identity in view.rows:
                if view.rows[identity][1]!=row:raise ValueError('运行中副本变化，稍后重新做只读预览')
                view.rows[identity]=('GLOBAL',row)
            else:view.rows[identity]=(market,row)
            view.replicas.setdefault(identity,{})[market]=path
            prefix=original_time(row['时间'])+'_'+safe_part(row['分类'],30)+'_'
            if path.stem.startswith(prefix):
                tail=path.stem[len(prefix):].split('_')
                if len(tail)>=2 and tail[1]==source_key(row['url']):view.industries[identity]=tail[0]
    return view


def plan_summary_cleanup(dataset):
    operations=[];unverified=[]
    for identity,(market,row) in dataset.rows.items():
        text,tags=trim_financial_noise(row['主要内容'])
        wrapper=only_filing_wrapper(text)
        if not tags and not wrapper:continue
        if '时间信息' not in row or identity not in dataset.industries:
            unverified.append(identity);continue
        path=bounded(dataset.root/'.runtime/simple/audit'/(identity+'.json'),dataset.root)
        audit=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        if audit.get('record_hash')!=digest(row):
            unverified.append(identity);continue
        quarantine=not text.strip()
        operations.append({'source_id':identity,'market':market,'previous':row,'text':text,'tags':tags,
            'quarantine':quarantine,'review_wrapper':wrapper,'review_only':not tags,'audit_path':path,'audit':audit})
    return operations,unverified


def clean_saved_summaries(dataset):
    """Caller holds the project writer lock. No model, downloads or raw-cache deletion."""
    operations,unverified=plan_summary_cleanup(dataset)
    report={'version':NOISE_VERSION,'cleaned_records':0,'cleaned_files':0,'quarantined_records':0,
        'unverified_skipped':unverified,'changes':[],'backup':None,'finished_at':None}
    report['needs_review']=[item['source_id'] for item in operations if item.get('review_wrapper')]
    operations=[item for item in operations if not item.get('review_only')]
    if not operations:report['finished_at']=now();return report
    originals={}
    for item in operations:
        for path in dataset.replicas[item['source_id']].values():originals[path.relative_to(dataset.root).as_posix()]=path.read_bytes()
        path=item['audit_path'];originals[path.relative_to(dataset.root).as_posix()]=path.read_bytes()
    name='summary_cleanup_'+datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d_%H%M%S_%f')+'.zip'
    backup=bounded(dataset.root/'archive'/name,dataset.root);backup.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(backup,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,raw in originals.items():archive.writestr(name,raw)
        archive.writestr('manifest.json',json.dumps({name:digest(raw) for name,raw in originals.items()}))
    with zipfile.ZipFile(backup) as archive:
        if any(archive.read(name)!=raw for name,raw in originals.items()):raise ValueError('摘要清理备份校验失败，未修改原件')
    report['backup']=str(backup)
    for item in operations:
        identity=item['source_id'];row=item['previous'];stamp=now()
        if item['quarantine']:
            target=dataset.quarantine(identity,'全部正文为完整匹配的确定噪声模板，清理后为空；原件已备份')
            report['quarantined_records']+=1
            change={'source_id':identity,'action':'quarantined','path':str(target.relative_to(dataset.root))}
        else:
            if identity not in dataset.industries:raise ValueError('无法保留原产业与文件路径，未继续清理：'+identity)
            updated=dict(row,主要内容=item['text'])
            updated['时间信息']=dict(row['时间信息'],feature_available_at=stamp,availability_basis='migration')
            audit=dict(item['audit']);audit.update(record_hash=digest(updated),feature_available_at=stamp,
                summary_text_cleanup={'version':NOISE_VERSION,'at':stamp,'previous_record_hash':digest(row),'tags':item['tags']})
            dataset.save(item['market'],updated,dataset.industries[identity],audit_update=audit)
            report['cleaned_records']+=1;report['cleaned_files']+=len(dataset.replicas[identity])
            change={'source_id':identity,'action':'cleaned','before_chars':len(row['主要内容']),'after_chars':len(item['text']),'tags':item['tags']}
        report['changes'].append(change)
    report['finished_at']=now();return report
