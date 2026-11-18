"""Opt-in isolated validation; never invokes search or writes production data."""
import argparse
import json
import tempfile
from pathlib import Path
from finance_agent.runtime import setup
ROOT=Path(__file__).resolve().parent
setup(ROOT)
from finance_agent.core import bounded,digest,dump,now
from simple_crawler import load_config
from qwen_agent import Qwen
from web_sources import Sources
from analysis_metadata import public_analysis
from record_files import Dataset,source_id
from record_time import with_time_information
from record_quality import prepare_record_audit,finalize_record
from raw_cleanup import register_page,cleanup_page
from structured_export import export_parquet,read_export


def validate_snapshot():
    """Current formal inputs remain read-only; output is an isolated acceptance artifact."""
    from structured_export import snapshot
    cfg=load_config(ROOT/'config.yaml');records,fingerprints=snapshot(ROOT,cfg.output_dir)
    if any(not path.is_file() or digest(path.read_bytes())!=sha for path,sha in fingerprints.items()):
        raise ValueError('现有采集器刚修改了输入，请稍后重试快照验证')
    root=bounded(ROOT/'.runtime/interface_acceptance',ROOT);created=[]
    try:
        for market,row,audit in records:
            path=bounded(root/'data'/market/(row['source_id']+'.json'),root);dump(path,row);created.append((path,digest(path.read_bytes())))
            if audit:
                path=bounded(root/'.runtime/simple/audit'/(row['source_id']+'.json'),root);dump(path,audit);created.append((path,digest(path.read_bytes())))
        manifest=export_parquet(root);tables=read_export(root)
        report=dict(status='completed',kind='read_only_formal_snapshot',at=now(),rows=manifest['rows'],
            columns={name:list(table[0]) if table else schemas_names(name) for name,table in tables.items()},
            output=str(root/'structured'),original_input_files=len(fingerprints),formal_json_rewritten=0,
            reliable_content_hash_count=sum(bool(x['content_hash']) for x in tables['article_metadata']),
            null_feature_availability=sum(x['feature_available_at'] is None for x in tables['article_metadata']),
            empty_reasons=manifest['empty_reasons'])
        dump(ROOT/'logs/interface_snapshot.json',report);print(json.dumps(report,ensure_ascii=False,indent=2));return 0
    finally:
        for path,sha in created:
            if path.is_file() and digest(path.read_bytes())==sha:bounded(path,root).unlink()


def schemas_names(name):
    from structured_export import schemas
    return schemas()[name].names


def validate(live_source=False):
    cfg=load_config(ROOT/'config.yaml');cfg.ollama.max_calls_per_cycle=12
    parent=bounded(ROOT/'.runtime/interface_validation',ROOT);parent.mkdir(parents=True,exist_ok=True)
    report=dict(started_at=now(),kind='real_download_and_local_qwen' if live_source else 'synthetic_source_with_real_local_qwen',search_calls=0)
    with tempfile.TemporaryDirectory(dir=parent) as directory:
        root=bounded(Path(directory),ROOT);model=Qwen(cfg.ollama,root);sources=Sources(cfg,root);page=None;path=None
        try:
            if live_source:
                candidate=dict(url='https://www.federalreserve.gov/newsevents/pressreleases/monetary20240918a.htm',title='Federal Reserve issues FOMC statement',origin_market='US',first_seen_at=now())
                page=sources.fetch(candidate)
            else:
                candidate=dict(url='https://example.com/synthetic-market-interface',title='Example Motors quarterly operating update',origin_market='US',first_seen_at=now())
                page=dict(url=candidate['url'],title=candidate['title'],source_title=candidate['title'],origin_market='US',fetched_at=now(),
                    date_context='Published November 10, 2026',text='Example Motors (NYSE: EXM) reported third-quarter 2026 revenue of $12 million, up 8 percent from the same period in 2025. Battery input costs rose 5 percent from the prior year. Nova Cells supplies Example Motors with batteries under a three-year supply agreement signed in October 2026. Management expects battery deliveries to increase in the fourth quarter but noted that higher lithium prices could increase manufacturing costs. The company has not changed its full-year revenue guidance. The supply agreement does not guarantee minimum order volumes.')
            identity=source_id(page['url']);path=root/'.runtime/simple/pages'/(identity+'.json');dump(path,page);register_page(root,path,page,identity)
            note=model.summarize(page,cfg.monitoring)
            if not note.get('useful') or not note.get('time'):raise ValueError('验证样本未形成有效摘要/发布日期')
            row=with_time_information(dict(source_id=identity,url=page['url'],title=page.get('source_title',page['title']),
                时间=note['time'],分类=note['category'],关键词=note['keywords'],主要内容=note['content'],**public_analysis(note)),candidate,page)
            ds=Dataset(root,'data');audit=prepare_record_audit(root,row,note,page,candidate,model,note['market'])
            ds.save(note['market'],row,note['industry'],audit)
            projection=export_parquet(root)
            audit=finalize_record(root,ds,row,note,page,candidate,model,path)
            tables=read_export(root)
            report.update(status='completed',model_calls=model.calls,source_url=page['url'],body_hash=digest(page['text'].encode('utf-8')),
                llm_inferred_at=audit.get('llm_inferred_at'),code_version=audit.get('code_version'),
                cleaning=page.get('cleaning','synthetic'),raw_cleanup_status=audit['raw_cleanup_status'],raw_page_exists=path.exists(),
                row=row,relations={k:audit.get(k) for k in ['entity_details','events','industry_impacts','relation_quality_flags']},
                parquet_rows=projection['rows'],model_digest=model.model_digest,
                feature_available_at=tables['article_metadata'][0]['feature_available_at'].isoformat())
            report['raw_deleted_paths']=audit['raw_deleted_paths'];report['raw_deleted_at']=audit['raw_deleted_at']
        except Exception as error:
            report.update(status='failed',error_type=type(error).__name__,error=str(error)[:220],model_calls=model.calls)
        finally:
            if path is not None and page is not None:
                proof=cleanup_page(root,path,page,source_id(page['url']),'validation_finally')
                report['validation_finally_cleanup']=proof['raw_cleanup_status'];report['raw_page_exists']=path.exists()
            model.close();sources.close()
    report['finished_at']=now();report['isolated_directory_removed']=not root.exists()
    dump(ROOT/'logs'/('interface_live_source.json' if live_source else 'interface_local_qwen.json'),report)
    print(json.dumps({k:v for k,v in report.items() if k not in {'row','relations'}},ensure_ascii=False,indent=2))
    return 0 if report['status']=='completed' else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='隔离小样本验证：真实本机Qwen；可选下载一篇已知美联储页面，不搜索、不写正式data')
    modes=parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--local-qwen',action='store_true');modes.add_argument('--live-source',action='store_true')
    modes.add_argument('--snapshot',action='store_true',help='只读现有正式数据，隔离生成Parquet验收快照，不调用模型/搜索')
    args=parser.parse_args()
    raise SystemExit(validate_snapshot() if args.snapshot else validate(args.live_source))
